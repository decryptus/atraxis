# Development and validation

Read [engineering requirements](https://github.com/decryptus/atraxis/blob/main/AGENTS.md)
before changing the project. Atraxis owns inventory and operator interfaces;
Atropos owns rule intelligence, persistence and guarded workflow execution.
Atraxis uses the separately packaged API client and must not import the engine.

For installation and operation, use the [user guide](../index.rst).
The [public roadmap](../ROADMAP.md) distinguishes existing behavior from planned scope.
The [1.0 delivery baseline](v1-delivery.md) fixes included/deferred functionality,
the four exit gates and when existing evidence needs to be repeated.

## Local development

Use Python 3.11 or 3.12, the system `libmagic` library and a fresh environment:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install .
python scripts/check-test-collection.py --runner unittest tests
python -m unittest discover -s tests -v
```

Install optional UI/client extras only for the relevant suites. Rules integration
uses the matching client from the public Atropos source checkout. GitHub workflows
pin the integrated service inputs and install them into separate environments.

## Reproducible checks

- Core: collection guard and unittest on Python 3.11/3.12.
- Installed package: build sdist and wheel, install the wheel in a fresh environment,
  check dependencies, and exercise CLI and SQLite recovery outside the checkout.
- Documentation: `python -m sphinx -n -W --keep-going -b html . docs/_build/html`.
- Textual and web: synthetic terminal/browser tests and interface captures.
- Network and container chain: real HTTPS, native services, scoped dispatch,
  disposable synthetic effects and failure injection.
- Browser/engine: native forms with separately installed Atropos, Redis and Auton.
- Operator Compose: scoped access, credential/TLS renewal, persistent state,
  worker profiles, recreation and cold restoration.

The source pins and Auton fixture patch are recorded in
[acceptance inputs](../scripts/chain/public-inputs.json). The patch bounds OIDC
transport work for the pinned upstream fixture; it is not a claim about an
upstream package release. Every integrated run records its exact source provenance.

## Installed package evidence

The package workflow builds the source archive and its wheel on Python 3.11 and
3.12. It checks out the exact PR head (or pushed commit), installs the single
wheel into a fresh base environment and runs the collection guard and existing
installed tests outside the source checkout. Dependency checks and piped test
commands must succeed; retaining a log never turns a failed command into a pass.

Each `installed-package-<python>` artifact is retained for 14 days and contains:

- The wheel and source archive actually built by that job.
- `package-evidence/manifest.json`, recording source commit/tree, Python and package
  versions, plus each archive's SHA-256 and size.
- The build frontend environment and installed base environment from
  `pip freeze --all`, including bootstrap tools.
- Dependency-check, collection and installed-test logs.

Artifacts are also retained on failure when files exist. A manifest identifies
files; it does not claim the job passed. Verify the run/job outcome, collection
and test result before treating an artifact as qualified. Keep the exact archive
hash associated with the installed wheel reference in the dependency inventory.
The build frontend inventory does not capture every isolated build dependency,
system library or base-image byte, and is not a reproducible-build guarantee.
These base-package checks do not qualify every optional UI/client profile or
operational recovery. No registry release or deployment is performed.

## Operator access acceptance

The `operator-compose.yml` workflow builds separately installed services and runs
the collection guard followed by the single integrated `compose_tests` case.
`operator_access.py` uses the actual operator guide's YAML access example and an
authenticated, verified-HTTPS source on the disposable project's network. It
checks that the reader sees only the granted source, cannot change organization
or read engine rules, and receives no administrative delegation. Live account
revocation denies the next inventory read; reinitializing an existing account
store must fail without changing its directory or audit.

The same case rotates a login with planned service recreation, replaces the
engine token without changing service container identities, and renews both web
and engine TLS chains. Old passwords, tokens and CA trust must fail; the current
credentials must still read the same rule. Inventory account authority remains
separate from engine account authority; inventory revocation is not claimed to
revoke engine rights. The token check establishes no service restart, not that
an existing long-lived client object reloads its token file.

The temporary inventory configuration is removed before the existing worker
drill. After that drill, `inventory_recovery.py` adds a separate, scoped inventory
reader while keeping the original reader revoked. It leaves the new profile,
source credentials and renewed CA in the cold backup. Restoration into separate
empty volumes checks the preserved directory/audit, engine-account revocation,
rotated credentials/TLS, rule history and integrity. The source fixture is absent
at startup, so its restored configuration must report unavailable with no cached
snapshot presented as a new observation.

Inspect `OPERATOR_ACCESS_REPORT`, `COMPOSE_WORKER_REPORT`, `COMPOSE_REPORT` and
the recorded source pins. These are test-owned containers, synthetic credentials,
certificate authorities and read-only source requests. There is no production
probe, automated certificate issuer, live target or external notification channel.
Real site provisioning, authority mounts, issuer renewal and host-loss recovery
remain separate operational qualification.

## Recovery on a separate host

The operator workflow now transfers its disposable cold backup to a second GitHub
runner. The destination must have a different kernel boot ID and DMI VM UUID,
no source volumes and none of the three application/proxy image tags. It loads
the exact exported images without rebuilding or pulling, and restores both data
volumes and the private site files into new destinations. The source job must
first pass its existing acceptance, verify installed package bytes and remove its
two disposable Compose projects and volumes.

The Docker daemon ID is retained as diagnostic metadata: separate hosted VMs have
been observed with the same persisted ID. It is not a unique host-identity gate.
The destination still rejects the source VM/boot and any pre-existing source image
tags or data volumes before image import.

A manifest digest passed through the workflow job output binds all five transfer
files, source revision, image IDs and archived fingerprints. Verification rejects
missing/extra files, links, oversized or changed inputs, a different source
revision and reuse of the original host. Restore checks complete file contents,
ownership and modes before application startup, and refuses occupied volumes.
The separate test checks TLS authentication, rotated login/token refusal, retained
account revisions/audits, revoked membership, rule/history/audit equality, terminal
work outcomes, SQLite integrity and the existing resource protections.

One additional synthetic `core.echo` admission remains queued with both execution
namespaces paused at backup time. Restoration preserves that work and pause. A
started worker must leave it unchanged. The test operator then stops that worker,
revokes its action grant, explicitly confirms reconciliation and resumes; the
worker must cancel the same run without an effect. This is a controlled recovery
test, never an automatic production resume or a new external action.

The source also admits a separate two-step synthetic notification workflow through
the installed client/API. Its registered Alertmanager transport sends one request
to a token-protected loopback HTTP fixture, which durably records acceptance and
closes the connection without a response. The guarded worker must retain an
uncertain outcome, its target lock and no action result; the later step must not
run. This is a disposable receipt fixture, not a live Alertmanager deployment.

After restoration, exact state, lock, audit and receipt equality are checked.
Workers are polled with the receipt fixture available, both before and after the
explicit execution resume. The fixture deliberately counts every POST without
deduplication, so a replay cannot be hidden by its receiver. The separate
reconciliation right must be present at confirmation, and a stale state digest
must be refused. Successful explicit attestation records the evidence and actor,
releases the lock atomically and preserves original history, attempts and empty
results. Neither reconciliation nor a repeated event admission executes the
uncertain action or its later step. The result artifact includes these checks
under `uncertainty` and sets `uncertain_work_exercised=true`.

The synthetic receipt ledger is archived with the engine volume and its fixture
is recreated on the destination. This checks recovery and replay prevention; it
does not independently establish the survival or current state of a real external
recipient. No live notification or intervention is issued by this test.

The active inventory profile is then exercised on the destination against its
own authenticated HTTPS fixture, started from the archived fixture files and the
same exported web image. Source and receipt timestamps must advance beyond the
pre-backup observation. The reader sees only its granted source, native units
and unknown values; wrong organizations, the original revoked reader and engine
rule access remain denied. A source timeout makes readiness fail while preserving
the last observation as history. Redirects are refused without replacing that
snapshot. Reconnection produces a new current observation. Finally, revoking the
restored reader through the reviewed account command denies status, readiness and
UI access immediately while preserving previous audit entries. Source logs must
contain only authenticated GET requests. Inspect the result artifact's `inventory`
checks; `live_source_qualified` remains false.

Run the collection guard before `unittest discover -s recovery_tests -v`; the case
requires the source job's verified transfer and digest and never silently skips.
Inspect `RECOVERY_SOURCE_RELEASED`, `HOST_RECOVERY_REPORT` and
`operator-host-recovery-evidence`. The one-day transfer artifact contains only
disposable test credentials, keys and databases; it is separate from the retained
credential-free result artifact and must never receive live site data.

This qualifies migration of the tested SQLite/storage-queue fixture across two
independent CI hosts, including an uncertain synthetic HTTP receipt and an active
synthetic inventory profile. It does not qualify reconciliation against a surviving
real external system, an installed live inventory source, newer revocations held by an external identity
authority, a site certificate issuer, live Auton/notification destinations or
recovery of the deployed pilot. Those operational gates remain open.

## Local administration CLI validation

Core tests exercise UID/web identity separation, CLI-only configuration without
web routes, offline source add/update/remove, bounded private requests, scoped
grant add/remove, revocation and conflicts across check/apply. The review digest
also binds target paths and the account directory, including an approved-connection
change while the daemon configuration is unchanged. Post-replacement sync failure
must return an unconfirmed outcome without retrying an already saved change.

Installed-package acceptance runs source onboarding and inventory grant/revoke
through the built wheel's CLI outside the checkout, without Textual or network
sources. This establishes the offline operator path, not source connectivity,
service restart behavior or a live installation's authority configuration.

## Endurance scope

The manual four-hour workflow retains the complete short gate, at least 14,400
seconds of real load, at least 600 seconds of quiescence and ten observations
without new admissions, followed by recreation and recovery verification.
Containers retain 512 MiB, 2 CPUs and 128 PIDs; the harness retains four workers
and capacity 24. Check `LOAD_REPORT`, the complete phase, admitted/completed work,
synthetic effects, SQLite integrity/foreign keys, Redis queues and exact histories.
Preserve logs, JSONL measurements and installed-package/source provenance.

Report collected/executed/skipped counts and actual load/quiescence durations.
Inspect anonymous/file/current/peak memory by quartile and during quiescence.
Stability over one measured interval does not prove the absence of leaks in
general; memory failing to decrease alone does not prove a leak. Do not lower
assertions, deadlines or resource limits to obtain a passing run.

Tests use synthetic endpoints and credentials. Never point them at production
by default. No workflow in this repository publishes packages or deploys services.
