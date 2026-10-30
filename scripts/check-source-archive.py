"""Verify tracked source bytes in the sdist, then extract for offline tests."""
import argparse
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import tomllib

MAX_ARCHIVE_BYTES = 10 * 1024 * 1024


def verify_and_extract(root: Path, archive: Path, output: Path) -> Path:
    metadata = tomllib.loads((root / 'pyproject.toml').read_text())['project']
    prefix = f"{metadata['name']}-{metadata['version']}"
    if archive.is_symlink() or not archive.is_file() or not 0 < archive.stat().st_size <= MAX_ARCHIVE_BYTES:
        raise ValueError('invalid_source_archive')
    tracked = subprocess.check_output(
        ['git', 'ls-files', '-z'], cwd=root, timeout=10).decode().split('\0')
    expected = {name: (root / name).read_bytes() for name in tracked if name}
    with tarfile.open(archive, 'r:gz') as source:
        members = source.getmembers()
        names = [member.name for member in members]
        if len(set(names)) != len(names):
            raise ValueError('duplicate_source_member')
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or '..' in path.parts or not path.parts
                    or path.parts[0] != prefix or not (member.isfile() or member.isdir())):
                raise ValueError('invalid_source_member')
        for name, content in expected.items():
            try:
                member = source.getmember(f'{prefix}/{name}')
            except KeyError:
                raise ValueError(f'missing_source_file: {name}') from None
            if not member.isfile() or source.extractfile(member).read() != content:
                raise ValueError(f'changed_source_file: {name}')
        output.mkdir(parents=True, exist_ok=False)
        source.extractall(output, filter='data')
    print(f'Verified {len(expected)} tracked files in source archive; extracted to {output / prefix}')
    return output / prefix


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    verify_and_extract(Path(__file__).resolve().parents[1], args.archive, args.output)
