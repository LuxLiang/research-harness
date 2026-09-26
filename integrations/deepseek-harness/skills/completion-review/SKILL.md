---
name: completion-review
description: Judge completion strictly against the active ResearchPlan success criteria after synthesis.
---

# completion-review

Read `../_shared/artifact-boundaries.md`, `../_shared/failure-taxonomy.md`, and `../_shared/scientific-evaluation-policy.md` before acting.
Use only the revision-pinned ContextBundle. Treat session history as execution provenance, never scientific truth.

## Purpose

Evaluate research completion against preregistered ResearchPlan success criteria.

## Inputs

- Active ResearchPlan
- Materialized outputs
- ScientificClaims
- Experiments
- Reviews

## Outputs

- COMPLETION Review
- Structured completion gate candidate

## Tools

- `research_artifact_read`
- `research_artifact_resolve`
- `research_artifact_propose`
- `research_action_submit`

## Procedure

1. Evaluate every work package, required output, and success criterion against pinned evidence.
2. Never replace criteria evaluation with artifact counts or completion percentages.
3. Treat a controller-authored VERIFIED revision r+1 as the lifecycle promotion of an independently reviewed revision r only when verification.review_refs and a VERIFIES edge pin the same-Claim PASS Review at exactly r and the scientific body is unchanged; never demand a Review of the already-promoted revision or accept an arbitrary stale Review.
4. Use research-artifact/v0.1.2 with assessment scheme COMPLETION and outcome COMPLETE or INCOMPLETE; failure-class labels belong in issues or the action failure classification, never assessment.outcome.
5. Return COMPLETE, INCOMPLETE, or exactly one typed rethink level.

## Completion Conditions

- Every criterion has evidence and a disposition
- Verdict is one allowed deterministic value

## Failure Classes

- `CRITERIA_UNEVALUABLE`
- `EVIDENCE_INCOMPLETE`

Report the narrowest applicable class. Do not choose a workflow transition; submit evidence for deterministic GateService mapping.

## Non-goals

- Forced completion
- Automatic rethink escalation
- Artifact modification

## Permissions

- Profile: `reviewer`
- Sandbox: `read-only`
- Operations: `READ`, `QUERY`, `CREATE`
- Artifact kinds: `Review`
- Field policy: Create Review only
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
- `plan_revision`
- `criterion_evidence_refs`

## Forbidden Behaviors

- Using artifact counts instead of Plan criteria
- Failing open or forcing completion
- Modifying reviewed outputs
- Inferring accepted science from conversation history or final prose.
- Directly modifying canonical artifacts or selecting the next global state.

## Submission

Validate every candidate, stage only allowed proposals/resources, and call `research_action_submit` with the current `action_id` and `bundle_sha256`. A natural-language final answer has no transition authority.
