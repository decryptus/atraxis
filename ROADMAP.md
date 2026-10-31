# Atraxis roadmap

Updated 31 October 2026. Atraxis 1.0 has a bounded reference configuration.
This roadmap distinguishes implemented behavior from later work and broader
qualification. Future priorities may change; no future release date is promised.

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
what was tested and what remains outside this release. Package publication stays
a separate operation on the reviewed sources and exact CI artifacts.

The operational reference uses a current direct Monit observation, one approved
non-destructive HTTPS diagnostic through Auton, and the existing local Alertmanager
receiver. Actual firing/resolution and duplicate refusal are verified. This does
not enable automatic remediation or qualify email/SMS/chat or human receipt.
The public demonstration remains read-only.

The earlier four-hour synthetic SQLite/Redis campaign is retained at its original
inputs; it is not an integrated-queue or live throughput benchmark. Future scope
and broader operational promises require their own evidence.

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
