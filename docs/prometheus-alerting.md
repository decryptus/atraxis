# Notify from Covenant metrics with Alertmanager

The metric path is **Covenant → Prometheus → Alertmanager → your receiver**.
Covenant exports measurements, Prometheus evaluates alert rules, and Alertmanager
groups and delivers notifications. Atraxis reads selected metric observations;
it does not manage Prometheus rules, Alertmanager routing or silences.

These alert rules are separate from Atraxis/Atropos workflow rules. Receiving an
alert does not authorize or execute an intervention. For an explicitly approved
notification after an Atropos intervention, use the separate
[Atropos Alertmanager adapter](https://github.com/decryptus/atropos/blob/main/docs/user/alertmanager.md).

## Define collection and observation semantics

Start with the [Covenant/Prometheus connection](prometheus.md). In addition to a
business metric, export an explicit HTTP-collection result and, when available,
the source's last successful observation timestamp. The examples below assume:

- `orders_source_up`: `1` after successful HTTP collection, `0` after failure.
- `orders_observed_at_seconds`: the source's observation time as Unix seconds.
- A Prometheus scrape job named `covenant-orders`.

Adapt these names and semantics to your exporter. HTTP success does not prove that
every field is valid or the workload is healthy. A retained value scraped again
has a recent Prometheus timestamp without necessarily being a new observation.
Keep Covenant's deliberate `default`, `on_fail` and `on_noresult` behavior; define
availability/freshness metrics explicitly instead of clearing all retained values.

Save a rule file such as `rules/orders.yml`:

```yaml
groups:
  - name: covenant-orders
    rules:
      - alert: OrdersCollectionUnavailable
        expr: >-
          (orders_source_up{job="covenant-orders"} == 0)
          or (up{job="covenant-orders"} == 0)
          or absent(orders_source_up{job="covenant-orders"})
        for: 60s
        labels:
          severity: warning
      - alert: OrdersObservationStale
        expr: >-
          (time() - orders_observed_at_seconds{job="covenant-orders"} > 180)
          or (orders_observed_at_seconds{job="covenant-orders"} > time() + 30)
          or absent(orders_observed_at_seconds{job="covenant-orders"})
        for: 60s
        labels:
          severity: warning
```

The example permits 180 seconds of observation age and 30 seconds of future clock
skew, then holds a continuously true condition for 60 seconds before firing.
Choose thresholds for your collection schedule and clock synchronization. Missing
data remains unknown; it is not converted to a zero-valued business metric.

Add the file and your protected Alertmanager destination to Prometheus:

```yaml
rule_files:
  - rules/orders.yml
alerting:
  alertmanagers:
    - scheme: https
      authorization:
        type: Bearer
        credentials_file: /run/secrets/alertmanager-token
      static_configs:
        - targets: [alerts.example.org]
```

Use an ingress that validates the dedicated token. Keep certificate/hostname
verification enabled; configure the appropriate trusted CA for private TLS.
The example hostnames are placeholders, not services provided by Atraxis.

## Configure a receiver

For a receiver implementing Alertmanager's standard webhook contract:

```yaml
route:
  receiver: local-demo
  group_by: [alertname, job]
  group_wait: 5s
  group_interval: 30s
  repeat_interval: 4h
receivers:
  - name: local-demo
    webhook_configs:
      - url: https://receiver.example.org/alerts
        send_resolved: true
        max_alerts: 16
        timeout: 5s
        http_config:
          authorization:
            type: Bearer
            credentials_file: /run/secrets/alert-webhook-token
          follow_redirects: false
          proxy_from_environment: false
```

Use a receiver you control and an explicitly selected destination. Keep tokens
outside the configuration text and readable only by the service account. Email,
SMS and chat destinations require their own receiver configuration; this example
does not configure any of them.

The receiver must validate authentication, body size and the complete batch before
acknowledging it. Handle `truncatedAlerts` explicitly: a truncated batch is not a
complete view. Choose `max_alerts` and grouping for your expected number of series.
Persist any required receipt before responding successfully. Alertmanager can
retry or repeat notifications; bounded deduplication is not an exactly-once guarantee.

## Validate the complete path

Check configuration before reloading the relevant service:

```sh
promtool check rules rules/orders.yml
promtool check config prometheus.yml
amtool check-config alertmanager.yml
```

On an isolated test source, verify pending → firing → resolved and inspect the
receiver's matching receipts. Also check collection failure, old observation
timestamps, receiver outage and recovery. Do not induce failures on supervised
production services merely to demonstrate the path.

A Prometheus firing state shows rule evaluation. Appearance in Alertmanager shows
alert acceptance. A webhook receipt shows delivery to that receiver. None of these
alone proves delivery to a person or a downstream channel.

The [public Atraxis demo](https://atraxis.io/demo.html?lang=en) shows rule states and
sanitized receipts from a local demonstration receiver. Its clearly identified
pipeline check uses a time-limited synthetic signal with real notification
transport. The page is read-only: it cannot create silences, change routes, send
messages or execute interventions. It retains historical state if monitoring
fails and exposes no private labels, annotations, credentials or raw errors.

See the official [Prometheus alerting rules](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)
and [Alertmanager configuration](https://prometheus.io/docs/alerting/latest/configuration/)
for the complete third-party contracts.
