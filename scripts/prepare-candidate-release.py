"""Stage only the exact, previously qualified Atraxis prerelease archives."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import tomllib


def prepare(root: Path) -> None:
    metadata = tomllib.loads((root / 'pyproject.toml').read_text())['project']
    version = metadata['version']
    if metadata['name'] != 'atraxis' or not re.fullmatch(r'1\.0\.0rc[1-9][0-9]*', version):
        raise ValueError('prerelease_version_required')
    evidence = json.loads((root / 'candidate-ci.json').read_text())
    download = root / 'candidate-download'
    manifest = json.loads((download / 'package-evidence/manifest.json').read_text())
    for field, expected in [('source_commit', evidence['tested_commit']),
                            ('source_tree', evidence['source_tree']),
                            ('distribution', 'atraxis'), ('version', version)]:
        if manifest.get(field) != expected:
            raise ValueError('package_identity_mismatch')
    expected_names = {f'dist/atraxis-{version}-py3-none-any.whl',
                      f'dist/atraxis-{version}.tar.gz'}
    if set(manifest['files']) != expected_names:
        raise ValueError('unexpected_archives')
    actual = {f'dist/{p.name}' for p in (download / 'dist').iterdir()}
    if actual != expected_names:
        raise ValueError('unexpected_archive_files')
    for name, entry in manifest['files'].items():
        file = download / name
        if file.is_symlink() or not file.is_file() or not 0 < file.stat().st_size <= 10 * 1024 * 1024:
            raise ValueError('invalid_archive')
        if file.stat().st_size != entry['size'] or hashlib.sha256(file.read_bytes()).hexdigest() != entry['sha256']:
            raise ValueError('archive_digest_mismatch')
    assets = root / 'candidate-assets'
    assets.mkdir(exist_ok=False)
    for name in sorted(expected_names):
        shutil.copyfile(download / name, assets / Path(name).name)
    evidence.update(version=version, distribution='atraxis', package_manifest=manifest,
                    operational_gates_closed=False, registry_publication=False)
    (assets / 'candidate-provenance.json').write_text(json.dumps(evidence, indent=2) + '\n')
    lines = [f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n'
             for p in sorted(assets.iterdir())]
    (assets / 'SHA256SUMS').write_text(''.join(lines))
    print('Verified prerelease archives and source identity:', version)


if __name__ == '__main__':
    prepare(Path.cwd())
