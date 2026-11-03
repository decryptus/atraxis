"""Bound expensive untrusted PEM/key validation before any filesystem/network effect."""
from atraxis.installation.probe import context
from atraxis.installation.tls import validate_tls
from atraxis.installation.validation import MAX_SOURCES, MAX_TLS_BYTES, require
from atraxis.installation.worker import run, WORKER_TIMEOUT


def _worker(pipe, certificate, private_key, server_name, cas):
    try:
        validate_tls(certificate, private_key, server_name)
        for ca in cas: context(ca)
        outcome = 'ok'
    except BaseException:
        outcome = 'invalid_tls'
    try:
        pipe.send_bytes(outcome.encode('ascii'))
    finally:
        pipe.close()


def check(certificate, private_key, server_name, cas):
    require(type(cas) is tuple and 1 <= len(cas) <= MAX_SOURCES, 'invalid_tls', 'tls')
    for value in (certificate, private_key, *cas):
        require(type(value) is bytes and 0 < len(value) <= MAX_TLS_BYTES, 'invalid_tls', 'tls')
    run(_worker, (certificate, private_key, server_name, cas), field='tls',
        timeout_code='validation_timeout', failure_code='invalid_tls', timeout=WORKER_TIMEOUT)
