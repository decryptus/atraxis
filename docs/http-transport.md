# Read-only HTTP transport

`HTTPTransport.fetch(SourceConfig, cancel=None)` performs one request and returns
a `TransportResult`. The same transport accepts an internal `ReadRequest` for the
CLI's fixed Atraxis `/v1/status` route, with separate client credentials. Source
configuration still accepts only monit-docker and CertLord kinds. This internal adapter is used by the periodic collector;
see [collection and authenticated inventory](collection-inventory.md). It is not
a separate CLI command.

## Requests and credentials

Only GET is issued, on `/v1/status` for monit-docker or
`/api/certificates/observations` for CertLord. Each source supplies its own Basic
credential file, reread at request time. Passwords stay inside the one-request
worker and are absent from command arguments, result repr and error codes.
Responses can contain sensitive source data: payloads are returned to the caller,
not logged or included in repr. The caller must apply the pure domain parser
before accepting a snapshot.

Remote origins require HTTPS with certificate and hostname verification. A custom
CA file is reread for each request; otherwise the Python/OpenSSL default trust
store is used. There is no insecure TLS option. Literal loopback HTTP is reserved
for explicitly configured fixtures. Proxy environment variables and netrc are not
used. Redirects are refused without following the Location or forwarding credentials.
Configured HTTPS destinations may resolve to private addresses: this supports
private installations and is not a network egress sandbox. Source configuration
must remain operator-controlled; no tenant-supplied destination API exists here.

## Time and concurrency bounds

A shared transport instance admits at most four requests by default (configurable
in its constructor from 1 to 32), and only one request per source identity.
Admission does not queue: overlap or exhausted capacity returns `busy` immediately.
Callers may run fetches concurrently, but must reuse the same transport instance
to share these limits. Multiple independent instances do not share a global budget.

Each request runs in a fresh Python worker process. The parent uses a monotonic
deadline including process startup, DNS, TLS/connect, headers, body and JSON
decoding. A subprocess is used because a blocked resolver or trickling body cannot
reliably be interrupted by a socket timeout or an abandoned Python thread.
Timeout/cancellation kills and reaps the child before releasing admission.
Cancellation is polled every 50 ms; process launch, OS scheduling and process
cleanup can add overhead. This is not a hard real-time guarantee.

The child accepts bounded configuration input and reads at most the configured
response body limit plus one byte. Successful data travels back as bounded base64
JSON, then is decoded in the parent. The byte limit bounds body buffering, not
total Python process memory. Process startup and memory costs have not been
benchmarked under sustained load. No automatic retries are performed.

## Response contract

Only status 200 with an `application/json` object and identity content encoding is
accepted. Duplicate keys, NaN/infinities (including overflowing exponents), malformed
UTF-8/JSON, conflicting or duplicate length/framing headers, truncation and excessive
bodies are rejected. Chunked responses are accepted within the same body/deadline
limits. The standard library also bounds individual header lines and header count.
Compression is refused to avoid decompression expansion.

| Outcome | Meaning |
| --- | --- |
| `success` | A bounded JSON object was received; domain validation is still required |
| `busy`, `cancelled` | Admission refused or caller cancelled; no successful snapshot |
| `timeout` | Request deadline or socket timeout reached |
| `tls_error` | TLS negotiation or certificate/hostname verification failed |
| `authentication_error`, `forbidden` | HTTP 401 or 403 |
| `endpoint_unavailable` | HTTP 404 or 410 |
| `redirect_refused` | HTTP 3xx, not followed |
| `source_error` | Other non-200 HTTP status |
| `invalid_payload`, `response_too_large` | Invalid response or body exceeds configured limit |
| `configuration_error` | Invalid runtime budgets, origin or credential configuration |
| `transport_error` | Other I/O, worker or protocol failure; no raw exception disclosed |

Results include source identity, wall-clock attempt/completion timestamps and
monotonic completion time and whole-request `duration_seconds`. Completion metadata is also present for failures; it is
not evidence of source freshness. Source timestamps, stale-state handling and
retention belong to the domain collector. A transport failure must never become
an empty healthy inventory.

## Verification and remaining work

Synthetic loopback tests cover both fixed routes, source authentication, proxy/netrc
isolation, status errors, redirects, body framing/limits, malformed JSON, silent
headers/body, trickling responses, cancellation, overlap, capacity, credential
rotation, real TLS trust/hostname rejection and blocked DNS worker cleanup/recovery.
No live installation, repeated issuance, saturation, Docker deployment or production
acceptance is claimed. Scheduling, application shutdown, source-specific parsing,
authenticated inventory and CLI reads are now connected and tested separately.
Synthetic Docker runtime validation is recorded in the milestone review. Explicitly
authorized live acceptance remains outstanding.
