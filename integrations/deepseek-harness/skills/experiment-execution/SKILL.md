---
name: experiment-execution
description: Execute a frozen Experiment protocol faithfully and record complete reproducibility and raw-output provenance.
---

# experiment-execution

Read `../_shared/artifact-boundaries.md`, `../_shared/failure-taxonomy.md`, and `../_shared/scientific-evaluation-policy.md` before acting.
Also read `../_shared/experiment-integrity-policy.md` and preserve the protocol lock.
Use only the revision-pinned ContextBundle. Treat session history as execution provenance, never scientific truth.

## Purpose

Faithfully execute a frozen Experiment and preserve all raw outcomes.

## Inputs

- READY Experiment
- Protocol lock
- Pinned code data configuration and environment

## Outputs

- Run records
- Raw output resources
- Execution failures

## Tools

- `research_artifact_read`
- `resource_read`
- `bash`
- `filesystem`
- `runtime`
- `research_artifact_validate`
- `research_artifact_propose`
- `research_action_submit`

## Procedure

1. Verify the protocol hash and derive stable run IDs before execution.
2. Execute all preregistered runs and preserve raw outputs, failures, and checksums.
3. Record engineering deviations; stop and request redesign for scientific protocol changes.

## Completion Conditions

- Every required run terminates
- Every run pins protocol code data config and environment
- Raw outputs and failures are retained

## Failure Classes

- `IMPLEMENTATION_FAILURE`
- `TIMEOUT`
- `RESOURCE_EXHAUSTED`
- `PROTOCOL_DEVIATION`
- `DATA_CORRUPTION`

Report the narrowest applicable class. Do not choose a workflow transition; submit evidence for deterministic GateService mapping.

## Non-goals

- Interpreting results
- Selecting favorable seeds
- Changing locked protocol fields

## Permissions

- Profile: `experiment`
- Sandbox: `run-write`
- Operations: `READ`, `QUERY`, `VALIDATE`, `REVISE`, `RESOURCE_WRITE`, `RUN`
- Artifact kinds: `Experiment`
- Field policy: Append runs and update execution status only
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
- `protocol_revision`
- `protocol_sha256`
- `code_commit`
- `dataset_hashes`
- `config_hash`
- `environment_hash`

## Forbidden Behaviors

- Changing locked scientific protocol fields
- Omitting failed negative or contradictory runs
- Selecting metrics seeds or thresholds after seeing results
- Inferring accepted science from conversation history or final prose.
- Directly modifying canonical artifacts or selecting the next global state.

## Submission

Validate every candidate, stage only allowed proposals/resources, and call `research_action_submit` with the current `action_id` and `bundle_sha256`. A natural-language final answer has no transition authority.
