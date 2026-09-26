---
name: final-review
description: Perform adversarial final review of a frozen manuscript for novelty, theory, Lean, experiments, citations, reproducibility, and overclaiming.
---

# final-review

Read `../_shared/artifact-boundaries.md`, `../_shared/failure-taxonomy.md`, and `../_shared/scientific-evaluation-policy.md` before acting.
Use only the revision-pinned ContextBundle. Treat session history as execution provenance, never scientific truth.

## Purpose

Perform adversarial final scientific, reproducibility, traceability, and editorial review.

## Inputs

- Frozen manuscript commit
- Project revision
- In-paper Claims
- Evidence
- Lean and Experiment Reviews

## Outputs

- Revision-pinned FINAL Review
- Structured final gate candidate

## Tools

- `research_artifact_read`
- `research_artifact_resolve`
- `resource_read`
- `manuscript_read`
- `research_artifact_propose`
- `research_action_submit`

## Procedure

1. Use manuscript_read to read every frozen manuscript path and paper/traceability.yaml completely at the action input commit before issuing a verdict; record their returned SHA-256 values in the Review evidence.
2. Treat a current IN_PAPER ScientificClaim revision r+1 as covered by the independent PASS Review of VERIFIED revision r only when the controller-authored promotion changed solely status, paper_locations, revision, updated_at, and provenance.updated_by; verification.review_refs and the VERIFIES edge must still pin that same Claim at revision r, and the scientific statement, assumptions, dependencies, and proof-resource refs must be byte-for-byte unchanged. Never demand a new theory Review of such a lifecycle-only r+1 revision. Arbitrary edits, skipped revisions, or stale Reviews remain blockers.
3. Audit novelty, theory, Lean, experiments, statistics, citations, reproducibility, traceability, and overclaiming.
4. Require a self-contained submission-grade paper, not a short artifact summary: verify precise filtration and protocol, complete Base and Optimistic pseudocode, exact theorem assumptions and correction terms, proof roadmaps plus complete appendices, theorem-level related work, limitations, and an honestly labeled future experiment protocol. Missing complete proofs or replacing them with internal artifact identifiers requires EDITORIAL_REVISION when the science is already verified.
5. Confirm that the chosen-arm construction is only a matched analytical baseline and that the strict efficiency result is described only as the verified finite witness, not universal or asymptotic dominance.
6. Close the Review consistently: when assessment.outcome is PASS, set top-level status to RESOLVED, recommendation to ACCEPT, and leave no OPEN or ACCEPTED issue; for a non-passing outcome keep the Review OPEN or IN_RESOLUTION and record every unresolved issue. A PASS Review with top-level status OPEN is invalid because FINAL PASS cannot depend on an unresolved Review.
7. Classify each issue as editorial or scientific and attach precise targets and evidence.
8. Return PASS, EDITORIAL_REVISION, TARGETED_FOLLOWUP, or typed rethink without approving the Project.

## Completion Conditions

- Every hard blocker has a target and evidence
- Verdict uses the allowed structured scheme

## Failure Classes

- `HARD_BLOCKER`
- `OVERCLAIM`
- `UNREPRODUCIBLE_RESULT`
- `NOVELTY_FAILURE`
- `TRACEABILITY_GAP`

Report the narrowest applicable class. Do not choose a workflow transition; submit evidence for deterministic GateService mapping.

## Non-goals

- Editing the manuscript
- Approving the Project
- Hiding negative results

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
- `manuscript_commit`
- `project_revision`
- `independent_session`

## Forbidden Behaviors

- Editing reviewed artifacts or manuscript
- Approving the Project or choosing the transition
- Waiving a scientific hard blocker
- Inferring accepted science from conversation history or final prose.
- Directly modifying canonical artifacts or selecting the next global state.

## Submission

Validate every candidate, stage only allowed proposals/resources, and call `research_action_submit` with the current `action_id` and `bundle_sha256`. A natural-language final answer has no transition authority.
