"""Coherent bundle qualification and native observation consumers."""

from copy import deepcopy
from dataclasses import replace
import unittest

from atraxis.client import validate_inventory, render_inventory
from atraxis.inventory_search import SearchQuery, search_inventory
from atraxis.search_client import validate_result
from atraxis.resource_scopes import filter_source, target_key
from atraxis.models import Freshness
from atraxis.parsing import InvalidPayload
from atraxis.prometheus_monit import parse_prometheus_monit, query_parameters, selection
from atraxis.rule_observations import (
    ObservationBinding,
    ObservationError,
    ObservedInventory,
)
from atraxis.service import ReadService
from atraxis.web import render_inventory as render_web
from test_collection import result, source
from test_observations import NOW

ID = "a" * 64
SPEC = dict(prefix="parity_", match={"job": "fixture", "instance": "exporter:9118"})
VALUES = {
    "source_ready": 1,
    "observed_at_seconds": NOW - 2,
    "containers": 1,
    "container_info": 1,
    "cpu_percent": 250,
    "memory_percent": 92,
    "maintenance": 0,
    "restart_attempts": 2,
    "health_status": 1,
    "disk_percent": 60,
    "inode_percent": 50,
    "readable": 1,
    "writable": 0,
}


def response():
    rows = [dict(metric=dict(__name__="up", **SPEC["match"]), values=[[NOW, "1"]])]
    for suffix, value in VALUES.items():
        labels = dict(__name__="parity_" + suffix, **SPEC["match"])
        if suffix not in ("source_ready", "observed_at_seconds", "containers"):
            labels["id"] = ID
        if suffix == "container_info":
            labels.update(name="fixture", status="running")
        if suffix == "health_status":
            labels["health"] = "healthy"
        if suffix in ("disk_percent", "inode_percent", "readable", "writable"):
            labels.update(group="data", path="/data")
        rows.append(dict(metric=labels, values=[[NOW, str(value)]]))
    return dict(status="success", data=dict(resultType="matrix", result=rows))


def row(payload, suffix):
    name = "up" if suffix == "up" else "parity_" + suffix
    return next(r for r in payload["data"]["result"] if r["metric"]["__name__"] == name)


class BundleTests(unittest.TestCase):
    def parse(self, payload=None, **changes):
        arguments = dict(
            metric=SPEC,
            source_id="via",
            received_at=NOW,
            monotonic=100,
            max_age=60,
            max_resources=10,
            max_clock_skew=2,
        )
        arguments.update(changes)
        return parse_prometheus_monit(
            response() if payload is None else payload, **arguments
        )

    def inventory(self, payload=None):
        service = ReadService(
            [source("via", kind="prometheus_monit", metric=SPEC)], clock=lambda: 100.0
        )
        service.set_running(True)
        service.record("via", result("via", response() if payload is None else payload))
        return validate_inventory(service.status())

    def binding(self, **changes):
        return replace(
            ObservationBinding(
                "org", ("container", "fixture"), "via", ID, ("data", "/data")
            ),
            **changes,
        )

    def test_nine_fields_native_identity_and_attestation(self):
        inventory = self.inventory()
        observed = ObservedInventory(inventory, clock=lambda: 100.0)
        inputs = (
            "cpu_percent",
            "memory_percent",
            "maintenance",
            "restart_attempts",
            "health_status",
            "disk_percent",
            "inode_percent",
            "readable",
            "writable",
        )
        expected = dict(
            zip(inputs, (250.0, 92.0, False, 2, "healthy", 60.0, 50.0, True, False))
        )
        values, age = observed.facts(self.binding(), inputs)
        self.assertEqual(values, expected)
        self.assertIs(type(values["restart_attempts"]), int)
        self.assertIs(type(values["maintenance"]), bool)
        self.assertEqual(age, 2)
        self.assertEqual(
            observed.attestation(self.binding(), inputs)["source"]["native_id"], ID
        )
        self.assertIn("Monit-Docker via Prometheus", render_web(inventory))
        self.assertIn(ID, render_inventory(inventory, details=True))
        self.assertEqual(
            target_key(dict(kind="container", source="via", id=ID)),
            ("container", "via", ID),
        )
        inventory['context'] = dict(subject='alice', organization='org',
                                    organizations=[dict(id='org', name='Organization')])
        inventory["sources"] = [
            filter_source(inventory["sources"][0], [("container", ID)])
        ]
        query = SearchQuery(kind="container", q="fixture")
        matched = validate_result(
            search_inventory(validate_inventory(inventory), query), query, None
        )
        self.assertEqual(matched["total"], 1)
        self.assertEqual(matched["items"][0]["native_id"], ID)
        inventory["sources"] = [
            filter_source(inventory["sources"][0], [("container", "b" * 64)])
        ]
        self.assertEqual(
            search_inventory(validate_inventory(inventory), query)["total"], 0
        )

    def test_old_health_and_name_labels_do_not_override_current_state(self):
        payload = response()
        for suffix, label, value in (
            ("health_status", "health", "unhealthy"),
            ("container_info", "name", "old"),
        ):
            old = deepcopy(row(payload, suffix))
            old["metric"][label] = value
            old["values"] = [[NOW - 1, "1"]]
            payload["data"]["result"].append(old)
        fields = dict(self.parse(payload, max_resources=1).resources[0].fields)
        self.assertEqual(fields["health"], "healthy")
        self.assertEqual(fields["name"], "fixture")

    def test_removed_metric_is_unknown_immediately_at_next_scrape(self):
        payload = response()
        row(payload, "cpu_percent")["values"] = [[NOW - 1, "250"]]
        inventory = self.inventory(payload)
        self.assertTrue(inventory["ready"])
        with self.assertRaises(ObservationError):
            ObservedInventory(inventory).facts(self.binding(), ["cpu_percent"])
        self.assertEqual(
            ObservedInventory(inventory).facts(self.binding(), ["memory_percent"])[0],
            {"memory_percent": 92.0},
        )

    def test_origin_age_not_scrape_age_controls_expiry(self):
        payload = response()
        row(payload, "observed_at_seconds")["values"] = [[NOW, str(NOW - 61)]]
        self.assertEqual(self.parse(payload).freshness, Freshness.STALE)
        payload = response()
        row(payload, "observed_at_seconds")["values"] = [[NOW, str(NOW + 3)]]
        self.assertEqual(self.parse(payload).freshness, Freshness.UNKNOWN)
        self.assertEqual(self.parse(received_at=NOW - 3).freshness, Freshness.UNKNOWN)

    def test_down_unready_absent_and_misaligned_guards_fail_closed(self):
        for suffix in ("up", "source_ready", "observed_at_seconds", "containers"):
            payload = response()
            row(payload, suffix)["values"][0][0] = NOW - 1
            with self.subTest(suffix=suffix):
                self.assertFalse(self.parse(payload).authoritative)
            payload = response()
            payload["data"]["result"].remove(row(payload, suffix))
            with self.subTest(missing=suffix):
                self.assertFalse(self.parse(payload).authoritative)
        for suffix in ("up", "source_ready"):
            payload = response()
            row(payload, suffix)["values"][0][1] = "0"
            self.assertFalse(self.parse(payload).authoritative)

    def test_up_failure_retains_history_only_then_recovers(self):
        service = ReadService(
            [source("via", kind="prometheus_monit", metric=SPEC)], clock=lambda: 100.0
        )
        service.set_running(True)
        service.record("via", result("via", response()))
        payload = response()
        row(payload, "up")["values"] = [[NOW + 1, "0"]]
        service.record("via", result("via", payload))
        inventory = validate_inventory(service.status())
        self.assertFalse(inventory["ready"])
        self.assertIsNotNone(inventory["sources"][0]["last_valid_snapshot"])
        with self.assertRaises(ObservationError):
            ObservedInventory(inventory).facts(self.binding(), ["cpu_percent"])
        service.record("via", result("via", response()))
        self.assertTrue(service.status()["ready"])

    def test_co_scraped_duplicate_id_health_or_hidden_series_is_rejected(self):
        for suffix in (
            "container_info",
            "health_status",
            "cpu_percent",
            "disk_percent",
        ):
            payload = response()
            duplicate = deepcopy(row(payload, suffix))
            duplicate["metric"]["hidden"] = "other"
            payload["data"]["result"].append(duplicate)
            with self.subTest(suffix=suffix), self.assertRaises(InvalidPayload):
                self.parse(payload)
        payload = response()
        payload["data"]["result"].append(deepcopy(row(payload, "source_ready")))
        self.assertFalse(self.parse(payload).authoritative)

    def test_zero_containers_authoritative_only_with_current_guards(self):
        payload = response()
        payload["data"]["result"] = [
            r for r in payload["data"]["result"] if "id" not in r["metric"]
        ]
        row(payload, "containers")["values"][0][1] = "0"
        snapshot = self.parse(payload)
        self.assertTrue(snapshot.authoritative)
        self.assertEqual(snapshot.resources, ())
        row(payload, "containers")["values"][0][1] = "1"
        with self.assertRaises(InvalidPayload):
            self.parse(payload)

    def test_bad_values_ids_and_unknown_health_rejected(self):
        for suffix, value in (
            ("cpu_percent", "-1"),
            ("maintenance", "2"),
            ("restart_attempts", "1.5"),
            ("readable", "2"),
            ("containers", "1.5"),
            ("cpu_percent", "NaN"),
        ):
            payload = response()
            row(payload, suffix)["values"][0][1] = value
            with self.subTest(suffix=suffix), self.assertRaises(InvalidPayload):
                self.parse(payload)
        for suffix, key, value in (
            ("cpu_percent", "id", "b" * 64),
            ("health_status", "health", "starting"),
            ("container_info", "name", ""),
            ("disk_percent", "path", ""),
        ):
            payload = response()
            row(payload, suffix)["metric"][key] = value
            with self.subTest(suffix=suffix), self.assertRaises(InvalidPayload):
                self.parse(payload)

    def test_zero_and_false_preserved_and_missing_health_refused(self):
        payload = response()
        for suffix in ("cpu_percent", "memory_percent", "restart_attempts", "readable"):
            row(payload, suffix)["values"][0][1] = "0"
        payload["data"]["result"].remove(row(payload, "health_status"))
        observed = ObservedInventory(self.inventory(payload))
        facts, _ = observed.facts(
            self.binding(),
            ["cpu_percent", "restart_attempts", "readable", "maintenance"],
        )
        self.assertEqual(
            facts,
            {
                "cpu_percent": 0.0,
                "restart_attempts": 0,
                "readable": False,
                "maintenance": False,
            },
        )
        with self.assertRaises(ObservationError):
            observed.facts(self.binding(), ["health_status"])

    def test_source_binding_and_clock_expiry_remain_enforced(self):
        clock = [100.0]
        observed = ObservedInventory(self.inventory(), clock=lambda: clock[0])
        for binding in (
            self.binding(source_id="other"),
            self.binding(native_id="b" * 64),
            self.binding(filesystem=("data", "/other")),
        ):
            with self.assertRaises(ObservationError):
                observed.facts(binding, ["readable"])
        clock[0] += 59
        with self.assertRaises(ObservationError):
            observed.facts(self.binding(), ["cpu_percent"])

    def test_two_source_instances_fail_and_recover_independently(self):
        sources = [
            source(name, kind="prometheus_monit", metric=SPEC)
            for name in ("east", "west")
        ]
        service = ReadService(sources, clock=lambda: 100.0)
        service.set_running(True)
        for name in ("east", "west"):
            service.record(name, result(name, response()))
        service.record("east", result("east", outcome="timeout"))
        east, west = validate_inventory(service.status())["sources"]
        self.assertFalse(east["current"])
        self.assertTrue(west["current"])
        self.assertEqual(east["last_valid_snapshot"]["resources"][0]["native_id"], ID)
        self.assertEqual(west["last_valid_snapshot"]["resources"][0]["native_id"], ID)
        self.assertNotEqual(east["source_id"], west["source_id"])
        service.record("east", result("east", response()))
        self.assertTrue(service.status()["ready"])

    def test_exact_one_target_configuration_and_bounded_query(self):
        params = query_parameters(SPEC, 60, 2, 10)
        self.assertIn("up|parity_source_ready|", params["query"])
        self.assertIn('instance="exporter:9118",job="fixture"', params["query"])
        self.assertTrue(params["query"].endswith("[60s]"))
        self.assertEqual(params["limit"], 205)
        for spec in (
            dict(SPEC, prefix=".*"),
            dict(SPEC, match={"job": "fixture"}),
            dict(SPEC, match=dict(SPEC["match"], id=ID)),
            dict(SPEC, extra=True),
        ):
            with self.assertRaises(InvalidPayload):
                selection(spec)

    def test_wrong_target_unknown_metric_partial_and_malformed_payloads_rejected(self):
        for key, value in (
            ("job", "other"),
            ("instance", "other"),
            ("__name__", "unapproved"),
        ):
            payload = response()
            row(payload, "cpu_percent")["metric"][key] = value
            with self.assertRaises(InvalidPayload):
                self.parse(payload)
        payload = response()
        payload["warnings"] = ["partial"]
        with self.assertRaises(InvalidPayload):
            self.parse(payload)
        for values in ([], [[NOW, "1"], [NOW, "2"]], [[True, "1"]], [[NOW, True]]):
            payload = response()
            row(payload, "cpu_percent")["values"] = values
            with self.assertRaises(InvalidPayload):
                self.parse(payload)

    def test_future_field_stamp_and_input_mutation_refused(self):
        payload = response()
        original = deepcopy(payload)
        self.parse(payload)
        self.assertEqual(payload, original)
        row(payload, "cpu_percent")["values"] = [[NOW + 1, "1"]]
        with self.assertRaises(InvalidPayload):
            self.parse(payload)

    def test_resource_and_sample_limits_apply_to_whole_bundle(self):
        with self.assertRaises(InvalidPayload):
            self.parse(max_resources=0)
        payload = response()
        row(payload, "cpu_percent")["values"] = [
            [NOW - 1001 + i, "1"] for i in range(1001)
        ]
        with self.assertRaises(InvalidPayload):
            self.parse(payload)
        payload = response()
        for record in payload["data"]["result"]:
            record["values"] = [[NOW - 999 + i, "1"] for i in range(1000)]
        with self.assertRaises(InvalidPayload):
            self.parse(payload)
