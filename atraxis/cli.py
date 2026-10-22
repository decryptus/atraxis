"""Explicit commands: no implicit daemon or terminal UI startup."""
import argparse
import signal
import sys
import threading

from atraxis.config import DEFAULT_CONFIG, ConfigurationError, load_config


def main(argv=None):
    parser = argparse.ArgumentParser(prog='atraxis')
    commands = parser.add_subparsers(dest='command', required=True)
    from atraxis.setup import add_parser as add_setup
    from atraxis.doctor import add_parser as add_doctor
    add_setup(commands)
    add_doctor(commands)
    from atraxis.retention import add_parser as add_retention
    add_retention(commands)
    for name in ('check-config', 'serve'):
        command = commands.add_parser(name)
        command.add_argument('--config', default=DEFAULT_CONFIG)
    control = commands.add_parser('rules-serve', help='separate loopback rule control service')
    control.add_argument('--config', required=True)
    control.add_argument('--port', type=int, default=8081)
    from atraxis.client_config import DEFAULT_CLIENT_CONFIG
    for name in ('status', 'inventory'):
        command = commands.add_parser(name)
        command.add_argument('--config', default=DEFAULT_CLIENT_CONFIG)
        command.add_argument('--json', action='store_true')
        command.add_argument('-o', '--organization', help='explicit inventory organization ID')
    from atraxis.search_commands import add_parser as add_search
    add_search(commands)
    from atraxis.rule_commands import add_parser
    add_parser(commands)
    from atraxis.account_commands import add_parser as add_accounts
    add_accounts(commands)
    from atraxis.account_admin_commands import add_parser as add_admin
    add_admin(commands)
    from atraxis.inventory_commands import add_parser as add_inventory_admin
    add_inventory_admin(commands)
    options = parser.parse_args(argv)
    if options.command == 'retention':
        from atraxis.retention import main as retention_main
        return retention_main(options)
    if options.command in ('setup', 'doctor'):
        from atraxis import setup, doctor
        return (setup if options.command == 'setup' else doctor).main(options)
    if options.command == 'search':
        from atraxis.search_commands import main as search_main
        return search_main(options)
    if options.command == 'inventory-admin':
        from atraxis.inventory_commands import main as inventory_admin_main
        return inventory_admin_main(options)
    if options.command == 'accounts-admin':
        from atraxis.account_admin_commands import main as admin_main
        return admin_main(options)
    if options.command == 'accounts':
        from atraxis.account_commands import main as accounts_main
        return accounts_main(options)
    if options.command == 'rules':
        from atraxis.rule_commands import main as rules_main
        return rules_main(options)
    if options.command == 'rules-serve':
        try:
            from atraxis.rule_server import serve
            serve(options.config, options.port)
            return 0
        except KeyboardInterrupt:
            return 130
        except Exception:
            print('Rule service configuration or startup failed', file=sys.stderr)
            return 2
    if options.command in ('status', 'inventory'):
        from atraxis.client import EXIT_DEGRADED, EXIT_ERROR, EXIT_OK, read_inventory, render_inventory
        from atraxis.client_config import load_client_config
        cancel = threading.Event()
        previous = None
        try:
            if threading.current_thread() is threading.main_thread():
                previous = signal.signal(signal.SIGTERM, lambda signum, frame: cancel.set())
            from dataclasses import replace
            from atraxis.accounts import identifier
            if options.organization is not None and not identifier(options.organization):
                raise ConfigurationError('invalid_inventory_context')
            client = replace(load_client_config(options.config), organization=options.organization)
            payload, error = read_inventory(client, cancel=cancel)
            if cancel.is_set():
                return 143
            if error:
                message = ('Choose an organization with --organization ID.'
                           if error == 'organization_required' else 'Inventory request failed: ' + error)
                print(message, file=sys.stderr)
                return EXIT_ERROR
            print(render_inventory(payload, details=options.command == 'inventory',
                                   as_json=options.json), end='')
            return EXIT_OK if payload['ready'] else EXIT_DEGRADED
        except ConfigurationError:
            print('Invalid client configuration', file=sys.stderr)
            return EXIT_ERROR
        except KeyboardInterrupt:
            return 130
        except BrokenPipeError:
            return EXIT_ERROR
        finally:
            if previous is not None:
                signal.signal(signal.SIGTERM, previous)
    try:
        config = load_config(options.config)
        if options.command == 'check-config':
            print('Configuration valid')
        else:
            from atraxis.server import serve
            serve(config)
        return 0
    except (ConfigurationError, OSError, ValueError):
        print('Configuration or server startup failed', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
