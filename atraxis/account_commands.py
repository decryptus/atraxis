"""Explicit, local account administration using trusted POSIX file ownership."""
import json
import sys
from pathlib import Path

import yaml

from atraxis.config import _StrictLoader
from atraxis.rule_files import read_private
from atraxis.account_storage import storage_config
from atraxis.accounts import AccountError, Directory, default_directory
from atraxis.account_store import AccountStore, load_candidate

MAX_STORAGE_CONFIG_BYTES = 65536


def add_parser(commands):
    parser = commands.add_parser('accounts', help='local private account directory administration')
    location = parser.add_mutually_exclusive_group(required=True)
    location.add_argument('--store', help='storage URL or SQLite path')
    location.add_argument('--storage-config', help='private backend/url/options YAML file')
    parser.add_argument('--backend', help='installed account storage backend (with --store)')
    actions = parser.add_subparsers(dest='account_command', required=True)
    init = actions.add_parser('init')
    init.add_argument('--subject', required=True)
    init.add_argument('--administrator', action='store_true', help='explicit local bootstrap of global account administration')
    actions.add_parser('show')
    actions.add_parser('audit')
    actions.add_parser('export')
    backup = actions.add_parser('backup', help='write a private complete SQLite account archive')
    backup.add_argument('--output', required=True)
    restore = actions.add_parser('restore', help='restore into a new SQLite database with services stopped')
    restore.add_argument('--input', required=True)
    restore.add_argument('--confirm-services-stopped', action='store_true')
    contexts = actions.add_parser('contexts')
    contexts.add_argument('--subject', required=True)
    for name in ('check', 'apply'):
        action = actions.add_parser(name)
        action.add_argument('--file', required=True)
        if name == 'apply':
            action.add_argument('--expected-revision', required=True)
            action.add_argument('--confirm', action='store_true', required=True)


def main(options):
    try:
        if options.storage_config:
            if options.backend is not None:
                raise AccountError('invalid_account_storage')
            path = Path(options.storage_config).absolute()
            config = storage_config(yaml.load(read_private(path, MAX_STORAGE_CONFIG_BYTES), Loader=_StrictLoader), path.parent)
        else:
            config = {'backend': options.backend or 'sqlite', 'url': options.store}
        store = AccountStore(config)
        command = options.account_command
        if command in ('backup', 'restore'):
            from atraxis.account_backup import backup, restore
            result = (backup(config, options.output) if command == 'backup' else
                      restore(config, options.input, services_stopped=options.confirm_services_stopped))
        elif command == 'init':
            directory = default_directory(options.subject)
            if options.administrator:
                from atraxis.account_entities import GRANT_OPERATIONS
                document = directory.document()
                document['entity_delegations'].append(dict(id='root-entities',subject=options.subject,scope=dict(kind='global',id=None),
                    operations=sorted(GRANT_OPERATIONS),parent=None,enabled=True,delegable=True))
                directory = Directory(document)
            result = {'revision': store.apply(directory, None)}
        elif command in ('check', 'apply'):
            candidate = load_candidate(options.file)
            if command == 'apply':
                result = {'revision': store.apply(candidate, options.expected_revision)}
            else:
                current = store.snapshot(audit=False)
                result = {'revision': current['revision'], 'changes': {}}
                for key in ('tenants', 'organizations', 'groups', 'users', 'memberships', 'delegations', 'entity_delegations'):
                    before = {json.dumps(item, sort_keys=True) for item in current['directory'][key]}
                    after = {json.dumps(item, sort_keys=True) for item in candidate.document()[key]}
                    result['changes'][key] = {'removed': [json.loads(item) for item in sorted(before-after)],
                                              'added': [json.loads(item) for item in sorted(after-before)]}
        else:
            snapshot = store.snapshot(audit=command=='audit')
            if command == 'contexts':
                directory = Directory(snapshot['directory'])
                result = [{'organization': org, 'tenant': directory.organizations[org]['tenant'],
                           'groups': directory.effective_groups(options.subject, org)}
                          for org in directory.contexts(options.subject)]
            elif command == 'export':
                result = snapshot['directory']
            elif command == 'audit':
                result = snapshot['audit']
            else:
                result = {'revision': snapshot['revision'], 'directory': snapshot['directory']}
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (AccountError, OSError, ValueError, TypeError, RecursionError, yaml.YAMLError):
        print('Account operation failed; check private files, input and current revision.', file=sys.stderr)
        return 2
