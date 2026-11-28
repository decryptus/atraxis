"""Candidate archive verification and G3 evidence never overstate a run."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    'candidate_evidence', ROOT / 'scripts/qualification/candidate_evidence.py')
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

TAG = 'v1.0.12rc1'
COMMIT = 'b' * 40
RUN = {'GITHUB_REPOSITORY': 'decryptus/atraxis', 'GITHUB_WORKFLOW': 'two-hour container endurance',
       'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1'}


class CandidateFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.assets = self.root / 'assets'
        self.assets.mkdir()
        self.wheel = self.assets / 'atraxis-1.0.12rc1-py3-none-any.whl'
        self.wheel.write_bytes(b'synthetic wheel')
        (self.assets / 'atraxis-1.0.12rc1.tar.gz').write_bytes(b'synthetic sdist')
        self.provenance = dict(version='1.0.12rc1', distribution='atraxis', prerelease=True,
                               operational_gates_closed=False, release_commit=COMMIT,
                               tested_commit='a' * 40,
                               wheel_sha256=hashlib.sha256(b'synthetic wheel').hexdigest())
        self.write()

    def write(self):
        (self.assets / 'candidate-provenance.json').write_text(json.dumps(self.provenance))
        lines = [f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n'
                 for p in sorted(self.assets.iterdir()) if p.name != 'SHA256SUMS']
        (self.assets / 'SHA256SUMS').write_text(''.join(lines))


class CandidateVerificationTests(CandidateFixture):
    def test_exact_candidate_identity_is_returned(self):
        candidate = _MODULE.verify_candidate(self.assets, TAG, COMMIT)
        self.assertEqual(candidate['version'], '1.0.12rc1')
        self.assertEqual(candidate['wheel_sha256'], self.provenance['wheel_sha256'])

    def test_stable_or_malformed_references_are_refused(self):
        for tag, commit in (('v1.0.12', COMMIT), ('1.0.12rc1', COMMIT), (TAG, 'short')):
            with self.subTest(tag=tag, commit=commit):
                with self.assertRaisesRegex(ValueError, '^invalid_candidate_reference$'):
                    _MODULE.verify_candidate(self.assets, tag, commit)

    def test_substituted_archive_is_refused(self):
        self.wheel.write_bytes(b'substituted wheel')
        with self.assertRaisesRegex(ValueError, '^candidate_checksum_mismatch$'):
            _MODULE.verify_candidate(self.assets, TAG, COMMIT)

    def test_extra_or_linked_asset_is_refused(self):
        (self.assets / 'extra.txt').write_text('extra')
        with self.assertRaisesRegex(ValueError, '^unexpected_candidate_assets$'):
            _MODULE.verify_candidate(self.assets, TAG, COMMIT)
        (self.assets / 'extra.txt').unlink()
        outside = self.root / 'outside'
        outside.write_bytes(self.wheel.read_bytes())
        self.wheel.unlink()
        self.wheel.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, '^invalid_candidate_asset$'):
            _MODULE.verify_candidate(self.assets, TAG, COMMIT)

    def test_provenance_must_match_the_tag_commit_and_wheel(self):
        for field, value in (('version', '1.0.12rc2'), ('release_commit', 'c' * 40),
                             ('wheel_sha256', 'f' * 64), ('prerelease', False),
                             ('operational_gates_closed', True)):
            with self.subTest(field=field):
                original = self.provenance[field]
                self.provenance[field] = value
                self.write()
                with self.assertRaisesRegex(ValueError, '^candidate_provenance_mismatch$'):
                    _MODULE.verify_candidate(self.assets, TAG, COMMIT)
                self.provenance[field] = original
                self.write()


class EnduranceEvidenceTests(CandidateFixture):
    def setUp(self):
        super().setUp()
        self.artifacts = self.root / 'artifacts'
        self.artifacts.mkdir()
        (self.artifacts / 'installed-candidates.json').write_text('{"atraxis-chain:ci": {}}')
        self.report = dict(load_seconds=7206.4, minimum_seconds=7200, completed_rounds=240,
                           accepted_test_runs=6000, recovery_verified=True)

    def record(self, *records):
        with (self.artifacts / 'endurance.jsonl').open('w') as stream:
            for record in records:
                stream.write(json.dumps(record) + '\n')

    def evidence(self):
        with patch.dict(os.environ, RUN):
            return _MODULE.endurance_evidence(self.assets, TAG, self.artifacts, COMMIT)

    def test_completed_run_is_recorded_as_passed_for_the_candidate_wheel(self):
        self.record(dict(phase='cycle'), dict(phase='complete', report=self.report))
        evidence = self.evidence()
        self.assertEqual(evidence['status'], 'passed')
        self.assertEqual(evidence['gate'], 'G3')
        self.assertEqual(evidence['candidate']['wheel_sha256'], self.provenance['wheel_sha256'])
        self.assertEqual(evidence['run']['id'], 123)
        stored = json.loads((self.artifacts / 'g3-evidence.json').read_text())
        self.assertEqual(stored, evidence)

    def test_incomplete_or_insufficient_runs_are_recorded_as_failed(self):
        cases = {
            'missing': None,
            'interrupted': [dict(phase='complete', report=self.report), dict(phase='cycle')],
            'short': [dict(phase='complete', report=dict(self.report, load_seconds=7199.0))],
            'unrecovered': [dict(phase='complete', report=dict(self.report, recovery_verified=False))],
            'other_profile': [dict(phase='complete', report=dict(self.report, minimum_seconds=600))],
            'no_rounds': [dict(phase='complete', report=dict(self.report, completed_rounds=0))],
        }
        for name, records in cases.items():
            with self.subTest(case=name):
                path = self.artifacts / 'endurance.jsonl'
                if path.exists():
                    path.unlink()
                if records is not None:
                    self.record(*records)
                self.assertEqual(self.evidence()['status'], 'failed')

    def test_passed_run_requires_the_installed_candidate_record(self):
        (self.artifacts / 'installed-candidates.json').unlink()
        self.record(dict(phase='complete', report=self.report))
        with self.assertRaisesRegex(ValueError, '^installed_candidate_record_required$'):
            self.evidence()

    def test_evidence_requires_the_producing_run_identity(self):
        self.record(dict(phase='complete', report=self.report))
        with patch.dict(os.environ, dict(RUN, GITHUB_RUN_ID='')):
            with self.assertRaisesRegex(ValueError, '^actions_run_identity_required$'):
                _MODULE.endurance_evidence(self.assets, TAG, self.artifacts, COMMIT)

    def test_unverified_candidate_produces_no_evidence(self):
        self.record(dict(phase='complete', report=self.report))
        self.wheel.write_bytes(b'substituted wheel')
        with self.assertRaises(ValueError):
            self.evidence()
        self.assertFalse((self.artifacts / 'g3-evidence.json').exists())


class CandidateImageTests(unittest.TestCase):
    def test_candidate_image_matches_the_repository_runtime_except_installation(self):
        def lines(path):
            return [line for line in path.read_text().splitlines()
                    if line and not line.startswith('#')]
        source = lines(ROOT / 'Dockerfile')
        candidate = lines(ROOT / 'scripts/chain/candidate.Dockerfile')
        install = ('COPY', 'RUN pip', '    && rm -rf /tmp/candidate')
        self.assertEqual(candidate[:2], ['ARG BASE', 'FROM ${BASE}'])
        self.assertTrue(source[0].startswith('FROM python:'))
        self.assertEqual([l for l in source[1:] if not l.startswith(install)],
                         [l for l in candidate[2:] if not l.startswith(install)])


if __name__ == '__main__':
    unittest.main()
