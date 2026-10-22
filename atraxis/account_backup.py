"""Trusted-host, data-only account backups and fresh SQLite restoration."""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from atraxis.accounts import AccountError
from atraxis.account_storage import storage_config, open_storage
from atraxis.account_store import AccountStore
from atraxis.account_sqlite import SQLiteTransaction
from atraxis.account_lock import private_lock
from atraxis.rule_files import read_private

FORMAT = 1
MAX_BYTES = 32 * 1024 * 1024
MAX_AUDIT_ROWS = 100000
FIELDS = frozenset({'version', 'backend', 'snapshot'})


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def parent_private(path):
    metadata = path.parent.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077):
        raise AccountError('backup_requires_private_directory')


def sync_directory(path):
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def summary(payload):
    snapshot = payload['snapshot']
    return dict(version=FORMAT, backend='sqlite', revision=snapshot['revision'],
                audit_entries=len(snapshot['audit']), sha256=hashlib.sha256(canonical(payload)).hexdigest())


def checked(payload):
    if (type(payload) is not dict or set(payload) != FIELDS
            or type(payload['version']) is not int or payload['version'] != FORMAT
            or payload['backend'] != 'sqlite'):
        raise AccountError('invalid_account_backup')
    AccountStore._decode(payload['snapshot'])
    if len(payload['snapshot']['audit']) > MAX_AUDIT_ROWS:
        raise AccountError('account_backup_limit_exceeded')
    return payload


def read_backup(path):
    return decode_backup(read_private(path, MAX_BYTES))


def decode_backup(raw):
    if len(raw) > MAX_BYTES: raise AccountError('account_backup_limit_exceeded')
    outer = json.loads(raw)
    if (type(outer) is not dict or set(outer) != {'sha256', 'payload'}
            or outer['sha256'] != hashlib.sha256(canonical(outer['payload'])).hexdigest()):
        raise AccountError('account_backup_integrity_failed')
    return checked(outer['payload'])


def sqlite_adapter(config):
    config = storage_config(config)
    if config['backend'] != 'sqlite': raise AccountError('unsupported_account_backup_backend')
    return open_storage(config)


def backup(config, output):
    adapter = sqlite_adapter(config)
    # One read snapshot includes directory, all audit and the membership projection.
    with adapter._connect() as connection:
        connection.execute('BEGIN'); adapter._schema(connection, False)
        if connection.execute('SELECT COUNT(*) FROM account_audit').fetchone()[0] > MAX_AUDIT_ROWS:
            raise AccountError('account_backup_limit_exceeded')
        snapshot = AccountStore._decode(SQLiteTransaction(connection).read())
    payload = checked(dict(version=FORMAT, backend='sqlite', snapshot=snapshot))
    raw = canonical(dict(payload=payload, sha256=hashlib.sha256(canonical(payload)).hexdigest()))
    if len(raw) > MAX_BYTES: raise AccountError('account_backup_limit_exceeded')
    path = Path(output); parent_private(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
    except BaseException:
        path.unlink()
        raise
    sync_directory(path)
    return summary(payload)


def restore(config, archive, *, services_stopped=False):
    if services_stopped is not True: raise AccountError('services_stop_confirmation_required')
    adapter = sqlite_adapter(config)
    payload = read_backup(archive)
    path = adapter.path; parent_private(path)
    with private_lock(path):
        if any(os.path.lexists(str(path) + suffix) for suffix in ('', '-wal', '-shm', '-journal')):
            raise AccountError('account_restore_requires_new_database')
        fd, temporary = tempfile.mkstemp(prefix='.account-restore-', suffix='.db', dir=path.parent)
        os.close(fd)
        temporary = Path(temporary)
        try:
            target = sqlite_adapter(str(temporary))
            data = payload['snapshot']
            with target.transaction(initialize=True) as transaction:
                for event in data['audit'][:-1]: transaction.append_audit(event)
                transaction.write(data)
                if AccountStore._decode(transaction.read()) != data:
                    raise AccountError('account_restore_validation_failed')
                if transaction.connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
                    raise AccountError('account_restore_validation_failed')
            # Publish only a complete, closed database; never overwrite a destination.
            with temporary.open('rb') as stream: os.fsync(stream.fileno())
            os.link(temporary, path)
            temporary.unlink()
            sync_directory(path)
        finally:
            for suffix in ('', '.lock', '-wal', '-shm', '-journal'):
                candidate = Path(str(temporary) + suffix)
                if candidate.exists(): candidate.unlink()
    return summary(payload)
