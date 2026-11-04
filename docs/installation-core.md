# Installation preparation core

This implementation candidate is the first stage of guided Atraxis installation.
The preparation API backs the [Textual preparation wizard](setup.md#guided-preparation-in-the-terminal). Existing `setup`/Compose behavior is unchanged.
Do not consume its bundle as an Atraxis daemon configuration.

## Scope and dependencies

`atraxis.installation.engine.Engine` separates untrusted installation data from
host-owned authority. The simple and advanced screens and any future batch frontend call this
same engine. Preview validates the plan offline and reports `unverified`.
`prepare` validates everything again, probes the sources, then creates a fresh
private input bundle and reports `prepared`, with `services_started: false`.

Development installation: `python -m pip install ".[installer]"`. The extra pins
Cryptography for certificate inspection; it does not add Textual, an engine,
Docker access, a privileged helper or a new entry point. Callers of the spawned
probe must use Python's normal `if __name__ == '__main__':` entry guard.

No account or grant is created, no SQL is executed, no schema is changed, no
shell command is assembled, and no service is started. Atropos provisioning,
certificate issuance and Nginx configuration remain outside this stage.

## Trust boundaries

The threat model is hostile form/batch data and hostile source responses inside
an otherwise trusted local process and OS account. It is not a sandbox against
root, malicious Python code, an already compromised installer UID, or a malicious
operator who owns the host capabilities. The implementation targets Linux/POSIX.

The host must separately provision:

- A private installation root at `/var/lib/atraxis/installations`, owned by the
  installer effective UID, mode `0700`. All ancestors must be owned by root or
  that UID, without group/other write access, and must be physical directories except for one explicitly declared alias.
- An immutable `Authority`: exact allowed HTTPS origins including ports,
  explicit IP networks and exact allowed IPv4 listener addresses. The form cannot
  supply or extend this policy. Broad default routes are refused. Loopback and
  private source addresses need explicit allowance; link-local, unspecified,
  multicast and ambiguous/mapped addresses are refused.
- Credential and TLS material acquired securely by the embedding application.
  The engine accepts values/bytes, never caller-selected material file paths.
  The terminal entry point imports only fixed, bounded regular files from a trusted directory.

An advanced-mode switch cannot alter any of these capabilities. `Root(fd)` is a
trusted internal directory capability, used with disposable test directories;
it is not a public output-directory option or a field in the installation plan.

## Input contract

`preview(raw)` and `prepare(raw, ...)` take a bounded UTF-8 JSON **byte string**.
Duplicate keys, non-finite values, excessive nesting, unknown fields and implicit
type conversions are rejected. The exact version-1 fields are:

| Field | Meaning and restriction |
| --- | --- |
| `version` | Integer `1`, not a boolean |
| `name` | Fresh child slug: `[a-z][a-z0-9_-]{0,47}` |
| `listen_address` | Canonical IPv4 literal from host authority |
| `listen_port` | Integer 1024–65535; actually reserved during preparation |
| `server_name` | Canonical IP or lowercase ASCII domain; exact certificate SAN match |
| `sources` | 1–16 unique source IDs with exactly `source_id`, `kind`, `origin` |

This stage accepts the direct `monit_docker` and `certlord` adapters. Prometheus
adapter setup remains future wizard scope; existing runtime support is unchanged.
Origins must be canonical HTTPS origins without path, credentials, query,
fragment, whitespace, zone identifier or interpolation syntax. There is no silent
trimming, IDN conversion or malformed-address correction.

The keyword arguments to `prepare` are `login`, `source_credentials`,
`certificate`, `private_key`, and `source_cas`. Credential maps have exactly
`username`/`password`; per-source maps must match the source IDs exactly. Login
passwords have a 16-character minimum; source passwords must be nonempty. Values
are bounded and control characters refused. Quotes, dollar signs and SQL-looking
password text remain JSON data; they are never interpolated into commands or
queries. `login.json` contains prepared input, **not an initialized account**.

TLS inputs must be bounded PEM bytes. The server key is unencrypted PKCS#8,
RSA ≥2048 or EC ≥256 bits. It must match the leaf certificate; dates, exact SAN,
leaf constraints, optional server-auth EKU and supplied issuer signatures are
checked. Wildcards and legacy key encodings are intentionally unsupported in this
first stage. The supplied server chain is not proof of public/browser trust or
revocation status. Such deployment checks still belong to later qualification.
Expensive server-key and CA parsing runs in a disposable worker with a five-second
deadline (`validation_timeout` on expiry), before port reservation, source access
or bundle creation. Byte/count limits are enforced before spawning.
Each source requires explicit PEM trust bytes; ambient client credentials and
environment-selected CA paths are not used.

## Real checks and effect order

1. Reparse the entire plan, recheck host authority, copy and validate credentials
   and TLS inputs. There is no reusable preview approval token.
2. Bind the selected local port without address/port reuse. Keep the reservation
   through source checks and bundle creation. This is a point-in-time check;
   ownership of that port is not guaranteed after preparation returns.
3. Open/check the trusted root. For each source, spawn a disposable bounded
   worker. Credentials pass through process IPC, never command-line arguments.
4. Resolve DNS once, check **every** returned address against the host authority,
   and connect directly to one approved numeric address while keeping the original
   hostname for certificate verification and SNI. Reject mixed allowed/denied DNS
   answers. Never follow a redirect, discover a proxy or retry a failed probe.
5. Issue one authenticated read of the adapter's fixed observation route. Enforce
   verified TLS, HTTP success, JSON content, response size, unique keys and the
   existing adapter's schema/freshness semantics. Empty authoritative inventory
   is valid; `200 OK` with arbitrary JSON or stale data is not. Five seconds bounds
   each worker's DNS, connection, response and decoding; cleanup adds a short
   bounded join. Sources run sequentially, at most 16.
6. Only after all probes pass, create the new bundle. Release the port and close
   owned descriptors on every exit. Preparation has no listening service.

A probe authenticates and reads; the remote service may record that request in
its access/audit log. No source mutation endpoint is called. External response
bodies and exception text are never returned. `InstallError.report()` exposes
only fixed status/field/code values. Embedding frontends must mask secret input,
avoid logging arguments/local variables and render these reports rather than
tracebacks. This API does not promise to erase Python secret objects from RAM.

## Storage and failure semantics

The writer verifies a declared alias with FD-relative readlink, then opens the independently approved physical path. Only an exact absolute target or its direct relative spelling is accepted. A swapped alias is never used for writing. The writer walks the physical root with directory FDs and `O_NOFOLLOW` at every
component, checks ownership/modes, and uses only a validated child slug. An
existing destination is always refused, including directories, files, hard links
and symlinks. It never overwrites or cleans up an existing installation.

The bundle directory is `0700`, files `0600`, with bounded file count and byte
sizes. Fixed files are `plan.json`, `login.json`, `server.crt`, `server.key`, and
`complete.json`; each source adds `source-<id>.json` and `source-<id>.ca.pem`.
All JSON is serialized. Individual files are create-only and fsynced; the
completion manifest is written last, followed by directory fsyncs. It records
SHA-256 digests of all other bundle files.

This is not an atomic directory rename protocol. A failed write may leave a
private incomplete directory. Failure during completion publication is reported
as `publication_uncertain`; the engine never retries, deletes or reuses it.
A future bundle consumer must parse the completion manifest, verify every file
and its protection, and revalidate current policy/material/network state. Mere
marker existence, a digest, or a previous successful preview must never grant
installation authority. No bundle consumer is implemented here.

## Qualification and next stages

`tests/test_installation.py` covers strict hostile input, secrets as data,
capability refusal, traversal/symlink/hard-link refusal, a symlink swap, concurrent
creation, permission rechecks, partial/uncertain writes, port occupation and
release, certificate/key/name/expiry mismatch, mixed DNS answers and pinned
connections. Disposable HTTPS servers exercise both native adapters, real TLS
and Basic authentication, TLS-validation/request timeout cleanup, redirects, stale/invalid/oversized
responses and full preparation success/failure. No production source is contacted.

Run the declaration guard and normal suite with the same interpreter:

```sh
python scripts/check-test-collection.py --runner unittest tests
python -m unittest discover -s tests -v
python -m sphinx -n -W --keep-going -b html . /tmp/atraxis-docs
```

The CI core suite installs the explicit extra on Python 3.11 and 3.12. Package CI
also installs it from the built wheel before the source-archive suite. Existing
installed-CLI checks still exercise the default wheel separately.

Remaining work includes service configuration generation, account bootstrap, a batch frontend, privileged installation where needed,
startup/rollback, and disposable deployment recovery acceptance. Keep the
roadmap, operator docs, package/image publication, websites and demos aligned when
those stages become qualified releases. This candidate requires review before
merge and does not publish or deploy anything.
