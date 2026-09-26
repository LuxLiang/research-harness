---
name: experiment-design
description: Lock a falsifiable and reproducible Experiment protocol, distinguishing prospective preregistration from post-hoc follow-up when outcomes are already visible.
---

# experiment-design

Read `../_shared/artifact-boundaries.md`, `../_shared/failure-taxonomy.md`, and `../_shared/scientific-evaluation-policy.md` before acting.
Also read `../_shared/experiment-integrity-policy.md` and preserve the protocol lock.
Use only the revision-pinned ContextBundle. Treat session history as execution provenance, never scientific truth.

## Purpose

Lock a reproducible and falsifiable Experiment protocol while distinguishing prospective preregistration from post-hoc follow-up.

## Inputs

- Planned Experiment output
- Frozen Claim revision
- LiteratureEvidence
- Dataset and code metadata

## Outputs

- Experiment proposal with protocol lock and READY candidate

## Tools

- `research_artifact_read`
- `research_artifact_query`
- `resource_metadata`
- `resource_write`
- `research_artifact_validate`
- `research_artifact_propose`
- `research_action_submit`

## Procedure

1. Define operationalization, baselines, data splits, metrics, seeds, ablations, stress tests, statistics, and exclusions.
2. Audit the pinned ContextBundle for prior outcome evidence; if any is visible, label the protocol post-hoc or retrospective, enumerate the visible evidence, and forbid outcome-blind or confirmatory language.
3. Define SUPPORTED, CONTRADICTED, and INCONCLUSIVE rules before viewing outcomes only for a genuinely prospective protocol; otherwise freeze successor-local rules without claiming prospective status.
4. Materialize synthetic or inline datasets as immutable resources and pin each returned URI and SHA-256 before READY.
5. Compile every generated argv against the pinned entrypoint parser/source, preserving exact option names and list tokenization; fail PROTOCOL_GAP rather than inventing flags.
6. Pin Claim, code, data, configuration, environment, and protocol hash only after locked fields are final; on mismatch use the validator-reported expected hash for the exact candidate.

## Completion Conditions

- Outcome rules are unambiguous
- Required resources and versions are pinned
- Visibility boundary is truthful and post-hoc protocols disclaim confirmatory inference

## Failure Classes

- `PROTOCOL_GAP`
- `BASELINE_UNAVAILABLE`
- `DATASET_UNAVAILABLE`
- `METRIC_AMBIGUOUS`
- `COMPUTE_INFEASIBLE`

Report the narrowest applicable class. Do not choose a workflow transition; submit evidence for deterministic GateService mapping.

## Non-goals

- Executing experiments
- Post-hoc threshold selection
- Interpreting results

## Permissions

- Profile: `producer`
- Sandbox: `read-only`
- Operations: `READ`, `QUERY`, `VALIDATE`, `CREATE`, `REVISE`
- Artifact kinds: `Experiment`
- Field policy: Full protocol fields only before READY
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
- `protocol_sha256`
- `visible_data_boundary`

## Forbidden Behaviors

- Inspecting outcome data before a protocol advertised as prospective
- Describing a post-hoc successor as outcome-blind or confirmatory
- Defining outcome rules after execution
- Giving the proposed method an unfair tuning budget
- Inferring accepted science from conversation history or final prose.
- Directly modifying canonical artifacts or selecting the next global state.

## Submission

Validate every candidate, stage only allowed proposals/resources, and call `research_action_submit` with the current `action_id` and `bundle_sha256`. A natural-language final answer has no transition authority.
