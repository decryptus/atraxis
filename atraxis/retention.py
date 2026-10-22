"""Offline retention headroom; no account connection, pruning or implicit backup."""
import hashlib
import json
import sys

import yaml
from sonicprobe.libs import xys
from atraxis import account_backup
from atraxis.config import _StrictLoader, _MAX_CONFIG_BYTES, CONFIG_SCHEMA
from atraxis.configuration_admin import MAX_HISTORY, validate_history
from atraxis.rule_files import read_private

WARNING_PERCENT = 80
REPORT_VERSION = 1


def capacity(used, limit, unit):
    return dict(used=used, limit=limit, remaining=max(0,limit-used), unit=unit,
                warning=used*100 >= limit*WARNING_PERCENT)


def inspect_file(path, *, accounts=False):
    maximum = account_backup.MAX_BYTES if accounts else _MAX_CONFIG_BYTES
    raw = read_private(path, maximum)
    report = dict(version=REPORT_VERSION, kind='account_archive' if accounts else 'daemon_configuration',
        warning_percent=WARNING_PERCENT, policy='retain_all_evidence', purge_supported=False,
        restore_verified=False, capacity={'file_bytes':capacity(len(raw), maximum, 'bytes')})
    if accounts:
        payload = account_backup.decode_backup(raw)
        report['revision'] = payload['snapshot']['revision']
        report['sha256'] = hashlib.sha256(account_backup.canonical(payload)).hexdigest()
        report['capacity']['audit_entries'] = capacity(len(payload['snapshot']['audit']), account_backup.MAX_AUDIT_ROWS, 'events')
    else:
        document = yaml.load(raw, Loader=_StrictLoader)
        if not xys.validate(document, CONFIG_SCHEMA):raise ValueError('invalid_configuration')
        history = document.get('configuration_history', [])
        validate_history(history)
        report['sha256'] = hashlib.sha256(raw).hexdigest()
        report['capacity']['configuration_events'] = capacity(len(history), MAX_HISTORY, 'events')
        report['runtime_validated'] = False  # No referenced profile, credential or endpoint is opened.
    report['attention_required'] = any(metric['warning'] for metric in report['capacity'].values())
    return report


def add_parser(commands):
    parser = commands.add_parser('retention', help='inspect archive/configuration headroom offline; never delete evidence')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--accounts-backup')
    group.add_argument('--config')


def main(options):
    try:
        report = inspect_file(options.accounts_backup or options.config, accounts=bool(options.accounts_backup))
        print(json.dumps(report, indent=2))
        return 1 if report['attention_required'] else 0
    except (OSError, ValueError, TypeError, KeyError, RecursionError, yaml.YAMLError):
        print('Retention inspection failed; check the private file, format and integrity.', file=sys.stderr)
        return 2
