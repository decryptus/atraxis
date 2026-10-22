"""Explicit JSON adapter for existing private-file membership consumers."""
from contextlib import contextmanager
import os
import json
from copy import deepcopy
from pathlib import Path
import tempfile

from atraxis.accounts import AccountError
from atraxis.account_lock import private_lock
from atraxis.rule_files import read_private

MAX_PAYLOAD_BYTES = 8 * 1024 * 1024


class JSONStorage:
    def __init__(self, config):
        self.path = Path(config['url'][len('json:///'):])

    def read(self, *, audit=True):
        try:
            from atraxis.account_store import AccountStore
            return AccountStore._decode(json.loads(read_private(self.path, MAX_PAYLOAD_BYTES)))
        except FileNotFoundError:
            if os.path.lexists(self.path):
                raise AccountError('account_store_unavailable') from None
            return None
        except (OSError, ValueError):
            raise AccountError('account_store_unavailable') from None

    @contextmanager
    def transaction(self, *, initialize=False):
        with private_lock(self.path):
            if not initialize and not os.path.lexists(self.path):
                raise AccountError('account_store_unavailable')
            transaction = JSONTransaction(self)
            yield transaction
            if transaction.pending is not None:
                self._commit(transaction.pending)

    def _commit(self, payload):
        payload = json.dumps(payload,sort_keys=True,ensure_ascii=True).encode()
        if not 0 < len(payload) <= MAX_PAYLOAD_BYTES:
            raise AccountError('account_store_capacity_reached')
        descriptor, temporary = tempfile.mkstemp(prefix='.accounts-', dir=self.path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            parent = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def revision(self):
        from atraxis.account_store import AccountStore
        return AccountStore._decode(self.read())['revision']

    def search(self, subject, query):
        from atraxis.account_store import AccountStore
        from atraxis.account_search import search_accounts
        from atraxis.accounts import Directory
        from atraxis.account_delegation import view
        data = AccountStore._decode(self.read())
        directory = Directory(data['directory'])
        organizations = view(directory,subject)
        from atraxis.account_entities import view as entity_view
        entities = entity_view(directory, subject)
        if not organizations and not entities['entity_grants']: raise PermissionError('account_administration_denied')
        return search_accounts(dict(subject=subject,revision=data['revision'],organizations=organizations, **entities),query)


class JSONTransaction:
    def __init__(self, storage):
        self.storage = storage
        self.pending = None

    def read(self, *, audit=True):
        return self.pending if self.pending is not None else self.storage.read()

    def write(self, payload):
        self.pending = deepcopy(payload)
