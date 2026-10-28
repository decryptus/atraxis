# Configure collection from the browser

Source configuration is optional and disabled by default. An explicitly authorized
installation administrator can add, edit or remove saved collection sources at
`/ui/configuration`. Every change requires a review and separate confirmation.
Saving never probes a source, starts an intervention or restarts the daemon.
The page distinguishes saved settings from the sources running in this process.

## Enable the administration page

First provision accounts, authenticated HTTP usernames and source credentials on
the host. This authority is installation-wide: organization membership alone,
account delegation and inventory read grants do not confer configuration rights.
Keep the daemon configuration and administration profile in private directories
(mode 0700) owned by the daemon user, with regular files mode 0600. Use the same
protected directories for the account adapter's writable files.

Create a private administration profile beside the daemon configuration:

```yaml
version: 1
store: accounts.db
identities:
  - username: alice
    subject: alice
    organization: default
administrators:
  - subject: alice
    organization: default
connections:
  monitored-host:
    kind: monit_docker
    origin: https://monitor.example
    credentials_file: monitor-credentials.json
    ca_file: monitor-ca.pem
```

The account store uses the existing generic [storage adapter](accounts.md)
configuration; a path selects SQLite by default. The mapped account must currently
have enabled membership in the named organization and be listed explicitly in
`administrators`. An empty administrator list revokes all configuration access.
Host administrators own these mappings and the approved connection catalog.
Connection types are `monit_docker`, `certlord` and `prometheus`; their credential and CA formats
follow [source configuration](source-configuration.md). `ca_file` is optional for
system-trusted HTTPS. Paths resolve beside the administration profile.

The same profile supports [local CLI administration](local-administration.md) with
an explicit `uid` identity instead of a web `username`. An identity cannot contain
both. A profile without `configuration_web_origin` enables only local administration;
the page requires the paired settings below.

Add the paired settings to the existing daemon's `general` section:

```yaml
configuration_web_file: configuration-admin.yml
configuration_web_origin: https://atraxis.example
```

Use the actual browser origin, without a path. Retain the existing authentication
and trusted TLS ingress configuration; no forwarded username becomes an identity.
Only a literal loopback address may use HTTP for disposable local testing.
Restart explicitly to enable the page. The Inventory page then links to it.

## Review and save

Select **Add source**, choose a host-approved connection and enter a source ID,
request timeout, collection interval and maximum observation age. Their limits are
1–60, 1–3600 and 1–86400 seconds respectively, with timeout ≤ interval ≤ age.
Review the current and proposed values, then check the confirmation and save.
Editing preserves other source limits. Removal also has a separate review.
Credentials and credential paths are never form fields or rendered values.

Saving changes the existing daemon configuration and appends its audit event in
one atomic file replacement. Other configuration sections remain intact, although
YAML formatting/comments are not retained. Changes apply only after an explicit
daemon restart; inspect pending work and source dependencies before restarting.
Inventory read grants, rule scopes and intervention bindings remain separately
managed. Removing a source does not rewrite those authorities or cancel a run.

Concurrent changes or revoked rights invalidate a reviewed form. Open a fresh
form instead of resubmitting it. An unconfirmed write may already be saved: inspect
the configuration and recent history before another attempt. Forms are single-use
and expire after 15 minutes. Coordinate host edits while the daemon is stopped;
filesystem locks serialize cooperating application writers, not arbitrary editors.

The file is bounded at 64 KiB and its configuration history at 100 events; reaching
a bound refuses another save without trimming evidence. Retention/archival tooling
is not yet provided. The page displays the latest 20 events. Back up the complete
configuration, authority profiles and credentials in a protected archive before an administrator-maintained retention change.
This page configures sources only; engine transport, authentication, connection
catalogs and account authorities continue to be provisioned on the host.
