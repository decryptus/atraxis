"""Publication guard rejects unqualified or substituted package artifacts."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

_SPEC = importlib.util.spec_from_file_location('candidate_release',
    Path(__file__).resolve().parents[1] / 'scripts/prepare-candidate-release.py')
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)


class CandidateReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'pyproject.toml').write_text('[project]\nname="atraxis"\nversion="1.0.0rc1"\n')
        (self.root / 'candidate-ci.json').write_text(json.dumps(dict(
            tested_commit='a' * 40, release_commit='b' * 40, source_tree='c' * 40)))
        self.download = self.root / 'candidate-download'
        (self.download / 'dist').mkdir(parents=True)
        (self.download / 'package-evidence').mkdir()
        self.manifest = dict(source_commit='a' * 40, source_tree='c' * 40,
                             distribution='atraxis', version='1.0.0rc1', files={})
        for name in ['atraxis-1.0.0rc1-py3-none-any.whl', 'atraxis-1.0.0rc1.tar.gz']:
            data = ('synthetic archive ' + name).encode()
            (self.download / 'dist' / name).write_bytes(data)
            self.manifest['files']['dist/' + name] = dict(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        self.save_manifest()

    def save_manifest(self):
        (self.download / 'package-evidence/manifest.json').write_text(json.dumps(self.manifest))

    def test_exact_artifacts_are_preserved_with_checksums(self):
        _MODULE.prepare(self.root)
        assets = self.root / 'candidate-assets'
        for name in self.manifest['files']:
            self.assertEqual((self.download / name).read_bytes(), (assets / Path(name).name).read_bytes())
        for line in (assets / 'SHA256SUMS').read_text().splitlines():
            digest, name = line.split('  ')
            self.assertEqual(digest, hashlib.sha256((assets / name).read_bytes()).hexdigest())
        self.assertFalse(json.loads((assets / 'candidate-provenance.json').read_text())['operational_gates_closed'])

    def test_other_source_or_tree_or_version_is_rejected(self):
        for field in ['source_commit', 'source_tree', 'version', 'distribution']:
            with self.subTest(field=field):
                original = self.manifest[field]
                self.manifest[field] = 'different'
                self.save_manifest()
                with self.assertRaisesRegex(ValueError, 'package_identity'):
                    _MODULE.prepare(self.root)
                self.manifest[field] = original
        self.assertFalse((self.root / 'candidate-assets').exists())

    def test_changed_archive_is_rejected_before_staging(self):
        file = next((self.download / 'dist').iterdir())
        file.write_bytes(b'substituted')
        with self.assertRaisesRegex(ValueError, 'archive_digest'):
            _MODULE.prepare(self.root)
        self.assertFalse((self.root / 'candidate-assets').exists())

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

    def test_stable_version_cannot_use_candidate_workflow(self):
        (self.root / 'pyproject.toml').write_text('[project]\nname="atraxis"\nversion="1.0.0"\n')
        with self.assertRaisesRegex(ValueError, 'prerelease_version'):
            _MODULE.prepare(self.root)


if __name__ == '__main__':
    unittest.main()
