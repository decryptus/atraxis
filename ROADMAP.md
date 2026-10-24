# Atraxis roadmap

Updated 25 October 2026. Atraxis is an experimental coordination application.
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
- Rule authoring, scenario tests, explicit activation and rollback, intervention
  inspection and uncertainty reconciliation through the independent Atropos API
  client. Atropos owns the engine and authoritative rule state.
- A single-host Docker Compose installation with persistent storage, verified TLS
  and optional, explicitly selected worker profiles.

Collection never authorizes an intervention. Saving or testing a rule does not
activate it. Execution requires an approved binding and current permissions.

## Next milestones

1. Complete the operator installation journey, including credential and authority
   provisioning, renewal and recovery documentation.
2. Qualify installed sources, explicitly approved Auton targets and downstream
   notifications in a controlled operational environment.
3. Complete a live cold-backup/restore and host-loss drill while retaining audit,
   uncertain outcomes and deduplication evidence.
4. Extend sustained-operation and retained-history measurements before publishing
   capacity guidance.
5. Finalize supported contracts, packaging and distribution, then qualify a release
   candidate against the documented acceptance gates.

A restricted read-only pilot is running. It does not qualify live intervention
execution, downstream notifications or general production readiness.

The public demonstration includes Covenant metrics and a separate Prometheus →
Alertmanager path with a local webhook receiver. A bounded synthetic signal checks
actual firing and resolved delivery without acting on supervised services. Local
receiver acceptance does not qualify email/SMS/chat delivery or human receipt.

## Qualification and limits

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
