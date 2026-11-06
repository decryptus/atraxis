# Atraxis roadmap

Updated 6 November 2026. Atraxis 1.0 has a bounded reference configuration.
This roadmap distinguishes implemented behavior from later work and broader
qualification. Future priorities may change; no future release date is promised.

## 1.0.6 — ingress and form budgets (qualification pending)

The candidate separates Basic failure budgets by account behind configured trusted
proxies. The supplied nginx configurations enforce a per-client request budget;
existing deployments must apply both changes together. Four active forms per
account/controller replace the previous limit of sixteen. Core CI now rejects
compact Python statements (Ruff E701–E703), with the earlier formatting-only
commit recorded for blame tooling.

Unit and disposable Compose acceptance cover account isolation, expiry, form
eviction, ingress bursts and forged forwarding headers. These runtime changes
are not part of 1.0.5 and require a newly qualified package before publication or
deployment; the previous release's operational evidence cannot qualify them.
The guided installer currently pins the published 1.0.5 image, which still has
the shared Basic peer budget. Updating that pin to a newly qualified image is
required before the guided deployment can deliver the complete correction.
The registry Compose starter also remains on its separately qualified 1.0.1
images; its application image and proxy trust configuration must be upgraded
together after qualification. The synthetic source Compose test exercises the
new runtime with explicit proxy trust, independently of those published pins.

## 1.0.5 readability patch

The readability patch expands compact Python statements throughout the application. Its exact package passed operational cold recovery and two-hour endurance qualification. See the [release notes](docs/releases/1.0.5.md).

## 1.0.4 access controls

Atraxis 1.0.4 limits repeated Basic authentication failures, filters TCP peers for
non-loopback proxy deployments, isolates rule form tokens and renders scoped
inventory navigation directly. Existing non-loopback configurations need
`general.trusted_proxy_networks` before upgrade. See the [runtime guide](docs-runtime.md).
The Python install and core test matrices include 3.13.

## 1.0.3 HTTP read deadlines

The installed-image tests exercise slow headers and bodies with HTTPdis 0.6.39. See the [release notes and qualification scope](docs/releases/1.0.3.md).

## 1.0.2 release

The guided installer passed its Compose and native installation tests. The
1.0.2 runtime passed operational recovery and a two-hour synthetic endurance run. See the [1.0.2 qualification notes](docs/releases/1.0.2.md).
The release is available on [GitHub](https://github.com/decryptus/atraxis/releases/tag/v1.0.2), [PyPI](https://pypi.org/project/atraxis/1.0.2/) and [Docker Hub](https://hub.docker.com/r/decryptus/atraxis/tags?name=1.0.2).

## Available today

- Read-only HTTP collection and an authenticated inventory API and web interface,
  with explicit current, historical and unavailable observations.
- Selected numeric Prometheus samples, including Covenant exports, with explicit
  label projection, sample timestamps, read grants and inventory search. This
  experimental adapter does not supply metric-driven interventions or graphs.
- Complete Monit-Docker and CertLord inventory through optional Covenant observation
  plugins and Prometheus, with native field/freshness correspondence checks.
  Direct API access remains recommended; production capacity and retention costs
  for the indirect routes remain to be qualified.
- Scoped accounts, organizations, memberships, bounded delegation and inventory
  permissions; section searches through the documented web, CLI and Textual flows.
- SQLite account storage, explicit backup/restore, configuration setup and
  diagnostics.
- Local source configuration and inventory grant commands with explicit OS identity,
  separate review/apply, current authority checks and atomic audit. Host-provisioned
  connection catalogs and inventory scopes remain separately managed.
- Rule authoring, scenario tests, explicit activation and rollback, intervention
  inspection and uncertainty reconciliation through the independent Atropos API
  client. Atropos owns the engine and authoritative rule state.
- A single-host Docker Compose installation with persistent storage, verified TLS
  and optional, explicitly selected worker profiles.
- An [operator journey](docs/operator-journey.md) covering separate credentials,
  scoped read access, renewal and recovery using the existing commands.

Collection never authorizes an intervention. Saving or testing a rule does not
activate it. Execution requires an approved binding and current permissions.

## First stable reference

Atraxis 1.0 closes the scope, operational cold-recovery and explicitly authorized
integration gates for the [documented reference configuration](docs/v1.0.md).
The [delivery baseline](docs/v1-delivery.md) and contributor evidence index retain
what was tested and what remains outside this release.
[Atraxis 1.0.0](https://github.com/decryptus/atraxis/releases/tag/v1.0.0) publishes
the exact qualified CI archives with their source and validation provenance.

The operational reference uses a current direct Monit observation, one approved
non-destructive HTTPS diagnostic through Auton, and the existing local Alertmanager
receiver. Actual firing/resolution and duplicate refusal are verified. This does
not enable automatic remediation or qualify email/SMS/chat or human receipt.
The public demonstration remains read-only.

The earlier four-hour synthetic SQLite/Redis campaign is retained at its original
inputs; it is not an integrated-queue or live throughput benchmark. Future scope
and broader operational promises require their own evidence.

## Patch 1.0.1

Patch `1.0.1` selects the stable client and adds the Docker image. It selects the published Atropos client
1.0.0 and aligns Compose/integration inputs with the matching Atropos 1.0.0 tag.
Fresh optional-client installation and the existing integration/recovery checks
must pass on the exact change. The published Atraxis 1.0.0 archives and their
original qualification remain unchanged. Package and image publication remain explicit; no deployment or extended
endurance claim follows automatically from this dependency update.

## Guided installation — implementation candidate

The first stage adds an internal preparation engine and synthetic security tests:
strict plans, host-owned filesystem/network capabilities, real source HTTPS and
adapter checks, certificate/key matching and create-only private bundles. See the
[contributor design and limits](docs/installation-core.md).

The Textual preparation wizard now provides simple/advanced inputs, source selection, credential entry, review and explicit preparation. The host supplies fixed TLS materials and may declare one physical storage alias. The operator guide includes captures of the actual screens. Optional host-authorized deployment now generates and starts either a two-container Compose site or a native systemd user service. Both check authenticated HTTPS readiness.
Remaining stages include Atropos rule-engine integration, assisted policy provisioning, a batch entry point and upgrade/recovery acceptance. Each stage
must reuse the engine checks instead of adding frontend-only validation.

PyPI/Docker Hub releases, Compose, public websites and demonstrations remain on
their published baseline. They require updates when the integrated installation
flow is qualified and explicitly released; this internal candidate is not enabled
on those surfaces.

## Qualification and limits

Disposable Compose acceptance exercises the operator guide's read-only access
example against an authenticated HTTPS fixture. It verifies source isolation,
wrong-context denial, account revocation, login rotation with recreation, engine
token replacement without service restart and web/engine CA renewal. Subsequent
cold restoration preserves the rotated credentials, account state and audit,
rule history and revoked access. A separate scoped inventory reader and its TLS
source configuration remain active in the cold backup. This does not qualify
automated certificate issuance, site identity provisioning or live host recovery.
See [validation scope](docs/contributing.md#operator-access-acceptance).

A separate-host recovery case transfers the exact application/proxy images,
complete cold volumes and site files between two disposable CI hosts. It verifies
retained audits, revoked access, queued work and execution pause; an explicit
resume after action revocation must cancel the queued synthetic run without an
effect. A synthetic HTTP receipt accepted before a lost response also retains its
uncertain state and target lock across the transfer. Guarded workers do not replay
it; explicit reconciliation checks current rights and the reviewed state, records
an attestation and releases the lock without executing later steps. These synthetic checks qualify SQLite with the storage-backed queue;
see the
[separate-host scope](docs/contributing.md#recovery-on-a-separate-host). The separate
operational recovery described in the [1.0 support matrix](docs/v1.0.md) verifies
the existing pilot state, installed inventory sources and current authority on a
distinct host. Reconciliation against arbitrary surviving external systems remains
outside that bounded operational reference.

The restored inventory profile initially reports its absent source as unavailable.
Reconnecting a disposable HTTPS source must produce fresh source and receipt
timestamps, preserve read scope and unknown values, and grant no engine rights.
Timeouts and refused redirects retain the last snapshot as history; reconnection
restores current data. Revoking the restored reader must immediately deny access.
These checks use synthetic data and do not qualify a live source or its authority.

A controlled synthetic chain completed 14,436.558 seconds of load, 292 cycles and
7,300 test admissions, followed by 653.346 seconds of quiescence and recreation
recovery. The workload used four workers, queue capacity 24 and container limits
of 512 MiB, 2 CPUs and 128 PIDs. Those results describe that bounded workload;
they do not establish general leak absence, maximum throughput or long-term
production stability.

Remote account authority, additional account storage adapters, inventory catalog
editing and global cross-section search remain future work. High availability
and hot backup are outside the initial stable-release scope.

The 1.0 support matrix names the qualified versioned configuration, API and storage
contracts. Unsupported formats are rejected explicitly; stored data is not silently
converted or deleted. Prototype compatibility and arbitrary component combinations
are not promised.
