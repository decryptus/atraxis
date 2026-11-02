"""Exercise the distributed rules client and persistent accounts in the image."""
import json
from pathlib import Path
import subprocess
import tomllib
import uuid

ROOT = Path.cwd()
IMAGE = 'atraxis:test'
VERSION = tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['version']
HARDENING = ['--network', 'none', '--read-only', '--cap-drop', 'ALL',
    '--security-opt', 'no-new-privileges', '--pids-limit', '128', '--memory', '512m',
    '--cpus', '2', '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m,uid=10001,gid=10001,mode=0700']


def run(arguments, *, python=False, volume=None, capture=False):
    command = ['docker', 'run', '--rm', *HARDENING, '--mount',
        'type=bind,src=' + str(ROOT) + ',dst=/acceptance,readonly', '-w', '/tmp']
    if volume:
        command += ['--mount', 'type=volume,src=' + volume + ',dst=/var/lib/atraxis']
    if python:
        command += ['--entrypoint', 'python']
    return subprocess.run(command + [IMAGE, *arguments], check=True, timeout=180,
        capture_output=capture, text=True).stdout


run(['-c', "import os,importlib.metadata,importlib.util; assert os.getuid()==os.getgid()==10001; assert importlib.metadata.version('atraxis')==" + repr(VERSION) + "; assert importlib.metadata.version('atropos-client')=='1.0.0'; assert importlib.util.find_spec('atropos') is None; assert importlib.util.find_spec('zen') is None"], python=True)
run(['-m', 'pip', 'check'], python=True)
run(['/acceptance/scripts/check-test-collection.py', '--runner', 'unittest', '/acceptance/client_tests'], python=True)
run(['-m', 'unittest', 'discover', '-s', '/acceptance/client_tests', '-v'], python=True)
subprocess.run(['python', 'scripts/docker-acceptance.py', '--image', IMAGE], check=True, timeout=300)
volume = 'atraxis-image-check-' + uuid.uuid4().hex
subprocess.run(['docker', 'volume', 'create', volume], check=True, timeout=30)
try:
    run(['accounts', '--store', '/var/lib/atraxis/accounts.db', 'init', '--subject', 'fixture-admin', '--administrator'], volume=volume)
    run(['-c', "import sqlite3; c=sqlite3.connect('file:/var/lib/atraxis/accounts.db?mode=ro',uri=True); assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'; assert c.execute('SELECT COUNT(*) FROM sqlite_master WHERE type=\"table\"').fetchone()[0]>0; print('Account database retained across container recreation')"], python=True, volume=volume)
finally:
    subprocess.run(['docker', 'volume', 'rm', volume], check=True, timeout=30)
