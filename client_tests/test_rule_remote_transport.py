"""Network boundary tests independent of the optional private Atropos package."""
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import yaml

from atropos_client.config import ConfigurationError
from atraxis.rule_remote import RemoteRuleOperator, RemoteRuleError, load_remote_config
from atraxis.rule_remote_worker import fetch
from atraxis.rule_protocol import MAX_RESPONSE_BYTES, command


class RemoteTransportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.token = secrets.token_urlsafe(32)
        self.tokenfile = self.root/'access.token'
        self.tokenfile.write_text(self.token); self.tokenfile.chmod(0o600)
        self.profile = self.root/'remote.yml'
        self.requests = []

    def server(self, *, status=200, body=b'{"result":{"ok":true}}', headers=None, delay=0, tls=None):
        received = self.requests
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append((self.path, self.headers.get('Authorization'), self.rfile.read(int(self.headers['Content-Length']))))
                time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header('Content-Type','application/json')
                    self.send_header('X-Rule-Service','atraxis')
                    self.send_header('Content-Length',str(len(body)))
                    for key,value in headers or []: self.send_header(key,value)
                    self.end_headers(); self.wfile.write(body)
                except OSError: pass
            def log_message(self,*args): pass
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls)
            server.socket = context.wrap_socket(server.socket,server_side=True)
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(lambda:(server.shutdown(),server.server_close(),thread.join()))
        return server

    def client(self, server, **changes):
        document = {'origin':'http://127.0.0.1:%d'%server.server_port,'allow_loopback_http':True,
                    'token_file':str(self.tokenfile),'timeout':2}
        document.update(changes)
        self.profile.write_text(yaml.safe_dump(document)); self.profile.chmod(0o600)
        return RemoteRuleOperator(self.profile)

    def test_private_profiles_tokens_and_explicit_https(self):
        server = self.server(); self.client(server)
        for change in ({'origin':'http://192.0.2.1'}, {'origin':'https://name.invalid/path','allow_loopback_http':False},
                       {'origin':'https://user:pass@name.invalid','allow_loopback_http':False},
                       {'timeout':True}, {'actor':'alice'}, {'allow_loopback_http':1}):
            with self.subTest(change=change), self.assertRaises(ConfigurationError): self.client(server,**change)
        self.client(server); self.profile.chmod(0o644)
        with self.assertRaises(ConfigurationError): load_remote_config(self.profile)
        self.profile.chmod(0o600); self.tokenfile.chmod(0o644)
        with self.assertRaises(ConfigurationError): load_remote_config(self.profile)
        self.tokenfile.chmod(0o600)
        link=self.root/'token-link';link.symlink_to(self.tokenfile)
        with self.assertRaises(ConfigurationError):self.client(server,token_file=str(link))
        self.assertEqual(self.requests,[])

    def test_no_redirect_and_no_ambient_proxy_or_credential_fallback(self):
        server=self.server(status=302,headers=[('Location','http://127.0.0.1:1/stolen')])
        client=self.client(server)
        with patch.dict('os.environ',{'HTTP_PROXY':'http://127.0.0.1:1','HTTPS_PROXY':'http://127.0.0.1:1','ALL_PROXY':'http://127.0.0.1:1'}):
            with self.assertRaisesRegex(RemoteRuleError,'rule_service_unavailable'):client.catalog()
        self.assertEqual(len(self.requests),1)
        self.assertEqual(self.requests[0][:2],('/v1/rules/command','Bearer '+self.token))

    def test_deadline_kills_worker_and_mutations_are_never_retried(self):
        client=self.client(self.server(delay=2),timeout=1)
        started=time.monotonic()
        with self.assertRaisesRegex(RemoteRuleError,'rule_outcome_unknown'):
            client.deactivate('cpu',expected_generation='0'*32)
        self.assertLess(time.monotonic()-started,1.8)
        self.assertEqual(len(self.requests),1)

    def test_transport_and_server_errors_never_echo_response_secrets(self):
        for status in (401,403,409,500,503):
            client=self.client(self.server(status=status,body=b'SECRET_MARKER'))
            with self.subTest(status=status), self.assertRaises((PermissionError,RemoteRuleError)) as caught:
                client.deactivate('cpu',expected_generation=None)
            self.assertNotIn('SECRET_MARKER',str(caught.exception))
            if status >= 500:self.assertEqual(str(caught.exception),'rule_outcome_unknown')

    def test_malformed_success_response_is_not_reported_as_success(self):
        for body,headers in ((b'{"result":NaN}',None),(b'{"result":0,"result":1}',None),
                             (b'{"error":"SECRET"}',None),(b'{"result":0}', [('Content-Encoding','gzip')]),
                             (b'{}',[('Content-Length',str(MAX_RESPONSE_BYTES+1))])):
            client=self.client(self.server(body=body,headers=headers))
            with self.assertRaisesRegex(RemoteRuleError,'rule_outcome_unknown'):
                client.deactivate('cpu',expected_generation=None)

    def certificate(self,name,san):
        cert,key=self.root/(name+'.pem'),self.root/(name+'.key')
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
             '-subj','/CN=fixture','-addext','subjectAltName='+san,'-keyout',str(key),'-out',str(cert)],
             check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
        return cert,key

    def test_real_tls_ca_and_hostname_are_verified(self):
        cert,key=self.certificate('valid','IP:127.0.0.1')
        server=self.server(tls=(cert,key))
        client=self.client(server,origin='https://127.0.0.1:%d'%server.server_port,
                           allow_loopback_http=False,ca_file=str(cert))
        self.assertEqual(client.catalog(),{'ok':True})
        untrusted=self.client(server,origin=client.config.origin,allow_loopback_http=False)
        with self.assertRaises(RemoteRuleError):untrusted.catalog()
        cert,key=self.certificate('wrong','DNS:wrong.invalid')
        server=self.server(tls=(cert,key))
        wrong=self.client(server,origin='https://127.0.0.1:%d'%server.server_port,
                           allow_loopback_http=False,ca_file=str(cert))
        with self.assertRaises(RemoteRuleError):wrong.catalog()
        self.assertEqual(len(self.requests),1)

    def test_request_validation_happens_before_network(self):
        client=self.client(self.server())
        with self.assertRaises(TypeError):client.catalog(actor='admin')
        with self.assertRaises(AttributeError):client.execute()
        with self.assertRaises(RemoteRuleError):client.import_draft('x'*524288,rule_kind='intervention',selections=[],expected_revision=None)
        with self.assertRaises(ValueError):command(b'{"method":"catalog","arguments":{},"actor":"admin"}')
        self.assertEqual(self.requests,[])

    def test_token_is_reloaded_and_removed_credentials_fail_closed(self):
        client=self.client(self.server());client.catalog()
        self.tokenfile.unlink()
        with self.assertRaises(RemoteRuleError):client.catalog()
        self.assertEqual(len(self.requests),1)

    def test_environment_only_cli_uses_verified_tls_and_never_prints_token(self):
        import os
        cert,key=self.certificate('environment','IP:127.0.0.1')
        server=self.server(tls=(cert,key))
        env=dict(os.environ, ATRAXIS_SERVER_URL='https://127.0.0.1:%d'%server.server_port,
                 ATRAXIS_TOKEN=self.token, ATRAXIS_CA_FILE=str(cert), ATRAXIS_TIMEOUT='3')
        env.pop('ATRAXIS_TOKEN_FILE',None)
        completed=subprocess.run([sys.executable,'-m','atraxis.cli','rules','--remote','catalog'],
                                 env=env,capture_output=True,text=True,timeout=10)
        self.assertEqual(completed.returncode,0,completed.stderr)
        self.assertEqual(json.loads(completed.stdout),{'ok':True})
        self.assertNotIn(self.token,completed.stdout+completed.stderr)
        self.assertEqual(self.requests[0][1],'Bearer '+self.token)
        env['ATRAXIS_TOKEN_FILE']=str(self.tokenfile)
        rejected=subprocess.run([sys.executable,'-m','atraxis.cli','rules','--remote','catalog'],
                                env=env,capture_output=True,text=True,timeout=10)
        self.assertEqual(rejected.returncode,2)
        self.assertNotIn(self.token,rejected.stdout+rejected.stderr)
        self.assertEqual(len(self.requests),1)

    def test_environment_token_makes_one_request_and_unknown_mutation_stays_unknown(self):
        import os
        cert,key=self.certificate('unknown-environment','IP:127.0.0.1')
        server=self.server(status=503,tls=(cert,key))
        with patch.dict(os.environ,{'ATRAXIS_SERVER_URL':'https://127.0.0.1:%d'%server.server_port,
             'ATRAXIS_TOKEN':self.token,'ATRAXIS_CA_FILE':str(cert)},clear=True):
            client=RemoteRuleOperator()
        with patch.dict(os.environ, {'ATRAXIS_TOKEN': self.token, 'ATROPOS_TOKEN': self.token}), \
             patch('atropos_client.client.subprocess.run',wraps=subprocess.run) as run:
            with self.assertRaisesRegex(RemoteRuleError,'rule_outcome_unknown'):
                client.deactivate('cpu',expected_generation=None)
        self.assertEqual(len(self.requests),1)
        self.assertEqual(run.call_count,1)
        self.assertNotIn(self.token,repr(run.call_args.args))
        self.assertEqual(json.loads(run.call_args.kwargs['input'])['token'],self.token)
        self.assertNotIn('ATRAXIS_TOKEN', run.call_args.kwargs['env'])
        self.assertNotIn('ATROPOS_TOKEN', run.call_args.kwargs['env'])

    def test_queue_full_is_known_only_for_the_verified_service_error_envelope(self):
        for body,headers,expected in [
            (b'{"error":"rule_queue_full"}',None,'rule_queue_full'),
            (b'{"error":"other"}',None,'rule_outcome_unknown'),
            (b'{"error":"rule_queue_full"}',[('X-Rule-Service','wrong')],'rule_outcome_unknown'),
            (b'x'*1025,None,'rule_outcome_unknown')]:
            with self.subTest(expected=expected):
                before=len(self.requests)
                client=self.client(self.server(status=429,body=body,headers=headers))
                with self.assertRaisesRegex(RemoteRuleError,expected):
                    client.deactivate('cpu',expected_generation=None)
                self.assertEqual(len(self.requests),before+1)
