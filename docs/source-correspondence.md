# Direct and Prometheus source correspondence

This contributor qualification compares the nine Monit-Docker rule inputs with
metrics exported by Covenant and read through the Atraxis Prometheus adapter.
The valid fixture values correspond, but this does **not** establish that the
metric adapter can replace direct observations for live rules.

## Tested mapping

The synthetic exporter configuration is
[`correspondence_tests/covenant-monit.yml`](https://github.com/decryptus/atraxis/blob/main/correspondence_tests/covenant-monit.yml).
It selects exactly one container by full ID and one filesystem by group and path.
These are qualification metric names, not a production naming contract.

| Direct JSON field | Rule input / metric suffix after `parity_` | Preserved meaning |
| --- | --- | --- |
| `cpu_percent` | `cpu_percent` | Percent; 250 and 275.5 remain valid multi-core values |
| `mem_percent` | `memory_percent` | Percent; zero stays zero |
| `maintenance_active` | `maintenance` | Boolean encoded as 0/1, decoded explicitly |
| `health` | `health_status` | `healthy` or `unhealthy` label, with gauge value 1 |
| `restart_attempts` | `restart_attempts` | Nonnegative integer; no inferred time window or rate |
| `filesystems[].disk_percent` | `disk_percent` | Percent |
| `filesystems[].inode_percent` | `inode_percent` | Percent |
| `filesystems[].fs_readable` | `readable` | 0/1 converted to a boolean |
| `filesystems[].fs_writable` | `writable` | 0/1 converted to a boolean |

The fixture uses strict type checks and removes invalid or absent values. Tests
cover all nine fields, unknown health, real zero/false values, duplicate or wrong
identities, failure and recovery. The test-only decoder requires exactly one
series; it is not installed as a production observation adapter.

## Differences confirmed by real queries

1. **Source freshness differs from scrape freshness.** A fresh Prometheus sample
   can contain a value from an old source snapshot. The direct adapter rejects
   that snapshot while the metric adapter can report ready. The fixture exposes
   `parity_observed_at_seconds`, but Atraxis does not join it to other metrics.
2. **Removal is not immediately visible through a range selector.** After Covenant
   removes a missing CPU value, Prometheus still returns its previous sample
   until the configured age window expires. Recovery to an actual zero is tested.
3. **Readiness is a separate signal.** The source-ready gauge changes to zero
   while an older CPU sample remains queryable. A metric's ready status does not
   establish upstream functional readiness.
4. **Changing a label creates another series.** An unhealthy-to-healthy transition
   leaves both health label sets in the range. Picking the first resource would
   give an arbitrary result; production observation mapping needs an explicit
   current-state policy that rejects ambiguity.
5. **Identity is not interchangeable.** Container ID and filesystem labels are
   preserved, but the metric's hashed native ID is not the container ID. Two real
   Prometheus instances preserve separate Atraxis source IDs. The current live
   observation adapter rejects metric resources; it accepts direct container
   resources only.

These differences are asserted as current behavior. A passing qualification suite
does not mean the differences have been fixed. A future live mapping must bind
source, container and filesystem identity explicitly; validate source readiness
and origin freshness; and define coherent snapshot/series selection across the
independently queried metrics. It must reject missing, stale or ambiguous facts.

## Reproduction and limits

The dedicated Prometheus CI job installs the real Covenant collector at commit
`3b7e446e494847065ac9890e089da0fa75ff590f` and checksum-verified Prometheus 3.5.0.
It explicitly guards collection and runs all eight qualification tests:

```sh
export COVENANT_SOURCE=/absolute/path/to/pinned/covenant
export PROMETHEUS_BINARY=/absolute/path/to/prometheus
python scripts/check-test-collection.py --runner unittest correspondence_tests
python -m unittest discover -s correspondence_tests -v
```

Use the dependencies pinned by `.github/workflows/prometheus.yml`. The harness
starts two disposable Prometheus processes and a loopback exporter with synthetic
credentials. It exercises Covenant's real collector/JMESPath filter, Prometheus
scraping/query storage, and Atraxis's real HTTP transport, parser, service and
direct observation projection. The shared servers intentionally retain series
history across sequential test cases; no production endpoint is contacted.

The source JSON is synthetic. This is not acceptance of a deployed Monit-Docker
service, Covenant HTTP plugin/HTTPdis daemon, CertLord correspondence, remote TLS,
source-clock accuracy, capacity, or engine/action execution. The exporter fixture
does not establish a complete production configuration or atomic multi-metric
snapshots. No runtime adapter, live-rule binding or deployed configuration changes.
See the [initial Prometheus acceptance record](prometheus-acceptance.md) and
[user metric guide](prometheus.md) for the existing contracts.
