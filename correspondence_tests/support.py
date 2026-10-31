"""Disposable real Prometheus servers and Covenant collectors on loopback."""

from copy import deepcopy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

import bcrypt
import yaml
from prometheus_client import generate_latest
from prometheus_client.parser import text_string_to_metric_families

sys.path.insert(0, str(Path(os.environ["COVENANT_SOURCE"]).resolve()))
from covenant.classes.target import CovenantRegistry, CovenantTarget
from covenant.plugins.http import CovenantHttpPlugin
from covenant.classes.plugins import CovenantEPTObject
from covenant.filters import fjmespath  # Registers real JMESPath filtering.

from atraxis.client import validate_inventory
from atraxis.rule_observations import (
    ObservedInventory,
    ObservationBinding,
    SCALAR_FIELDS,
    FILESYSTEM_FIELDS,
)
from atraxis.service import ReadService
from atraxis.source_config import parse_sources
from atraxis.transport import HTTPTransport, TransportResult

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "covenant-monit-legacy.yml"
CONTAINER = "a" * 64
INPUTS = tuple(SCALAR_FIELDS) + tuple(FILESYSTEM_FIELDS)
MAX_AGE = 15


def payload():
    now = time.time()
    return dict(
        api_version=1,
        ready=True,
        running=True,
        last_cycle_success=True,
        cycles_total=2,
        errors_total=0,
        age_seconds=1,
        last_success_at=now - 1,
        last_cycle_finished_at=now - 1,
        containers=[
            dict(
                id=CONTAINER,
                name="fixture",
                status="running",
                health="unhealthy",
                cpu_percent=250,
                mem_percent=92,
                maintenance_active=False,
                restart_attempts=2,
                filesystems=[
                    dict(
                        group="data",
                        path="/var/lib/data",
                        disk_usage=120,
                        disk_available=80,
                        disk_total=200,
                        disk_percent=60,
                        inode_usage=5,
                        inode_available=5,
                        inode_total=10,
                        inode_percent=50,
                        fs_mode="rw",
                        fs_readable=1,
                        fs_writable=0,
                        fs_executable=None,
                    )
                ],
            )
        ],
    )


class Chain:
    SCRAPE_INTERVAL = "250ms"
    SCRAPE_TIMEOUT = "200ms"
    def __init__(self, *, http_plugin=False):
        self.http_plugin = http_plugin
        self.source_server = None
        self.source_available = True
        self.source_calls = []
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.lock = threading.Lock()
        self.plugin_lock = threading.Lock()
        self.raw = payload()
        self.registry = CovenantRegistry()
        self.target = CovenantTarget(
            "parity",
            {},
            yaml.safe_load(FIXTURE.read_text())["endpoints"]["monit"]["metrics"][0][
                "collects"
            ],
            self.registry,
        )
        self.processes, self.logs, self.sources = [], [], {}
        self.exporter = None
        self.available = True
        logging.getLogger("covenant.collect").setLevel(logging.CRITICAL)

    def start(self):
        chain = self
        if self.http_plugin:

            class SourceHandler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_GET(self):
                    chain.source_calls.append((self.command, self.path))
                    if self.path != "/v1/status" or not chain.source_available:
                        self.send_error(503)
                        return
                    with chain.lock:
                        data = json.dumps(chain.raw).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)

            self.source_server = ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler)
            self.source_thread = threading.Thread(
                target=self.source_server.serve_forever, daemon=True
            )
            self.source_thread.start()
            self.registry = CovenantRegistry()
            self.target = CovenantTarget(
                "parity",
                dict(
                    url="http://127.0.0.1:"
                    + str(self.source_server.server_port)
                    + "/v1/status",
                    method="get",
                    format="json",
                    timeout=1,
                    allow_redirects=False,
                ),
                yaml.safe_load(FIXTURE.read_text())["endpoints"]["monit"]["metrics"][0][
                    "collects"
                ],
                self.registry,
            )
            self.plugin = CovenantHttpPlugin("correspondence")
            self.plugin.targets = [self.target]
            self.plugin.registry = self.registry
            logging.getLogger("covenant.plugins.http").setLevel(logging.CRITICAL)

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path != "/metrics":
                    self.send_error(404)
                    return
                if not chain.available:
                    self.send_error(503)
                    return
                data = chain.exposition()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.exporter = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.exporter.serve_forever, daemon=True)
        self.thread.start()
        credentials = self.root / "credentials.json"
        credentials.write_text(
            json.dumps(dict(username="reader", password="synthetic-only"))
        )
        credentials.chmod(0o600)
        for name in ("east", "west"):
            root = self.root / name
            root.mkdir()
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            config = {
                "global": {
                    "scrape_interval": self.SCRAPE_INTERVAL,
                    "scrape_timeout": self.SCRAPE_TIMEOUT,
                },
                "scrape_configs": [
                    {
                        "job_name": "correspondence",
                        "static_configs": [
                            {"targets": ["127.0.0.1:" + str(self.exporter.server_port)]}
                        ],
                    }
                ],
            }
            (root / "prometheus.yml").write_text(yaml.safe_dump(config))
            (root / "web.yml").write_text(
                yaml.safe_dump(
                    {
                        "basic_auth_users": {
                            "reader": bcrypt.hashpw(
                                b"synthetic-only", bcrypt.gensalt(rounds=4)
                            ).decode()
                        }
                    }
                )
            )
            log = (root / "prometheus.log").open("w")
            self.logs.append(log)
            process = subprocess.Popen(
                [
                    str(Path(os.environ["PROMETHEUS_BINARY"]).resolve()),
                    "--config.file=" + str(root / "prometheus.yml"),
                    "--web.config.file=" + str(root / "web.yml"),
                    "--web.listen-address=127.0.0.1:" + str(port),
                    "--storage.tsdb.path=" + str(root / "data"),
                ],
                stdout=log,
                stderr=log,
            )
            self.processes.append(process)
            documents = []
            for field in (*INPUTS, "observed_at_seconds", "source_ready"):
                labels = ["job"]
                if field in INPUTS:
                    labels += ["id"]
                if field in FILESYSTEM_FIELDS:
                    labels += ["group", "path"]
                if field == "health_status":
                    labels += ["health"]
                documents.append(
                    dict(
                        source_id=name + "." + field,
                        kind="prometheus",
                        origin="http://127.0.0.1:" + str(port),
                        allow_loopback_http=True,
                        credentials_file=str(credentials),
                        timeout=2,
                        poll_interval=2,
                        max_age=MAX_AGE,
                        metric=dict(
                            name="parity_" + field,
                            match={"job": "correspondence"},
                            labels=labels,
                        ),
                    )
                )
            self.sources[name] = {
                s.source_id.split(".", 1)[1]: s
                for s in parse_sources(documents, root / "atraxis.yml")
            }
        return self

    def close(self):
        for process in self.processes:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if self.exporter is not None:
            self.exporter.shutdown()
            self.exporter.server_close()
            self.thread.join(5)
        if self.source_server is not None:
            self.source_server.shutdown()
            self.source_server.server_close()
            self.source_thread.join(5)
        for log in self.logs:
            log.close()
        self.temp.cleanup()

    def set(self, raw):
        with self.lock:
            self.raw = deepcopy(raw)
        return time.time()

    def exposition(self):
        if self.http_plugin:
            # Covenant's endpoint worker serializes collection and exposition.
            # A separate lock leaves the synthetic source HTTP handler free to read.
            with self.plugin_lock:
                return self.plugin.do_metrics(
                    CovenantEPTObject(
                        "correspondence",
                        "synthetic",
                        "correspondence",
                        "metrics",
                        {},
                        {},
                        lambda obj: None,
                    )
                )
        with self.lock:
            self.target(deepcopy(self.raw))
            return generate_latest(self.registry)

    def samples(self, name):
        return [
            s
            for f in text_string_to_metric_families(self.exposition().decode())
            for s in f.samples
            if s.name == "parity_" + name
        ]

    def read(self, field, site="east"):
        source = self.sources[site][field]
        service = ReadService([source])
        service.set_running(True)
        service.record(source.source_id, HTTPTransport().fetch(source))
        return validate_inventory(service.status())

    def wait(self, field, after=0, site="east"):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if any(p.poll() is not None for p in self.processes):
                raise AssertionError("Prometheus exited unexpectedly")
            status = self.read(field, site)
            if status["ready"]:
                resources = status["sources"][0]["last_valid_snapshot"]["resources"]
                if (
                    resources
                    and max(r["fields"]["sampled_at"] for r in resources) > after
                ):
                    return status
            time.sleep(0.1)
        raise AssertionError("Expected sample not observed: " + field)

    def bundle_source(self, site="east"):
        return replace(
            self.sources[site]["cpu_percent"],
            source_id=site + ".bundle",
            kind="prometheus_monit",
            metric=dict(
                prefix="parity_",
                match={
                    "job": "correspondence",
                    "instance": "127.0.0.1:" + str(self.exporter.server_port),
                },
            ),
        )

    def bundle(self, after=0, site="east"):
        source = self.bundle_source(site)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = HTTPTransport().fetch(source)
            if result.outcome == "success":
                rows = result.payload["data"]["result"]
                stamps = [
                    r["values"][-1][0] for r in rows if r["metric"]["__name__"] == "up"
                ]
                if stamps and max(stamps) > after:
                    service = ReadService([source])
                    service.set_running(True)
                    service.record(source.source_id, result)
                    return validate_inventory(service.status())
            time.sleep(0.1)
        raise AssertionError("Expected bundle scrape not observed")

    def direct(self, raw):
        source = replace(
            self.sources["east"]["cpu_percent"],
            source_id="direct",
            kind="monit_docker",
            metric=None,
        )
        service = ReadService([source])
        service.set_running(True)
        service.record(
            "direct",
            TransportResult(
                "direct", "success", time.time(), time.time(), time.monotonic(), raw
            ),
        )
        return service.status()


def direct_facts(chain, raw, inputs=INPUTS):
    binding = ObservationBinding(
        "fixture",
        ("container", "fixture"),
        "direct",
        CONTAINER,
        ("data", "/var/lib/data"),
    )
    return ObservedInventory(chain.direct(raw)).facts(binding, inputs)[0]


def metric_fact(status, field):
    """Test-only decoding, not an installed observation adapter."""
    resources = status["sources"][0]["last_valid_snapshot"]["resources"]
    assert len(resources) == 1, "Qualification decoder requires an unambiguous series"
    resource = resources[0]
    value = resource["fields"]["value"]
    labels = {x["name"]: x["value"] for x in resource["fields"]["labels"]}
    assert labels["id"] == CONTAINER
    if field in FILESYSTEM_FIELDS:
        assert (labels["group"], labels["path"]) == ("data", "/var/lib/data")
    if field == "health_status":
        assert value == 1 and labels["health"] in ("healthy", "unhealthy")
        return labels["health"]
    if field in ("maintenance", "readable", "writable"):
        assert value in (0, 1)
        return bool(value)
    if field == "restart_attempts":
        assert value >= 0 and value.is_integer()
        return int(value)
    return value
