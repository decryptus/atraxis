# Run the Atraxis Docker image

The Docker Hub target is [decryptus/atraxis](https://hub.docker.com/r/decryptus/atraxis).
The first intended stable image is **1.0.1**, paired with Atropos 1.0.0. This
checkout is still `1.0.1.dev0`: image builds are tested, but stable publication
requires the finalized package and its source tag. Check available Docker Hub
tags before pulling; an empty repository does not contain a runnable image.

The Linux amd64 image contains Atraxis and `atropos-client[observations]==1.0.0`.
It provides the inventory/API/web service and CLI commands. The engine, ZEN,
Auton server, Prometheus, Covenant and Textual terminal UI are not bundled.
Direct Monit-Docker and CertLord connections need neither Prometheus nor Covenant.
Atropos remains an optional separately configured service for rule management.

Once the stable tag is available:

```sh
docker pull decryptus/atraxis:1.0.1
docker run --rm --network none decryptus/atraxis:1.0.1 --help
```

Use an exact version tag or digest in deployments. `latest`, `1` and `1.0` are
moving aliases. Processes run as UID/GID **10001:10001**. Mount prepared private
configuration read-only at `/etc/atraxis`, and retain account/audit state under
`/var/lib/atraxis`. Private bind-mounted directories/files need that UID's access,
with directories `0700` and files `0600`. Do not put credentials into images.

## Linux host example

Prepare `atraxis.yml`, credentials and source profiles following the
[setup guide](setup.md) and [operator journey](operator-journey.md). Retain
`general.listen_addr: 127.0.0.1` and port `9810`; use absolute persistent account
paths below `/var/lib/atraxis`. Empty sources explicitly report not ready.

```sh
export ATRAXIS_CONFIG_DIR=/absolute/private/atraxis-config
export ATRAXIS_VERSION=1.0.1
docker compose -f deploy/docker/compose.yml up -d
```

The example uses Linux host networking and relies on the configured loopback
listener. Put a TLS reverse proxy on the same host for remote access, with the
correct origin configuration for rule forms. Do not expose unencrypted Basic
authentication on a public interface. The [operator Compose guide](docker-compose.md)
provides the alternative shared-network-namespace proxy layout and coordinated
Atraxis/Atropos recovery, using builds from pinned source checkouts.

The same image can run local account/source administration by overriding the
command. Review and apply operations remain separate, and privileges come from
configured identities. Image availability does not grant permissions, configure
sources, activate rules or dispatch actions. Back up application state and verify
version compatibility before replacing a deployed container.
