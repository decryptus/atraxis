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
            for key in (
                "origin",
                "renewal_owner",
                "status",
                "pending_action",
                "material_state",
                "not_after_timestamp_seconds",
                "issuance_retry_count",
            ):
                self.assertEqual(fields[key], direct[key])
            self.assertEqual(
                fields["tls_verification_status"], direct["verification"]["status"]
            )
            self.assertEqual(
                fields["tls_verified_at"], direct["verification"]["checked_at"]
            )
            for key in (
                "domains",
                "fingerprint_sha256",
                "observed_at",
                "verification",
                "version",
            ):
                self.assertIsNone(fields[key])
        self.assertTrue(self.chain.source_calls)
        self.assertEqual(
            {(method, path) for method, path, _ in self.chain.source_calls},
            {("GET", "/api/certificates/metrics")},
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
        self.assertEqual(fields["tls_verification_status"], "failed")
        self.assertIsNone(fields["tls_verified_at"])
        self.assertIsNone(fields["verification"])

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
        self.assertEqual(fields["tls_verification_status"], "not_checked")

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

    def test_collection_freshness_does_not_claim_individual_observation_freshness(self):
        # The native metric endpoint omits this per-certificate timestamp entirely.
        raw = deepcopy(self.raw)
        raw["started_at"] = iso(time.time() - MAX_AGE - 40)
        raw["certificates"][0]["observed_at"] = iso(time.time() - MAX_AGE - 30)
        self.assertFalse(self.chain.direct(raw)["ready"])
        fields = self.fields(self.chain.bundle(self.chain.set(raw)))
        self.assertIsNone(fields["observed_at"])
        self.assertEqual(fields["projection"], "certlord_metrics")
