# Atraxis user guide

Atraxis is experimental GPLv3 software. It collects observations in read-only
mode and exposes authorized inventory through its API, CLI, web and Textual
interfaces. Atropos is the independent service for rule lifecycle and workflows.

[Release candidate scope and limitations](v1-candidate.md).

## Install and configure

- [Operator journey: installation, access, renewal and recovery](operator-journey.md)
- [Install from the public source repository](https://github.com/decryptus/atraxis#install-from-source)
- [Setup and diagnostics](setup.md)
- [Single-host Docker Compose installation](docker-compose.md)
- [Runtime configuration](../docs-runtime.md)
- [Choose a source adapter: direct access or optional Prometheus](source-configuration.md#choose-an-adapter)
- [Source profiles and collection limits](source-configuration.md)
- [Selected numeric metrics through Prometheus](prometheus.md)
- [Complete Monit-Docker inventory through Covenant and Prometheus](prometheus-monit.md)
- [Complete CertLord inventory through Covenant and Prometheus](prometheus-certlord.md)
- [Metric alerts and Alertmanager delivery](prometheus-alerting.md)
- [Browser source configuration](web-configuration.md)
- [Local source and inventory administration](local-administration.md)

## Observe and operate

- [Collection, freshness and inventory](collection-inventory.md)
- [Command-line usage](cli.md)
- [Web inventory](web-inventory.md)
- [Inventory search](inventory-search.md)
- [Accounts and permissions](accounts.md)
- [Inventory grant administration](inventory-administration.md)
- [Offline retention reports](retention.md)
- [HTTP transport behavior](http-transport.md)

## Work with rules

- [Install the Atropos client and connect to the service](rule-remote.md)
- [Browser rule workbench](web-rules.md)
- [Rule lifecycle and explicit activation](rule-lifecycle.md)
- [Local operator CLI](rule-console.md)
- [Textual terminal interface](textual.md)
- [Explicit workflow dispatch](rule-dispatch.md)
- [Verified observations and isolated tests](observations-and-tests.md)

Saving or testing a draft does not activate it or execute an intervention.
Follow each guide's current permission, TLS and configuration requirements.

## Development

Use the separate [contributor guide](https://github.com/decryptus/atraxis/blob/main/CONTRIBUTING.md) for architecture and
testing, or the [public roadmap](../ROADMAP.md) for planned scope.
