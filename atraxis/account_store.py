"""Account policy and atomic revisions, independent of storage adapters."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

import yaml

from atraxis.accounts import AccountError, Directory
from atraxis.config import _StrictLoader
from atraxis.rule_files import read_private
from atraxis.account_storage import open_storage, storage_config

MAX_FILE_BYTES = 8 * 1024 * 1024
REVISION = re.compile(r'[0-9a-f]{32}')


def load_candidate(path):
    try:
        return Directory(yaml.load(read_private(path, MAX_FILE_BYTES), Loader=_StrictLoader))
    except (OSError, ValueError, TypeError, KeyError, RecursionError, yaml.YAMLError):
        raise AccountError('invalid_account_candidate') from None


class AccountStore:
    def __init__(self, path):
        self.adapter = open_storage(path)
        self.path = getattr(self.adapter, "path", None)

    def snapshot(self, *, audit=True):
        return self._decode(self.adapter.read(audit=audit), audit_complete=audit)

    def revision(self):
        return self.adapter.revision()

    def search(self, subject, query):
        return self.adapter.search(subject, query)

    @staticmethod
    def _decode(data, *, audit_complete=True):
        try:
            if (type(data) is not dict or set(data) != {'version', 'revision', 'directory', 'audit', 'membership'}
                    or type(data['version']) is not int or data['version'] != 3
                    or type(data['revision']) is not str or not REVISION.fullmatch(data['revision'])
                    or type(data['audit']) is not list or not data['audit']):
                raise ValueError()
            previous = None if audit_complete else data['audit'][0]['previous']
            for entry in data['audit']:
                if (type(entry) is not dict or not {'revision','previous','uid','time','sha256'} <= set(entry)
                        or not set(entry) <= {'revision','previous','uid','time','sha256','actor'}
                        or entry['previous'] != previous
                        or type(entry['revision']) is not str or not REVISION.fullmatch(entry['revision'])
                        or type(entry['uid']) is not int or entry['uid'] < 0
                        or type(entry['time']) is not str or len(entry['time']) > 64
                        or type(entry['sha256']) is not str or not re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])):
                    raise ValueError()
                if 'actor' in entry:
                    validate_actor(entry['actor'])
                previous = entry['revision']
            directory = Directory(data['directory'])
            if previous != data['revision'] or data['audit'][-1]['sha256'] != digest(directory):
                raise ValueError()
            if data['membership'] != directory.membership_projection(data['revision']):
                raise ValueError()
            return data
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            raise AccountError('account_store_unavailable') from None

    @contextmanager
    def _locked(self):
        with self.adapter.transaction() as transaction:
            yield transaction

    def apply(self, directory, expected_revision, *, authorize=None, actor=None):
        directory = Directory(directory.document())
        if actor is not None:
            validate_actor(actor)
        with self.adapter.transaction(initialize=expected_revision is None) as transaction:
            raw = transaction.read(audit=False)
            exists = raw is not None
            if expected_revision is None:
                if exists:
                    raise AccountError('account_revision_conflict')
                previous, audit = None, []
            else:
                snapshot = self._decode(raw, audit_complete=False)
                if expected_revision != snapshot['revision']:
                    raise AccountError('account_revision_conflict')
                previous, audit = snapshot['revision'], snapshot['audit']
            if authorize is not None:
                if previous is None:
                    raise PermissionError('account_initialization_requires_local_authority')
                authorize(Directory(snapshot['directory']), directory)
            revision = uuid.uuid4().hex
            audit.append({'revision': revision, 'previous': previous, 'uid': os.getuid(),
                          'time': datetime.now(timezone.utc).isoformat(), 'sha256': digest(directory),
                          **({'actor':dict(actor)} if actor is not None else {})})
            data = {'version': 3, 'revision': revision, 'directory': directory.document(), 'audit': audit,
                    'membership': directory.membership_projection(revision)}
            transaction.write(data)
            return revision


def digest(directory):
    return hashlib.sha256(json.dumps(directory.document(), sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def require_membership(profile_path, accounts_file, subject, organization):
    if accounts_file is None:
        return
    config = storage_config(accounts_file, Path(profile_path).absolute().parent)
    directory = Directory(AccountStore(config).snapshot(audit=False)['directory'])
    if not directory.allows_membership(subject, organization):
        raise PermissionError('account_membership_required')


def validate_actor(actor):
    from atraxis.accounts import identifier
    from atraxis.account_delegation import OPERATIONS
    from atraxis.account_entities import ALL_OPERATIONS
    if (type(actor) is not dict or set(actor) != {'subject','operation','organization','target'}
            or type(actor['operation']) is not str or actor['operation'] not in OPERATIONS | ALL_OPERATIONS
            or any(not identifier(actor[k]) for k in ('subject','target'))
            or not (identifier(actor['organization']) or actor['organization'] is None and actor['operation'] in ALL_OPERATIONS - {'organization.create','organization.update','group.create','group.update'})):
        raise AccountError('invalid_account_audit_actor')
