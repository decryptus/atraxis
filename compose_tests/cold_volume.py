"""Bounded archive helper for disposable Compose tests, not an operator API."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tarfile

MAX_BYTES = 32 * 1024 * 1024
MAX_ENTRIES = 512


def fingerprint(root):
    result = {}
    for path in sorted(Path(root).rglob('*')):
        info = path.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError('unsupported_archive_entry')
        result[path.relative_to(root).as_posix()] = dict(
            mode=stat.S_IMODE(info.st_mode), uid=info.st_uid, gid=info.st_gid,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)
    return result


def pack(root, destination):
    root, destination = Path(root), Path(destination)
    entries = fingerprint(root)
    if len(entries) > MAX_ENTRIES or sum((root / n).stat().st_size for n in entries if (root / n).is_file()) > MAX_BYTES:
        raise ValueError('archive_limit')
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as output, tarfile.open(fileobj=output, mode='w') as archive:
        for name in entries:
            archive.add(root / name, arcname=name, recursive=False)
    return hashlib.sha256(destination.read_bytes()).hexdigest()


def restore(root, source, digest):
    root, source = Path(root), Path(source)
    if source.is_symlink() or source.stat().st_size > MAX_BYTES + 1024 * 1024:
        raise ValueError('archive_limit')
    if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
        raise ValueError('archive_digest_mismatch')
    if root.is_symlink() or not root.is_dir() or any(root.iterdir()):
        raise ValueError('restore_destination_not_empty')
    with tarfile.open(source, 'r:') as archive:
        entries = archive.getmembers()
        names = set()
        total = 0
        for item in entries:
            name = PurePosixPath(item.name)
            if (name.is_absolute() or '..' in name.parts or str(name) != item.name
                    or item.name in ('', '.') or item.name in names
                    or not (item.isdir() or item.isfile())
                    or item.mode & ~0o777 or item.uid != 10001 or item.gid != 10001):
                raise ValueError('unsupported_archive_entry')
            names.add(item.name)
            total += item.size
        if len(entries) > MAX_ENTRIES or total > MAX_BYTES:
            raise ValueError('archive_limit')
        for item in entries:
            path = root / item.name
            path.parent.mkdir(parents=True, exist_ok=True)
            if item.isdir():
                path.mkdir(exist_ok=True)
            else:
                with archive.extractfile(item) as input_stream, path.open('xb') as output:
                    while data := input_stream.read(65536):
                        output.write(data)
            if (path.stat().st_uid, path.stat().st_gid) != (10001, 10001):
                os.chown(path, 10001, 10001)
            path.chmod(item.mode)
    return fingerprint(root)


if __name__ == '__main__':
    command = sys.argv[1]
    if command == 'fingerprint':
        result = fingerprint('/data')
    elif command == 'pack':
        result = pack('/data', '/backup/' + sys.argv[2] + '.tar')
    elif command == 'restore':
        result = restore('/data', '/backup/' + sys.argv[2] + '.tar', sys.argv[3])
    else:
        raise ValueError('unsupported_archive_command')
    print(json.dumps(result, sort_keys=True))
