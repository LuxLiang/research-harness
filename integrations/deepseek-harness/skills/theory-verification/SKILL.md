---
name: theory-verification
description: Independently and adversarially review a fixed ScientificClaim revision and informal proof during theory verification.
---

# theory-verification

Read `../_shared/artifact-boundaries.md`, `../_shared/failure-taxonomy.md`, and `../_shared/scientific-evaluation-policy.md` before acting.
Also read `../_shared/theory-verification-policy.md` and apply it without waiver.
Use only the revision-pinned ContextBundle. Treat session history as execution provenance, never scientific truth.

## Purpose

Independently and adversarially verify an informal mathematical argument.

## Inputs

- Frozen ScientificClaim revision
- Informal proof resource
- Dependency Claims
- Mathematical LiteratureEvidence

## Outputs

- Revision-pinned THEORY Review

## Tools

- `research_artifact_read`
- `research_artifact_resolve`
- `resource_read`
- `counterexample_check`
- `research_artifact_propose`
- `research_action_submit`

## Procedure

1. Check assumptions, dependency validity, logical steps, dimensions, constants, and probabilistic conditions in topological order.
2. Search boundary cases and counterexamples independently of the development session.
3. Report PASS, a precise gap, false claim evidence, or typed rethink with locators.

## Completion Conditions

- Every proof step is audited
- Every failure has a locator and evidence
- Review targets the exact Claim revision and commit
- PASS requires every claimed proof resource to be revision-pinned with a verified SHA-256
- The exact displayed Claim is checked literally without silently repairing a false clause

## Failure Classes

- `PROOF_GAP`
- `ASSUMPTION_GAP`
- `FALSE_CLAIM`
- `RETHINK_CLAIM`
- `RETHINK_PLAN`
- `RETHINK_QUESTION`
- `VERIFICATION_INCONCLUSIVE`

Report the narrowest applicable class. Do not choose a workflow transition; submit evidence for deterministic GateService mapping.

## Non-goals

- Rewriting the proof
- Modifying the target Claim
- Lean certification

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
- `target_revision`
- `target_git_commit`
- `independent_session`

## Forbidden Behaviors

- Modifying the reviewed Claim or proof
- Sharing the theory-development session
- Passing a proof with an unresolved gap
- Treating an action-local scratch path as pinned evidence
- Charitably replacing a false displayed statement with a nearby true statement
- Inferring accepted science from conversation history or final prose.
- Directly modifying canonical artifacts or selecting the next global state.

## Submission

Validate every candidate, stage only allowed proposals/resources, and call `research_action_submit` with the current `action_id` and `bundle_sha256`. A natural-language final answer has no transition authority.
