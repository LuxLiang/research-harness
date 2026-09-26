"""Controlled staging and validation for canonical scientific artifacts."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .resources import resource_path
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator, FormatChecker
import yaml

from .lifecycle import stage_transition_allowed, transition_allowed
from .runtime_types import (
    PROPOSAL_VERSION,
    SUBMISSION_VERSION,
    RuntimeValidationError,
    SubmissionConflict,
    atomic_write_json,
    sha256_json,
)
from .tool_adapters import ExperimentExecutionAdapter
from .workspace import (
    ARTIFACT_KINDS,
    ArtifactRecord,
    ArtifactValidationError,
    ArtifactWorkspace,
    RevisionConflict,
)


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
OPERATIONS = {"CREATE", "REVISE"}
OUTCOMES = {"SUBMITTED", "NO_CHANGE", "FAILED"}
FAILURE_CLASSIFICATIONS = {
    "LOCAL_RETRY",
    "INCOMPLETE",
    "RETHINK_CLAIM",
    "RETHINK_PLAN",
    "RETHINK_QUESTION",
}

EXPERIMENT_PROTOCOL_FIELDS = {
    "hypothesis", "method", "baselines", "datasets", "metrics",
    "configuration", "interpretation_plan", "protocol_lock",
}
EXPERIMENT_EXECUTION_MUTABLE_FIELDS = {
    "revision", "updated_at", "provenance", "status", "runs",
}
EXPERIMENT_EXECUTION_IDENTITY_FIELDS = {
    "id", "kind", "project_id", "schema_version", "created_at",
}


@dataclass(frozen=True)
class Proposal:
    proposal_version: str
    proposal_id: str
    project_id: str
    action_id: str
    operation: str
    artifact_id: str
    kind: str
    base_revision: int | None
    bundle_sha256: str
    proposer_session_id: str
    candidate: dict[str, Any]
    proposal_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_version": self.proposal_version,
            "proposal_id": self.proposal_id,
            "project_id": self.project_id,
            "action_id": self.action_id,
            "operation": self.operation,
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "base_revision": self.base_revision,
            "bundle_sha256": self.bundle_sha256,
            "proposer_session_id": self.proposer_session_id,
            "candidate": copy.deepcopy(self.candidate),
            "proposal_sha256": self.proposal_sha256,
        }


class ArtifactService:
    """Agent-safe façade: reads canonical state and writes only quarantine."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.workspace = ArtifactWorkspace(self.root)
        self.runtime_dir = self.root / ".harness" / "research"
        self.schema_dir = resource_path(self.root, "schemas/runtime/v0.1")
        self._proposal_validator = self._load_schema("proposal.schema.json")
        self._submission_validator = self._load_schema("submission.schema.json")
        self._traceability_validator = self._load_schema("traceability.schema.json")

    def _load_schema(self, name: str) -> Draft202012Validator:
        path = self.schema_dir / name
        if not path.is_file():
            raise RuntimeValidationError(f"runtime schema not found: {path}")
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        return Draft202012Validator(schema, format_checker=FormatChecker())

    def read(self, artifact_id: str) -> dict[str, Any]:
        record = self.workspace.get(artifact_id)
        return {
            "id": record.id,
            "kind": record.kind,
            "path": str(record.path),
            "artifact": copy.deepcopy(record.data),
        }

    def query(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": record.id,
                "kind": record.kind,
                "status": record.data.get("status"),
                "revision": record.data.get("revision"),
                "path": str(record.path),
            }
            for record in self.workspace.query(
                kind=kind, status=status, project_id=project_id
            )
        ]

    def resolve(self, refs: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        resolved: list[dict[str, Any]] = []
        for ref in refs:
            record = self.workspace.get(str(ref.get("id", "")))
            if record.kind != ref.get("kind"):
                raise RuntimeValidationError(
                    f"{record.id} is {record.kind}, not {ref.get('kind')}"
                )
            pinned = ref.get("revision")
            if pinned is not None and pinned != record.data.get("revision"):
                raise RevisionConflict(
                    f"{record.id} revision is {record.data.get('revision')}, expected {pinned}"
                )
            resolved.append(self.read(record.id))
        return resolved

    def action_dir(self, project_id: str, action_id: str) -> Path:
        self._require_safe(project_id, "project_id")
        self._require_safe(action_id, "action_id")
        return self.runtime_dir / "staging" / project_id / action_id

    def stage_proposal(
        self,
        *,
        proposal_id: str,
        project_id: str,
        action_id: str,
        operation: str,
        artifact_id: str,
        kind: str,
        base_revision: int | None,
        bundle_sha256: str,
        proposer_session_id: str,
        candidate: Mapping[str, Any],
    ) -> Proposal:
        for value, label in (
            (proposal_id, "proposal_id"),
            (project_id, "project_id"),
            (action_id, "action_id"),
            (artifact_id, "artifact_id"),
        ):
            self._require_safe(value, label)
        if operation not in OPERATIONS:
            raise RuntimeValidationError(f"unknown proposal operation: {operation}")
        if kind not in ARTIFACT_KINDS:
            raise RuntimeValidationError(f"unknown artifact kind: {kind}")
        if not re.fullmatch(r"[0-9a-f]{64}", bundle_sha256):
            raise RuntimeValidationError("bundle_sha256 must be a lowercase SHA-256")
        context = self._load_context(project_id, action_id)
        if context.get("bundle_sha256") != bundle_sha256:
            raise RuntimeValidationError("proposal references a different context bundle")
        candidate_copy = copy.deepcopy(dict(candidate))
        # Execution agents own only an append/update patch, while artifact
        # schemas describe a complete Experiment. Expand a *strictly mutable*
        # patch against the canonical revision at the host boundary. Any
        # candidate that mentions a scientific/locked field remains a full
        # candidate and is checked (and rejected on change) below.
        if (
            kind == "Experiment"
            and operation == "REVISE"
            and context.get("role") == "experiment-execution"
            and set(candidate_copy).issubset(
                EXPERIMENT_EXECUTION_MUTABLE_FIELDS
                | EXPERIMENT_EXECUTION_IDENTITY_FIELDS
            )
        ):
            current = self.workspace.get(artifact_id)
            merged = copy.deepcopy(current.data)
            for identity in EXPERIMENT_EXECUTION_IDENTITY_FIELDS:
                if (
                    identity in candidate_copy
                    and candidate_copy[identity] != current.data.get(identity)
                ):
                    raise RuntimeValidationError(
                        f"experiment execution patch cannot change {identity}"
                    )
                candidate_copy.pop(identity, None)
            patch_provenance = candidate_copy.pop("provenance", None)
            merged.update(candidate_copy)
            if isinstance(patch_provenance, Mapping):
                provenance = copy.deepcopy(merged.get("provenance", {}))
                provenance.update(copy.deepcopy(dict(patch_provenance)))
                merged["provenance"] = provenance
            candidate_copy = merged
        # The protocol digest is derived metadata, not a scientific degree of
        # freedom. Seal it at the trust boundary after the design session has
        # fixed every protocol field, so agents cannot accidentally lock a
        # serialization variant or spend retries guessing the host's hash.
        if (
            kind == "Experiment"
            and context.get("role") == "experiment-design"
            and candidate_copy.get("status") == "READY"
        ):
            protocol_lock = candidate_copy.get("protocol_lock")
            if isinstance(protocol_lock, dict):
                protocol_lock["sha256"] = ExperimentExecutionAdapter.protocol_hash(
                    candidate_copy
                )
        material = {
            "proposal_version": PROPOSAL_VERSION,
            "proposal_id": proposal_id,
            "project_id": project_id,
            "action_id": action_id,
            "operation": operation,
            "artifact_id": artifact_id,
            "kind": kind,
            "base_revision": base_revision,
            "bundle_sha256": bundle_sha256,
            "proposer_session_id": proposer_session_id,
            "candidate": candidate_copy,
        }
        proposal = Proposal(**material, proposal_sha256=sha256_json(material))
        value = proposal.to_dict()
        self._require_schema(self._proposal_validator, value, "proposal")

        path = self.action_dir(project_id, action_id) / "proposals" / f"{proposal_id}.json"
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing != value:
                raise SubmissionConflict(f"proposal ID already has different content: {proposal_id}")
            return proposal
        atomic_write_json(path, value)
        return proposal

    def list_proposals(self, project_id: str, action_id: str) -> list[Proposal]:
        path = self.action_dir(project_id, action_id) / "proposals"
        proposals: list[Proposal] = []
        for candidate_path in sorted(path.glob("*.json")) if path.is_dir() else []:
            value = json.loads(candidate_path.read_text(encoding="utf-8"))
            self._require_schema(self._proposal_validator, value, "proposal")
            expected_hash = value.pop("proposal_sha256")
            actual_hash = sha256_json(value)
            value["proposal_sha256"] = expected_hash
            if expected_hash != actual_hash:
                raise RuntimeValidationError(
                    f"proposal hash mismatch: {candidate_path.name}"
                )
            proposals.append(Proposal(**value))
        return proposals

    def validate_action(
        self, project_id: str, action_id: str, proposal_ids: Iterable[str] | None = None
    ) -> dict[str, Any]:
        requested = set(proposal_ids or [])
        proposals = self.list_proposals(project_id, action_id)
        if requested:
            proposals = [p for p in proposals if p.proposal_id in requested]
            missing = requested - {p.proposal_id for p in proposals}
            if missing:
                raise RuntimeValidationError(
                    f"unknown proposal IDs: {', '.join(sorted(missing))}"
                )
        if not proposals:
            raise RuntimeValidationError("action has no proposals to validate")

        artifact_ids = [proposal.artifact_id for proposal in proposals]
        if len(set(artifact_ids)) != len(artifact_ids):
            raise RuntimeValidationError("one action may propose each artifact at most once")

        context = self._load_context(project_id, action_id)
        overrides: dict[str, dict[str, Any]] = {}
        additions: list[ArtifactRecord] = []
        for proposal in proposals:
            self._validate_proposal_semantics(proposal, context)
            if proposal.operation == "CREATE":
                path = self.workspace.artifact_path(
                    kind=proposal.kind,
                    artifact_id=proposal.artifact_id,
                    project_id=(
                        None if proposal.kind == "Project" else proposal.project_id
                    ),
                )
                additions.append(ArtifactRecord(path, proposal.candidate))
            else:
                overrides[proposal.artifact_id] = proposal.candidate

        if context.get("role") == "lean-verification":
            schemes = {
                proposal.candidate.get("assessment", {}).get("scheme")
                for proposal in proposals
                if proposal.kind == "Review"
            }
            missing = {"LEAN", "AXIOM_AUDIT"} - schemes
            if missing:
                raise RuntimeValidationError(
                    "lean-verification requires both LEAN and AXIOM_AUDIT Reviews: "
                    + ", ".join(sorted(missing))
                )

        issues = self.workspace.validate(overrides=overrides, additions=additions)
        if issues:
            raise ArtifactValidationError(issues)
        return {
            "valid": True,
            "project_id": project_id,
            "action_id": action_id,
            "proposal_ids": [proposal.proposal_id for proposal in proposals],
            "proposal_hashes": [proposal.proposal_sha256 for proposal in proposals],
            "artifacts": artifact_ids,
            "artifact_refs": [
                {
                    "id": proposal.artifact_id,
                    "kind": proposal.kind,
                    "revision": proposal.candidate["revision"],
                }
                for proposal in proposals
            ],
            "assessments": [
                {
                    "artifact_id": proposal.artifact_id,
                    "scheme": proposal.candidate.get("assessment", {}).get("scheme"),
                    "outcome": proposal.candidate.get("assessment", {}).get("outcome"),
                    "target_ref": proposal.candidate.get("target", {}).get(
                        "artifact_ref"
                    ),
                }
                for proposal in proposals
                if proposal.kind == "Review"
            ],
        }

    def submit_action(
        self,
        *,
        project_id: str,
        action_id: str,
        bundle_sha256: str,
        proposal_ids: Iterable[str],
        outcome: str,
        gate_assessment: Mapping[str, Any] | None = None,
        failure_classification: str | None = None,
    ) -> dict[str, Any]:
        if outcome not in OUTCOMES:
            raise RuntimeValidationError(f"unknown submission outcome: {outcome}")
        if (
            failure_classification is not None
            and failure_classification not in FAILURE_CLASSIFICATIONS
        ):
            raise RuntimeValidationError(
                f"unknown failure classification: {failure_classification}"
            )
        context = self._require_matching_context(
            project_id, action_id, bundle_sha256
        )
        proposal_id_list = list(proposal_ids)

        # If execution staged exactly one append-only Experiment proposal but
        # accidentally omitted its ID from a FAILED submission, there is no
        # scientific choice to make: select that unique staged proposal.  Two
        # or more candidates remain ambiguous and fail closed.
        staged_for_action = self.list_proposals(project_id, action_id)
        if (
            outcome == "FAILED"
            and not proposal_id_list
            and context.get("role") == "experiment-execution"
            and len(staged_for_action) == 1
            and staged_for_action[0].kind == "Experiment"
            and staged_for_action[0].candidate.get("status") in {"RUNNING", "FAILED"}
            and bool(staged_for_action[0].candidate.get("runs"))
        ):
            proposal_id_list = [staged_for_action[0].proposal_id]

        # Preserve a uniquely committed execution record even when the agent
        # mistakes an inconclusive scientific summary for an action failure and
        # omits its append-only proposal.  Recovery is intentionally mechanical
        # and fail-closed: one target Experiment, no staged proposals, and one
        # committed adapter run are all required.
        if (
            outcome == "FAILED"
            and not proposal_id_list
            and context.get("role") == "experiment-execution"
            and not staged_for_action
        ):
            target_ids = list(
                context.get("allowed_outputs", {}).get("artifact_ids", [])
            )
            evidence_root = self.runtime_dir / "actions" / project_id / action_id
            committed_runs: list[dict[str, Any]] = []
            for run_state in sorted(evidence_root.glob("run-*/run-state.json")):
                try:
                    persisted = json.loads(run_state.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if (
                    persisted.get("status") == "COMMITTED"
                    and isinstance(persisted.get("run"), dict)
                ):
                    committed_runs.append(copy.deepcopy(persisted["run"]))
            if len(target_ids) == 1 and len(committed_runs) == 1:
                current = self.workspace.get(str(target_ids[0]))
                run = committed_runs[0]
                existing_run_ids = {
                    item.get("run_id") for item in current.data.get("runs", [])
                }
                if (
                    current.kind == "Experiment"
                    and current.data.get("status") in {"READY", "RUNNING"}
                    and run.get("run_id") not in existing_run_ids
                ):
                    recovered = self.stage_proposal(
                        proposal_id=f"host-recovered-{action_id}",
                        project_id=project_id,
                        action_id=action_id,
                        operation="REVISE",
                        artifact_id=current.id,
                        kind="Experiment",
                        base_revision=int(current.data["revision"]),
                        bundle_sha256=bundle_sha256,
                        proposer_session_id=f"host-recovery-{action_id}",
                        candidate={
                            "revision": int(current.data["revision"]) + 1,
                            "updated_at": run.get("finished_at")
                            or current.data.get("updated_at"),
                            "provenance": {"updated_by": {
                                "actor_type": "agent",
                                "actor_id": "research-controller-execution-evidence-recovery",
                                "session_id": action_id,
                            }},
                            "status": "RUNNING",
                            "runs": [*current.data.get("runs", []), run],
                        },
                    )
                    proposal_id_list = [recovered.proposal_id]

        # A terminated experiment is scientific evidence even when its frozen
        # entrypoint returns non-zero (for example, a preregistered analyzer
        # deliberately exits 1 for FAIL/INCOMPLETE).  Execution agents have
        # occasionally confused that scientific terminal state with failure of
        # the surrounding action and requested an infrastructure retry.  When
        # they nevertheless staged a valid, append-only Experiment revision,
        # accept the evidence and leave interpretation to the independent
        # experiment-verification session.  This is deliberately narrow: no
        # proposal, a non-Experiment proposal, or an invalid candidate retains
        # the original FAILED action semantics.
        if (
            outcome == "FAILED"
            and proposal_id_list
            and context.get("role") == "experiment-execution"
        ):
            staged = {
                proposal.proposal_id: proposal
                for proposal in self.list_proposals(project_id, action_id)
            }
            selected = [staged.get(item) for item in proposal_id_list]
            if (
                all(proposal is not None for proposal in selected)
                and all(proposal.kind == "Experiment" for proposal in selected)
                and all(
                    proposal.candidate.get("status") in {"RUNNING", "FAILED"}
                    and bool(proposal.candidate.get("runs"))
                    for proposal in selected
                )
            ):
                self.validate_action(project_id, action_id, proposal_id_list)
                outcome = "SUBMITTED"
                failure_classification = None
        value: dict[str, Any] = {
            "submission_version": SUBMISSION_VERSION,
            "project_id": project_id,
            "action_id": action_id,
            "bundle_sha256": bundle_sha256,
            "proposal_ids": proposal_id_list,
            "outcome": outcome,
        }
        if gate_assessment is not None:
            value["gate_assessment"] = copy.deepcopy(dict(gate_assessment))
        if failure_classification is not None:
            value["failure_classification"] = failure_classification
        self._require_schema(self._submission_validator, value, "submission")
        if outcome == "SUBMITTED":
            if value["proposal_ids"]:
                self.validate_action(project_id, action_id, value["proposal_ids"])
            else:
                self.validate_resource_action(project_id, action_id)
        path = self.action_dir(project_id, action_id) / "submission.json"
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing != value:
                raise SubmissionConflict("action already has a different submission")
            return existing
        atomic_write_json(path, value)
        return value

    def validate_resource_action(
        self, project_id: str, action_id: str
    ) -> dict[str, Any]:
        """Validate a writer action that produces paper resources, not artifacts."""

        context = self._load_context(project_id, action_id)
        if context.get("role") != "scientific-writing":
            raise RuntimeValidationError(
                "only scientific-writing may submit without artifact proposals"
            )
        path = self.root / "projects" / project_id / "paper" / "traceability.yaml"
        if not path.is_file():
            raise RuntimeValidationError("writer action requires paper/traceability.yaml")
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise RuntimeValidationError(f"invalid manuscript traceability: {exc}") from exc
        self._require_schema(self._traceability_validator, value, "traceability")
        if value.get("project_id") != project_id:
            raise RuntimeValidationError("traceability belongs to the wrong project")
        for entry in value.get("entries", []):
            self.resolve(entry.get("source_refs", []))
        return {"valid": True, "traceability": str(path.relative_to(self.root))}

    def load_submission(self, project_id: str, action_id: str) -> dict[str, Any] | None:
        path = self.action_dir(project_id, action_id) / "submission.json"
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        self._require_schema(self._submission_validator, value, "submission")
        return value

    def _validate_proposal_semantics(
        self, proposal: Proposal, context: Mapping[str, Any]
    ) -> None:
        candidate = proposal.candidate
        if candidate.get("id") != proposal.artifact_id:
            raise RuntimeValidationError("candidate id does not match proposal artifact_id")
        if candidate.get("kind") != proposal.kind:
            raise RuntimeValidationError("candidate kind does not match proposal kind")
        member_project = (
            candidate.get("id")
            if proposal.kind == "Project"
            else candidate.get("project_id")
        )
        if member_project != proposal.project_id:
            raise RuntimeValidationError("candidate belongs to the wrong project")

        index = self.workspace.index()
        current = index.get(proposal.artifact_id)
        if proposal.operation == "CREATE":
            if current is not None:
                raise RevisionConflict(f"artifact already exists: {proposal.artifact_id}")
            if proposal.base_revision is not None or candidate.get("revision") != 1:
                raise RuntimeValidationError("CREATE requires base_revision null and revision 1")
        else:
            if current is None:
                raise RevisionConflict(f"artifact does not exist: {proposal.artifact_id}")
            current_revision = current.data.get("revision")
            if proposal.base_revision != current_revision:
                raise RevisionConflict(
                    f"{proposal.artifact_id} revision is {current_revision}, expected {proposal.base_revision}"
                )
            if candidate.get("revision") != current_revision + 1:
                raise RuntimeValidationError("REVISE candidate revision must be base + 1")
            for immutable in ("id", "kind", "project_id", "created_at"):
                if candidate.get(immutable) != current.data.get(immutable):
                    raise RuntimeValidationError(f"REVISE cannot change {immutable}")
            old_status = current.data.get("status")
            new_status = candidate.get("status")
            if old_status != new_status and not transition_allowed(
                proposal.kind, str(old_status), str(new_status)
            ):
                raise RuntimeValidationError(
                    f"invalid {proposal.kind} status transition: {old_status} -> {new_status}"
                )
            if proposal.kind == "Project":
                old_stage = current.data.get("stage")
                new_stage = candidate.get("stage")
                if old_stage != new_stage and not stage_transition_allowed(
                    str(old_stage), str(new_stage)
                ):
                    raise RuntimeValidationError(
                        f"invalid Project stage transition: {old_stage} -> {new_stage}"
                    )
            if (
                proposal.kind == "Experiment"
                and current.data.get("status")
                in {"READY", "RUNNING", "COMPLETED", "FAILED", "INVALIDATED"}
            ):
                changed_top_level = {
                    path.split(".", 1)[0]
                    for path in self._changed_paths(current.data, candidate)
                }
                locked_changes = sorted(
                    changed_top_level & EXPERIMENT_PROTOCOL_FIELDS
                )
                if locked_changes:
                    raise RuntimeValidationError(
                        "READY Experiment protocol is immutable; create a new Experiment: "
                        + ", ".join(locked_changes)
                    )

        allowed = context.get("allowed_outputs", {})
        allowed_kinds = set(allowed.get("artifact_kinds", []))
        allowed_ids = set(allowed.get("artifact_ids", []))
        create_ids = set(allowed.get("create_ids", []))
        if proposal.kind not in allowed_kinds:
            raise RuntimeValidationError(f"action may not produce {proposal.kind}")
        if proposal.operation == "CREATE" and proposal.artifact_id not in create_ids:
            raise RuntimeValidationError(f"action may not create {proposal.artifact_id}")
        if proposal.operation == "REVISE" and proposal.artifact_id not in allowed_ids:
            raise RuntimeValidationError(f"action may not revise {proposal.artifact_id}")
        rule = next(
            (
                item
                for item in allowed.get("rules", [])
                if item.get("kind") == proposal.kind
                and proposal.operation in item.get("operations", [])
            ),
            None,
        )
        if rule is None:
            raise RuntimeValidationError(
                f"{proposal.operation} {proposal.kind} is outside the Skill permission policy"
            )
        if proposal.operation == "REVISE" and current is not None:
            permitted = set(rule.get("fields", []))
            changed = self._changed_paths(current.data, candidate)
            denied = sorted(
                path
                for path in changed
                if "*" not in permitted
                and not any(path == field or path.startswith(field + ".") for field in permitted)
            )
            if denied:
                raise RuntimeValidationError(
                    "Skill may not revise fields: " + ", ".join(denied)
                )
        role = str(context.get("role", ""))
        if proposal.kind == "ResearchProposal" and role == "proposal-revision":
            if current is None:
                raise RuntimeValidationError("proposal-revision requires an existing ResearchProposal")
            self._validate_proposal_revision_authority(
                current.data, candidate, context
            )
        if proposal.kind == "ScientificClaim" and candidate.get("status") in {
            "VERIFIED", "IN_PAPER"
        }:
            if current is None or candidate.get("status") != current.data.get("status"):
                raise RuntimeValidationError(
                    "agent proposals cannot promote ScientificClaim verification status"
                )
        if role == "theory-development" and candidate.get("status") in {
            "VERIFIED", "IN_PAPER"
        }:
            raise RuntimeValidationError("theory-development stops at SUPPORTED")
        if role == "experiment-design" and candidate.get("status") not in {
            "DRAFT", "PLANNED", "READY"
        }:
            raise RuntimeValidationError(
                "experiment-design may only produce DRAFT, PLANNED, or READY"
            )
        if (
            role == "experiment-design"
            and proposal.kind == "Experiment"
            and self._context_exposes_experiment_outcomes(context)
        ):
            serialized = json.dumps(candidate, sort_keys=True).lower()
            misleading = (
                "outcome-blind",
                "no-outcomes-inspected",
                "no outcomes inspected",
                "no outcome was inspected",
                "no outcome data was inspected",
            )
            used = [marker for marker in misleading if marker in serialized]
            if used:
                raise RuntimeValidationError(
                    "context already exposes prior experiment outcomes; "
                    "the successor protocol must be labelled post-hoc or retrospective "
                    "and cannot claim outcome blindness: " + ", ".join(used)
                )
        if role == "experiment-execution" and candidate.get("status") not in {
            "RUNNING", "FAILED"
        }:
            raise RuntimeValidationError(
                "experiment-execution may only record RUNNING or FAILED; "
                "experiment-verification owns COMPLETED and result"
            )
        if proposal.kind == "Review":
            expected_schemes = {
                "literature-novelty": {"NOVELTY"},
                "theory-verification": {"THEORY"},
                "lean-verification": {"LEAN", "AXIOM_AUDIT"},
                "semantic-alignment-review": {"SEMANTIC_ALIGNMENT"},
                "experiment-verification": {"EXPERIMENT"},
                "completion-review": {"COMPLETION"},
                "consistency-review": {"CONSISTENCY"},
                "final-review": {"FINAL"},
                "proposal-correctness-review": {"PROPOSAL_CORRECTNESS"},
            }.get(role)
            if expected_schemes is not None:
                scheme = candidate.get("assessment", {}).get("scheme")
                if scheme not in expected_schemes:
                    raise RuntimeValidationError(
                        f"{role} Review requires assessment scheme in "
                        + ", ".join(sorted(expected_schemes))
                    )
            if role == "final-review":
                outcome = candidate.get("assessment", {}).get("outcome")
                unresolved = [
                    issue for issue in candidate.get("issues", [])
                    if issue.get("status") in {"OPEN", "ACCEPTED"}
                ]
                if outcome == "PASS":
                    if candidate.get("status") != "RESOLVED":
                        raise RuntimeValidationError(
                            "FINAL PASS Review must have top-level status RESOLVED"
                        )
                    if candidate.get("recommendation") != "ACCEPT":
                        raise RuntimeValidationError(
                            "FINAL PASS Review must recommend ACCEPT"
                        )
                    if unresolved:
                        raise RuntimeValidationError(
                            "FINAL PASS Review cannot retain OPEN or ACCEPTED issues"
                        )

    @staticmethod
    def _context_exposes_experiment_outcomes(context: Mapping[str, Any]) -> bool:
        """Return whether the pinned bundle already reveals scientific outcomes.

        Experiment design can legitimately continue after an exploratory or audit
        result, but it must not describe that successor as outcome-blind.  This
        check deliberately uses only the immutable ContextBundle, never session
        history.
        """
        for record in context.get("artifacts", []):
            if not isinstance(record, Mapping):
                continue
            content = record.get("content")
            if not isinstance(content, Mapping):
                continue
            kind = record.get("kind") or content.get("kind")
            if kind == "Experiment":
                if content.get("runs") or content.get("result"):
                    return True
                if content.get("status") in {"COMPLETED", "FAILED", "INVALIDATED"}:
                    return True
            if kind == "Review":
                assessment = content.get("assessment")
                if not isinstance(assessment, Mapping):
                    continue
                if assessment.get("scheme") in {"EXPERIMENT", "COMPLETION"}:
                    return True
        return False

    @staticmethod
    def _validate_proposal_revision_authority(
        current: Mapping[str, Any], candidate: Mapping[str, Any],
        context: Mapping[str, Any],
    ) -> None:
        """Require immutable inputs and accepted Decisions for material issues."""

        for field in ("source_document", "rubric_source_document", "rubric_document", "proposal_type", "language"):
            if candidate.get(field) != current.get(field):
                raise RuntimeValidationError(
                    f"proposal-revision cannot change immutable field {field}"
                )
        old_cycle = int(current.get("revision_cycle", 0))
        new_cycle = candidate.get("revision_cycle")
        if new_cycle != old_cycle + 1 or int(new_cycle) > 3:
            raise RuntimeValidationError(
                "proposal-revision must increment revision_cycle exactly once, up to 3"
            )
        old_document = current.get("current_document")
        new_document = candidate.get("current_document")
        if not isinstance(new_document, Mapping) or new_document == old_document:
            raise RuntimeValidationError(
                "proposal-revision must create a new immutable current_document"
            )
        expected_ordinal = old_cycle + 2
        document_match = re.search(
            r"resources/proposal/revisions/rev-([0-9]{3,})\.md$",
            str(new_document.get("uri", "")),
        )
        if document_match is None or int(document_match.group(1)) < expected_ordinal:
            raise RuntimeValidationError(
                "proposal revision document must use an immutable rev-NNN.md "
                f"with N >= {expected_ordinal:03d}"
            )
        old_logs = list(current.get("change_log_refs", []))
        new_logs = list(candidate.get("change_log_refs", []))
        if new_logs[:len(old_logs)] != old_logs or len(new_logs) != len(old_logs) + 1:
            raise RuntimeValidationError(
                "proposal-revision must append exactly one immutable change log"
            )
        log_match = re.search(
            r"resources/proposal/changes/rev-([0-9]{3,})\.yaml$",
            str(new_logs[-1].get("uri", "")),
        )
        if log_match is None or int(log_match.group(1)) != int(document_match.group(1)):
            raise RuntimeValidationError(
                "proposal document and change log must use the same immutable rev-NNN ordinal"
            )

        artifacts = [
            item.get("content", {}) for item in context.get("artifacts", [])
            if isinstance(item, Mapping)
        ]
        material_issue_ids = {
            str(issue["id"])
            for artifact in artifacts
            if artifact.get("kind") == "Review"
            and artifact.get("target", {}).get("artifact_ref", {}).get("id") == current.get("id")
            and artifact.get("target", {}).get("artifact_ref", {}).get("revision") == current.get("revision")
            for issue in artifact.get("issues", [])
            if issue.get("severity") in {"BLOCKER", "MAJOR"}
            and issue.get("status") in {"OPEN", "ACCEPTED"}
        }
        authorized_issue_ids = {
            str(artifact.get("proposal_issue", {}).get("issue_id"))
            for artifact in artifacts
            if artifact.get("kind") == "Decision"
            and artifact.get("status") == "ACCEPTED"
            and artifact.get("proposal_issue", {}).get("action") in {"ACCEPT", "EDIT"}
        }
        missing = sorted(material_issue_ids - authorized_issue_ids)
        if missing:
            raise RuntimeValidationError(
                "material proposal changes lack accepted Decisions: " + ", ".join(missing)
            )

    @classmethod
    def _changed_paths(
        cls, before: Any, after: Any, prefix: str = ""
    ) -> set[str]:
        if isinstance(before, dict) and isinstance(after, dict):
            result: set[str] = set()
            for key in set(before) | set(after):
                path = f"{prefix}.{key}" if prefix else str(key)
                if key not in before or key not in after:
                    result.add(path)
                else:
                    result.update(cls._changed_paths(before[key], after[key], path))
            return result
        if before != after:
            return {prefix}
        return set()

    def _load_context(self, project_id: str, action_id: str) -> dict[str, Any]:
        path = self.action_dir(project_id, action_id) / "context.json"
        if not path.is_file():
            raise RuntimeValidationError("action context has not been built")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise RuntimeValidationError("action context must be a JSON object")
        return value

    def _require_matching_context(
        self, project_id: str, action_id: str, bundle_sha256: str
    ) -> dict[str, Any]:
        context = self._load_context(project_id, action_id)
        if context.get("bundle_sha256") != bundle_sha256:
            raise RuntimeValidationError("proposal references a different context bundle")
        return context

    @staticmethod
    def _require_schema(
        validator: Draft202012Validator, value: Any, label: str
    ) -> None:
        issues = sorted(
            validator.iter_errors(value),
            key=lambda error: (list(error.absolute_path), error.message),
        )
        if issues:
            details = [
                {
                    "path": ".".join(str(part) for part in error.absolute_path) or "$",
                    "message": error.message,
                }
                for error in issues
            ]
            raise RuntimeValidationError(f"invalid {label}", details=details)

    @staticmethod
    def _require_safe(value: str, label: str) -> None:
        if not SAFE_ID.fullmatch(value):
            raise RuntimeValidationError(f"{label} contains unsafe characters")
