# Development and validation

Read [engineering requirements](https://github.com/decryptus/atraxis/blob/main/AGENTS.md)
before changing the project. Atraxis owns inventory and operator interfaces;
Atropos owns rule intelligence, persistence and guarded workflow execution.
Atraxis uses the separately packaged API client and must not import the engine.

For installation and operation, use the [user guide](../index.rst).
The [public roadmap](../ROADMAP.md) distinguishes existing behavior from planned scope.

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
drill. The subsequent cold restoration into separate empty volumes checks the
preserved reader directory/audit, existing engine-account revocation, rotated
credentials/TLS, rule history and integrity. It does not establish continued
activation of the temporary reader profile after restoration.

Inspect `OPERATOR_ACCESS_REPORT`, `COMPOSE_WORKER_REPORT`, `COMPOSE_REPORT` and
the recorded source pins. These are test-owned containers, synthetic credentials,
certificate authorities and read-only source requests. There is no production
probe, automated certificate issuer, live target or external notification channel.
Real site provisioning, authority mounts, issuer renewal and host-loss recovery
remain separate operational qualification.

## Recovery on a separate host

The operator workflow now transfers its disposable cold backup to a second GitHub
runner. The destination must have a different kernel boot ID and Docker daemon ID,
no source volumes and none of the three application/proxy image tags. It loads
the exact exported images without rebuilding or pulling, and restores both data
volumes and the private site files into new destinations. The source job must
first pass its existing acceptance, verify installed package bytes and remove its
two disposable Compose projects and volumes.

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

Run the collection guard before `unittest discover -s recovery_tests -v`; the case
requires the source job's verified transfer and digest and never silently skips.
Inspect `RECOVERY_SOURCE_RELEASED`, `HOST_RECOVERY_REPORT` and
`operator-host-recovery-evidence`. The one-day transfer artifact contains only
disposable test credentials, keys and databases; it is separate from the retained
credential-free result artifact and must never receive live site data.

This qualifies migration of the tested SQLite/storage-queue fixture across two
independent CI hosts. It does not exercise an uncertain external action, an active
inventory source after restoration, newer revocations held by an external identity
authority, a site certificate issuer, live Auton/notification destinations or
recovery of the deployed pilot. Those operational gates remain open.

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
