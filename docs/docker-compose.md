# Install with Docker Compose

Use the [operator journey](operator-journey.md) alongside this guide to provision
identities, add scoped inventory access before opening the service, and plan
credential/certificate renewal and recovery.

This single-host starter runs Atraxis and Atropos behind separate TLS
proxies. It builds local images from separate source checkouts; it publishes no
image. Use Docker Compose 2.24 or newer with BuildKit. This is not a release or
permission to deploy a particular site.

This Compose profile fixes its Python base image, Debian package snapshot and
Python dependency archives. The qualified platform is Linux amd64 with Python
3.12.16; other architectures need their own acceptance. Builds fail if locked
inputs are unavailable or do not match. Updating security fixes requires a reviewed
lock update and rebuilding the images; restarting a container does not update them.

The default is SQLite storage and its durable storage-backed queue. No Redis,
Auton, execution worker, action binding, automatic activation or intervention is
started. Optional profiles below start guarded workers after explicit configuration.
Do not replace a deployed Redis queue with this starter's storage queue.

## Prepare sources and a private site directory

Use matching, reviewed Atraxis and Atropos revisions. Atropos must include its
independent `client/` source. Build contexts must be clean source checkouts without
credentials or site data. Put all site files outside both checkouts. Record both
commit IDs before building; no branch is fetched or merged by Compose.

The public Atropos source revision qualified with this export is
`dedb114217c3df5fe0ab79d7acc53cd5636ebee0`:

```sh
git clone https://github.com/decryptus/atropos.git ../atropos
git -C ../atropos checkout dedb114217c3df5fe0ab79d7acc53cd5636ebee0
```

Copy `deploy/compose/site.env.example` to a private operator-owned file outside
the repository. Set absolute `ATROPOS_SOURCE` and `ATRAXIS_SITE_DIR` paths.
The example exposure is **127.0.0.1:8443**. Bind a different interface only after
reviewing site firewall, DNS, certificate trust and permitted clients.

Create these four private directories under `ATRAXIS_SITE_DIR`:

| Directory | Required files |
| --- | --- |
| `web/` | `atraxis.yml`, `users.htpasswd`, `web-rules.yml`, `engine-client.yml`, `engine.token`, `engine-ca.pem` |
| `engine/` | `engine.yml` |
| `web-tls/` | `server.crt`, `server.key` |
| `engine-tls/` | `server.crt`, `server.key` |

Copy the corresponding `*.yml.example` files from `deploy/compose/`, removing
the `.example` suffix. Replace every placeholder. Provision a strong
HTTPdis-compatible login hash and a separate random engine token using trusted
administration tooling. Never put secret values in shell arguments, Git, URLs,
Compose environment variables or logs. Store only the token's SHA256 digest in
the engine identity. Username mapping and engine subject/organization must agree;
the example grants nothing. Add only the intended engine resource/read grants.

The engine certificate must cover DNS name `engine-tls`; `engine-ca.pem` must
trust its issuer. The web certificate must cover the browser hostname. Include
the leaf/intermediate chain as required by your CA. Neither hop disables
certificate or hostname checks. Keep private keys separate by component.

Use UID/GID **10001:10001**, directory mode `0700`, private file mode `0600`
for service-owned profiles, secrets and keys. Root ownership is supported by
some profile validators but does not make a `0600` file readable by UID 10001.
Keep proxy configuration files from this checkout readable. Bind mounts reject
missing paths rather than silently creating directories. Rootless/user-remapped
Docker requires the corresponding host ownership mapping and site acceptance.

## Build, initialize and start

Run from the Atraxis checkout. In the commands below replace
`/private/site.env` with your protected operator file:

```sh
docker compose --env-file /private/site.env -f deploy/compose/compose.yml config --quiet
docker compose --env-file /private/site.env -f deploy/compose/compose.yml build
```

For a **new, empty installation only**, initialize the named data-volume roots.
These explicit one-off commands run as root only to set ownership of the two
mount points (with CHOWN on those one-off helpers); long-running services use
UID 10001. Start the two proxies first: even an offline one-off application
command needs its configured network namespace. The applications are not yet running:

```sh
docker compose --env-file /private/site.env -f deploy/compose/compose.yml up -d --no-build web-tls engine-tls
docker compose --env-file /private/site.env -f deploy/compose/compose.yml run --rm --no-deps --user 0:0 --cap-add CHOWN --entrypoint sh web -c 'chmod 700 /var/lib/atraxis && chown 10001:10001 /var/lib/atraxis'
docker compose --env-file /private/site.env -f deploy/compose/compose.yml run --rm --no-deps --user 0:0 --cap-add CHOWN --entrypoint sh engine -c 'chmod 700 /var/lib/atropos && chown 10001:10001 /var/lib/atropos'
docker compose --env-file /private/site.env -f deploy/compose/compose.yml run --rm --no-deps web check-config --config /etc/atraxis/atraxis.yml
docker compose --env-file /private/site.env -f deploy/compose/compose.yml up -d --no-build
```

Compose startup order is not readiness. Through verified HTTPS, test an actual
login, invalid-login refusal and intended scoped access. An empty source list
returns **503 readiness**, even when the HTTP service is alive. Add your read-only
HTTPS sources using the [source guide](source-configuration.md).

The basic mapping enables rule reading only in the web UI. Browser changes need
a separately configured exact public origin and current engine permissions.
Shared inventory remains the starter's behavior until you explicitly configure
[account scopes](accounts.md) and [inventory access](web-inventory.md).
Do not add untrusted users before doing so. Account bootstrap is a trusted
operator action; Compose creates no administrator, default password or grant.
Store writable account databases under `/var/lib/atraxis`; profiles remain under
the read-only `/etc/atraxis` mount. In-browser source/grant profile editing needs
a separately designed writable mount layout; this starter does not enable it.

Both application listeners stay on **127.0.0.1 in their own proxy's network
namespace**. Only web TLS is published; the engine TLS endpoint is internal to
the project network. The network allows outbound HTTPS source connections.
No Docker socket, host networking or privileged service is mounted. Each service
has a read-only root, temporary `/tmp`, dropped capabilities, no new privileges,
512 MiB memory, 2 CPUs, 128 PIDs and bounded Docker logs. Restart policy is
deliberately `no`; an operator inspects failures and uncertain outcomes before
restarting. Resource limits are starter bounds, not a sizing guarantee.

## Stop, recreate and rotate

Use `stop` for planned maintenance and `up -d --no-build` to start explicitly.
If either worker profile has been used, stop **all** services with
`docker compose --env-file /private/site.env -f deploy/compose/compose.yml --profile '*' stop`.
Stopping the API alone does not pause independently running workers.
After image/configuration changes, use `up -d --no-build --force-recreate` for
the whole project: app/proxy pairs must be recreated together because they share
network namespaces. Named data volumes survive normal recreation and `down`.
**Do not use `down --volumes` on an installation you intend to preserve.**
The project name identifies its volumes; changing it starts a different store.

Mounting the private profile directories rather than individual secret files
allows same-filesystem atomic credential replacement to become visible.
Login replacement still requires a web process restart; engine tokens retain
the existing [rotation procedure](rule-remote.md#rotate-a-file-backed-engine-token).
Record host file changes externally without recording secrets or password hashes.
No integrated credential-change audit or cross-replica invalidation is added.

## Enable guarded workers explicitly

Two optional profiles reuse the exact engine image, private engine configuration
and `engine-data` volume. Neither starts with the default `up` command:

| Profile | Service | Behavior |
| --- | --- | --- |
| `simulation` | `test-worker` | Runs `rules-worker --test`; only registered simulators, separate test queue and locks |
| `execution` | `worker` | Runs `rules-worker`; rechecks current identity, rights, binding, active generation and freshness before each attempt |

Each container has one worker and the same UID, read-only root, 512 MiB, 2 CPU,
128 PID and no automatic restart protections as the engine. Four replicas mean
four workers; limits apply **per container**, not to the deployment as a whole.
Workers have no published listener. Their network permits approved outbound HTTPS
connections; their `127.0.0.1` is not the engine proxy's network namespace.

First configure the private engine profile with approved resources, scoped rights,
exact bindings and, when needed, an external Auton executor using the
[engine executor guide](https://github.com/decryptus/atropos/blob/main/docs/user/execution-adapters.md).
Keep its credentials and CA under the private `engine/` directory, readable by
UID 10001. This Compose file does not provision a daemon, action or target.
Saving a rule, activating it and admitting an intervention remain separate actions.

Configure and inspect the outstanding-work limit before admitting work. For the
**unchanged example namespace and SQLite/storage queue only**:

```sh
docker compose --env-file /private/site.env -f deploy/compose/compose.yml run --rm --no-deps -e ATROPOS_STORAGE_BACKEND=sqlite -e ATROPOS_STORAGE_URL=sqlite:////var/lib/atropos/engine.db -e ATROPOS_QUEUE_BACKEND=storage engine --namespace operator-site queue-configure --max-outstanding 24
docker compose --env-file /private/site.env -f deploy/compose/compose.yml run --rm --no-deps -e ATROPOS_STORAGE_BACKEND=sqlite -e ATROPOS_STORAGE_URL=sqlite:////var/lib/atropos/engine.db -e ATROPOS_QUEUE_BACKEND=storage engine --namespace operator-site queue-configure --test --max-outstanding 24
```

These trusted-host commands use environment role settings and `--namespace`, not
`engine.yml`. If either differs, use the matching engine administration procedure;
do not accidentally configure a different database or namespace. A zero limit
pauses new admissions without evicting accepted or uncertain work. It does not
pause execution. The example value 24 is a starting bound, not a sizing guarantee.

Start only the mode intended for this installation:

```sh
docker compose --env-file /private/site.env -f deploy/compose/compose.yml --profile simulation up -d --no-build test-worker
docker compose --env-file /private/site.env -f deploy/compose/compose.yml --profile execution up -d --no-build --scale worker=4 worker
```

Starting a worker can immediately process previously admitted work. Review queued
and uncertain runs first. A receipt confirms admission, not successful execution.
Test mode never falls back to a real action. Preserve and reconcile uncertain
outcomes before allowing new work; do not blindly retry a remote effect.
Restart workers after changing adapter definitions. During whole-project
recreation, repeat the chosen profiles and replica count explicitly. Use the
engine's `storage-pause`/`storage-status` procedure before planned maintenance,
wait for in-flight work to finish, then stop all profiles; inspect remaining
uncertain work before the explicit reconciled resume.

## Cold backup and restore

This starter offers a **cold** operator backup, not automatic or hot backup.
Stop the whole project, including both optional worker profiles, and confirm no service or other process writes either
volume. Record source commits/image IDs, project name, volume names and the
configuration revision. Securely back up both complete named volumes, including
any SQLite WAL/SHM companions, plus the four private site directories and your
operator environment file. TLS keys and credentials require protected storage.
Use your approved volume-backup tooling; copying a live main SQLite file alone
is insufficient. Never purge uncertain-work evidence.

Restore into separate, empty volumes and private site directories while services
remain stopped. Preserve ownership/modes and the recorded configuration/version.
Run SQLite integrity and foreign-key checks, then follow the existing
[account restore](accounts.md) and engine recovery/current-rights procedures
before enabling execution. Reconnect only intended sources and destinations;
old backups do not restore revoked authority. Never blindly retry uncertain
external effects. Do not overwrite the working installation to test a restore.

Preserve the inventory access profile, source credentials and CA files with their
restricted ownership and modes. Review current account and source permissions
before reconnecting. Restoring configuration does not restore an in-memory
inventory observation: the source remains unavailable until a successful fresh
collection. Check the source timestamp separately from the receipt timestamp.
A later collection failure retains the last valid snapshot as historical data;
it must not be treated as current or as permission to execute an action.

When replacing the host, retain the exact application and proxy images as well as
their IDs and source revisions. Transfer the complete cold backup and its recorded
hashes through your protected backup channel; check every file before loading an
image or populating a destination. Prepare Docker, empty volumes and private site
directories on the replacement, and restore the required ownership and modes.
Use the recorded images with `--no-build --pull never`; rebuilding from a current
branch is a different software change and needs its own qualification.

Start only the API and TLS services for inspection. Confirm that execution remains
paused in both engine namespaces, review current authority and external outcomes,
and explicitly resume only after reconciliation. Starting a worker is not a
recovery check unless its preserved pause has first been verified. A restored
queued run must still pass current execution permissions when eventually resumed.

The controlled Compose case exercises HTTPS login/refusal, empty-source readiness,
engine rule/history/audit preservation through recreation and cold restoration into
a second project's empty volumes, plus account revision/audit preservation,
continued refusal of a revoked account, current engine grant checks, SQLite
integrity and runtime protections. Its worker extension uses synthetic
`core.echo` work to check separate real/test queues, execution-time revocation,
deduplication, recreation and retained run outcomes after cold restoration.
It does not establish live Auton intervention or downstream delivery acceptance.
Its archive helper is a synthetic test fixture,
not a general operator backup/import command. Use your approved backup tooling. Physical host/disk loss,
multi-host TLS, live destinations and site backup/restore acceptance remain open.

An additional controlled test transfers the exact images, site files and cold
volumes to a separate disposable host. It checks preserved authentication, audits,
queued work and execution pause, followed by explicit resume with a revoked action
refused. A synthetic HTTP request whose response was lost keeps its uncertain
outcome and target lock after restoration. Reconciliation requires current rights,
the exact reviewed state and an explicit verification reference; it records an
operator attestation without replaying the request or executing remaining steps.
Check the external system independently before making that attestation on your
site: a copied receipt is historical evidence, not proof of its current state.
This is evidence for the tested SQLite/storage-queue installation; it does
not qualify your site's host-loss recovery, external authority, certificate issuer
or an uncertain external effect. See the contributor
[separate-host recovery scope](contributing.md#recovery-on-a-separate-host).
