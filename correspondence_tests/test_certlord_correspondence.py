"""Compare native JSON with real CertLord -> Covenant -> Prometheus projection."""

from copy import deepcopy
import time
import unittest
from certlord_support import CertlordChain, certificate_payload, iso, ID
from support import MAX_AGE


class CertlordCorrespondenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chain = CertlordChain()
        cls.addClassCleanup(cls.chain.close)
        cls.chain.start()

    def setUp(self):
        self.chain.available = True
        self.chain.source_available = True
        self.chain.bad_exposition = False
        self.raw = certificate_payload()
        self.changed = self.chain.set(self.raw)

    def fields(self, inventory):
        self.assertTrue(inventory["ready"])
        resource = inventory["sources"][0]["last_valid_snapshot"]["resources"][0]
        self.assertEqual(resource["native_id"], ID)
        return resource["fields"]

    def test_native_fields_and_verified_tls_correspond_on_two_prometheus_instances(
        self,
    ):
        direct = self.chain.direct(self.raw)["sources"][0]["last_valid_snapshot"][
            "resources"
        ][0]["fields"]
        for site in ("east", "west"):
            fields = self.fields(self.chain.bundle(self.changed, site))
            self.assertEqual(fields, direct)
        self.assertTrue(self.chain.source_calls)
        self.assertEqual(
            {(method, path) for method, path, _ in self.chain.source_calls},
            {("GET", "/api/certificates/observations")},
        )
        self.assertTrue(
            all(
                auth and auth.startswith("Basic ")
                for _, _, auth in self.chain.source_calls
            )
        )

    def test_label_transitions_and_tls_failure_do_not_retain_verified_state(self):
        self.chain.bundle(self.changed)
        self.raw["certificates"][0].update(status="renew", pending_action="create")
        self.raw["certificates"][0]["verification"].update(
            status="failed", error_code="fingerprint_mismatch"
        )
        changed = self.chain.set(self.raw)
        fields = self.fields(self.chain.bundle(changed))
        self.assertEqual(fields["status"], "renew")
        self.assertEqual(fields["verification"]["status"], "failed")
        self.assertEqual(fields["verification"]["error_code"], "fingerprint_mismatch")

    def test_disappearance_and_empty_inventory_are_authoritative(self):
        self.chain.bundle(self.changed)
        self.raw["certificates"] = []
        changed = self.chain.set(self.raw)
        inventory = self.chain.bundle(changed)
        self.assertTrue(inventory["ready"])
        self.assertEqual(
            inventory["sources"][0]["last_valid_snapshot"]["resources"], []
        )

    def test_missing_material_and_not_checked_tls_preserve_unknowns(self):
        record = self.raw["certificates"][0]
        record.update(
            material_state="missing",
            not_before=None,
            not_after=None,
            not_after_timestamp_seconds=None,
            fingerprint_sha256=None,
            verification=None,
            pending_action="none",
            issuance_retry_count=None,
        )
        changed = self.chain.set(self.raw)
        fields = self.fields(self.chain.bundle(changed))
        self.assertIsNone(fields["not_after"])
        self.assertIsNone(fields["issuance_retry_count"])
        self.assertIsNone(fields["verification"])

    def test_zero_retry_and_expired_certificate_keep_native_meaning(self):
        record = self.raw["certificates"][0]
        record["issuance_retry_count"] = 0
        record.update(
            not_after_timestamp_seconds=time.time() - 100,
            not_after=iso(time.time() - 100),
        )
        changed = self.chain.set(self.raw)
        fields = self.fields(self.chain.bundle(changed))
        self.assertEqual(fields["issuance_retry_count"], 0)
        self.assertLess(fields["not_after_timestamp_seconds"], time.time())

    def test_old_future_and_unavailable_source_fail_and_recover(self):
        for completed in (time.time() - MAX_AGE - 30, time.time() + 120):
            raw = deepcopy(self.raw)
            raw["completed_timestamp_seconds"] = completed
            self.assertFalse(self.chain.bundle(self.chain.set(raw))["ready"])
        self.chain.set(self.raw)
        self.chain.source_available = False
        self.assertFalse(self.chain.bundle(time.time())["ready"])
        self.chain.source_available = True
        self.chain.available = False
        self.assertFalse(self.chain.bundle(time.time())["ready"])
        self.chain.available = True
        self.assertTrue(self.chain.bundle(time.time())["ready"])

    def test_invalid_exposition_duplicate_identity_and_incomplete_material_fail(self):
        self.chain.bad_exposition = True
        self.assertFalse(self.chain.bundle(time.time())["ready"])
        self.chain.bad_exposition = False
        self.raw["certificates"] *= 2
        self.assertFalse(self.chain.bundle(self.chain.set(self.raw))["ready"])
        self.raw = certificate_payload()
        self.raw["certificates"][0]["not_after_timestamp_seconds"] = None
        self.assertFalse(self.chain.bundle(self.chain.set(self.raw))["ready"])

    def test_fresh_collection_with_old_certificate_is_stale_like_direct(self):
        raw = deepcopy(self.raw)
        raw["started_at"] = iso(time.time() - MAX_AGE - 40)
        raw["certificates"][0]["observed_at"] = iso(time.time() - MAX_AGE - 30)
        self.assertFalse(self.chain.direct(raw)["ready"])
        self.assertFalse(self.chain.bundle(self.chain.set(raw))["ready"])

    def test_domain_fingerprint_version_changes_preserve_uuid_and_full_metadata(self):
        self.chain.bundle(self.changed)
        self.raw["certificates"][0].update(
            domains=["new.test", "*.new.test"],
            version=2**54 + 1,
            fingerprint_sha256="c" * 64,
        )
        expected = self.chain.direct(self.raw)["sources"][0]["last_valid_snapshot"]
        actual = self.chain.bundle(self.chain.set(self.raw))["sources"][0][
            "last_valid_snapshot"
        ]
        self.assertEqual(actual["source_metadata"], expected["source_metadata"])
        self.assertEqual(actual["resources"][0]["native_id"], ID)
        self.assertEqual(
            actual["resources"][0]["fields"], expected["resources"][0]["fields"]
        )

    def test_real_native_certificate_projection_preserves_material_fingerprint(self):
        from datetime import datetime, timezone, timedelta
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from certlord.services.observations import certificate_observation

        started = time.time()
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, "native.example.test")]
        )
        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(1)
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=30))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName("native.example.test")]),
                critical=False,
            )
            .sign(key, hashes.SHA256())
        )
        record = certificate_observation(
            ID,
            "native.example.test",
            dict(
                origin="external",
                status="deployed",
                cert=cert.public_bytes(serialization.Encoding.PEM).decode(),
            ),
            7,
            {},
        )
        completed = time.time()
        raw = dict(
            schema_version=1,
            started_at=iso(started),
            completed_at=iso(completed),
            completed_timestamp_seconds=completed,
            certificates=[record],
        )
        expected = self.chain.direct(raw)["sources"][0]["last_valid_snapshot"][
            "resources"
        ][0]["fields"]
        actual = self.fields(self.chain.bundle(self.chain.set(raw)))
        self.assertEqual(actual, expected)
        self.assertEqual(
            actual["fingerprint_sha256"], cert.fingerprint(hashes.SHA256()).hex()
        )

    def test_real_sample_clock_matches_raw_selector_not_query_evaluation(self):
        import requests
        from atraxis.transport import HTTPTransport

        self.chain.bundle(self.changed)
        source = self.chain.bundle_source()
        response = HTTPTransport().fetch(source).payload
        clock = next(
            r
            for r in response["data"]["result"]
            if r["metric"].get("covenant_metric") == "up"
            and r["metric"].get("covenant_sample_time") == "1"
        )
        reference = requests.get(
            source.origin + "/api/v1/query",
            params=dict(query="timestamp(up)", time=clock["value"][0]),
            auth=("reader", "synthetic-only"),
            timeout=2,
        ).json()
        self.assertEqual(clock["value"][1], reference["data"]["result"][0]["value"][1])
        self.assertLessEqual(float(clock["value"][1]), clock["value"][0])

    def test_wire_codec_is_identical_to_exporter_schema(self):
        from pathlib import Path
        import atraxis.observation_codec as consumer
        import covenant.services.observation_codec as exporter

        self.assertEqual(
            Path(consumer.__file__).read_bytes(), Path(exporter.__file__).read_bytes()
        )
