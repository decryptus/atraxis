"""Validate published release assets and stage only missing PyPI distributions."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

RELEASE = '6602f58ce315416d3bfc9eb16c399c158b7578e5'
VERSION = '1.0.3'
PROVENANCE = 'release-provenance.json'
FILES = {
    'atraxis-1.0.3-py3-none-any.whl': '114260fb23f13d0a38181a265f473f0f8e9d5ad0610fed5bfb920d2eabab6330',
    'atraxis-1.0.3.tar.gz': '94db8fdf07e185d0ffba76874837a3a4d6b9b323a26cba2532aab7b03511947d',
}
ASSETS = {**FILES, PROVENANCE: 'd434fdbcd040d9f16e9091da32728f86821543ab427f2c0bc785efb2417be1fc',
          'SHA256SUMS': 'b8132d58897f69531409a5a28437864cb4f8bd510e97f4bcd9505e43428babad'}
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
