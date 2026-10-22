"""Storage selection; installed adapters provide atomic, serialized transactions."""
from copy import deepcopy
from importlib.metadata import entry_points
from pathlib import Path
import re
from typing import Protocol, ContextManager

from atraxis.accounts import AccountError

ENTRY_POINT_GROUP = 'atraxis.account_storage'
MAX_URL_LENGTH = 4096
BACKEND_NAME = re.compile(r'[a-z][a-z0-9_-]{0,63}')


class Transaction(Protocol):
    def read(self, *, audit: bool = True) -> dict | None: ...
    def write(self, document: dict) -> None: ...


class Storage(Protocol):
    def revision(self) -> str: ...
    def search(self, subject, query) -> dict: ...
    def read(self, *, audit: bool = True) -> dict | None: ...
    def transaction(self, *, initialize: bool = False) -> ContextManager[Transaction]: ...


def storage_config(value, base=None):
    """Paths select SQLite; mappings select an installed backend with an opaque URL."""
    if isinstance(value, Path):
        value = str(value)
    if type(value) is str:
        value = {'backend': 'sqlite', 'url': value}
    if (type(value) is not dict or not {'url'} <= set(value)
            or not set(value) <= {'backend', 'url', 'options'}):
        raise AccountError('invalid_account_storage')
    backend, url = value.get('backend', 'sqlite'), value['url']
    options = value.get('options', {})
    if (type(backend) is not str or not BACKEND_NAME.fullmatch(backend)
            or type(url) is not str or not 0 < len(url) <= MAX_URL_LENGTH or not url.isprintable()
            or type(options) is not dict):
        raise AccountError('invalid_account_storage')
    if backend in ('sqlite', 'json'):
        if options:
            raise AccountError('invalid_account_storage')
        prefix = backend + ':///'
        if url.startswith(prefix):
            url = url[len(prefix):]
        elif '://' in url:
            raise AccountError('invalid_account_storage')
        if not url or url == ':memory:' or url.startswith('file:') or '?' in url or '#' in url:
            raise AccountError('invalid_account_storage')
        path = Path(url)
        if not path.is_absolute():
            path = Path(base or Path.cwd()) / path
        url = prefix + str(path.absolute())
    return {'backend': backend, 'url': url, 'options': deepcopy(options)}


def open_storage(value):
    config = storage_config(value)
    if config['backend'] == 'sqlite':
        from atraxis.account_sqlite import SQLiteStorage
        return SQLiteStorage(config)
    if config['backend'] == 'json':
        from atraxis.account_json import JSONStorage
        return JSONStorage(config)
    try:
        matches = list(entry_points(group=ENTRY_POINT_GROUP, name=config['backend']))
        if len(matches) != 1:
            raise AccountError('account_storage_backend_unavailable')
        adapter = matches[0].load()(config)
        if not all(callable(getattr(adapter, name, None)) for name in ('read', 'transaction', 'revision', 'search')):
            raise AccountError('invalid_account_storage_adapter')
        return adapter
    except AccountError:
        raise
    except Exception:
        raise AccountError('account_storage_backend_unavailable') from None
