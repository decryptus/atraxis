"""The installed bundle adapter closes the gaps asserted by generic-metric tests."""

from copy import deepcopy
import time
import unittest

from atraxis.rule_observations import (
    ObservationBinding,
    ObservationError,
    ObservedInventory,
)
from support import Chain, CONTAINER, INPUTS, MAX_AGE, direct_facts, payload


class GuardedCorrespondenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chain = Chain(http_plugin=True)
        cls.addClassCleanup(cls.chain.close)
        cls.chain.start()

    def setUp(self):
        self.raw = payload()
        self.chain.available = True
        self.chain.source_available = True
        self.changed = self.chain.set(self.raw)

    def facts(self, inventory, fields=INPUTS, site="east"):
        binding = ObservationBinding(
            "fixture",
            ("container", "fixture"),
            site + ".bundle",
            CONTAINER,
            ("data", "/var/lib/data"),
        )
        return ObservedInventory(inventory).facts(binding, fields)[0]

    def test_nine_native_facts_match_on_both_prometheus_instances(self):
        for site in ("east", "west"):
            inventory = self.chain.bundle(self.changed, site)
            self.assertTrue(inventory["ready"])
            self.assertTrue(self.chain.source_calls)
            self.assertEqual(set(self.chain.source_calls), {("GET", "/v1/status")})
            self.assertEqual(
                self.facts(inventory, site=site), direct_facts(self.chain, self.raw)
            )
            self.assertEqual(
                inventory["sources"][0]["last_valid_snapshot"]["resources"][0][
                    "native_id"
                ],
                CONTAINER,
            )

    def test_disappearance_is_missing_at_next_scrape_and_zero_recovers(self):
        self.assertEqual(
            self.facts(self.chain.bundle(self.changed))["cpu_percent"], 250
        )
        del self.raw["containers"][0]["cpu_percent"]
        changed = self.chain.set(self.raw)
        inventory = self.chain.bundle(changed)
        self.assertTrue(inventory["ready"])
        with self.assertRaises(ObservationError):
            self.facts(inventory, ["cpu_percent"])
        self.raw["containers"][0]["cpu_percent"] = 0
        changed = self.chain.set(self.raw)
        self.assertEqual(self.facts(self.chain.bundle(changed))["cpu_percent"], 0)

    def test_health_transition_and_return_preserve_only_current_state(self):
        for health in ("unhealthy", "healthy", "unhealthy"):
            self.raw["containers"][0]["health"] = health
            changed = self.chain.set(self.raw)
            self.assertEqual(
                self.facts(self.chain.bundle(changed))["health_status"], health
            )

    def test_origin_expiry_future_and_invalid_timestamp_are_rejected(self):
        for stamp in (time.time() - MAX_AGE - 30, time.time() + 120, None, "bad"):
            raw = deepcopy(self.raw)
            raw["last_success_at"] = stamp
            changed = self.chain.set(raw)
            inventory = self.chain.bundle(changed)
            with self.subTest(stamp=stamp):
                self.assertFalse(inventory["ready"])
                with self.assertRaises(ObservationError):
                    self.facts(inventory)

    def test_source_unready_and_failed_scrape_cannot_supply_facts(self):
        self.chain.bundle(self.changed)
        self.raw["ready"] = False
        self.raw["last_cycle_success"] = False
        changed = self.chain.set(self.raw)
        self.assertFalse(self.chain.bundle(changed)["ready"])
        self.chain.set(payload())
        self.chain.source_available = False
        changed = time.time()
        self.assertFalse(self.chain.bundle(changed)["ready"])
        self.chain.source_available = True
        self.chain.available = False
        changed = time.time()
        self.assertFalse(self.chain.bundle(changed)["ready"])
        self.chain.available = True
        changed = time.time()
        self.assertTrue(self.chain.bundle(changed)["ready"])

    def test_duplicate_identity_refused_and_empty_selection_is_authoritative(self):
        self.raw["containers"] *= 2
        changed = self.chain.set(self.raw)
        self.assertFalse(self.chain.bundle(changed)["ready"])
        self.raw["containers"] = []
        changed = self.chain.set(self.raw)
        inventory = self.chain.bundle(changed)
        self.assertTrue(inventory["ready"])
        self.assertEqual(
            inventory["sources"][0]["last_valid_snapshot"]["resources"], []
        )
        with self.assertRaises(ObservationError):
            self.facts(inventory)

    def test_unknown_health_and_filesystem_removal_do_not_reuse_old_values(self):
        self.chain.bundle(self.changed)
        self.raw["containers"][0]["health"] = "starting"
        self.raw["containers"][0]["filesystems"] = []
        changed = self.chain.set(self.raw)
        inventory = self.chain.bundle(changed)
        for fields in (["health_status"], ["readable"]):
            with self.assertRaises(ObservationError):
                self.facts(inventory, fields)
        self.assertEqual(self.facts(inventory, ["cpu_percent"]), {"cpu_percent": 250})
