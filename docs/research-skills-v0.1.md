# Research Skills v0.1

A Skill is a reusable scientific capability. An Agent is a temporary DeepSeek
Harness session executing exactly one controller-selected Skill. Skills stage
artifacts or Reviews but never promote canonical files or choose transitions.

## State mapping

| State or branch phase | Skill |
|---|---|
| DISCOVERY_QUESTION | question-framing |
| DISCOVERY_LITERATURE / FEASIBILITY | literature-novelty |
| PLANNING_DRAFT | research-planning |
| PROPOSAL_STRUCTURING / NOVELTY / PLAN_RECONSTRUCTION | question-framing / literature-novelty / research-planning |
| PROPOSAL_CORRECTNESS_REVIEW / REVISION | proposal-correctness-review / proposal-revision |
| Theory DEVELOP | theory-development |
| Theory VERIFY, adversarial | theory-verification |
| Theory VERIFY, core formal | lean-formalization, then independent theory-verification, lean-verification, semantic-alignment-review |
| Experiment PREPARE / RUN / ASSESS | experiment-design / experiment-execution / experiment-verification |
| SYNTHESIS_BUILD / COMPLETION_GATE | research-synthesis / completion-review |
| FOLLOWUP_TARGETED | targeted-followup |
| CONSISTENCY_GATE | consistency-review |
| WRITING_DRAFT / FINAL_REVIEW | scientific-writing / final-review |

Human approval, routing, materialization, join, interrupts, and DONE are
controller-only. The complete contracts are the nineteen `skill.yaml` files;
their `SKILL.md` companions are the injected operational instructions.

## Preserved pAI/MSc mechanisms

The design preserves the Practical/Rigor/Narrative perspectives, adversarial
literature novelty falsification, divergent/convergent planning, explicit
theory Claim graphs and proof-gap disclosure, separate experiment design and
verification, track merge, completion verification, duality/consistency check,
targeted follow-up, and hard-blocker final review. Artifacts replace scattered
handoff files and deterministic gates replace model-controlled routing.

It deliberately excludes pAI/MSc fail-open completion, stall-based forced
completion, fixed percentage gates, multi-model counsel, tree search, and
campaign mode. Lean formalization and semantic alignment are Research Harness
extensions rather than inherited pAI/MSc roles.

| Research Harness Skill | pAI/MSc source adapted |
|---|---|
| question-framing | Practical Compass, Rigor & Novelty, Narrative Architect, persona synthesis |
| literature-novelty | LiteratureReviewAgent and adversarial literature prompt |
| research-planning | BrainstormAgent, FormalizeGoalsAgent, ResearchPlanWriteupAgent |
| theory-development | MathLiterature, MathProposer, MathProver |
| theory-verification | MathRigorousVerifier and MathEmpiricalVerifier |
| Lean and semantic Skills | New Research Harness capability; ProofTranscription traceability principles only |
| experiment design/execution/verification | ExperimentDesign, Experimentation, ExperimentVerification |
| research-synthesis | TrackMerge and FormalizeResults |
| completion-review | `verify_completion` node without fail-open or percentage heuristics |
| consistency-review | DualityCheck extended to artifacts and Lean |
| targeted-followup | FormalizeGoals follow-up and literature loop |
| scientific-writing | ResourcePreparation, Writeup, Proofreading |
| final-review | ReviewerAgent and review-verdict validation |

Implementation references are the public pAI/MSc
[persona prompts](https://github.com/PoggioAI/PoggioAI_MSc/blob/b2118e6f9cc07009da5f22b5816907e1f90cfe03/consortium/prompts/persona_instructions.py),
[literature prompt](https://github.com/PoggioAI/PoggioAI_MSc/blob/b2118e6f9cc07009da5f22b5816907e1f90cfe03/consortium/prompts/literature_review_instructions.py),
[math primer](https://github.com/PoggioAI/PoggioAI_MSc/blob/b2118e6f9cc07009da5f22b5816907e1f90cfe03/MATH_RESEARCH_PRIMER.md),
[workflow graph](https://github.com/PoggioAI/PoggioAI_MSc/blob/b2118e6f9cc07009da5f22b5816907e1f90cfe03/consortium/graph.py),
and [reviewer prompt](https://github.com/PoggioAI/PoggioAI_MSc/blob/b2118e6f9cc07009da5f22b5816907e1f90cfe03/consortium/prompts/reviewer_instructions.py).

## Verification and integrity

Prover, theory verifier, Lean verifier, and semantic reviewer always use
different sessions. CORE_FORMAL has no waiver and forbids `sorry`, `sorryAx`,
and unapproved axioms. A library or formalization gap is not evidence that a
mathematical statement is false.

After the final required verification Skill submits passing revision-pinned
Reviews, the reducer emits a host-only `PROMOTE_VERIFIED_CLAIM` command. The
scientific transaction collects those Reviews, increments the Claim revision,
and changes only verification metadata and status. No model tool exposes this
command.

Experiment design locks all scientific protocol fields before outcomes are
visible. Execution appends pinned runs without interpretation. Independent
verification applies preregistered outcome rules and preserves all failed,
negative, contradictory, and inconclusive results.

Every supporting or verifying evidence edge is revision- or checksum-pinned.
Manuscript theorems, major results, novelty statements, figures, and tables are
bound in `paper/traceability.yaml`. Writer sessions cannot create or revise a
ScientificClaim.

## Failure ownership

Skills report fine-grained failures. GateService maps them to local retry,
INCOMPLETE, one explicit RETHINK level, targeted follow-up, or BLOCKED. Retry
exhaustion never silently escalates a rethink level. Conversation prose has no
control authority.

FEASIBILITY, COMPLETION, CONSISTENCY, and FINAL gates require the corresponding
machine-readable Review scheme. GateEvaluator derives the legal controller
verdict from its assessment and rejects a model-supplied gate candidate that
disagrees, so the Review supplies scientific judgment without owning routing.
