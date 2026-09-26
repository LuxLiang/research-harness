---
name: semantic-alignment-review
description: Compare a ScientificClaim, informal theorem and proof, and Lean theorem for exact semantic alignment.
---

# semantic-alignment-review

Read `../_shared/artifact-boundaries.md`, `../_shared/failure-taxonomy.md`, and `../_shared/scientific-evaluation-policy.md` before acting.
Also read `../_shared/theory-verification-policy.md` and apply it without waiver.
Use only the revision-pinned ContextBundle. Treat session history as execution provenance, never scientific truth.

## Purpose

Audit semantic equivalence across ScientificClaim, informal proof, and Lean theorem.

## Inputs

- Frozen ScientificClaim revision
- Informal theorem and proof
- Lean source
- Assumption and conclusion mapping

## Outputs

- Revision-pinned SEMANTIC_ALIGNMENT Review

## Tools

- `research_artifact_read`
- `research_artifact_resolve`
- `resource_read`
- `research_artifact_propose`
- `research_action_submit`

## Procedure

1. Compare assumptions, quantifiers, domains, rates, constants, probability qualifiers, and conclusion strength field by field.
2. Report every strengthened assumption, weakened conclusion, or domain and quantifier mismatch precisely.
3. Remain independent from development and Lean build sessions.

## Completion Conditions

- Every semantic dimension has a PASS or precise mismatch
- All three representations are hash-pinned

## Failure Classes

- `ASSUMPTION_MISMATCH`
- `CONCLUSION_MISMATCH`
- `DOMAIN_MISMATCH`
- `QUANTIFIER_MISMATCH`
- `RATE_CONSTANT_MISMATCH`

Report the narrowest applicable class. Do not choose a workflow transition; submit evidence for deterministic GateService mapping.

## Non-goals

- Proving Lean type checks
- Editing any representation
- Repairing mismatches

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
- `claim_sha256`
- `proof_sha256`
- `lean_sha256`
- `independent_session`

## Forbidden Behaviors

- Editing Claim proof or Lean source
- Waiving a semantic mismatch
- Sharing the formalizer or prover session
- Inferring accepted science from conversation history or final prose.
- Directly modifying canonical artifacts or selecting the next global state.

## Submission

Validate every candidate, stage only allowed proposals/resources, and call `research_action_submit` with the current `action_id` and `bundle_sha256`. A natural-language final answer has no transition authority.
