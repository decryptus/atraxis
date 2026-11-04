"""Qualify a new inventory deployment on a disposable CI host with real managers."""
import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from atraxis.installation.engine import Engine, serialize
from atraxis.installation.filesystem import Root, DIRECTORY_FLAGS
from atraxis.installation.validation import Authority
from test_installation import LOGIN, https_fixture, material, monit, plan


def main(mode):
    if os.environ.get('GITHUB_ACTIONS') != 'true' or os.geteuid() == 0:
        raise SystemExit('This acceptance requires a disposable non-root GitHub runner.')
    host = '127.0.0.1'
    if mode == 'compose':
        host = subprocess.check_output(['docker', 'network', 'inspect', 'bridge', '--format',
                                        '{{(index .IPAM.Config 0).Gateway}}'], text=True).strip()
    with tempfile.TemporaryDirectory(dir=Path.home()) as temp, https_fixture(payload=monit, name=host, bind=host) as (url, ca, seen):
        name = 'guided-' + uuid4().hex[:12]
        directory = Path(temp) / name
        doc = dict(plan(url), name=name)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            doc['listen_port'] = sock.getsockname()[1]
        root = Root(os.open(temp, DIRECTORY_FLAGS))
        cert, key, _ = material()
        unit = f'atraxis-{name}.service'
        try:
            engine = Engine(Authority((url,), (host + '/32',)), root, deployment_modes=(mode,))
            result = engine.prepare(serialize(doc), login=LOGIN, source_credentials={'monit': LOGIN},
                                    certificate=cert, private_key=key, source_cas={'monit': ca}, deployment=mode)
            assert result['status'] == 'ready' and len(seen) >= 2
            assert not (directory / 'login.json').exists()
            print(f'{mode}: fresh deployment, anonymous refusal and authenticated HTTPS readiness passed.')
        finally:
            root.close()
            if mode == 'systemd' and (directory / unit).exists():
                subprocess.run(['systemctl', '--user', 'disable', '--now', unit], check=False)
            if mode == 'compose' and (directory / 'compose.yaml').exists():
                subprocess.run(['docker', 'compose', '-f', str(directory / 'compose.yaml'), 'down'], check=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['systemd', 'compose'])
    main(parser.parse_args().mode)
