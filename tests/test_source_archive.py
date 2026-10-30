"""A source archive must contain the reviewed files before it is extracted."""
import importlib.util
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('source_archive',
    Path(__file__).resolve().parents[1] / 'scripts/check-source-archive.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
PREFIX = 'atraxis-1.0.0rc1'


class SourceArchiveTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.files = {'pyproject.toml': b'[project]\nname="atraxis"\nversion="1.0.0rc1"\n',
                      'scripts/fixture.py': b'print("fixture")\n'}
        for name, data in self.files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        self.archive = self.root / 'source.tar.gz'
        self.output = self.root / 'extracted'
        mock = patch.object(MODULE.subprocess, 'check_output',
                            return_value=('\0'.join(self.files) + '\0').encode())
        mock.start()
        self.addCleanup(mock.stop)

    def write_archive(self, files=None, extra=None):
        with tarfile.open(self.archive, 'w:gz') as archive:
            for name, data in (self.files if files is None else files).items():
                member = tarfile.TarInfo(f'{PREFIX}/{name}')
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            if extra is not None:
                archive.addfile(extra, io.BytesIO(b''))

    def verify(self):
        return MODULE.verify_and_extract(self.root, self.archive, self.output)

    def test_verified_source_extracts_without_changing_bytes(self):
        self.write_archive()
        extracted = self.verify()
        for name, data in self.files.items():
            self.assertEqual((extracted / name).read_bytes(), data)

    def test_missing_or_changed_file_rejects_before_extraction(self):
        for mode in ['missing', 'changed']:
            with self.subTest(mode=mode):
                files = dict(self.files)
                if mode == 'missing':
                    del files['scripts/fixture.py']
                else:
                    files['scripts/fixture.py'] = b'substituted'
                self.write_archive(files)
                with self.assertRaisesRegex(ValueError, f'{mode}_source_file'):
                    self.verify()
                self.assertFalse(self.output.exists())

    def test_unsafe_or_duplicate_member_rejects_before_extraction(self):
        for name in [f'{PREFIX}/../escape', '/absolute', 'other/file', f'{PREFIX}/pyproject.toml',
                     f'{PREFIX}/./pyproject.toml', f'{PREFIX}//pyproject.toml']:
            with self.subTest(name=name):
                self.write_archive(extra=tarfile.TarInfo(name))
                with self.assertRaisesRegex(ValueError, '(invalid|duplicate)_source_member'):
                    self.verify()
                self.assertFalse(self.output.exists())
        link = tarfile.TarInfo(f'{PREFIX}/link')
        link.type = tarfile.SYMTYPE
        link.linkname = 'pyproject.toml'
        self.write_archive(extra=link)
        with self.assertRaisesRegex(ValueError, 'invalid_source_member'):
            self.verify()
        self.assertFalse(self.output.exists())

    def test_empty_or_symlink_archive_is_rejected(self):
        self.archive.touch()
        with self.assertRaisesRegex(ValueError, 'invalid_source_archive'):
            self.verify()
        self.archive.unlink()
        self.archive.symlink_to(self.root / 'pyproject.toml')
        with self.assertRaisesRegex(ValueError, 'invalid_source_archive'):
            self.verify()


if __name__ == '__main__':
    unittest.main()
