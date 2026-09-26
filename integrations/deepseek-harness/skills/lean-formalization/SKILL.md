---
name: lean-formalization
description: Faithfully map a frozen core ScientificClaim and its informal proof into Lean without changing scientific semantics.
---

# lean-formalization

Read `../_shared/artifact-boundaries.md`, `../_shared/failure-taxonomy.md`, and `../_shared/scientific-evaluation-policy.md` before acting.
Also read `../_shared/theory-verification-policy.md` and apply it without waiver.
Use only the revision-pinned ContextBundle. Treat session history as execution provenance, never scientific truth.

## Purpose

Faithfully map a frozen ScientificClaim and informal proof into Lean.

## Inputs

- Frozen ScientificClaim revision
- Informal proof
- Dependency Claims
- Lean environment manifest

## Outputs

- Lean source resource
- Assumption and conclusion mapping
- Verification-resource-only Claim revision

## Tools

- `research_artifact_read`
- `research_artifact_resolve`
- `resource_write`
- `lean`
- `lake`
- `research_artifact_validate`
- `research_artifact_propose`
- `research_action_submit`

## Procedure

1. Map assumptions, quantifiers, domains, constants, and conclusion bidirectionally before coding.
2. Write a theorem that preserves the mapping without strengthening assumptions or weakening conclusions.
3. Record every ambiguous or unavailable mapping instead of silently changing semantics.

## Completion Conditions

- Lean theorem parses
- Bidirectional mapping is complete
- Source and mapping hashes are recorded

## Failure Classes

- `FORMALIZATION_GAP`
- `LIBRARY_GAP`
- `AMBIGUOUS_CLAIM`
- `SEMANTIC_CHANGE_REQUIRED`

Report the narrowest applicable class. Do not choose a workflow transition; submit evidence for deterministic GateService mapping.

## Non-goals

- Changing Claim semantics
- Declaring the proof verified
- Hiding unmapped assumptions

## Permissions

- Profile: `formal`
- Sandbox: `scratch-write`
- Operations: `READ`, `QUERY`, `VALIDATE`, `REVISE`, `RESOURCE_WRITE`, `RUN`
- Artifact kinds: `ScientificClaim`
- Field policy: Verification protocol and resource refs only
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
- `source_sha256`
- `mapping_sha256`

## Forbidden Behaviors

- Strengthening assumptions or weakening conclusions
- Inserting sorry sorryAx or custom axioms
- Declaring the Lean proof verified
- Inferring accepted science from conversation history or final prose.
- Directly modifying canonical artifacts or selecting the next global state.

## Submission

Validate every candidate, stage only allowed proposals/resources, and call `research_action_submit` with the current `action_id` and `bundle_sha256`. A natural-language final answer has no transition authority.
