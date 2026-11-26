"""Verify published candidate archives and record gate evidence bound to them.

Evidence never states more than the producing run observed: a missing,
incomplete or failed run is recorded as failed, never as passed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys

CANDIDATE_TAG = re.compile(r'v((0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)rc[1-9][0-9]*)')
COMMIT = re.compile(r'[0-9a-f]{40}')
PROVENANCE = 'candidate-provenance.json'
CHECKSUMS = 'SHA256SUMS'
ASSET_LIMIT = 10 * 1024 * 1024
EVIDENCE_LIMIT = 64 * 1024 * 1024
ENDURANCE_SECONDS = 7200
ENDURANCE_MAX_ROUNDS = 240
EVIDENCE_SCHEMA = 1
G3_EVIDENCE = 'g3-evidence.json'


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verify_candidate(assets, tag, commit):
    """Return the candidate identity after checking every published archive."""
    match = CANDIDATE_TAG.fullmatch(tag)
    if not match or not COMMIT.fullmatch(commit):
        raise ValueError('invalid_candidate_reference')
    version = match.group(1)
    wheel = f'atraxis-{version}-py3-none-any.whl'
    sdist = f'atraxis-{version}.tar.gz'
    expected = {wheel, sdist, PROVENANCE, CHECKSUMS}
    if {p.name for p in assets.iterdir()} != expected:
        raise ValueError('unexpected_candidate_assets')
    for name in expected:
        path = assets / name
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= ASSET_LIMIT:
            raise ValueError('invalid_candidate_asset')
    listed = {}
    for line in (assets / CHECKSUMS).read_text().splitlines():
        digest, separator, name = line.partition('  ')
        if not separator or not re.fullmatch('[0-9a-f]{64}', digest) or name in listed:
            raise ValueError('invalid_candidate_checksums')
        listed[name] = digest
    if set(listed) != expected - {CHECKSUMS}:
        raise ValueError('invalid_candidate_checksums')
    for name, digest in listed.items():
        if sha256(assets / name) != digest:
            raise ValueError('candidate_checksum_mismatch')
    provenance = json.loads((assets / PROVENANCE).read_text())
    wheel_sha256 = listed[wheel]
    if (provenance.get('version') != version or provenance.get('distribution') != 'atraxis'
            or provenance.get('prerelease') is not True
            or provenance.get('operational_gates_closed') is not False
            or provenance.get('release_commit') != commit
            or provenance.get('wheel_sha256') != wheel_sha256):
        raise ValueError('candidate_provenance_mismatch')
    return dict(tag=tag, version=version, release_commit=commit,
                tested_commit=provenance.get('tested_commit'), wheel=wheel,
                wheel_sha256=wheel_sha256, sdist_sha256=listed[sdist])


def actions_run():
    keys = ('GITHUB_REPOSITORY', 'GITHUB_WORKFLOW', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT')
    values = {key: os.environ.get(key, '') for key in keys}
    if not all(values.values()):
        raise ValueError('actions_run_identity_required')
    return dict(repository=values['GITHUB_REPOSITORY'], workflow=values['GITHUB_WORKFLOW'],
                id=int(values['GITHUB_RUN_ID']), attempt=int(values['GITHUB_RUN_ATTEMPT']))


def endurance_report(artifacts):
    """Return the completed endurance report, or None when the run did not complete."""
    path = artifacts / 'endurance.jsonl'
    if path.is_symlink() or not path.is_file() or path.stat().st_size > EVIDENCE_LIMIT:
        return None
    records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    complete = [r for r in records if r.get('phase') == 'complete']
    if len(complete) != 1 or records[-1] is not complete[0]:
        return None
    return complete[0].get('report')


def endurance_evidence(assets, tag, artifacts, commit):
    candidate = verify_candidate(assets, tag, commit)
    report = endurance_report(artifacts)
    passed = bool(
        isinstance(report, dict)
        and report.get('minimum_seconds') == ENDURANCE_SECONDS
        and isinstance(report.get('load_seconds'), (int, float))
        and report['load_seconds'] >= ENDURANCE_SECONDS
        and isinstance(report.get('completed_rounds'), int)
        and 0 < report['completed_rounds'] <= ENDURANCE_MAX_ROUNDS
        and report.get('recovery_verified') is True)
    evidence = dict(schema=EVIDENCE_SCHEMA, gate='G3', candidate=candidate, run=actions_run(),
                    status='passed' if passed else 'failed')
    if isinstance(report, dict):
        evidence.update({key: report.get(key) for key in (
            'load_seconds', 'minimum_seconds', 'completed_rounds',
            'accepted_test_runs', 'recovery_verified')})
    installed = artifacts / 'installed-candidates.json'
    if installed.is_file() and not installed.is_symlink():
        evidence['installed_candidates_sha256'] = sha256(installed)
    elif passed:
        raise ValueError('installed_candidate_record_required')
    (artifacts / G3_EVIDENCE).write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n')
    return evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest='command', required=True)
    verify = commands.add_parser('verify', help='verify the published candidate archives')
    verify.add_argument('assets', type=Path)
    verify.add_argument('tag')
    verify.add_argument('commit')
    endurance = commands.add_parser('endurance', help='record G3 evidence for the candidate')
    endurance.add_argument('assets', type=Path)
    endurance.add_argument('tag')
    endurance.add_argument('artifacts', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'verify':
            candidate = verify_candidate(args.assets, args.tag, args.commit)
            print('Verified candidate', candidate['tag'], 'wheel', candidate['wheel_sha256'])
        else:
            evidence = endurance_evidence(args.assets, args.tag, args.artifacts,
                                          os.environ.get('GITHUB_SHA', ''))
            print('G3 evidence', evidence['status'], 'for', evidence['candidate']['wheel_sha256'])
    except (OSError, ValueError) as error:
        code = str(error) if isinstance(error, ValueError) and str(error).isidentifier() else type(error).__name__
        print('Candidate evidence refused:', code, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
