# Atraxis 1.0 release candidate

Historical RC scope. For the stable package, see [Atraxis 1.0](v1.0.md).

Version `1.0.0rc1` is a prerelease for evaluation. It is not a stable-production
release, a live-site recovery qualification or a guarantee about external effects.

## Included and reference configuration

The candidate includes existing read-only inventory and source collection,
authenticated web/API/CLI/Textual interfaces, scoped account administration,
and optional rule authoring, scenario tests and explicit lifecycle operations
through the independent Atropos service. Saving/testing does not activate a rule;
activation does not dispatch work. Current permissions and approved bindings
remain required for execution.

| Component | Candidate configuration | Qualification boundary |
| --- | --- | --- |
| Atraxis | `1.0.0rc1`, Python 3.11 or 3.12, Linux amd64 reference platform | Installed package, synthetic core/UI/network and Compose acceptance |
| Operator Compose | Python 3.12, verified TLS, SQLite accounts, SQLite engine storage, integrated queue | Disposable installation and transfer/recovery between two CI hosts; operational site recovery remains open |
| Atropos engine | `0.1.0a21`; integrated source `dedb114217c3df5fe0ab79d7acc53cd5636ebee0` | Independently installed engine, still an alpha dependency; no claim of an Atropos stable release |
| Independent client | `0.1.0a10`, source from the same Atropos revision | Client/API and source identity checked independently of the engine |
| Rules and workflows | Existing version-1 YAML authoring/workflow models; ZEN 2.1.2 | First-match typed decisions, bounded registered-action workflows; no arbitrary scripts or JDM imports |
| Native inventory | Direct `monit_docker` and `certlord`; optional `prometheus_monit` and `prometheus_certlord` through Covenant and Prometheus | Complete native projections, including certificate domains/fingerprints; unknown, unavailable, authoritative empty and historical values stay distinct |
| Selected metrics | Generic `prometheus` adapter, with Covenant optional | Explicit numeric selections; does not reconstruct native inventories |
| Execution | Existing approved Auton adapter; workers opt-in | CI uses disposable effects and a pinned, patched Auton fixture; live targets are not yet qualified |
| Notifications | Existing Alertmanager adapter | Local/synthetic receiver evidence does not establish actual email/SMS/chat receipt |
| Optional storage | Existing JSON accounts and engine Redis/PostgreSQL/MySQL/MariaDB adapters retained | Existing contract/TLS tests only; not additional operational support promises for this first target |

Follow the [setup guide](setup.md) and [Compose guide](docker-compose.md). The
Compose default starts no execution worker. Explicit `simulation` and `execution`
profiles retain their different rights and isolation requirements. Install
matching engine/client sources when enabling optional rule interfaces; never
install an engine inside the Atraxis process.

## Source adapter prerequisites

Prefer direct `monit_docker` and `certlord` connections when Atraxis can reach
their APIs. Neither requires Covenant or Prometheus. Each configured source
discovers the resources returned by its API; domains and containers do not need
individual configuration. The generic `prometheus` adapter retains explicit metric
selections. See [adapter selection](source-configuration.md#choose-an-adapter).

The optional `prometheus_monit` and `prometheus_certlord` routes require both
Prometheus and Covenant's `monit_observation` / `certlord_observation` plugins.
These plugins are available in Covenant source revision
[`331e56367ef92ff24a5f3931b650d9301a298bed`](https://github.com/decryptus/covenant/commit/331e56367ef92ff24a5f3931b650d9301a298bed).
Use a build containing that change: the previously published Covenant `0.0.72`
does not include it. Atraxis's candidate publication does not publish Covenant.
The legacy metric plugins cannot supply complete inventories.

The indirect routes preserve the same inventory fields and native freshness
semantics, with collection/scrape delay taken into account. They have no automatic
fallback to direct APIs or deduplication between separately configured sources.
Prometheus retention and changing text labels incur additional storage costs;
the 10,000-row and 4 MiB transport bounds reject an oversized snapshot as a whole.
Capacity for a production inventory remains to be qualified. See the
[Monit guide](prometheus-monit.md) and [CertLord guide](prometheus-certlord.md).

## Limits and remaining stable-release work

1. Qualify a fresh reference site and full cold restoration on a distinct host,
   including identities, TLS renewal, configuration, persistent state and audits.
   Recheck current revocations, preserve pause and reconcile uncertain effects
   before any explicit resume. Existing isolated recovery tests remain acquired.
2. Qualify one approved reference execution target and actual notification
   destination. Preserve test markers, duplicate handling and unknown outcomes.
   Do not select recipients or trigger actions from configuration examples.
3. Assess retained-history/load evidence for that exact profile and fill only
   evidenced gaps. The prior bounded four-hour synthetic campaign is retained,
   but is not a general performance, long-term stability or leak-absence claim.
4. Package matching final sources after those gates close and publish the stable
   release with the same bounded support matrix. This candidate does not complete
   that final gate.

The current API/configuration/storage formats remain experimental during the
candidate phase. Unsupported formats fail explicitly; no automatic conversion,
deleting old data or cross-backend migration is introduced. Back up existing
state before evaluating a different version. A candidate version number does
not grant compatibility with every earlier prototype.

SSO, new backends/source adapters, global search, graphs, automatic
metric-to-intervention wiring, high availability and hot backup are later work.
None is required to finish this 1.0 reference profile. A relevant defect is fixed
within scope; it does not reopen unrelated qualification.

Return to the [user guide](README.md) or [roadmap](../ROADMAP.md).
