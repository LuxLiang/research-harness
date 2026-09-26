---
name: proposal-revision
description: Revise a frozen academic research proposal from revision-pinned novelty and correctness Reviews while preserving its language, structure, evidence boundaries, and human authority over major scientific changes.
---

# proposal-revision

Read `../_shared/artifact-boundaries.md`, `../_shared/failure-taxonomy.md`, and `../_shared/scientific-evaluation-policy.md` before acting.
Use only the revision-pinned ContextBundle. Treat session history as execution provenance, never scientific truth.

## Purpose

Produce an authorized immutable proposal revision and exact change log.

## Inputs

- Current ResearchProposal revision
- Novelty Review
- PROPOSAL_CORRECTNESS Review
- Accepted Decisions

## Outputs

- Immutable proposal Markdown
- Change log
- ResearchProposal revision

## Tools

- `research_artifact_read`
- `research_artifact_resolve`
- `resource_read`
- `research_proposal_write`
- `research_artifact_validate`
- `research_artifact_propose`
- `research_action_submit`

## Procedure

1. Preserve source language section order and academic voice.
2. Apply minor fixes and only human-authorized material changes.
3. Record issue IDs decisions locators and dispositions in the change log.

## Completion Conditions

- Every material change has authority
- Current document and change log hashes resolve
- No immutable input was overwritten

## Failure Classes

- `HUMAN_DECISION_REQUIRED`
- `UNAUTHORIZED_MATERIAL_CHANGE`
- `SOURCE_MAP_GAP`
- `EVIDENCE_GAP`

Report the narrowest applicable class. Do not choose a workflow transition; submit evidence for deterministic GateService mapping.

## Non-goals

- Changing research intent without approval
- Producing scientific results

## Permissions

- Profile: `producer`
- Sandbox: `read-only`
- Operations: `READ`, `QUERY`, `RESOURCE_WRITE`, `REVISE`
- Artifact kinds: `ResearchProposal`
- Field policy: Full target ResearchProposal plus create-only revision resources
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
- `proposal_revision`
- `decision_refs`
- `document_sha256`
- `change_log_sha256`

## Forbidden Behaviors

- Overwriting immutable resources
- Reordering without authority
- Inventing evidence
- Resolving major issues without a Decision
- Inferring accepted science from conversation history or final prose.
- Directly modifying canonical artifacts or selecting the next global state.

## Submission

Validate every candidate, stage only allowed proposals/resources, and call `research_action_submit` with the current `action_id` and `bundle_sha256`. A natural-language final answer has no transition authority.
