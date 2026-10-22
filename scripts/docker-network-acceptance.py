#!/usr/bin/env python3
"""Disposable network namespaces, real nginx, verified TLS and fault injection.

Requires Docker and cryptography in the host test environment. No published ports,
external services, production credentials or image publication.
"""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[1]
HARDENING = ['--read-only', '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m',
             '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
             '--pids-limit', '128', '--memory', '512m', '--cpus', '2',
             '--env', 'PYTHONDONTWRITEBYTECODE=1']


def docker(*args, timeout=120, check=True):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError('docker_command_failed\n' + result.stdout + result.stderr)
    return result


class Authority:
    def __init__(self, name):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
        now = datetime.now(timezone.utc)
        self.cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(self.key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(self.key, hashes.SHA256()))

    def leaf(self, root, variant, host, *, expired=False, lifetime_hours=2):
        if lifetime_hours not in (2, 4, 8):
            raise ValueError('unsupported_fixture_certificate_lifetime')
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(timezone.utc)
        cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(self.cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=2))
            .not_valid_after(now - timedelta(days=1) if expired else now + timedelta(hours=lifetime_hours))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
            .sign(self.key, hashes.SHA256()))
        (root / (variant + '.crt')).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (root / (variant + '.key')).write_bytes(key.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))

    def pem(self):
        return self.cert.public_bytes(serialization.Encoding.PEM)


def fixtures(root):
    authority, untrusted = Authority('acceptance CA'), Authority('untrusted acceptance CA')
    for role in ('monitor', 'certificates', 'atraxis', 'gateway', 'client'):
        directory = root / role
        directory.mkdir()
        (directory / 'ca.pem').write_bytes(authority.pem())
    for role in ('monitor', 'certificates', 'gateway'):
        directory = root / role
        authority.leaf(directory, 'ok', role + '.test')
        if role == 'monitor':
            authority.leaf(directory, 'wrong-host', 'wrong.test')
            authority.leaf(directory, 'expired', 'monitor.test', expired=True)
            untrusted.leaf(directory, 'untrusted', 'monitor.test')
    (root / 'client/untrusted-ca.pem').write_bytes(untrusted.pem())
    password = base64.b64encode(hashlib.sha1(b'synthetic-reader-password').digest()).decode()
    (root / 'atraxis/users').write_text('reader:{SHA}' + password + '\n')
    (root / 'atraxis/outbound.json').write_text(json.dumps(
        dict(username='source-reader', password='synthetic-source-password')))
    config = {'general': {'auth_basic_file': 'users', 'listen_addr': '0.0.0.0',
                          'behind_tls_proxy': True}, 'sources': [
        dict(source_id=role, kind=kind, origin='https://' + role + '.test:8443',
             ca_file='ca.pem', credentials_file='outbound.json',
             poll_interval=2, timeout=2, max_age=60)
        for role, kind in (('monitor', 'monit_docker'), ('certificates', 'certlord'))]}
    # JSON is also YAML; avoid adding a YAML dependency to the host harness.
    (root / 'atraxis/atraxis.yml').write_text(json.dumps(config))
    (root / 'client/client.json').write_text(json.dumps(
        dict(username='reader', password='synthetic-reader-password')))
    for name, ca in (('client', 'ca.pem'), ('bad-ca', 'untrusted-ca.pem')):
        (root / 'client' / (name + '.yml')).write_text(json.dumps(
            dict(origin='https://gateway.test:8443', ca_file=ca,
                 credentials_file='client.json', timeout=2)))
    (root / 'gateway/nginx.conf').write_text('''
pid /tmp/nginx.pid;
error_log /dev/stderr warn;
events { worker_connections 128; }
http {
  access_log off;
  client_body_temp_path /tmp/client;
  proxy_temp_path /tmp/proxy;
  fastcgi_temp_path /tmp/fastcgi;
  uwsgi_temp_path /tmp/uwsgi;
  scgi_temp_path /tmp/scgi;
  server {
    listen 8443 ssl;
    server_name gateway.test;
    ssl_certificate /fixtures/ok.crt;
    ssl_certificate_key /fixtures/ok.key;
    ssl_protocols TLSv1.2 TLSv1.3;
    location / {
      proxy_pass http://atraxis.test:9810;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-User "";
      proxy_set_header X-Remote-User "";
      proxy_set_header X-Forwarded-For "";
      proxy_connect_timeout 2s;
      proxy_read_timeout 5s;
      proxy_next_upstream off;
    }
  }
}
''')
    # Disposable synthetic secrets only. Each container sees only its own role's
    # fixtures, read-only; no host credentials or daemon socket are mounted.
    root.chmod(0o755)
    for path in root.rglob('*'):
        path.chmod(0o755 if path.is_dir() else 0o644)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', required=True)
    parser.add_argument('--ingress-image', required=True)
    args = parser.parse_args()
    prefix = 'atraxis-network-' + uuid.uuid4().hex
    networks = {name: prefix + '-' + name for name in ('edge', 'backend', 'sources')}
    containers = {name: prefix + '-' + name for name in
                  ('monitor', 'certificates', 'atraxis', 'gateway', 'client')}
    with tempfile.TemporaryDirectory(prefix='atraxis-network-') as directory:
        root = Path(directory)
        fixtures(root)
        try:
            for network in networks.values():
                docker('network', 'create', '--internal', network)

            def create(role, network, command, *, ingress=False):
                return docker('create', '--name', containers[role], *HARDENING,
                    '--network', networks[network], '--network-alias', role + '.test',
                    '-v', str(root / role) + ':/fixtures:ro',
                    '-v', str(ROOT / 'scripts/network/fixture.py') + ':/fixture.py:ro',
                    '--entrypoint', 'nginx' if ingress else 'python',
                    args.ingress_image if ingress else args.image, *command)

            for role in ('monitor', 'certificates'):
                create(role, 'sources', ['/fixture.py', 'source'])
                docker('start', containers[role])
            create('atraxis', 'backend', ['-m', 'atraxis', 'serve', '--config', '/fixtures/atraxis.yml'])
            docker('network', 'connect', '--alias', 'atraxis.test', networks['sources'], containers['atraxis'])
            docker('start', containers['atraxis'])
            create('gateway', 'backend', ['-c', '/fixtures/nginx.conf', '-g', 'daemon off;'], ingress=True)
            docker('network', 'connect', '--alias', 'gateway.test', '--alias', 'wrong.test',
                   networks['edge'], containers['gateway'])
            docker('start', containers['gateway'])
            create('client', 'edge', ['-c', 'import time; time.sleep(3600)'])
            docker('start', containers['client'])

            def probe(*command):
                result = docker('exec', containers['client'], 'python', '/fixture.py', *command, timeout=40)
                print(result.stdout, end='', flush=True)

            def state(expected):
                probe('wait', expected)

            def mode(value):
                docker('exec', containers['monitor'], 'python', '-c',
                       'from pathlib import Path; Path("/tmp/mode").write_text(' + repr(value) + ')')

            state('healthy')
            addresses = json.loads(docker('inspect', containers['atraxis']).stdout)[0]['NetworkSettings']['Networks']
            probe('security', *(value['IPAddress'] for value in addresses.values()))
            for config, expected in (('client', 0), ('bad-ca', 1)):
                result = docker('exec', containers['client'], 'atraxis', 'inventory', '--json',
                    '--config', '/fixtures/' + config + '.yml', check=False)
                assert result.returncode == expected, result.stderr
                if expected == 0:
                    assert json.loads(result.stdout)['ready']
                else:
                    assert 'tls_error' in result.stderr and not result.stdout
            print('Installed remote CLI and CA rejection: passed', flush=True)
            docker('network', 'disconnect', networks['sources'], containers['monitor'])
            state('disconnected')
            result = docker('exec', containers['client'], 'atraxis', 'inventory', '--json',
                '--config', '/fixtures/client.yml', check=False)
            assert result.returncode == 2 and not json.loads(result.stdout)['ready']
            docker('network', 'connect', '--alias', 'monitor.test', networks['sources'], containers['monitor'])
            state('healthy')
            for value, expected in (('wrong-host', 'tls_error'), ('expired', 'tls_error'),
                                    ('untrusted', 'tls_error'), ('silent', 'timeout'),
                                    ('redirect', 'redirect_refused')):
                mode(value)
                state(expected)
                mode('ok')
                state('healthy')
            for role, route in (('monitor', '/v1/status'), ('certificates', '/api/certificates/observations')):
                rows = docker('exec', containers[role], 'python', '-c',
                              'print(open("/tmp/requests.jsonl").read())').stdout.splitlines()
                requests = [json.loads(row) for row in rows if row]
                assert requests and all(row['method'] == 'GET' and row['path'] == route
                                        and row['authorized'] for row in requests)
            print('Read-only source requests; redirect credentials never forwarded: passed', flush=True)
            docker('stop', '--time', '10', containers['atraxis'], timeout=20)
            state_data = json.loads(docker('inspect', containers['atraxis']).stdout)[0]['State']
            assert state_data['ExitCode'] == 0 and not state_data['OOMKilled']
            print('Network acceptance passed; no deployment or physical multi-host claim', flush=True)
        finally:
            for container in reversed(tuple(containers.values())):
                logs = docker('logs', container, timeout=20, check=False)
                if logs.returncode == 0:
                    # Logs only come from these disposable synthetic services.
                    print(logs.stdout + logs.stderr, end='')
                docker('rm', '-f', container, timeout=20, check=False)
            for network in reversed(tuple(networks.values())):
                docker('network', 'rm', network, timeout=20, check=False)


if __name__ == '__main__':
    main()
