# Read-only CLI

`atraxis status` summarizes configured sources. `atraxis inventory` also prints
each retained resource and its projected fields. Both issue exactly one
`GET /v1/status` to Atraxis; neither starts a daemon, polls the upstream tools,
opens a TUI, pages output nor performs a business mutation.

When the daemon enables scoped inventory, these reads enforce the same live
membership and read grants as the browser. A sole organization is selected
automatically; the output identifies it. No visible sources returns degraded
status (exit 2). Partial source views preserve only selected resources and display
`Selected resources only`; source-wide metadata is unavailable in text and JSON.
Collection readiness does not assert that a selected ID exists.
Use `-o ID` (or `--organization ID`) on either command to select an organization.
Without it, accounts with several organizations receive an error asking for an
explicit selection. No global inventory fallback or saved session selection occurs.
The client refuses a successful response with a missing or different organization
context when an ID was requested. Current server membership and grants still apply.


## Separate client settings

The default client file is `/etc/atraxis/client.yml`; override it with `--config`.
It is distinct from the daemon's `/etc/atraxis/atraxis.yml`. A reader does not need
source addresses or source credentials.

```yaml
origin: https://atraxis.example.org
credentials_file: reader.json
# ca_file: internal-ca.pem
# timeout: 10
# max_response_bytes: 4194304
```

`reader.json` contains the Atraxis API identity, matching an account provisioned
in its inbound htpasswd file:

```json
{"username": "reader", "password": "replace-with-your-API-password"}
```

Restrict this file to the client user. The password is read from the file, not
passed on the command line. Relative file paths resolve against the client YAML.
Credential syntax, CA validation and destination restrictions reuse the same
implementation as [source connections](source-configuration.md), with separate
credentials and a fixed Atraxis read route. The CLI never falls back to source
credentials, environment proxy credentials or netrc.

Remote origins require verified HTTPS. For a literal loopback listener only,
`origin: http://127.0.0.1:9810` requires `allow_loopback_http: true`. No arbitrary
path, URL userinfo, redirects, query strings or TLS-verification bypass is allowed.
Timeout is an integer from 1 to 60 seconds (default 10); response size is an integer
from 1024 to 4194304 bytes (default 4 MiB). YAML is bounded, strict and validated
with XYS; extra/duplicate keys, aliases and coercion are rejected.

## Commands and scripts

```sh
atraxis status --config /path/to/client.yml
atraxis inventory --config /path/to/client.yml -o operations
atraxis inventory --config /path/to/client.yml --json > inventory.json
```

Text has no ANSI colors, interactive prompt or hidden truncation of identities.
Each source shows availability, current/historical/unknown state, age and resource
count. Historical resources remain visible and are explicitly labelled. Unknown
inventory is not reported as zero resources. Names are JSON-escaped for terminal
safety; control characters in recognized response values are rejected.

`--json` on either command returns the same validated inventory envelope.
The client conservatively adds its entire request duration to retained ages and
rechecks each snapshot against `max_age_seconds`; a response that ages out in
transit becomes historical and exits 2. It never upgrades server-degraded data. Unknown
fields are excluded from output. The client validates envelope types, identities,
readiness consistency and provenance; the server's source adapters own domain
validation. Numeric nulls stay null. Fixed transport/schema errors go to stderr;
no raw HTTP error body or exception text is printed.

| Exit | Meaning |
| --- | --- |
| 0 | API read succeeded; all configured sources have current observations |
| 1 | Configuration, authentication, transport, size or response-schema error |
| 2 | API read succeeded but inventory is degraded/not ready (also argparse's standard usage-error code) |
| 130 | Interrupted with Ctrl-C |
| 143 | SIGTERM requested while the client was reading |

A degraded response is still printed, including with `--json`; preserve stdout if
your script needs historical facts. A source can be current while its workload
is unhealthy or its certificate expired: exit 0 is inventory freshness, not a
workload-health assertion. Always inspect the domain fields for those facts.

There is one bounded request, no automatic retry. The shared transport cancels
and reaps its child on interruption. OS scheduling/process cleanup can add timeout
overhead. The API is not paginated: an inventory exceeding the configured byte
limit fails as `response_too_large`, with no partial successful output. Set source
budgets accordingly; splitting/pagination remains a separate future decision.

Tests use synthetic local sources and a real local Atraxis daemon, with separate
inbound/outbound credentials, degraded/error exit codes and SIGTERM during a
blocked read. No deployed-source or production-capacity claim follows from them.

## Rule operator commands

The separate [rule console guide](rule-console.md) covers the optional local trusted-operator
CLI/TUI, scoped draft/test/activation workflow and one-shot current observation evaluation.
It uses an explicit private operator profile; the status/inventory API stays read-only.

## Inventory search

See the [inventory search guide](inventory-search.md) for authorized search before
pagination, combined filters, the CLI and Textual interfaces.
