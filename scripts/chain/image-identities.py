"""Verify disposable CI images and retain only public, bounded identity fields."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / 'scripts/chain/image-inputs.json'
DOCKER_SOCKET = '/var/run/docker.sock'
INSPECT_LIMIT = 2 * 1024 * 1024
INSPECT_TIMEOUT = 30
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
PYTHON_DOCKERFILES = ('Dockerfile', 'scripts/chain/engine.Dockerfile',
                     'scripts/chain/auton.Dockerfile')
DERIVED_DOCKERFILES = ('scripts/network/Dockerfile', 'scripts/chain/atraxis.Dockerfile')
BROWSER_WORKFLOWS = ('.github/workflows/web.yml', '.github/workflows/browser-engine.yml')
CHAIN_IMAGES = ('atraxis-acceptance:ci', 'atraxis-ingress:ci', 'atraxis-chain:ci',
                'atropos-chain:ci', 'auton-chain:ci')


class DockerConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(INSPECT_TIMEOUT)
        self.sock.connect(DOCKER_SOCKET)


def inspect(kind, identity):
    if kind not in ('images', 'containers'):
        raise ValueError('Unsupported Docker identity resource')
    connection = DockerConnection('localhost', timeout=INSPECT_TIMEOUT)
    try:
        connection.request('GET', f'/{kind}/{quote(identity, safe="")}/json')
        response = connection.getresponse()
        content = response.read(INSPECT_LIMIT + 1)
        if response.status != 200 or len(content) > INSPECT_LIMIT:
            raise ValueError('Docker identity inspection failed')
        return json.loads(content)
    finally:
        connection.close()


def load_lock():
    lock = json.loads(LOCK.read_text())
    if lock['schema_version'] != 1 or lock['platform'] != 'linux/amd64':
        raise ValueError('Unsupported integration image lock')
    if set(lock['images']) != {'python', 'playwright'}:
        raise ValueError('Expected Python and Playwright image inputs')
    for entry in lock['images'].values():
        if not re.fullmatch(r'[a-z0-9./:-]+@sha256:[0-9a-f]{64}', entry['reference']):
            raise ValueError('Expected an immutable image reference')
        values = [entry['linux_amd64_manifest'], entry['linux_amd64_image_id'],
                  *entry['rootfs_layers']]
        if not entry['rootfs_layers'] or not all(DIGEST.fullmatch(v) for v in values):
            raise ValueError('Expected SHA-256 image identities and base layers')
    return lock


def check_declarations(lock, root=ROOT):
    expected = {p: 'FROM ' + lock['images']['python']['reference'] for p in PYTHON_DOCKERFILES}
    expected.update({p: 'FROM atraxis-acceptance:ci' for p in DERIVED_DOCKERFILES})
    for path, declaration in expected.items():
        lines = (root / path).read_text().splitlines()
        if [line.strip() for line in lines if line.strip().startswith('FROM ')] != [declaration]:
            raise ValueError('Dockerfile image input differs from the lock')
    for path in BROWSER_WORKFLOWS:
        lines = (root / path).read_text().splitlines()
        expected = 'image: ' + lock['images']['playwright']['reference']
        if [line.strip() for line in lines if line.strip().startswith('image: ')] != [expected]:
            raise ValueError('Browser image input differs from the lock')


def image_identity(info):
    identity = {'image_id': info['Id'], 'platform': info['Os'] + '/' + info['Architecture'],
                'rootfs_layers': info['RootFS']['Layers']}
    if identity['platform'] != 'linux/amd64' or not DIGEST.fullmatch(identity['image_id']):
        raise ValueError('Expected a Linux amd64 image identity')
    if not identity['rootfs_layers'] or not all(DIGEST.fullmatch(v) for v in identity['rootfs_layers']):
        raise ValueError('Missing image layer identities')
    return identity


def chain_identities(lock):
    expected = lock['images']['python']['rootfs_layers']
    records = {}
    for name in CHAIN_IMAGES:
        identity = image_identity(inspect('images', name))
        if identity['rootfs_layers'][:len(expected)] != expected:
            raise ValueError('Built image does not retain the locked Python base layers')
        records[name] = identity
    return records


def browser_identity(container_id, lock):
    if not re.fullmatch(r'[0-9a-f]{64}', container_id):
        raise ValueError('Expected the explicit GitHub job container ID')
    expected = lock['images']['playwright']
    container = inspect('containers', container_id)
    if (container['Config']['Image'] != expected['reference']
            or container['Image'] != expected['linux_amd64_image_id']):
        raise ValueError('Running browser container differs from the image lock')
    identity = image_identity(inspect('images', container['Image']))
    if (identity['image_id'] != expected['linux_amd64_image_id']
            or identity['rootfs_layers'] != expected['rootfs_layers']):
        raise ValueError('Browser image configuration differs from the lock')
    return {'reference': expected['reference'], **identity}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', choices=('chain', 'web', 'browser-engine'))
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    lock = load_lock()
    check_declarations(lock)
    identities = (chain_identities(lock) if args.profile == 'chain' else
                  browser_identity(os.environ['CI_JOB_CONTAINER_ID'], lock))
    paths = (*PYTHON_DOCKERFILES, *DERIVED_DOCKERFILES, *BROWSER_WORKFLOWS,
             'scripts/chain/image-inputs.json', 'scripts/chain/image-identities.py')
    source = subprocess.check_output(['git', '-c', 'safe.directory=' + str(ROOT),
                                      '-C', str(ROOT), 'rev-parse', 'HEAD'],
                                     text=True, timeout=30).strip()
    record = {'schema_version': 1, 'profile': args.profile, 'source_sha': source,
              'inputs': lock, 'identities': identities,
              'file_sha256': {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths}}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2) + '\n')
    print('Verified integration image identities:', args.profile)


if __name__ == '__main__':
    main()
