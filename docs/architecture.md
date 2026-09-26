# Architecture

Research Harness separates scientific state from execution history. Accepted
artifacts and Git commits record the research; agent sessions record model
calls, tool use, failures, and provenance. Conversation text is never used as
an implicit state transition or as accepted evidence.

## Execution flow

1. The controller loads the checkpoint and determines the next legal action.
2. A context builder selects bounded dependencies and pins their revisions and
   hashes in an immutable context bundle.
3. The selected runtime executes one assigned skill. It returns structured
   candidate artifacts, reviews, or a declared failure.
4. The staging validator checks schemas, references, base revisions, and the
   skill's allowed operations and fields.
5. Evidence gates derive an allowed event from structured reviews. The reducer
   computes the next state; the model cannot select it directly.
6. A scientific transaction rechecks state under a lock, promotes declared
   files, validates the workspace, and commits the checkpoint with explicit
   Git paths. The controller can then dispatch the next action.

Human approval states stop execution. Theory and experiment branches share a
frozen plan and join at synthesis; logical separation does not promise parallel
execution. Planned outputs are materialized by the controller before their
assigned skills fill in scientific content.

## Ownership

| Component | Owns |
| --- | --- |
| Python validators and reducer | Schemas, semantic rules, gates, legal transitions |
| Controller | Scheduling, action identity, routing, approval boundaries |
| Runtime / agent sessions | Model and tool execution, execution provenance |
| Skills | Proposals and structured scientific judgments within field permissions |
| Scientific transaction | Canonical files, checkpoint, explicit-path Git commit |
| Human operator | Budget decisions, required approvals, recovery authorization |

The bundled Python runtime is synthetic. The optional TypeScript controller
runs model actions through DeepSeek Harness and calls the Python semantic
sidecar. Both paths use the framework's validation and transition rules; model
execution requires the host's providers and services.

## Persistence and recovery

Canonical state lives in `projects/<project>/`, including
`orchestrator/state.yaml`. Staging, immutable context bundles, journals, and
execution bookkeeping live under ignored `.harness/research/` paths.

Each action has a stable identity tied to its input state. Stale revisions or
context drift invalidate candidates. Accepted transaction results make repeated
promotion of the same action idempotent; stable experiment run IDs prevent
repeated runs from becoming duplicate evidence.

Transactions journal their work and recover prepared changes by finishing or
rolling them back. A successful local Git commit is the scientific commit
point; a later journal-finalization failure does not undo it. Unrelated Git
changes are not swept into scientific commits. Optional remote push failures
retain the local commit and block further dispatch pending recovery.

Pause, cancellation, and blocked states are distinct. Pause preserves resumable
work; cancellation archives the project; blocked records a recovery condition.
Corrupt checkpoints fail closed. Retry exhaustion never silently relaxes a
scientific gate or escalates the scope of replanning.

## Source map

| Path | Responsibility |
| --- | --- |
| `research_artifacts/` | Python API, CLI, validation, state machine, transactions |
| `research_evals/` | Deterministic scientific integrity evaluators |
| `schemas/` | Artifact, checkpoint, runtime, and evaluation contracts |
| `config/` | Default model routing |
| `integrations/deepseek-harness/` | Model host plugin, skills, capability presets |
| `evals/` | Adversarial cases, regression baseline, failure matrix |
| `tests/` | Unit/integration tests and fictional fixtures |
| `scripts/` | Plugin installer and operator utilities |

Builds bundle canonical resources into the Python wheel. Resources are not
maintained as a second source tree. Workspaces remain separate from the library.

## Boundaries

Local locks provide single-writer coordination, not distributed transactions.
The framework does not supply a new agent loop, database, vector store, or
cross-project memory. Validation checks declared evidence relationships and
review requirements; scientific judgments still depend on reviewers, tools,
and the quality of underlying evidence.
