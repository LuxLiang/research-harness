"""Recoverable promotion of staged research artifacts and controller state."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

import yaml

from .artifact_service import ArtifactService, Proposal
from .orchestrator import OrchestratorEvent, ResearchOrchestrator, utc_now
from .proposal_review import import_proposal_resources
from .proposal_review import load_decisions
from .runtime_types import (
    GitConflict,
    RuntimeValidationError,
    atomic_write_json,
    atomic_write_text,
    sha256_json,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - MVP target is Linux/macOS
    fcntl = None  # type: ignore[assignment]


def _draft_plan_recency_key(record: Any) -> tuple[int, str, int, str]:
    """Order competing draft Plans by producing action, then stable metadata.

    A plan revision request intentionally leaves the rejected draft canonical.
    The replacement starts again at artifact revision 1, so artifact revision
    alone cannot identify the draft produced by the latest planning action.
    """

    match = re.search(r"-(\d+)-planning-draft$", str(record.id))
    action_seq = int(match.group(1)) if match else -1
    return (
        action_seq,
        str(record.data.get("updated_at", "")),
        int(record.data.get("revision", 0)),
        str(record.id),
    )


class ScientificTransaction:
    """The only service allowed to promote quarantine into canonical state."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.artifacts = ArtifactService(self.root)
        self.orchestrator = ResearchOrchestrator(self.root)
        self.runtime_dir = self.root / ".harness" / "research"

    def initialize_project(
        self, *, project_id: str, objective: str, run_id: str | None = None,
        commit: bool = True, workflow_mode: str = "FULL_RESEARCH",
        proposal_path: str | Path | None = None,
        rubric_path: str | Path | None = None,
        source_proposal_ref: Mapping[str, Any] | None = None,
        source_materials: Iterable[str | Path] | None = None,
    ) -> dict[str, Any]:
        """Create a Project and its initial checkpoint without hand-written YAML."""
        if not re.fullmatch(r"proj-[a-z0-9][a-z0-9-]*", project_id):
            raise RuntimeValidationError("project_id must be a lowercase proj- slug")
        path = self.root / "projects" / project_id / "project.yaml"
        if path.exists():
            raise RuntimeValidationError(f"project already exists: {project_id}")
        if workflow_mode not in {"FULL_RESEARCH", "PROPOSAL_REVIEW"}:
            raise RuntimeValidationError(f"unsupported workflow_mode: {workflow_mode}")
        if workflow_mode == "PROPOSAL_REVIEW" and proposal_path is None:
            raise RuntimeValidationError("PROPOSAL_REVIEW requires proposal_path")
        if workflow_mode == "FULL_RESEARCH" and (proposal_path is not None or rubric_path is not None):
            raise RuntimeValidationError("proposal and rubric inputs require PROPOSAL_REVIEW")
        material_paths = list(source_materials or [])
        if workflow_mode != "FULL_RESEARCH" and material_paths:
            raise RuntimeValidationError("source materials require FULL_RESEARCH")
        initial_head = self._git_head(required=commit)
        timestamp = utc_now()
        slug = project_id.removeprefix("proj-")
        actor = {"actor_type": "human", "actor_id": "researcher"}
        proposal = None
        created_paths: list[Path] = []
        proposal_id = f"proposal-{slug}"
        proposal_artifact_path = self.root / "projects" / project_id / "proposals" / f"{proposal_id}.yaml"
        input_resources: list[dict[str, str]] = []
        if workflow_mode == "PROPOSAL_REVIEW":
            imported = import_proposal_resources(
                self.root, project_id, proposal_path or "", rubric_path
            )
            created_paths.extend(imported.pop("created_paths"))
            proposal = {
                "schema_version": "research-artifact/v0.1.3", "kind": "ResearchProposal",
                "id": proposal_id, "project_id": project_id,
                "title": f"Proposal review for {slug}", "status": "IMPORTED", "revision": 1,
                "created_at": timestamp, "updated_at": timestamp,
                "provenance": {"created_by": actor, "updated_by": actor}, "tags": ["proposal-review"],
                "proposal_type": "ACADEMIC_RESEARCH", **imported,
                "source_map": None, "question_refs": [], "plan_ref": None,
                "review_refs": [], "change_log_refs": [], "revision_cycle": 0,
            }
            atomic_write_text(
                proposal_artifact_path,
                yaml.safe_dump(proposal, sort_keys=False, allow_unicode=True),
            )
            created_paths.append(proposal_artifact_path)

        if material_paths:
            input_resources, input_paths = self._import_source_materials(
                project_id, material_paths, timestamp
            )
            created_paths.extend(input_paths)

        artifact = {
            "schema_version": (
                "research-artifact/v0.1.4" if input_resources else
                "research-artifact/v0.1.3" if workflow_mode == "PROPOSAL_REVIEW" or source_proposal_ref else
                "research-artifact/v0.1.2"
            ), "kind": "Project",
            "id": project_id, "title": objective, "status": "ACTIVE", "revision": 1,
            "created_at": timestamp, "updated_at": timestamp,
            "provenance": {"created_by": actor, "updated_by": actor}, "tags": ["mvp"],
            "name": objective, "slug": slug, "objective": objective, "stage": "QUESTION",
            "owners": [actor], "research_questions": [], "active_plan": None,
            "deliverables": ([{"kind": "research_proposal", "path": "resources/proposal/revisions/rev-001.md", "status": "DRAFT"}]
                             if workflow_mode == "PROPOSAL_REVIEW" else
                             [{"kind": "manuscript", "path": "paper/manuscript/draft.md", "status": "PLANNED"}]),
            "metadata": {"domains": ["scientific-research"], "keywords": ["research-harness"],
                         "started_on": timestamp[:10], "target_venues": [],
                         "confidentiality": "PRIVATE", "license": "MIT"},
        }
        if artifact["schema_version"] in {
            "research-artifact/v0.1.3", "research-artifact/v0.1.4"
        }:
            artifact.update({
                "workflow_mode": workflow_mode,
                "active_proposal": ({"id": proposal_id, "kind": "ResearchProposal", "revision": 1} if proposal else None),
                "source_proposal_ref": copy.deepcopy(source_proposal_ref),
            })
        if artifact["schema_version"] == "research-artifact/v0.1.4":
            artifact["input_resources"] = input_resources
        atomic_write_text(path, yaml.safe_dump(artifact, sort_keys=False, allow_unicode=True))
        created_paths.append(path)
        try:
            self._require_transaction_valid(project_id)
            project_commit = self._git_head(required=commit)
            if commit:
                project_commit = self._commit(created_paths, project_id, "initialize-project", [])
            checkpoint = self.orchestrator.initialize(
                project_id, run_id or f"run-{slug}-001", str(project_commit), at=timestamp,
                workflow_mode=workflow_mode,
            )
            checkpoint_path = self.orchestrator.store.path_for(project_id)
            checkpoint_commit = project_commit
            if commit:
                checkpoint_commit = self._commit(
                    [checkpoint_path], project_id, "initialize-orchestrator", []
                )
            return {"project_id": project_id, "checkpoint": checkpoint, "commit_hash": checkpoint_commit}
        except Exception:
            current_head = self._git_head(required=False)
            if not commit or current_head == initial_head:
                for created in reversed(created_paths):
                    if created.exists() and created.is_file():
                        created.unlink()
            raise

    def _import_source_materials(
        self,
        project_id: str,
        source_materials: Iterable[str | Path],
        imported_at: str,
    ) -> tuple[list[dict[str, str]], list[Path]]:
        """Copy repeatable FULL_RESEARCH inputs into immutable project resources."""

        destination = self.root / "projects" / project_id / "resources" / "input"
        resources: list[dict[str, str]] = []
        created: list[Path] = []
        manifest_entries: list[dict[str, str]] = []
        seen_sources: set[Path] = set()
        for position, raw_path in enumerate(source_materials, start=1):
            source = Path(raw_path).expanduser().resolve()
            if source in seen_sources:
                raise RuntimeValidationError(f"duplicate source material: {source}")
            seen_sources.add(source)
            if not source.is_file():
                raise RuntimeValidationError(f"source material is not a file: {source}")
            if source.stat().st_size > 25 * 1024 * 1024:
                raise RuntimeValidationError(
                    f"source material exceeds 25 MiB limit: {source}"
                )
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", source.name).strip(".-")
            if not safe_name:
                safe_name = "source"
            target = destination / f"{position:03d}-{safe_name}"
            self._atomic_copy_bytes(source, target)
            created.append(target)
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            relative = target.relative_to(self.root).as_posix()
            media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
            resource = {
                "uri": relative,
                "sha256": digest,
                "media_type": media_type,
                "description": f"Pinned source material: {source.name}",
            }
            resources.append(resource)
            manifest_entries.append({
                "source_path": str(source),
                "copied_uri": relative,
                "sha256": digest,
                "media_type": media_type,
            })

        manifest_path = destination / "source-manifest.json"
        manifest = {
            "manifest_version": "research-input-manifest/v0.1",
            "project_id": project_id,
            "imported_at": imported_at,
            "resources": manifest_entries,
        }
        atomic_write_json(manifest_path, manifest)
        created.append(manifest_path)
        manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        resources.append({
            "uri": manifest_path.relative_to(self.root).as_posix(),
            "sha256": manifest_digest,
            "media_type": "application/json",
            "description": "Pinned source-material import manifest",
        })
        return resources, created

    @staticmethod
    def _atomic_copy_bytes(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", dir=target.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(source.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, target)
        except Exception:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
            raise

    def begin_action(
        self,
        *,
        project_id: str,
        action_id: str,
        input_git_commit: str,
        expected_seq: int,
        at: str | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Persist the pre-dispatch checkpoint as an auditable control commit."""

        with self._project_lock(project_id):
            checkpoint = self.orchestrator.store.load(project_id)
            pending = checkpoint.get("pending_action")
            if isinstance(pending, dict) and pending.get("action_id") == action_id:
                return {"checkpoint": checkpoint, "commit_hash": self._git_head(required=commit)}
            current_head = self._git_head(required=commit)
            if current_head is not None and current_head != input_git_commit.lower():
                raise GitConflict(
                    f"Git HEAD is {current_head}, expected {input_git_commit.lower()}"
                )
            checkpoint_path = self.orchestrator.store.path_for(project_id)
            self._require_clean_targets([checkpoint_path])
            backup = self._backup_values([checkpoint_path])
            journal_action = f"begin-{action_id}"
            journal_path = self.runtime_dir / "journals" / f"{sha256_json({'project': project_id, 'action': journal_action, 'head': current_head})}.json"
            journal = {
                "transaction_version": "research-transaction/v0.1",
                "transaction_id": journal_path.stem,
                "project_id": project_id,
                "action_id": journal_action,
                "status": "PREPARED",
                "paths": [str(checkpoint_path.relative_to(self.root))],
                "backups": backup,
                "commit_hash": None,
                "prepared_at": utc_now(),
            }
            atomic_write_json(journal_path, journal)
            commit_completed = False
            try:
                result = self.orchestrator.begin_action(
                    project_id,
                    action_id,
                    input_git_commit,
                    expected_seq=expected_seq,
                    at=at,
                )
                commit_hash = current_head
                if commit:
                    commit_hash = self._commit(
                        [checkpoint_path], project_id, journal_action, []
                    )
                    commit_completed = True
                journal["status"] = "COMMITTED"
                journal["commit_hash"] = commit_hash
                journal["committed_at"] = utc_now()
                atomic_write_json(journal_path, journal)
            except Exception:
                if not commit_completed:
                    self._restore_backups(backup)
                    self._unstage([checkpoint_path])
                    journal["status"] = "ROLLED_BACK"
                    journal["rolled_back_at"] = utc_now()
                    atomic_write_json(journal_path, journal)
                raise
            return {"checkpoint": result, "commit_hash": commit_hash}

    def apply_control_event(
        self,
        *,
        project_id: str,
        event: Mapping[str, Any],
        expected_git_commit: str,
        expected_seq: int,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Apply a controller-only event and commit its checkpoint explicitly."""

        with self._project_lock(project_id):
            current_head = self._git_head(required=commit)
            if current_head is not None and current_head != expected_git_commit.lower():
                raise GitConflict(
                    f"Git HEAD is {current_head}, expected {expected_git_commit.lower()}"
                )
            checkpoint_path = self.orchestrator.store.path_for(project_id)
            project_path = self.root / self.artifacts.workspace.get(project_id).path
            control_paths = [checkpoint_path, project_path]
            if event.get("type") == "PROPOSAL_FINAL_APPROVED":
                active_proposal = self.artifacts.workspace.get(project_id).data.get("active_proposal")
                if isinstance(active_proposal, Mapping):
                    control_paths.append(
                        self.root / self.artifacts.workspace.get(str(active_proposal["id"])).path
                    )
            if (
                event.get("type") == "TRACK_ADVANCED"
                and event.get("payload", {}).get("status") == "COMPLETED"
            ):
                active_plan = self.orchestrator.store.load(project_id).get("active_plan")
                if isinstance(active_plan, Mapping):
                    control_paths.append(
                        self.root / self.artifacts.workspace.get(str(active_plan["id"])).path
                    )
            decision_path = self._control_decision_path(
                project_id, event, expected_seq
            )
            if decision_path is not None:
                control_paths.append(decision_path)
            self._require_clean_targets(control_paths)
            backup = self._backup_values(control_paths)
            journal_action = f"control-{event['type']}-{expected_seq}"
            journal_path = self.runtime_dir / "journals" / f"{sha256_json({'project': project_id, 'action': journal_action, 'head': current_head})}.json"
            journal = {
                "transaction_version": "research-transaction/v0.1",
                "transaction_id": journal_path.stem,
                "project_id": project_id,
                "action_id": journal_action,
                "status": "PREPARED",
                "paths": [str(path.relative_to(self.root)) for path in control_paths],
                "backups": backup,
                "commit_hash": None,
                "prepared_at": utc_now(),
            }
            atomic_write_json(journal_path, journal)
            commit_completed = False
            try:
                reduction = self.orchestrator.apply(
                    project_id,
                    OrchestratorEvent(
                        str(event["type"]),
                        str(event.get("at") or utc_now()),
                        dict(event.get("payload", {})),
                    ),
                    expected_seq=expected_seq,
                )
                changed_refs = self._apply_project_commands(project_id, reduction.commands)
                self._sync_checkpoint_refs(reduction.checkpoint, changed_refs)
                self.orchestrator.store.save(
                    reduction.checkpoint,
                    expected_seq=int(reduction.checkpoint["checkpoint_seq"]),
                )
                self._apply_decision_commands(
                    project_id, reduction.commands, event, expected_seq
                )
                self._require_transaction_valid(project_id)
                commit_hash = current_head
                if commit:
                    commit_hash = self._commit(
                        control_paths,
                        project_id,
                        journal_action,
                        [],
                    )
                    commit_completed = True
                journal["status"] = "COMMITTED"
                journal["commit_hash"] = commit_hash
                journal["committed_at"] = utc_now()
                atomic_write_json(journal_path, journal)
            except Exception:
                if not commit_completed:
                    self._restore_backups(backup)
                    self._unstage(control_paths)
                    journal["status"] = "ROLLED_BACK"
                    journal["rolled_back_at"] = utc_now()
                    atomic_write_json(journal_path, journal)
                raise
            return {
                "checkpoint": reduction.checkpoint,
                "commands": [
                    {"type": command.type, "payload": dict(command.payload)}
                    for command in reduction.commands
                ],
                "commit_hash": commit_hash,
            }

    def promote_action(
        self,
        *,
        project_id: str,
        action_id: str,
        expected_git_commit: str,
        expected_checkpoint_seq: int | None = None,
        event: Mapping[str, Any] | None = None,
        commit: bool = True,
        push: bool = False,
        push_attempts: int = 3,
    ) -> dict[str, Any]:
        """Validate, promote, optionally reduce an event, commit, and push."""

        result_path = self.artifacts.action_dir(project_id, action_id) / "transaction-result.json"
        if result_path.is_file():
            return json.loads(result_path.read_text(encoding="utf-8"))

        submission = self.artifacts.load_submission(project_id, action_id)
        if submission is None or submission.get("outcome") != "SUBMITTED":
            raise RuntimeValidationError("action has no SUBMITTED handler result")
        proposal_ids = submission.get("proposal_ids", [])
        if proposal_ids:
            validation = self.artifacts.validate_action(
                project_id, action_id, proposal_ids
            )
        else:
            self.artifacts.validate_resource_action(project_id, action_id)
            validation = {
                "proposal_hashes": [],
                "artifacts": [],
            }
        proposals = [
            proposal
            for proposal in self.artifacts.list_proposals(project_id, action_id)
            if proposal.proposal_id in set(proposal_ids)
        ]
        transaction_id = sha256_json(
            {
                "project_id": project_id,
                "action_id": action_id,
                "proposal_hashes": validation["proposal_hashes"],
                "event": event,
            }
        )
        journal_path = self.runtime_dir / "journals" / f"{transaction_id}.json"

        with self._project_lock(project_id):
            if result_path.is_file():
                return json.loads(result_path.read_text(encoding="utf-8"))
            current_head = self._git_head(required=commit or push)
            if current_head is not None and current_head != expected_git_commit.lower():
                raise GitConflict(
                    f"Git HEAD is {current_head}, expected {expected_git_commit.lower()}"
                )
            if expected_checkpoint_seq is not None:
                checkpoint = self.orchestrator.store.load(project_id)
                if checkpoint["checkpoint_seq"] != expected_checkpoint_seq:
                    raise GitConflict(
                        f"checkpoint is {checkpoint['checkpoint_seq']}, expected {expected_checkpoint_seq}"
                    )

            paths = [self._proposal_path(proposal) for proposal in proposals]
            resource_paths = self._proposal_resource_paths(project_id, proposals)
            self._require_immutable_resource_changes(resource_paths)
            paths.extend(resource_paths)
            paper_paths: list[Path] = []
            if not proposals:
                paper_paths = self._changed_paper_paths(project_id)
                if not paper_paths:
                    raise RuntimeValidationError(
                        "scientific-writing submitted no changed paper resources"
                    )
                paths.extend(paper_paths)
                trace_path = (
                    self.root
                    / "projects"
                    / project_id
                    / "paper"
                    / "traceability.yaml"
                )
                trace = yaml.safe_load(trace_path.read_text(encoding="utf-8"))
                claim_ids = {
                    ref["id"]
                    for entry in trace.get("entries", [])
                    for ref in entry.get("source_refs", [])
                    if ref.get("kind") == "ScientificClaim"
                }
                paths.extend(
                    self.root / self.artifacts.workspace.get(claim_id).path
                    for claim_id in sorted(claim_ids)
                )
            checkpoint_path = self.orchestrator.store.path_for(project_id)
            if event is not None:
                paths.append(checkpoint_path)
                paths.append(self.root / self.artifacts.workspace.get(project_id).path)
                if event.get("type") in {
                    "SKILL_COMPLETED", "SKILL_REVISION_REQUIRED"
                }:
                    output_id = event.get("payload", {}).get("output_id")
                    checkpoint = self.orchestrator.store.load(project_id)
                    progress = checkpoint.get("skill_progress", {}).get(
                        output_id, {}
                    )
                    ref = progress.get("artifact_ref")
                    if isinstance(ref, Mapping):
                        paths.append(
                            self.root
                            / self.artifacts.workspace.get(str(ref["id"])).path
                        )
                    active_plan = checkpoint.get("active_plan")
                    if isinstance(active_plan, Mapping):
                        paths.append(
                            self.root
                            / self.artifacts.workspace.get(
                                str(active_plan["id"])
                            ).path
                        )
                if (
                    event.get("type") == "GATE_RECORDED"
                    and event.get("payload", {}).get("gate", {}).get("gate_type")
                    == "CONSISTENCY"
                    and event.get("payload", {}).get("gate", {}).get("verdict")
                    == "PASS"
                ):
                    paths.extend(
                        self.root / record.path
                        for record in self._empirical_claim_records(project_id)
                    )
                if expected_checkpoint_seq is not None:
                    decision_path = self._control_decision_path(
                        project_id, event, expected_checkpoint_seq
                    )
                    if decision_path is not None:
                        paths.append(decision_path)
            paths = self._dedupe_paths(paths)
            mutable_action_paths = set(paper_paths) | set(resource_paths)
            self._require_clean_targets(
                path for path in paths if path not in mutable_action_paths
            )
            journal = {
                "transaction_version": "research-transaction/v0.1",
                "transaction_id": transaction_id,
                "project_id": project_id,
                "action_id": action_id,
                "status": "PREPARED",
                "expected_git_commit": expected_git_commit.lower(),
                "proposal_hashes": validation["proposal_hashes"],
                "paths": [str(path.relative_to(self.root)) for path in paths],
                "backups": self._backup_values(paths),
                "commit_hash": None,
                "prepared_at": utc_now(),
            }
            atomic_write_json(journal_path, journal)

            reduction = None
            commit_completed = False
            try:
                for proposal in proposals:
                    path = self._proposal_path(proposal)
                    atomic_write_text(
                        path,
                        yaml.safe_dump(
                            proposal.candidate, sort_keys=False, allow_unicode=True
                        ),
                    )
                self._require_transaction_valid(project_id)
                if event is not None:
                    if expected_checkpoint_seq is None:
                        raise RuntimeValidationError(
                            "event promotion requires expected_checkpoint_seq"
                        )
                    reduction = self.orchestrator.apply(
                        project_id,
                        OrchestratorEvent(
                            str(event["type"]),
                            str(event.get("at") or utc_now()),
                            dict(event.get("payload", {})),
                        ),
                        expected_seq=expected_checkpoint_seq,
                        artifact_overrides={
                            proposal.artifact_id: proposal.candidate
                            for proposal in proposals
                        },
                    )
                    changed_refs = self._apply_project_commands(project_id, reduction.commands)
                    self._sync_checkpoint_refs(reduction.checkpoint, changed_refs)
                    self.orchestrator.store.save(
                        reduction.checkpoint,
                        expected_seq=int(reduction.checkpoint["checkpoint_seq"]),
                    )
                    self._apply_decision_commands(
                        project_id,
                        reduction.commands,
                        event,
                        expected_checkpoint_seq,
                    )
                    self._require_transaction_valid(project_id)
                commit_hash = current_head
                if commit:
                    commit_hash = self._commit(paths, project_id, action_id, proposals)
                    commit_completed = True
                journal["status"] = "COMMITTED"
                journal["commit_hash"] = commit_hash
                journal["committed_at"] = utc_now()
                atomic_write_json(journal_path, journal)
            except Exception:
                # A successful Git commit is the scientific commit point. If
                # journal finalization crashes afterwards, leave PREPARED for
                # recover() rather than making the worktree disagree with HEAD.
                if not commit_completed:
                    self._restore_backups(journal["backups"])
                    self._unstage(paths)
                    journal["status"] = "ROLLED_BACK"
                    journal["rolled_back_at"] = utc_now()
                    atomic_write_json(journal_path, journal)
                raise

            pushed = False
            push_error: str | None = None
            if push and commit_hash is not None:
                pushed, push_error = self._push(push_attempts)
            result = {
                "transaction_id": transaction_id,
                "project_id": project_id,
                "action_id": action_id,
                "artifact_ids": [proposal.artifact_id for proposal in proposals],
                "proposal_ids": proposal_ids,
                "commit_hash": commit_hash,
                "pushed": pushed,
                "push_pending": bool(push and not pushed),
                "push_error": push_error,
                "checkpoint": reduction.checkpoint if reduction else None,
                "commands": (
                    [
                        {"type": command.type, "payload": dict(command.payload)}
                        for command in reduction.commands
                    ]
                    if reduction
                    else []
                ),
            }
            atomic_write_json(result_path, result)
            return result

    def approve_plan(
        self,
        *,
        project_id: str,
        expected_git_commit: str,
        expected_checkpoint_seq: int,
        actor_id: str = "research-controller",
        allow_blockers: bool = False,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Host-only automatic approval for an in-scope, blocker-free Plan."""

        with self._project_lock(project_id):
            head = self._git_head(required=commit)
            if head != expected_git_commit.lower():
                raise GitConflict(f"Git HEAD is {head}, expected {expected_git_commit}")
            checkpoint = self.orchestrator.store.load(project_id)
            if checkpoint["checkpoint_seq"] != expected_checkpoint_seq:
                raise GitConflict(
                    f"checkpoint is {checkpoint['checkpoint_seq']}, expected {expected_checkpoint_seq}"
                )
            if checkpoint.get("state") != "PLANNING_APPROVAL":
                raise RuntimeValidationError("Plan approval requires PLANNING_APPROVAL")
            plans = self.artifacts.workspace.query(
                kind="ResearchPlan", status="DRAFT", project_id=project_id
            )
            if not plans:
                raise RuntimeValidationError("no DRAFT ResearchPlan is available for approval")
            plan = max(plans, key=_draft_plan_recency_key)
            blocking = [
                review.id
                for review in self.artifacts.workspace.query(kind="Review", project_id=project_id)
                if review.data.get("target", {}).get("artifact_ref", {}).get("id") == plan.id
                and review.data.get("status") in {"OPEN", "IN_RESOLUTION"}
                and any(
                    issue.get("status") in {"OPEN", "ACCEPTED"}
                    and issue.get("severity") in {"BLOCKER", "MAJOR"}
                    for issue in review.data.get("issues", [])
                )
            ]
            if blocking and not allow_blockers:
                raise RuntimeValidationError(
                    "Plan requires a human scope decision",
                    details={"blocking_reviews": sorted(blocking)},
                )
            timestamp = utc_now()
            candidate = copy.deepcopy(plan.data)
            candidate["status"] = "APPROVED"
            candidate["revision"] = int(candidate["revision"]) + 1
            candidate["updated_at"] = timestamp
            candidate["provenance"]["updated_by"] = {
                "actor_type": "human" if allow_blockers else "agent", "actor_id": actor_id
            }
            plan_ref = {"id": plan.id, "kind": "ResearchPlan", "revision": candidate["revision"]}
            event = {
                "type": "PLAN_APPROVED",
                "at": timestamp,
                "payload": {"plan_ref": plan_ref, "reason": (
                    "Human approved scope decision" if allow_blockers
                    else "Automatic in-scope MVP approval"
                ), "actor_type": "human" if allow_blockers else "agent", "actor_id": actor_id},
            }
            checkpoint_path = self.orchestrator.store.path_for(project_id)
            project_path = self.root / self.artifacts.workspace.get(project_id).path
            plan_path = self.root / plan.path
            decision_path = self._control_decision_path(project_id, event, expected_checkpoint_seq)
            paths = self._dedupe_paths(
                [plan_path, checkpoint_path, project_path]
                + ([] if decision_path is None else [decision_path])
            )
            self._require_clean_targets(paths)
            backups = self._backup_values(paths)
            action_id = f"auto-plan-approval-{expected_checkpoint_seq}"
            journal_path = self.runtime_dir / "journals" / f"{sha256_json({'project': project_id, 'action': action_id, 'head': head})}.json"
            journal = {
                "transaction_version": "research-transaction/v0.1",
                "transaction_id": journal_path.stem,
                "project_id": project_id,
                "action_id": action_id,
                "status": "PREPARED",
                "paths": [str(path.relative_to(self.root)) for path in paths],
                "backups": backups,
                "commit_hash": None,
                "prepared_at": timestamp,
            }
            atomic_write_json(journal_path, journal)
            committed = False
            try:
                atomic_write_text(plan_path, yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True))
                reduction = self.orchestrator.apply(
                    project_id,
                    OrchestratorEvent("PLAN_APPROVED", timestamp, event["payload"]),
                    expected_seq=expected_checkpoint_seq,
                )
                changed_refs = self._apply_project_commands(project_id, reduction.commands)
                self._sync_checkpoint_refs(reduction.checkpoint, changed_refs)
                self.orchestrator.store.save(
                    reduction.checkpoint,
                    expected_seq=int(reduction.checkpoint["checkpoint_seq"]),
                )
                self._apply_decision_commands(project_id, reduction.commands, event, expected_checkpoint_seq)
                self._require_transaction_valid(project_id)
                commit_hash = head
                if commit:
                    commit_hash = self._commit(paths, project_id, action_id, [])
                    committed = True
                journal.update(status="COMMITTED", commit_hash=commit_hash, committed_at=utc_now())
                atomic_write_json(journal_path, journal)
                return {"checkpoint": reduction.checkpoint, "commit_hash": commit_hash}
            except Exception:
                if not committed:
                    self._restore_backups(backups)
                    self._unstage(paths)
                    journal.update(status="ROLLED_BACK", rolled_back_at=utc_now())
                    atomic_write_json(journal_path, journal)
                raise

    def materialize_outputs(
        self,
        *,
        project_id: str,
        action_id: str,
        expected_git_commit: str,
        expected_checkpoint_seq: int,
        commit: bool = True,
        push: bool = False,
        push_attempts: int = 3,
    ) -> dict[str, Any]:
        """Deterministically create execution artifacts from planned outputs."""

        result_path = (
            self.artifacts.action_dir(project_id, action_id)
            / "materialization-result.json"
        )
        if result_path.is_file():
            return json.loads(result_path.read_text(encoding="utf-8"))
        with self._project_lock(project_id):
            if result_path.is_file():
                return json.loads(result_path.read_text(encoding="utf-8"))
            head = self._git_head(required=commit or push)
            if head != expected_git_commit.lower():
                raise GitConflict(f"Git HEAD is {head}, expected {expected_git_commit}")
            checkpoint = self.orchestrator.store.load(project_id)
            if checkpoint["checkpoint_seq"] != expected_checkpoint_seq:
                raise GitConflict(
                    f"checkpoint is {checkpoint['checkpoint_seq']}, expected {expected_checkpoint_seq}"
                )
            if checkpoint.get("state") != "EXECUTION_MATERIALIZE":
                raise RuntimeValidationError("materialization requires EXECUTION_MATERIALIZE")
            plan_ref = checkpoint.get("active_plan")
            if not isinstance(plan_ref, Mapping):
                raise RuntimeValidationError("materialization requires an active Plan")
            plan = self.artifacts.workspace.get(str(plan_ref["id"]))
            candidate_plan = copy.deepcopy(plan.data)
            timestamp = utc_now()
            created: dict[str, dict[str, Any]] = {}
            mappings = {
                item["local_id"]: item["artifact_ref"]
                for package in candidate_plan.get("work_packages", [])
                for item in package.get("materialized_outputs", [])
            }
            outputs = [
                (package, output)
                for package in candidate_plan.get("work_packages", [])
                if package.get("track") in {"THEORY", "EXPERIMENT"}
                for output in package.get("planned_outputs", [])
                if output.get("required")
                and output.get("kind") in {"ScientificClaim", "Experiment"}
                and output["local_id"] not in mappings
            ]
            project_slug = project_id.removeprefix("proj-")
            existing_artifact_ids = set(self.artifacts.workspace.index())
            for kind in ("ScientificClaim", "Experiment"):
                for package, output in outputs:
                    if output.get("kind") != kind:
                        continue
                    output_id = output["local_id"]
                    prefix = "claim" if kind == "ScientificClaim" else "exp"
                    artifact_id = f"{prefix}-{project_slug}-{output_id}"
                    if artifact_id in existing_artifact_ids:
                        raise RuntimeValidationError(
                            "materialized artifact ID already exists; revised plans must "
                            f"use a new planned-output local_id: {artifact_id}"
                        )
                    if kind == "ScientificClaim":
                        value = self._materialized_claim(
                            project_id, artifact_id, output, mappings, timestamp
                        )
                    else:
                        value = self._materialized_experiment(
                            project_id, artifact_id, output, mappings, timestamp
                        )
                    created[artifact_id] = value
                    ref = {"id": artifact_id, "kind": kind, "revision": 1}
                    mappings[output_id] = ref
                    package.setdefault("materialized_outputs", []).append(
                        {"local_id": output_id, "artifact_ref": ref}
                    )
                    if package.get("status") == "TODO":
                        package["status"] = "IN_PROGRESS"
            if not created:
                raise RuntimeValidationError("active Plan has no unmaterialized execution outputs")
            candidate_plan["revision"] = int(candidate_plan["revision"]) + 1
            if candidate_plan.get("status") == "APPROVED":
                candidate_plan["status"] = "IN_PROGRESS"
            candidate_plan["updated_at"] = timestamp
            candidate_plan["provenance"]["updated_by"] = {
                "actor_type": "agent",
                "actor_id": "research-controller",
            }
            plan_path = self.root / plan.path
            artifact_paths = [
                self.root
                / self.artifacts.workspace.artifact_path(
                    kind=value["kind"],
                    artifact_id=artifact_id,
                    project_id=project_id,
                )
                for artifact_id, value in created.items()
            ]
            checkpoint_path = self.orchestrator.store.path_for(project_id)
            project_path = self.root / self.artifacts.workspace.get(project_id).path
            paths = self._dedupe_paths(
                [plan_path, *artifact_paths, checkpoint_path, project_path]
            )
            self._require_clean_targets(paths)
            backups = self._backup_values(paths)
            transaction_id = sha256_json(
                {
                    "project_id": project_id,
                    "action_id": action_id,
                    "expected_git_commit": expected_git_commit.lower(),
                    "artifacts": sorted(created),
                }
            )
            journal_path = (
                self.runtime_dir / "journals" / f"{transaction_id}.json"
            )
            journal = {
                "transaction_version": "research-transaction/v0.1",
                "transaction_id": transaction_id,
                "project_id": project_id,
                "action_id": action_id,
                "status": "PREPARED",
                "expected_git_commit": expected_git_commit.lower(),
                "proposal_hashes": [],
                "paths": [str(path.relative_to(self.root)) for path in paths],
                "backups": backups,
                "commit_hash": None,
                "prepared_at": timestamp,
            }
            atomic_write_json(journal_path, journal)
            commit_completed = False
            try:
                atomic_write_text(
                    plan_path,
                    yaml.safe_dump(
                        candidate_plan, sort_keys=False, allow_unicode=True
                    ),
                )
                for path, value in zip(artifact_paths, created.values()):
                    atomic_write_text(
                        path,
                        yaml.safe_dump(value, sort_keys=False, allow_unicode=True),
                    )
                self._require_transaction_valid(project_id)
                event = OrchestratorEvent(
                    "OUTPUTS_MATERIALIZED",
                    timestamp,
                    {
                        "action_id": action_id,
                        "plan_ref": {
                            "id": plan.id,
                            "kind": "ResearchPlan",
                            "revision": candidate_plan["revision"],
                        },
                        "target_output_ids": [
                            output["local_id"] for _, output in outputs
                        ],
                    },
                )
                reduction = self.orchestrator.apply(
                    project_id, event, expected_seq=expected_checkpoint_seq
                )
                self._apply_project_commands(project_id, reduction.commands)
                self._require_transaction_valid(project_id)
                commit_hash = head
                if commit:
                    commit_hash = self._commit(
                        paths, project_id, action_id, []
                    )
                    commit_completed = True
                journal["status"] = "COMMITTED"
                journal["commit_hash"] = commit_hash
                journal["committed_at"] = utc_now()
                atomic_write_json(journal_path, journal)
            except Exception:
                if not commit_completed:
                    self._restore_backups(backups)
                    self._unstage(paths)
                    journal["status"] = "ROLLED_BACK"
                    journal["rolled_back_at"] = utc_now()
                    atomic_write_json(journal_path, journal)
                raise
            pushed = False
            push_error = None
            if push and commit_hash is not None:
                pushed, push_error = self._push(push_attempts)
            result = {
                "artifact_ids": sorted(created),
                "commit_hash": commit_hash,
                "pushed": pushed,
                "push_pending": bool(push and not pushed),
                "push_error": push_error,
                "checkpoint": reduction.checkpoint,
            }
            atomic_write_json(result_path, result)
            return result

    @staticmethod
    def _materialized_claim(
        project_id: str,
        artifact_id: str,
        output: Mapping[str, Any],
        mappings: Mapping[str, Mapping[str, Any]],
        timestamp: str,
    ) -> dict[str, Any]:
        dependencies = [
            {"claim_ref": copy.deepcopy(mappings[item]), "relation": "USES"}
            for item in output.get("depends_on_outputs", [])
            if item in mappings and mappings[item].get("kind") == "ScientificClaim"
        ]
        actor = {"actor_type": "agent", "actor_id": "research-controller"}
        return {
            "schema_version": "research-artifact/v0.1.2",
            "kind": "ScientificClaim",
            "id": artifact_id,
            "project_id": project_id,
            "title": output["description"],
            "status": (
                "FORMALIZED"
                if output.get("verification_profile") == "EMPIRICAL"
                else "IDEA"
            ),
            "revision": 1,
            "created_at": timestamp,
            "updated_at": timestamp,
            "provenance": {"created_by": actor, "updated_by": actor},
            "tags": [],
            "claim_type": (
                "EMPIRICAL_CLAIM"
                if output.get("verification_profile") == "EMPIRICAL"
                else "CONJECTURE"
            ),
            "statement": output["description"],
            "assumptions": [],
            "dependencies": dependencies,
            "evidence": [],
            "verification": {
                "method": "NONE",
                "protocol": "Pending assigned verification Skills.",
                "resource_refs": [],
                "review_refs": [],
                "conclusion": "",
            },
            "paper_locations": [],
        }

    @staticmethod
    def _materialized_experiment(
        project_id: str,
        artifact_id: str,
        output: Mapping[str, Any],
        mappings: Mapping[str, Mapping[str, Any]],
        timestamp: str,
    ) -> dict[str, Any]:
        claim_refs = [
            mappings[item]
            for item in output.get("depends_on_outputs", [])
            if item in mappings and mappings[item].get("kind") == "ScientificClaim"
        ]
        if not claim_refs:
            raise RuntimeValidationError(
                f"Experiment output {output['local_id']} has no materialized Claim dependency"
            )
        actor = {"actor_type": "agent", "actor_id": "research-controller"}
        description = output["description"]
        return {
            "schema_version": "research-artifact/v0.1.2",
            "kind": "Experiment",
            "id": artifact_id,
            "project_id": project_id,
            "title": description,
            "status": "DRAFT",
            "revision": 1,
            "created_at": timestamp,
            "updated_at": timestamp,
            "provenance": {"created_by": actor, "updated_by": actor},
            "tags": [],
            "hypothesis": {
                "claim_ref": copy.deepcopy(claim_refs[0]),
                "operationalization": "Pending experiment-design Skill.",
            },
            "method": description,
            "baselines": [],
            "datasets": [],
            "metrics": [],
            "configuration": {
                "parameters": {},
                "environment": "Pending experiment-design Skill.",
            },
            "interpretation_plan": {
                "supported_when": ["Defined by experiment-design before READY."],
                "contradicted_when": ["Defined by experiment-design before READY."],
                "inconclusive_when": ["Defined by experiment-design before READY."],
                "analysis_plan": "Pending experiment-design Skill.",
                "seeds": [0],
                "repetitions": 1,
                "hyperparameter_budget": "Pending experiment-design Skill.",
                "ablations": [],
                "statistical_procedure": "Pending experiment-design Skill.",
                "exclusions": [],
            },
            "runs": [],
        }

    def recover(self, project_id: str | None = None) -> list[dict[str, Any]]:
        """Finish journal bookkeeping or roll back incomplete local promotions."""

        journal_dir = self.runtime_dir / "journals"
        outcomes: list[dict[str, Any]] = []
        for path in sorted(journal_dir.glob("*.json")) if journal_dir.is_dir() else []:
            journal = json.loads(path.read_text(encoding="utf-8"))
            if project_id is not None and journal.get("project_id") != project_id:
                continue
            if journal.get("status") != "PREPARED":
                continue
            action_id = str(journal["action_id"])
            committed = self._commit_for_action(action_id)
            if committed is not None:
                journal["status"] = "COMMITTED"
                journal["commit_hash"] = committed
                journal["recovered_at"] = utc_now()
                atomic_write_json(path, journal)
                outcome = "finished"
            else:
                self._restore_backups(journal["backups"])
                self._unstage([self.root / item for item in journal["paths"]])
                journal["status"] = "ROLLED_BACK"
                journal["recovered_at"] = utc_now()
                atomic_write_json(path, journal)
                outcome = "rolled_back"
            outcomes.append(
                {"transaction_id": journal["transaction_id"], "outcome": outcome}
            )
        return outcomes

    def fail_closed_checkpoint(
        self, *, project_id: str, reason: str
    ) -> dict[str, Any]:
        """Restore the last Git-valid checkpoint and enter BLOCKED.

        This recovery path never consults a session transcript.  The corrupt
        bytes are quarantined for diagnosis, while Git supplies the last valid
        control state used to construct a new auditable BLOCKED checkpoint.
        """

        with self._project_lock(project_id):
            checkpoint_path = self.orchestrator.store.path_for(project_id)
            try:
                return {"checkpoint": self.orchestrator.store.load(project_id), "recovered": False}
            except Exception:
                pass
            base, source_commit = self._last_valid_checkpoint(project_id)
            corrupt = checkpoint_path.read_bytes() if checkpoint_path.is_file() else b""
            digest = hashlib.sha256(corrupt).hexdigest()
            quarantine = (
                self.runtime_dir / "quarantine" / project_id
                / f"state-{digest}.yaml"
            )
            quarantine.parent.mkdir(parents=True, exist_ok=True)
            if not quarantine.exists():
                quarantine.write_bytes(corrupt)

            project_record = self.artifacts.workspace.get(project_id)
            project_path = self.root / project_record.path
            paths = [checkpoint_path, project_path]
            backups = self._backup_values(paths)
            current_head = self._git_head(required=True)
            action_id = f"recover-corrupted-checkpoint-{base['checkpoint_seq'] + 1}"
            journal_path = self.runtime_dir / "journals" / f"{sha256_json({'project': project_id, 'action': action_id, 'head': current_head})}.json"
            journal = {
                "transaction_version": "research-transaction/v0.1",
                "transaction_id": journal_path.stem,
                "project_id": project_id,
                "action_id": action_id,
                "status": "PREPARED",
                "paths": [str(path.relative_to(self.root)) for path in paths],
                "backups": backups,
                "commit_hash": None,
                "prepared_at": utc_now(),
            }
            atomic_write_json(journal_path, journal)
            committed = False
            try:
                checkpoint = copy.deepcopy(base)
                previous_state = str(checkpoint["state"])
                resume_state = (
                    checkpoint.get("resume_state")
                    if previous_state in {"BLOCKED", "PAUSED_BY_USER"}
                    else previous_state
                )
                if resume_state in {None, "BLOCKED", "PAUSED_BY_USER", "CANCELLED_BY_USER", "DONE"}:
                    resume_state = "DISCOVERY_QUESTION"
                checkpoint.update({
                    "checkpoint_seq": int(checkpoint["checkpoint_seq"]) + 1,
                    "state": "BLOCKED",
                    "status": "BLOCKED",
                    "resume_state": resume_state,
                    "blocker": {
                        "reason": reason,
                        "recorded_at": utc_now(),
                        "recovery_condition": "Inspect quarantined state and record an ACCEPTED recovery Decision before UNBLOCK.",
                    },
                    "pause": None,
                    "cancellation": None,
                    "updated_at": utc_now(),
                })
                self.orchestrator.store.require_valid(checkpoint)
                atomic_write_text(
                    checkpoint_path,
                    yaml.safe_dump(checkpoint, sort_keys=False, allow_unicode=True),
                )

                project = copy.deepcopy(project_record.data)
                if project.get("status") != "BLOCKED":
                    project["status"] = "BLOCKED"
                    project["revision"] = int(project["revision"]) + 1
                    project["updated_at"] = checkpoint["updated_at"]
                    project["provenance"]["updated_by"] = {
                        "actor_type": "agent",
                        "actor_id": "research-controller-recovery",
                    }
                    atomic_write_text(
                        project_path,
                        yaml.safe_dump(project, sort_keys=False, allow_unicode=True),
                    )
                self._require_transaction_valid(project_id)
                commit_hash = self._commit(paths, project_id, action_id, [])
                committed = True
                journal.update(
                    status="COMMITTED", commit_hash=commit_hash,
                    committed_at=utc_now(), source_commit=source_commit,
                )
                atomic_write_json(journal_path, journal)
                return {
                    "checkpoint": checkpoint,
                    "recovered": True,
                    "source_commit": source_commit,
                    "quarantine_path": str(quarantine.relative_to(self.root)),
                    "commit_hash": commit_hash,
                }
            except Exception:
                if not committed:
                    self._restore_backups(backups)
                    self._unstage(paths)
                    journal.update(status="ROLLED_BACK", rolled_back_at=utc_now())
                    atomic_write_json(journal_path, journal)
                raise

    def record_recovery_decision(
        self,
        *,
        project_id: str,
        reason: str,
        expected_git_commit: str,
        actor_id: str = "researcher",
    ) -> dict[str, Any]:
        """Record the explicit human authority required to leave BLOCKED."""

        with self._project_lock(project_id):
            head = self._git_head(required=True)
            if head != expected_git_commit.lower():
                raise GitConflict(f"Git HEAD is {head}, expected {expected_git_commit}")
            checkpoint = self.orchestrator.store.load(project_id)
            if checkpoint["state"] != "BLOCKED":
                raise RuntimeValidationError("recovery Decision is only valid in BLOCKED")
            slug = project_id.removeprefix("proj-")
            decision_id = f"decision-{slug}-recovery-{checkpoint['checkpoint_seq'] + 1}"
            path = self.root / self.artifacts.workspace.artifact_path(
                kind="Decision", artifact_id=decision_id, project_id=project_id
            )
            if path.is_file():
                record = self.artifacts.workspace.get(decision_id)
                return copy.deepcopy(record.data)
            self._require_clean_targets([path])
            backups = self._backup_values([path])
            timestamp = utc_now()
            project = self.artifacts.workspace.get(project_id)
            actor = {"actor_type": "human", "actor_id": actor_id}
            decision = {
                "schema_version": "research-artifact/v0.1.2",
                "kind": "Decision",
                "id": decision_id,
                "project_id": project_id,
                "title": "Authorize recovery from BLOCKED",
                "status": "ACCEPTED",
                "revision": 1,
                "created_at": timestamp,
                "updated_at": timestamp,
                "provenance": {"created_by": actor, "updated_by": actor},
                "tags": ["recovery"],
                "decision": "The recorded blocker has been inspected and recovery is authorized.",
                "context": str(checkpoint.get("blocker", {}).get("reason", "BLOCKED")),
                "reason": reason,
                "alternatives_considered": [{
                    "option": "Remain BLOCKED",
                    "rejected_because": "The stated recovery condition has been addressed.",
                }],
                "evidence_refs": [],
                "impact": [{
                    "affected_ref": {
                        "id": project_id,
                        "kind": "Project",
                        "revision": project.data["revision"],
                    },
                    "description": "Allows the deterministic controller to resume the saved state.",
                }],
                "approved_by": [actor],
                "effective_at": timestamp,
            }
            action_id = f"record-{decision_id}"
            journal_path = self.runtime_dir / "journals" / f"{sha256_json({'project': project_id, 'action': action_id, 'head': head})}.json"
            journal = {
                "transaction_version": "research-transaction/v0.1",
                "transaction_id": journal_path.stem,
                "project_id": project_id,
                "action_id": action_id,
                "status": "PREPARED",
                "paths": [str(path.relative_to(self.root))],
                "backups": backups,
                "commit_hash": None,
                "prepared_at": timestamp,
            }
            atomic_write_json(journal_path, journal)
            committed = False
            try:
                atomic_write_text(
                    path, yaml.safe_dump(decision, sort_keys=False, allow_unicode=True)
                )
                self._require_transaction_valid(project_id)
                commit_hash = self._commit([path], project_id, action_id, [])
                committed = True
                journal.update(status="COMMITTED", commit_hash=commit_hash, committed_at=utc_now())
                atomic_write_json(journal_path, journal)
                return decision
            except Exception:
                if not committed:
                    self._restore_backups(backups)
                    self._unstage([path])
                    journal.update(status="ROLLED_BACK", rolled_back_at=utc_now())
                    atomic_write_json(journal_path, journal)
                raise

    def record_proposal_decisions(
        self,
        *,
        project_id: str,
        decisions_path: str | Path,
        expected_git_commit: str,
        actor_id: str = "researcher",
    ) -> dict[str, Any]:
        """Pin a batch decision file and materialize human Decisions."""

        with self._project_lock(project_id):
            head = self._git_head(required=True)
            if head != expected_git_commit.lower():
                raise GitConflict(f"Git HEAD is {head}, expected {expected_git_commit}")
            checkpoint = self.orchestrator.store.load(project_id)
            if checkpoint["state"] != "PROPOSAL_WAITING_DECISIONS":
                raise RuntimeValidationError(
                    "proposal decisions are only valid in PROPOSAL_WAITING_DECISIONS"
                )
            value = load_decisions(decisions_path, project_id)
            project = self.artifacts.workspace.get(project_id)
            proposal_ref = project.data.get("active_proposal")
            if not isinstance(proposal_ref, Mapping):
                raise RuntimeValidationError("project has no active proposal")
            proposal = self.artifacts.workspace.get(str(proposal_ref["id"]))
            current_revision = int(proposal.data["revision"])
            if int(value.get("proposal_revision", -1)) != current_revision:
                raise RuntimeValidationError(
                    f"decision file targets proposal revision {value.get('proposal_revision')}, current is {current_revision}"
                )
            reviews = [
                record for record in self.artifacts.workspace.query(kind="Review", project_id=project_id)
                if record.data.get("target", {}).get("artifact_ref", {}).get("id") == proposal.id
                and record.data.get("target", {}).get("artifact_ref", {}).get("revision") == current_revision
            ]
            material = {
                issue["id"]: issue
                for review in reviews
                for issue in review.data.get("issues", [])
                if issue.get("severity") in {"BLOCKER", "MAJOR"}
                and issue.get("status") in {"OPEN", "ACCEPTED"}
            }
            supplied = {str(item["issue_id"]): item for item in value["decisions"]}
            missing = sorted(set(material) - set(supplied))
            unknown = sorted(set(supplied) - set(material))
            if missing or unknown:
                raise RuntimeValidationError(
                    "proposal decision coverage mismatch",
                    details={"missing": missing, "unknown": unknown},
                )

            timestamp = utc_now()
            actor = {"actor_type": "human", "actor_id": actor_id}
            slug = project_id.removeprefix("proj-")
            resource_path = (
                self.root / "projects" / project_id / "resources" / "proposal"
                / "human-decisions" / f"revision-{current_revision}-seq-{checkpoint['checkpoint_seq']}.yaml"
            )
            decision_paths: list[Path] = []
            decisions: list[dict[str, Any]] = []
            all_authorized = True
            batch_digest = hashlib.sha256(
                yaml.safe_dump(value, sort_keys=True).encode("utf-8")
            ).hexdigest()[:8]
            for issue_id, item in sorted(supplied.items()):
                action = str(item["action"])
                if action == "REJECT":
                    all_authorized = False
                decision_id = (
                    f"decision-{slug[:30]}-proposal-{checkpoint['checkpoint_seq']}-"
                    f"{issue_id[:40]}-{batch_digest}"
                )
                path = self.root / self.artifacts.workspace.artifact_path(
                    kind="Decision", artifact_id=decision_id, project_id=project_id
                )
                issue = material[issue_id]
                reason = str(item.get("reason") or item.get("instruction") or "Accept the reviewer resolution.")
                decision = {
                    "schema_version": "research-artifact/v0.1.3", "kind": "Decision",
                    "id": decision_id, "project_id": project_id,
                    "title": f"Proposal issue decision: {issue_id}",
                    "status": "ACCEPTED" if action in {"ACCEPT", "EDIT"} else "REJECTED",
                    "revision": 1, "created_at": timestamp, "updated_at": timestamp,
                    "provenance": {"created_by": actor, "updated_by": actor},
                    "tags": ["proposal-review", "human-authority"],
                    "decision": f"{action} resolution for proposal issue {issue_id}.",
                    "context": str(issue.get("description")), "reason": reason,
                    "alternatives_considered": [{"option": "Leave the issue unchanged", "rejected_because": "A recorded disposition is required."}],
                    "evidence_refs": [{"id": proposal.id, "kind": "ResearchProposal", "revision": current_revision}],
                    "impact": [{"affected_ref": {"id": proposal.id, "kind": "ResearchProposal", "revision": current_revision}, "description": str(issue.get("suggested_resolution"))}],
                    "proposal_issue": {
                        "issue_id": issue_id,
                        "action": action,
                        "instruction": item.get("instruction"),
                    },
                    "approved_by": [actor], "effective_at": timestamp,
                }
                atomic_write_text(path, yaml.safe_dump(decision, sort_keys=False, allow_unicode=True))
                decision_paths.append(path)
                decisions.append(decision)
            atomic_write_text(resource_path, yaml.safe_dump(value, sort_keys=False, allow_unicode=True))
            paths = [resource_path, *decision_paths]
            try:
                self._require_transaction_valid(project_id)
                commit_hash = self._commit(paths, project_id, "proposal-human-decisions", [])
            except Exception:
                for path in paths:
                    if path.exists():
                        path.unlink()
                self._unstage(paths)
                raise
            return {
                "decisions": decisions,
                "all_authorized": all_authorized,
                "resource": str(resource_path.relative_to(self.root)),
                "commit_hash": commit_hash,
            }

    def evaluate_proposal_gate(self, *, project_id: str) -> dict[str, Any]:
        """Build and validate the deterministic same-revision proposal gate."""

        checkpoint = self.orchestrator.store.load(project_id)
        if checkpoint.get("state") != "PROPOSAL_REVIEW_GATE":
            raise RuntimeValidationError(
                "proposal gate evaluation requires PROPOSAL_REVIEW_GATE"
            )
        project = self.artifacts.workspace.get(project_id)
        pointer = project.data.get("active_proposal")
        if not isinstance(pointer, Mapping):
            raise RuntimeValidationError("project has no active proposal")
        proposal = self.artifacts.workspace.get(str(pointer["id"]))
        ref = {
            "id": proposal.id,
            "kind": proposal.kind,
            "revision": proposal.data["revision"],
        }
        reviews = [
            item
            for item in self.artifacts.workspace.query(
                kind="Review", project_id=project_id
            )
            if item.data.get("target", {}).get("artifact_ref") == ref
            and item.data.get("assessment", {}).get("scheme")
            in {"NOVELTY", "PROPOSAL_CORRECTNESS"}
        ]
        latest: dict[str, Any] = {}
        for review in sorted(
            reviews,
            key=lambda item: (
                str(item.data.get("updated_at", "")),
                int(item.data.get("revision", 0)),
                item.id,
            ),
        ):
            latest[str(review.data["assessment"]["scheme"])] = review
        if set(latest) != {"NOVELTY", "PROPOSAL_CORRECTNESS"}:
            raise RuntimeValidationError(
                "proposal gate lacks same-revision novelty and correctness Reviews"
            )
        novelty = latest["NOVELTY"].data["assessment"]["outcome"]
        correctness = latest["PROPOSAL_CORRECTNESS"].data["assessment"]["outcome"]
        open_issues = [
            issue
            for review in latest.values()
            for issue in review.data.get("issues", [])
            if issue.get("status") in {"OPEN", "ACCEPTED"}
        ]
        material = any(
            issue.get("severity") in {"BLOCKER", "MAJOR"}
            for issue in open_issues
        )
        minor = any(
            issue.get("severity") in {"MINOR", "SUGGESTION"}
            for issue in open_issues
        )
        if novelty not in {"OPEN", "PARTIAL"} or correctness != "PASS" or material:
            verdict = "REVISION_REQUIRED"
        elif minor:
            verdict = "AUTO_REVISION"
        else:
            verdict = "PASS"
        review_refs = [
            {"id": item.id, "kind": "Review", "revision": item.data["revision"]}
            for item in latest.values()
        ]
        gate = {
            "gate_id": (
                f"gate-{project_id.removeprefix('proj-')}-proposal-"
                f"{checkpoint['checkpoint_seq']}"
            ),
            "gate_type": "PROPOSAL_REVIEW",
            "verdict": verdict,
            "based_on": [ref, *review_refs],
            "target_refs": [ref],
            "target_output_ids": [],
            "review_refs": review_refs,
            "rationale": (
                "Deterministic same-revision novelty and correctness gate."
            ),
            "recorded_at": utc_now(),
        }
        self.orchestrator.gates.require_valid(gate, None)
        return gate

    def write_proposal_decision_template(self, *, project_id: str) -> dict[str, Any]:
        """Write the operator template for every current material proposal issue."""

        checkpoint = self.orchestrator.store.load(project_id)
        if checkpoint.get("state") != "PROPOSAL_WAITING_DECISIONS":
            raise RuntimeValidationError(
                "decision template requires PROPOSAL_WAITING_DECISIONS"
            )
        project = self.artifacts.workspace.get(project_id)
        pointer = project.data.get("active_proposal")
        if not isinstance(pointer, Mapping):
            raise RuntimeValidationError("project has no active proposal")
        proposal = self.artifacts.workspace.get(str(pointer["id"]))
        ref = {
            "id": proposal.id,
            "kind": proposal.kind,
            "revision": proposal.data["revision"],
        }
        issues = [
            issue
            for review in self.artifacts.workspace.query(
                kind="Review", project_id=project_id
            )
            if review.data.get("target", {}).get("artifact_ref") == ref
            for issue in review.data.get("issues", [])
            if issue.get("severity") in {"BLOCKER", "MAJOR"}
            and issue.get("status") in {"OPEN", "ACCEPTED"}
        ]
        if not issues:
            raise RuntimeValidationError(
                "proposal decision state has no material issues"
            )
        value = {
            "project_id": project_id,
            "proposal_revision": proposal.data["revision"],
            "decisions": [
                {
                    "issue_id": issue["id"],
                    "action": "ACCEPT",
                    "reason": "",
                    "instruction": "",
                }
                for issue in sorted(issues, key=lambda issue: str(issue["id"]))
            ],
        }
        path = (
            self.runtime_dir
            / "requests"
            / project_id
            / "proposal-decisions.yaml"
        )
        atomic_write_text(
            path, yaml.safe_dump(value, sort_keys=False, allow_unicode=True)
        )
        return {"path": str(path), "template": value}

    def convert_approved_proposal(
        self, *, project_id: str, derived_project_id: str,
        expected_git_commit: str, commit: bool = True,
    ) -> dict[str, Any]:
        """Create a FULL_RESEARCH project pinned to an approved proposal revision."""

        if self._git_head(required=commit) != expected_git_commit.lower():
            raise GitConflict("Git HEAD changed before proposal conversion")
        checkpoint = self.orchestrator.store.load(project_id)
        if checkpoint.get("state") != "DONE":
            raise RuntimeValidationError("only a completed proposal review can be converted")
        source_project = self.artifacts.workspace.get(project_id)
        proposal_pointer = source_project.data.get("active_proposal")
        if not isinstance(proposal_pointer, Mapping):
            raise RuntimeValidationError("source project has no active proposal")
        proposal = self.artifacts.workspace.get(str(proposal_pointer["id"]))
        if proposal.data.get("status") != "APPROVED":
            raise RuntimeValidationError("conversion requires an APPROVED proposal revision")
        source_ref = {"id": proposal.id, "kind": "ResearchProposal", "revision": proposal.data["revision"]}
        initialized = self.initialize_project(
            project_id=derived_project_id,
            objective=f"Research derived from approved proposal {proposal.id} r{proposal.data['revision']}",
            workflow_mode="FULL_RESEARCH", source_proposal_ref=source_ref,
            commit=commit,
        )

        derived_slug = derived_project_id.removeprefix("proj-")
        timestamp = utc_now()
        actor = {"actor_type": "agent", "actor_id": "proposal-converter"}
        created: list[Path] = []
        question_refs: list[dict[str, Any]] = []
        question_map: dict[str, dict[str, Any]] = {}
        for position, old_ref in enumerate(proposal.data.get("question_refs", []), start=1):
            old = self.artifacts.workspace.get(str(old_ref["id"]))
            new = copy.deepcopy(old.data)
            new_id = f"rq-{derived_slug}-proposal-{position}"
            new.update({
                "id": new_id, "project_id": derived_project_id, "revision": 1,
                "created_at": timestamp, "updated_at": timestamp,
                "provenance": {"created_by": actor, "updated_by": actor},
            })
            path = self.root / self.artifacts.workspace.artifact_path(
                kind="ResearchQuestion", artifact_id=new_id, project_id=derived_project_id
            )
            atomic_write_text(path, yaml.safe_dump(new, sort_keys=False, allow_unicode=True))
            created.append(path)
            new_ref = {"id": new_id, "kind": "ResearchQuestion", "revision": 1}
            question_refs.append(new_ref)
            question_map[old.id] = new_ref

        source_plans = self.artifacts.workspace.query(kind="ResearchPlan", project_id=project_id)
        active_plan = None
        if source_plans:
            pinned_plan = proposal.data.get("plan_ref")
            old_plan = (
                self.artifacts.workspace.get(str(pinned_plan["id"]))
                if isinstance(pinned_plan, Mapping)
                else max(source_plans, key=lambda item: (str(item.data.get("updated_at", "")), int(item.data["revision"])))
            )
            plan = copy.deepcopy(old_plan.data)
            plan_id = f"plan-{derived_slug}-approved-proposal"
            plan.update({
                "id": plan_id, "project_id": derived_project_id, "status": "DRAFT",
                "revision": 1, "created_at": timestamp, "updated_at": timestamp,
                "provenance": {"created_by": actor, "updated_by": actor},
                "question_refs": question_refs,
            })
            for package in plan.get("work_packages", []):
                package["input_refs"] = [
                    question_map[ref["id"]] for ref in package.get("input_refs", [])
                    if ref.get("id") in question_map
                ]
                package["materialized_outputs"] = []
                if package.get("status") == "DONE":
                    package["status"] = "TODO"
            plan_path = self.root / self.artifacts.workspace.artifact_path(
                kind="ResearchPlan", artifact_id=plan_id, project_id=derived_project_id
            )
            atomic_write_text(plan_path, yaml.safe_dump(plan, sort_keys=False, allow_unicode=True))
            created.append(plan_path)
            active_plan = {"id": plan_id, "kind": "ResearchPlan", "revision": 1}

        project = self.artifacts.workspace.get(derived_project_id)
        original_project = copy.deepcopy(project.data)
        candidate = copy.deepcopy(project.data)
        candidate["research_questions"] = question_refs
        candidate["active_plan"] = active_plan
        candidate["revision"] = int(candidate["revision"]) + 1
        candidate["updated_at"] = timestamp
        candidate["provenance"]["updated_by"] = actor
        project_path = self.root / project.path
        atomic_write_text(project_path, yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True))
        created.append(project_path)
        try:
            self._require_transaction_valid(project_id)
            commit_hash = self._git_head(required=commit)
            if commit:
                commit_hash = self._commit(created, derived_project_id, "convert-approved-proposal", [])
        except Exception:
            # initialize_project is already an auditable commit; only remove the
            # uncommitted derived overlays from this second phase.
            for path in created:
                if path != project_path and path.exists():
                    path.unlink()
            atomic_write_text(
                project_path,
                yaml.safe_dump(original_project, sort_keys=False, allow_unicode=True),
            )
            self._unstage(created)
            raise
        return {
            "source_project": project_id, "project_id": derived_project_id,
            "source_proposal_ref": source_ref, "question_refs": question_refs,
            "plan_ref": active_plan, "checkpoint": initialized["checkpoint"],
            "commit_hash": commit_hash,
        }

    def push_head(self, attempts: int = 3) -> dict[str, Any]:
        """Replicate the current scientific commit without changing local truth."""

        commit_hash = self._git_head(required=True)
        pushed, error = self._push(attempts)
        return {
            "commit_hash": commit_hash,
            "pushed": pushed,
            "push_pending": not pushed,
            "push_error": error,
        }

    def _proposal_path(self, proposal: Proposal) -> Path:
        relative = self.artifacts.workspace.artifact_path(
            kind=proposal.kind,
            artifact_id=proposal.artifact_id,
            project_id=None if proposal.kind == "Project" else proposal.project_id,
        )
        return self.root / relative

    def _proposal_resource_paths(
        self, project_id: str, proposals: Iterable[Proposal]
    ) -> list[Path]:
        """Collect local immutable resources promoted with artifact proposals.

        Agents still write only staging/scratch locations.  A runtime adapter
        may copy an accepted proof, Lean report, or audit output into the
        project's resources directory before submission; this method verifies
        the advertised digest and makes that file part of the same recoverable
        Git transaction as the referencing artifact.
        """

        expected_root = (self.root / "projects" / project_id / "resources").resolve()
        paths: list[Path] = []

        def visit(value: Any) -> None:
            if isinstance(value, Mapping):
                uri = value.get("uri")
                digest = value.get("sha256")
                if isinstance(uri, str) and isinstance(digest, str):
                    if "://" not in uri and not Path(uri).is_absolute():
                        path = (self.root / uri).resolve()
                        try:
                            path.relative_to(expected_root)
                        except ValueError:
                            pass
                        else:
                            if not path.is_file():
                                raise RuntimeValidationError(
                                    f"referenced project resource does not exist: {uri}"
                                )
                            actual = hashlib.sha256(path.read_bytes()).hexdigest()
                            if actual != digest:
                                raise RuntimeValidationError(
                                    f"resource digest mismatch for {uri}"
                                )
                            paths.append(path)
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        for proposal in proposals:
            visit(proposal.candidate)
        return self._dedupe_paths(paths)

    def _apply_project_commands(
        self, project_id: str, commands: Iterable[Any]
    ) -> dict[str, dict[str, Any]]:
        """Materialize deterministic Project status/stage commands atomically."""

        record = self.artifacts.workspace.get(project_id)
        candidate = copy.deepcopy(record.data)
        changed = False
        changed_refs: dict[str, dict[str, Any]] = {}
        for command in commands:
            if command.type == "PROMOTE_VERIFIED_CLAIM":
                ref = self._promote_verified_claim(command.payload.get("artifact_ref"))
                changed_refs[ref["id"]] = ref
            elif command.type == "PROMOTE_EMPIRICAL_CLAIMS":
                for ref in self._promote_empirical_claims(project_id):
                    changed_refs[ref["id"]] = ref
            elif command.type == "PROMOTE_PROPOSAL_APPROVED":
                ref = self._promote_proposal_approved(project_id)
                changed_refs[ref["id"]] = ref
                candidate["active_proposal"] = copy.deepcopy(ref)
                changed = True
            elif command.type == "FINALIZE_EXPERIMENT":
                ref = self._finalize_experiment(command.payload.get("artifact_ref"))
                changed_refs[ref["id"]] = ref
            elif command.type == "FINALIZE_TRACK_WORK_PACKAGES":
                ref = self._finalize_track_work_packages(
                    project_id, str(command.payload.get("track"))
                )
                changed_refs[ref["id"]] = ref
                if candidate.get("active_plan", {}).get("id") == ref["id"]:
                    candidate["active_plan"] = copy.deepcopy(ref)
                    changed = True
            elif command.type == "APPLY_PAPER_TRACEABILITY":
                self._apply_paper_traceability(project_id)
            elif command.type == "UPDATE_PROJECT_STATUS":
                target = str(command.payload["status"])
                if candidate.get("status") != target:
                    candidate["status"] = target
                    changed = True
            elif command.type == "UPDATE_PROJECT_STAGE":
                target = str(command.payload["stage"])
                if candidate.get("stage") != target:
                    candidate["stage"] = target
                    changed = True
            elif command.type == "UPDATE_PROJECT_ACTIVE_PLAN":
                target = copy.deepcopy(command.payload["plan_ref"])
                if candidate.get("active_plan") != target:
                    candidate["active_plan"] = target
                    changed = True
            elif command.type == "FINALIZE_DELIVERABLES":
                for deliverable in candidate.get("deliverables", []):
                    if deliverable.get("status") != "FINAL":
                        deliverable["status"] = "FINAL"
                        changed = True
            elif command.type == "UPDATE_PROJECT_QUESTION":
                refs = list(candidate.get("research_questions", []))
                known = {item["id"] for item in refs}
                for artifact_id in command.payload.get("artifact_ids", []):
                    question = self.artifacts.workspace.get(str(artifact_id))
                    if question.kind == "ResearchQuestion" and question.id not in known:
                        refs.append({
                            "id": question.id, "kind": "ResearchQuestion",
                            "revision": question.data["revision"],
                        })
                        known.add(question.id)
                if refs != candidate.get("research_questions", []):
                    candidate["research_questions"] = refs
                    changed = True
        if not changed:
            return changed_refs
        candidate["revision"] = int(candidate["revision"]) + 1
        candidate["updated_at"] = utc_now()
        candidate["provenance"]["updated_by"] = {
            "actor_type": "agent",
            "actor_id": "research-orchestrator",
        }
        issues = self.artifacts.workspace.validate(overrides={project_id: candidate})
        if issues:
            from .workspace import ArtifactValidationError

            raise ArtifactValidationError(issues)
        path = self.root / record.path
        atomic_write_text(
            path, yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True)
        )
        changed_refs[project_id] = {
            "id": project_id, "kind": "Project", "revision": candidate["revision"]
        }
        return changed_refs

    def _promote_proposal_approved(self, project_id: str) -> dict[str, Any]:
        project = self.artifacts.workspace.get(project_id)
        ref = project.data.get("active_proposal")
        if not isinstance(ref, Mapping):
            raise RuntimeValidationError("proposal approval requires an active proposal")
        record = self.artifacts.workspace.get(str(ref["id"]))
        candidate = copy.deepcopy(record.data)
        if candidate.get("status") == "APPROVED":
            return {"id": record.id, "kind": record.kind, "revision": candidate["revision"]}
        candidate["status"] = "APPROVED"
        reviewed_revision = int(candidate["revision"])
        candidate["review_refs"] = [
            {"id": review.id, "kind": "Review", "revision": review.data["revision"]}
            for review in self.artifacts.workspace.query(kind="Review", project_id=project_id)
            if review.data.get("target", {}).get("artifact_ref", {}).get("id") == record.id
            and review.data.get("target", {}).get("artifact_ref", {}).get("revision") == reviewed_revision
            and review.data.get("assessment", {}).get("scheme") in {"NOVELTY", "PROPOSAL_CORRECTNESS"}
        ]
        plans = self.artifacts.workspace.query(kind="ResearchPlan", project_id=project_id)
        if plans:
            question_ids = {ref.get("id") for ref in candidate.get("question_refs", [])}
            matching = [
                plan for plan in plans
                if question_ids.issubset({
                    ref.get("id") for ref in plan.data.get("question_refs", [])
                })
            ]
            if not matching:
                raise RuntimeValidationError(
                    "approved proposal has no consistent reconstructed ResearchPlan"
                )
            plan = max(
                matching,
                key=lambda item: (str(item.data.get("updated_at", "")), int(item.data["revision"])),
            )
            candidate["plan_ref"] = {
                "id": plan.id, "kind": "ResearchPlan", "revision": plan.data["revision"]
            }
        candidate["revision"] = int(candidate["revision"]) + 1
        candidate["updated_at"] = utc_now()
        candidate["provenance"]["updated_by"] = {
            "actor_type": "human", "actor_id": "researcher"
        }
        atomic_write_text(
            self.root / record.path,
            yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True),
        )
        return {"id": record.id, "kind": record.kind, "revision": candidate["revision"]}

    def _promote_verified_claim(self, ref: Any) -> dict[str, Any]:
        """Apply a host-only Claim promotion after required Skill gates pass."""

        if not isinstance(ref, Mapping) or ref.get("kind") != "ScientificClaim":
            raise RuntimeValidationError("verification command lacks a Claim reference")
        claim = self.artifacts.workspace.get(str(ref.get("id")))
        if claim.data.get("status") not in {"FORMALIZED", "SUPPORTED"}:
            raise RuntimeValidationError(
                f"{claim.id} must be FORMALIZED or SUPPORTED before deterministic verification"
            )
        current_revision = int(claim.data["revision"])
        reviews = [
            record
            for record in self.artifacts.workspace.query(
                kind="Review", project_id=claim.data.get("project_id")
            )
            if record.data.get("target", {}).get("artifact_ref", {}).get("id")
            == claim.id
            and record.data.get("target", {}).get("artifact_ref", {}).get(
                "revision"
            )
            == current_revision
        ]
        candidate = copy.deepcopy(claim.data)
        candidate["revision"] = current_revision + 1
        candidate["status"] = "VERIFIED"
        candidate["updated_at"] = utc_now()
        candidate["provenance"]["updated_by"] = {
            "actor_type": "agent",
            "actor_id": "research-controller",
        }
        verification = candidate.setdefault("verification", {})
        proof_resources = [
            copy.deepcopy(resource)
            for resource in verification.get("resource_refs", [])
            if isinstance(resource, Mapping)
        ]
        verification["method"] = (
            "MIXED"
            if proof_resources or any(
                review.data.get("assessment", {}).get("scheme")
                in {"LEAN", "AXIOM_AUDIT", "SEMANTIC_ALIGNMENT"}
                for review in reviews
            )
            else "EXPERT_REVIEW"
        )
        verification["review_refs"] = [
            {
                "id": review.id,
                "kind": "Review",
                "revision": review.data["revision"],
            }
            for review in sorted(reviews, key=lambda item: item.id)
        ]
        verification["conclusion"] = (
            "Deterministic verification gate accepted all required Review schemes."
        )
        evidence = list(candidate.get("evidence", []))
        for resource in proof_resources:
            evidence.append(
                {
                    "relation": "SUPPORTS",
                    "source_resource": resource,
                    "summary": "Immutable proof resource audited by the independent verifier.",
                }
            )
        for review in sorted(reviews, key=lambda item: item.id):
            if (
                review.data.get("assessment", {}).get("scheme") == "THEORY"
                and review.data.get("assessment", {}).get("outcome") == "PASS"
            ):
                evidence.append(
                    {
                        "relation": "VERIFIES",
                        "source_ref": {
                            "id": review.id,
                            "kind": "Review",
                            "revision": review.data["revision"],
                        },
                        "summary": "Independent revision-pinned THEORY Review returned PASS.",
                    }
                )
        candidate["evidence"] = evidence
        atomic_write_text(
            self.root / claim.path,
            yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True),
        )
        return {"id": claim.id, "kind": "ScientificClaim", "revision": candidate["revision"]}

    def _empirical_claim_records(self, project_id: str) -> list[Any]:
        project = self.artifacts.workspace.get(project_id)
        plan_ref = project.data.get("active_plan")
        if not isinstance(plan_ref, Mapping):
            return []
        plan = self.artifacts.workspace.get(str(plan_ref["id"]))
        claim_ids = {
            str(mapping["artifact_ref"]["id"])
            for package in plan.data.get("work_packages", [])
            for output in package.get("planned_outputs", [])
            for mapping in package.get("materialized_outputs", [])
            if mapping.get("local_id") == output.get("local_id")
            and output.get("kind") == "ScientificClaim"
            and output.get("verification_profile") == "EMPIRICAL"
        }
        return [self.artifacts.workspace.get(claim_id) for claim_id in sorted(claim_ids)]

    def _promote_empirical_claims(self, project_id: str) -> list[dict[str, Any]]:
        """Promote empirical hypotheses only after experiment and consistency PASS."""

        records = self._empirical_claim_records(project_id)
        if not records:
            return []
        all_records = self.artifacts.workspace.query(project_id=project_id)
        consistency_reviews = [
            record for record in all_records
            if record.kind == "Review"
            and record.data.get("assessment", {}).get("scheme") == "CONSISTENCY"
            and record.data.get("assessment", {}).get("outcome") == "PASS"
        ]
        if not consistency_reviews:
            raise RuntimeValidationError(
                "EMPIRICAL Claim promotion requires a CONSISTENCY PASS Review"
            )
        promoted: list[dict[str, Any]] = []
        for claim in records:
            if claim.data.get("status") in {"VERIFIED", "IN_PAPER"}:
                continue
            experiments = [
                record for record in all_records
                if record.kind == "Experiment"
                and record.data.get("status") == "COMPLETED"
                and record.data.get("hypothesis", {}).get("claim_ref", {}).get("id")
                == claim.id
                and record.data.get("hypothesis", {}).get("claim_ref", {}).get("revision")
                == claim.data.get("revision")
                and record.data.get("result", {}).get("hypothesis_outcome") == "SUPPORTED"
            ]
            if len(experiments) != 1:
                raise RuntimeValidationError(
                    f"{claim.id} requires exactly one supported completed Experiment"
                )
            experiment = experiments[0]
            experiment_reviews = [
                record for record in all_records
                if record.kind == "Review"
                and record.data.get("assessment", {}).get("scheme") == "EXPERIMENT"
                and record.data.get("assessment", {}).get("outcome") == "SUPPORTED"
                and record.data.get("target", {}).get("artifact_ref", {}).get("id")
                == experiment.id
            ]
            if len(experiment_reviews) != 1:
                raise RuntimeValidationError(
                    f"{claim.id} requires exactly one supporting EXPERIMENT Review"
                )
            candidate = copy.deepcopy(claim.data)
            candidate["status"] = "VERIFIED"
            candidate["revision"] = int(candidate["revision"]) + 1
            candidate["updated_at"] = utc_now()
            candidate["provenance"]["updated_by"] = {
                "actor_type": "agent", "actor_id": "research-controller"
            }
            candidate.setdefault("evidence", []).append({
                "relation": "VERIFIES",
                "source_ref": {
                    "id": experiment.id, "kind": "Experiment",
                    "revision": experiment.data["revision"],
                },
                "summary": "A protocol-locked Experiment and independent Review supported the preregistered hypothesis.",
            })
            candidate["verification"] = {
                **candidate.get("verification", {}),
                "method": "EXPERIMENT",
                "review_refs": [
                    {"id": experiment_reviews[0].id, "kind": "Review", "revision": experiment_reviews[0].data["revision"]},
                    {"id": consistency_reviews[-1].id, "kind": "Review", "revision": consistency_reviews[-1].data["revision"]},
                ],
                "conclusion": "Controller promoted the empirical Claim after EXPERIMENT SUPPORTED and CONSISTENCY PASS.",
            }
            atomic_write_text(
                self.root / claim.path,
                yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True),
            )
            promoted.append({
                "id": claim.id, "kind": "ScientificClaim",
                "revision": candidate["revision"],
            })
        return promoted

    def _finalize_experiment(self, ref: Any) -> dict[str, Any]:
        """Apply the independent verifier outcome without granting it mutation rights."""
        if not isinstance(ref, Mapping) or ref.get("kind") != "Experiment":
            raise RuntimeValidationError("experiment finalization lacks an Experiment reference")
        experiment = self.artifacts.workspace.get(str(ref.get("id")))
        current_revision = int(experiment.data["revision"])
        reviews = [
            record for record in self.artifacts.workspace.query(
                kind="Review", project_id=experiment.data.get("project_id")
            )
            if record.data.get("target", {}).get("artifact_ref", {}).get("id") == experiment.id
            and record.data.get("target", {}).get("artifact_ref", {}).get("revision") == current_revision
            and record.data.get("assessment", {}).get("scheme") == "EXPERIMENT"
        ]
        if len(reviews) != 1:
            raise RuntimeValidationError(
                f"{experiment.id} requires exactly one current EXPERIMENT Review"
            )
        outcome = reviews[0].data["assessment"]["outcome"]
        if outcome not in {"SUPPORTED", "CONTRADICTED", "INCONCLUSIVE"}:
            raise RuntimeValidationError(f"invalid experiment outcome: {outcome}")
        if not experiment.data.get("runs"):
            raise RuntimeValidationError(f"{experiment.id} has no preserved runs")
        candidate = copy.deepcopy(experiment.data)
        candidate["status"] = "COMPLETED"
        candidate["revision"] = current_revision + 1
        candidate["updated_at"] = utc_now()
        candidate["provenance"]["updated_by"] = {
            "actor_type": "agent", "actor_id": "research-controller"
        }
        metric_values: dict[str, list[float]] = {}
        for run in candidate.get("runs", []):
            for name, value in run.get("metric_values", {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    metric_values.setdefault(str(name), []).append(float(value))
        aggregate_metrics = {
            name: sum(values) / len(values) for name, values in metric_values.items()
            if values
        }
        candidate["result"] = {
            "summary": reviews[0].data.get("summary", "Independent experiment verification."),
            "aggregate_metrics": aggregate_metrics,
            "hypothesis_outcome": outcome,
            "limitations": candidate.get("result", {}).get("limitations", []),
        }
        atomic_write_text(
            self.root / experiment.path,
            yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True),
        )
        return {"id": experiment.id, "kind": "Experiment", "revision": candidate["revision"]}

    def _finalize_track_work_packages(self, project_id: str, track: str) -> dict[str, Any]:
        checkpoint = self.orchestrator.store.load(project_id)
        plan_ref = checkpoint.get("active_plan")
        if not isinstance(plan_ref, Mapping):
            raise RuntimeValidationError("track completion requires an active Plan")
        plan = self.artifacts.workspace.get(str(plan_ref["id"]))
        candidate = copy.deepcopy(plan.data)
        track_names = (
            {"THEORY", "EXPERIMENT"}
            if track == "all"
            else {track.upper()}
        )
        changed = False
        for package in candidate.get("work_packages", []):
            if package.get("track") in track_names and package.get("status") != "DONE":
                package["status"] = "DONE"
                changed = True
            for mapping in package.get("materialized_outputs", []):
                ref = mapping.get("artifact_ref", {})
                if ref.get("id") in self.artifacts.workspace.index():
                    current = self.artifacts.workspace.get(str(ref["id"]))
                    ref["revision"] = current.data["revision"]
        branches = checkpoint.get("branch_states", {})
        selected = [value for value in branches.values() if value != "NOT_SELECTED"]
        if selected and all(value in {"COMPLETED", "WAIVED"} for value in selected):
            if candidate.get("status") == "IN_PROGRESS":
                candidate["status"] = "COMPLETED"
                changed = True
            for milestone in candidate.get("milestones", []):
                if milestone.get("status") == "PENDING":
                    milestone["status"] = "MET"
                    changed = True
        if changed:
            candidate["revision"] = int(candidate["revision"]) + 1
            candidate["updated_at"] = utc_now()
            candidate["provenance"]["updated_by"] = {
                "actor_type": "agent", "actor_id": "research-controller"
            }
            atomic_write_text(
                self.root / plan.path,
                yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True),
            )
        return {"id": plan.id, "kind": "ResearchPlan", "revision": candidate["revision"]}

    @staticmethod
    def _sync_checkpoint_refs(
        checkpoint: dict[str, Any], changed_refs: Mapping[str, Mapping[str, Any]]
    ) -> None:
        active = checkpoint.get("active_plan")
        if isinstance(active, dict) and active.get("id") in changed_refs:
            checkpoint["active_plan"] = copy.deepcopy(changed_refs[str(active["id"])])
        for progress in checkpoint.get("skill_progress", {}).values():
            ref = progress.get("artifact_ref")
            if isinstance(ref, dict) and ref.get("id") in changed_refs:
                progress["artifact_ref"] = copy.deepcopy(changed_refs[str(ref["id"])])

    def _apply_paper_traceability(self, project_id: str) -> None:
        """Apply writer anchor bindings without granting writer Claim access."""

        path = self.root / "projects" / project_id / "paper" / "traceability.yaml"
        trace = yaml.safe_load(path.read_text(encoding="utf-8"))
        manuscript = str(trace["manuscript_paths"][0])
        anchors: dict[str, list[str]] = {}
        for entry in trace.get("entries", []):
            for ref in entry.get("source_refs", []):
                if ref.get("kind") == "ScientificClaim":
                    anchors.setdefault(str(ref["id"]), []).append(
                        str(entry["anchor"])
                    )
        for claim_id, claim_anchors in anchors.items():
            claim = self.artifacts.workspace.get(claim_id)
            if claim.data.get("status") not in {"VERIFIED", "IN_PAPER"}:
                raise RuntimeValidationError(
                    f"writer traced non-VERIFIED Claim {claim_id}"
                )
            candidate = copy.deepcopy(claim.data)
            locations = {
                (item["manuscript_path"], item["anchor"])
                for item in candidate.get("paper_locations", [])
            }
            locations.update((manuscript, anchor) for anchor in claim_anchors)
            candidate["paper_locations"] = [
                {"manuscript_path": item[0], "anchor": item[1]}
                for item in sorted(locations)
            ]
            candidate["status"] = "IN_PAPER"
            candidate["revision"] = int(candidate["revision"]) + 1
            candidate["updated_at"] = utc_now()
            candidate["provenance"]["updated_by"] = {
                "actor_type": "agent",
                "actor_id": "research-controller",
            }
            atomic_write_text(
                self.root / claim.path,
                yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True),
            )

    def _apply_decision_commands(
        self,
        project_id: str,
        commands: Iterable[Any],
        event: Mapping[str, Any],
        checkpoint_seq: int,
    ) -> None:
        decision_commands = [
            command for command in commands if command.type.startswith("CREATE_")
        ]
        if not decision_commands:
            return
        path = self._control_decision_path(project_id, event, checkpoint_seq)
        if path is None:
            return
        command_names = ", ".join(command.type for command in decision_commands)
        timestamp = str(event.get("at") or utc_now())
        payload = dict(event.get("payload", {}))
        actor_type = str(payload.get("actor_type") or (
            "human" if event["type"] in {"USER_CANCEL", "FINAL_APPROVED"} else "agent"
        ))
        actor_id = str(payload.get("actor_id") or (
            "researcher" if actor_type == "human" else "research-orchestrator"
        ))
        decision_id = path.stem
        candidate = {
            "schema_version": "research-artifact/v0.1.2",
            "kind": "Decision",
            "id": decision_id,
            "project_id": project_id,
            "title": f"Controller decision for {event['type']}",
            "status": "ACCEPTED",
            "revision": 1,
            "created_at": timestamp,
            "updated_at": timestamp,
            "provenance": {
                "created_by": {"actor_type": actor_type, "actor_id": actor_id},
                "updated_by": {"actor_type": actor_type, "actor_id": actor_id},
            },
            "tags": ["orchestrator", "control-decision"],
            "decision": f"Accept deterministic control event {event['type']}.",
            "context": f"The state machine emitted {command_names}.",
            "reason": str(payload.get("reason") or payload.get("rationale") or event["type"]),
            "alternatives_considered": [],
            "evidence_refs": [],
            "impact": [
                {
                    "affected_ref": {"id": project_id, "kind": "Project"},
                    "description": "The deterministic controller applies the recorded workflow consequence.",
                }
            ],
            "approved_by": [{"actor_type": actor_type, "actor_id": actor_id}],
            "effective_at": timestamp,
        }
        atomic_write_text(
            path, yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True)
        )

    def _control_decision_path(
        self, project_id: str, event: Mapping[str, Any], checkpoint_seq: int
    ) -> Path | None:
        event_type = str(event.get("type"))
        payload = event.get("payload", {})
        creates_decision = event_type in {
            "USER_CANCEL", "PLAN_APPROVED", "PLAN_REVISION_REQUIRED", "FINAL_APPROVED"
        }
        if event_type == "GATE_RECORDED" and isinstance(payload, Mapping):
            gate = payload.get("gate")
            creates_decision = isinstance(gate, Mapping) and gate.get("verdict") in {
                "RETHINK_PLAN", "RETHINK_QUESTION"
            }
        if not creates_decision:
            return None
        slug = project_id.removeprefix("proj-")
        event_slug = str(event_type).lower().replace("_", "-")
        decision_id = f"decision-{slug}-{event_slug}-{checkpoint_seq + 1}"
        relative = self.artifacts.workspace.artifact_path(
            kind="Decision", artifact_id=decision_id, project_id=project_id
        )
        return self.root / relative

    def _require_transaction_valid(self, project_id: str) -> None:
        """Validate a transaction without deadlocking on unrelated legacy lint.

        Candidate artifacts are validated before promotion.  This overlay keeps
        all workspace-wide schema, reference, lifecycle, and scientific checks,
        while limiting newly introduced invocation-policy lint to the Project
        touched by the transaction.  A standalone workspace audit remains
        strict over every canonical Experiment.
        """

        project = self.artifacts.workspace.get(project_id)
        self.artifacts.workspace.require_valid(
            overrides={project_id: copy.deepcopy(project.data)}
        )

    @contextmanager
    def _project_lock(self, project_id: str) -> Iterator[None]:
        if fcntl is None:
            raise RuntimeValidationError("project file locks are unavailable on this platform")
        path = self.runtime_dir / "locks" / f"{project_id}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=check,
        )

    def _git_head(self, *, required: bool) -> str | None:
        result = self._git("rev-parse", "HEAD", check=False)
        if result.returncode != 0:
            if required:
                raise GitConflict("workspace is not a Git repository")
            return None
        return result.stdout.strip().lower()

    def _require_clean_targets(self, paths: Iterable[Path]) -> None:
        relative = [str(path.relative_to(self.root)) for path in paths]
        if not relative or self._git_head(required=False) is None:
            return
        result = self._git("status", "--porcelain", "--", *relative)
        if result.stdout.strip():
            raise GitConflict(
                "transaction targets contain uncommitted changes",
                details={"status": result.stdout.splitlines()},
            )

    def _require_immutable_resource_changes(self, paths: Iterable[Path]) -> None:
        """Allow new action resources, but never overwrite a tracked resource."""

        for path in paths:
            relative = str(path.relative_to(self.root))
            tracked = self._git(
                "ls-files", "--error-unmatch", "--", relative, check=False
            )
            if tracked.returncode != 0:
                continue
            changed = self._git("diff", "--quiet", "--", relative, check=False)
            if changed.returncode != 0:
                raise GitConflict(
                    f"immutable scientific resource was overwritten: {relative}"
                )

    def _changed_paper_paths(self, project_id: str) -> list[Path]:
        paper = Path("projects") / project_id / "paper"
        result = self._git(
            "status", "--porcelain", "--untracked-files=all", "--", str(paper)
        )
        paths: list[Path] = []
        for line in result.stdout.splitlines():
            relative = line[3:]
            if " -> " in relative:
                relative = relative.split(" -> ", 1)[1]
            path = (self.root / relative).resolve()
            expected = (self.root / paper).resolve()
            try:
                path.relative_to(expected)
            except ValueError as exc:
                raise RuntimeValidationError(
                    f"writer changed a path outside paper/: {relative}"
                ) from exc
            if path.is_file():
                paths.append(path)
        return self._dedupe_paths(paths)

    def _commit(
        self,
        paths: Iterable[Path],
        project_id: str,
        action_id: str,
        proposals: Iterable[Proposal],
    ) -> str:
        relative = [str(path.relative_to(self.root)) for path in paths]
        self._git("add", "--", *relative)
        message = (
            f"research({project_id}): accept {action_id}\n\n"
            + "Artifacts: "
            + (
                ", ".join(sorted(proposal.artifact_id for proposal in proposals))
                or "paper resources"
            )
        )
        result = self._git("commit", "--only", "-m", message, "--", *relative, check=False)
        if result.returncode != 0:
            raise GitConflict(
                f"Git commit failed: {result.stderr.strip() or result.stdout.strip()}"
            )
        return self._git_head(required=True) or ""

    def _push(self, attempts: int) -> tuple[bool, str | None]:
        last_error: str | None = None
        for attempt in range(max(1, attempts)):
            result = self._git("push", "origin", "HEAD", check=False)
            if result.returncode == 0:
                return True, None
            last_error = result.stderr.strip() or result.stdout.strip()
            if attempt + 1 < attempts:
                time.sleep(min(2**attempt, 4))
        return False, last_error

    def _commit_for_action(self, action_id: str) -> str | None:
        if self._git_head(required=False) is None:
            return None
        result = self._git(
            "log", "--all", "-1", "--format=%H", f"--grep={action_id}",
            check=False,
        )
        return result.stdout.strip().lower() or None

    def _last_valid_checkpoint(self, project_id: str) -> tuple[dict[str, Any], str]:
        relative = str(self.orchestrator.store.path_for(project_id).relative_to(self.root))
        history = self._git("log", "--format=%H", "--", relative, check=False)
        for commit in history.stdout.splitlines():
            shown = self._git("show", f"{commit}:{relative}", check=False)
            if shown.returncode != 0:
                continue
            try:
                value = yaml.safe_load(shown.stdout)
                if not isinstance(value, dict):
                    continue
                self.orchestrator.store.require_valid(value)
            except Exception:
                continue
            return value, commit.lower()
        raise RuntimeValidationError(
            f"no valid Git checkpoint exists for {project_id}; manual repository recovery is required"
        )

    def _backup_values(self, paths: Iterable[Path]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for path in paths:
            relative = str(path.relative_to(self.root))
            values[relative] = (
                {
                    "exists": True,
                    "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
                }
                if path.is_file()
                else {"exists": False, "content_base64": None}
            )
        return values

    def _restore_backups(self, backups: Mapping[str, Mapping[str, Any]]) -> None:
        for relative, backup in backups.items():
            path = self.root / relative
            if backup.get("exists"):
                content = base64.b64decode(str(backup["content_base64"]))
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(f".{path.name}.recovery-{os.getpid()}")
                temporary.write_bytes(content)
                os.replace(temporary, path)
            elif path.exists():
                path.unlink()

    def _unstage(self, paths: Iterable[Path]) -> None:
        if self._git_head(required=False) is None:
            return
        relative = [str(path.relative_to(self.root)) for path in paths]
        if not relative:
            return
        self._git("restore", "--staged", "--", *relative, check=False)

    @staticmethod
    def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
        result: list[Path] = []
        seen: set[Path] = set()
        for path in paths:
            if path not in seen:
                seen.add(path)
                result.append(path)
        return result
