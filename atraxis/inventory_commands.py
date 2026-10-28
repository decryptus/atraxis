"""Trusted local inventory administration; identity comes only from OS UID."""
import json
import sys
from atraxis.administration_cli import add_review_commands, apply_review, read_request, review_revision
from atraxis.inventory_admin import InventoryAdmin


def add_parser(commands):
    parser = commands.add_parser('inventory-admin', help='local inventory read-grant administration')
    parser.add_argument('--config', required=True, help='private inventory-access profile')
    actions = parser.add_subparsers(dest='inventory_admin_command', required=True)
    actions.add_parser('tui', help='review inventory read grants in Textual')
    add_review_commands(actions)


def main(options):
    try:
        admin = InventoryAdmin(options.config)
        if options.inventory_admin_command == 'tui':
            from atraxis.inventory_textual import run
            return run(admin)
        if options.inventory_admin_command == 'show':
            result = admin.inspect()
        else:
            plan, _ = admin.propose(read_request(options.file))
            if options.inventory_admin_command == 'apply':
                result = dict(event_id=apply_review(admin, plan, options.expected_revision), saved=True)
            else:
                result = dict(revision=review_revision(admin, plan), request=plan['request'],
                              actor=plan['identity']['subject'], grant=plan['grant'])
        print(json.dumps(result, indent=2))
        return 0
    except PermissionError:
        print('Inventory administration denied.', file=sys.stderr)
        return 3
    except ImportError:
        print('Install the Textual optional dependency to use this command.', file=sys.stderr)
        return 2
    except Exception:
        print('Inventory administration unavailable or outcome unconfirmed. Inspect current grants before retrying.', file=sys.stderr)
        return 2
