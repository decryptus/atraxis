# Complete observation capacity experiment

The complete observation adapters preserve the native projection within their
bounds. A row-count bound alone does not describe their usable capacity: the
Prometheus query repeats field labels in its value and scrape-clock rows, and can
reach Atraxis's configured response-byte limit first. This experiment measures
that expansion and checks refusal/recovery through the actual chain.

## Reproduce

Use Linux, the project environment and the pinned dependencies/checkouts in
`.github/workflows/prometheus.yml`. Set `COVENANT_SOURCE`, `CERTLORD_SOURCE` and
`PROMETHEUS_BINARY` as in the [correspondence suite](source-correspondence.md).

```sh
python scripts/check-observation-capacity.py --output observation-capacity.json
```

The default runs four source generations for each of eight scenarios. CI uses
two generations and uploads the JSON report; `--smoke` checks only one resource
per adapter with two generations. The report records dependency revisions,
Python/Prometheus versions and hashes of the harness files. A script failure
leaves completed scenarios in the report and exits unsuccessfully.

Each scenario uses two fresh Prometheus processes and empty disposable storage,
the actual Covenant observation HTTP plugin, authenticated synthetic loopback
JSON and Atraxis's actual bounded HTTP transport/parser. There are no configurable
external destinations. Source calls must remain GET-only at the fixed observation
route. Existing correspondence tests separately cover actual native producers.

Scrapes run every two seconds with a 1.5-second timeout. Atraxis uses a five-second
request timeout, 600-second freshness budget, 1,000-resource ceiling and the same
4 MiB response budget as the documented configuration. Freshness is deliberately
generous here; the correspondence suite covers stale observations. The diagnostic
query reader has a separate 8 MiB ceiling so it can measure a response which the
real Atraxis transport must reject at 4 MiB. It never supplies that larger response
to the inventory service.

## Fixture and assertions

Monit containers have one filesystem and distinct 64-character IDs/names. Missing
optional scalars remain null and `actions` is null. CertLord certificates have
distinct UUIDs, two domains, fingerprints and a populated verification object.
These are illustrative shapes, not universal resource-count limits.

The script discovers each shape's codec row boundary, then exercises 1, 100, 200
and that boundary count. On every generation, both Prometheus instances must
return the new manifest digest. Under-budget observations must match **all** native
resource IDs/fields and source metadata. Over-budget HTTP reads must return
`response_too_large` and leave the inventory unready. The instant vector must keep
the same row count while TSDB head series increase across changed generations.

At each codec boundary, one additional resource must make Covenant emit only a
failed observation; Atraxis must reject it. A subsequent one-resource snapshot
must recover and match the direct projection. This checks both transport limits
without accepting truncated inventories or silently raising consumer budgets.

## Local results — 31 October 2026

The eight-scenario run used Python 3.12 and Prometheus 3.5.0, Atraxis base
`fa93f834f09cbf2e7fbd477938892d09cdd23611`, Covenant
`331e56367ef92ff24a5f3931b650d9301a298bed` and CertLord
`908834f3d3b89d579a908391ce435cc5b419451f`. Four source generations were checked
on both replicas (64 reads), plus codec overflow and recovery for both kinds.
MiB values below are rounded; the configured consumer ceiling is exactly
4,194,304 bytes.
The [raw JSON report](evidence/observation-capacity-2026-10-31.json) retains every
sample and the harness hashes used for this run.

| Fixture | Resources | Typed rows | Export MiB | Query MiB | Atraxis result |
| --- | ---: | ---: | ---: | ---: | --- |
| Monit | 1 | 56 | 0.01 | 0.03 | Native projection equal |
| Monit | 100 | 4,313 | 0.42 | 2.08 | Native projection equal |
| Monit | 200 | 8,613 | 0.85 | 4.16 | `response_too_large` |
| Monit | 232 | 9,989 | 0.99 | 4.82 | `response_too_large` |
| CertLord | 1 | 31 | <0.01 | 0.02 | Native projection equal |
| CertLord | 100 | 2,506 | 0.31 | 1.36 | Native projection equal |
| CertLord | 200 | 5,006 | 0.62 | 2.72 | Native projection equal |
| CertLord | 399 | 9,981 | 1.23 | 5.43 | `response_too_large` |

For these shapes, codec rows are `13 + 43 × containers` and
`6 + 25 × certificates`. Resource 233 or 400 respectively exceeds 10,000 rows;
the actual Covenant exporter refuses those snapshots. These codec maxima are
already beyond the consumer's response budget. The experiment brackets successful
sizes; it does not search for or promise an exact maximum usable resource count.

Head series increased by one per Monit generation at every tested size. For
CertLord, the increases were 5, 203, 403 and 801 series per generation for 1, 100,
200 and 399 certificates respectively. The query vector size stayed constant for
each scenario: historical label generations were retained in the TSDB but did
not leak into the current inventory. These figures describe the explicit timestamp
update pattern below, not every possible CertLord deployment.

For accepted inventories, median fetch-and-projection times ranged from 0.106 to
0.491 seconds across scenarios. After the initial startup generation, measured
update-to-read times ranged from 1.34 to 2.17 seconds across all scenarios;
initial reads included startup and took up to 7.91 seconds. Sampled Prometheus RSS
ranged from 77.0 to 172.0 MiB per process. These are local diagnostics, subject to
the measurement limitations below, not deployment sizing recommendations.

## Interpretation and limits

`query_bytes` measures the actual uncompressed Prometheus JSON body. Exposition
bytes include Covenant's availability metric. Source bytes describe the synthetic
JSON, not a production payload distribution. TSDB `headStats.numSeries` is read
from the [Prometheus status API](https://prometheus.io/docs/prometheus/latest/querying/api/#tsdb-stats);
it includes the scrape's auxiliary series and retained label generations.

Each generation changes Monit counters/CPU values and numeric cycle timestamps.
For CertLord it changes collection start/completion, each certificate's observation
timestamp and each verification timestamp. Other certificate dates and fingerprints
stay fixed. This intentionally exposes string-timestamp churn without simulating
renewal or resource deletion. Updating numeric values still changes the manifest
digest, creating a series even for Monit.

`fetch_and_projection_seconds` measures the real bounded fetch plus inventory
projection. `update_to_read_seconds` also includes diagnostic polling, exporter
measurement and sequential reads of both instances; it is an observed upper bound
for this harness, not an independent per-replica latency or an operator SLO.
RSS is sampled from each Prometheus process after reads; it is neither peak memory
nor memory attributable only to these series. Timings and RSS are diagnostics,
with no machine-dependent performance pass threshold.

These short runs do not qualify production capacity, long-term retention/disk
cost, compaction, leaks, TLS/network delay or fleet-wide concurrency. An exploratory
one-second scrape/800 ms timeout run had intermittent scrape timeouts on this host;
the recorded configuration uses two seconds/1.5 seconds. Size scrape deadlines
against the actual exporter and inventory. Instant queries stop old generations
from reaching the inventory, but do not remove them from Prometheus storage.
