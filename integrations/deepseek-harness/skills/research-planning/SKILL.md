---
name: research-planning
description: Design an executable ResearchPlan with primary and fallback strategies and deterministic THEORY, EXPERIMENT, or MIXED routing inputs.
---

# research-planning

Read `../_shared/artifact-boundaries.md`, `../_shared/failure-taxonomy.md`, and `../_shared/scientific-evaluation-policy.md` before acting.
Use only the revision-pinned ContextBundle. Treat session history as execution provenance, never scientific truth.

## Purpose

Produce an executable ResearchPlan from accepted questions and evidence.

## Inputs

- Active ResearchQuestion
- Assessed or verified LiteratureEvidence
- Accepted Decisions
- Previous Plan and Reviews
- Frozen ResearchProposal and source map in PROPOSAL_REVIEW
- Project input_resources in FULL_RESEARCH v0.1.4

## Outputs

- ResearchPlan proposal

## Tools

- `research_artifact_read`
- `research_artifact_query`
- `resource_read`
- `research_artifact_validate`
- `research_artifact_propose`
- `research_action_submit`

## Procedure

1. Read every relevant pinned Project input resource in chunks no larger than 20,000 characters, continuing from next_offset_chars until it is null.
2. Generate multiple candidate strategies and compare feasibility, information gain, assumptions, and risk adversarially.
3. Choose a primary and fallback strategy and recommend THEORY, EXPERIMENT, or MIXED routing.
4. Define work packages, milestones, success criteria, and planned outputs without materializing artifacts.
5. Choose verification_profile CORE_FORMAL only when the pinned scope explicitly requires Lean formalization and a pinned Lean environment is available. Otherwise use ADVERSARIAL for rigorous independently reviewed mathematical claims; do not silently add Lean work to an evidence-grade theory project.
6. Preserve required reviewer-session isolation in the output DAG. When the active question requires Base, Optimistic, chosen-arm, separation, or delay results to be developed and reviewed independently, give each theorem family its own stable THEORY-track ScientificClaim output; do not merge distinct developer/reviewer obligations into one ledger output.
7. All executable theorem outputs in a THEORY-only route must belong to THEORY work packages. Do not create a required REVIEW-track output for synthesis: research-synthesis and the completion/consistency gates run after the selected track joins.
8. For a newly created ResearchPlan, omit superseded_by. A plan must never point superseded_by to itself; supersession metadata is only valid when an older revision is actually replaced by a distinct canonical successor.
9. Use the exact ResearchPlan v0.1.2 contract. Bind candidate id, kind, and project_id to the current proposal authorization: candidate id equals research_artifact_propose.artifact_id, kind is exactly ResearchPlan, and project_id is the current project. Beyond the common envelope, allow only project_id, question_refs, strategy, work_packages, milestones, and optional superseded_by; never invent route, verification_profile, strategy_assessment, primary_strategy, fallback_strategy, output_dag, success_criteria, risks, or pinned_input_refs at top level. Provenance contains only created_by and updated_by. Every work package contains exactly id, title, track, objective, input_refs, depends_on, planned_outputs, materialized_outputs, success_criteria, and status; start materialized_outputs empty and status TODO. Every planned output contains exactly local_id, kind, description, required, and depends_on_outputs, plus verification_profile for ScientificClaim. Every milestone contains exactly id, description, acceptance_criteria, and status PENDING. Copy a non-empty canonical revision-pinned active ResearchQuestion reference into question_refs. Validate once before propose; on failure change only named fields and retain the authorized identity binding.
10. In PROPOSAL_REVIEW map the submitted methodology before recommending improvements and preserve material divergences.

## Completion Conditions

- Plan dependency graphs are acyclic
- Required outputs have stable local IDs and verification profiles
- Success criteria and fallback are explicit

## Failure Classes

- `NO_FEASIBLE_STRATEGY`
- `SUCCESS_CRITERIA_AMBIGUOUS`
- `DEPENDENCY_CYCLE`
- `RESOURCE_MISMATCH`

Report the narrowest applicable class. Do not choose a workflow transition; submit evidence for deterministic GateService mapping.

## Non-goals

- Creating Claim or Experiment artifacts
- Plan approval
- Work execution

## Permissions

- Profile: `producer`
- Sandbox: `read-only`
- Operations: `READ`, `QUERY`, `VALIDATE`, `CREATE`, `REVISE`
- Artifact kinds: `ResearchPlan`
- Field policy: Full target ResearchPlan only
- Canonical promotion: forbidden; only the host controller may accept proposals.

## Provenance

Record all of the following in the proposal, Review, or immutable resource manifest:

- `project_id`
- `run_id`
- `action_id`
- `dsh_session_id`
- `bundle_sha256`
- `input_git_commit`
- `pinned_input_refs`

## Forbidden Behaviors

- Creating placeholder Claim or Experiment artifacts
- Approving or routing the Plan
- Omitting fallback or failure criteria
- Inferring accepted science from conversation history or final prose.
- Directly modifying canonical artifacts or selecting the next global state.

## Submission

Validate every candidate, stage only allowed proposals/resources, and call `research_action_submit` with the current `action_id` and `bundle_sha256`. A natural-language final answer has no transition authority.
