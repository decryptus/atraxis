"""Compare every installed Python source module with the pinned public inputs."""
import hashlib
import json
from pathlib import Path
import subprocess


def files(directory):
    directory = Path(directory)
    return {str(p.relative_to(directory.parent)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(directory.rglob('*.py'))}


client = files('.acceptance/engine/client/atropos_client')
targets = {
    'atraxis-chain:ci': (dict(atraxis='0.0.1.dev0', sonicprobe='0.3.62'),
                         files('atraxis') | client),
    'atropos-chain:ci': ({'atraxis-atropos': '0.1.0a21', 'atropos-client': '0.1.0a10',
                          'sonicprobe': '0.3.62'}, files('.acceptance/engine/atropos') | client),
    'auton-chain:ci': ({}, files('.auton-source/auton')),
}
verifier = '''import hashlib, importlib.util, json
from importlib.metadata import version
from pathlib import Path
versions, files = json.loads(%r)
for name, expected in versions.items():
    assert version(name) == expected, name
for name, expected in files.items():
    package, relative = name.split('/', 1)
    spec = importlib.util.find_spec(package)
    assert spec is not None and spec.origin, package
    path = Path(spec.origin).parent / relative
    assert str(path.resolve()).startswith('/usr/local/lib/python3.12/site-packages/'), name
    assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, name
print('Installed candidate bytes verified:', len(files))
'''
images = {}
for image, (versions, expected) in targets.items():
    if not expected:
        raise ValueError('empty_installed_candidate_manifest')
    subprocess.run(['docker', 'run', '--rm', '-i', '--network', 'none', '--read-only',
                    '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                    '--memory', '512m', '--cpus', '2', '--pids-limit', '128',
                    '--workdir', '/tmp', '--entrypoint', 'python', image, '-'],
                   input=verifier % json.dumps([versions, expected]), text=True,
                   check=True, timeout=60)
    images[image] = dict(files=expected, versions=versions, id=subprocess.check_output(
        ['docker', 'image', 'inspect', '--format', '{{.Id}}', image],
        text=True, timeout=30).strip())
Path('acceptance-artifacts/installed-candidates.json').write_text(json.dumps(images, indent=2) + '\n')
