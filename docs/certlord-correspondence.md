# CertLord metric correspondence qualification

The `prometheus_certlord` adapter consumes Covenant's existing native CertLord
metric contract. Its public configuration and limits are in the
[user guide](prometheus-certlord.md). It adds no CertLord or Covenant code changes.

## Evidence

Eight real-chain tests use:

- CertLord's actual `prometheus_metrics` formatter at commit
  `908834f3d3b89d579a908391ce435cc5b419451f` to render synthetic observation JSON.
- Covenant's actual `certlord` plugin at commit
  `3b7e446e494847065ac9890e089da0fa75ff590f`, including its snapshot validator,
  fixed HTTP GET source, explicit Basic credentials and failure responses.
- Two real checksum-pinned Prometheus 3.5.0 processes.
- Atraxis's actual HTTP transport, source service, bundle decoder and API client.

Available states, native UUID, expiry and retry count are compared with the direct
JSON projection. TLS status and successful-check time are compared with the direct
verification object. Tests cover transitions, deletion/empty inventory, missing
material, unknown verification, zero retries, expired certificates, stale/future
collection times, source/scrape failures, malformed output, duplicate UUIDs,
incomplete material and recovery. Only GET `/api/certificates/metrics` source calls
are accepted in the fixture.

A separate test demonstrates the limitation rather than hiding it: a certificate
with an old individual `observed_at` can appear in a recently completed collection.
The direct source rejects that stale observation; the metric projection reports
collection freshness and explicitly keeps `observed_at` unavailable. It is not
accepted by the live rule-observation adapter.

Unit tests cover bounded decoding, required fields and native types, hidden-label
ambiguity, exact target configuration, origin-time bounds, monotonic expiry,
client validation, scoped UUID grants/search, UI/CLI display, unknown fields,
source isolation and historical retention. The matrix validation is shared with
the Monit-Docker bundle, whose existing unit and real-chain tests continue to run.

## Reproduce

The dedicated Prometheus workflow installs the needed formatter dependency and
checks out both pinned repositories. Every test in `correspondence_tests` is
collected explicitly; no optional source is silently skipped:

```sh
export COVENANT_SOURCE=/absolute/path/to/pinned/covenant
export CERTLORD_SOURCE=/absolute/path/to/pinned/certlord
export PROMETHEUS_BINARY=/absolute/path/to/prometheus
python scripts/check-test-collection.py --runner unittest correspondence_tests
python -m unittest discover -s correspondence_tests -v
```

The suite contains 23 tests: eight CertLord cases, eight generic Monit-Docker
metric comparisons and seven coherent Monit-Docker cases. All endpoints,
credentials, TSDBs and records are disposable synthetic fixtures. The supplied
`examples/covenant-certlord.yml` is loaded by the actual plugin; only the source
URL and synthetic credential location are replaced for loopback testing.

## Limits

This is correspondence of the **exported metric fields**, not complete parity with
CertLord's observation API. Domains, version, fingerprint, not-before, individual
observation time, last retry time and full verification evidence are not exported.
The new projection keeps those facts unavailable, uses UUID display names and
labels collection-only freshness. It does not add certificate rule execution.

The tests do not run CertLord Vault/Redis collection, issuance or deployment, nor
the complete Covenant HTTPdis daemon/worker queue. They do not establish production
TLS, clock synchronization, capacity or deployed compatibility. No live source,
notification, rule activation or deployment is performed.
