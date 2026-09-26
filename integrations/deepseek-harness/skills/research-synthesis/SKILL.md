---
name: research-synthesis
description: Merge joined theory, experiment, and literature outputs into explicit revision-pinned Claim-Evidence relationships.
---

# research-synthesis

Read `../_shared/artifact-boundaries.md`, `../_shared/failure-taxonomy.md`, and `../_shared/scientific-evaluation-policy.md` before acting.
Use only the revision-pinned ContextBundle. Treat session history as execution provenance, never scientific truth.

## Purpose

Merge theory, experiment, and literature into explicit Claim-Evidence relationships.

## Inputs

- Joined ScientificClaims
- Completed Experiments
- LiteratureEvidence
- Track Reviews

## Outputs

- Claim evidence revisions
- Synthesis Review or Decision proposals

## Tools

- `research_artifact_read`
- `research_artifact_query`
- `research_artifact_resolve`
- `research_artifact_validate`
- `research_artifact_propose`
- `research_action_submit`

## Procedure

1. Link supporting contradicting and verifying evidence to each required Claim using pinned revisions.
2. Distinguish verified, partially supported, contradicted, and unknown conclusions.
3. Preserve limitations, negative results, and unresolved contradictions verbatim in the evidence graph.
4. Keep a repaired defect from an obsolete Claim revision visible as negative provenance but mark its synthesis issue RESOLVED after the current revision has independent PASS evidence; leave OPEN only issues that still affect a current Claim or active Plan criterion.
5. Treat a controller-authored VERIFIED revision r+1 as the lifecycle promotion of independently reviewed revision r only when verification.review_refs and a VERIFIES edge pin a same-Claim PASS Review at exactly r and the scientific statement, assumptions, dependencies, and proof-resource refs are unchanged. Count that as current-revision verified evidence and never emit REVISION_MISMATCH merely because the controller recorded verification by incrementing the Claim revision; arbitrary edits, skipped revisions, and stale Reviews remain mismatches.

## Completion Conditions

- Every required output has an explicit evidence status
- All contradictions and limitations remain visible

## Failure Classes

- `EVIDENCE_GAP`
- `REVISION_MISMATCH`
- `UNRESOLVED_CONTRADICTION`

Report the narrowest applicable class. Do not choose a workflow transition; submit evidence for deterministic GateService mapping.

## Non-goals

- Inventing results
- Resolving contradictions by narrative
- Choosing completion verdict

## Permissions

- Profile: `producer`
- Sandbox: `read-only`
- Operations: `READ`, `QUERY`, `VALIDATE`, `CREATE`, `REVISE`
- Artifact kinds: `ScientificClaim`, `Review`, `Decision`
- Field policy: Claim evidence and status fields plus new Review or Decision
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
- `evidence_resource_hashes`

## Forbidden Behaviors

- Deleting or obscuring contradictory evidence
- Inventing results or literature support
- Promoting an unsupported Claim
- Inferring accepted science from conversation history or final prose.
- Directly modifying canonical artifacts or selecting the next global state.

## Submission

Validate every candidate, stage only allowed proposals/resources, and call `research_action_submit` with the current `action_id` and `bundle_sha256`. A natural-language final answer has no transition authority.
