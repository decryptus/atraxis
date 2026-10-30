"""Closed wire protocol, exact native parity and fail-closed scrape semantics."""

from copy import deepcopy
from dataclasses import replace
import unittest
from atraxis.observation_codec import encode, decode, MAX_ROWS
from atraxis.prometheus_observation import query_parameters, selection, MAX_VECTOR
from atraxis.models import Freshness
from observation_support import native, vector, parse, direct, row, spec, NOW

KINDS = ("monit", "certlord")


class CompleteObservationTests(unittest.TestCase):
    def test_every_field_metadata_type_and_freshness_equal_native(self):
        for kind in KINDS:
            raw = native(kind)
            expected = direct(kind, raw)
            self.assertEqual(
                parse(kind, raw), replace(expected, source_kind="prometheus_" + kind)
            )
            self.assertEqual(
                encode(raw, kind), encode(decode(*self._encoded(raw, kind)), kind)
            )

    def _encoded(self, raw, kind):
        rows, sha = encode(raw, kind)
        return rows, kind, sha

    def test_input_unchanged_and_unknown_private_fields_not_exported(self):
        for kind in KINDS:
            raw = native(kind)
            raw["secret"] = "do-not-export"
            raw["containers" if kind == "monit" else "certificates"][0][
                "secret"
            ] = "do-not-export"
            original = deepcopy(raw)
            result = vector(kind, raw)
            parse(kind, response=result)
            self.assertEqual(raw, original)
            self.assertNotIn("do-not-export", repr(result))

    def test_missing_null_zero_false_empty_list_are_preserved(self):
        raw = native("monit")
        item = raw["containers"][0]
        for value in (None, 0, 0.0):
            item["cpu_percent"] = value
            self.assertEqual(
                parse("monit", raw).resources, direct("monit", raw).resources
            )
        del item["cpu_percent"]
        for filesystems in (None, []):
            item["filesystems"] = filesystems
            self.assertEqual(
                parse("monit", raw).resources, direct("monit", raw).resources
            )

    def test_removed_changed_or_extra_row_cannot_masquerade_as_complete(self):
        for kind in KINDS:
            for change in ("delete", "value", "duplicate", "extra"):
                payload = vector(kind)
                r = row(payload, "value")
                if change == "delete":
                    payload["data"]["result"].remove(r)
                elif change == "value":
                    r["value"][1] = "7"
                elif change == "duplicate":
                    payload["data"]["result"].append(deepcopy(r))
                else:
                    r["metric"]["secret"] = "extra"
                with (
                    self.subTest(kind=kind, change=change),
                    self.assertRaises(ValueError),
                ):
                    parse(kind, response=payload)

    def test_guards_missing_down_and_mixed_scrape_never_authoritative(self):
        for kind in KINDS:
            for suffix in (
                "up",
                "observation_up",
                "manifest",
                "collected_at_seconds",
                "collection_elapsed_seconds",
                "value",
            ):
                payload = vector(kind)
                row(payload, suffix, clock=True)["value"][1] = str(NOW - 1)
                self.assertFalse(parse(kind, response=payload).authoritative)
            for suffix in ("up", "observation_up"):
                payload = vector(kind)
                row(payload, suffix)["value"][1] = "0"
                self.assertFalse(parse(kind, response=payload).authoritative)
            payload = vector(kind)
            payload["data"]["result"] = [
                r
                for r in payload["data"]["result"]
                if not r["metric"]["covenant_metric"].endswith("manifest")
            ]
            self.assertFalse(parse(kind, response=payload).authoritative)

    def test_prometheus_evaluation_time_never_rejuvenates_old_sample(self):
        for kind in KINDS:
            payload = vector(kind, stamp=NOW - 61, collected=NOW - 61)
            self.assertFalse(parse(kind, response=payload).authoritative)
            for kwargs in (
                dict(stamp=NOW + 3),
                dict(collected=NOW + 3),
                dict(collected=NOW - 10),
            ):
                self.assertFalse(
                    parse(kind, response=vector(kind, **kwargs)).authoritative
                )

    def test_source_clock_age_and_http_duration_match_native(self):
        raw = native("monit")
        result = parse(
            "monit",
            response=vector(
                "monit", raw, stamp=NOW - 2, collected=NOW - 1, elapsed=0.5
            ),
        )
        self.assertEqual(
            result,
            replace(direct("monit", raw, elapsed=1.5), source_kind="prometheus_monit"),
        )
        raw["age_seconds"] = 61
        self.assertEqual(parse("monit", raw).freshness, Freshness.STALE)

    def test_native_validation_rejects_bad_types_ids_duplicates_and_limits(self):
        for kind in KINDS:
            collection = "containers" if kind == "monit" else "certificates"
            identity = "id" if kind == "monit" else "certificate_id"
            for change in ("id", "duplicate", "type", "limit"):
                raw = native(kind)
                if change == "id":
                    raw[collection][0][identity] = "bad"
                elif change in ("duplicate", "limit"):
                    raw[collection] *= 11 if change == "limit" else 2
                else:
                    raw["api_version" if kind == "monit" else "schema_version"] = True
                for reader in (direct, parse):
                    with self.assertRaises(ValueError):
                        reader(kind, raw)

    def test_target_isolation_and_no_arbitrary_query(self):
        for kind in KINDS:
            query = query_parameters(spec(kind), 60, 2, 10, kind)["query"]
            self.assertIn("timestamp(", query)
            self.assertNotIn("[60s]", query)
            for changes in (
                dict(match={"job": "fixture"}),
                dict(extra=True),
                dict(match={"job": "fixture", "instance": "exporter", "path": "root"}),
            ):
                with self.assertRaises(ValueError):
                    selection(dict(spec(kind), **changes), kind)
            payload = vector(kind)
            payload["data"]["result"][0]["metric"]["job"] = "other"
            with self.assertRaises(ValueError):
                parse(kind, response=payload)

    def test_warnings_bad_values_partial_clocks_and_large_response_rejected(self):
        for kind in KINDS:
            for change in ("warning", "nan", "bool", "clock", "large"):
                payload = vector(kind)
                if change == "warning":
                    payload["warnings"] = ["partial"]
                elif change == "nan":
                    payload["data"]["result"][0]["value"][1] = "NaN"
                elif change == "bool":
                    payload["data"]["result"][0]["value"][0] = True
                elif change == "clock":
                    payload["data"]["result"].pop()
                else:
                    payload["data"]["result"] *= (
                        MAX_VECTOR // len(payload["data"]["result"]) + 1
                    )
                with (
                    self.subTest(kind=kind, change=change),
                    self.assertRaises(ValueError),
                ):
                    parse(kind, response=payload)

    def test_empty_inventory_and_unready_have_same_native_result(self):
        for kind in KINDS:
            raw = native(kind)
            raw["containers" if kind == "monit" else "certificates"] = []
            self.assertEqual(
                parse(kind, raw),
                replace(direct(kind, raw), source_kind="prometheus_" + kind),
            )
        raw.update()  # Native Monit readiness is independent from successful HTTP export.
        raw = native("monit")
        raw.update(
            ready=False,
            last_cycle_success=False,
            containers=[],
            age_seconds=None,
            last_success_at=None,
        )
        self.assertEqual(
            parse("monit", raw),
            replace(direct("monit", raw), source_kind="prometheus_monit"),
        )

    def test_long_native_strings_large_integers_and_float_types_survive(self):
        for kind in KINDS:
            raw = native(kind)
            if kind == "monit":
                raw["containers"][0].update(
                    name="x" * 4096, cpu_percent=0.0, restart_attempts=2**60 + 1
                )
            else:
                raw["certificates"][0].update(domains=["x" * 4096], version=2**60 + 1)
            self.assertEqual(
                parse(kind, raw),
                replace(direct(kind, raw), source_kind="prometheus_" + kind),
            )
