#!/usr/bin/env python3
"""Disposable, network-isolated image acceptance with synthetic credentials only."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
HARDENING = ['--network', 'none', '--read-only', '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m',
             '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--pids-limit', '128',
             '--memory', '512m', '--cpus', '2', '--env', 'PYTHONDONTWRITEBYTECODE=1']
PROBE = '''
import base64, http.client, json, os, time
assert os.getuid() == 10001 and os.getgid() == 10001
headers = {'Authorization': 'Basic ' + base64.b64encode(b'reader:fixture-password').decode()}
for attempt in range(100):
    try:
        conn = http.client.HTTPConnection('127.0.0.1', 9810, timeout=1)
        conn.request('GET', '/healthz', headers=headers)
        response = conn.getresponse()
        assert response.status == 200 and json.loads(response.read()) == {'alive': True}
        conn.close()
        break
    except OSError:
        time.sleep(.1)
else:
    raise AssertionError('listener_startup_failed')
for path, expected, auth in (('/v1/status', 401, {}), ('/readyz', 503, headers),
                              ('/v1/status', 200, headers)):
    conn = http.client.HTTPConnection('127.0.0.1', 9810, timeout=2)
    conn.request('GET', path, headers=auth)
    response = conn.getresponse()
    assert response.status == expected
    body = response.read()
    if path == '/v1/status' and auth:
        status = json.loads(body)
        assert not status['ready'] and status['sources'] == []
    conn.close()
print('Default entrypoint, UID/GID 10001, authentication and empty-source readiness: passed')
'''


def docker(*args, expected=0, timeout=120):
    result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode != expected:
        raise RuntimeError('docker_command_failed\n' + result.stdout + result.stderr)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', required=True)
    image = parser.parse_args().image
    prefix = 'atraxis-acceptance-' + uuid.uuid4().hex
    daemon, integration = prefix + '-daemon', prefix + '-integration'
    with tempfile.TemporaryDirectory(prefix='atraxis-fixture-') as directory:
        fixtures = Path(directory)
        fixtures.chmod(0o755)
        hashed = base64.b64encode(hashlib.sha1(b'fixture-password').digest()).decode()
        (fixtures / 'users').write_text('reader:{SHA}' + hashed + '\n')
        (fixtures / 'atraxis.yml').write_text('general:\n  auth_basic_file: users\nsources: []\n')
        (fixtures / 'client.yml').write_text('origin: http://127.0.0.1:9810\n'
            'allow_loopback_http: true\ncredentials_file: client.json\n')
        (fixtures / 'client.json').write_text(json.dumps({'username': 'reader', 'password': 'fixture-password'}))
        for path in fixtures.iterdir():
            path.chmod(0o644)  # Synthetic fixture only; accessible to container UID 10001.
        try:
            docker('run', '-d', '--name', daemon, *HARDENING,
                   '-v', str(fixtures) + ':/etc/atraxis:ro', image)
            print(docker('exec', daemon, 'python', '-c', PROBE).stdout, end='')
            status = docker('exec', daemon, 'atraxis', 'status', '--json', expected=2)
            assert json.loads(status.stdout)['reason'] == 'collectors_not_configured'
            before = time.monotonic()
            docker('stop', '--time', '10', daemon, timeout=20)
            state = json.loads(docker('inspect', '--format', '{{json .State}}', daemon).stdout)
            assert state['ExitCode'] == 0 and not state['OOMKilled'] and not state['Running']
            print('Default PID 1 SIGTERM: clean exit in %.3f seconds' % (time.monotonic() - before))
            # Discovery covers the entire mounted suite; this image acceptance
            # intentionally executes the five end-to-end runtime scenarios.
            check = docker('run', '--rm', '--name', integration, *HARDENING,
                '-v', str(ROOT / 'tests') + ':/acceptance/tests:ro',
                '-v', str(ROOT / 'scripts') + ':/acceptance/scripts:ro',
                '-v', str(ROOT / 'compose_tests') + ':/acceptance/compose_tests:ro',
                '-v', str(ROOT / 'deploy/compose/locks/record-environment.py') +
                    ':/acceptance/deploy/compose/locks/record-environment.py:ro',
                '--workdir', '/tmp', '--entrypoint', 'python', image,
                '/acceptance/scripts/check-test-collection.py', '--runner', 'unittest', '/acceptance/tests')
            print(check.stdout, end='')
            result = docker('run', '--rm', '--name', integration, *HARDENING,
                '-v', str(ROOT / 'tests') + ':/acceptance/tests:ro',
                '--workdir', '/tmp', '--entrypoint', 'python', image,
                '-m', 'unittest', 'discover', '-s', '/acceptance/tests', '-p', 'test_inventory_runtime.py', '-v')
            print(result.stdout + result.stderr, end='')
            print('Docker acceptance passed: isolated fixtures, no image push or deployment')
        finally:
            # Only containers created by this invocation; never prune the daemon.
            for name in (daemon, integration):
                logs = subprocess.run(['docker', 'logs', name], capture_output=True, text=True, timeout=20)
                if logs.returncode == 0 and (logs.stdout or logs.stderr):
                    print(logs.stdout + logs.stderr, end='')
                subprocess.run(['docker', 'rm', '-f', name], capture_output=True, timeout=20)


if __name__ == '__main__':
    main()
