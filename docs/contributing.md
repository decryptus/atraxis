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
- Operator Compose: persistent state, worker profiles, recreation and cold restoration.

The source pins and Auton fixture patch are recorded in
[acceptance inputs](../scripts/chain/public-inputs.json). The patch bounds OIDC
transport work for the pinned upstream fixture; it is not a claim about an
upstream package release. Every integrated run records its exact source provenance.

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
