"""Reject changed package inputs, copied wheels and unexpected installed versions."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'deploy/compose/locks/record-environment.py'
SPEC = importlib.util.spec_from_file_location('compose_environment', SCRIPT)
environment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(environment)


class ComposeEnvironmentTests(unittest.TestCase):
    def test_hash_locked_download_rejects_changed_archive(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            sources = root / 'sources'
            sources.mkdir()
            archive = sources / 'example.whl'
            archive.write_bytes(b'original wheel')
            digest = environment.file_identity(archive)['sha256']
            lock = root / 'requirements.txt'
            lock.write_text('example==1 --hash=sha256:' + digest + '\n')
            environment.locked_downloads(sources, lock)
            archive.write_bytes(b'changed wheel')
            with self.assertRaisesRegex(ValueError, 'archive_mismatch'):
                environment.locked_downloads(sources, lock)

    def test_copied_wheel_set_rejects_missing_extra_and_changed_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / 'example.whl'
            wheel.write_bytes(b'qualified wheel')
            expected = environment.file_set(root)
            environment.verify_wheels(root, expected)
            wheel.unlink()
            with self.assertRaises(ValueError):
                environment.verify_wheels(root, expected)
            wheel.write_bytes(b'qualified wheel')
            extra = root / 'extra.whl'
            extra.write_bytes(b'unexpected')
            with self.assertRaisesRegex(ValueError, 'wheels_mismatch'):
                environment.verify_wheels(root, expected)
            extra.unlink()
            wheel.write_bytes(b'different wheel')
            with self.assertRaisesRegex(ValueError, 'wheels_mismatch'):
                environment.verify_wheels(root, expected)

    def test_file_identity_refuses_symlink_and_size_over_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / 'archive'
            archive.write_bytes(b'1234')
            link = root / 'link'
            link.symlink_to(archive)
            for path, limit in ((link, 4), (archive, 3)):
                with self.subTest(path=path.name):
                    with self.assertRaisesRegex(ValueError, 'file_invalid'):
                        environment.file_identity(path, limit)

    def test_version_comparison_normalizes_names_but_rejects_drift(self):
        expected = {'PyYAML': '6.0.3', 'typing_extensions': '4.16.0'}
        actual = {'pyyaml': '6.0.3', 'typing-extensions': '4.16.0'}
        environment.verify_versions(actual, expected)
        for changed in (dict(actual, pyyaml='6.0.4'), dict(actual, unexpected='1'), {'pyyaml': '6.0.3'}):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(ValueError, 'versions_mismatch'):
                    environment.verify_versions(changed, expected)
