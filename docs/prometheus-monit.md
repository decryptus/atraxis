# Monit-Docker observations through Covenant

Use `kind: prometheus_monit` to read container observations through Covenant and
Prometheus. This adapter checks upstream readiness and observation time, selects
one coherent scrape, and preserves full Docker IDs and filesystem group/path
identities. It supplies the same nine supported rule inputs as the direct
Monit-Docker adapter. Additional container measurements remain unknown.

## Configure the exporter and scrape

The [Covenant endpoint example](https://github.com/decryptus/atraxis/blob/main/examples/covenant-monit.yml)
selects **one container and one filesystem**. Merge the `endpoints.monit` entry
into an existing Covenant configuration. Replace every example container ID
(the 64 `a` characters), the filesystem group/path, and the source URL. It is not
an inventory of every container on that host. Keep a distinct prefix and Atraxis
source identity for each selected scope; never combine different upstream sources
under the same prefix on one exporter target.

Provision the source's read credentials and verified TLS using Covenant's HTTP
plugin configuration. The example fixes GET, JSON decoding, a finite timeout and
no redirects. Restrict who can reach the exporter and source. Do not put passwords
in metric labels. The adapter does not configure or mutate Monit-Docker.

The filters explicitly remove failed/absent/invalid measurements. Keep these
policies: retaining an old value or substituting a default can make a fresh scrape
misrepresent an old fact. Missing is not zero. The endpoint must produce all values
and metadata from the same source response; Covenant's serial HTTP endpoint worker
collects that response and then renders the exposition. Independently updated
metrics or recording rules do not satisfy this contract.

For example, configure Prometheus to scrape the endpoint:

```yaml
scrape_configs:
  - job_name: covenant-monit
    scrape_interval: 15s
    scrape_timeout: 10s
    scheme: https
    metrics_path: /metrics/monit
    honor_timestamps: false
    static_configs:
      - targets: [covenant.example.org:443]
```

Supply the appropriate TLS and read authentication settings for your installation.
`honor_timestamps: false` gives every exported series the scrape's timestamp, as
used by Prometheus's own `up` series. Do not rename `job`/`instance` differently
between `up` and the exported metrics. Origin observation time remains a metric
value; it is not replaced by the scrape time.

## Configure Atraxis

```yaml
sources:
  - source_id: selected-container-via-covenant
    kind: prometheus_monit
    origin: https://prometheus.example.org
    credentials_file: credentials/prometheus.json
    metric:
      prefix: parity_
      match:
        job: covenant-monit
        instance: covenant.example.org:443
    timeout: 5
    poll_interval: 15
    max_age: 60
    max_clock_skew: 2
    max_resources: 10
```

The prefix matches the supplied exporter example; change all corresponding names
together if you choose another prefix. Both `job` and `instance` must be exact,
nonempty matches for one scrape target. Extra exact target labels are allowed;
resource labels (`id`, `name`, `status`, `health`, `group`, `path`) cannot be target
matchers. A host-approved browser connection profile can contain this complete
`metric` block; operators cannot override the contract in a source-add request.

Use `atraxis check-config` before restarting the daemon. Then check the inventory
for current container observations. Container grants and searches use `kind:
container`, the configured Atraxis source ID, and the full Docker ID. Switching
from a direct source requires an explicit source/binding/grant configuration
change; no existing rule is rebound, activated or dispatched by this adapter.

## Required metric contract

With the configured prefix, expose these gauges in one endpoint response:

| Suffix | Value and required resource labels |
| --- | --- |
| `source_ready` | 1 only when the source reports ready and its last collection succeeded; otherwise 0 or absent |
| `observed_at_seconds` | Source's last successful observation time, Unix seconds |
| `containers` | Number of containers in the explicitly selected scope |
| `container_info` | 1 with full `id`, `name`, `status` |
| `cpu_percent`, `memory_percent` | Nonnegative percent with `id`; CPU can exceed 100 |
| `maintenance` | 0/1 with `id`, converted to a boolean |
| `restart_attempts` | Nonnegative integer with `id`; not a rate or inferred restart window |
| `health_status` | 1 with `id`, `health` equal to `healthy` or `unhealthy` |
| `disk_percent`, `inode_percent` | Nonnegative percent with `id`, `group`, `path` |
| `readable`, `writable` | 0/1 with `id`, `group`, `path`, converted to booleans for rules |

Prometheus must additionally return its own `up` for that exact target. The three
source metadata gauges and `up` must each identify exactly one series. Duplicate
container/field/filesystem identities in the selected scrape are rejected. A
current zero container count with matching metadata establishes an empty selected
scope; a missing query or failed scrape does not.

## Freshness, disappearance and failures

Atraxis makes one bounded matrix query for the fixed metric names. The latest
`up` sample defines the scrape. It requires `up=1`, `source_ready=1`, and matching
scrape timestamps for the source observation time and container count. Only
resource samples at that same timestamp are eligible. Old health labels and
removed measurements from earlier scrapes are excluded even while Prometheus
retains their history.

Age is the older of source-observation age and scrape age, and then advances with
Atraxis's monotonic clock. Both must fit `max_age`. Excessive future timestamps,
missing metadata, ambiguous identities or partial/warning responses fail closed.
Keep source, Prometheus and Atraxis clocks synchronized; this contract cannot
prove a source timestamp is truthful.

A missing field becomes unknown and cannot supply a rule fact. Other valid fields
remain usable, as with a direct source. A failed/unready source retains its previous
snapshot as historical only. Recovery requires valid fresh metadata and data.
Changes become observable after the next Prometheus scrape and Atraxis poll; this
is not an instantaneous failure detector.

Limits include the configured response-byte bound, at most `max_resources`
containers and separately filesystems, 1,000 samples per series and 10,000 samples
per response. Returned series (including old label generations) are bounded by
`min(10000, 20 * max_resources + 4)`. Excess or ambiguity fails the response rather
than truncating it. Prefixes use the classic metric-name grammar, at most 80
characters; up to 12 exact target matchers and 256 characters per label value.
The encoded query must fit 8 KiB. Large scopes/history may need narrower selection.

The general `kind: prometheus` [metric adapter](prometheus.md) keeps its existing
sample-display semantics and cannot supply container rule facts. CertLord requires
its own correspondence contract and is not covered by this adapter.
