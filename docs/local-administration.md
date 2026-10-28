# Local source and inventory administration

Use `sources-admin` to add, update or remove saved collection sources and
`inventory-admin` to assign or revoke inventory read grants. Both are local
trusted-operator commands in the base package; Textual is optional. They read
private files and the account directory without contacting an observation source,
restarting the daemon or submitting interventions.

## Provision authority once

The host owner provisions the account directory, source credentials, approved
connections and administration scopes. A command cannot appoint an administrator,
change the connection catalog, create membership or grant engine permissions.
Use private directories (`0700`) and regular files (`0600`) on a supported local
POSIX filesystem. The operator needs the same file and lock access as the existing
web writer. These tools are not an OS privilege boundary.

For sources, use the profile documented in [browser configuration](web-configuration.md),
with a separate UID identity. Obtain the intended operator's numeric UID with
`id -u`; replace the illustrative `1000` below:

```yaml
version: 1
store: accounts.db
identities:
  - uid: 1000
    subject: alice
    organization: default
administrators:
  - subject: alice
    organization: default
connections:
  monitor:
    kind: monit_docker
    origin: https://monitor.example.org
    credentials_file: monitor-credentials.json
```

The mapped subject must be an enabled member of the named organization and an
explicit installation administrator. Bind the profile to the existing daemon
configuration with `general.configuration_web_file: configuration-admin.yml`.
Despite its name, that field also selects the local administration profile.
Omit `configuration_web_origin` for CLI-only administration: no configuration web
routes or navigation link are enabled. A web origin still requires a profile.
Relative credential and CA paths resolve beside the administration profile.

For inventory, add `uid: 1000` and `subject: alice` as a separate identity in the
[version-3 inventory profile](inventory-administration.md#local-textual-editor).
The mapped account needs explicit inventory administration targets and current
membership. Configuration authority and inventory authority are independent.
Web username mappings and `USER`/`LOGNAME` cannot authorize either local command;
there is no actor override.

## Review a source change

Inspect the saved source summaries, approved connections and latest audit:

```sh
atraxis sources-admin --config /private/daemon.yml show
```

Create a private change request, for example `/private/source-change.yml`:

```yaml
operation: source.add
source_id: monitor-a
connection: monitor
timeout: 10
poll_interval: 60
max_age: 180
```

Use `source.update` with the same fields to edit an existing source. Other source
limits are retained. Removal needs only `operation: source.remove` and
`source_id`. URLs, credentials, metric selections and CA paths come exclusively
from the approved connection; they are not request overrides.

```sh
atraxis sources-admin --config /private/daemon.yml check --file /private/source-change.yml
atraxis sources-admin --config /private/daemon.yml apply --file /private/source-change.yml \
  --expected-revision REVIEW_REVISION --confirm
```

Read the `check` output before replacing `REVIEW_REVISION` with its `revision`.
That digest binds the exact request, target paths, actor, account directory,
authority profile and saved configuration. It is distinct from the saved-state
revision returned by `show`. A changed request or revision requires another review;
never automatically rerun `check` and `apply` to bypass a conflict.

Success returns the audit `event_id`, `saved: true` and `runtime_changed: false`.
Saving is atomic with its audit event. It preserves other configuration sections
but normalizes YAML formatting and comments. It does not grant readers access.
The CLI cannot tell whether the running process matches the saved configuration.
Inspect source dependencies and pending work before an explicit daemon restart.
Only a subsequent fresh observation establishes source availability.

## Review inventory access

Inspect only the organizations, grants and recent audit within your current
administration bounds:

```sh
atraxis inventory-admin --config /private/inventory-access.yml show
```

For a recipient who already belongs to the organization, a private request can be:

```yaml
operation: grant.add
organization: default
subject_kind: user
subject: reader
targets:
  - kind: source
    id: monitor-a
```

The source must already belong to the host-provisioned inventory catalog and be
within the administrator's targets. An account `group` is also supported. Exact
resource selectors follow the [inventory scope contract](web-inventory.md).

```sh
atraxis inventory-admin --config /private/inventory-access.yml check --file /private/grant-change.yml
atraxis inventory-admin --config /private/inventory-access.yml apply --file /private/grant-change.yml \
  --expected-revision REVIEW_REVISION --confirm
```

To revoke, use `operation: grant.remove` and `grant: GRANT_ID`, where `GRANT_ID`
is the exact grant `id` from `show`, then review and apply again. Overlapping grants
can retain access. Subsequent inventory reads use the new profile without a daemon
restart; previously delivered data is not recalled.

## Failures and limits

Both commands output JSON on success (exit `0`), return `3` for denied authority,
and `2` for invalid input, conflicts or an unavailable/unconfirmed outcome. Errors
do not expose exception text, credential values or storage URLs. Requests are
bounded at 32 KiB and reject duplicate YAML keys, aliases and symlinks. Current
profile, source, history and account bounds remain in force.

Apply requires both the reviewed revision and `--confirm`; it never retries.
An unconfirmed write may already be saved. Inspect `show` and the audit before
deciding whether another change is needed. Keep backups: neither command trims
history. Coordinate manual file edits while administration is stopped; locks
serialize cooperating application writers, not arbitrary editors.
