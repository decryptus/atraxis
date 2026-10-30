"""Complete certificate identities, domains, fingerprints and native freshness."""

from copy import deepcopy
from dataclasses import replace
import unittest
from atraxis.client import validate_inventory, render_inventory
from atraxis.inventory_search import SearchQuery, search_inventory
from atraxis.search_client import validate_result
from atraxis.resource_scopes import filter_source
from atraxis.rule_observations import (
    ObservationBinding,
    ObservationError,
    ObservedInventory,
)
from atraxis.service import ReadService
from atraxis.web import render_inventory as render_web
from atraxis.web_inventory_search import render_search
from atraxis.models import Freshness
from observation_support import native, vector, spec, parse, direct, NOW
from test_collection import result, source
from test_observations import iso

ID = "12345678-1234-4234-8234-123456789abc"


def inventory(raw=None):
    service = ReadService(
        [source("source-a", kind="prometheus_certlord", metric=spec("certlord"))],
        clock=lambda: 100.0,
    )
    service.set_running(True)
    service.record("source-a", result("source-a", vector("certlord", raw)))
    return validate_inventory(service.status())


class CertlordCompleteTests(unittest.TestCase):
    def test_full_fields_in_web_cli_domain_search_and_scoped_result(self):
        data = inventory()
        fields = data["sources"][0]["last_valid_snapshot"]["resources"][0]["fields"]
        self.assertEqual(fields["fingerprint_sha256"], "a" * 64)
        self.assertEqual(fields["version"], 2**54 + 1)
        self.assertIn("example.test", render_web(data))
        self.assertIn("a" * 64, render_inventory(data, details=True))
        self.assertNotIn("unavailable in this metric feed", render_web(data))
        data["sources"] = [filter_source(data["sources"][0], [("certificate", ID)])]
        query = SearchQuery(q="example.test")
        matches = validate_result(search_inventory(data, query), query, None)
        self.assertEqual(matches["total"], 1)
        self.assertIn("example.test", render_search(matches))
        data["sources"] = [
            filter_source(
                data["sources"][0],
                [("certificate", "22345678-1234-4234-8234-123456789abc")],
            )
        ]
        self.assertEqual(search_inventory(data, query)["total"], 0)

    def test_per_certificate_oldest_observation_controls_freshness(self):
        raw = native("certlord")
        raw["started_at"] = iso(-120)
        raw["certificates"][0]["observed_at"] = iso(-61)
        expected = direct("certlord", raw)
        self.assertEqual(expected.freshness, Freshness.STALE)
        self.assertEqual(
            parse("certlord", raw), replace(expected, source_kind="prometheus_certlord")
        )
        raw["certificates"].append(
            dict(
                raw["certificates"][0],
                certificate_id="22345678-1234-4234-8234-123456789abc",
                observed_at=iso(-2),
            )
        )
        self.assertEqual(parse("certlord", raw).freshness, Freshness.STALE)

    def test_renewal_keeps_uuid_and_updates_version_domains_and_fingerprint(self):
        raw = native("certlord")
        first = parse("certlord", raw).resources[0]
        raw["certificates"][0].update(
            version=2, domains=["new.test"], fingerprint_sha256="c" * 64
        )
        second = parse("certlord", raw).resources[0]
        self.assertEqual(first.key, second.key)
        self.assertNotEqual(first.fields, second.fields)
        self.assertEqual(second, direct("certlord", raw).resources[0])

    def test_failed_missing_and_null_verification_exactly_match_native(self):
        for status in ("verified", "failed", None):
            raw = native("certlord")
            item = raw["certificates"][0]
            if status is None:
                item["verification"] = None
            else:
                item["verification"].update(
                    status=status,
                    error_code="fingerprint_mismatch" if status == "failed" else None,
                    observed_fingerprint_sha256="c" * 64,
                )
            self.assertEqual(
                parse("certlord", raw).resources, direct("certlord", raw).resources
            )
        for state in ("missing", "invalid"):
            raw = native("certlord")
            raw["certificates"][0].update(
                material_state=state,
                not_before=None,
                not_after=None,
                not_after_timestamp_seconds=None,
                fingerprint_sha256=None,
            )
            self.assertEqual(
                parse("certlord", raw).resources, direct("certlord", raw).resources
            )

    def test_expiry_future_clock_empty_and_monotonic_freshness_match_native(self):
        raw = native("certlord")
        raw["certificates"][0].update(
            not_after=iso(-1), not_after_timestamp_seconds=NOW - 1
        )
        self.assertTrue(parse("certlord", raw).authoritative)
        raw.update(completed_at=iso(3), completed_timestamp_seconds=NOW + 3)
        self.assertEqual(parse("certlord", raw).freshness, Freshness.UNKNOWN)
        self.assertEqual(parse("certlord").freshness_at(156), Freshness.STALE)

    def test_certificate_read_does_not_grant_rule_execution_authority(self):
        binding = ObservationBinding("org", ("certificate", ID), "source-a", ID)
        with self.assertRaises(ObservationError):
            ObservedInventory(inventory()).facts(binding, ["health_status"])

    def test_source_isolation_history_and_recovery(self):
        service = ReadService(
            [
                source(k, kind="prometheus_certlord", metric=spec("certlord"))
                for k in ("east", "west")
            ],
            clock=lambda: 100.0,
        )
        service.set_running(True)
        for k in ("east", "west"):
            service.record(k, result(k, vector("certlord")))
        service.record("east", result("east", outcome="timeout"))
        east, west = validate_inventory(service.status())["sources"]
        self.assertFalse(east["current"])
        self.assertTrue(west["current"])
        self.assertIsNotNone(east["last_valid_snapshot"])
        service.record("east", result("east", vector("certlord")))
        self.assertTrue(service.status()["ready"])
