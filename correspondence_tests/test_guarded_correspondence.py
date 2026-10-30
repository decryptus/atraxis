"""The installed bundle adapter closes the gaps asserted by generic-metric tests."""

from copy import deepcopy
import time
import unittest

from atraxis.rule_observations import (
    ObservationBinding,
    ObservationError,
    ObservedInventory,
)
from certlord_support import MonitChain
from support import CONTAINER, INPUTS, MAX_AGE, direct_facts, payload


class GuardedCorrespondenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.chain = MonitChain()
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
            self.assertEqual(
                {(m, p) for m, p, _ in self.chain.source_calls}, {("GET", "/v1/status")}
            )
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

    def test_native_age_expiry_and_invalid_timestamp_are_rejected(self):
        for changes in (
            dict(age_seconds=MAX_AGE + 30),
            dict(last_success_at=None),
            dict(last_success_at="bad"),
        ):
            raw = deepcopy(self.raw)
            raw.update(changes)
            self.assertFalse(self.chain.direct(raw)["ready"])
            self.assertFalse(self.chain.bundle(self.chain.set(raw))["ready"])

    def test_full_metadata_filesystems_and_additional_containers_equal_direct(self):
        self.raw.update(
            last_error_code=0,
            actions=dict(
                executed=2,
                cooldown=0,
                pending=1,
                **{"restart-limit": 3, "maintenance": 4, "dry-run": 5},
            ),
        )
        self.raw["containers"][0].update(
            mem_usage=2**54 + 1,
            mem_limit=2**55 + 1,
            io_read=7,
            io_write=8,
            net_rx=9,
            net_tx=10,
            pid=1,
            oom_events=0,
            starts_recent=3,
            pids_current=4,
            pids_limit=5,
            pids_percent=80,
            event_window_seconds=60,
            event_window_end=int(time.time()),
            event_history_complete=1,
            manual_actions_protected=True,
            restart_limit=5,
            maintenance_until=0,
        )
        self.raw["containers"].append(
            dict(self.raw["containers"][0], id="b" * 64, name="another")
        )
        expected = self.chain.direct(self.raw)["sources"][0]["last_valid_snapshot"]
        changed = self.chain.set(self.raw)
        for site in ("east", "west"):
            actual = self.chain.bundle(changed, site)["sources"][0][
                "last_valid_snapshot"
            ]
            self.assertEqual(actual["source_metadata"], expected["source_metadata"])
            self.assertEqual(
                [(r["native_id"], r["fields"]) for r in actual["resources"]],
                [(r["native_id"], r["fields"]) for r in expected["resources"]],
            )

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

    def test_real_native_monitor_status_and_snapshot_projection(self):
        import os, sys
        from pathlib import Path
        from types import SimpleNamespace

        sys.path.insert(0, str(Path(os.environ["MONIT_SOURCE"]).resolve()))
        from monit_docker.service import MonitorService
        from monit_docker.domain.models import ContainerSnapshot

        record = ContainerSnapshot(**self.raw["containers"][0])
        service = MonitorService(
            lambda observer: SimpleNamespace(snapshots=[record]),
            clock=time.time,
            monotonic=lambda: 100.0,
        )
        service.run_cycle()
        import json
        raw = json.loads(json.dumps(service.status(), allow_nan=False))
        expected = self.chain.direct(raw)["sources"][0]["last_valid_snapshot"]
        actual = self.chain.bundle(self.chain.set(raw))["sources"][0][
            "last_valid_snapshot"
        ]
        self.assertEqual(actual["source_metadata"], expected["source_metadata"])
        self.assertEqual(
            actual["resources"][0]["fields"], expected["resources"][0]["fields"]
        )
