---
name: literature-novelty
description: Adversarially investigate literature through direct, conceptual, citation-neighborhood, and equivalence search during discovery or feasibility review.
---

# literature-novelty

Read `../_shared/artifact-boundaries.md`, `../_shared/failure-taxonomy.md`, and `../_shared/scientific-evaluation-policy.md` before acting.
Use only the revision-pinned ContextBundle. Treat session history as execution provenance, never scientific truth.

## Purpose

Adversarially test novelty claims using verified literature evidence.

## Inputs

- Active ResearchQuestion
- Related ScientificClaims
- Existing LiteratureEvidence and Reviews

## Outputs

- LiteratureEvidence proposals
- Revision-pinned novelty Review at feasibility and proposal gates

## Tools

- `research_artifact_read`
- `research_artifact_query`
- `research_artifact_resolve`
- `research_artifact_validate`
- `research_artifact_propose`
- `academic_search`
- `citation_neighborhood`
- `academic_source_read`
- `resource_read`
- `research_action_submit`

## Procedure

1. Search direct wording, conceptual formulations, citation neighborhoods, and equivalent results.
2. In FULL_RESEARCH DISCOVERY_LITERATURE, use the available create slots in one action to preserve one LiteratureEvidence candidate for every materially relied-on external work, up to the action limit; do not collapse several papers into one search-log artifact or stop after the first relevant paper while a core hypothesis still lacks a search record.
3. After an in-place ResearchQuestion reframe, query prior canonical LiteratureEvidence. When a fully read paper's recorded result and difference still apply to the revised central target, REVISE that LiteratureEvidence to append a relation to the current ResearchQuestion revision while preserving every older relation and all negative or contradictory content. Reuse verified evidence instead of rerunning the same search; never retarget evidence whose scientific relationship changed.
4. Fetch decisive open full texts with academic_source_read and read each returned text_resource completely with resource_read before assigning theorem-level outcomes.
5. When rereading a canonical LiteratureEvidence source_resource, pass its exact recorded uri to resource_read; never reconstruct a filename from the sha256 digest.
6. Verify decisive sources at exact theorem, section, or page locators.
7. Classify each target as OPEN, PARTIAL, KNOWN, EQUIVALENT_KNOWN, or UNCERTAIN.
8. For FULL_RESEARCH feasibility, use PARTIAL only when verified sources establish the closest material baselines and the exact target differs along explicit falsifiable dimensions, so the remaining uncertainty is the research obligation itself. Use UNCERTAIN when decisive sources are unreadable/unverified or the target-to-baseline difference cannot yet be established. This distinction does not weaken PROPOSAL_REVIEW's final same-revision novelty gate.
9. For a FULL_RESEARCH theory-verification question, assess feasibility novelty on the central claimed difference, not on whether literature already proves every correctness hypothesis or makes every supporting mechanism independently novel. When fully read closest sources establish incompatible feedback/objective/theorem premises and the proposed combination is explicitly falsifiable, classify that central gap as PARTIAL even though its theorem remains to be proved or refuted; theorem truth belongs to theory development and verification. Controller, optimism, and delay may be KNOWN secondary mechanisms without erasing a distinct central target.
10. Before submitting a FULL_RESEARCH feasibility Review, cross-check its outcome against canonical LiteratureEvidence. If fully read evidence with exact theorem/section/page locators explicitly records target-specific differences for the central hypothesis and no evidence classifies it KNOWN or EQUIVALENT, the Review must not claim that no verified falsifiable difference exists or use the absence of a paper proving the proposed theorem as the sole reason for UNCERTAIN. Preserve unresolved correctness as a theory obligation and use PARTIAL with calibrated confidence; use UNCERTAIN only for a separately identified unreadable source or genuinely unestablished central difference.
11. Keep feasibility issue severity consistent with the deterministic gate. An OPEN/BLOCKER or OPEN/MAJOR issue means the central question is not feasible as framed and therefore cannot accompany an OPEN/PARTIAL PASS. If verified prior art has already been excluded by the active question's narrowed scope, record that limitation as RESOLVED (with the schema-required resolution) or as a non-blocking MINOR/SUGGESTION; do not report the discarded broad-firstness claim as an unresolved MAJOR against the narrowed question. Treat uncertainty in an explicitly supporting or conditional extension as non-blocking at this discovery gate unless it invalidates the central claimed difference. This calibration changes workflow severity, never the scientific assessment item or its evidence.
12. Represent each actual external work, never the proposal or a search-log bundle, as one research-artifact/v0.1.2 LiteratureEvidence with the full common envelope. Its paper requires title, non-empty authors, year, venue, and an identifier; relations target the pinned active ResearchQuestion or ScientificClaim and use BASELINE, EXTENSION, CONTRADICTION, or EQUIVALENT.
13. For LiteratureEvidence use object-valued main_results entries with statement and locator, differences entries with dimension/cited_work/current_research, novelty_impact with assessment/rationale, confidence with score/rationale, and source_resource for the captured source.
14. Branch on state and workflow_mode. In FULL_RESEARCH DISCOVERY_LITERATURE there is no ResearchProposal and no Review: create and submit only valid LiteratureEvidence candidates targeting the active ResearchQuestion or ScientificClaim. In FULL_RESEARCH DISCOVERY_FEASIBILITY_GATE create a research-artifact/v0.1.2 NOVELTY Review targeting the active ResearchQuestion and submit the complete FEASIBILITY gate object. In PROPOSAL_REVIEW create the proposal novelty Review as research-artifact/v0.1.3 with the full common envelope. Pin every Review target to the current target artifact revision from the ContextBundle and target.git_commit to input_git_commit; never substitute a document rev-NNN number. Use reviewer.actor plus reviewer_type LITERATURE, assessment scheme NOVELTY, item fields subject/outcome/confidence/evidence_refs, complete issue fields, and captured manifests in evidence_resources.
15. Use these exact candidate shapes as the starting point; replace placeholders with pinned values and evidence, but do not rename, omit, or add fields unless the schema requires it.

LiteratureEvidence CREATE candidate (one candidate per actual external paper):
{
  "schema_version": "research-artifact/v0.1.2",
  "kind": "LiteratureEvidence",
  "id": "lit-...",
  "project_id": "proj-...",
  "title": "Evidence: <paper title>",
  "status": "ASSESSED",
  "revision": 1,
  "created_at": "<RFC3339>",
  "updated_at": "<RFC3339>",
  "provenance": {
    "created_by": {"actor_type": "agent", "actor_id": "literature-novelty", "session_id": "<current session>"},
    "updated_by": {"actor_type": "agent", "actor_id": "literature-novelty", "session_id": "<current session>"}
  },
  "tags": ["proposal-review", "novelty"],
  "paper": {
    "title": "<actual external paper title>",
    "authors": [{"name": "<author>"}],
    "year": 2026,
    "venue": "<venue or arXiv>",
    "identifiers": {"arxiv": "<id>"},
    "version": "<version if known>"
  },
  "relations": [{
    "target_ref": {"id": "<pinned rq or claim id>", "kind": "ResearchQuestion", "revision": 1},
    "type": "BASELINE",
    "explanation": "<relationship>"
  }],
  "main_results": [{"statement": "<result>", "locator": "<theorem/section/page or metadata locator>"}],
  "differences": [{"dimension": "<dimension>", "cited_work": "<paper>", "current_research": "<proposal>"}],
  "novelty_impact": {"assessment": "NARROWS", "rationale": "<why>"},
  "confidence": {"score": 0.5, "rationale": "<why>"},
  "source_resource": {"uri": "<captured resource URI>", "sha256": "<64 lowercase hex>", "media_type": "application/json", "description": "<capture>"}
}

FULL_RESEARCH FEASIBILITY Review CREATE candidate:
{
  "schema_version": "research-artifact/v0.1.2",
  "kind": "Review",
  "id": "review-<exact allowed create id>",
  "project_id": "proj-...",
  "title": "Feasibility novelty review: <question title>",
  "status": "OPEN",
  "revision": 1,
  "created_at": "<RFC3339>",
  "updated_at": "<RFC3339>",
  "provenance": {
    "created_by": {"actor_type": "agent", "actor_id": "literature-novelty", "session_id": "<current session>"},
    "updated_by": {"actor_type": "agent", "actor_id": "literature-novelty", "session_id": "<current session>"}
  },
  "tags": ["feasibility", "novelty"],
  "target": {
    "artifact_ref": {"id": "<active ResearchQuestion id>", "kind": "ResearchQuestion", "revision": 1},
    "git_commit": "<input_git_commit>"
  },
  "reviewer": {
    "actor": {"actor_type": "agent", "actor_id": "literature-novelty", "session_id": "<current session>"},
    "reviewer_type": "LITERATURE"
  },
  "summary": "<calibrated feasibility summary>",
  "assessment": {
    "scheme": "NOVELTY",
    "outcome": "PARTIAL",
    "items": [{
      "subject": "<core target>",
      "outcome": "PARTIAL",
      "confidence": 0.5,
      "evidence_refs": [{"id": "<canonical LiteratureEvidence id>", "kind": "LiteratureEvidence", "revision": 1}]
    }]
  },
  "evidence_resources": [{"uri": "<captured manifest URI>", "sha256": "<64 lowercase hex>", "media_type": "application/json", "description": "<capture>"}],
  "issues": [{
    "id": "novelty-issue-001",
    "title": "<non-blocking feasibility limitation>",
    "description": "<why this narrows interpretation without invalidating the central target>",
    "severity": "MINOR",
    "status": "OPEN",
    "evidence_refs": [{"id": "<canonical LiteratureEvidence id>", "kind": "LiteratureEvidence", "revision": 1}]
  }],
  "recommendation": "MINOR_REVISION"
}

FULL_RESEARCH FEASIBILITY gate_assessment_json (all fields required):
{
  "gate_id": "gate-<action slug>",
  "gate_type": "FEASIBILITY",
  "verdict": "PASS",
  "based_on": [
    {"id": "<active ResearchQuestion id>", "kind": "ResearchQuestion", "revision": 1},
    {"id": "<every canonical LiteratureEvidence cited by the Review>", "kind": "LiteratureEvidence", "revision": 1},
    {"id": "<NOVELTY Review create id>", "kind": "Review", "revision": 1}
  ],
  "target_refs": [{"id": "<active ResearchQuestion id>", "kind": "ResearchQuestion", "revision": 1}],
  "target_output_ids": [],
  "review_refs": [{"id": "<NOVELTY Review create id>", "kind": "Review", "revision": 1}],
  "rationale": "<why OPEN or PARTIAL remains feasible, or why KNOWN/EQUIVALENT_KNOWN requires rethink>",
  "recorded_at": "<RFC3339>"
}
FEASIBILITY maps Review assessment OPEN or PARTIAL to gate verdict PASS, and KNOWN, EQUIVALENT_KNOWN, or UNCERTAIN to RETHINK_QUESTION. A PASS based_on list must include the active ResearchQuestion, every assessed LiteratureEvidence materially relied on by the Review, and the new Review ref. Do not use a two-field gate object and never force incomplete evidence to PARTIAL merely to pass.

Proposal novelty Review CREATE candidate:
{
  "schema_version": "research-artifact/v0.1.3",
  "kind": "Review",
  "id": "review-...",
  "project_id": "proj-...",
  "title": "Proposal novelty review: <title>",
  "status": "OPEN",
  "revision": 1,
  "created_at": "<RFC3339>",
  "updated_at": "<RFC3339>",
  "provenance": {
    "created_by": {"actor_type": "agent", "actor_id": "literature-novelty", "session_id": "<current session>"},
    "updated_by": {"actor_type": "agent", "actor_id": "literature-novelty", "session_id": "<current session>"}
  },
  "tags": ["proposal-review", "novelty"],
  "target": {
    "artifact_ref": {"id": "<ResearchProposal id>", "kind": "ResearchProposal", "revision": 4},
    "git_commit": "<input_git_commit>"
  },
  "reviewer": {
    "actor": {"actor_type": "agent", "actor_id": "literature-novelty", "session_id": "<current session>"},
    "reviewer_type": "LITERATURE"
  },
  "summary": "<summary>",
  "assessment": {
    "scheme": "NOVELTY",
    "outcome": "UNCERTAIN",
    "items": [{
      "subject": "<core hypothesis or contribution>",
      "outcome": "UNCERTAIN",
      "confidence": 0.5,
      "evidence_refs": [{"id": "lit-...", "kind": "LiteratureEvidence", "revision": 1}],
      "locator": "<proposal locator>"
    }]
  },
  "evidence_resources": [{"uri": "<manifest URI>", "sha256": "<64 lowercase hex>", "media_type": "application/json", "description": "<search manifest>"}],
  "issues": [{
    "id": "novelty-issue-001",
    "title": "<title>",
    "description": "<description>",
    "severity": "MAJOR",
    "status": "OPEN",
    "evidence_refs": [{"id": "lit-...", "kind": "LiteratureEvidence", "revision": 1}],
    "locator": "<proposal locator>",
    "suggested_resolution": "<specific revision or verification step>"
  }],
  "recommendation": "MAJOR_REVISION"
}
Valid NOVELTY assessment outcomes are OPEN, PARTIAL, KNOWN, EQUIVALENT_KNOWN, UNCERTAIN, PASS, REVISION_REQUIRED, and INVALID. Valid issue severities are BLOCKER, MAJOR, MINOR, and SUGGESTION. The actor object is nested under reviewer.actor. The target object contains artifact_ref and git_commit. There is no criterion/result/rationale item shape and no HIGH/MEDIUM severity.

16. Validate the final candidates before submission. In FULL_RESEARCH DISCOVERY_LITERATURE submit one or more LiteratureEvidence candidates with outcome SUBMITTED and do not fail because ResearchProposal is absent. In DISCOVERY_FEASIBILITY_GATE validate the v0.1.2 Review and submit its exact create ref in a complete gate_assessment_json. In PROPOSAL_REVIEW validate LiteratureEvidence and Review candidates together. Treat UNCERTAIN, KNOWN, and EQUIVALENT_KNOWN as valid scientific outcomes submitted with outcome SUBMITTED, not as execution failures.
17. If a decisive source is SOURCE_UNVERIFIED, encode that as an UNCERTAIN assessment item and an OPEN issue, then submit the valid artifacts with outcome SUBMITTED. SOURCE_UNVERIFIED is a scientific limitation and gate failure, not an action-execution failure. Use FAILED only when the tools cannot create and validate the required artifact contract.

## Completion Conditions

- Every core hypothesis has a search record
- Every known or equivalent finding is either verified or explicitly SOURCE_UNVERIFIED and UNCERTAIN
- Confidence and limitations are explicit

## Failure Classes

- `NOVELTY_KNOWN`
- `NOVELTY_EQUIVALENT`
- `NOVELTY_UNCERTAIN`
- `SOURCE_UNVERIFIED`
- `SEARCH_TOOL_UNAVAILABLE`

Report the narrowest applicable class. Do not choose a workflow transition; submit evidence for deterministic GateService mapping.

## Non-goals

- Proving absolute novelty
- Research planning
- Treating no search hit as proof of openness

## Permissions

- Profile: `producer`
- Sandbox: `read-only`
- Operations: `READ`, `QUERY`, `VALIDATE`, `CREATE`, `REVISE`
- Artifact kinds: `LiteratureEvidence`, `Review`
- Field policy: Literature production or Review creation according to state
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
- `queries`
- `source_versions`
- `access_times`

## Forbidden Behaviors

- Fabricating a citation or source locator
- Labeling uncertain evidence as certain
- Hiding direct equivalent or contradictory prior work
- Inferring accepted science from conversation history or final prose.
- Directly modifying canonical artifacts or selecting the next global state.

## Submission

Validate every candidate, stage only allowed proposals/resources, and call `research_action_submit` with the current `action_id` and `bundle_sha256`. A natural-language final answer has no transition authority.
