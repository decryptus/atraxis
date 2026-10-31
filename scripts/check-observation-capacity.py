#!/usr/bin/env python3
"""Bounded synthetic observation qualification; never accepts external targets."""

import argparse
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "correspondence_tests"))

import requests
from certlord_support import ObservationChain, certificate_payload, iso
from support import payload
from atraxis.observation_codec import encode, InvalidObservation, MAX_ROWS
from atraxis.prometheus_observation import query_parameters
from atraxis.service import ReadService
from atraxis.transport import HTTPTransport

DIAGNOSTIC_LIMIT = 8 * 1024 * 1024
DEADLINE_SECONDS = 30


class CapacityChain(ObservationChain):
    SCRAPE_INTERVAL = "2s"
    SCRAPE_TIMEOUT = "1500ms"

    def bundle_source(self, site="east"):
        return replace(super().bundle_source(site), max_age=600, timeout=5,
                       max_resources=1000)


def fixture(kind, count):
    raw = payload() if kind == "monit" else certificate_payload()
    key = "containers" if kind == "monit" else "certificates"
    template = raw[key][0]
    raw[key] = []
    for i in range(count):
        item = deepcopy(template)
        if kind == "monit":
            item.update(id=f"{i + 1:064x}", name=f"fixture-{i:04d}")
        else:
            item.update(certificate_id=str(uuid.UUID(int=i + 1)),
                        domains=[f"cert-{i:04d}.example.test",
                                 f"www.cert-{i:04d}.example.test"])
        raw[key].append(item)
    return raw


def refresh(raw, kind, generation):
    now = time.time()
    if kind == "monit":
        raw.update(last_success_at=now - 1, last_cycle_finished_at=now - 1,
                   cycles_total=generation + 2)
        for item in raw["containers"]:
            item["cpu_percent"] = 250 + generation
    else:
        raw.update(started_at=iso(now - 3), completed_at=iso(now - 1),
                   completed_timestamp_seconds=now - 1)
        for item in raw["certificates"]:
            item["observed_at"] = iso(now - 2)
            item["verification"]["checked_at"] = iso(now - 5)


def row_boundary(kind):
    low, high = 0, 1000
    while low + 1 < high:
        middle = (low + high) // 2
        try:
            encode(fixture(kind, middle), kind)
        except InvalidObservation:
            high = middle
        else:
            low = middle
    return low


def get(session, origin, path, params=None):
    # Diagnostic reads may exceed the consumer's 4 MiB limit, but remain bounded.
    with session.get(origin + path, params=params, timeout=5,
                     allow_redirects=False, stream=True) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_content(65536):
            data.extend(chunk)
            if len(data) > DIAGNOSTIC_LIMIT:
                raise AssertionError("diagnostic_response_limit")
    return json.loads(data), len(data)


def current_query(session, source, kind, after, digest):
    params = query_parameters(source.metric, source.max_age, source.timeout,
                              source.max_resources, kind)
    deadline = time.monotonic() + DEADLINE_SECONDS
    while time.monotonic() < deadline:
        try:
            document, size = get(session, source.origin, "/api/v1/query", params)
        except requests.ConnectionError:
            time.sleep(0.1)
            continue
        assert document["status"] == "success", "query_failed"
        rows = document["data"]["result"]
        clocks = [float(r["value"][1]) for r in rows
                  if r["metric"].get("covenant_sample_time") == "1"
                  and r["metric"].get("covenant_metric") == "up"]
        manifests = [r["metric"].get("sha256") for r in rows
                     if r["metric"].get("covenant_metric") ==
                     f"covenant_{kind}_observation_manifest"]
        down = any(r["metric"].get("covenant_metric") ==
                   f"covenant_{kind}_observation_up"
                   and "covenant_sample_time" not in r["metric"]
                   and float(r["value"][1]) == 0 for r in rows)
        if clocks and min(clocks) > after:
            if (digest is not None and manifests and set(manifests) == {digest}) or (
                    digest is None and down and not manifests):
                return size, len(rows)
        time.sleep(0.1)
    raise AssertionError("expected_generation_not_observed")


def projection(inventory):
    assert inventory["ready"], "inventory_not_ready"
    snapshot = inventory["sources"][0]["last_valid_snapshot"]
    return snapshot["source_metadata"], [
        (r["native_id"], r["fields"]) for r in snapshot["resources"]]


def rss_bytes(process):
    # Linux only, sampled after reads; this is not a process peak or a leak test.
    for line in Path(f"/proc/{process.pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    raise AssertionError("rss_unavailable")


def scenario(kind, count, generations, boundary):
    chain = CapacityChain(kind)
    session = requests.Session()
    session.trust_env = False
    session.auth = ("reader", "synthetic-only")
    records = []
    started = time.monotonic()
    try:
        chain.raw = fixture(kind, count)
        chain.start()
        services = {site: ReadService([chain.bundle_source(site)])
                    for site in ("east", "west")}
        for service in services.values():
            service.set_running(True)
        raw = deepcopy(chain.raw)
        for generation in range(generations):
            refresh(raw, kind, generation)
            rows, digest = encode(raw, kind)
            expected = projection(chain.direct(raw))
            changed = chain.set(raw)
            changed_clock = time.monotonic()
            exposition_bytes = len(chain.exposition())
            for index, site in enumerate(services):
                source = chain.bundle_source(site)
                query_bytes, vector_rows = current_query(
                    session, source, kind, changed, digest)
                read_started = time.monotonic()
                result = HTTPTransport().fetch(source)
                services[site].record(source.source_id, result)
                inventory = services[site].status()
                read_seconds = time.monotonic() - read_started
                elapsed = time.monotonic() - changed_clock
                if query_bytes <= source.max_response_bytes:
                    assert result.outcome == "success", result.outcome
                    assert projection(inventory) == expected, "projection_mismatch"
                else:
                    assert result.outcome == "response_too_large", result.outcome
                    assert not inventory["ready"], "oversized_inventory_current"
                stats, _ = get(session, source.origin, "/api/v1/status/tsdb")
                records.append(dict(
                    generation=generation, site=site, typed_rows=len(rows),
                    source_bytes=len(json.dumps(raw).encode()),
                    exposition_bytes=exposition_bytes, query_bytes=query_bytes,
                    vector_rows=vector_rows, outcome=result.outcome,
                    update_to_read_seconds=round(elapsed, 4),
                    fetch_and_projection_seconds=round(read_seconds, 4),
                    head_series=stats["data"]["headStats"]["numSeries"],
                    rss_bytes=rss_bytes(chain.processes[index])))
        for site in services:
            samples = [r for r in records if r["site"] == site]
            assert len({s["vector_rows"] for s in samples}) == 1, "historical_labels_returned"
            assert samples[-1]["head_series"] > samples[0]["head_series"], "churn_not_observed"
        if count == boundary:
            # Exercise real exporter rejection, then smaller-inventory recovery.
            changed = chain.set(fixture(kind, boundary + 1))
            for site, service in services.items():
                source = chain.bundle_source(site)
                current_query(session, source, kind, changed, None)
                result = HTTPTransport().fetch(source)
                assert result.outcome == "success", result.outcome
                service.record(source.source_id, result)
                inventory = service.status()
                assert not inventory["ready"], "row_overflow_current"
                attempt = inventory["sources"][0]["last_attempt"]
                assert attempt["outcome"] == "success", attempt["outcome"]
                assert attempt["observation_freshness"] == "unknown"
                assert not inventory["sources"][0]["current"]
            raw = fixture(kind, 1)
            _, digest = encode(raw, kind)
            changed = chain.set(raw)
            for site, service in services.items():
                source = chain.bundle_source(site)
                current_query(session, source, kind, changed, digest)
                service.record(source.source_id, HTTPTransport().fetch(source))
                assert projection(service.status()) == projection(chain.direct(raw))
        assert {(m, p) for m, p, _ in chain.source_calls} == {("GET", chain.route)}
        return dict(kind=kind, resources=count, row_boundary_resources=boundary,
                    overflow_and_recovery_checked=count == boundary,
                    duration_seconds=round(time.monotonic() - started, 3),
                    samples=records)
    finally:
        session.close()
        chain.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--generations", type=int, default=4)
    parser.add_argument("--smoke", action="store_true",
                        help="One resource and two generations per adapter")
    args = parser.parse_args()
    if not 2 <= args.generations <= 10:
        parser.error("generations must be between 2 and 10")
    revisions = {}
    for name, directory in (("atraxis_base", ROOT),
                            ("covenant", os.environ["COVENANT_SOURCE"]),
                            ("certlord", os.environ["CERTLORD_SOURCE"])):
        revisions[name] = subprocess.check_output(
            ["git", "-C", str(directory), "rev-parse", "HEAD"], text=True).strip()
    report = dict(schema_version=1, python=platform.python_version(),
                  platform=platform.platform(), max_rows=MAX_ROWS,
                  revisions=revisions,
                  harness_sha256={path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                                  for path in ("scripts/check-observation-capacity.py",
                                               "correspondence_tests/support.py",
                                               "correspondence_tests/certlord_support.py")},
                  prometheus_version=subprocess.check_output(
                      [os.environ["PROMETHEUS_BINARY"], "--version"], text=True).splitlines()[0],
                  cpu_count=os.cpu_count(),
                  scrape_interval_seconds=2, scrape_timeout_seconds=1.5,
                  scenarios=[])
    for kind in ("monit", "certlord"):
        boundary = row_boundary(kind)
        for count in ([1] if args.smoke else [1, 100, 200, boundary]):
            result = scenario(kind, count, 2 if args.smoke else args.generations,
                              boundary)
            report["scenarios"].append(result)
            args.output.write_text(json.dumps(report, indent=2) + "\n")
            samples = result["samples"]
            print(json.dumps(dict(kind=kind, resources=count,
                  outcomes=sorted({s["outcome"] for s in samples}),
                  max_query_bytes=max(s["query_bytes"] for s in samples),
                  median_read_seconds=round(statistics.median(
                      s["fetch_and_projection_seconds"] for s in samples), 4))),
                  flush=True)


if __name__ == "__main__":
    main()
