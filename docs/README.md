# Atraxis user guide

Atraxis is experimental GPLv3 software. It collects observations in read-only
mode and exposes authorized inventory through its API, CLI, web and Textual
interfaces. Atropos is the independent service for rule lifecycle and workflows.

## Install and configure

- [Install from the public source repository](https://github.com/decryptus/atraxis#install-from-source)
- [Setup and diagnostics](setup.md)
- [Single-host Docker Compose installation](docker-compose.md)
- [Runtime configuration](../docs-runtime.md)
- [Source profiles and collection limits](source-configuration.md)
- [Covenant metrics through Prometheus](prometheus.md)
- [Browser source configuration](web-configuration.md)

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
