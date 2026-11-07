"""Validate published release assets and stage only missing PyPI distributions."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

RELEASE = 'f99d099019b01ebb27ef1023d9c0c347c198d980'
VERSION = '1.0.8'
PROVENANCE = 'release-provenance.json'
FILES = {
    'atraxis-1.0.8-py3-none-any.whl': '7fbca81ff3635965cfcca9f917e08fd0ec24687b6c8a771f582da75ea6c26cd4',
    'atraxis-1.0.8.tar.gz': '1267a7a67d18a1a51af36ae6ae5ebe4eb10625eac66d5e788d4d7c59f0af0fe6',
}
ASSETS = {**FILES, PROVENANCE: '9f693974748c032d6d41de5e36ef7000a2ad5a81a760725f8d7f0818266c1d01',
          'SHA256SUMS': '678b316f188004185ff9a5344f031f7b3b0b300d8ceb449d09f269537bf127e1'}
LIMIT = 10 * 1024 * 1024


def fetch(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError('registry_response_too_large')
    return data


def verify_release(source):
    if {p.name for p in source.iterdir()} != set(ASSETS):
        raise ValueError('unexpected_release_files')
    for name, digest in ASSETS.items():
        path = source / name
        if path.is_symlink() or not 0 < path.stat().st_size <= LIMIT or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('release_digest_mismatch')
    report = json.loads((source / PROVENANCE).read_bytes())
    if (report['version'] != VERSION or report['release_commit'] != RELEASE
            or report['prerelease'] or not report['operational_gates_closed']):
        raise ValueError('qualified_release_required')


def existing_files():
    try:
        release = json.loads(fetch('https://pypi.org/pypi/atraxis/' + VERSION + '/json'))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return set()
        raise
    if release['info']['name'].lower() != 'atraxis' or release['info']['version'] != VERSION:
        raise ValueError('unexpected_registry_release')
    present = set()
    for entry in release['urls']:
        name = entry['filename']
        url = urlsplit(entry['url'])
        if (name not in FILES or name in present or entry['yanked']
                or entry['digests']['sha256'] != FILES[name] or url.scheme != 'https'
                or url.hostname != 'files.pythonhosted.org'
                or hashlib.sha256(fetch(entry['url'])).hexdigest() != FILES[name]):
            raise ValueError('registry_file_mismatch')
        present.add(name)
    return present


if __name__ == '__main__':
    if sys.argv[1:] == ['--verify']:
        if existing_files() != set(FILES):
            raise SystemExit('PyPI release incomplete')
    elif sys.argv[1:] == ['--stage']:
        source = Path('release-assets')
        verify_release(source)
        missing = set(FILES) - existing_files()
        upload = Path('pypi-upload')
        upload.mkdir()
        for name in sorted(missing):
            shutil.copyfile(source / name, upload / name)
    else:
        raise SystemExit('Use --stage or --verify')
