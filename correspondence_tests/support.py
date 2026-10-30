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

FIXTURE = Path(__file__).with_name("covenant-monit.yml")
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
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.lock = threading.Lock()
        self.raw = payload()
        self.registry = CovenantRegistry()
        self.target = CovenantTarget(
            "parity", {}, yaml.safe_load(FIXTURE.read_text())["collects"], self.registry
        )
        self.processes, self.logs, self.sources = [], [], {}
        self.exporter = None
        logging.getLogger("covenant.collect").setLevel(logging.CRITICAL)

    def start(self):
        chain = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path != "/metrics":
                    self.send_error(404)
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
                "global": {"scrape_interval": "250ms", "scrape_timeout": "200ms"},
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
        for log in self.logs:
            log.close()
        self.temp.cleanup()

    def set(self, raw):
        with self.lock:
            self.raw = deepcopy(raw)
        return time.time()

    def exposition(self):
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
