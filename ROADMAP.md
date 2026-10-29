# Atraxis roadmap

Updated 29 October 2026. Atraxis is an experimental coordination application.
This roadmap distinguishes implemented behavior from work still required for a
stable release. Priorities may change; no release date is promised.

## Available today

- Read-only HTTP collection and an authenticated inventory API and web interface,
  with explicit current, historical and unavailable observations.
- Selected numeric Prometheus samples, including Covenant exports, with explicit
  label projection, sample timestamps, read grants and inventory search. This
  experimental adapter does not supply metric-driven interventions or graphs.
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

## Four steps to the first stable release

The [1.0 scope and delivery gates](docs/v1-delivery.md) fix the functional baseline
to implemented capabilities, define concrete exit criteria and separate later
features. Existing qualification is reused within its proven scope.

1. **G1 — scope and contracts:** record the exact supported configurations and
   matching contracts. The functional baseline is documented; the explicit
   intervention-language decision and candidate support manifest remain open.
2. **G2 — installation and complete recovery:** qualify a fresh operational site
   and recovery on another host, including identity, certificates, configuration,
   persistent state, audit, revocation and uncertain work.
3. **G3 — final integration and endurance:** qualify installed sources, approved
   Auton targets, actual notification destinations and representative retained-history
   workloads for the configurations claimed as supported.
4. **G4 — release candidate:** finalize packages, documentation and the evidence
   index after the preceding gates pass. Publication is a separate action.

G2–G4 remain open. New features do not automatically join this baseline; relevant
defects are corrected without reopening unrelated completed checks. No new campaign
or external notification is authorized merely by this roadmap.

A restricted read-only pilot is running. It does not qualify live intervention
execution, downstream notifications or general production readiness.

The public demonstration includes Covenant metrics and a separate Prometheus →
Alertmanager path with a local webhook receiver. A bounded synthetic signal checks
actual firing and resolved delivery without acting on supervised services. Local
receiver acceptance does not qualify email/SMS/chat delivery or human receipt.

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
an attestation and releases the lock without executing later steps. This advances
recovery qualification for SQLite with the storage-backed queue. Recovery of a
live site, current external authority, installed live inventory sources and reconciliation
against surviving real external systems remain open; see the
[separate-host scope](docs/contributing.md#recovery-on-a-separate-host).

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

Current configuration, API and storage formats are experimental. Unsupported
formats must be rejected explicitly; stored data must not be silently converted
or deleted.
