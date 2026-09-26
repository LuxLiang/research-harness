---
name: targeted-followup
description: Convert gate blockers into the smallest additional Literature, Theory, Experiment, or Formal work set.
---

# targeted-followup

Read `../_shared/artifact-boundaries.md`, `../_shared/failure-taxonomy.md`, and `../_shared/scientific-evaluation-policy.md` before acting.
Use only the revision-pinned ContextBundle. Treat session history as execution provenance, never scientific truth.

## Purpose

Convert specific blockers into the smallest feasible follow-up work set.

## Inputs

- Gate targets
- Review issues
- Active ResearchPlan
- Completed outputs

## Outputs

- ResearchPlan revision with minimal new work packages and planned outputs

## Tools

- `research_artifact_read`
- `research_artifact_query`
- `research_artifact_validate`
- `research_artifact_propose`
- `research_action_submit`

## Procedure

1. Read the active ResearchPlan and pin its exact artifact ID and revision.
2. Locate the smallest causal gap for each blocker and preserve all satisfied work.
3. Stage exactly one CREATE proposal for a successor ResearchPlan with status IN_PROGRESS; pin the completed active plan as an input and never revise it in place.
4. Add only necessary Literature, Theory, Experiment, or Formal work and dependencies.
5. Map each blocker to one work item and explicit success criteria.

## Completion Conditions

- Every blocker maps to a work item
- Unaffected tracks remain complete
- Follow-up DAG is valid

## Failure Classes

- `BLOCKER_UNLOCALIZED`
- `NO_FEASIBLE_FOLLOWUP`
- `DEPENDENCY_EXPANSION_REQUIRED`

Report the narrowest applicable class. Do not choose a workflow transition; submit evidence for deterministic GateService mapping.

## Non-goals

- Rewriting the whole Plan
- Executing follow-up
- Retrying unaffected tracks

## Permissions

- Profile: `producer`
- Sandbox: `read-only`
- Operations: `READ`, `QUERY`, `VALIDATE`, `CREATE`
- Artifact kinds: `ResearchPlan`
- Field policy: Create one successor plan pinned to the completed active ResearchPlan
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
- `source_gate_refs`
- `preserved_work_items`

## Forbidden Behaviors

- Rerunning unaffected work packages
- Executing the planned follow-up
- Escalating rethink level without a typed Review
- Revising a completed active ResearchPlan in place
- Inferring accepted science from conversation history or final prose.
- Directly modifying canonical artifacts or selecting the next global state.

## Submission

Validate every candidate, stage only allowed proposals/resources, and call `research_action_submit` with the current `action_id` and `bundle_sha256`. A natural-language final answer has no transition authority.
