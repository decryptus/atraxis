"""Trusted local inventory administration; identity comes only from OS UID."""
import sys
from atraxis.inventory_admin import InventoryAdmin


def add_parser(commands):
    parser = commands.add_parser('inventory-admin', help='local inventory read-grant administration')
    parser.add_argument('--config', required=True, help='private inventory-access profile')
    actions = parser.add_subparsers(dest='inventory_admin_command', required=True)
    actions.add_parser('tui', help='review inventory read grants in Textual')


def main(options):
    try:
        from atraxis.inventory_textual import run
        return run(InventoryAdmin(options.config))
    except PermissionError:
        print('Inventory administration denied.', file=sys.stderr)
        return 3
    except ImportError:
        print('Install the Textual optional dependency to use this command.', file=sys.stderr)
        return 2
    except Exception:
        print('Inventory administration unavailable or outcome unconfirmed. Inspect current grants before retrying.', file=sys.stderr)
        return 2
