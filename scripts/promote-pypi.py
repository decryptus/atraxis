"""Validate published release assets and stage only missing PyPI distributions."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

RELEASE = '8e51fa15fa9c9348d8d253ac300ba90493ecf97f'
VERSION = '1.0.7'
PROVENANCE = 'release-provenance.json'
FILES = {
    'atraxis-1.0.7-py3-none-any.whl': '771ecf96fdcb6761b9567fec60914979f11242cf4c66426d4153405e00db766a',
    'atraxis-1.0.7.tar.gz': 'ebf1c572cf2bbc99f336847481dd4f08d28b73943c9d19fb7bf64a3ca7160330',
}
ASSETS = {**FILES, PROVENANCE: '1e61887ccdcd9b40bd7686100b588be94e99eaed058cb464d4c2608186753580',
          'SHA256SUMS': '357afe2fdbf6df9d71975ef4bd5d3c819936763c9aec189fb22e37c19140673b'}
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
