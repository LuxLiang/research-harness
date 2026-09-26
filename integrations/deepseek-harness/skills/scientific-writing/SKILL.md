---
name: scientific-writing
description: Write or editorially revise a scientific manuscript using only accepted, revision-pinned research artifacts and traceable resources.
---

# scientific-writing

Read `../_shared/artifact-boundaries.md`, `../_shared/failure-taxonomy.md`, and `../_shared/scientific-evaluation-policy.md` before acting.
Use only the revision-pinned ContextBundle. Treat session history as execution provenance, never scientific truth.

## Purpose

Produce a manuscript whose scientific statements are fully traceable to accepted artifacts.

## Inputs

- Verified ScientificClaims
- Completed Experiments
- Verified LiteratureEvidence
- Reviews
- Existing manuscript

## Outputs

- Manuscript resources
- Figures and tables
- paper/traceability.yaml
- Anchor bindings

## Tools

- `research_artifact_read`
- `research_artifact_resolve`
- `resource_read`
- `paper_filesystem`
- `citation_validate`
- `research_action_submit`

## Procedure

1. Produce a self-contained submission-grade manuscript rather than an artifact synopsis: include the precise protocol and filtration, both algorithms as executable pseudocode, all theorem assumptions and correction terms, proof roadmaps in the main text, complete proofs in appendices, a theorem-level related-work comparison, limitations, and the preregistered future experiment protocol without fabricating results.
2. Read every proof resource referenced by an included VERIFIED or IN_PAPER ScientificClaim completely with resource_read. Faithfully reconstruct its definitions, lemmas, and proof steps; do not replace a complete proof with a citation to an internal artifact or a one-paragraph assertion.
3. Present Base and Optimistic as the two proposed algorithms, and describe the chosen-arm construction only as a premise-matched analytical baseline. State the finite strict-separation witness exactly as finite and never inflate it into universal or asymptotic dominance.
4. Prepare resources and write only claims, results, numbers, and novelty statements supported by pinned artifacts.
5. Bind every theorem, major empirical result, novelty statement, figure, and table to revision-pinned evidence.
6. Audit traceability and label unsupported material as discussion, hypothesis, or future work.
7. Write paper/traceability.yaml using the exact research-traceability/v0.1 contract with only traceability_version, project_id, manuscript_paths, and entries at top level; every entry has exactly anchor, statement_type, source_refs, and resource_refs. Put action and manuscript provenance in paper/provenance.yaml, never as extra traceability keys.
8. After the manuscript and schema-valid traceability are durable, call research_action_submit with proposal_ids_json set to [] and outcome SUBMITTED; a writer action has no artifact proposals and must not use FAILED merely because the proposal list is empty.

## Completion Conditions

- The manuscript is self-contained and submission-grade apart from explicitly unexecuted experiments
- Every included theorem has a complete proof or exact appendix pointer
- No major statement lacks a trace
- All numbers come from pinned runs
- Citation keys resolve to LiteratureEvidence

## Failure Classes

- `TRACEABILITY_GAP`
- `MISSING_RESOURCE`
- `CITATION_FAILURE`
- `OVERCLAIM`

Report the narrowest applicable class. Do not choose a workflow transition; submit evidence for deterministic GateService mapping.

## Non-goals

- Creating ScientificClaims
- Changing scientific conclusions
- Inventing results

## Permissions

- Profile: `writer`
- Sandbox: `paper-write`
- Operations: `READ`, `QUERY`, `RESOURCE_WRITE`
- Artifact kinds: none
- Field policy: Paper resources only and no scientific artifact mutation
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
- `manuscript_commit`
- `traceability_sha256`

## Forbidden Behaviors

- Creating or scientifically modifying a Claim
- Writing untraced numbers figures or novelty statements
- Presenting SUPPORTED or INCONCLUSIVE results as proved or positive
- Inferring accepted science from conversation history or final prose.
- Directly modifying canonical artifacts or selecting the next global state.

## Submission

Validate every candidate, stage only allowed proposals/resources, and call `research_action_submit` with the current `action_id` and `bundle_sha256`. A natural-language final answer has no transition authority.
