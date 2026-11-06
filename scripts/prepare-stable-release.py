"""Stage exact qualified 1.0 archives after reviewed operational gate closure."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import tomllib
import zipfile


def wheel_payload_digest(path, version):
    """Compare installed payload, metadata headers and scripts across qualified builds."""
    info = f'atraxis-{version}.dist-info'
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        required = {info + '/' + name for name in ('METADATA', 'RECORD', 'WHEEL', 'entry_points.txt')}
        if (len(set(names)) != len(names) or not required.issubset(names)
                or sum(entry.file_size for entry in wheel.infolist()) > 32 * 1024 * 1024):
            raise ValueError('invalid_wheel_payload')
        members = {}
        for name in sorted(names):
            if name == info + '/RECORD':
                continue  # Generated index of member checksums, including the README metadata.
            data = wheel.read(name)
            if name == info + '/METADATA':
                data, separator, _description = data.partition(b'\n\n')
                if not separator:
                    raise ValueError('invalid_wheel_metadata')
            members[name] = hashlib.sha256(data).hexdigest()
    return hashlib.sha256(json.dumps(members, sort_keys=True).encode()).hexdigest()


def prepare(root: Path) -> None:
    metadata = tomllib.loads((root / 'pyproject.toml').read_text())['project']
    version = metadata['version']
    if metadata['name'] != 'atraxis' or not re.fullmatch(r'1\.0\.(0|[1-9][0-9]*)', version):
        raise ValueError('stable_version_required')
    qualification = json.loads((root / f'docs/releases/{version}-qualification.json').read_text())
    if (qualification.get('version') != version or qualification.get('schema') != 1
            or qualification.get('gates') != {'G1': 'closed', 'G2': 'closed', 'G3': 'closed'}
            or not qualification.get('support_matrix')
            or not qualification.get('evidence_sha256')
            or any(not re.fullmatch('[0-9a-f]{64}', value)
                   for value in qualification['evidence_sha256'].values())):
        raise ValueError('reviewed_operational_gates_required')
    if version == '1.0.3' or 'baseline_qualification_sha256' in qualification:
        baseline_version = '1.0.2' if version == '1.0.3' else '1.0.0'
        baseline_path = root / f'docs/releases/{baseline_version}-qualification.json'
        baseline_bytes = baseline_path.read_bytes()
        baseline = json.loads(baseline_bytes)
        if (hashlib.sha256(baseline_bytes).hexdigest() != qualification.get('baseline_qualification_sha256')
                or baseline['runtime_files_sha256'] != qualification['runtime_files_sha256']):
            raise ValueError('patch_baseline_identity_mismatch')
    paths = sorted((root / 'atraxis').rglob('*.py'))
    if any(p.is_symlink() for p in paths):
        raise ValueError('operational_runtime_identity_mismatch')
    runtime = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    if not runtime or runtime != qualification.get('runtime_files_sha256'):
        raise ValueError('operational_runtime_identity_mismatch')
    evidence = json.loads((root / 'stable-ci.json').read_text())
    download = root / 'stable-download'
    manifest = json.loads((download / 'package-evidence/manifest.json').read_text())
    for field, expected in [('source_commit', evidence['tested_commit']),
                            ('source_tree', evidence['source_tree']),
                            ('distribution', 'atraxis'), ('version', version)]:
        if manifest.get(field) != expected:
            raise ValueError('package_identity_mismatch')
    expected_names = {f'dist/atraxis-{version}-py3-none-any.whl', f'dist/atraxis-{version}.tar.gz'}
    if set(manifest['files']) != expected_names:
        raise ValueError('unexpected_archives')
    if {f'dist/{p.name}' for p in (download / 'dist').iterdir()} != expected_names:
        raise ValueError('unexpected_archive_files')
    for name, entry in manifest['files'].items():
        file = download / name
        if file.is_symlink() or not file.is_file() or not 0 < file.stat().st_size <= 10 * 1024 * 1024:
            raise ValueError('invalid_archive')
        if file.stat().st_size != entry['size'] or hashlib.sha256(file.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('archive_digest_mismatch')
    if version in ('1.0.2', '1.0.4', '1.0.5'):
        wheel = download / f'dist/atraxis-{version}-py3-none-any.whl'
        if wheel_payload_digest(wheel, version) != qualification.get('pilot_wheel_payload_sha256'):
            raise ValueError('pilot_wheel_payload_mismatch')
    assets = root / 'stable-assets'
    assets.mkdir(exist_ok=False)
    for name in sorted(expected_names):
        shutil.copyfile(download / name, assets / Path(name).name)
    evidence.update(version=version, distribution='atraxis', package_manifest=manifest,
                    operational_qualification=qualification, operational_gates_closed=True,
                    prerelease=False, registry_publication=False)
    (assets / 'release-provenance.json').write_text(json.dumps(evidence, indent=2) + '\n')
    lines = [f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n' for p in sorted(assets.iterdir())]
    (assets / 'SHA256SUMS').write_text(''.join(lines))
    print('Verified stable archives, source identity and reviewed gates:', version)


if __name__ == '__main__':
    prepare(Path.cwd())
