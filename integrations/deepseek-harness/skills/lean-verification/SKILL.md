---
name: lean-verification
description: Independently perform a clean Lean build and axiom audit for a fixed core Claim formalization.
---

# lean-verification

Read `../_shared/artifact-boundaries.md`, `../_shared/failure-taxonomy.md`, and `../_shared/scientific-evaluation-policy.md` before acting.
Also read `../_shared/theory-verification-policy.md` and apply it without waiver.
Use only the revision-pinned ContextBundle. Treat session history as execution provenance, never scientific truth.

## Purpose

Independently build Lean proof resources and audit their axioms.

## Inputs

- Frozen ScientificClaim revision
- Lean source
- Semantic mapping
- Locked Lean toolchain

## Outputs

- LEAN Review
- AXIOM_AUDIT Review
- Build and audit resources

## Tools

- `research_artifact_read`
- `resource_read`
- `lean`
- `lake`
- `axiom_audit`
- `research_artifact_propose`
- `research_action_submit`

## Procedure

1. Perform a clean build using the locked Lean and mathlib versions.
2. Reject sorry, sorryAx, and custom axioms outside the project allowlist and record actual axioms.
3. Distinguish proof, library, formalization, and suspected mathematical failures.

## Completion Conditions

- Clean build passes
- Axiom audit passes
- Reviews include source and toolchain hashes

## Failure Classes

- `LEAN_PROOF_GAP`
- `LIBRARY_GAP`
- `FORMALIZATION_GAP`
- `UNAPPROVED_AXIOM`
- `SORRY_FOUND`
- `MATHEMATICAL_FAILURE_SUSPECTED`

Report the narrowest applicable class. Do not choose a workflow transition; submit evidence for deterministic GateService mapping.

## Non-goals

- Weakening the theorem to compile
- Treating a library gap as a false claim
- Modifying target artifacts

## Permissions

- Profile: `formal-reviewer`
- Sandbox: `scratch-write`
- Operations: `READ`, `QUERY`, `CREATE`, `RESOURCE_WRITE`, `RUN`
- Artifact kinds: `Review`
- Field policy: Create Reviews and immutable audit resources only
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
- `lean_version`
- `mathlib_commit`
- `build_manifest`
- `axiom_list`
- `independent_session`

## Forbidden Behaviors

- Rewriting the theorem or mapping
- Accepting sorry sorryAx or unapproved axioms
- Classifying a library gap as mathematical failure without evidence
- Inferring accepted science from conversation history or final prose.
- Directly modifying canonical artifacts or selecting the next global state.

## Submission

Validate every candidate, stage only allowed proposals/resources, and call `research_action_submit` with the current `action_id` and `bundle_sha256`. A natural-language final answer has no transition authority.
