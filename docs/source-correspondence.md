# Direct and Covenant source correspondence

The `prometheus_monit` adapter supplies the nine supported Monit-Docker rule
inputs through Covenant and Prometheus, preserving the container's native ID and
filesystem group/path. The exporter and query contract is documented in the
[user guide](prometheus-monit.md). This contributor record describes the evidence
and its limits.

## Qualification result

| Case | General metric adapter | Monit-Docker bundle adapter |
| --- | --- | --- |
| Valid nine-field fixture | Values match after test-only decoding | Native container facts match the direct adapter |
| Fresh scrape of old source data | Can be current | Rejected using origin observation time |
| Missing CPU after a new scrape | Old sample remains in the range | CPU is unknown; rule fact is refused |
| Unhealthy → healthy → unhealthy | Different label sets remain in the range | Only the selected scrape's health is used |
| Source unavailable, exporter still reachable | Other retained metrics can look current | Source readiness/metadata invalidates the bundle |
| Exporter scrape fails | Previous samples remain in the range | Latest `up=0` invalidates the bundle |
| Zero/false and multi-core CPU | Numeric values preserved | Native types/units preserved; CPU above 100 accepted |
| Empty selected scope | Empty generic query is unknown | Current zero count plus matching metadata is authoritative |
| Same container on two Prometheus sources | Distinct source IDs | Distinct source IDs with the same full container ID |

The generic adapter intentionally retains its original semantics. Its eight
baseline tests still demonstrate the original gaps. Seven additional tests run
the installed bundle adapter through two real Prometheus instances and Covenant's
real HTTP plugin against a synthetic Monit-Docker HTTP endpoint. They verify GET
`/v1/status` reads, current facts, health transitions, missing fields, invalid/stale/
future origin timestamps, source and exporter failures, recovery, ambiguous IDs
and empty scope. No test-only metric-to-fact decoder is used on this path.

## Design and consumer checks

One bounded matrix query returns `up`, three source metadata gauges, container
identity and the nine measurement families. A latest successful scrape supplies
the anchor timestamp; all guards must match it and only resource samples at that
anchor are projected. Origin and scrape age both constrain the snapshot. The
existing native parser validates the reconstructed container/filesystem fields;
its synthetic envelope counters are discarded, not published as observations.

A dedicated source kind makes the stronger contract explicit. The ordinary metric
adapter does not acquire observation authority. The API client, web inventory,
search/resource grants and observation projection use container identities for
the new kind. Host-approved connection profiles retain control of the metric
contract. Rule bindings, generations, rights and attestations retain their existing
checks; adding this adapter does not activate a rule or submit an intervention.

Unit tests cover malformed/partial responses, clock bounds, missing/misaligned/
duplicate guards, duplicate fields and hidden labels, history/recovery, native
attestation fields, expiry during use, wrong-source bindings, source isolation,
configuration administration, and whole-response budgets. The collector import
boundary remains independent of HTTP, UI, credentials and source SDKs.

## Reproduction

The Prometheus workflow pins Covenant to
`3b7e446e494847065ac9890e089da0fa75ff590f` and checksum-verifies Prometheus 3.5.0.
It explicitly guards and executes the Monit-Docker correspondence tests alongside the CertLord suite:

```sh
export COVENANT_SOURCE=/absolute/path/to/pinned/covenant
export CERTLORD_SOURCE=/absolute/path/to/pinned/certlord
export PROMETHEUS_BINARY=/absolute/path/to/prometheus
python scripts/check-test-collection.py --runner unittest correspondence_tests
python -m unittest discover -s correspondence_tests -v
```

Install the dependencies pinned in `.github/workflows/prometheus.yml`. The tests
read the exact `collects` block from `examples/covenant-monit.yml`, replacing the
remote destination with a disposable loopback HTTP server. The local harness
serializes the plugin call as Covenant's endpoint worker does. The two Prometheus
processes, source server, exporter, synthetic credentials and TSDBs are disposable.
The generic and bundle test classes each retain their own series history.

Normal unit tests also require the collection guard before execution:

```sh
python scripts/check-test-collection.py --runner unittest tests
python -m unittest discover -s tests -v
```

## Remaining acceptance limits

This establishes correspondence for the nine supported rule inputs with the
specified exporter contract. The source JSON is synthetic, not a running Docker
installation. The test calls the real Covenant HTTP plugin and renders its output;
it does not start the full Covenant HTTPdis daemon or its queue workers. It does
not qualify a deployed installation, remote TLS, clock synchronization, capacity,
or actual engine/action execution. CertLord has a separate
[correspondence record](certlord-correspondence.md).

The contract requires a coherent source response rendered by the serial exporter
endpoint and removal of invalid/missing values. Generic retain/default policies,
independently updated gauges, recording rules, or aggregation across upstream
sources cannot establish that contract. Detection occurs at the next scrape and
Atraxis poll. Bounds include retained label generations; excessive churn fails
closed. Deployment must explicitly configure the exporter, matching scrape target
and source/binding/grants; this change does not deploy or rebind anything.
