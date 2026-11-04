"""Load the wizard's capabilities from a fixed operator-provisioned directory."""
from contextlib import contextmanager
import os
import stat

from atraxis.installation.engine import Engine
from atraxis.installation.filesystem import open_root, trusted_directory
from atraxis.installation.validation import (
    Authority, InstallError, MAX_PLAN_BYTES, MAX_TLS_BYTES, decode, parse, require,
)

HOST_DIRECTORY = '/etc/atraxis/installer'


def _read(directory, name, limit):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    with os.fdopen(os.open(name, flags, dir_fd=directory), 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid in (0, os.geteuid())
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1
                and 0 < info.st_size <= limit, 'invalid_authority')
        data = stream.read(limit + 1)
        require(0 < len(data) <= limit, 'invalid_authority')
        return data


@contextmanager
def session():
    directory = trusted_directory(HOST_DIRECTORY)
    root = None
    try:
        data = decode(_read(directory, 'policy.json', MAX_PLAN_BYTES))
        require(type(data) is dict and set(data) in (
            {'listeners', 'networks', 'sources'}, {'listeners', 'networks', 'sources', 'root_link'}),
            'invalid_authority')
        require(type(data['listeners']) is list and data['listeners']
                and type(data['networks']) is list, 'invalid_authority')
        listener = data['listeners'][0]
        plan = parse(dict(version=1, name='site', server_name=listener, listen_port=8443,
                          listen_address=listener, sources=data['sources']))
        authority = Authority(tuple(s.origin for s in plan.sources),
                              tuple(data['networks']), tuple(data['listeners']))
        authority.check(plan)
        link = data.get('root_link', {})
        require(type(link) is dict and ('root_link' not in data or
                (set(link) == {'alias', 'target'} and all(type(v) is str for v in link.values()))),
                'invalid_authority')
        root = open_root(**link)
        cert = _read(directory, 'server.crt', MAX_TLS_BYTES)
        key = _read(directory, 'server.key', MAX_TLS_BYTES)
        cas = {s.source_id: _read(directory, f'source-{s.source_id}.ca.pem', MAX_TLS_BYTES)
               for s in plan.sources}
        yield Engine(authority, root), plan, cert, key, cas
    finally:
        os.close(directory)
        if root is not None:
            root.close()
