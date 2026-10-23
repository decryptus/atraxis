# Prometheus adapter review

This contributor record describes the initial read-only Covenant/Prometheus
integration. User configuration and failure semantics are in the separate
[metric guide](prometheus.md).

## Scope and contracts

- Pure metric selection/projection is separate from HTTP collection and interfaces.
- The existing bounded GET transport supplies explicit credentials, TLS validation,
  response limits, deadline and cancellation handling.
- An exact range selector preserves sample timestamps. Only the latest finite
  numeric value per series is projected; no history, query editor or alert API is
  added. Prometheus owns stored history and alert evaluation.
- Selected labels define public identity and visibility. Ambiguous projections
  fail; private labels and raw errors are not forwarded.
- Failure, stale data and empty queries preserve historical inventory. Current
  samples do not establish origin-data freshness or upstream health.
- Metric resources use the existing read-grant and search projection. They cannot
  become live Atropos intervention facts through the observation adapter.

## Covenant review

Covenant was inspected at commit
[`3b7e446e494847065ac9890e089da0fa75ff590f`](https://github.com/decryptus/covenant/commit/3b7e446e494847065ac9890e089da0fa75ff590f).
Its existing suite collected and executed 77 tests, with no skips, in an isolated
Python 3.12 environment. No Covenant source or configuration change is included.

Reproduction with its real collector and JMESPath filter confirmed that a gauge
can retain its previous value when the metric validator rejects an object. That
observation alone does not establish a bug: fallback/retention semantics belong
to the exporter configuration. `on_fail` and `on_noresult` handle different paths;
`default` has different handling in labeled and unlabeled collectors. The adapter
preserves those semantics instead of imposing a global removal policy.

## Validation and limits

`tests/test_prometheus.py` exercises parsing, selected-label identity, actual
sample timestamps, signed values, limits, ambiguous/partial results, source
isolation, history/recovery, read scopes, search, client validation, web escaping
and real loopback GET transport. Configuration-administration tests cover the
host-approved metric selection. The normal unittest collection guard includes
these tests; no test root is silently skipped.

Local validation collected and executed 439 unittest cases with no skips.
`scripts/check_prometheus_chain.py` additionally runs a real Prometheus 3.5.0
server against a loopback fixture using Covenant's actual collector and JMESPath
filter. It verifies retained `12` with a later sample timestamp, configured `-1`
fallback, and recovery to `9`, through Atraxis's real HTTP transport and service.
The Prometheus archive is checksum-pinned in its dedicated CI job. This fixture
does not run Covenant's HTTP plugin or HTTPdis daemon and does not contact a
deployed source. Covenant's separate suite covers its runtime paths.

The browser acceptance fixture includes a metric source produced by the real
parser/service, alongside container and certificate sources, at desktop/mobile
widths. It remains synthetic HTTPdis/Chromium acceptance, not evidence from a
deployed Covenant/Prometheus installation. Documentation builds treat warnings as
errors. Check the PR's recorded test results and CI before merging.

The first adapter does not establish deployed source compatibility, metric-driven
intervention safety, capacity, upstream validity, or source-clock accuracy.
