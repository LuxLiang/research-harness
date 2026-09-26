# Research Harness Step 6 — Full MVP Wiring v0.1

The runnable loop is:

```text
checkpoint → ActionCompiler → bounded Skill action → DeepSeek agent session
→ staged submission → field-diff permission validation → scientific transaction
→ local Git commit → deterministic gate/reducer → next action
```

`integrations/deepseek-harness/src/controller.ts` is the production runtime
controller. It mounts the existing DeepSeek Harness Agent API, presets, skills,
session persistence, tool registry, sandbox and approval policy. Python remains
the single implementation of artifact, gate and state-machine semantics through
the versioned stdio sidecar.

`research_artifacts.mvp.MVPController` supplies the same deterministic host loop
for CLI and CI. `SyntheticSkillRuntime` is intentionally a test adapter: it
produces schema-valid adversarial fixtures without pretending to be a model
runtime. It is used by the required no-network end-to-end test; production model
actions continue to use the Cordis plugin and `DeepSeekRuntimeAdapter`.

## CLI

```bash
research init <project> --objective "..."
research run <project> --budget-percent <percent>
research status <project>
research pause <project> --reason "..."
research resume <project>
research approve <project>
research cancel <project> --reason "..."
```

`run` stops only at `WAITING_FINAL_APPROVAL`, `WAITING_HUMAN:BUDGET`, `BLOCKED`,
`DONE`, pause/cancel, or an explicit action limit. Planning approval is automatic only when no open
BLOCKER/MAJOR Plan review requires a scope decision. Final approval is always a
human command.

Local Git commit is the scientific commit point. Optional remote replication is
best-effort and does not block later scientific work.

Step 9 adds an explicit per-run budget gate before dispatch. It does not add a
scientific state: budget exhaustion preserves the exact pending action and
returns the controller stop reason `WAITING_HUMAN:BUDGET`.

## Deterministic tool adapters

- `LeanToolAdapter` pins Lean/mathlib identity, runs a clean build, scans for
  `sorry`/`sorryAx`, audits declared axioms, and optionally invokes a stronger
  checker. An agent-authored Review cannot replace its report.
- `ExperimentExecutionAdapter` validates the protocol hash, derives stable run
  IDs, preserves raw output and provenance, reuses an existing run ID on resume,
  and refuses any post-lock scientific-field change.

## Compatibility clarifications

Two v0.1.2 lifecycle transitions were made explicit because each corresponds to
one already-specified atomic Skill action, not a new workflow state:

- `ScientificClaim IDEA → SUPPORTED` may occur in one theory-development
  proposal that both formalizes the statement and attaches evidence.
- `Experiment DRAFT → READY` may occur in one experiment-design proposal that
  completes and locks preregistration.

No artifact kind, global state, agent loop, model protocol, session store, or
tool protocol was added.
