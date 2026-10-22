# Current observations and safe end-to-end tests

You can review a workflow using current monit-docker measurements, or exercise
its complete internal path in an isolated test mode.

## Current measurements

With an observation-required binding configured by the administrator:

```sh
atraxis rules --remote-config /private/remote.yml observe-dispatch-plan cpu \
  --target container:web-example --binding BINDING_SHA256
atraxis rules --remote-config /private/remote.yml observe-dispatch cpu \
  --target container:web-example --binding BINDING_SHA256 \
  --expected-generation REVIEWED_GENERATION --event-id incident-001 --confirm
```

There is no measurement file to enter. Atraxis fetches current inventory, checks
the exact source/container/filesystem mapping and signs evidence for Atropos.
Atropos checks the evidence, freshness, current rule and permissions. Confirmation
collects again; a successful preview never authorizes use of stale measurements.
Normal `observe` and activation still do not start interventions automatically.

After a lost dispatch response, inspect `dispatch-status` with the same event ID.
A new request must not silently produce a second intervention. Simultaneous
observation requests using the same engine credential can invalidate an earlier
short-lived challenge; explicitly refresh a failed preview.

## Administrator testing

Test permission is separate from execution permission. A scoped test user can
supply synthetic scenario values to evaluate a rule without changing real systems.
A pure preview performs no queueing or action:

```sh
atraxis rules --remote-config /private/remote.yml simulate-dispatch cpu \
  --target container:web-example --input scenario.json --binding BINDING_SHA256
```

To test through a workflow and its simulated alert, use a stable test event ID:

```sh
atraxis rules --remote-config /private/remote.yml test-dispatch cpu \
  --target container:web-example --input scenario.json --binding BINDING_SHA256 \
  --event-id admin-test-001
atraxis rules --remote-config /private/remote.yml test-dispatch-status cpu \
  --event-id admin-test-001
```

Atropos uses a separate test queue and an explicitly started test worker. The
same `test: true` context and test ID follow the source event, decision, run,
steps, results and alert. Every action needs a simulator; an unsupported action
is refused without calling its real implementation. The test run needs scoped
`read`/`test` permissions including the tested actions, not `execute` permission.

By default the alert simulator returns `[TEST]` and `delivery: simulated` in the
results, without sending a message. Atropos can optionally use a
[dedicated test receiver](https://github.com/decryptus/atropos/blob/main/docs/user/test-alert-delivery.md).
The receiver must acknowledge the same test and delivery IDs; status then shows
`delivery: test_receiver_acknowledged`. Configuration and secrets stay in Atropos.
Atraxis cannot choose a destination or remove the test marker.

A missing or invalid acknowledgement leaves the test `uncertain`, with no
automatic resend or error branch. Inspect the receiving service before resolving
the run or attempting another test. Direct Alertmanager/email integration and
the remote Auton program are not exercised by simulation. Neither test command creates
activation evidence or alters a saved rule. The commands above use the CLI;
see the [terminal guide](rule-console.md) for the available workbench controls.

Administrators: see the [Atropos setup and safety contract](https://github.com/decryptus/atropos/blob/main/docs/user/observations-and-tests.md)
for key provisioning, source requirements, permissions, the `--test` worker,
retained evidence, revocation and remaining external-integration limits.


## Intervention notifications

An explicitly configured Atropos [Alertmanager action](https://github.com/decryptus/atropos/blob/main/docs/user/alertmanager.md)
can follow a confirmed intervention. Its destination, routing labels and exact
adapter revision are approved in Atropos; Atraxis supplies neither credentials nor
notification destinations. API acceptance does not prove downstream delivery.
The action's separate simulator uses the marked test-receiver contract described
above. Existing configurations and default alert behavior remain unchanged.
