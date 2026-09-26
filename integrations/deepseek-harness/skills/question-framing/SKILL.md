---
name: question-framing
description: Frame a vague research objective as a precise, scoped, falsifiable ResearchQuestion during discovery or reframing. Do not use for novelty search.
---

# question-framing

Read `../_shared/artifact-boundaries.md`, `../_shared/failure-taxonomy.md`, and `../_shared/scientific-evaluation-policy.md` before acting.
Use only the revision-pinned ContextBundle. Treat session history as execution provenance, never scientific truth.

## Purpose

Convert a vague objective into a precise and falsifiable ResearchQuestion.

## Inputs

- Project
- Accepted Decisions
- Previous ResearchQuestion when reframing
- Frozen ResearchProposal and current document in PROPOSAL_REVIEW
- Project input_resources in FULL_RESEARCH v0.1.4

## Outputs

- ResearchQuestion proposal

## Tools

- `research_artifact_read`
- `research_artifact_query`
- `resource_read`
- `research_proposal_write`
- `research_artifact_validate`
- `research_artifact_propose`
- `research_action_submit`

## Procedure

1. Read every relevant pinned Project input resource in chunks no larger than 20,000 characters, continuing from next_offset_chars until it is null.
2. Analyze the objective through Practical, Rigor, and Narrative perspectives.
3. Define problem, scope, hypotheses, expected contributions, and risks without novelty search.
4. Check that every hypothesis is testable and reconcile perspective conflicts.
5. When the pinned gate history contains two or more FEASIBILITY RETHINK_QUESTION verdicts, narrow the next ResearchQuestion to one central falsifiable novelty hypothesis. Keep known controllers, baselines, optimism variants, delay reductions, and correctness sublemmas as included supporting obligations or planned extensions rather than separate novelty hypotheses; do not silently drop required deliverables.
6. In the first DISCOVERY_QUESTION action create the ResearchQuestion with status ACTIVE; DRAFT cannot satisfy the deterministic stage gate. After a FEASIBILITY RETHINK_QUESTION, REVISE the most recent active ResearchQuestion in place instead of creating a new artifact ID, so revision-pinned LiteratureEvidence relations accumulate on one scientific target. Preserve the ID, increment revision exactly once, and retain prior evidence-bearing scope unless the gate explicitly requires narrowing it.
7. Keep ResearchQuestion.provenance schema-exact: it may contain only created_by and updated_by actor objects. Record project_id, run_id, action_id, dsh_session_id, bundle_sha256, input_git_commit, and pinned_input_refs in a narrative field such as background, never as extra provenance keys.
8. In PROPOSAL_REVIEW preserve source meaning and write a field-level source map without inventing specificity.

## Completion Conditions

- The question is precise and falsifiable
- Included and excluded scope are explicit
- Contributions and risks are recorded
- In PROPOSAL_REVIEW an immutable JSON field-level source-map resource is written with research_proposal_write.relative_path source-maps/proposal-rNNN.json and pinned in ResearchProposal.source_map

## Failure Classes

- `AMBIGUOUS_OBJECTIVE`
- `UNTESTABLE_HYPOTHESIS`
- `SCOPE_CONFLICT`
- `INPUT_INCOMPLETE`

Report the narrowest applicable class. Do not choose a workflow transition; submit evidence for deterministic GateService mapping.

## Non-goals

- Literature search
- Novelty judgment
- Plan approval

## Permissions

- Profile: `producer`
- Sandbox: `read-only`
- Operations: `READ`, `QUERY`, `VALIDATE`, `CREATE`, `REVISE`, `RESOURCE_WRITE`
- Artifact kinds: `ResearchQuestion`, `ResearchProposal`
- Field policy: Full ResearchQuestion; in PROPOSAL_STRUCTURING only status question_refs source_map and revision metadata of ResearchProposal
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

## Forbidden Behaviors

- Performing or implying a novelty search
- Inventing specificity not supported by Project or Decisions
- Choosing a workflow transition
- Inferring accepted science from conversation history or final prose.
- Directly modifying canonical artifacts or selecting the next global state.

## Submission

Validate every candidate, stage only allowed proposals/resources, and call `research_action_submit` with the current `action_id` and `bundle_sha256`. A natural-language final answer has no transition authority.
