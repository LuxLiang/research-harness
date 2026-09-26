---
name: consistency-review
description: Audit cross-track Claim-Evidence, literature novelty, revision, and Lean semantic consistency before writing.
---

# consistency-review

Read `../_shared/artifact-boundaries.md`, `../_shared/failure-taxonomy.md`, and `../_shared/scientific-evaluation-policy.md` before acting.
Use only the revision-pinned ContextBundle. Treat session history as execution provenance, never scientific truth.

## Purpose

Audit consistency across tracks, evidence, novelty, formalization, and manuscript inputs.

## Inputs

- Active ResearchPlan
- ScientificClaims
- Experiments
- LiteratureEvidence
- Lean and Review resources

## Outputs

- CONSISTENCY Review
- Structured consistency gate candidate

## Tools

- `research_artifact_read`
- `research_artifact_resolve`
- `resource_read`
- `research_artifact_propose`
- `research_action_submit`

## Procedure

1. Check Theory to Experiment, Claim to Evidence, Literature to novelty, and Claim to Lean relationships.
2. Detect stale revisions, contradictions, invalidated novelty, and semantic mismatches.
3. Treat a controller-authored VERIFIED revision r+1 as the lifecycle promotion of independently reviewed revision r only when verification.review_refs and a VERIFIES edge pin a same-Claim PASS Review at exactly r and the scientific statement, assumptions, dependencies, and proof-resource refs are unchanged. Count that Review as current lifecycle evidence; arbitrary edits, skipped revisions, and stale Reviews remain STALE_EVIDENCE.
4. Identify the smallest affected artifact and planned-output set for follow-up.

## Completion Conditions

- Return PASS or a targeted typed failure
- Every failure has target refs and output IDs

## Failure Classes

- `STALE_EVIDENCE`
- `CLAIM_EVIDENCE_CONFLICT`
- `NOVELTY_INVALIDATED`
- `SEMANTIC_MISMATCH`

Report the narrowest applicable class. Do not choose a workflow transition; submit evidence for deterministic GateService mapping.

## Non-goals

- Direct artifact repair
- Global reruns
- Hiding contradictions

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
- `consistency_snapshot`

## Forbidden Behaviors

- Modifying reviewed artifacts
- Ignoring stale or contradictory evidence
- Requesting work outside the minimal affected set
- Inferring accepted science from conversation history or final prose.
- Directly modifying canonical artifacts or selecting the next global state.

## Submission

Validate every candidate, stage only allowed proposals/resources, and call `research_action_submit` with the current `action_id` and `bundle_sha256`. A natural-language final answer has no transition authority.
