"""Real CertLord formatter and Covenant plugin, with disposable synthetic HTTP."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import json
import os
from pathlib import Path
import sys
import threading
import time
import yaml

from support import Chain, payload, CONTAINER
from covenant.plugins.observations import (
    CovenantCertlordObservationPlugin,
    CovenantMonitObservationPlugin,
)
from covenant.classes.plugins import CovenantEPTObject, EPTS_SYNC
from atraxis.service import ReadService
from atraxis.transport import TransportResult

sys.path.insert(0, str(Path(os.environ["CERTLORD_SOURCE"]).resolve()))
from certlord.services.observations import prometheus_metrics

ID = "12345678-1234-4234-8234-123456789abc"
FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "covenant-certlord.yml"


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat()


def certificate_payload():
    now = time.time()
    record = dict(
        certificate_id=ID,
        domains=["example.test"],
        version=1,
        status="deployed",
        origin="acme",
        renewal_owner="acme",
        pending_action="renew",
        material_state="readable",
        not_before=iso(now - 86400),
        not_after=iso(now + 86400),
        not_after_timestamp_seconds=now + 86400,
        fingerprint_sha256="a" * 64,
        issuance_retry_count=3,
        last_issuance_retry_at=iso(now - 10),
        observed_at=iso(now - 2),
        verification=dict(
            status="verified",
            checked_at=iso(now - 5),
            error_code=None,
            expected_fingerprint_sha256="a" * 64,
            observed_fingerprint_sha256="a" * 64,
            target_sha256="b" * 64,
        ),
    )
    return dict(
        schema_version=1,
        started_at=iso(now - 3),
        completed_at=iso(now - 1),
        completed_timestamp_seconds=now - 1,
        certificates=[record],
    )


class ObservationChain(Chain):
    def __init__(self, kind="certlord"):
        super().__init__()
        self.kind = kind
        self.raw = certificate_payload() if kind == "certlord" else payload()
        self.route = (
            "/api/certificates/observations" if kind == "certlord" else "/v1/status"
        )
        self.bad_exposition = False
        self.previous_sync = EPTS_SYNC.copy()

    def start(self):
        chain = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                chain.source_calls.append(
                    (self.command, self.path, self.headers.get("Authorization"))
                )
                auth = (
                    "Basic " + base64.b64encode(b"reader:synthetic-certlord").decode()
                )
                if (
                    self.path != chain.route
                    or self.headers.get("Authorization") != auth
                ):
                    self.send_error(403)
                    return
                if not chain.source_available:
                    self.send_error(503)
                    return
                with chain.lock:
                    data = (
                        b"malformed"
                        if chain.bad_exposition
                        else json.dumps(chain.raw).encode()
                    )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.source_server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.source_thread = threading.Thread(
            target=self.source_server.serve_forever, daemon=True
        )
        self.source_thread.start()
        config = yaml.safe_load(
            (FIXTURE.parent / ("covenant-" + self.kind + ".yml")).read_text()
        )["endpoints"]["certificates" if self.kind == "certlord" else "monit"]
        config["metrics"][0]["config"].update(
            url="http://127.0.0.1:" + str(self.source_server.server_port) + self.route,
            username="reader",
            password_file="reader.password",
            timeout=1,
        )
        (self.root / "reader.password").write_text("synthetic-certlord\n")
        (self.root / "reader.password").chmod(0o600)
        config.update(
            general=dict(result_timeout=30, server_id="synthetic"),
            covenant=dict(config_dir=str(self.root)),
        )
        cls = (
            CovenantCertlordObservationPlugin
            if self.kind == "certlord"
            else CovenantMonitObservationPlugin
        )
        self.certlord_plugin = cls("observation-correspondence")
        self.certlord_plugin.init(config)
        self.certlord_plugin.safe_init()
        return super().start()

    def close(self):
        try:
            super().close()
        finally:
            EPTS_SYNC.clear()
            EPTS_SYNC.update(self.previous_sync)

    def exposition(self):
        with self.plugin_lock:
            return self.certlord_plugin.do_metrics(
                CovenantEPTObject(
                    "fixture",
                    "fixture:1",
                    "fixture",
                    "metrics",
                    {},
                    {},
                    lambda obj: None,
                )
            )

    def bundle_source(self, site="east"):
        source = super().bundle_source(site)
        return replace(
            source,
            kind="prometheus_" + self.kind,
            metric=dict(
                match=source.metric["match"],
                **(
                    {"prefix": "covenant_monit_observation_"}
                    if self.kind == "monit"
                    else {}
                ),
            ),
            max_response_bytes=4 * 1024 * 1024,
        )

    def direct(self, raw):
        source = replace(
            self.bundle_source(),
            source_id="direct",
            kind="certlord" if self.kind == "certlord" else "monit_docker",
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

    def bundle(self, after=0, site="east"):
        from atraxis.transport import HTTPTransport
        from atraxis.client import validate_inventory

        source = self.bundle_source(site)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            result = HTTPTransport().fetch(source)
            if result.outcome == "success":
                clocks = [
                    float(r["value"][1])
                    for r in result.payload["data"]["result"]
                    if r["metric"].get("covenant_metric") == "up"
                    and r["metric"].get("covenant_sample_time") == "1"
                ]
                if clocks and max(clocks) > after:
                    service = ReadService([source])
                    service.set_running(True)
                    service.record(source.source_id, result)
                    return validate_inventory(service.status())
            time.sleep(0.1)
        raise AssertionError("Expected complete observation scrape not observed")


class CertlordChain(ObservationChain):
    pass


class MonitChain(ObservationChain):
    def __init__(self):
        super().__init__("monit")
