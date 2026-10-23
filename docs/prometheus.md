# Read Covenant metrics through Prometheus

Atraxis can display the latest samples of explicitly selected Prometheus metrics.
Covenant collects and transforms source data, Prometheus scrapes and stores the
exported metrics, and Atraxis reads Prometheus. Covenant remains independently
configured and operational. Atraxis does not change its fallback behavior.

This experimental adapter supports finite numeric samples in the inventory API,
web inventory and inventory search, including CLI and Textual search. It does not
provide time-series graphs, arbitrary PromQL, histogram objects, alert management
or metric-driven interventions. Atropos observation bindings do not accept these
metric resources as live intervention facts.

## Configure the chain

First configure Covenant's source and exported metric, for example using its
[orders quickstart](https://github.com/decryptus/covenant/tree/master/examples/quickstart).
Check its exposition before adding a Prometheus scrape job. For an installation
with the `orders` endpoint, the job has this shape:

```yaml
scrape_configs:
  - job_name: covenant-orders
    scrape_interval: 30s
    scrape_timeout: 10s
    scheme: https
    metrics_path: /metrics/orders
    static_configs:
      - targets: [covenant.example.org]
```

Adapt the target, TLS trust and authentication to your Covenant installation.
The hostname is an example, not a public service. Verify that Prometheus contains
`orders_pending{job="covenant-orders"}` before configuring Atraxis.

Add this entry to the daemon's `sources` list:

```yaml
sources:
  - source_id: covenant-orders
    kind: prometheus
    origin: https://prometheus.example.org
    credentials_file: credentials/prometheus.json
    metric:
      name: orders_pending
      match:
        job: covenant-orders
      labels: [job, instance]
    poll_interval: 60
    timeout: 10
    max_age: 180
    max_resources: 100
```

Use a read-only Basic-authenticated Prometheus endpoint or protected ingress.
Credentials, verified TLS, explicit CA bundles and transport deadlines follow the
[source configuration contract](source-configuration.md). Keep source configuration
and credential files private. `atraxis check-config` validates them offline;
restart the daemon after a reviewed configuration change, then inspect `/ui`.
The `setup` command prepares monit-docker/CertLord starters; add the Prometheus
selection in YAML. A browser configuration administrator can select a connection
whose complete `metric` block was approved locally in the connection profile.

Each source selects one metric name plus at least one exact, nonempty label match.
There is no free-form query input. `labels` is the explicit list of labels that
Atraxis may expose; it must include the matcher keys. Other labels are discarded.
Select enough labels to identify each series: two series differing only in a
discarded label make the response ambiguous and are rejected, not merged.

A metric resource's ID depends on its metric name and selected label names/values.
Changes to those labels change its ID; value and timestamp changes do not.
Resource grants use `kind: metric`, the configured source ID and the `native_id`
returned by the API. Existing source, server and resource-group grants also apply.
Search with `atraxis search inventory --kind metric`; searchable text includes
metric names and selected label values. Metrics have no inferred workload status.

## Understand the timestamp and retained values

Atraxis reads samples within the `max_age` window and displays the latest sample
for each returned series. `sampled_at` is its Prometheus sample timestamp, not the
query time and not necessarily the time when the original source measured data.
For ordinary Covenant exposition, it reflects the Prometheus scrape.

For example, if Covenant keeps a previous value of `12` after rejecting a new
value, Prometheus can scrape `12` again with a new timestamp. Atraxis displays
that sample. It must not claim that the source measured `12` again or is healthy.
This is compatible with deliberately configured last-known-value behavior.

Covenant's `on_fail`, `on_noresult` and `default` remain Covenant configuration.
They are not overridden by Atraxis. Failed collection, missing filter result and
a value rejected by a metric validator are different cases. `default` also has
different handling in labeled and unlabeled collectors. Validate your chosen
filter/fallback combination against the data you expect; do not treat every
missing measurement as zero. A separate availability metric such as
`orders_source_up` describes request success, not validation of every field.

A source is current only while collection runs and its accepted latest samples
are within `max_age`. Its displayed age is the oldest of those latest samples.
Series can remain visible within the window after Prometheus stops receiving
them. Neither their presence nor a current source proves upstream availability.
If origin-data freshness matters, export a separate last-successful-observation
timestamp or validity metric with explicitly defined semantics.

On request failure, invalid/partial response or unknown freshness, Atraxis retains
the previous valid snapshot as historical. An empty query result means unknown,
not zero and not an authoritative empty inventory. Recovery replaces the retained
snapshot only after a valid, current result. No time-series history is stored in
Atraxis; it remains in Prometheus.

## Bounds and troubleshooting

The adapter issues only GET requests to `/api/v1/query`, using an exact range
selector and the configured timeout. It accepts a matrix of float samples;
native histograms, non-finite values, warnings or informational annotations are
rejected. Source response-byte and resource limits remain enforced.

There are at most 16 selected labels and 16 matchers, 256 characters per label
value, 1,000 samples per series and 10,000 samples per response. The metric name
uses the classic ASCII Prometheus grammar, up to 128 characters. A query over its
8 KiB encoded budget is rejected. `max_resources` counts returned series; exceeding
it fails the whole response. Select a narrower scope or shorten `max_age` when a
budget is exceeded. These are acceptance limits, not performance guarantees.

If the source is unavailable, check its credentials, TLS and configured origin.
If no current snapshot appears, check the metric name, exact labels, scrape state,
sample age and response bounds directly in Prometheus. Private response bodies
and exception text are not exposed in Atraxis errors.
