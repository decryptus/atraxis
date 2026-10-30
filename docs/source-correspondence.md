# Complete observation correspondence

Acceptance is equality with Atraxis's direct native projection, not a chosen list
of numeric metrics. Source IDs/kinds and receipt times identify the chosen transport;
resource native IDs, fields, types, source metadata and native freshness semantics
must otherwise agree. Monit observation age includes measured HTTP/retention delay.

The previous nine-input Monit contract and limited CertLord metric contract did
not meet that requirement. Native metric exports omitted inventory information,
and subset assertions could pass while fields were absent. Their limits must not
be described as full parity. The legacy generic-metric fixture remains under
`correspondence_tests/fixtures` solely to exercise eight known baseline behaviors.

## Contract and coverage

Covenant's optional observation plugins read the existing JSON APIs. A closed
allowlist covers every field consumed by `parse_monit` and `parse_certlord`.
Unrecognized source data, PEM and action/audit details are excluded, just as in the
direct inventory projection. No source SDK or dependency on Atraxis is introduced
into Covenant; the stdlib wire codec is identical in both repositories, checked by
the chain suite. Atraxis reuses the native parsers after reconstruction.

| Area | Acceptance checks |
| --- | --- |
| Monit source metadata | Ready/running/cycle success, counters, age/timestamps, error code, all action counters |
| Containers | Every native measurement, process/event/restart/policy field, all health states, full ID/name/status |
| Filesystems | All disk/inode quantities, modes/access flags, group/path identity and list order |
| Certificates | Every native field, multiple domains, versions, fingerprint, retries, complete TLS evidence |
| Precision | Integers beyond 2^53, floats, booleans, null versus zero, missing versus empty lists |
| Lifecycle | Multiple resources, replacements, renewal with stable UUID, changed labels, deletion, empty inventory |
| Freshness | Native Monit age plus transport, oldest certificate observation, expiry distinct from age, clock skew |
| Faults | Source/target failure and recovery, malformed response, duplicate IDs, missing/changed/extra rows, digest mismatch |
| Consumers | API/web/CLI/search, scoped grants, nine Monit rule inputs and source-bound attestations |

## Scrape clocks

An instant query avoids returning historical string-label generations. Its API
sample timestamp is query evaluation time, so it cannot establish freshness.
`timestamp` must consume each raw fixed-family selector **before** any
`label_replace`: applying it after label replacement also yields evaluation time.
The fixed query combines values and actual sample clocks in one evaluation, then
checks every row against the latest `up` timestamp. No independent query can race
a second scrape. The manifest's version, row count and content digest detect
partial or altered field sets. This digest detects transport inconsistency; it is
not authentication, which remains the TLS/credential boundary.

## Reproduce

Install the project and pinned chain dependencies from `.github/workflows/prometheus.yml`.
Set `COVENANT_SOURCE`, `CERTLORD_SOURCE`, `MONIT_SOURCE` to the pinned source checkouts,
and `PROMETHEUS_BINARY` to the checksum-verified Prometheus 3.5.0 executable.

```sh
python scripts/check-test-collection.py --runner unittest tests
python -m unittest discover -s tests -v
python scripts/check-test-collection.py --runner unittest correspondence_tests
python -m unittest discover -s correspondence_tests -v
```

The chain runs actual Covenant HTTP plugins and two real Prometheus processes
against disposable authenticated loopback JSON endpoints. Additional tests use
native Monit status production and CertLord certificate projection with synthetic
material. Covenant separately exercises actual YAML loading, workers and HTTPdis
for both new plugins in an isolated subprocess. No production Docker/Vault/Redis,
issuance, deployment, rule activation or notification occurs.

Bounds reject oversize snapshots explicitly. These tests do not establish production
capacity, remote TLS setup, synchronized deployment clocks or deployment acceptance.
See the user guides for transport delay, budgets and storage cardinality.
