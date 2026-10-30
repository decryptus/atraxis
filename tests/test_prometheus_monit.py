"""Full Monit projection through rule inputs, scoped clients and history."""

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
from observation_support import native, vector, spec, parse, direct
from test_collection import result, source

ID = "a" * 64


def inventory(raw=None):
    service = ReadService(
        [source("source-a", kind="prometheus_monit", metric=spec("monit"))],
        clock=lambda: 100.0,
    )
    service.set_running(True)
    service.record("source-a", result("source-a", vector("monit", raw)))
    return validate_inventory(service.status())


class MonitCompleteTests(unittest.TestCase):
    def binding(self, **changes):
        return replace(
            ObservationBinding(
                "org", ("container", "fixture"), "source-a", ID, ("data", "/data")
            ),
            **changes,
        )

    def test_nine_rule_inputs_and_source_bound_attestation(self):
        observed = ObservedInventory(inventory(), clock=lambda: 100.0)
        expected = dict(
            cpu_percent=250,
            memory_percent=92.5,
            maintenance=False,
            restart_attempts=2,
            health_status="healthy",
            disk_percent=60,
            inode_percent=50,
            readable=True,
            writable=False,
        )
        facts, age = observed.facts(self.binding(), tuple(expected))
        self.assertEqual(facts, expected)
        self.assertEqual(age, 5)
        self.assertIs(type(facts["maintenance"]), bool)
        self.assertIs(type(facts["restart_attempts"]), int)
        self.assertEqual(
            observed.attestation(self.binding(), tuple(expected))["source"][
                "native_id"
            ],
            ID,
        )
        for binding in (
            self.binding(source_id="other"),
            self.binding(native_id="b" * 64),
            self.binding(filesystem=("data", "/other")),
        ):
            with self.assertRaises(ObservationError):
                observed.facts(binding, ["readable"])

    def test_unknown_measurement_and_health_states_match_direct(self):
        for health in ("healthy", "unhealthy", "starting", "none", "unknown"):
            raw = native("monit")
            raw["containers"][0]["health"] = health
            self.assertEqual(
                parse("monit", raw).resources, direct("monit", raw).resources
            )
        raw = native("monit")
        raw["containers"][0]["cpu_percent"] = None
        with self.assertRaises(ObservationError):
            ObservedInventory(inventory(raw)).facts(self.binding(), ["cpu_percent"])

    def test_full_client_render_search_scope_and_metadata(self):
        data = inventory()
        snap = data["sources"][0]["last_valid_snapshot"]
        self.assertEqual(snap["source_metadata"]["actions"]["executed"], 5)
        self.assertEqual(snap["resources"][0]["fields"]["mem_usage"], 2**54 + 1)
        self.assertIn("Monit-Docker via Prometheus", render_web(data))
        self.assertIn(ID, render_inventory(data, details=True))
        data["sources"] = [filter_source(data["sources"][0], [("container", ID)])]
        query = SearchQuery(q="fixture")
        matched = validate_result(search_inventory(data, query), query, None)
        self.assertEqual(matched["total"], 1)
        self.assertIsNone(data["sources"][0]["last_valid_snapshot"]["source_metadata"])
        data["sources"] = [filter_source(data["sources"][0], [("container", "b" * 64)])]
        self.assertEqual(search_inventory(data, query)["total"], 0)

    def test_replacement_multiple_containers_and_filesystem_order(self):
        raw = native("monit")
        second = deepcopy(raw["containers"][0])
        second["id"] = "b" * 64
        raw["containers"].append(second)
        raw["containers"][0]["filesystems"].append(
            dict(second["filesystems"][0], path="/other")
        )
        self.assertEqual(parse("monit", raw).resources, direct("monit", raw).resources)
        self.assertEqual(len(parse("monit", raw).resources), 2)

    def test_source_failure_history_recovery_and_monotonic_expiry(self):
        service = ReadService(
            [
                source(k, kind="prometheus_monit", metric=spec("monit"))
                for k in ("east", "west")
            ],
            clock=lambda: 100.0,
        )
        service.set_running(True)
        for k in ("east", "west"):
            service.record(k, result(k, vector("monit")))
        service.record("east", result("east", outcome="timeout"))
        east, west = validate_inventory(service.status())["sources"]
        self.assertFalse(east["current"])
        self.assertTrue(west["current"])
        self.assertIsNotNone(east["last_valid_snapshot"])
        service.record("east", result("east", vector("monit")))
        self.assertTrue(service.status()["ready"])
        clock = [100.0]
        observed = ObservedInventory(inventory(), clock=lambda: clock[0])
        clock[0] += 56
        with self.assertRaises(ObservationError):
            observed.facts(self.binding(), ["cpu_percent"])
