"""CertLord native metric projection: available facts, gaps and failure semantics."""

from copy import deepcopy
from dataclasses import replace
import unittest
from atraxis.client import validate_inventory, render_inventory
from atraxis.inventory_search import SearchQuery, search_inventory
from atraxis.models import Freshness
from atraxis.parsing import InvalidPayload
from atraxis.prometheus_certlord import (
    parse_prometheus_certlord,
    query_parameters,
    selection,
    PREFIX,
    NAMES,
    INFO,
    EXPIRY,
    RETRY,
    TLS,
    VERIFIED,
    COMPLETED,
    COUNT,
    UNKNOWN_FIELDS,
    validate_fields,
)
from atraxis.resource_scopes import filter_source, target_key
from atraxis.rule_observations import (
    ObservationBinding,
    ObservedInventory,
    ObservationError,
)
from atraxis.search_client import validate_result
from atraxis.service import ReadService
from atraxis.web import render_inventory as render_web
from atraxis.web_inventory_search import render_search
from test_collection import result, source
from test_observations import NOW

ID = "12345678-1234-4234-8234-123456789abc"
SPEC = dict(match={"job": "certificates", "instance": "exporter:9118"})


def response():
    values = {
        "up": 1,
        "source_up": 1,
        COMPLETED: NOW - 2,
        COUNT: 1,
        INFO: 1,
        EXPIRY: NOW + 86400,
        RETRY: 3,
        TLS: 1,
        VERIFIED: NOW - 5,
    }
    rows = []
    for name, key in NAMES.items():
        labels = dict(__name__=name, **SPEC["match"])
        if key in (INFO, EXPIRY, RETRY, TLS, VERIFIED):
            labels["certificate_id"] = ID
        if key == INFO:
            labels.update(
                origin="acme",
                renewal_owner="acme",
                status="deployed",
                pending_action="renew",
                material_state="readable",
            )
        if key == TLS:
            labels["status"] = "verified"
        rows.append(dict(metric=labels, values=[[NOW, str(values[key])]]))
    return dict(status="success", data=dict(resultType="matrix", result=rows))


def row(payload, key):
    return next(
        r for r in payload["data"]["result"] if NAMES[r["metric"]["__name__"]] == key
    )


class CertlordMetricTests(unittest.TestCase):
    def parse(self, payload=None, **changes):
        args = dict(
            metric=SPEC,
            source_id="via",
            received_at=NOW,
            monotonic=100.0,
            max_age=60,
            max_resources=10,
            max_clock_skew=2,
        )
        args.update(changes)
        return parse_prometheus_certlord(
            response() if payload is None else payload, **args
        )

    def inventory(self, payload=None):
        service = ReadService(
            [source("via", kind="prometheus_certlord", metric=SPEC)],
            clock=lambda: 100.0,
        )
        service.set_running(True)
        service.record("via", result("via", response() if payload is None else payload))
        return validate_inventory(service.status())

    def test_available_fields_uuid_types_and_unknowns(self):
        snapshot = self.parse()
        self.assertTrue(snapshot.authoritative)
        self.assertEqual(snapshot.age_at_receipt, 2)
        resource = snapshot.resources[0]
        fields = dict(resource.fields)
        self.assertEqual((resource.kind, resource.native_id), ("certificate", ID))
        self.assertEqual(fields["issuance_retry_count"], 3)
        self.assertIs(type(fields["issuance_retry_count"]), int)
        self.assertEqual(fields["not_after_timestamp_seconds"], NOW + 86400)
        self.assertEqual(fields["tls_verification_status"], "verified")
        self.assertTrue(all(fields[k] is None for k in UNKNOWN_FIELDS))
        self.assertNotIn("fingerprint_mismatch", repr(snapshot))

    def test_api_web_cli_search_and_scoped_uuid_grants(self):
        inventory = self.inventory()
        self.assertIn(ID, render_web(inventory))
        self.assertIn("Collection freshness only", render_web(inventory))
        self.assertIn("certlord_metrics", render_inventory(inventory, details=True))
        self.assertEqual(
            target_key(dict(kind="certificate", source="via", id=ID)),
            ("certificate", "via", ID),
        )
        inventory["context"] = dict(
            subject="alice",
            organization="org",
            organizations=[dict(id="org", name="Organization")],
        )
        inventory["sources"] = [
            filter_source(inventory["sources"][0], [("certificate", ID)])
        ]
        query = SearchQuery(kind="certificate", q=ID)
        matched = validate_result(
            search_inventory(validate_inventory(inventory), query), query, "org"
        )
        self.assertEqual(matched["total"], 1)
        self.assertIn(ID, render_search(matched))
        self.assertIn("Collection freshness only", render_search(matched))
        self.assertEqual(
            search_inventory(inventory, SearchQuery(q="example.test"))["total"], 0
        )

    def test_no_live_rule_observation_authority(self):
        binding = ObservationBinding("org", ("certificate", ID), "via", ID)
        with self.assertRaises(ObservationError):
            ObservedInventory(self.inventory()).facts(binding, ["health_status"])

    def test_old_labels_and_removed_certificates_are_not_current(self):
        payload = response()
        old = deepcopy(row(payload, INFO))
        old["metric"]["status"] = "renew"
        old["values"] = [[NOW - 1, "1"]]
        payload["data"]["result"].append(old)
        self.assertEqual(
            dict(self.parse(payload).resources[0].fields)["status"], "deployed"
        )
        for record in payload["data"]["result"]:
            if "certificate_id" in record["metric"]:
                record["values"][0][0] = NOW - 1
        row(payload, COUNT)["values"][0][1] = "0"
        self.assertEqual(self.parse(payload).resources, ())

    def test_tls_verified_to_failed_drops_old_verified_time(self):
        payload = response()
        row(payload, TLS)["metric"]["status"] = "failed"
        row(payload, VERIFIED)["values"][0][0] = NOW - 1
        fields = dict(self.parse(payload).resources[0].fields)
        self.assertEqual(fields["tls_verification_status"], "failed")
        self.assertIsNone(fields["tls_verified_at"])

    def test_missing_material_and_no_retry_keep_unknown_instead_of_zero(self):
        payload = response()
        row(payload, INFO)["metric"].update(
            material_state="missing", renewal_owner="external", pending_action="none"
        )
        for key in (EXPIRY, RETRY):
            row(payload, key)["values"][0][0] = NOW - 1
        fields = dict(self.parse(payload).resources[0].fields)
        self.assertIsNone(fields["not_after_timestamp_seconds"])
        self.assertIsNone(fields["issuance_retry_count"])
        payload = response()
        row(payload, RETRY)["values"][0][1] = "0"
        self.assertEqual(
            dict(self.parse(payload).resources[0].fields)["issuance_retry_count"], 0
        )

    def test_expired_certificate_is_still_a_current_observation(self):
        payload = response()
        row(payload, EXPIRY)["values"][0][1] = str(NOW - 86400)
        snapshot = self.parse(payload)
        self.assertTrue(snapshot.authoritative)
        self.assertLess(
            dict(snapshot.resources[0].fields)["not_after_timestamp_seconds"], NOW
        )

    def test_collection_stale_future_and_monotonic_expiry(self):
        for stamp, freshness in (
            (NOW - 61, Freshness.STALE),
            (NOW + 3, Freshness.UNKNOWN),
        ):
            payload = response()
            row(payload, COMPLETED)["values"][0][1] = str(stamp)
            self.assertEqual(self.parse(payload).freshness, freshness)
        self.assertEqual(self.parse().freshness_at(159), Freshness.STALE)

    def test_down_missing_misaligned_and_duplicate_guards_fail_closed(self):
        for key in ("up", "source_up", COMPLETED, COUNT):
            payload = response()
            row(payload, key)["values"][0][0] = NOW - 1
            self.assertFalse(self.parse(payload).authoritative)
            payload = response()
            payload["data"]["result"].append(deepcopy(row(payload, key)))
            self.assertFalse(self.parse(payload).authoritative)
            payload = response()
            payload["data"]["result"].remove(row(payload, key))
            self.assertFalse(self.parse(payload).authoritative)
        for key in ("up", "source_up"):
            payload = response()
            row(payload, key)["values"][0][1] = "0"
            self.assertFalse(self.parse(payload).authoritative)

    def test_incomplete_expiry_retry_tls_or_verification_is_rejected(self):
        for key in (INFO, EXPIRY, RETRY, TLS, VERIFIED):
            payload = response()
            payload["data"]["result"].remove(row(payload, key))
            with self.subTest(key=key), self.assertRaises(InvalidPayload):
                self.parse(payload)
        payload = response()
        row(payload, COUNT)["values"][0][1] = "2"
        with self.assertRaises(InvalidPayload):
            self.parse(payload)

    def test_duplicate_hidden_series_wrong_uuid_enums_and_values_rejected(self):
        for key in (INFO, EXPIRY, RETRY, TLS, VERIFIED):
            payload = response()
            duplicate = deepcopy(row(payload, key))
            duplicate["metric"]["hidden"] = "other"
            payload["data"]["result"].append(duplicate)
            with self.assertRaises(InvalidPayload):
                self.parse(payload)
        for key, label, value in (
            (INFO, "certificate_id", "bad"),
            (EXPIRY, "certificate_id", "22345678-1234-4234-8234-123456789abc"),
            (INFO, "origin", "private"),
            (TLS, "status", "healthy"),
        ):
            payload = response()
            row(payload, key)["metric"][label] = value
            with self.assertRaises(InvalidPayload):
                self.parse(payload)
        for key, value in (
            (RETRY, "-1"),
            (RETRY, "1.5"),
            (EXPIRY, "-1"),
            (INFO, "0"),
            (TLS, "0"),
            (VERIFIED, str(NOW + 3)),
        ):
            payload = response()
            row(payload, key)["values"][0][1] = value
            with self.assertRaises(InvalidPayload):
                self.parse(payload)

    def test_explicit_target_query_limits_and_no_arbitrary_promql(self):
        self.assertIn(
            "covenant_certlord_source_up", query_parameters(SPEC, 60, 2, 10)["query"]
        )
        for spec in (
            {},
            {"match": {"job": "certificates"}},
            dict(SPEC, prefix="other"),
            {"match": dict(SPEC["match"], certificate_id=ID)},
        ):
            with self.assertRaises(InvalidPayload):
                selection(spec)
        payload = response()
        payload["warnings"] = ["partial"]
        with self.assertRaises(InvalidPayload):
            self.parse(payload)
        with self.assertRaises(InvalidPayload):
            self.parse(max_resources=0)
        payload = response()
        row(payload, EXPIRY)["values"] = [[NOW - 1001 + i, "1"] for i in range(1001)]
        with self.assertRaises(InvalidPayload):
            self.parse(payload)

    def test_client_rejects_fabricated_fields_and_bad_projection(self):
        fields = dict(self.parse().resources[0].fields)
        for key, value in (
            ("domains", ["invented.test"]),
            ("observed_at", "invented"),
            ("projection", "other"),
            ("origin", []),
            ("tls_verification_status", {}),
        ):
            altered = dict(fields, **{key: value})
            with self.assertRaises(InvalidPayload):
                validate_fields(altered, ID)

    def test_source_isolation_history_and_recovery(self):
        service = ReadService(
            [
                source(k, kind="prometheus_certlord", metric=SPEC)
                for k in ("east", "west")
            ],
            clock=lambda: 100.0,
        )
        service.set_running(True)
        for k in ("east", "west"):
            service.record(k, result(k, response()))
        service.record("east", result("east", outcome="timeout"))
        east, west = validate_inventory(service.status())["sources"]
        self.assertFalse(east["current"])
        self.assertTrue(west["current"])
        self.assertIsNotNone(east["last_valid_snapshot"])
        service.record("east", result("east", response()))
        self.assertTrue(service.status()["ready"])

    def test_input_is_unchanged_and_empty_inventory_is_authoritative(self):
        payload = response()
        original = deepcopy(payload)
        self.parse(payload)
        self.assertEqual(payload, original)
        payload["data"]["result"] = [
            r for r in payload["data"]["result"] if "certificate_id" not in r["metric"]
        ]
        row(payload, COUNT)["values"][0][1] = "0"
        self.assertTrue(self.parse(payload).authoritative)
        self.assertEqual(self.parse(payload).resources, ())
