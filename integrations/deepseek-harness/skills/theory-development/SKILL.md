---
name: theory-development
description: Develop theorem, lemma, hypothesis, and proof resources for an assigned theory output or replacement Claim while exposing assumptions and gaps.
---

# theory-development

Read `../_shared/artifact-boundaries.md`, `../_shared/failure-taxonomy.md`, and `../_shared/scientific-evaluation-policy.md` before acting.
Also read `../_shared/theory-verification-policy.md` and apply it without waiver.
Use only the revision-pinned ContextBundle. Treat session history as execution provenance, never scientific truth.

## Purpose

Develop explicit ScientificClaims, dependency graphs, and honest proof resources.

## Inputs

- Planned Claim output
- Mathematical LiteratureEvidence
- Dependency Claims
- Reviews
- Project input_resources in FULL_RESEARCH v0.1.4

## Outputs

- ScientificClaim proposals
- Immutable proof resources
- Replacement links

## Tools

- `research_artifact_read`
- `research_artifact_query`
- `resource_read`
- `research_artifact_validate`
- `research_artifact_propose`
- `resource_write`
- `math_check`
- `research_action_submit`

## Procedure

1. Read every relevant pinned Project input resource completely in chunks no larger than 20,000 characters.
2. On initial development create the exact planned Claim ID; on targeted revision or rethink revise the exact canonical target Claim ID with base_revision equal to the ContextBundle revision, and never create an action-local replacement, alias, or parallel Claim.
3. Fix quantifiers, domains, constants, assumptions, and dependency edges before proving.
4. Construct a proof plan and proof in dependency order while testing boundary cases.
5. Record every gap and counterexample concern; stop at SUPPORTED and never self-verify.

## Completion Conditions

- Claim is at least FORMALIZED
- Dependencies form a valid DAG
- Proof resources and known gaps are recorded

## Failure Classes

- `PROOF_GAP`
- `DEPENDENCY_GAP`
- `ASSUMPTION_GAP`
- `FALSE_CLAIM_SUSPECTED`

Report the narrowest applicable class. Do not choose a workflow transition; submit evidence for deterministic GateService mapping.

## Non-goals

- Self-marking VERIFIED
- Hiding proof gaps
- Choosing workflow transitions

## Permissions

- Profile: `theory`
- Sandbox: `scratch-write`
- Operations: `READ`, `QUERY`, `VALIDATE`, `CREATE`, `REVISE`, `RESOURCE_WRITE`
- Artifact kinds: `ScientificClaim`
- Field policy: Assigned target Claim only
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
- `proof_sha256`
- `dependency_revisions`

## Forbidden Behaviors

- Marking a Claim VERIFIED or IN_PAPER
- Hiding assumptions proof gaps or counterexamples
- Reusing a verifier session
- Creating a new Claim ID for a targeted revision of an existing canonical Claim
- Inferring accepted science from conversation history or final prose.
- Directly modifying canonical artifacts or selecting the next global state.

## Submission

Validate every candidate, stage only allowed proposals/resources, and call `research_action_submit` with the current `action_id` and `bundle_sha256`. A natural-language final answer has no transition authority.
