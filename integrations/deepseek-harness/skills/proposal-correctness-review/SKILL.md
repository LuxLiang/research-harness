---
name: proposal-correctness-review
description: Independently audit an academic research proposal for evidence-level factual, logical, mathematical, statistical, methodological, feasibility, citation, and claim-strength correctness without executing the proposed research.
---

# proposal-correctness-review

Read `../_shared/artifact-boundaries.md`, `../_shared/failure-taxonomy.md`, and `../_shared/scientific-evaluation-policy.md` before acting.
Use only the revision-pinned ContextBundle. Treat session history as execution provenance, never scientific truth.

## Purpose

Independently audit a frozen academic proposal for evidence-level correctness without executing the research.

## Inputs

- ResearchProposal
- Current proposal resource
- Active ResearchQuestion
- Reconstructed ResearchPlan
- LiteratureEvidence
- Optional rubric
- Prior Reviews

## Outputs

- Revision-pinned PROPOSAL_CORRECTNESS Review

## Tools

- `research_artifact_read`
- `research_artifact_resolve`
- `resource_read`
- `academic_search`
- `academic_source_read`
- `research_artifact_propose`
- `research_artifact_validate`
- `research_action_submit`

## Procedure

1. Read every pinned textual input completely in chunks no larger than 20,000 characters. For a truncated resource_read result, continue from next_offset_chars until it is null; never judge from only the first chunk or a tool-result spill file.
2. Separate asserted facts and known results from proposed hypotheses.
3. Check citations logic assumptions mathematics statistics identification data availability method fit feasibility and claim strength.
4. Attach exact locators severities evidence and actionable resolutions.
5. Create exactly one research-artifact/v0.1.3 Review using this candidate shape. Replace placeholders with pinned values and findings; do not rename the target, reviewer, assessment, or issue fields.
{
  "schema_version": "research-artifact/v0.1.3",
  "kind": "Review",
  "id": "review-...",
  "project_id": "proj-...",
  "title": "Proposal correctness review: <title>",
  "status": "OPEN",
  "revision": 1,
  "created_at": "<RFC3339>",
  "updated_at": "<RFC3339>",
  "provenance": {
    "created_by": {"actor_type": "agent", "actor_id": "proposal-correctness-review", "session_id": "<current session>"},
    "updated_by": {"actor_type": "agent", "actor_id": "proposal-correctness-review", "session_id": "<current session>"}
  },
  "tags": ["proposal-review", "correctness"],
  "target": {
    "artifact_ref": {"id": "<ResearchProposal id>", "kind": "ResearchProposal", "revision": 4},
    "git_commit": "<input_git_commit>"
  },
  "reviewer": {
    "actor": {"actor_type": "agent", "actor_id": "proposal-correctness-review", "session_id": "<current session>"},
    "reviewer_type": "PROPOSAL_CORRECTNESS"
  },
  "summary": "<evidence-graded summary>",
  "assessment": {
    "scheme": "PROPOSAL_CORRECTNESS",
    "outcome": "REVISION_REQUIRED",
    "items": [{
      "subject": "<fact, theorem, proof chain, method, or feasibility claim>",
      "outcome": "PASS, FAIL, or INDETERMINATE",
      "confidence": 0.5,
      "evidence_refs": [{"id": "<pinned plan, question, literature, or review id>", "kind": "ResearchPlan", "revision": 1}],
      "locator": "<exact proposal section/theorem/equation/proof locator>"
    }]
  },
  "evidence_resources": [{"uri": "<pinned resource URI>", "sha256": "<64 lowercase hex>", "media_type": "text/markdown", "description": "<source description>"}],
  "issues": [{
    "id": "correctness-issue-001",
    "title": "<title>",
    "description": "<specific failed step, unsupported premise, or source gap>",
    "severity": "BLOCKER",
    "status": "OPEN",
    "evidence_refs": [{"id": "<pinned evidence id>", "kind": "ResearchPlan", "revision": 1}],
    "locator": "<exact proposal locator>",
    "suggested_resolution": "<actionable source-faithful correction or verification step>"
  }],
  "recommendation": "MAJOR_REVISION"
}
Valid assessment outcomes are PASS, REVISION_REQUIRED, INVALID, and—when the evidence is incomplete—UNCERTAIN. Valid issue severities are BLOCKER, MAJOR, MINOR, and SUGGESTION. Assessment item outcome is a non-empty calibrated string, so use PASS, FAIL, or INDETERMINATE there. Do not use HIGH/MEDIUM severities, target_ref, reviewer.actor_id at the reviewer level, criterion/result/rationale items, or free-form issue codes.

6. Validate the final Review before research_action_submit. A scientific outcome of REVISION_REQUIRED, INVALID, or UNCERTAIN is submitted with outcome SUBMITTED; it is not an action-execution failure.

## Completion Conditions

- Every core assertion has an evidence disposition
- Every issue has an exact locator and actionable resolution
- Review pins the current ResearchProposal revision

## Failure Classes

- `SOURCE_UNVERIFIED`
- `LOGICAL_CONFLICT`
- `MATHEMATICAL_ERROR`
- `STATISTICAL_GAP`
- `METHOD_INVALID`
- `DATA_UNAVAILABLE`
- `FEASIBILITY_GAP`

Report the narrowest applicable class. Do not choose a workflow transition; submit evidence for deterministic GateService mapping.

## Non-goals

- Executing experiments
- Formal proof
- Editing the proposal

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
- `proposal_revision`
- `source_versions`
- `access_times`

## Forbidden Behaviors

- Editing reviewed resources
- Treating an untested hypothesis as a false fact
- Accepting inaccessible decisive evidence as verified
- Choosing workflow transitions
- Inferring accepted science from conversation history or final prose.
- Directly modifying canonical artifacts or selecting the next global state.

## Submission

Validate every candidate, stage only allowed proposals/resources, and call `research_action_submit` with the current `action_id` and `bundle_sha256`. A natural-language final answer has no transition authority.
