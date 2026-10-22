# Remote rule management through Atraxis

Atraxis authenticates the operator and forwards rule commands to the independent
Atropos API. Atropos owns persistence, validation, tests, activation, evaluation
and engine audit. Atraxis has no rule database, Redis credentials or ZEN runtime.
The original `atraxis serve` inventory daemon remains read-only and independent.

## Install the optional client

The public [Atropos repository](https://github.com/decryptus/atropos) contains a separate `client/` Python package.
Install its directory or wheel, then install Atraxis with its `rules` extra:

```sh
git clone https://github.com/decryptus/atropos.git ../atropos
git -C ../atropos checkout 58fe1241267eca99f78229b44f3f2599229594a5
python -m pip install '../atropos/client[observations]'
python -m pip install '.[rules]'
```

This installs the client, not the `atraxis-atropos` engine or its Redis/ZEN
requirements. The engine is installed and started separately on its own host;
see the [Atropos service guide](https://github.com/decryptus/atropos/blob/main/docs/user/rule-service.md).
Install the matching client from source; this source publication does not imply a package-registry release.

## Gateway profile: version 2

Copy `examples/rules/service.yml` to a private mode-0600 file in a trusted directory:

```yaml
version: 2
identities:
  - token_sha256: REPLACE_WITH_ATRAXIS_TOKEN_SHA256
    subject: alice
    organization: example
    atropos_client: alice-atropos.yml
```

The referenced server-side client profile also has mode 0600:

```yaml
server:
  url: https://atropos.example.invalid
token_file: alice-atropos.token
timeout: 15
# ca_file: organization-ca.pem
```

Every mapping has a dedicated Atropos identity credential. Atropos's token must
resolve to the **same subject and organization**: Atraxis sends an identity
precondition with every command and Atropos refuses a mismatch. No actor/grant
claim supplied by a terminal is trusted. User-facing Atraxis credentials are not
forwarded to Atropos. Do not configure a shared engine administrator credential.

Generate independent random tokens for each hop using `secrets.token_urlsafe(32)`;
store their SHA-256 digests in the respective identity profiles and their raw
values only in protected token files. The engine guide contains a complete safe
file-creation example. Provision secrets through your existing secure channel,
never Git, command arguments or URLs.

```sh
atraxis rules-serve --config /private/gateway.yml --port 8081
```

This HTTPdis listener binds only to `127.0.0.1`. For remote access, place it behind
an explicitly configured trusted local HTTPS reverse proxy forwarding only
`POST /v1/rules/command`. Apply bounded body/connection deadlines and admission
limits at the proxy; never log tokens or rule bodies. The local unencrypted hop
and host are trusted. Neither proxy nor production deployment is provisioned.

The profile accepts no `storage`, `redis_url`, `namespace`, `resources` or `grants`.
Those fields belong to Atropos. Version-1 embedded profiles are rejected, with
migration instructions below. Identity mapping files reload on use; replace them
atomically. Revocation is checked on subsequent requests and outgoing operations;
it does not cancel an operation already authorized by Atropos.

## Terminal profile

The operator's own `remote.yml` points to **Atraxis**, using a different token:

```yaml
server:
  url: https://atraxis.example.invalid
token_file: atraxis-access.token
timeout: 15
# ca_file: organization-ca.pem
```

Both profile and token file must be private regular files, owned by root/current
UID, without symlinks. Relative paths resolve against the profile directory.
`origin` remains a legacy alias for `server.url`; declaring both is rejected.
The client verifies TLS CA and hostname. Explicit literal-loopback HTTP requires
`allow_loopback_http: true` for testing. There is no insecure TLS switch, redirect,
ambient proxy/netrc lookup, automatic retry or client environment-variable support
yet. The 1–60 second per-call deadline includes DNS and blocking I/O.

```sh
atraxis rules --remote-config /private/remote.yml catalog
atraxis rules --remote-config /private/remote.yml create cpu-high \
  --name cpu --kind intervention --target container_group:webapps
atraxis rules --remote-config /private/remote.yml draft cpu
atraxis rules --remote-config /private/remote.yml test cpu REVISION --cases cases.json
atraxis rules --remote-config /private/remote.yml activate cpu REVISION \
  --test-id REPORT_ID --expected-generation none
atraxis rules --remote-config /private/remote.yml tui --name cpu --cases cases.json
```

All [console commands](rule-console.md) use the same boundary. Import sends local
file contents; export writes YAML returned by Atropos. The TUI requires review and
Enter before a transition. The local `--operator-config` alternative maps POSIX
UID to an Atropos API profile; it also has no engine or storage access.

## Observations and permissions

Optional `inventory_client` and source/container/filesystem bindings remain in
Atraxis. `observe` fetches the exact authorized active rule through Atropos,
projects current source observations to declared inputs, then asks Atropos to
evaluate them. No rule condition is interpreted locally. Stale, ambiguous,
missing or wrong-source observations fail; generation and freshness are checked
again around evaluation. Manual `evaluate` inputs are not trusted telemetry.

Atropos owns authoritative lifecycle grants and target membership for this
prototype. Atraxis owns user authentication and the gateway mapping. Automatic
policy synchronization, central permission-administration UI, SSO, token expiry
and distributed revocation are not implemented. Provision policy explicitly in
the engine until that integration exists.

## Errors and recovery

The bounded JSON command API remains `{"method":"active","arguments":{"name":"cpu"}}`.
It rejects actor/organization overrides, cookies/browser Origin requests,
ambiguous framing, duplicate JSON keys and non-finite numbers. Request/response
limits remain 512 KiB/16 MiB. Successful responses carry `X-Rule-Service: atraxis`;
the gateway's engine client requires `X-Rule-Service: atropos`.

HTTP 401/403 denies access, 400/413 rejects input, 404 means unavailable engine
records, 409 means a revision/generation/evidence conflict, and 503 means an
unavailable service or unknown outcome. CLI codes remain 0 success, 1 failed
scenario tests, 2 operation/configuration/network error, 3 denied, 130 interrupted.
No raw exception or credential is returned to the caller.

**A lost mutation response may follow a committed change in Atropos.** No layer
retries automatically or falls back to a local engine. Inspect draft/history,
active version and audit before explicitly deciding to retry. Per-hop deadlines
are independent; client cancellation does not cancel an engine commit. Generation
CAS is not an exactly-once delivery or general idempotency-key protocol.

## Migration from the embedded prototype

Move the old profile's storage, namespace, resources and grants to the standalone
Atropos service. Preserve the Redis database, namespace, subjects and organizations
so existing revisions and active generations remain available without rewriting
records. Provision distinct engine tokens and convert Atraxis to the version-2
mapping shown above. Keep observation bindings and inventory access in Atraxis.
Stop old embedded writers before starting the split services; inspect active state
and audit before further changes. The old Python embedding factory is removed.
No user data or production configuration is automatically moved by this change.

## Explicit workflow dispatch

The current client adds `plan_dispatch`, `dispatch` and `dispatch_status` on the same authenticated command route. See [review and dispatch](rule-dispatch.md). Atropos owns binding approval and execution; no action parameters or workflow definitions can be injected into a dispatch request.

## Environment configuration for CLI and Textual

With the matching `atropos-client`, choose the remote connection explicitly:

```sh
export ATRAXIS_SERVER_URL=https://atraxis.example.invalid
export ATRAXIS_TOKEN_FILE=/private/atraxis-access.token
export ATRAXIS_TIMEOUT=15
atraxis rules --remote catalog
atraxis rules --remote tui --name cpu --ui textual
```

`ATRAXIS_SERVER_URL` addresses the **Atraxis gateway**. It is not an Atropos storage
URL. The optional `ATRAXIS_CA_FILE` selects a CA bundle. `ATRAXIS_TOKEN` is an
alternative to `ATRAXIS_TOKEN_FILE`; do not set both. Prefer a private mounted
file. Never put a bearer token in command arguments or examples committed to Git.

Settings resolve in this order: explicit command options > `ATRAXIS_*` environment
> `--remote-config` YAML > defaults. `--server-url`, `--token-file`, `--ca-file` and
`--request-timeout` go before the rule subcommand. They also work with
`--remote-config /private/remote.yml`. The default deadline is 15 seconds; the
accepted range is 1–60. Empty or invalid explicit values fail instead of falling
back. A higher-priority credential replaces the lower-priority credential as a
whole; token and token-file settings at the same level are always ambiguous.

```sh
atraxis rules --remote-config /private/remote.yml --request-timeout 20 catalog
```

YAML-relative paths use the YAML directory; environment/command-relative paths
use the working directory. TLS verification, token-file permissions and bounded
requests are identical in CLI and Textual. Environment/command destination
settings cannot enable plain HTTP. An environment token is captured when the
client starts; token files are reread on every request.

`--operator-config` does not accept remote command overrides. Server-owned gateway
connections ignore both `ATRAXIS_*` and `ATROPOS_*` environment settings: they retain
the administrator's per-user Atropos API profiles and identity checks. This feature
does not move any rule, policy or rule storage into Atraxis.

## Queue admission refusal

With independent client 0.1.0a6 and engine 0.1.0a11, a verified HTTP 429 carrying
`rule_queue_full` is an explicit refusal of a new workflow/test admission.
The CLI reports this fixed error code and does not retry. A generic proxy 429 or
malformed response remains an unknown mutation outcome. Keep the stable event ID
for status recovery; do not equate transport failure with safe resubmission.
Queue limits are configured by the Atropos administrator, not in Atraxis profiles.


## Explicit organization context

One authenticated username, OS UID or gateway token may have several mappings,
one per organization, all naming the same subject. Each mapping retains a separate
Atropos credential for exactly that subject and organization. Duplicate organization
mappings or different subjects under one identity are rejected. With multiple
eligible mappings, no organization is chosen implicitly.

The browser offers an organization selection page and carries the choice in links,
GET fields and confirmation URLs across inventory, rules and interventions. Tabs
are independent. A form is bound to the selected organization and cannot be reused
in another. An unavailable inventory scope is refused rather than replaced with
a global inventory view. Current membership and server mappings are rechecked.

For local or remote CLI and terminal commands, select before the subcommand:

```sh
atraxis rules --operator-config operator.yml --organization operations tui --name cpu --ui textual
atraxis rules --remote-config client.yml --organization operations history cpu
```

The matching client is **0.1.0a9**. Remote selection uses an explicit
`X-Atraxis-Organization` request header. The gateway resolves the acting subject
from the authenticated token and the destination from its own mapping; the header
is only a selection, never a permission or credential. Direct Atropos clients
remain bound to their engine credential. Switching terminal organization requires
a new command/session, so pending confirmations cannot change context silently.


## Rotate a file-backed engine token

This procedure rotates the dedicated Atropos credential referenced by
`token_file` in the server-owned independent-client profile. It does not change
the browser login or the optional incoming Atraxis gateway credential.

Prepare a new independent random token in a private trusted staging file using
your existing secure provisioning channel. Never include it in command arguments,
URLs, YAML, Git, application logs or audit records. Keep its raw value only in
protected token files and its SHA-256 digest in the engine identity configuration.

1. Retain the exact subject, organization, destinations and grants. A rotation
   changes authentication material, not who the credential represents.
2. Atomically replace the protected engine identity configuration with the new
   digest for that same subject/organization. Keep unrelated identities and policy
   unchanged. Atropos reloads its identity configuration on subsequent requests.
3. Atomically replace the protected `token_file` at the existing configured
   path with the new raw token. File-backed clients reread that file for each
   call, including already-created client objects. A changed profile path or
   destination is a separate configuration operation.
4. Use verified HTTPS to require refusal of the old token, a successful read
   with the replacement under the same expected identity, and unchanged scoped
   permissions. Inspect the actual browser or terminal journey as appropriate.
   Verify every configured process/replica; do not infer distributed revocation
   from one listener's result.

Revoking the server-side old digest before publishing the new client token
creates a deliberate deny window. Calls during that interval fail closed;
there is no previous-token or shared-administrator fallback. Do not automatically
retry a mutation that may already have committed. Inspect its durable status,
history and audit before any explicit next action.

The independent client sends the expected subject and organization on every
business operation. Another valid user's token must not be substituted into
this file, even if that user has broader privileges. Missing/unreadable token
files fail rather than reuse cached access. The file-backed procedure does not
apply to an explicitly supplied inline/environment token: such a client requires
its own approved reconfiguration and validation.

No service restart is required for this unchanged-path, file-backed engine-token
procedure. It does not terminate an action already authorized or reverse a
completed effect, and it does not grant new permissions. Engine-token rotation
alone does not invalidate browser confirmations for an unchanged browser identity;
those confirmations still require their own current rights and exact state checks.

Record the operator, component, stable identity, time and verified outcome through
trusted host change management, without raw tokens or token digests. External file
replacement is not itself an integrated account/rule audit event. Source/Auton
secrets, incoming remote gateway tokens, deployment-specific secret distribution,
expiry/SSO, self-service credential administration and cross-replica revocation
require separate procedures and acceptance.
