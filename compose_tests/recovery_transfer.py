"""Transfer disposable CI state to another runner; never a site backup API."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess

ROOT = Path(__file__).resolve().parents[1]
TRANSFER = ROOT / 'acceptance-transfer'
MANIFEST = 'manifest.json'
FILES = frozenset({'web.tar', 'engine.tar', 'site.tar', 'context.json', 'images.tar'})
IMAGES = ('atraxis-install-web:local', 'atraxis-install-engine:local', 'atraxis-install-proxy:local')
MAX_IMAGE_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_BYTES = 33 * 1024 * 1024
MAX_CONTEXT_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 16384
CHUNK_BYTES = 1024 * 1024
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')
VM_ID = Path('/sys/class/dmi/id/product_uuid')
SHA256 = re.compile(r'[0-9a-f]{64}')
COMMIT = re.compile(r'[0-9a-f]{40}')


def digest(path, limit):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError('transfer_file_invalid')
    result = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(CHUNK_BYTES):
            result.update(chunk)
    return result.hexdigest()


def limit(name):
    return MAX_IMAGE_BYTES if name == 'images.tar' else (
        MAX_CONTEXT_BYTES if name == 'context.json' else MAX_ARCHIVE_BYTES)


def command(*args, timeout=30):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ValueError('transfer_command_failed')
    return result.stdout.strip()


def identity():
    # Docker's persisted ID can be cloned in hosted-runner base images.
    return dict(boot=BOOT_ID.read_text().strip(), vm=VM_ID.read_text().strip().lower(),
        daemon=command('docker', 'info', '--format', '{{.ID}}'))


def write_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True)
        stream.write('\n')


def capture(backup, context, project, archives, fingerprints):
    """Called only after the full source Compose acceptance passes."""
    TRANSFER.mkdir(mode=0o700)  # Never replace an earlier attempt.
    expected = dict(archives)
    for role in ('web', 'engine', 'site'):
        path = backup / (role + '.tar')
        if digest(path, MAX_ARCHIVE_BYTES) != expected[role]:
            raise ValueError('transfer_archive_changed')
        shutil.copyfile(path, TRANSFER / path.name)
        (TRANSFER / path.name).chmod(0o600)
    write_private(TRANSFER / 'context.json', context)
    images = {name: command('docker', 'image', 'inspect', '--format', '{{.Id}}', name) for name in IMAGES}
    command('docker', 'image', 'save', '--output', str(TRANSFER / 'images.tar'), *IMAGES, timeout=180)
    (TRANSFER / 'images.tar').chmod(0o600)
    manifest = dict(version=1, synthetic_only=True, source=identity(), source_project=project,
        source_commit=command('git', '-C', str(ROOT), 'rev-parse', 'HEAD'),
        images=images, fingerprints=fingerprints,
        files={name: dict(sha256=digest(TRANSFER / name, limit(name)),
            size=(TRANSFER / name).stat().st_size) for name in sorted(FILES)})
    write_private(TRANSFER / MANIFEST, manifest)
    # The CI upload action runs as the invoking runner user, not root.
    if 'SUDO_UID' in os.environ and 'SUDO_GID' in os.environ:
        owner = int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID'])
        for path in [TRANSFER, *(TRANSFER / name for name in FILES | {MANIFEST})]:
            os.chown(path, *owner)


def verify(directory, expected_digest, commit, destination_identity):
    """Validate the whole transfer before loading images or creating volumes."""
    if not isinstance(expected_digest, str) or not SHA256.fullmatch(expected_digest):
        raise ValueError('transfer_digest_invalid')
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('transfer_directory_invalid')
    if {p.name for p in directory.iterdir()} != FILES | {MANIFEST}:
        raise ValueError('transfer_file_set_invalid')
    if digest(directory / MANIFEST, MAX_MANIFEST_BYTES) != expected_digest:
        raise ValueError('transfer_manifest_mismatch')
    manifest = json.loads((directory / MANIFEST).read_text())
    if (manifest.get('version') != 1 or manifest.get('synthetic_only') is not True
            or not COMMIT.fullmatch(commit) or manifest.get('source_commit') != commit
            or set(manifest.get('files', {})) != FILES
            or set(manifest.get('images', {})) != set(IMAGES)
            or set(manifest.get('fingerprints', {})) != {'web', 'engine', 'site'}):
        raise ValueError('transfer_manifest_invalid')
    for image in manifest['images'].values():
        if not isinstance(image, str) or not image.startswith('sha256:') or not SHA256.fullmatch(image[7:]):
            raise ValueError('transfer_image_invalid')
    source = manifest.get('source', {})
    for key in ('boot', 'vm'):
        if not source.get(key) or not destination_identity.get(key):
            raise ValueError('transfer_host_identity_missing:' + key)
        if source[key] == destination_identity[key]:
            raise ValueError('transfer_requires_another_host:' + key)
    for name in FILES:
        path, expected = directory / name, manifest['files'][name]
        if (digest(path, limit(name)) != expected['sha256']
                or path.stat().st_size != expected['size']):
            raise ValueError('transfer_file_mismatch')
    return manifest


def confirm_images(manifest):
    for name, image in manifest['images'].items():
        if command('docker', 'image', 'inspect', '--format', '{{.Id}}', name) != image:
            raise ValueError('transfer_image_mismatch')
