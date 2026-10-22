# Rule lifecycle through the Atropos API

Atraxis is the presentation and command layer for ecosystem services. **Atropos
owns rules, their storage, validation, tests, active versions, decisions and
engine audit.** Atraxis never imports the engine or opens its Redis database.

The CLI/TUI send commands through a separately packaged `atropos-client`, which
has no ZEN or Redis dependency. Saved drafts and imports are persisted only by
Atropos. Rule content may exist transiently in an editor or HTTP request; Atraxis
has no authoritative local rule repository or offline fallback.

The user workflow remains:

1. Choose a template or edit/import YAML.
2. Save a draft through Atropos's API.
3. Ask Atropos to test the exact revision against synthetic scenarios.
4. Review and explicitly activate that revision using its passing report and the
   expected current generation.
5. Read history/audit or request rollback through the same API.

Atropos checks current domain/resource/provider permissions and records lifecycle
transitions. Atraxis authenticates the user and pins its subject/organization to
an independently authenticated Atropos credential. A request cannot override that
mapping. Editing or evaluation does not authorize action execution.

See [remote setup and migration](rule-remote.md), [CLI/TUI commands](rule-console.md)
and the private [Atropos lifecycle guide](https://github.com/decryptus/atropos/blob/main/docs/user/rule-lifecycle.md).

The former `atraxis.rules.build_atropos_rule_application` embedding factory has
been removed. Engine-side Python integrations use `atropos.rule_application`;
Atraxis extensions use `atropos_client.client.RuleClient` over verified HTTPS.

[Explicit workflow dispatch](rule-dispatch.md) is a separate reviewed operation, with its own execution permissions and approved binding.


## Intervention and test admissions in audit

The web lifecycle audit includes intervention and test-run admissions alongside
activation, rollback and deactivation. Each admission shows its rule revision and
run ID; interventions also show the reviewed binding and generation, while tests
retain their explicit test marker. A blue admission notice does not confirm that
the job ran or succeeded. Check the engine's run status before drawing that
conclusion. Audit browsing does not start or retry work.
