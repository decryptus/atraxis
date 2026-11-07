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

## Basic attempts behind a trusted ingress

Atraxis 1.0.7 adds client-address isolation and bounded-table eviction.
The published 1.0.7 image and current registry Compose starter include both.

The generated nginx configuration limits each client address to ten requests per
second, with a burst of twenty and HTTP 429 for excess requests. It overwrites
`X-Forwarded-For` with `$remote_addr`, never `$proxy_add_x_forwarded_for` or a
client-supplied value. Atraxis reads this header only when the TCP peer is in
`general.trusted_proxy_networks`. Exactly one bare IPv4/IPv6 address is required;
missing, duplicate, chained, scoped or malformed values return HTTP 400 before
password verification. Untrusted direct clients cannot select their limiter key
with forwarding headers. Forwarded usernames never establish identity.

The failure budgets are eight per (client address, account) and 64 per client
address, over a fixed 60-second window. A failing client cannot lock a different
address out of the same account. The table holds at most 4096 addresses; on a new
failure at capacity, it evicts the oldest entry. An unknown address is not blocked
merely because the table is full. Successful authentication does not add entries.
Eviction may discard an attacker's budget under high churn: this is an explicit
availability tradeoff, so keep nginx ingress throttling enabled.

Upgrade the application and proxy configuration together. Existing custom proxies
must overwrite the header as above before enabling the new application. Keep the
application port private and trust only the actual immediate proxy. The guided
Compose namespace uses `127.0.0.1/32`. Additional ingress hops require a separate
explicit trusted-address design; arbitrary forwarding chains are not accepted.

The 1.0.9 guided installer and the Compose starter in tag v1.0.9 deploy image
1.0.7 with this proxy configuration. Existing installations need a coordinated
application and proxy upgrade.

Limits are local to each application process. Clients sharing a NAT address share
budgets, and these controls cannot guarantee availability under a distributed
attack. There is deliberately no global per-account lock that a remote attacker
could use against clients at other addresses.

The guided installer accepts IPv4 listener addresses. When a trusted ingress forwards
an IPv6 client address, Basic authentication failures share one budget per /64.
IPv4-mapped addresses share the budget of the corresponding IPv4 client. A custom
dual-stack ingress must preserve the client address: if Docker presents only its
gateway to nginx, the application can only limit the shared gateway.
