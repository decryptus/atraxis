# HTTP daemon prototype

Atraxis now has explicit `serve` and `check-config` commands. The same foreground
process runs locally, under a service manager or in Docker. HTTPdis handles
routing and Basic authentication; Sonicprobe supplies its worker infrastructure.
XYS (Sonicprobe 0.3.60) validates a compiled, fixed configuration schema.
DWho performs data-only configuration normalization. CLI entry points do not
own domain behavior and never start a TUI or daemon implicitly.

## Installation and configuration

For a validated starter configuration and step-by-step checks, use the
[setup and diagnostic guide](docs/setup.md).

Use Python 3.11 or 3.12. Package metadata excludes Python 3.13+ because the pinned
HTTPdis dependency still imports `cgi`, removed from the Python 3.13 standard
library. Broader Python support requires a dependency update and runtime validation.
The host also needs the libmagic shared library used by HTTPdis/python-magic
(`libmagic1` on Debian-family installations). The Dockerfile installs it explicitly
and checks daemon imports during the image build.

```sh
python -m pip install .
atraxis check-config --config /etc/atraxis/atraxis.yml
atraxis serve --config /etc/atraxis/atraxis.yml
```

Use `examples/atraxis.yml`. Relative authentication-file paths resolve relative to
the YAML file. Unknown keys, duplicate keys, aliases, invalid types, missing
files and files over 64 KiB fail with a safe error. The safe YAML loader rejects
duplicates, aliases and custom tags before XYS checks the shape, exact scalar
types and numeric bounds. XYS does not coerce strings/numbers or apply modifiers.
Atraxis then checks IPv4 binding/proxy consistency and authentication-file access.
The trusted schema is compiled once; input YAML cannot define a schema or validator. Source settings can be validated offline with `check-config`; see
[source configuration](docs/source-configuration.md). `serve` starts bounded periodic
collection after authentication setup and listener binding. `sources: []` remains
valid but never ready. See [inventory semantics](docs/collection-inventory.md).
The authentication file must contain an HTTPdis-compatible htpasswd entry.
Provision a strong password hash with your normal administration tooling; do not
reuse the synthetic SHA-1 fixtures used solely by tests. Keep this file readable
only by the operator and the service identity. YAML contains its path, not a password.
`check-config` validates configuration and file availability; actual hash/password
verification occurs when a client authenticates. Credentials are loaded at startup;
restart is required after rotating the file in this prototype.

Defaults: IPv4 loopback, port 9810, four workers (maximum 32). All three read routes
require Basic authentication. Use HTTPS termination remotely. A non-loopback bind
requires explicit `behind_tls_proxy: true`; this acknowledges deployment intent,
it does not configure TLS or enforce network isolation automatically. Expose the
internal listener only to the trusted proxy. No forwarded identity headers are trusted.

| Route | Authenticated response |
| --- | --- |
| GET/HEAD /healthz | 200, alive |
| GET/HEAD /readyz | 200 when visible sources are current; otherwise 503 |
| GET/HEAD /v1/status | 200, inventory with provenance, freshness and last-attempt outcomes |

Enable `general.inventory_access_file` for organization and user/group read scopes;
see [inventory access and context selection](docs/web-inventory.md#access-and-scope).
Without that profile, these read endpoints share the configured inventory with all
authenticated accounts. With multiple memberships, `/v1/status` and `/readyz`
require `?organization=ID` (409 without selection); invalid/revoked scope gives 403
and unavailable access configuration gives 503.

The inventory routes do not mutate resources. Empty status is explicitly not ready; it does not claim
to have collected an empty infrastructure. HTTPdis request handling uses bounded
workers, a 1 KiB request-body limit and a five-second socket inactivity timeout.
The latter is not a whole-request wall-clock deadline. SIGINT/SIGTERM stop the
foreground process, cancel/join source readers and close the server. The [read-only CLI](docs/cli.md) consults this API using separate client settings.

## Docker

The Dockerfile builds a non-root image (UID/GID 10001) and starts the same daemon.
Use a separate container YAML with `listen_addr: 0.0.0.0` and
`behind_tls_proxy: true`, mounted at `/etc/atraxis/atraxis.yml` read-only. Mount the
htpasswd file read-only at `/run/secrets/atraxis.htpasswd`, readable by UID 10001.
Put the container and TLS reverse proxy on a dedicated network; do not publish
port 9810 to the Internet. Docker DNS/service names provide internal addressing;
public DNS points to the proxy, which owns TLS termination.

The image build/runtime is validated by CI `docker-acceptance` job
using synthetic sources and credentials. Run `docker build -t atraxis-acceptance:local .`
then `python3 scripts/docker-acceptance.py --image atraxis-acceptance:local` to repeat
the isolated checks. See the [validation guide](docs/contributing.md)
for exact evidence and limits. There is no image publication workflow or deployment.

## Checks

```sh
python scripts/check-test-collection.py --runner unittest tests
python -m unittest discover -s tests -v
```

The unittest suite covers the core, strict configuration, explicit CLI dispatch, actual
loopback HTTPdis requests, authentication, HEAD, unavailable readiness and server
cleanup. Configuration regressions include boundary values, bool/int separation,
unknown fields, unavailable authentication files and redaction of XYS diagnostics.
A separate installed-package check outside the checkout validates
`check-config`, authenticated HTTP startup and SIGTERM shutdown. Test credentials
and inventory are synthetic. This does not validate remote TLS/proxy configuration,
load/saturation or a Docker runtime.
