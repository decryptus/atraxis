"""Bounded, read-only HTTPS qualification with an explicit destination capability."""
import base64
import http.client
import json
import socket
import ssl
import time
from urllib.parse import urlsplit

from atraxis.installation.validation import (InstallError, MAX_TLS_BYTES, credentials,
    origin, require, unique, reject_constant, KINDS)
from atraxis.installation.worker import run
from atraxis.models import Freshness
from atraxis.source_adapters import DecodeContext, get_adapter

PROBE_TIMEOUT = 5.0
RESPONSE_BYTES = 4 * 1024 * 1024
MAX_DNS_ANSWERS = 32
MAX_AGE = 60
MAX_RESOURCES = 10000
MAX_CLOCK_SKEW = 2


def context(ca):
    """Explicit PEM trust only; no environment-selected CA files or client keys."""
    try:
        require(type(ca) is bytes and 0 < len(ca) <= MAX_TLS_BYTES)
        value = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        value.minimum_version = ssl.TLSVersion.TLSv1_2
        value.load_verify_locations(cadata=ca.decode('ascii'))
        return value
    except Exception:
        raise InstallError('invalid_tls', 'sources') from None


def resolve(host, port, authority):
    answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    require(0 < len(answers) <= MAX_DNS_ANSWERS, 'destination_denied', 'sources')
    addresses = []
    for family, kind, protocol, _canonical, endpoint in answers:
        require(family in (socket.AF_INET, socket.AF_INET6) and kind == socket.SOCK_STREAM
                and protocol == socket.IPPROTO_TCP, 'destination_denied', 'sources')
        ip = authority.check_address(endpoint[0])
        require(ip.version == (4 if family == socket.AF_INET else 6), 'destination_denied', 'sources')
        if family == socket.AF_INET6:
            require(endpoint[2:] == (0, 0), 'destination_denied', 'sources')
        addresses.append((family, str(ip)))
    # Every answer must be authorized. Connection never resolves the name again.
    return sorted(set(addresses))[0]


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port, endpoint, tls):
        super().__init__(host, port, timeout=PROBE_TIMEOUT, context=tls)
        self.endpoint = endpoint

    def connect(self):
        family, ip = self.endpoint
        raw = socket.socket(family, socket.SOCK_STREAM)
        try:
            raw.settimeout(PROBE_TIMEOUT)
            raw.connect((ip, self.port))
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _request(source, secret, ca, authority):
    """Runs only in a disposable worker; the parent also bounds DNS and decoding."""
    authority.validate()
    require(source.kind in KINDS and origin(source.origin) in authority.origins,
            'destination_denied', 'sources')
    secret = credentials(secret)
    tls = context(ca)
    parsed = urlsplit(source.origin)
    endpoint = resolve(parsed.hostname, parsed.port or 443, authority)
    connection = PinnedHTTPS(parsed.hostname, parsed.port or 443, endpoint, tls)
    start = time.monotonic()
    try:
        encoded = base64.b64encode((secret['username'] + ':' + secret['password']).encode('utf-8')).decode('ascii')
        connection.request('GET', get_adapter(source.kind).ROUTE,
            headers={'Authorization': 'Basic ' + encoded, 'Accept': 'application/json',
                     'Accept-Encoding': 'identity', 'Connection': 'close'})
        response = connection.getresponse()
        require(not 300 <= response.status < 400, 'redirect_refused', 'sources')
        require(response.status not in (401, 403), 'authentication_failed', 'sources')
        require(response.status == 200, 'invalid_response', 'sources')
        require(response.getheader('Content-Type', '').split(';')[0].strip().lower() == 'application/json'
                and response.getheader('Content-Encoding', 'identity') == 'identity', 'invalid_response', 'sources')
        data = response.read(RESPONSE_BYTES + 1)
        require(len(data) <= RESPONSE_BYTES, 'invalid_response', 'sources')
        payload = json.loads(data.decode('utf-8'), object_pairs_hook=unique, parse_constant=reject_constant)
        now = time.monotonic()
        snapshot = get_adapter(source.kind).decode(payload, DecodeContext(source.source_id,
            time.time(), now, MAX_AGE, MAX_RESOURCES, MAX_CLOCK_SKEW, now - start))
        require(snapshot.authoritative and snapshot.freshness == Freshness.FRESH, 'source_unready', 'sources')
    finally:
        connection.close()


def _worker(pipe, source, secret, ca, authority):
    try:
        _request(source, secret, ca, authority)
        outcome = 'ok'
    except InstallError as error:
        outcome = error.code
    except ssl.SSLError:
        outcome = 'tls_failed'
    except (ValueError, UnicodeError, RecursionError):
        outcome = 'invalid_response'
    except BaseException:
        # No exception text, HTTP body, URL or credential leaves this process.
        outcome = 'probe_failed'
    try:
        pipe.send_bytes(outcome.encode('ascii'))
    finally:
        pipe.close()


def check(source, secret, ca, authority):
    """One bounded worker per source, secrets through an anonymous pipe, never argv.

    Spawn avoids inheriting the install-directory capability and listener socket.
    No retries, proxy discovery, redirects or source mutations.
    """
    run(_worker, (source, secret, ca, authority), field='sources',
        timeout_code='probe_timeout', failure_code='probe_failed', timeout=PROBE_TIMEOUT)
