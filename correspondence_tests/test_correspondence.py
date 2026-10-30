"""Qualification only: real Covenant and Prometheus, synthetic source, no actions."""

from copy import deepcopy
import time
import unittest

from atraxis.rule_observations import (
    ObservationBinding,
    ObservationError,
    ObservedInventory,
)
from support import (
    Chain,
    CONTAINER,
    INPUTS,
    MAX_AGE,
    direct_facts,
    metric_fact,
    payload,
)


class CorrespondenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chain = Chain()
        cls.addClassCleanup(cls.chain.close)
        cls.chain.start()

    def setUp(self):
        self.raw = payload()
        self.changed = self.chain.set(self.raw)
        self.chain.wait("source_ready", self.changed)

    def test_01_nine_facts_and_identity_survive_both_prometheus_instances(self):
        expected = direct_facts(self.chain, self.raw)
        self.assertEqual(set(expected), set(INPUTS))
        for site in ("east", "west"):
            actual = {
                field: metric_fact(self.chain.wait(field, self.changed, site), field)
                for field in INPUTS
            }
            self.assertEqual(actual, expected)
            for field in (
                "maintenance",
                "readable",
                "writable",
                "health_status",
                "restart_attempts",
            ):
                self.assertIs(type(actual[field]), type(expected[field]))
        east = self.chain.read("cpu_percent", "east")["sources"][0]
        west = self.chain.read("cpu_percent", "west")["sources"][0]
        self.assertNotEqual(east["source_id"], west["source_id"])
        self.assertEqual(
            east["last_valid_snapshot"]["resources"][0]["native_id"],
            west["last_valid_snapshot"]["resources"][0]["native_id"],
        )
        self.assertNotEqual(
            east["last_valid_snapshot"]["resources"][0]["native_id"], CONTAINER
        )
        with self.assertRaises(ObservationError):
            ObservedInventory(self.chain.read("cpu_percent")).facts(
                ObservationBinding(
                    "fixture", ("container", "fixture"), "east.cpu_percent", CONTAINER
                ),
                ["cpu_percent"],
            )

    def test_02_unknown_invalid_values_are_omitted_not_zero(self):
        cases = (
            ("cpu_percent", "cpu_percent", (None, "250", {"value": 250}, True, -1)),
            ("memory_percent", "mem_percent", (None, "92", True, -1)),
            ("restart_attempts", "restart_attempts", (None, "2", True, -1, 1.5)),
            ("maintenance", "maintenance_active", (None, "false", 0, 1)),
            ("disk_percent", "disk_percent", (None, "60", True, -1)),
            ("inode_percent", "inode_percent", (None, "50", True, -1)),
            ("readable", "fs_readable", (None, "1", True, -1, 2)),
            ("writable", "fs_writable", (None, "0", False, -1, 2)),
        )
        for field, key, values in cases:
            for value in values:
                raw = deepcopy(self.raw)
                target = raw["containers"][0]
                if field in ("disk_percent", "inode_percent", "readable", "writable"):
                    target = target["filesystems"][0]
                target[key] = value
                with self.subTest(field=field, value=value):
                    self.chain.set(raw)
                    self.assertEqual(self.chain.samples(field), [])
                    with self.assertRaises(ObservationError):
                        direct_facts(self.chain, raw, [field])
        for value in (None, "none", "starting", "unknown"):
            raw = deepcopy(self.raw)
            raw["containers"][0]["health"] = value
            with self.subTest(health=value):
                self.chain.set(raw)
                self.assertEqual(self.chain.samples("health_status"), [])
                with self.assertRaises(ObservationError):
                    direct_facts(self.chain, raw, ["health_status"])

    def test_03_removed_metric_remains_in_range_query_until_age_limit(self):
        first = self.chain.wait("cpu_percent", self.changed)
        self.assertEqual(metric_fact(first, "cpu_percent"), 250)
        raw = deepcopy(self.raw)
        del raw["containers"][0]["cpu_percent"]
        changed = self.chain.set(raw)
        self.assertEqual(self.chain.samples("cpu_percent"), [])
        self.chain.wait("source_ready", changed)
        retained = self.chain.read("cpu_percent")
        self.assertTrue(retained["ready"])
        self.assertEqual(metric_fact(retained, "cpu_percent"), 250)
        with self.assertRaises(ObservationError):
            direct_facts(self.chain, raw, ["cpu_percent"])
        # Wait for the real Prometheus range to age out, not a mocked timestamp.
        deadline = time.monotonic() + MAX_AGE + 5
        while time.monotonic() < deadline:
            if not self.chain.read("cpu_percent")["ready"]:
                break
            time.sleep(0.25)
        else:
            self.fail("Removed series did not expire from the bounded range")
        raw["containers"][0]["cpu_percent"] = 0
        changed = self.chain.set(raw)
        self.assertEqual(
            metric_fact(self.chain.wait("cpu_percent", changed), "cpu_percent"), 0
        )
        self.assertEqual(
            direct_facts(self.chain, raw, ["cpu_percent"])["cpu_percent"], 0
        )

    def test_04_new_scrape_does_not_prove_origin_freshness(self):
        raw = deepcopy(self.raw)
        raw.update(
            age_seconds=MAX_AGE + 30,
            last_success_at=time.time() - MAX_AGE - 30,
            last_cycle_finished_at=time.time() - MAX_AGE - 30,
        )
        changed = self.chain.set(raw)
        current = self.chain.wait("cpu_percent", changed)
        origin = self.chain.wait("observed_at_seconds", changed)
        fields = origin["sources"][0]["last_valid_snapshot"]["resources"][0]["fields"]
        self.assertTrue(current["ready"])
        self.assertLess(time.time() - fields["sampled_at"], MAX_AGE)
        self.assertGreater(time.time() - fields["value"], MAX_AGE)
        with self.assertRaises(ObservationError):
            direct_facts(self.chain, raw, ["cpu_percent"])

    def test_05_health_transition_exposes_both_label_sets_in_the_range(self):
        self.chain.wait("health_status", self.changed)
        raw = deepcopy(self.raw)
        raw["containers"][0]["health"] = "healthy"
        changed = self.chain.set(raw)
        current = self.chain.wait("health_status", changed)
        resources = current["sources"][0]["last_valid_snapshot"]["resources"]
        healths = {
            dict((v["name"], v["value"]) for v in r["fields"]["labels"])["health"]
            for r in resources
        }
        self.assertEqual(healths, {"healthy", "unhealthy"})
        self.assertEqual(
            direct_facts(self.chain, raw, ["health_status"])["health_status"], "healthy"
        )

    def test_06_unavailable_source_requires_joining_readiness(self):
        self.chain.wait("cpu_percent", self.changed)
        raw = deepcopy(self.raw)
        raw["ready"] = False
        raw["last_cycle_success"] = False
        changed = self.chain.set(raw)
        status = self.chain.wait("source_ready", changed)
        self.assertEqual(
            status["sources"][0]["last_valid_snapshot"]["resources"][0]["fields"][
                "value"
            ],
            0,
        )
        self.assertEqual(self.chain.samples("cpu_percent"), [])
        self.assertTrue(self.chain.read("cpu_percent")["ready"])
        with self.assertRaises(ObservationError):
            direct_facts(self.chain, raw, ["cpu_percent"])

    def test_07_wrong_or_ambiguous_identity_does_not_publish_selected_values(self):
        for mutate in (
            "other_container",
            "duplicate_container",
            "other_filesystem",
            "duplicate_filesystem",
        ):
            raw = deepcopy(self.raw)
            if mutate == "other_container":
                raw["containers"][0]["id"] = "b" * 64
            elif mutate == "duplicate_container":
                raw["containers"] *= 2
            elif mutate == "other_filesystem":
                raw["containers"][0]["filesystems"][0]["path"] = "/other"
            else:
                raw["containers"][0]["filesystems"] *= 2
            field = "disk_percent" if "filesystem" in mutate else "cpu_percent"
            with self.subTest(mutate=mutate):
                self.chain.set(raw)
                self.assertEqual(self.chain.samples(field), [])
                with self.assertRaises(ObservationError):
                    direct_facts(self.chain, raw, [field])

    def test_08_zero_false_and_cpu_over_one_core_preserve_units(self):
        raw = deepcopy(self.raw)
        raw["containers"][0].update(
            cpu_percent=275.5,
            mem_percent=0,
            maintenance_active=True,
            restart_attempts=0,
        )
        raw["containers"][0]["filesystems"][0].update(
            disk_percent=0, inode_percent=0, fs_readable=0, fs_writable=1
        )
        changed = self.chain.set(raw)
        expected = direct_facts(self.chain, raw)
        for field in INPUTS:
            if field == "health_status":
                continue  # Transition history is separately tested.
            self.assertEqual(
                metric_fact(self.chain.wait(field, changed), field), expected[field]
            )
