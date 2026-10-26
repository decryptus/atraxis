"""Reject incomplete, altered or wrong-host CI recovery evidence before import."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / 'compose_tests/recovery_transfer.py'
SPEC = importlib.util.spec_from_file_location('recovery_transfer', MODULE)
transfer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transfer)
COMMIT = 'a' * 40
DESTINATION = dict(boot='new-boot', daemon='new-daemon')


class RecoveryTransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in transfer.FILES:
            (self.root / name).write_bytes(b'bounded synthetic fixture')
        self.manifest = dict(version=1, synthetic_only=True, source_commit=COMMIT,
            source=dict(boot='old-boot', daemon='old-daemon'),
            images={name: 'sha256:' + 'b' * 64 for name in transfer.IMAGES},
            fingerprints={role: {} for role in ('web', 'engine', 'site')},
            files={name: dict(sha256=transfer.digest(self.root / name, transfer.limit(name)),
                size=(self.root / name).stat().st_size) for name in transfer.FILES})
        self.seal()

    def seal(self):
        (self.root / transfer.MANIFEST).write_text(json.dumps(self.manifest))
        self.digest = transfer.digest(self.root / transfer.MANIFEST, transfer.MAX_MANIFEST_BYTES)

    def verify(self, **kwargs):
        return transfer.verify(self.root, kwargs.get('digest', self.digest),
            kwargs.get('commit', COMMIT), kwargs.get('destination', DESTINATION))

    def test_valid_complete_transfer(self):
        self.assertEqual(self.verify(), self.manifest)

    def test_manifest_must_match_independent_workflow_output(self):
        self.manifest['synthetic_only'] = False
        (self.root / transfer.MANIFEST).write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, 'transfer_manifest_mismatch'):
            self.verify()

    def test_changed_archive_and_context_are_rejected(self):
        for name in ('web.tar', 'context.json', 'images.tar'):
            with self.subTest(name=name):
                path = self.root / name
                original = path.read_bytes()
                path.write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'transfer_file_mismatch'):
                    self.verify()
                path.write_bytes(original)

    def test_missing_or_extra_files_are_rejected(self):
        path = self.root / 'site.tar'
        path.rename(self.root / 'unexpected.tar')
        with self.assertRaisesRegex(ValueError, 'transfer_file_set_invalid'):
            self.verify()

    def test_links_are_rejected(self):
        path = self.root / 'web.tar'
        path.unlink()
        path.symlink_to(self.root / 'engine.tar')
        with self.assertRaisesRegex(ValueError, 'transfer_file_invalid'):
            self.verify()

    def test_oversized_file_rejected_before_read(self):
        with (self.root / 'site.tar').open('wb') as stream:
            stream.truncate(transfer.MAX_ARCHIVE_BYTES + 1)
        with self.assertRaisesRegex(ValueError, 'transfer_file_invalid'):
            self.verify()

    def test_wrong_revision_or_non_synthetic_manifest_rejected(self):
        with self.assertRaisesRegex(ValueError, 'transfer_manifest_invalid'):
            self.verify(commit='c' * 40)
        self.manifest['synthetic_only'] = False
        self.seal()
        with self.assertRaisesRegex(ValueError, 'transfer_manifest_invalid'):
            self.verify()

    def test_both_boot_and_daemon_must_be_different(self):
        for destination in (dict(boot='old-boot', daemon='new-daemon'),
                dict(boot='new-boot', daemon='old-daemon'), dict(boot='', daemon='')):
            with self.subTest(destination=destination):
                with self.assertRaisesRegex(ValueError, 'transfer_requires_another_host'):
                    self.verify(destination=destination)

    def test_unpinned_image_and_invalid_digest_rejected(self):
        with self.assertRaisesRegex(ValueError, 'transfer_digest_invalid'):
            self.verify(digest='not-a-digest')
        self.manifest['images'][transfer.IMAGES[0]] = 'latest'
        self.seal()
        with self.assertRaisesRegex(ValueError, 'transfer_image_invalid'):
            self.verify()
