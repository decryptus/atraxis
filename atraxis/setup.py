"""Explicit offline configuration preparation; never starts or contacts services."""
import os
from pathlib import Path
import stat

import yaml

from atraxis.config import ConfigurationError, parse_config

MAX_PATH_LENGTH = 4096
SOURCE_FIELDS = ('source_id', 'kind', 'origin', 'credentials_file')
STARTER_SOURCE_KINDS = ('certlord', 'monit_docker')


def absolute_path(value):
    if not isinstance(value, (str, Path)) or not 0 < len(str(value)) <= MAX_PATH_LENGTH or not str(value).isprintable():
        raise ConfigurationError('invalid_setup_path')
    return Path(value).absolute()


def candidate(*, output, auth_file, listen_addr='127.0.0.1', listen_port=9810,
              behind_tls_proxy=False, source_id=None, source_kind=None,
              source_origin=None, source_credentials_file=None, source_ca_file=None,
              allow_source_loopback_http=False):
    path = absolute_path(output)
    general = dict(listen_addr=listen_addr, listen_port=listen_port, max_workers=4,
                   auth_basic_file=str(absolute_path(auth_file)), behind_tls_proxy=behind_tls_proxy)
    source_values = (source_id, source_kind, source_origin, source_credentials_file)
    if any(value is not None for value in source_values) and not all(value is not None for value in source_values):
        raise ConfigurationError('incomplete_setup_source')
    if source_id is None and (source_ca_file is not None or allow_source_loopback_http):
        raise ConfigurationError('incomplete_setup_source')
    sources = []
    if source_id is not None:
        source = dict(zip(SOURCE_FIELDS, source_values))
        source['credentials_file'] = str(absolute_path(source_credentials_file))
        if source_ca_file is not None:
            source['ca_file'] = str(absolute_path(source_ca_file))
        if allow_source_loopback_http:
            source['allow_loopback_http'] = True
        sources.append(source)
    document = dict(general=general, sources=sources)
    parse_config(document, path)
    return path, document


def write_config(path, document):
    """Publish a complete private file without replacing any existing entry."""
    path = absolute_path(path)
    parse_config(document, path)  # Recheck referenced files immediately before writing.
    raw = yaml.safe_dump(document, sort_keys=False).encode('utf-8')
    # The operator must own a non-writable-by-others parent. A held directory FD
    # anchors creation/publication and cleanup even if its pathname is renamed.
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = None
    published = False
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o022:
            raise ConfigurationError('unsafe_setup_directory')
        import secrets
        name = '.atraxis-setup-' + secrets.token_hex(16)
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=descriptor)
        temporary = name
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path.name, src_dir_fd=descriptor, dst_dir_fd=descriptor, follow_symlinks=False)
        published = True
        os.fsync(descriptor)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=descriptor)
            except FileNotFoundError:
                pass
        os.close(descriptor)
    return published


def add_parser(commands):
    parser = commands.add_parser('setup', help='preview or explicitly write a validated starter configuration')
    parser.add_argument('--output', required=True)
    parser.add_argument('--auth-file', required=True, help='existing HTTPdis htpasswd file; never a password')
    parser.add_argument('--listen-addr', default='127.0.0.1')
    parser.add_argument('--listen-port', type=int, default=9810)
    parser.add_argument('--behind-tls-proxy', action='store_true')
    parser.add_argument('--source-id')
    parser.add_argument('--source-kind', choices=STARTER_SOURCE_KINDS,
                        help='starter source; configure Prometheus metric selection in YAML')
    parser.add_argument('--source-origin')
    parser.add_argument('--source-credentials-file')
    parser.add_argument('--source-ca-file')
    parser.add_argument('--allow-source-loopback-http', action='store_true')
    parser.add_argument('--write', action='store_true', help='create the new file; never overwrite or start the daemon')


def main(options):
    import sys
    try:
        fields = {key: value for key, value in vars(options).items() if key not in ('command', 'write')}
        path, document = candidate(**fields)
        if options.write:
            write_config(path, document)
            print('Configuration created. Run doctor before starting the daemon.')
        else:
            print('# Preview only. Add --write to create the file. No services were contacted.')
            print(yaml.safe_dump(document, sort_keys=False), end='')
        if not document['sources']:
            print('No sources configured; inventory will not be ready.', file=sys.stderr)
        print('This starter shares inventory with authenticated users. Configure inventory access before adding other users.', file=sys.stderr)
        return 0
    except (OSError, ValueError, TypeError, RecursionError, yaml.YAMLError):
        print('Setup failed. Check the complete source options, credential/CA files, TLS settings and destination; existing files are never replaced.', file=sys.stderr)
        return 1
