"""Stable publication from a qualified candidate (qualification schema 2)."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

_SPEC = importlib.util.spec_from_file_location('stable_release_candidate',
    Path(__file__).resolve().parents[1] / 'scripts/prepare-stable-release.py')
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

VERSION = '1.0.12'
CANDIDATE = '1.0.12rc2'
RUNTIME = {'atraxis/__init__.py': b'# fixture\n', 'atraxis/cli.py': b'def main():\n    pass\n'}


def build_wheel(path, version, *, files=RUNTIME, requires=b'Requires-Dist: example==1',
                description=b'README', extra=None):
    info = f'atraxis-{version}.dist-info/'
    entries = {info + 'WHEEL': b'Wheel-Version: 1.0', info + 'RECORD': b'index ' + path.name.encode(),
               info + 'entry_points.txt': b'[console_scripts]\natraxis=atraxis.cli:main',
               info + 'METADATA': b'Metadata-Version: 2.4\nName: atraxis\nVersion: ' + version.encode()
               + b'\n' + requires + b'\n\n' + description, **files, **(extra or {})}
    with zipfile.ZipFile(path, 'w') as wheel:
        for name, data in entries.items():
            wheel.writestr(name, data)
    return hashlib.sha256(path.read_bytes()).hexdigest()


class StableFromCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'pyproject.toml').write_text(f'[project]\nname="atraxis"\nversion="{VERSION}"\n')
        (self.root / 'stable-ci.json').write_text(json.dumps(dict(
            tested_commit='a' * 40, release_commit='b' * 40, source_tree='c' * 40)))
        for name, data in RUNTIME.items():
            (self.root / name).parent.mkdir(exist_ok=True)
            (self.root / name).write_bytes(data)
        self.download = self.root / 'stable-download'
        (self.download / 'dist').mkdir(parents=True)
        (self.download / 'package-evidence').mkdir()
        self.final = self.download / f'dist/atraxis-{VERSION}-py3-none-any.whl'
        self.candidate_dir = self.root / 'candidate-download'
        self.candidate_dir.mkdir()
        self.candidate = self.candidate_dir / f'atraxis-{CANDIDATE}-py3-none-any.whl'
        wheel_sha256 = build_wheel(self.candidate, CANDIDATE, description=b'Candidate README')
        evidence = {gate: dict(status='passed', wheel_sha256=wheel_sha256,
                               sha256=hashlib.sha256(gate.encode()).hexdigest(),
                               run=f'https://github.com/decryptus/atraxis/actions/runs/{n}')
                    for n, gate in enumerate(('G1', 'G2', 'G3'), 1)}
        self.qualification = dict(
            schema=2, version=VERSION, gates={'G1': 'closed', 'G2': 'closed', 'G3': 'closed'},
            support_matrix='fixture',
            candidate=dict(version=CANDIDATE, tag='v' + CANDIDATE, source_commit='d' * 40,
                           wheel_sha256=wheel_sha256,
                           wheel_payload_sha256=_MODULE.release_payload_digest(self.candidate, CANDIDATE)),
            evidence=evidence,
            evidence_sha256={gate: record['sha256'] for gate, record in evidence.items()},
            runtime_files_sha256={name: hashlib.sha256(data).hexdigest() for name, data in RUNTIME.items()})
        self.build_final()

    def build_final(self, **changes):
        build_wheel(self.final, VERSION, **changes)
        sdist = self.download / f'dist/atraxis-{VERSION}.tar.gz'
        sdist.write_bytes(b'synthetic sdist')
        files = {}
        for path in (self.final, sdist):
            data = path.read_bytes()
            files['dist/' + path.name] = dict(size=len(data), sha256=hashlib.sha256(data).hexdigest())
        (self.download / 'package-evidence/manifest.json').write_text(json.dumps(dict(
            source_commit='a' * 40, source_tree='c' * 40, distribution='atraxis',
            version=VERSION, files=files)))

    def save(self):
        path = self.root / f'docs/releases/{VERSION}-qualification.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.qualification))

    def refused(self, code):
        self.save()
        with self.assertRaisesRegex(ValueError, f'^{code}$'):
            _MODULE.prepare(self.root)
        self.assertFalse((self.root / 'stable-assets').exists())

    def test_final_build_equivalent_to_the_qualified_candidate_is_staged(self):
        self.save()
        _MODULE.prepare(self.root)
        provenance = json.loads((self.root / 'stable-assets/release-provenance.json').read_text())
        self.assertEqual(provenance['operational_qualification']['candidate']['version'], CANDIDATE)
        self.assertTrue(provenance['operational_gates_closed'])

    def test_unpublished_candidate_of_the_same_version_is_accepted(self):
        wheel = self.candidate_dir / f'atraxis-{VERSION}-py3-none-any.whl'
        digest = build_wheel(wheel, VERSION)
        candidate = self.qualification['candidate']
        candidate.update(version=VERSION, tag=None, package_artifact=15092141991, wheel_sha256=digest,
                         wheel_payload_sha256=_MODULE.release_payload_digest(wheel, VERSION))
        for record in self.qualification['evidence'].values():
            record['wheel_sha256'] = digest
        self.save()
        _MODULE.prepare(self.root)

    def test_any_payload_difference_requires_a_new_candidate(self):
        changes = [dict(files={**RUNTIME, 'atraxis/cli.py': b'def main():\n    return 1\n'}),
                   dict(extra={'atraxis/extra.py': b''}),
                   dict(extra={'extra.pth': b'import other'}),
                   dict(requires=b'Requires-Dist: example==2')]
        for change in changes:
            with self.subTest(change=sorted(change)):
                self.build_final(**change)
                self.refused('final_payload_differs_from_candidate')

    def test_version_text_and_description_may_differ(self):
        self.build_final(description=b'Final README with a different description')
        self.save()
        _MODULE.prepare(self.root)

    def test_candidate_wheel_must_be_the_qualified_bytes(self):
        build_wheel(self.candidate, CANDIDATE, description=b'Rebuilt README')
        self.refused('candidate_wheel_mismatch')
        self.candidate.unlink()
        self.refused('candidate_wheel_mismatch')

    def test_recorded_payload_digest_is_recomputed(self):
        self.qualification['candidate']['wheel_payload_sha256'] = 'e' * 64
        self.refused('candidate_payload_mismatch')

    def test_every_gate_needs_passed_evidence_for_the_candidate_wheel(self):
        evidence = self.qualification['evidence']
        for gate, field, value in (('G1', 'status', 'failed'), ('G2', 'wheel_sha256', 'f' * 64),
                                   ('G3', 'sha256', 'short'), ('G3', 'run', '')):
            with self.subTest(gate=gate, field=field):
                original = evidence[gate][field]
                evidence[gate][field] = value
                self.refused('candidate_evidence_required')
                evidence[gate][field] = original
        removed = evidence.pop('G2')
        self.refused('candidate_evidence_required')
        evidence['G2'] = removed
        self.qualification['evidence_sha256']['G1'] = 'f' * 64
        self.refused('candidate_evidence_required')

    def test_candidate_identity_must_match_the_release(self):
        candidate = self.qualification['candidate']
        for field, value in (('version', '1.0.13rc1'), ('version', '1.0.12.post1'),
                             ('tag', 'v1.0.12rc1'), ('source_commit', 'short'),
                             ('wheel_payload_sha256', None)):
            with self.subTest(field=field, value=value):
                original = candidate[field]
                candidate[field] = value
                self.refused('candidate_identity_required')
                candidate[field] = original

    def test_later_releases_cannot_use_the_legacy_schema(self):
        self.qualification.update(schema=1)
        self.refused('candidate_schema_required')

    def test_open_gate_is_refused(self):
        self.qualification['gates']['G2'] = 'open'
        self.refused('reviewed_operational_gates_required')

    def test_changed_runtime_is_refused(self):
        (self.root / 'atraxis/cli.py').write_bytes(b'changed\n')
        self.refused('operational_runtime_identity_mismatch')


if __name__ == '__main__':
    unittest.main()
