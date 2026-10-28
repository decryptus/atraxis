# Operator installation and recovery

Start here to install a private Atraxis instance, give a reader access to one
source, and maintain those accesses. Atraxis collects observations; Atropos owns
rule permissions and workflow execution. A working login or a fresh inventory
does not authorize an intervention.

## Choose the installation path

| Need | Start with | Expected result |
| --- | --- | --- |
| Try the interface without installing | [Public read-only demo](https://atraxis.io/demo.html?lang=en) | Public measurements and labelled synthetic scenarios; no private account |
| Collect sources with an existing service supervisor and HTTPS proxy | [Source installation](https://github.com/decryptus/atraxis#install-from-source), then [setup](setup.md) | A daemon configuration; you provide the service and verified HTTPS ingress |
| Run Atraxis and Atropos on one host | [Docker Compose](docker-compose.md) | Separate application/TLS pairs and persistent volumes; workers stay disabled |

Use the Compose guide for build, volume ownership and startup commands. Complete
the access configuration below **before opening the installation to users**.
Keep the chosen source revisions, project name and configuration revision in your
private operator record. Configuration files and credentials belong outside the
source checkouts and build contexts.

## Provision each identity separately

| Connection or authority | Provision | Does not grant |
| --- | --- | --- |
| Browser or inventory CLI → Atraxis | A login in `users.htpasswd`; the CLI uses a separate private JSON file with that login | Organization membership, inventory scope or engine rights |
| Atraxis → each observation source | A source-side read identity and private `credentials_file` | Atraxis login or an action on the source |
| Atraxis account directory | Stable subject, organization and membership | Inventory, rule or intervention permissions |
| Inventory access profile | Login-to-subject mapping and explicit read targets | Account administration or engine permissions |
| Atraxis → Atropos, when enabled | A dedicated token, exact subject/organization mapping, engine resources and grants | Implicit activation, execution or account administration |

Use a strong HTTPdis-compatible password hash for the inbound login. Outbound
Basic credentials use the private JSON format from the [source guide](source-configuration.md),
not the password hash. Generate engine tokens independently; keep their raw values
only in protected token files and their SHA-256 digests in engine identities.
The Compose examples contain placeholders and grant no engine permissions.

Use trusted administration tooling to create these files without putting secrets
in command arguments, URLs, terminal recordings or Git. Directory mode `0700` and
file mode `0600` are the starting permissions. The reading service must own or be
able to read each file: the Compose service UID/GID is `10001:10001`; a root-owned
`0600` file is not readable by that UID. Keep CA bundles and private TLS keys in
their designated component directories. See the [Compose file layout](docker-compose.md#prepare-sources-and-a-private-site-directory).

## Give a reader one inventory source

Once the host owner has provisioned approved connections, inventory catalog and
administration authority, use [local administration](local-administration.md) to
review source additions and read grants from the CLI. Adding a source does not
grant access; each operation has a separate review and audit. The bootstrap
example below remains useful for an installation with no delegated operator yet.

This example uses a **new** account store, login `reader-login`, subject `reader`,
organization `default` and configured source ID `monitor-a`. Replace these names
consistently for your installation. `init` creates the `default` organization;
it does not create a password or an administrator. Do not initialize over an
existing account store or switch an existing deployment to a new directory.

For Compose, after building and initializing the data volumes and starting the
TLS proxies as described in the installation guide, run these trusted local
commands while the application services are still stopped:

```sh
docker compose --env-file /private/site.env -f deploy/compose/compose.yml run --rm --no-deps web accounts --store /var/lib/atraxis/accounts.sqlite3 init --subject reader
docker compose --env-file /private/site.env -f deploy/compose/compose.yml run --rm --no-deps web accounts --store /var/lib/atraxis/accounts.sqlite3 contexts --subject reader
```

For a native installation, use the equivalent `atraxis accounts --store PATH`
commands in a private writable data directory owned by the service account.
Keep account data outside a read-only configuration mount. Local account commands
use trusted filesystem authority; they are not a browser self-registration API.

Save this private profile as `web/inventory-access.yml` under the Compose site
directory. Its SQLite path is the **container** path, not a host path:

```yaml
version: 3
store:
  backend: sqlite
  url: sqlite:////var/lib/atraxis/accounts.sqlite3
identities:
  - username: reader-login
    subject: reader
servers: []
resource_groups: []
grants:
  - organization: default
    subject_kind: user
    subject: reader
    targets:
      - kind: source
        id: monitor-a
administrators: []
audit: []
```

Add `inventory_access_file: /etc/atraxis/inventory-access.yml` to the existing
`general` mapping in `web/atraxis.yml`, preserving its other settings. Provision
`reader-login` in the login file and configure the source with exactly
`source_id: monitor-a`. A whole-source grant includes its present and future
resources and metadata. Use [narrower resource grants](web-inventory.md#servers-resources-and-resource-groups)
if that scope is too broad. With no access profile, authenticated users share the
configured inventory; an enabled but invalid profile denies scoped reads.

Validate the prepared installation before starting the applications:

```sh
docker compose --env-file /private/site.env -f deploy/compose/compose.yml run --rm --no-deps web doctor --config /etc/atraxis/atraxis.yml
docker compose --env-file /private/site.env -f deploy/compose/compose.yml run --rm --no-deps web doctor --config /etc/atraxis/atraxis.yml --probe-sources
```

The first command checks local configuration; the second explicitly contacts the
configured source observation endpoints. Neither proves browser login, TLS ingress
or engine permissions. Inspect the report and [exit codes](setup.md#check-configuration-and-connections)
before following the normal startup procedure.

Account administration is a separate choice. To delegate it, follow
[account bootstrap and bounded delegation](accounts.md#entity-administration).
The read-only example deliberately creates no administrator. Do not give the
reader global account authority just to make inventory visible.

## Connect rule access only when needed

Configure a [browser rule mapping](web-rules.md#connect-accounts-to-atropos) or
[local/remote operator profile](rule-remote.md). The dedicated engine token must
represent the exact mapped subject and organization. Inventory grants are not
Atropos grants; provision the intended engine resources and operations separately.
Leave browser mutation origins and worker profiles disabled until their separate
workflows are required and reviewed.

If account revocation must also govern rules, add `accounts_file` to the Atraxis
rule mapping. For direct engine access and queued work, configure Atropos and its
workers to read the **same live account authority**, as described in
[membership enforcement](accounts.md#require-membership-for-rule-access).
The stock Compose layout does not share the web account volume with the engine;
that extension needs an explicit read-only mount and site verification. Copying
the account database to the engine is not live authority synchronization.

## Check the installed result

Use the actual verified HTTPS ingress and fresh browser/client contexts:

| Check | Required observation |
| --- | --- |
| No login or wrong password | Authentication refused; no private inventory |
| `reader-login` in `default` | Only the intended source/resources are visible |
| Another organization or an unmapped login | Access denied, without fallback to shared inventory |
| Source observations | Current timestamps and valid data; distinguish retained historical values and unknown data |
| `/healthz` versus `/readyz` | Liveness alone is insufficient; an empty source list or unavailable scoped sources are not ready |
| Optional rule read | Correct subject/organization and scoped engine rights; no activation or dispatch |

For the CLI, prepare the separate [inventory client profile](cli.md#separate-client-settings),
then use `atraxis inventory --config /private/client.yml -o default`. Keep any
result containing installation data private. An exit code indicating fresh
inventory does not assert that every workload is healthy.

## Renew and revoke access

Make one planned credential change at a time and preserve stable identities and
unrelated grants. Atomic replacement means preparing a complete protected file
on the same filesystem before replacing the configured path. Check actual refusal
of the previous credential and access with its replacement through verified TLS.
Record identity, operator, time and outcome, without recording secret material.

| Material changed | How the change takes effect | Procedure |
| --- | --- | --- |
| Atraxis login file | Loaded at HTTP process startup; file replacement alone leaves the old password active | [Stop access, replace, restart and verify](setup.md#rotate-the-atraxis-login-password) |
| Source Basic credentials at the same path | Reread for each outbound request | Replace source-side access and the private JSON file; recheck with `doctor --probe-sources` and the running inventory |
| Source CA bundle at the same path | Read when constructing each request's TLS context | Coordinate CA trust and server-chain renewal, then verify source reads; changing the configured path requires configuration reload via restart |
| File-backed engine token at the same path | Client rereads per call; engine identity mapping reloads on subsequent requests | [Revoke the old digest, replace the token and verify](rule-remote.md#rotate-a-file-backed-engine-token) |
| Account membership or inventory grant | Checked on subsequent scoped requests; engine enforcement requires its own live authority configuration | [Account review/apply](accounts.md#review-and-apply) and [inventory grants](inventory-administration.md) |
| Web or engine TLS certificate/key | Publishing files alone does not reload the Compose nginx process | Use the planned maintenance sequence below |

For source credential rotation, coordinate the source's own revocation procedure;
Atraxis cannot revoke an account on another service. Any transition failure must
remain visible as unavailable/historical data. Preserve configured Covenant
fallback behavior: retained values are not evidence that a new read succeeded.

For Compose TLS renewal, prepare matching certificate/key pairs and required
chains using your certificate issuer. Verify the web hostname and internal
`engine-tls` name separately. During planned maintenance, pause/drain any enabled
workers using the engine procedure, stop all profiles, replace the protected files,
and recreate the application/proxy pairs together following
[stop, recreate and rotate](docker-compose.md#stop-recreate-and-rotate).
Verify HTTPS trust and authenticated access on both hops before resuming work.
Retain data volumes, project identity and uncertain-work evidence. This is a
maintenance procedure, not automated issuance, hot reload or a qualified CertLord
deployment integration. A CA change also requires coordinated client trust updates.

Revocation affects later authorization checks; it does not erase pages already
delivered, cancel an external action already started or undo a completed effect.
After a lost mutation response, inspect durable state and audit before deciding
what to do. Do not repeat an uncertain operation simply because login works again.

## Recover without replacing the working installation

For a forgotten password, use the login rotation procedure through trusted host
administration. For disabled membership or lost delegated administration, use
local `accounts export`, `check`, then exact-revision `apply`; retain identity IDs
and audit. Do not rerun `init`, invent a new administrator identity or disable the
access profile to regain access. An existing installation never needs a new
account database merely to recover an account.

For data or host recovery, keep account backup and whole-installation backup
distinct. [Account backup](accounts.md#complete-sqlite-account-backup-and-restore)
preserves its directory revision and audit; it does not contain engine work,
configuration or credentials. A coordinated [cold Compose backup](docker-compose.md#cold-backup-and-restore)
requires stopped writers and both complete data volumes plus the protected site
files. An exported editable directory is not a full account backup.

Restore into separate empty destinations with services stopped. Verify integrity,
source revisions, ownership, current membership and grants, and reconcile queued
or uncertain engine work before any explicit resume. An older backup can contain
credentials or rights revoked since it was taken; restoring bytes does not make
that authority current. Keep the existing installation and evidence until the
replacement is verified. Physical host-loss recovery, live execution targets and
downstream notification delivery still need separate operational qualification.

The [replacement-host procedure](docker-compose.md#cold-backup-and-restore) also
requires the recorded images and verified complete backup before any service
starts. Retain execution pause through inspection; a successful login or restored
rule history is not permission to resume pending actions. A separate-host test
exercises this sequence with disposable synthetic state. Site authority changes
since backup and uncertain external effects still require operator reconciliation.

The access and renewal examples have been exercised with disposable Compose
services, synthetic credentials and locally issued test certificates. This checks
the documented boundaries; it does not provision your site's identity authority,
automate certificate issuance or validate recovery on your host. Contributor
[acceptance scope](contributing.md#operator-access-acceptance) records the checks
and their limits.
