# Agent integration contract

The Artifact Layer is runtime-neutral. A harness adapter should expose its
operations through the runtime's existing typed tool/plugin mechanism rather
than adding another agent loop.

## Context production

The deterministic orchestrator queries artifacts by project, kind, status, and stable ID. It
then constructs a bounded context bundle containing only the selected artifact
revisions and direct dependencies. The complete chat transcript is not needed
to reconstruct scientific state.

Recommended role inputs and outputs:

| Role | Inputs | Produces |
|---|---|---|
| Question | Project and accepted Decisions | ResearchQuestion |
| Literature | Active questions and target Claims | LiteratureEvidence |
| Planner | Questions, literature, and Decisions | ResearchPlan |
| Theory | Approved plan, literature, and dependency Claims | ScientificClaim |
| Experiment | Plan, target Claim, and relevant Decisions | Experiment runs/result |
| Reviewer | Exact target revision and direct evidence | Review |
| Writer | Verified Claims, completed Experiments, verified Literature | Manuscript plus Claim locations |
| Orchestrator/human | Plan and Review recommendations | Explicit transitions and Decisions |

## Runtime-facing operations

The Python API and CLI provide the MVP contract:

- `discover`, `get`, and `query` for bounded reads;
- `validate` and `require_valid` for artifact contracts and stage gates;
- `graph_edges` and `mermaid_graph` for dependency inspection;
- `transition` for explicit optimistic-concurrency state changes;
- `ResearchOrchestrator.apply` for typed events and deterministic transitions;
- `CheckpointStore` for resumable, optimistic-concurrency control state;
- `RuntimeAdapter` as the narrow dispatch boundary to the existing harness.

An adapter should associate its runtime session ID with `provenance.updated_by`,
but session storage remains outside this layer. Reviewer agents may create
Review artifacts; they must not call a target transition as an implicit side
effect of their recommendation.

The runtime submits structured gate candidates rather than prose control
instructions. The gate evaluator validates pinned artifact inputs; the reducer
maps its enum verdict to exactly one declared next state. Session logs remain
execution traces and are not replayed during resume.

## Git handoff

The MVP intentionally leaves Git commit policy to the orchestrator/runtime.
After validated changes, it should commit a coherent scientific transaction,
for example an Experiment result, the evidence added to a Claim, and the
resolution of a Review issue. The commit message should include the project ID,
artifact IDs, and lifecycle transition.
