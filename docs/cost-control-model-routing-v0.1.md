# Cost Control + Model Routing v0.1

Step 9 adds an operational budget boundary without changing the scientific
state machine. Every new `research run` or pilot requires an explicitly
approved percentage of the user's current Codex allowance. When the runtime
cannot observe the allowance, percentages remain relative estimates and are
never represented as measured credits.

## Ownership and stop semantics

- `projects/<project>/orchestrator/budget.yaml` is the version-controlled run
  cost ledger and policy.
- Every initial approval or increase creates an ACCEPTED Decision Artifact.
- `WAITING_HUMAN:BUDGET` is a controller stop reason, not a new scientific
  workflow state. `state.yaml`, pending action, frozen ContextBundle, branch
  progress, and completed Skills remain unchanged.
- Refusing or deferring an increase leaves the project waiting. Budget
  exhaustion is never classified as scientific failure.

## Routing

`config/model-routing.v0.1.yaml` is the single alias and policy source:

| Tier | Default alias | Intended work |
|---|---|---|
| economy | `gpt-5.6-luna` | bulk/mechanical |
| balanced | `gpt-5.6-terra` | normal research |
| frontier | `gpt-5.6-sol` | critical scientific judgment |

The deterministic router evaluates Skill, risk, difficulty, uncertainty, prior
attempts, and the Skill quality floor. It may move only upward. Budget pressure
may stop dispatch, but cannot select a model below the floor. DeepSeek Harness
receives the selected alias and reasoning effort through its public per-agent
model-selection seam; session events provide token accounting when adapters
report it.

## Guard and ledger

Before dispatch, the guard reserves a configurable relative action estimate.
Routine actions cannot consume the verification reserve. Critical verification
may use it. On completion, the reservation becomes a ledger charge and records
available input/output/cache/reasoning token usage. Status groups charges by
stage, Skill, and tier and reports the current model, effort, pending estimate,
warnings, approved/used/remaining percentage, and reserve.

If an action does not fit, the controller records the exact action, state,
target, quality floor, selected model, and minimum/recommended additional
percentage. Approval resumes the already-persisted ContextBundle and does not
replay completed Skills.

## CLI

```bash
research run proj-example --budget-percent 10
research status proj-example
research budget proj-example --minimum
research budget proj-example --recommended
research budget proj-example --additional-percent 2.5
research budget proj-example --keep-paused
```

Without `--budget-percent`, an interactive terminal asks for 5%, 10%, 20%,
30%, or a custom percentage before starting. Tests and non-interactive drivers
must inject a percentage explicitly.

## Verification evidence

- Python suite: 102 tests passed, including four Step 9 budget/router tests.
- Scientific evaluation/hardening regressions remain green with zero new S4
  failures.
- DeepSeek TypeScript build and Cordis public-loader contract tests pass.

Known limitation: relative action charges are policy estimates unless the
provider reports usable allowance/credit conversion. Token counts are retained,
but the system does not invent a conversion to absolute Codex allowance.
