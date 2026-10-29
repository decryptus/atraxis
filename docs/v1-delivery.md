# First stable release: scope and delivery gates

Scope baseline: 29 October 2026. This is the bounded delivery plan for Atraxis
1.0 and its matching Atropos client/service. It does not change package versions,
declare current experimental formats stable, or authorize a release/deployment.
The matching [Atropos scope](https://github.com/decryptus/atropos/blob/main/docs/contributor/v1-delivery.md)
owns engine, workflow and storage decisions. Site evidence stays in private
operational records; this document contains no deployment credentials or results.

## Included functionality

Finish and qualify the implemented capabilities below. Do not add a new feature
to this delivery baseline merely because another qualification uncovers future work.

| Area | Included in the baseline | Boundary |
| --- | --- | --- |
| Observation | Existing read-only Monit-Docker, certificate and selected Prometheus adapters, including Covenant metrics | Available empty data, unavailable data and retained history remain distinct; collection never authorizes an intervention |
| Accounts and inventory | Tenants, organizations, groups/subgroups, memberships, bounded delegation, inventory read grants and section searches | Current authority is rechecked; no implicit cross-organization access |
| Administration | Existing web/CLI/Textual flows; reviewed local source and inventory-grant commands | OS identities, catalogs, connections and authorities are explicitly provisioned; a save grants no extra right or action |
| Rule interfaces | Existing authoring, scenarios, explicit lifecycle operations, intervention inspection and uncertainty reconciliation | Use the independent Atropos client; never embed the engine or implicitly activate/dispatch |
| Installation | Documented single-host Compose installation, authenticated verified-TLS access and explicitly selected workers | Linux/container reference deployment; Python 3.11 and 3.12 are the qualification targets |
| Accounts and recovery | SQLite account storage and the existing explicit JSON adapter; account backup and coordinated cold-site recovery | Unsupported formats are rejected without conversion/deletion; account backup alone is not whole-site recovery |

No new identity provider, storage adapter or notification implementation is
required by this baseline. Existing adapters are retained. A stable support claim
must name the exact qualified configuration; it must not imply every combination
of optional components has been tested.

## Contract baseline

These are the current contracts to review together, not a new compatibility layer.
The first-release manifest must pin the matching Atraxis, Atropos and client
revisions, package/dependency versions and chosen deployment/backend profiles.

| Contract | Current reference |
| --- | --- |
| Inventory status | [Status envelope schema 1](collection-inventory.md), with HTTP success distinct from functional readiness |
| Sources | [Source adapters and bounded transport](source-configuration.md); [reviewed local administration](local-administration.md) |
| Account authority | [Directory and SQLite schema 3](accounts.md), including complete audit and the membership projection |
| Inventory authority | [Inventory profile version 3](inventory-administration.md); catalog authority remains separately provisioned |
| Engine gateway | [Gateway profile version 2](rule-remote.md), expected actor/organization and the matching independent client |
| Dispatch and evidence | [Explicit dispatch](rule-dispatch.md) and [current observations/test isolation](observations-and-tests.md) |

The current rule authoring format and ZEN evaluator remain the tested candidate.
The final intervention-language decision is **open** and must be recorded
explicitly in Atropos before claiming the rule/workflow contract is frozen.
This plan does not silently make that choice. Prototype compatibility is still
not required; changing a contract requires its client, service, examples and
documentation to change together, without implicit migration of stored data.

## Four delivery gates

The order is shared with Atropos. Gates have concrete exit evidence; the presence
of code or a successful synthetic test is not by itself operational qualification.

| Gate | Deliverable | Exit criterion |
| --- | --- | --- |
| G1 — scope and contracts | This feature baseline, matching contract references and a candidate support manifest | Record the explicit language decision and the exact candidate versions/configurations; resolve contradictory contract claims |
| G2 — installation and complete recovery | A fresh operational installation and coordinated recovery on a distinct host | Verify identity, mounts, certificate renewal, all persistent state/configuration, audits and current revocations; preserve execution pause and reconcile uncertainty before any explicit resume |
| G3 — final integration and endurance | The supported source → rule → explicitly authorized action → notification path, plus representative retained-history/load evidence | Approved sources/targets/destinations and configured identities work; test markers, unknown outcomes and duplicate handling are preserved; bounded workload evidence states duration, limits, failures and scope |
| G4 — release candidate | Installable candidate packages, user/contributor documentation and one evidence index | Matching source/package identities, license/version/dependency checks and required CI pass; G1–G3 have no unresolved blocker for the claimed support matrix |

G1's functional baseline is recorded here. Its language decision and exact
candidate support manifest remain open. G2–G4 remain open operational/release
gates. Existing operator and separate-host synthetic acceptance, package tests
and measured workloads remain valid evidence within their stated scope; see
[development and validation](contributing.md). They are not reset by this plan.

Notification destinations not yet selected stay an explicit open prerequisite
for their operational check, not a reason to stop unrelated development. Do not
invent destinations, send test messages or trigger actions implicitly.

## Evidence reuse and change control

- Link existing evidence to its exact source/configuration, expected behavior and
  actual result. Keep successful checks closed when those inputs remain applicable.
- Repeat a check for a relevant source/configuration change, a failure, an uncovered
  requirement or a documented gap in its evidence. Do not add redundant campaigns
  merely to restate a result. Existing required PR checks still apply.
- A new bug affecting a gate is a correction within scope; an unrelated new
  capability is a later-release item. Record the reason for reopening a closed check.
- Keep synthetic, operational and proposed results distinct. Do not invent a
  percentage complete, release date, throughput promise, SLO or leak-absence claim.
- Preparing G4 does not publish it. Releases, tags, registries, website deployment
  and Read the Docs changes remain separate explicit actions.

## Deferred beyond this baseline

Remote account authority/SSO, additional account storage adapters, inventory
catalog editing, global cross-section search, new source adapters, automatic
metric-to-intervention wiring and new graphs are later work. High availability,
distributed failover and hot backup are outside the first stable release.
Do not remove existing capabilities or stored data to enforce this boundary.

Return to the [roadmap](../ROADMAP.md) or the [contributor guide](contributing.md).
