# Set up and diagnose Atraxis

Use Python 3.11 or 3.12 and install the native `libmagic` dependency, then install
Atraxis as described in the [runtime guide](../docs-runtime.md). `setup` prepares
a starter configuration. `doctor` explains configuration and source-read problems.
Neither command starts a daemon, creates account permissions or submits an
intervention. They run locally as the operator, not through a browser endpoint.

## Prepare the files

Create a directory owned by your service/operator account, with no group or
other write permission. The examples use `/etc/atraxis`; choose a directory you
own if installing without root. Prepare an HTTPdis-compatible htpasswd file with
a strong password hash using your normal administration tools. Keep it private.
`setup` references this file; it never asks for or prints a password.

For a source, prepare a separate private JSON credentials file containing its
`username` and `password` fields. Use an account allowed to read that component's
observation API. See [source configuration](source-configuration.md) for the
credential and optional CA-bundle formats. Atraxis login credentials and source
credentials are separate.

## Review a starter configuration

```sh
atraxis setup \
  --output /etc/atraxis/atraxis.yml \
  --auth-file /etc/atraxis/users.htpasswd \
  --source-id server-a \
  --source-kind monit_docker \
  --source-origin https://monit.example.net \
  --source-credentials-file /etc/atraxis/monit-credentials.json
```

This validates the candidate and prints a YAML preview without creating the
destination or contacting the source. Add `--source-ca-file /path/to/ca.pem` when
using a private CA. Use `--source-kind certlord` for certificate observations.
Paths supplied to `setup` resolve against your current directory and are written
as absolute references, so saving the configuration elsewhere does not change
which files are used. Credential contents and certificate material are not copied
into the configuration.

Repeat the same command with **`--write`** to create the file with mode `0600`.
The destination must not exist; existing files and symbolic links are never
replaced. A complete file is published only after validation and disk flush.
If an I/O failure is reported, inspect the destination before trying again: a
complete file may already have been published. Do not remove it blindly.

The listener defaults to `127.0.0.1:9810`. A non-loopback listener needs explicit
`--behind-tls-proxy` and a properly isolated HTTPS reverse proxy; this flag does
not install a proxy or validate its TLS configuration. Plain HTTP source reads
are allowed only for literal loopback addresses with
`--allow-source-loopback-http`, intended for local tests. Remote sources require
HTTPS; redirects are refused.

The four source options must be supplied together. You can omit them all to
prepare an empty starter, but it will never report ready without sources. The
starter supports one initial source; edit the YAML for additional sources and
advanced collection limits, then validate it again.

## Check configuration and connections

Start with the offline diagnostic:

```sh
atraxis doctor --config /etc/atraxis/atraxis.yml
```

It checks listener/authentication settings, source settings and configured
account, inventory and rule profiles independently. Missing/corrupt SQLite or
JSON account stores are reported without initialization or conversion. Other
account adapters are explicitly marked unverified and are not loaded by the
offline diagnostic. With rule profiles enabled, install the matching independent
Atropos client; its configured token/CA files are validated without RPCs or
environment-variable overrides. No remote engine permissions are inferred.

Explicitly opt in to one source observation read per configured source:

```sh
atraxis doctor --config /etc/atraxis/atraxis.yml --probe-sources
```

These are fixed-route GET requests to `/v1/status` for monit-docker and
`/api/certificates/observations` for CertLord. At most four run concurrently,
using each source's configured timeout/size/resource limits. Invalid local
configuration prevents all network probes. A source failure does not suppress
results for other sources. There are no automatic retries, redirects, rule
mutations, notifications or workflow submissions. Ctrl-C/SIGTERM cancel and reap
the reader processes. The diagnostic does not connect to the Atraxis daemon or
Atropos engine and does not test an HTTPS reverse proxy.

Successful HTTP delivery is insufficient: the payload, source readiness and
observation freshness must also validate. Stale or invalid data never becomes a
healthy empty inventory. The report contains fixed error codes and advice,
without source response bodies, credential contents or raw exceptions.

Add `--json` for structured output. `sources_ready: null` means **not checked**,
not ready. The result is a point-in-time check; the running daemon performs its
own continued collection.

| Exit code | Meaning |
| --- | --- |
| `0` | Requested checks passed; offline checks still do not establish source readiness |
| `1` | Invalid/unreadable local configuration or a required configured component |
| `2` | Operator attention needed: no sources, shared inventory, unverified adapter, or failed/stale source observations |
| `130` / `143` | Interrupted by Ctrl-C / SIGTERM |

## Enable access and start explicitly

The starter preserves existing behavior: **all authenticated users share the
configured inventory**. Before adding users who need scoped access, create the
[SQLite account directory](accounts.md), configure
[inventory access](web-inventory.md#access-and-scope) and rerun `doctor`.
Account administrator rights and Atropos intervention rights remain separate.
For rules, follow the [web rule connection guide](web-rules.md); `setup` does not
create an engine, implicit rule grant or action binding.

Once the diagnostic findings are resolved or explicitly understood:

```sh
atraxis check-config --config /etc/atraxis/atraxis.yml
atraxis serve --config /etc/atraxis/atraxis.yml
```

Visit `/ui` at the configured origin. Test actual login and scoped access with
the intended users. The diagnostic checks that the htpasswd file is readable and
nonempty; it does not verify a password or prove TLS ingress, deployment capacity
or end-to-end intervention readiness.


## Rotate the Atraxis login password

HTTP Basic usernames identify the same mapped accounts before and after a
password change. Account membership, delegated authority and engine permissions
are separate; do not rename/reuse an identity to rotate its password.

The login file is loaded when the Atraxis HTTP service starts. Replacing it
alone does **not** revoke the password already loaded in a running process.

1. Use a planned maintenance window. Stop the Atraxis HTTP service and verify
   the listener is closed; an external proxy must not keep forwarding to an old
   replica. If a credential is compromised, close access before preparing its
   replacement. Do not leave another process serving the previous login file.
2. In a private trusted directory, prepare the complete replacement login file
   using your approved HTTPdis-compatible strong password hashing tooling.
   Preserve all unrelated entries. Never put a password in a command argument,
   URL, configuration YAML, source repository, review ticket or terminal log.
   Publish the protected file by atomic replacement on the same local filesystem;
   preserve the required service ownership and private permissions.
3. Validate the daemon configuration, then start the service using its normal
   approved operator procedure. This is a restart, not hot password reload.
4. Through the intended verified HTTPS ingress, require a 401 for the old
   password and successful authentication for the replacement. Check an
   unaffected account and the intended account's actual scoped access separately.
   If verification fails, keep the listener closed while investigating; never
   silently restore a compromised secret.
5. Close old browser tabs/contexts and open a fresh authenticated context.
   Restart discards process-local review/confirmation tokens. Review any pending
   change again; do not resubmit an old confirmation or an uncertain mutation.

This procedure affects the HTTP login only, including its inventory/administration
surfaces. It does not rotate a source password, engine token, remote gateway
token or Auton credential. For the file-backed Atropos hop, use
[the separate engine-token procedure](rule-remote.md#rotate-a-file-backed-engine-token).

Record the component, account identity, operator, time and verified outcome in
your trusted host/change-management audit. Do not record raw secrets or password
hashes. Atraxis's account/rule audit records business changes; replacement of an
external credential file does not itself append an integrated credential-rotation
event. There is no password-reset API, self-service password screen, automatic
cross-replica invalidation or distributed cutoff of already authorized work.
