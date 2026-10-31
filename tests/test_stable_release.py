"""Publication guard rejects unqualified or substituted package artifacts."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

_SPEC = importlib.util.spec_from_file_location('stable_release',
    Path(__file__).resolve().parents[1] / 'scripts/prepare-stable-release.py')
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


class StableReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'pyproject.toml').write_text('[project]\nname="atraxis"\nversion="1.0.0"\n')
        (self.root / 'stable-ci.json').write_text(json.dumps(dict(
            tested_commit='a' * 40, release_commit='b' * 40, source_tree='c' * 40)))
        qualification=self.root/'docs/releases'
        qualification.mkdir(parents=True)
        (self.root/'atraxis').mkdir()
        (self.root/'atraxis/__init__.py').write_text('# fixture\n')
        (qualification/'1.0.0-qualification.json').write_text(json.dumps(dict(schema=1,version='1.0.0',
            gates={'G1':'closed','G2':'closed','G3':'closed'},support_matrix='fixture',evidence_sha256={'fixture':'d'*64},
            runtime_files_sha256={'atraxis/__init__.py':hashlib.sha256(b'# fixture\n').hexdigest()})))
        self.download = self.root / 'stable-download'
        (self.download / 'dist').mkdir(parents=True)
        (self.download / 'package-evidence').mkdir()
        self.manifest = dict(source_commit='a' * 40, source_tree='c' * 40,
                             distribution='atraxis', version='1.0.0', files={})
        for name in ['atraxis-1.0.0-py3-none-any.whl', 'atraxis-1.0.0.tar.gz']:
            data = ('synthetic archive ' + name).encode()
            (self.download / 'dist' / name).write_bytes(data)
            self.manifest['files']['dist/' + name] = dict(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        self.save_manifest()

    def save_manifest(self):
        (self.download / 'package-evidence/manifest.json').write_text(json.dumps(self.manifest))

    def test_exact_artifacts_are_preserved_with_checksums(self):
        _MODULE.prepare(self.root)
        assets = self.root / 'stable-assets'
        for name in self.manifest['files']:
            self.assertEqual((self.download / name).read_bytes(), (assets / Path(name).name).read_bytes())
        for line in (assets / 'SHA256SUMS').read_text().splitlines():
            digest, name = line.split('  ')
            self.assertEqual(digest, hashlib.sha256((assets / name).read_bytes()).hexdigest())
        self.assertTrue(json.loads((assets / 'release-provenance.json').read_text())['operational_gates_closed'])

    def test_other_source_or_tree_or_version_is_rejected(self):
        for field in ['source_commit', 'source_tree', 'version', 'distribution']:
            with self.subTest(field=field):
                original = self.manifest[field]
                self.manifest[field] = 'different'
                self.save_manifest()
                with self.assertRaisesRegex(ValueError, 'package_identity'):
                    _MODULE.prepare(self.root)
                self.manifest[field] = original
        self.assertFalse((self.root / 'stable-assets').exists())

    def test_changed_archive_is_rejected_before_staging(self):
        file = next((self.download / 'dist').iterdir())
        file.write_bytes(b'substituted')
        with self.assertRaisesRegex(ValueError, 'archive_digest'):
            _MODULE.prepare(self.root)
        self.assertFalse((self.root / 'stable-assets').exists())

    def test_missing_extra_or_symlink_archive_is_rejected(self):
        file = next((self.download / 'dist').iterdir())
        original = file.read_bytes()
        file.unlink()
        with self.assertRaisesRegex(ValueError, 'unexpected_archive'):
            _MODULE.prepare(self.root)
        file.write_bytes(original)
        extra = self.download / 'dist/extra.whl'
        extra.write_bytes(b'extra')
        with self.assertRaisesRegex(ValueError, 'unexpected_archive'):
            _MODULE.prepare(self.root)
        extra.unlink()
        external = self.root / 'external'
        external.write_bytes(original)
        file.unlink()
        file.symlink_to(external)
        with self.assertRaisesRegex(ValueError, 'invalid_archive'):
            _MODULE.prepare(self.root)

    def test_candidate_version_cannot_use_stable_workflow(self):
        (self.root / 'pyproject.toml').write_text('[project]\nname="atraxis"\nversion="1.0.0rc1"\n')
        with self.assertRaisesRegex(ValueError, 'stable_version'):
            _MODULE.prepare(self.root)

    def test_changed_runtime_cannot_reuse_operational_qualification(self):
        (self.root/'atraxis/__init__.py').write_text('# different runtime\n')
        with self.assertRaisesRegex(ValueError,'operational_runtime_identity'):
            _MODULE.prepare(self.root)
        self.assertFalse((self.root/'stable-assets').exists())

    def test_open_operational_gate_or_invalid_evidence_is_refused(self):
        path=self.root/'docs/releases/1.0.0-qualification.json'
        document=json.loads(path.read_text())
        document['gates']['G3']='open'
        path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError,'reviewed_operational'):
            _MODULE.prepare(self.root)
        document['gates']['G3']='closed'
        document['evidence_sha256']['fixture']='unverified'
        path.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError,'reviewed_operational'):
            _MODULE.prepare(self.root)
        self.assertFalse((self.root/'stable-assets').exists())


if __name__ == '__main__':
    unittest.main()
