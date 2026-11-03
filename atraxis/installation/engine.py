"""Internal preparation API shared by future UI/batch callers; not an installer CLI."""
import hashlib
import json
import socket

from atraxis.installation import probe
from atraxis.installation.filesystem import open_root
from atraxis.installation.tls import validate_tls
from atraxis.installation.validation import InstallError, credentials, decode, parse, require


def serialize(value):
    return (json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


class Engine:
    """Authority and root are trusted host capabilities, never form fields.

    Input materials are bytes, not arbitrary paths. An embedding application is
    responsible for securely acquiring them and must not log input objects.
    """
    def __init__(self, authority, root=None):
        authority.validate()
        self._authority = authority
        self._root = root

    def preview(self, raw):
        plan = parse(decode(raw))
        self._authority.check(plan)
        return {'status': 'unverified', 'plan': plan.document(), 'services_started': False}

    def prepare(self, raw, *, login, source_credentials, certificate, private_key, source_cas):
        # Preview is never a ticket to bypass checks. Reparse at the effect boundary.
        plan = parse(decode(raw))
        self._authority.check(plan)
        ids = {source.source_id for source in plan.sources}
        require(type(source_credentials) is dict and set(source_credentials) == ids,
                'invalid_secret', 'credentials')
        require(type(source_cas) is dict and set(source_cas) == ids, 'invalid_tls', 'sources')
        secrets = {key: credentials(value) for key, value in source_credentials.items()}
        cas = dict(source_cas)
        login = credentials(login, login=True)
        validate_tls(certificate, private_key, plan.server_name)
        for ca in cas.values(): probe.context(ca)
        files = {'plan.json': serialize(plan.document()), 'login.json': serialize(login),
                 'server.crt': certificate, 'server.key': private_key}
        for source in plan.sources:
            files['source-' + source.source_id + '.json'] = serialize(secrets[source.source_id])
            files['source-' + source.source_id + '.ca.pem'] = cas[source.source_id]
        files['complete.json'] = serialize({'version': 1, 'state': 'prepared',
            'sha256': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}})
        # Bind without SO_REUSEADDR/PORT and hold until publication; no listener is started.
        reservation = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        root = None
        try:
            try:
                reservation.bind((plan.listen_address, plan.listen_port))
            except OSError:
                raise InstallError('port_unavailable', 'listen_port') from None
            root = self._root if self._root is not None else open_root()
            root._check()
            for source in plan.sources:
                probe.check(source, secrets[source.source_id], cas[source.source_id], self._authority)
            return root.create(plan.name, files)
        finally:
            reservation.close()
            if root is not None and self._root is None: root.close()
