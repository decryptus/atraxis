# Atraxis engineering requirements

- This source repository is public under GPL-3.0-only by explicit owner authorization. Keep credentials, infrastructure configuration and non-public product intentions private. Publication does not authorize deployment or package releases.
- Keep domain models independent of HTTP, CLI, TUI, source SDKs and credentials. Reuse DWho, HTTPdis and Sonicprobe where their capabilities are needed; do not introduce them into pure parsing merely to add dependencies.
- Preserve autonomous local monit-docker operation. Atraxis owns Centrex; Covenant remains an exporter, Auton executes authorized jobs, Alertmanager delivers notifications.
- Inventory/source adapters remain read-only, without job submission or ambient credentials. Browser rule authoring must use the independent Atropos client with explicit origin, single-use actor-bound forms, current engine permissions and exact revision checks. Never activate or dispatch implicitly when saving/testing a draft.
- Validate source identity, types, freshness and bounded resource counts. Distinguish authoritative empty inventory, unavailable source and historical snapshot. Never replace unknown metrics with zero.
- Product errors and codes must be English and must not contain source secrets, raw response bodies or exception text. Fixed schemas, grammars, enums and limits belong in named module constants.
- Use unittest. Run `python scripts/check-test-collection.py --runner unittest tests` and then `python -m unittest discover -s tests -v`. Report collected, executed and skipped counts. Do not add pytest-only declarations.
- Keep tests synthetic and independent of production systems. Check source isolation, stale data, failure/recovery and no mutation behavior when extending adapters.
- Keep documentation and roadmap honest about implemented behavior, omissions and validation limits. Review before merging; no automatic publication.

## Separate user and contributor documentation

- Maintain two distinct entry points and tables of contents: user documentation
  for installation, configuration, operation, public APIs and troubleshooting;
  contributor documentation for architecture, internals, tests, benchmarks,
  release engineering and development plans.
- Keep README and package descriptions focused on users. Link to the contributor
  guide instead of embedding maintainer procedures. Library API examples belong
  in the user guide when they are needed to integrate the library.
- Keep registry publishing, CI setup, repository secrets and maintainer account
  configuration out of user guides, website manuals and package descriptions.
  Never include credential values or private infrastructure evidence in either guide.
- Put implementation reviews and acceptance records under the contributor
  navigation. Preserve user-facing compatibility limits, migration instructions,
  security requirements and failure semantics in the user documentation.
- Apply this separation to generated documentation and FR/EN website content.
  Update source content and generators together; do not patch only generated HTML.
- Before delivery, inspect both entry points, check links and build documentation
  with warnings treated as errors where supported. Review README/package text and
  the deployed manual for accidental maintainer content. Preserve GPLv3 licensing and existing review, release and deployment approval requirements.


- Rule lifecycle calls must resolve authenticated actor/organization server-side and enforce current scoped permissions. Preserve separate draft and active versions, exact-version test evidence, explicit generation conflicts and audited activation. Keep pure evaluation separate from action execution.

- Keep the rule operator console explicitly local/trusted: derive identity from OS mapping, protect its profile, and never accept actor/grant overrides from rule files. Observation projection must reject absent, stale, ambiguous or wrong-source data; keep native units and never guess restart-window semantics.

- Atraxis must not import the Atropos engine, Redis rule repositories or ZEN, or persist authoritative rules. Use the separately packaged API client. Atropos owns rule lifecycle intelligence and storage. Reject embedded version-1 profiles instead of silently retaining direct storage access.
- Public configuration names describe roles (storage/queue/cache with backend and url), not concrete backend prefixes; document the current format and reject unsupported formats explicitly.

- Preserve explicit decision-to-workflow admission: approved immutable binding, exact active generation, scoped action permission, durable event identity, and authorization before every worker attempt. Manual facts are not trusted telemetry; never silently wire observation to intervention or bypass the guarded worker.

- End-to-end tests must carry one immutable test context through events, decisions, runs, action contexts, results and alerts. Keep their queue/locks separate, require scoped test rights, call registered simulators only, and refuse unsupported/missing test mode. Never drop the marker or fall back to a real intervention/notification. Observation-required bindings must reject manual facts for real admission and enforce source-bound signed evidence and freshness.

- Optional test alert delivery must use an engine-owned approved test receiver, preserve the test context and require its matching acknowledgement. Keep destination changes explicit, never follow redirects or retry uncertain delivery, and never claim receiver acceptance proves downstream delivery.

## Backward compatibility

User decision — 7 October 2026: backward compatibility is not required for
Atraxis or Atropos. API, configuration, rule/workflow and storage formats may
change without legacy aliases, compatibility layers or automatic migrations.
Update the current client, service, examples and tests together. Document the
currently supported format and reject unsupported input explicitly. This policy
does not extend to other projects or authorize deleting stored data.

- Browser activation must be a separate review and explicit single-use confirmation. Pin the exact revision, test report and active generation; let Atropos enforce current scope rights, latest evidence and its audited atomic transition. Never dispatch an intervention from activation or retry an uncertain result.

- Browser rollback and deactivation require separate actor/operation-bound confirmations and exact active generations. Atropos alone validates prior activation, evidence and permissions. Audit browsing is read-only and must preserve engine-filtered window semantics; never infer an empty filtered window is the end or imply deactivation cancels running actions.

- Keep real web/engine acceptance in `engine_tests` explicit: two separately installed service environments, disposable test-owned Redis, real client/API/ZEN, no silent skips or ambient destinations. Synthetic browser CI alone must not be described as integrated engine acceptance; local HTTP/AOF restart evidence does not establish remote TLS, backup/restore or live Auton acceptance.

- 1.0.0 scope (8 October): tenant → organization → groups/subgroups → memberships,
  explicit bounded inheritance/delegation, no implicit cross-organization access.
  Rule archive/unarchive/delete require separate rights; preserve audit and linked
  intervention evidence. Never auto-activate on unarchive. Backup pauses execution
  while bounded persistent admission remains available; revalidate queued work at
  execution. Generic queue/storage contracts; Redis first. Hot backup targets 2.0.0.

- Inventory read-grant administration requires explicit bounded inventory authority,
  independent of account delegations and engine permissions. Keep catalog/authority
  edits local until separately designed. Confirmed writes lock account directory
  before inventory profile, recheck both revisions and authority, and replace grants
  plus audit atomically. Never retry an uncertain write or silently trim history.

- Local inventory grant administration derives its actor from an explicit OS UID
  mapping, never environment identity or an actor argument. Keep web usernames
  separate. Explicit CLI organization reads must reject absent/mismatched response
  contexts; never fall back to global inventory.

- Inventory search must run on the full authorized projection before counts and
  pagination, recheck live rights before releasing results, and never turn a filter
  into a read grant. Keep collection readiness separate from search results.
  Web/CLI/Textual use the same server query; no loaded-page filtering fallback.

- Account search uses the current delegated administration projection, never the
  global user directory. Filter/deduplicate before counting/paging and recheck
  identity plus directory revision before returning. No cached results on denial.

- SQL account adapters must store queryable entities and relationships in relational
  tables; never wrap the entire directory in a singleton payload BLOB. Use AnySQL
  for connections, disable automatic transaction replay, enforce lengths/signs,
  foreign keys and uniqueness in SQL, and test the actual search query plans.
  Fixed-width hex keys use CHAR(n) plus enforced length/alphabet constraints.
  SQLite auto-increment keys retain exact INTEGER PRIMARY KEY syntax with a
  nonnegative/positive CHECK; spelling UNSIGNED alone is not enforcement.

- Prototype formats may evolve. Do not add migration tooling implicitly. Reject
  unsupported databases without deleting or rewriting their contents; preserve
  existing deployments and stored data.
