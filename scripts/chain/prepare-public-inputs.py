"""Verify pinned public checkouts and apply the bounded Auton fixture patch."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parent
inputs = json.loads((root / 'public-inputs.json').read_text())


def git(path, *args):
    return subprocess.check_output(['git', '-C', str(path), *args],
                                   text=True, timeout=30).strip()


directories = {'atropos': '.acceptance/engine', 'auton': '.auton-source'}
if os.environ.get('ATRAXIS_COMPOSE_REGISTRY') == '1':
    lock = json.loads((root.parents[1] / 'deploy/compose/images.json').read_text())
    inputs['web_client'] = dict(inputs['atropos'])
    directories['web_client'] = '.acceptance/released-client'
    inputs['atropos']['commit'] = lock['images']['engine']['source_commit']

for role, directory in directories.items():
    if git(directory, 'rev-parse', 'HEAD') != inputs[role]['commit']:
        raise ValueError('acceptance_source_identity_mismatch: ' + role)
    if git(directory, 'status', '--porcelain'):
        raise ValueError('acceptance_source_not_clean: ' + role)

patch = root / 'auton-oidc-deadline.patch'
data = patch.read_bytes()
digest = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
if digest != inputs['auton']['patch_git_blob']:
    raise ValueError('acceptance_patch_identity_mismatch')
for args in [('apply', '--check', str(patch)), ('apply', str(patch)), ('diff', '--check')]:
    subprocess.run(['git', '-C', '.auton-source', *args], check=True, timeout=30)

evidence = Path('acceptance-artifacts')
evidence.mkdir(exist_ok=True)
provenance = dict(atraxis=git('.', 'rev-parse', 'HEAD'), inputs=inputs,
                  scope='Synthetic fixtures only; no deployed services',
                  load_seconds_required=14400, quiescence_seconds_required=600,
                  quiescence_observations_required=10,
                  limits=dict(memory_mib=512, cpus=2, pids=128, workers=4, capacity=24))
(evidence / 'candidate-provenance.json').write_text(json.dumps(provenance, indent=2) + '\n')
print('Public source identities and bounded Auton fixture verified')
