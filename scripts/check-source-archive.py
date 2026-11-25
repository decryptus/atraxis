"""Verify that the sdist holds exactly the allow-listed tracked source bytes, then extract it."""
import argparse
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tomllib

MAX_ARCHIVE_BYTES = 10 * 1024 * 1024
# Tracked files that the source distribution must carry byte for byte.
SOURCE_FILES = frozenset({'README.md', 'LICENSE', 'pyproject.toml', 'MANIFEST.in'})
SOURCE_PREFIXES = ('atraxis/',)
# Metadata that setuptools generates while building the archive.
GENERATED_FILES = frozenset({'PKG-INFO', 'setup.cfg'})
GENERATED_PREFIXES = ('atraxis.egg-info/',)
_EXCLUDED_SUFFIXES = ('.pyc', '.pyo', '.pyd')


def _is_source_file(name):
    return (name in SOURCE_FILES or name.startswith(SOURCE_PREFIXES)) and not (
        name.endswith(_EXCLUDED_SUFFIXES) or '/__pycache__/' in f'/{name}')


def _is_generated_file(name):
    return name in GENERATED_FILES or name.startswith(GENERATED_PREFIXES)


def verify_and_extract(root: Path, archive: Path, output: Path) -> Path:
    metadata = tomllib.loads((root / 'pyproject.toml').read_text())['project']
    prefix = f"{metadata['name']}-{metadata['version']}"
    if archive.is_symlink() or not archive.is_file() or not 0 < archive.stat().st_size <= MAX_ARCHIVE_BYTES:
        raise ValueError('invalid_source_archive')
    tracked = subprocess.check_output(
        ['git', 'ls-files', '-z'], cwd=root, timeout=10).decode().split('\0')
    expected = {name: (root / name).read_bytes() for name in tracked if name and _is_source_file(name)}
    with tarfile.open(archive, 'r:gz') as source:
        members = source.getmembers()
        names = [member.name for member in members]
        if len(set(names)) != len(names):
            raise ValueError('duplicate_source_member')
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or str(path) != member.name or '..' in path.parts or not path.parts
                    or path.parts[0] != prefix or not (member.isfile() or member.isdir())):
                raise ValueError('invalid_source_member')
            relative = '/'.join(path.parts[1:])
            if member.isfile() and relative not in expected and not _is_generated_file(relative):
                raise ValueError(f'unexpected_source_file: {relative}')
        for name, content in expected.items():
            try:
                member = source.getmember(f'{prefix}/{name}')
            except KeyError:
                raise ValueError(f'missing_source_file: {name}') from None
            if not member.isfile() or source.extractfile(member).read() != content:
                raise ValueError(f'changed_source_file: {name}')
        output.mkdir(parents=True, exist_ok=False)
        source.extractall(output, filter='data')
    print(f'Verified {len(expected)} allow-listed tracked files in source archive; extracted to {output / prefix}')
    return output / prefix


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    verify_and_extract(Path(__file__).resolve().parents[1], args.archive, args.output)
