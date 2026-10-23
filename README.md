# Atraxis

![Atraxis](assets/brand/svg/atraxis-logo-horizontal.svg)

Coordination layer for the Atraxis observability and automation ecosystem.

[Website (EN)](https://atraxis.io/) · [Site (FR)](https://atraxis.io/fr.html) ·
[User guide](docs/README.md) · [Contributing](CONTRIBUTING.md) · [Roadmap](ROADMAP.md)

## Try the public demo

[Open the demo (EN)](https://atraxis.io/demo.html?lang=en) ·
[Ouvrir la démo (FR)](https://atraxis.io/demo.html?lang=fr)

No account is required. Explore six synthetic resources, inspect retained observations
when a source is unavailable, and try a CPU-threshold example. This discovery
walkthrough runs entirely in the browser, stores no data and makes no service calls.
The local educational simulation is not connected to an Atropos engine or to the
authenticated pilot; installing Atraxis provides the actual service-backed interfaces.

## Install from source

Requires Python 3.11 or later. Start in a dedicated virtual environment:

```sh
git clone https://github.com/decryptus/atraxis.git
cd atraxis
python -m venv .venv
. .venv/bin/activate
python -m pip install .
```

Follow the [setup guide](docs/setup.md) before starting the daemon. To use rule
features, install the [independent Atropos client](docs/rule-remote.md#install-the-optional-client)
and connect to an explicitly configured Atropos service.

## Discover monit-docker in video

A three-minute tour of Docker monitoring, cron and serve modes, and the web and terminal interfaces.

- [Français — découvrir monit-docker](https://youtu.be/wPHl_Q2iyQs)
- [English — discover monit-docker](https://youtu.be/qyfNs5BTR3E)
- [All our videos on Decryptus](https://www.youtube.com/@decryptusHQ)

## Status

Experimental supervision and coordination software, licensed under GNU GPL version 3. Atraxis provides read-only HTTP collection, an in-memory inventory, an authenticated API and an optional web rule workbench. Readiness requires current observations from every configured source. This development version is not a general production-readiness claim.

Read containers from monit-docker, certificates from CertLord and selected numeric
metrics from Prometheus, including metrics exported by Covenant. The
[Covenant/Prometheus guide](docs/prometheus.md) explains configuration, retained
values and sample timestamps. Metric history and alert evaluation remain in
Prometheus; this adapter does not trigger interventions.

A [remote rule service](docs/rule-remote.md), [local rule operator CLI/TUI](docs/rule-console.md) and [rule API integration](docs/rule-lifecycle.md) now support scoped drafts, tests and explicit active versions through an independent Atropos service. Atraxis contains no rule engine or rule storage. Rule control remains separate from inventory collection and executes no interventions.

## Responsibilities

| Component | Responsibility |
| --- | --- |
| Atraxis | Read-only collection, authorized inventory and web/CLI/Textual operator interfaces |
| [Atropos](https://github.com/decryptus/atropos) | Rule lifecycle, scoped authorization, persistence and guarded workflow execution |
| Covenant | Collection, transformation and exposition of metrics as an exporter |
| Prometheus | Metrics storage and alert rule evaluation |
| Alertmanager | Notification routing, grouping and delivery |
| Auton | Execution of explicitly authorized jobs |
| monit-docker | Docker container monitoring and local automated actions |
| CertLord | TLS certificate issuance, renewal and deployment |

Each tool retains its own configuration and safety checks. Coordination by Atraxis must preserve those checks and the tool’s ability to operate independently.

## Name

Adrien selected the name Atraxis on 4 October 2026. It is inspired by the Atraxi from Doctor Who. This project is independent of the historical `anatolsommer/atraxi` Node.js HTTP monitoring tool.

## HTTP daemon and YAML

Start with the [setup and diagnostic guide](docs/setup.md): `atraxis setup`
previews a validated starter configuration, and `atraxis doctor` explains local
configuration problems and optionally checks current source observations.

See [runtime configuration and Docker notes](docs-runtime.md). Install with `python -m pip install .`, validate with `atraxis check-config --config /etc/atraxis/atraxis.yml` and start with `atraxis serve --config /etc/atraxis/atraxis.yml`. Commands are explicit; no automatic TUI startup. Source collection runs with the daemon. Use `atraxis status` or `atraxis inventory` with a separate client configuration to consult the API; see [CLI usage and exit codes](docs/cli.md).

For a single-host installation, follow the [Docker Compose guide](docs/docker-compose.md).
It covers separate verified TLS connections, persistent data, cold restoration
and explicit guarded-worker profiles for simulation and approved execution.

Source destinations, credentials and collection budgets can now be checked offline;
see [source configuration](docs/source-configuration.md). The daemon periodically reads configured
sources and retains the last valid snapshot when a source fails.

The [bounded read-only HTTP transport](docs/http-transport.md) is implemented and tested with synthetic fixtures. See [collection and inventory](docs/collection-inventory.md) for scheduling, freshness, API fields and limitations.


## Documentation

- **Users:** [installation and diagnostics](docs/setup.md), [Docker Compose](docs/docker-compose.md), configuration and operation in the [user guide](docs/README.md).
- **Contributors:** [architecture, tests and development](https://github.com/decryptus/atraxis/blob/main/CONTRIBUTING.md).



## Textual terminal interface

See the [Textual guide](docs/textual.md) for installation, navigation and operation confirmations.

The capture below shows the actual interface with synthetic demonstration data.

![Atraxis rule workbench with synthetic demonstration data](docs/images/textual-rules.png)

## Web inventory

Open `/ui` on the running daemon to view authenticated source, container and
certificate and metric snapshots. The page distinguishes current, historical and unknown
observations and refreshes on request. See [web inventory](docs/web-inventory.md)
for access, navigation and the current read-only scope.

The optional [web rule explorer](docs/web-rules.md) reads rule history, active
state and revision YAML from Atropos using server-side account mappings and
engine-enforced permissions.

With an explicitly configured browser origin, the [web rule workbench](docs/web-rules.md#enable-draft-editing-and-scenario-tests)
also edits existing YAML drafts and runs scenarios against an exact revision.
Atropos owns validation, rights, versions and reports; saving or testing does not activate rules or execute actions.

[Draft editor preview](docs/images/web-editor-desktop.png) · [Scenario report preview](docs/images/web-test-report-desktop.png)

A passing report can open an explicit activation review. Confirmation pins the tested
revision and current active generation; Atropos enforces rights and records the change.

Optional [browser source configuration](docs/web-configuration.md) provides reviewed
source changes for explicitly authorized installation administrators. Defaults stay
disabled; saved changes require an explicit daemon restart.

Use the offline [retention report](docs/retention.md) to inspect account archive
and configuration audit capacity without modifying evidence.

## Roadmap and license

See the [public roadmap](ROADMAP.md) for current capabilities and planned scope.
Project-owned code and documentation are licensed under GNU GPL version 3 only
([LICENSE](LICENSE), SPDX `GPL-3.0-only`). Dependencies retain their own licenses.
