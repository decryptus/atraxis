"""Generate and start new, host-authorized inventory-only installations."""
import base64
import http.client
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import time

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

from atraxis.installation.validation import InstallError, require

MODES = ('compose', 'systemd')
SAFE_PATH = re.compile(r'/[A-Za-z0-9_./-]+')
DOCKER = '/usr/bin/docker'
SYSTEMCTL = '/usr/bin/systemctl'
WEB_IMAGE = 'decryptus/atraxis:1.0.5@sha256:092d8494279ff253f3cd2287c83f809a341f5835ab837621e8549354300b980e'
PROXY_IMAGE = 'nginx:1.30.6@sha256:b565a9579cdabe43de09a5a278dc95ceee721dae1cacf85247effc4f6630d511'
NGINX = b"""pid /tmp/nginx.pid;
error_log /dev/stderr warn;
events { worker_connections 128; }
http {
    access_log off;
    limit_req_zone $binary_remote_addr zone=atraxis_clients:1m rate=10r/s;
    limit_req_status 429;
    client_body_temp_path /tmp/client;
    proxy_temp_path /tmp/proxy;
    fastcgi_temp_path /tmp/fastcgi;
    uwsgi_temp_path /tmp/uwsgi;
    scgi_temp_path /tmp/scgi;
    server {
        listen 8443 ssl;
        ssl_certificate /tls/server.crt;
        ssl_certificate_key /tls/server.key;
        ssl_protocols TLSv1.2 TLSv1.3;
        location / {
            limit_req zone=atraxis_clients burst=20 nodelay;
            proxy_pass http://127.0.0.1:9810;
            proxy_set_header Host $http_host;
            proxy_set_header X-Forwarded-For $remote_addr;
            proxy_next_upstream off;
        }
    }
}
"""


def host_directory(root, name):
    physical = os.readlink(f'/proc/self/fd/{root.fd}')
    require(SAFE_PATH.fullmatch(physical) and Path(physical).is_absolute()
            and os.path.samestat(os.stat(physical), os.fstat(root.fd)), 'unsafe_root', 'destination')
    return Path(physical) / name


def _mount(filename, destination):
    return dict(type='bind', source='./' + filename, target=destination, read_only=True,
                bind={'create_host_path': False})


def _compose(plan, public_files):
    def service(image):
        return dict(image=image, platform='linux/amd64', user=f'{os.geteuid()}:{os.getegid()}',
                    read_only=True, cap_drop=['ALL'], security_opt=['no-new-privileges:true'],
                    pids_limit=128, mem_limit='512m', cpus=2, restart='no',
                    tmpfs=['/tmp:rw,nosuid,nodev,noexec,size=32m,mode=1777'])

    proxy = service(PROXY_IMAGE)
    proxy.update(entrypoint=['nginx'], command=['-c', '/etc/nginx/nginx.conf', '-g', 'daemon off;'],
                 ports=[f'{plan.listen_address}:{plan.listen_port}:8443'],
                 volumes=[_mount('nginx.conf', '/etc/nginx/nginx.conf'),
                          _mount('server.crt', '/tls/server.crt'), _mount('server.key', '/tls/server.key')])
    web = service(WEB_IMAGE)
    web.update(network_mode='service:tls', depends_on=['tls'],
               volumes=[_mount(f, '/etc/atraxis/' + f) for f in public_files])
    return {'name': 'atraxis-' + plan.name, 'services': {'tls': proxy, 'web': web}}


def render(mode, plan, login, directory):
    require(mode in MODES, 'deployment_denied', 'destination')
    if sys.version_info >= (3, 13):
        import legacycrypt as crypt
    else:
        import crypt
    try:
        hashed = crypt.crypt(login['password'], crypt.mksalt(crypt.METHOD_SHA512, rounds=200000))
    except (ValueError, OSError):
        hashed = None
    require(hashed and hashed.startswith('$6$rounds=200000$'), 'invalid_secret', 'credentials')
    files = {'users.htpasswd': f"{login['username']}:{hashed}\n".encode()}
    sources = []
    mounted = ['atraxis.yml', 'users.htpasswd']
    for source in plan.sources:
        auth, ca = f'source-{source.source_id}.json', f'source-{source.source_id}.ca.pem'
        mounted.extend([auth, ca])
        sources.append(dict(source_id=source.source_id, kind=source.kind, origin=source.origin,
                            credentials_file=auth, ca_file=ca))
    config = dict(auth_basic_file='users.htpasswd',
                  listen_addr=plan.listen_address if mode == 'systemd' else '127.0.0.1',
                  listen_port=plan.listen_port if mode == 'systemd' else 9810)
    if mode == 'compose':
        config.update(behind_tls_proxy=True, trusted_proxy_networks=['127.0.0.1/32'])
    files['atraxis.yml'] = json.dumps(dict(general=config, sources=sources), indent=2).encode()
    if mode == 'compose':
        files['compose.yaml'] = json.dumps(_compose(plan, mounted), indent=2).encode()
        files['nginx.conf'] = NGINX
    else:
        require(SAFE_PATH.fullmatch(sys.executable) and SAFE_PATH.fullmatch(str(directory)),
                'unsafe_root', 'destination')
        files[f'atraxis-{plan.name}.service'] = f"""[Unit]
Description=Atraxis inventory ({plan.name})
[Service]
Type=exec
WorkingDirectory={directory}
ExecStart={sys.executable} -I -m atraxis serve --config atraxis.yml --tls-cert server.crt --tls-key server.key
UMask=0077
NoNewPrivileges=yes
Restart=no
[Install]
WantedBy=default.target
""".encode()
    return files


def _run(argv, directory, *, timeout=90):
    # Use only the local manager; ignore Compose overrides, remote endpoints and PATH injection.
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(Path.home()), 'LANG': 'C',
           'XDG_RUNTIME_DIR': f'/run/user/{os.geteuid()}'}
    try:
        return subprocess.run(argv, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout,
                              check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise InstallError('manager_unavailable', 'destination') from None


def preflight(mode, plan, parent):
    require(os.geteuid() != 0, 'deployment_denied', 'destination')
    if mode == 'systemd':
        result = _run([SYSTEMCTL, '--user', 'show', '--property=LoadState', '--value',
                       f'atraxis-{plan.name}.service'], parent)
        require(result.stdout.strip() == b'not-found' and result.returncode in (0, 4),
                'manager_unavailable' if not result.stdout else 'destination_exists', 'destination')
    else:
        probe = _run([DOCKER, '--host', 'unix:///var/run/docker.sock', 'info', '--format',
                      '{{json .SecurityOptions}}'], parent)
        require(probe.returncode == 0 and b'rootless' not in probe.stdout and b'userns' not in probe.stdout,
                'manager_unavailable', 'destination')
        version = _run([DOCKER, 'compose', 'version', '--short'], parent)
        detected = re.fullmatch(rb'v?(\d+)\.(\d+)\.\d+(?:[-+][\w.-]+)?', version.stdout.strip())
        require(version.returncode == 0 and detected
                and (int(detected[1]), int(detected[2])) >= (2, 24),
                'manager_unavailable', 'destination')
        for resource in ('container', 'network', 'volume'):
            cmd = [DOCKER, '--host', 'unix:///var/run/docker.sock', resource, 'ls', '-q']
            if resource == 'container':
                cmd.append('-a')
            result = _run(cmd + ['--filter', 'label=com.docker.compose.project=atraxis-' + plan.name], parent)
            require(result.returncode == 0 and not result.stdout.strip(), 'destination_exists', 'destination')


def start(mode, plan, directory):
    if mode == 'systemd':
        cmd = [SYSTEMCTL, '--user', 'enable', '--now', str(directory / f'atraxis-{plan.name}.service')]
    else:
        compose = [DOCKER, '--host', 'unix:///var/run/docker.sock', 'compose', '--env-file', '/dev/null',
                   '-p', 'atraxis-' + plan.name, '-f', str(directory / 'compose.yaml')]
        require(_run(compose + ['config', '--quiet'], directory).returncode == 0,
                'deployment_failed', 'destination')
        cmd = compose + ['up', '-d', '--no-build', '--no-recreate']
    try:
        started = _run(cmd, directory, timeout=300)
    except InstallError:
        raise InstallError('startup_uncertain', 'destination') from None
    require(started.returncode == 0, 'startup_uncertain', 'destination')


def verify(plan, certificate, login, timeout=45):
    leaf = x509.load_pem_x509_certificates(certificate)[0]
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.minimum_version = ssl.TLSVersion.TLSv1_2
    tls.load_verify_locations(cadata=leaf.public_bytes(Encoding.PEM).decode())
    tls.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    pinned = leaf.public_bytes(Encoding.DER)
    auth = base64.b64encode((login['username'] + ':' + login['password']).encode()).decode()
    denied_anonymous = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        connection = http.client.HTTPConnection(plan.listen_address, plan.listen_port, timeout=2)
        try:
            with socket.create_connection((plan.listen_address, plan.listen_port), timeout=2) as raw:
                connection.sock = tls.wrap_socket(raw, server_hostname=plan.server_name)
                require(connection.sock.getpeercert(binary_form=True) == pinned,
                        'startup_uncertain', 'tls')
                headers = {'Authorization': 'Basic ' + auth} if denied_anonymous else {}
                connection.request('GET', '/readyz', headers=headers)
                response = connection.getresponse()
                if not denied_anonymous:
                    if response.status == 401:
                        denied_anonymous = True
                    else:
                        require(response.status in (502, 503, 504), 'startup_uncertain', 'destination')
                elif response.status == 200 and json.loads(response.read(4097)) == {'ready': True, 'reason': None}:
                    return
        except InstallError:
            raise
        except (OSError, http.client.HTTPException, ValueError):
            pass
        finally:
            connection.close()
        time.sleep(0.5)
    raise InstallError('startup_uncertain', 'destination')
