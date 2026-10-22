"""Offline, synthetic container recovery fixture; not a product backup tool.

The caller stops every writer first. Product account/engine commands supply
validated data archives. Only listed configuration and external fixture evidence
are copied; Redis delivery is intentionally rebuilt from the SQL outbox.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

STATE = Path('/state')
BACKUP = Path('/backup')
FILES = {
    'web': ('ca.pem', 'users', 'source.json', 'access.yml', 'web.yml', 'backup.json'),
    'operator': ('ca.pem', 'alice.token', 'alice.yml', 'inventory.json', 'inventory.yml',
                 'observation.pem', 'facts.json', 'operator.yml', 'backup.json'),
    'engine': ('ca.pem', 'observation.pub', 'executor.password', 'alert.token',
               'bindings.yml', 'engine.yml', 'backup.json'),
    'redis': ('ca.pem', 'redis.conf'),
    'auton': ('ca.pem', 'users', 'auton.yml', 'jobs.db', 'effects'),
    'alert': ('ca.pem', 'alert.token', 'deliveries.jsonl'),
}
MAX_FILE_BYTES = 48 * 1024 * 1024


def private_directory(path):
    metadata = path.lstat()
    assert stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.getuid()
    assert stat.S_IMODE(metadata.st_mode) == 0o700


def checked_bytes(path):
    metadata = path.lstat()
    assert stat.S_ISREG(metadata.st_mode) and metadata.st_size <= MAX_FILE_BYTES
    return path.read_bytes()


def publish(path, data):
    with path.open('xb') as stream:
        os.chmod(path, 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def capture(role):
    private_directory(STATE)
    private_directory(BACKUP)
    destination = BACKUP / role
    destination.mkdir(mode=0o700)
    for name in (*FILES[role], 'initialized.json'):
        source = STATE / name
        if role == 'auton' and name == 'jobs.db':
            # All writers have stopped; SQLite's backup API also handles a
            # remaining committed WAL, without copying a live database file.
            with sqlite3.connect('file:' + str(source) + '?mode=ro', uri=True) as original:
                with sqlite3.connect(destination / name) as copy:
                    original.backup(copy)
                    assert copy.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            (destination / name).chmod(0o600)
        else:
            publish(destination / name, checked_bytes(source))
    manifest = {p.name: hashlib.sha256(checked_bytes(p)).hexdigest() for p in destination.iterdir()}
    publish(destination / 'manifest.json', json.dumps(manifest, sort_keys=True).encode())


def validate(role):
    private_directory(BACKUP)
    source = BACKUP / role
    private_directory(source)
    manifest = json.loads(checked_bytes(source / 'manifest.json'))
    assert set(manifest) == set(FILES[role]) | {'initialized.json'}
    assert {p.name for p in source.iterdir()} == set(manifest) | {'manifest.json'}
    for name, digest in manifest.items():
        path = source / name
        assert path.stat().st_uid == os.getuid() and stat.S_IMODE(path.stat().st_mode) == 0o600
        assert hashlib.sha256(checked_bytes(path)).hexdigest() == digest
    assert json.loads((source / 'initialized.json').read_text()) == dict(version=1, role=role)
    return manifest


def restore(role):
    manifest = validate(role)  # Validate every file before writing anything.
    private_directory(STATE)
    assert not any(STATE.iterdir())
    for name in manifest:
        publish(STATE / name, checked_bytes(BACKUP / role / name))
    if role in ('web', 'operator'):
        subprocess.run(['atraxis', 'accounts', '--store', str(STATE / 'accounts.db'),
            'restore', '--input', str(STATE / 'backup.json'), '--confirm-services-stopped'],
            check=True, stdout=subprocess.DEVNULL)
    # Engine restore requires a running, empty Redis delivery service and is
    # performed separately by the caller before starting its API or workers.


if __name__ == '__main__':
    command, role = sys.argv[1:]
    {'capture': capture, 'validate': validate, 'restore': restore}[command](role)
