---
name: experiment-verification
description: Independently audit a frozen Experiment for protocol fidelity, statistics, fairness, selective reporting, and reproducibility.
---

# experiment-verification

Read `../_shared/artifact-boundaries.md`, `../_shared/failure-taxonomy.md`, and `../_shared/scientific-evaluation-policy.md` before acting.
Also read `../_shared/experiment-integrity-policy.md` and preserve the protocol lock.
Use only the revision-pinned ContextBundle. Treat session history as execution provenance, never scientific truth.

## Purpose

Independently audit Experiment fidelity, statistics, fairness, and reproducibility.

## Inputs

- Frozen Experiment revision
- Raw outputs
- Pinned code data and configuration
- Plan success criteria

## Outputs

- Revision-pinned EXPERIMENT Review with supported contradicted or inconclusive assessment

## Tools

- `research_artifact_read`
- `research_artifact_resolve`
- `resource_read`
- `bash`
- `filesystem`
- `metric_recompute`
- `research_artifact_propose`
- `research_action_submit`

## Procedure

1. Check protocol fidelity and independently recompute primary metrics.
2. Audit seeds, variance, statistics, baseline fairness, leakage, ablations, and selective reporting.
3. Preserve negative results and classify only by preregistered outcome rules.

## Completion Conditions

- Outcome is SUPPORTED CONTRADICTED or INCONCLUSIVE
- Every finding traces to raw resources
- Reproduction attempts are recorded

## Failure Classes

- `REPRODUCIBILITY_FAILURE`
- `STATISTICAL_INSUFFICIENCY`
- `DATA_LEAKAGE`
- `BASELINE_UNFAIRNESS`
- `SELECTIVE_REPORTING`
- `PROTOCOL_DEVIATION`

Report the narrowest applicable class. Do not choose a workflow transition; submit evidence for deterministic GateService mapping.

## Non-goals

- Modifying Experiment
- Discarding contradictions
- Post-hoc outcome rules

## Permissions

- Profile: `experiment-reviewer`
- Sandbox: `run-write`
- Operations: `READ`, `QUERY`, `CREATE`, `RESOURCE_WRITE`, `RUN`
- Artifact kinds: `Review`
- Field policy: Create Review and audit resources only
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
- `reproduction_run_ids`
- `recomputation_hashes`
- `independent_session`

## Forbidden Behaviors

- Modifying the reviewed Experiment
- Using post-hoc metrics thresholds or exclusions
- Hiding failed negative or contradictory results
- Inferring accepted science from conversation history or final prose.
- Directly modifying canonical artifacts or selecting the next global state.

## Submission

Validate every candidate, stage only allowed proposals/resources, and call `research_action_submit` with the current `action_id` and `bundle_sha256`. A natural-language final answer has no transition authority.
