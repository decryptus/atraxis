"""Validate published release assets and stage only missing PyPI distributions."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

RELEASE = '0e37b7f755e591243af227006704269168461787'
VERSION = '1.0.2'
PROVENANCE = 'release-provenance.json'
FILES = {
    'atraxis-1.0.2-py3-none-any.whl': 'a45f76557093007265420eb631efead59be80086f444d89fdbe70885713acf0a',
    'atraxis-1.0.2.tar.gz': '5af858ae34dc6a37dcb5021fce92529616ef14614017f672188b0c5a4292594e',
}
ASSETS = {**FILES, PROVENANCE: 'cb1ba88ec8c27c1cf6a981ab4dd8ebcbcd66c5e54d244a32a91d7884eef4f319',
          'SHA256SUMS': 'b12b152604018c647f9b876caf2cbec9a082b9a359467f4ba028c760c215db7b'}
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
