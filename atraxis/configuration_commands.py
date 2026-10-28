"""Offline source administration using host-approved connections and OS identity."""
import json
import sys

from atraxis.administration_cli import add_review_commands, apply_review, read_request, review_revision
from atraxis.config import load_config
from atraxis.configuration_admin import ConfigurationAdmin


def add_parser(commands):
    parser = commands.add_parser('sources-admin', help='local saved source configuration')
    parser.add_argument('--config', required=True, help='private daemon configuration')
    actions = parser.add_subparsers(dest='sources_admin_command', required=True)
    add_review_commands(actions)


def main(options):
    try:
        config = load_config(options.config)
        if config.configuration_web_file is None:
            raise PermissionError('configuration_administration_not_configured')
        admin = ConfigurationAdmin(config.configuration_web_file, options.config)
        if options.sources_admin_command == 'show':
            result = admin.inspect()
            identity = result.pop('identity')
            result['actor'] = {key: identity[key] for key in ('subject', 'organization')}
        else:
            plan, _ = admin.propose(read_request(options.file))
            if options.sources_admin_command == 'apply':
                result = dict(event_id=apply_review(admin, plan, options.expected_revision),
                              saved=True, runtime_changed=False)
            else:
                result = dict(revision=review_revision(admin, plan), request=plan['request'],
                              actor={key: plan['identity'][key] for key in ('subject', 'organization')},
                              before=plan['before'], after=plan['after'])
        print(json.dumps(result, indent=2))
        return 0
    except PermissionError:
        print('Source administration denied.', file=sys.stderr)
        return 3
    except Exception:
        print('Source administration unavailable or outcome unconfirmed. Inspect saved sources and audit before retrying.', file=sys.stderr)
        return 2
