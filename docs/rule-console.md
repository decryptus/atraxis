# Rule operator console and current observations

For delegated access without Redis credentials, see [remote rule management](rule-remote.md).
The `--operator-config` mode described here remains restricted to trusted local administrators.

Use `atraxis rules` to create/import drafts, test, activate exact versions and
inspect decisions. A curses workbench uses the same Atropos API. The `--operator-config` mode is a **local trusted-administrator tool**, not a remotely delegated
multiuser control plane. The existing HTTP daemon still exposes read-only routes.

## Contents

- [Operator configuration](#operator-configuration)
- [Complete CLI workflow](#complete-cli-workflow)
- [Terminal workbench](#terminal-workbench)
- [Evaluate current observations](#evaluate-current-observations)
- [Permissions and failure behavior](#permissions-and-failure-behavior)

## Operator configuration

Install the separately packaged client as described in [remote setup](rule-remote.md).
The local trusted operator profile now maps a POSIX UID to an **Atropos API**
credential. It contains no rule storage or business policy:

```yaml
version: 2
identities:
  - uid: 1000
    subject: local-operator
    organization: example
    atropos_client: atropos-client.yml
```

Copy `examples/rules/operator.yml`, adjust your UID/mapping, and use mode 0600.
The referenced client profile contains `server.url`, `token_file` and optional
CA/timeout values. The engine verifies that its credential identifies the same
subject/organization. Private regular files and trusted parent directories are
required. Do not pass an actor or storage URL on the command line.

Version-1 embedded profiles require [explicit migration](rule-remote.md#migration-from-the-embedded-prototype).
The following CLI/TUI commands retain their syntax and send all rule operations
to Atropos. Observation bindings remain in Atraxis; Atropos owns rule evaluation.

## Complete CLI workflow

Create a new draft and note the returned full revision ID:

```sh
atraxis rules --operator-config /private/operator.yml create cpu-high \
  --name cpu --kind intervention --target container_group:webapps
atraxis rules --operator-config /private/operator.yml draft cpu
atraxis rules --operator-config /private/operator.yml test cpu REVISION \
  --cases examples/rules/cpu-cases.json
```

The test command returns an immutable report ID. After reviewing its cases and
results, activate the exact version with the generation last shown by `active`:

```sh
atraxis rules --operator-config /private/operator.yml active cpu
atraxis rules --operator-config /private/operator.yml activate cpu REVISION \
  --test-id REPORT_ID --expected-generation none
```

`none` is only correct before any active-state transition. After deactivation,
there is still a generation: use it, not `none`. A conflict requires refreshing
and reviewing state. No command retries writes automatically.

Combine templates with `--match all` or `--match any`. Repeat `--target` to select
several resources/groups. Use `--parameters parameters.json` for catalog overrides
and `--decision LABEL` for a decision label, not an executable operation.

```sh
atraxis rules --operator-config /private/operator.yml read cpu REVISION
atraxis rules --operator-config /private/operator.yml export cpu REVISION > cpu-export.yml
atraxis rules --operator-config /private/operator.yml import cpu-edited.yml \
  --kind intervention --target container_group:webapps --expected-revision DRAFT_HEAD
atraxis rules --operator-config /private/operator.yml history cpu --offset 0 --limit 20
atraxis rules --operator-config /private/operator.yml report cpu REVISION REPORT_ID
atraxis rules --operator-config /private/operator.yml audit cpu
```

An import for a new name uses `--expected-revision none`. Imports and edits create
new drafts and leave the active revision unchanged. Export emits canonical rule
YAML only; scope/permissions and test evidence are not embedded or granted by the
file. Protect redirected files with an appropriate shell umask. Do not redirect
onto a file currently being read by a command.

```sh
atraxis rules --operator-config /private/operator.yml rollback cpu OLDER_REVISION \
  --test-id OLDER_LATEST_PASSING_REPORT --expected-generation CURRENT_GENERATION
atraxis rules --operator-config /private/operator.yml deactivate cpu \
  --expected-generation CURRENT_GENERATION
```

For explicit synthetic input evaluation, use `evaluate cpu --target
container:web-example --input facts.json`. It returns a pure decision and the
active revision/generation; it executes no intervention.

## Terminal workbench

```sh
atraxis rules --operator-config /private/operator.yml tui \
  --name cpu --cases examples/rules/cpu-cases.json
```

Start with an existing scoped rule. Minimum terminal size: 76 columns × 15 rows.
The workbench uses standard curses and the shared rule application, keeping
presentation separate from rules, permissions and persistence.

| Key | Operation |
| --- | --- |
| Up/Down | Select a revision |
| PgUp/PgDn | Browse history pages |
| `r` | Refresh active state and history |
| `v`, `h`, `c` | View revision, audit or catalog |
| Left/Right | Scroll details, including long target lists |
| `t` | Test selected revision using the supplied cases file |
| `a`, `b` | Prepare activation or rollback of the selected tested revision |
| `d` | Prepare deactivation of the active version |
| Enter / Escape | Confirm the displayed exact change / cancel |
| `i` | Import a YAML/JSON file as a new draft with the current draft's scope |
| `o` | Evaluate current observations for an explicitly entered container target |
| `q` | Quit (first cancels a pending review) |

Activation/rollback requires a passing test run for that selection in this
workbench session; CLI activation can use an existing report ID. Review shows
revision, generation, domain and targets. A failed or uncertain write clears the
pending confirmation, preventing accidental repeat on Enter. Refresh and inspect
audit after an error. Catalog creation, parameter overrides and scope changes use
the CLI; the workbench does not yet include a form builder or embedded YAML editor.
File prompts currently accept printable ASCII paths.

## Evaluate current observations

Set `inventory_client` in the operator configuration to the existing Atraxis
read-API client YAML, with its separate credentials and verified HTTPS settings.
Bindings associate an authorized logical container with an exact `source_id` and
full Docker ID. Recreated containers require an explicit updated binding; names
are not used as a fallback. These Atraxis bindings and the separate Atropos authorization inventory are
operator-maintained, not automatically trusted from a source response.

```sh
atraxis rules --operator-config /private/operator.yml observe cpu \
  --target container:web-example
```

The existing bounded read client supplies the authenticated inventory, including
HTTP-transfer aging. Projection rejects historical, unavailable, stale or missing
observations, mismatched identities and unsupported inputs. Age continues to
advance locally and is checked again after ZEN evaluation. A generation change
between projection and evaluation invalidates the result. This is a one-shot read;
it does not start background policy execution.

| Rule input | monit-docker observation |
| --- | --- |
| `cpu_percent` | `cpu_percent`, preserving its source units (can exceed 100 on multicore systems) |
| `memory_percent` | `mem_percent` |
| `maintenance` | `maintenance_active`, strict boolean |
| `health_status` | `health`, only established healthy/unhealthy values |
| `restart_attempts` | Same named counter; no invented window |
| `disk_percent`, `inode_percent` | Exact configured filesystem's capacity observations |
| `readable`, `writable` | Exact filesystem's `fs_readable` / `fs_writable` 0/1 flags |

Filesystem inputs require an explicit `[group, path]` binding and exactly one
matching sample. Missing flags stay unknown; they never become false. Unknown,
starting or absent health checks are rejected for health-based decisions.

**`restart_count` is not projected.** A count of recent starts is not necessarily
a count of restarts, and a configured retry count is different again. The default
restart template remains usable with explicit synthetic/domain-supplied facts;
a live event-window contract must be agreed before mapping it. CertLord facts,
server metrics, temporal windows and automatic dispatch are not implemented by
this container projection.

## Permissions and failure behavior

All operations reuse scoped lifecycle authorization. `observe` needs read and
evaluate permission and rejects out-of-scope targets before contacting inventory.
File imports need `upload` provider permission; internal saves need `internal`;
export needs `read` and `export` with provider `download`. No export permission is
implied by read access alone.

Exit codes: 0 success, 1 failed scenario expectations, 2 invalid configuration,
missing dependencies/inputs, unavailable data/backend or conflicts, 3 access
denied, 130 interruption. Errors do not print raw credentials or upstream data.
These codes do not authorize blind retry: always inspect state after a write error.

See [version lifecycle semantics](rule-lifecycle.md) for pinned versions, audit,
rollback and the remaining distributed-policy and execution-binding limitations.

For explicit approved workflow submission from the CLI, see [review and dispatch](rule-dispatch.md). The current-observation command and TUI remain evaluation-only.

[Current-observation dispatch and isolated test commands](observations-and-tests.md) are additional CLI operations. Existing `observe` remains evaluation-only.


## Explicit organization context

One authenticated username, OS UID or gateway token may have several mappings,
one per organization, all naming the same subject. Each mapping retains a separate
Atropos credential for exactly that subject and organization. Duplicate organization
mappings or different subjects under one identity are rejected. With multiple
eligible mappings, no organization is chosen implicitly.

The browser offers an organization selection page and carries the choice in links,
GET fields and confirmation URLs across inventory, rules and interventions. Tabs
are independent. A form is bound to the selected organization and cannot be reused
in another. An unavailable inventory scope is refused rather than replaced with
a global inventory view. Current membership and server mappings are rechecked.

For local or remote CLI and terminal commands, select before the subcommand:

```sh
atraxis rules --operator-config operator.yml --organization operations tui --name cpu --ui textual
atraxis rules --remote-config client.yml --organization operations history cpu
```

The matching client is **0.1.0a9**. Remote selection uses an explicit
`X-Atraxis-Organization` request header. The gateway resolves the acting subject
from the authenticated token and the destination from its own mapping; the header
is only a selection, never a permission or credential. Direct Atropos clients
remain bound to their engine credential. Switching terminal organization requires
a new command/session, so pending confirmations cannot change context silently.
