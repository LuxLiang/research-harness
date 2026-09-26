"""Filesystem-backed Artifact Layer for Research Artifact Schema v0.1.2."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .resources import resource_path
from typing import Any, Iterable, Iterator, Mapping

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from .lifecycle import stage_transition_allowed, transition_allowed


SCHEMA_VERSION = "v0.1.2"
PROPOSAL_SCHEMA_VERSION = "v0.1.3"
INPUT_RESOURCE_SCHEMA_VERSION = "v0.1.4"
ARTIFACT_KINDS = {
    "Project",
    "ResearchQuestion",
    "ResearchPlan",
    "LiteratureEvidence",
    "ScientificClaim",
    "Experiment",
    "Review",
    "Decision",
    "ResearchProposal",
}
KIND_SCHEMAS = {
    "Project": "project.schema.json",
    "ResearchQuestion": "research-question.schema.json",
    "ResearchPlan": "research-plan.schema.json",
    "LiteratureEvidence": "literature-evidence.schema.json",
    "ScientificClaim": "scientific-claim.schema.json",
    "Experiment": "experiment.schema.json",
    "Review": "review.schema.json",
    "Decision": "decision.schema.json",
    "ResearchProposal": "research-proposal.schema.json",
}
KIND_DIRECTORIES = {
    "ResearchQuestion": "questions",
    "ResearchPlan": "plans",
    "LiteratureEvidence": "literature",
    "ScientificClaim": "claims",
    "Experiment": "experiments",
    "Review": "reviews",
    "Decision": "decisions",
    "ResearchProposal": "proposals",
}


class ArtifactError(RuntimeError):
    """Base error for artifact operations."""


class RevisionConflict(ArtifactError):
    """Raised when an optimistic-concurrency revision does not match."""


class ArtifactValidationError(ArtifactError):
    """Raised when a candidate artifact or workspace is invalid."""

    def __init__(self, issues: Iterable["ValidationIssue"]):
        self.issues = list(issues)
        super().__init__("\n".join(str(issue) for issue in self.issues))


@dataclass(frozen=True, order=True)
class ValidationIssue:
    """One deterministic validation finding."""

    path: str
    code: str
    message: str
    artifact_id: str | None = None

    def __str__(self) -> str:
        identity = f" [{self.artifact_id}]" if self.artifact_id else ""
        return f"{self.path}{identity}: {self.code}: {self.message}"


@dataclass(frozen=True)
class ArtifactRecord:
    """A parsed artifact and its workspace-relative location."""

    path: Path
    data: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.data.get("id", ""))

    @property
    def kind(self) -> str:
        return str(self.data.get("kind", ""))


class _StringTimestampLoader(yaml.SafeLoader):
    """Safe YAML loader that leaves timestamps as RFC3339/date strings."""


for first_char, resolvers in list(_StringTimestampLoader.yaml_implicit_resolvers.items()):
    _StringTimestampLoader.yaml_implicit_resolvers[first_char] = [
        resolver for resolver in resolvers if resolver[0] != "tag:yaml.org,2002:timestamp"
    ]


def _yaml_load(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return yaml.load(handle, Loader=_StringTimestampLoader)


def _json_path(parts: Iterable[Any]) -> str:
    result = "$"
    for part in parts:
        if isinstance(part, int):
            result += f"[{part}]"
        else:
            result += f".{part}"
    return result


def iter_artifact_refs(value: Any) -> Iterator[dict[str, Any]]:
    """Yield artifact references embedded in an artifact-like value."""

    if isinstance(value, dict):
        keys = set(value)
        if {"id", "kind"}.issubset(keys) and keys.issubset({"id", "kind", "revision"}):
            if value.get("kind") in ARTIFACT_KINDS:
                yield value
                return
        for child in value.values():
            yield from iter_artifact_refs(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_artifact_refs(child)


class ArtifactWorkspace:
    """Read, query, validate, graph, and transition a research workspace."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.schema_dir = resource_path(self.root, "schemas") / SCHEMA_VERSION
        self.proposal_schema_dir = resource_path(self.root, "schemas") / PROPOSAL_SCHEMA_VERSION
        self.input_resource_schema_dir = (
            resource_path(self.root, "schemas") / INPUT_RESOURCE_SCHEMA_VERSION
        )
        self.projects_dir = self.root / "projects"
        self._validators = self._load_validators()

    def _load_validators(self) -> dict[tuple[str, str], Draft202012Validator]:
        if not self.schema_dir.is_dir():
            raise ArtifactError(f"schema directory not found: {self.schema_dir}")

        schemas: dict[str, dict[str, Any]] = {}
        paths = list(sorted(self.schema_dir.glob("*.schema.json")))
        if self.proposal_schema_dir.is_dir():
            paths.extend(sorted(self.proposal_schema_dir.glob("*.schema.json")))
        if self.input_resource_schema_dir.is_dir():
            paths.extend(sorted(self.input_resource_schema_dir.glob("*.schema.json")))
        for path in paths:
            try:
                schema = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ArtifactError(f"cannot load schema {path}: {exc}") from exc
            Draft202012Validator.check_schema(schema)
            schemas[schema["$id"]] = schema

        base_names = {Path(value["$id"]).name for value in schemas.values() if "/v0.1.2/" in value["$id"]}
        expected = {"common.schema.json", *[name for kind, name in KIND_SCHEMAS.items() if kind != "ResearchProposal"]}
        missing = expected - base_names
        if missing:
            raise ArtifactError(f"missing schemas: {', '.join(sorted(missing))}")

        registry = Registry().with_resources(
            (schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()
        )
        checker = FormatChecker()
        validators: dict[tuple[str, str], Draft202012Validator] = {}
        for version in (
            SCHEMA_VERSION,
            PROPOSAL_SCHEMA_VERSION,
            INPUT_RESOURCE_SCHEMA_VERSION,
        ):
            for kind, filename in KIND_SCHEMAS.items():
                schema_id = f"https://research-harness.local/schemas/{version}/{filename}"
                schema = schemas.get(schema_id)
                if schema is None:
                    continue
                validators[(f"research-artifact/{version}", kind)] = Draft202012Validator(
                    schema, registry=registry, format_checker=checker
                )
        return validators

    def discover(self) -> list[ArtifactRecord]:
        """Discover version-controlled artifact candidates beneath projects/."""

        if not self.projects_dir.exists():
            return []
        records: list[ArtifactRecord] = []
        for path in sorted(self.projects_dir.glob("proj-*")):
            if not path.is_dir():
                continue
            candidates = [path / "project.yaml"]
            for directory in KIND_DIRECTORIES.values():
                candidates.extend(sorted((path / directory).glob("*.yaml")))
            for candidate in candidates:
                if not candidate.is_file():
                    continue
                try:
                    data = _yaml_load(candidate)
                except (OSError, yaml.YAMLError) as exc:
                    data = {"__load_error__": str(exc)}
                if not isinstance(data, dict):
                    data = {"__load_error__": "artifact root must be a YAML mapping"}
                records.append(ArtifactRecord(candidate.relative_to(self.root), data))
        return records

    def index(self) -> dict[str, ArtifactRecord]:
        """Return artifacts keyed by ID; duplicate IDs raise an error."""

        index: dict[str, ArtifactRecord] = {}
        for record in self.discover():
            if not record.id:
                continue
            if record.id in index:
                raise ArtifactError(
                    f"duplicate artifact ID {record.id}: {index[record.id].path}, {record.path}"
                )
            index[record.id] = record
        return index

    def get(self, artifact_id: str) -> ArtifactRecord:
        """Get one artifact by stable ID."""

        record = self.index().get(artifact_id)
        if record is None:
            raise ArtifactError(f"artifact not found: {artifact_id}")
        return record

    def query(
        self,
        *,
        kind: str | None = None,
        status: str | None = None,
        project_id: str | None = None,
    ) -> list[ArtifactRecord]:
        """Query artifacts without relying on a generated index."""

        records = self.discover()
        if kind is not None:
            records = [record for record in records if record.kind == kind]
        if status is not None:
            records = [record for record in records if record.data.get("status") == status]
        if project_id is not None:
            records = [
                record
                for record in records
                if record.data.get("project_id", record.id) == project_id
            ]
        return records

    def artifact_path(
        self, *, kind: str, artifact_id: str, project_id: str | None = None
    ) -> Path:
        """Return the canonical workspace-relative path for an artifact."""

        if kind == "Project":
            if project_id is not None and project_id != artifact_id:
                raise ArtifactError("Project project_id must be absent or equal its id")
            return Path("projects") / artifact_id / "project.yaml"
        directory = KIND_DIRECTORIES.get(kind)
        if directory is None:
            raise ArtifactError(f"unknown artifact kind: {kind}")
        if not project_id:
            raise ArtifactError(f"{kind} requires project_id")
        return Path("projects") / project_id / directory / f"{artifact_id}.yaml"

    def validate(
        self,
        overrides: Mapping[str, dict[str, Any]] | None = None,
        additions: Iterable[ArtifactRecord] | None = None,
    ) -> list[ValidationIssue]:
        """Validate the canonical workspace plus an optional candidate overlay."""

        records = self.discover()
        override_map = overrides or {}
        addition_records = list(additions or [])
        invocation_overlay_ids = set(override_map) | {
            record.id for record in addition_records if record.id
        }
        records = [
            ArtifactRecord(record.path, copy.deepcopy(override_map.get(record.id, record.data)))
            for record in records
        ]
        records.extend(
            ArtifactRecord(record.path, copy.deepcopy(record.data))
            for record in addition_records
        )
        issues: list[ValidationIssue] = []
        index: dict[str, ArtifactRecord] = {}
        schema_valid_ids: set[str] = set()

        for record in records:
            if "__load_error__" in record.data:
                issues.append(
                    ValidationIssue(str(record.path), "yaml", record.data["__load_error__"])
                )
                continue
            artifact_id = record.id or None
            kind = record.kind
            schema_version = str(record.data.get("schema_version", ""))
            validator = self._validators.get((schema_version, kind))
            if validator is None:
                issues.append(
                    ValidationIssue(str(record.path), "kind", f"unsupported kind/schema: {kind!r} {schema_version!r}", artifact_id)
                )
                continue
            schema_errors = sorted(
                validator.iter_errors(record.data),
                key=lambda error: (list(error.absolute_path), error.message),
            )
            for error in schema_errors:
                issues.append(
                    ValidationIssue(
                        f"{record.path}:{_json_path(error.absolute_path)}",
                        "schema",
                        error.message,
                        artifact_id,
                    )
                )
            if not schema_errors and artifact_id:
                schema_valid_ids.add(artifact_id)
            if artifact_id:
                if artifact_id in index:
                    issues.append(
                        ValidationIssue(
                            str(record.path),
                            "duplicate-id",
                            f"also defined at {index[artifact_id].path}",
                            artifact_id,
                        )
                    )
                else:
                    index[artifact_id] = record

            issues.extend(self._validate_layout(record))

        issues.extend(self._validate_references(index, schema_valid_ids))
        issues.extend(self._validate_claim_dag(index, schema_valid_ids))
        issues.extend(self._validate_claim_evidence(index, schema_valid_ids))
        issues.extend(self._validate_claim_verification_profiles(index, schema_valid_ids))
        issues.extend(self._validate_plan_work_packages(index, schema_valid_ids))
        issues.extend(
            self._validate_experiment_readiness(
                index, schema_valid_ids, invocation_overlay_ids
            )
        )
        issues.extend(self._validate_reviews(index, schema_valid_ids))
        issues.extend(self._validate_proposal_contracts(index, schema_valid_ids))
        issues.extend(self._validate_project_gates(index, schema_valid_ids))
        return sorted(set(issues))

    def require_valid(self, overrides: Mapping[str, dict[str, Any]] | None = None) -> None:
        """Raise with all findings when the workspace is invalid."""

        issues = self.validate(overrides=overrides)
        if issues:
            raise ArtifactValidationError(issues)

    def _validate_layout(self, record: ArtifactRecord) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        path = record.path
        artifact_id = record.id or None
        if len(path.parts) < 3 or path.parts[0] != "projects":
            return [ValidationIssue(str(path), "layout", "artifact must be inside projects/proj-*", artifact_id)]
        project_directory = path.parts[1]
        kind = record.kind
        if kind == "Project":
            if path.name != "project.yaml":
                issues.append(ValidationIssue(str(path), "layout", "Project must be project.yaml", artifact_id))
            if record.id and record.id != project_directory:
                issues.append(ValidationIssue(str(path), "layout", f"Project ID must equal directory {project_directory}", artifact_id))
        elif kind in KIND_DIRECTORIES:
            expected_directory = KIND_DIRECTORIES[kind]
            if len(path.parts) < 4 or path.parts[2] != expected_directory:
                issues.append(ValidationIssue(str(path), "layout", f"{kind} must be in {expected_directory}/", artifact_id))
            if record.id and path.stem != record.id:
                issues.append(ValidationIssue(str(path), "layout", "filename must equal artifact ID", artifact_id))
            if record.data.get("project_id") != project_directory:
                issues.append(ValidationIssue(str(path), "project-mismatch", f"project_id must be {project_directory}", artifact_id))
        return issues

    def _validate_references(
        self, index: Mapping[str, ArtifactRecord], valid: set[str]
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for source_id in sorted(valid):
            source = index[source_id]
            source_project = source.id if source.kind == "Project" else source.data.get("project_id")
            for ref in iter_artifact_refs(source.data):
                target_id = ref["id"]
                target = index.get(target_id)
                if target is None:
                    issues.append(ValidationIssue(str(source.path), "dangling-ref", f"reference does not exist: {target_id}", source_id))
                    continue
                if target.kind != ref["kind"]:
                    issues.append(ValidationIssue(str(source.path), "wrong-ref-kind", f"{target_id} is {target.kind}, not {ref['kind']}", source_id))
                target_project = target.id if target.kind == "Project" else target.data.get("project_id")
                derived_proposal_ref = (
                    source.kind == "Project"
                    and source.data.get("workflow_mode") == "FULL_RESEARCH"
                    and isinstance(source.data.get("source_proposal_ref"), Mapping)
                    and source.data["source_proposal_ref"].get("id") == target_id
                    and target.kind == "ResearchProposal"
                    and target.data.get("status") == "APPROVED"
                )
                if source_project != target_project and not derived_proposal_ref:
                    issues.append(ValidationIssue(str(source.path), "cross-project-ref", f"v0.1.2 forbids reference from {source_project} to {target_project}", source_id))
                pinned_revision = ref.get("revision")
                current_revision = target.data.get("revision")
                if isinstance(pinned_revision, int) and isinstance(current_revision, int) and pinned_revision > current_revision:
                    issues.append(ValidationIssue(str(source.path), "future-revision", f"{target_id} revision {pinned_revision} exceeds current revision {current_revision}", source_id))
        return issues

    def _validate_claim_dag(
        self, index: Mapping[str, ArtifactRecord], valid: set[str]
    ) -> list[ValidationIssue]:
        claims = {
            artifact_id: record
            for artifact_id, record in index.items()
            if artifact_id in valid and record.kind == "ScientificClaim"
        }
        edges = {
            artifact_id: [entry["claim_ref"]["id"] for entry in record.data.get("dependencies", [])]
            for artifact_id, record in claims.items()
        }
        issues: list[ValidationIssue] = []
        state: dict[str, int] = {}
        stack: list[str] = []

        def visit(node: str) -> None:
            state[node] = 1
            stack.append(node)
            for target in edges.get(node, []):
                if target not in claims:
                    continue
                if state.get(target) == 1:
                    start = stack.index(target)
                    cycle = " -> ".join([*stack[start:], target])
                    issues.append(ValidationIssue(str(claims[node].path), "claim-cycle", cycle, node))
                elif state.get(target, 0) == 0:
                    visit(target)
            stack.pop()
            state[node] = 2

        for claim_id in sorted(claims):
            if state.get(claim_id, 0) == 0:
                visit(claim_id)
        return issues

    def _validate_plan_work_packages(
        self, index: Mapping[str, ArtifactRecord], valid: set[str]
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for artifact_id in sorted(valid):
            record = index[artifact_id]
            if record.kind != "ResearchPlan":
                continue
            packages = record.data.get("work_packages", [])
            package_ids = [package.get("id") for package in packages]
            duplicates = {package_id for package_id in package_ids if package_ids.count(package_id) > 1}
            for duplicate in sorted(duplicates):
                issues.append(ValidationIssue(str(record.path), "duplicate-work-package", duplicate, artifact_id))
            known = set(package_ids)
            graph = {package["id"]: package.get("depends_on", []) for package in packages}
            for package_id, dependencies in graph.items():
                for dependency in dependencies:
                    if dependency not in known:
                        issues.append(ValidationIssue(str(record.path), "dangling-work-package", f"{package_id} depends on {dependency}", artifact_id))
            if _has_cycle(graph):
                issues.append(ValidationIssue(str(record.path), "work-package-cycle", "work-package dependencies must be a DAG", artifact_id))

            outputs = [
                output
                for package in packages
                for output in package.get("planned_outputs", [])
            ]
            output_ids = [output.get("local_id") for output in outputs]
            duplicate_outputs = {
                output_id for output_id in output_ids if output_ids.count(output_id) > 1
            }
            for duplicate in sorted(duplicate_outputs):
                issues.append(
                    ValidationIssue(
                        str(record.path),
                        "duplicate-planned-output",
                        duplicate,
                        artifact_id,
                    )
                )

            known_outputs = set(output_ids)
            output_graph = {
                output["local_id"]: output.get("depends_on_outputs", [])
                for output in outputs
            }
            output_kinds = {
                output["local_id"]: output.get("kind") for output in outputs
            }
            for output_id, dependencies in output_graph.items():
                for dependency in dependencies:
                    if dependency not in known_outputs:
                        issues.append(
                            ValidationIssue(
                                str(record.path),
                                "dangling-planned-output",
                                f"{output_id} depends on {dependency}",
                                artifact_id,
                            )
                        )
                if (
                    output_kinds.get(output_id) == "Experiment"
                    and not any(output_kinds.get(dependency) == "ScientificClaim" for dependency in dependencies)
                ):
                    issues.append(
                        ValidationIssue(
                            str(record.path),
                            "experiment-without-planned-claim",
                            f"{output_id} must depend on a ScientificClaim planned output",
                            artifact_id,
                        )
                    )
            if _has_cycle(output_graph):
                issues.append(
                    ValidationIssue(
                        str(record.path),
                        "planned-output-cycle",
                        "planned output dependencies must be a DAG",
                        artifact_id,
                    )
                )

            mappings = [
                mapping
                for package in packages
                for mapping in package.get("materialized_outputs", [])
            ]
            mapped_ids = [mapping.get("local_id") for mapping in mappings]
            duplicate_mappings = {
                output_id for output_id in mapped_ids if mapped_ids.count(output_id) > 1
            }
            for duplicate in sorted(duplicate_mappings):
                issues.append(
                    ValidationIssue(
                        str(record.path),
                        "duplicate-materialized-output",
                        duplicate,
                        artifact_id,
                    )
                )
            for mapping in mappings:
                output_id = mapping.get("local_id")
                artifact_ref = mapping.get("artifact_ref", {})
                if output_id not in known_outputs:
                    issues.append(
                        ValidationIssue(
                            str(record.path),
                            "unknown-materialized-output",
                            str(output_id),
                            artifact_id,
                        )
                    )
                elif artifact_ref.get("kind") != output_kinds.get(output_id):
                    issues.append(
                        ValidationIssue(
                            str(record.path),
                            "materialized-output-kind",
                            f"{output_id} expects {output_kinds.get(output_id)}, got {artifact_ref.get('kind')}",
                            artifact_id,
                        )
                    )

            for package in packages:
                package_output_ids = {
                    output["local_id"] for output in package.get("planned_outputs", [])
                }
                misplaced = sorted(
                    mapping["local_id"]
                    for mapping in package.get("materialized_outputs", [])
                    if mapping["local_id"] not in package_output_ids
                )
                if misplaced:
                    issues.append(
                        ValidationIssue(
                            str(record.path),
                            "misplaced-materialized-output",
                            f"work package {package['id']} contains mappings owned by another package: {', '.join(misplaced)}",
                            artifact_id,
                        )
                    )

            mapping_by_output = {
                mapping["local_id"]: mapping["artifact_ref"] for mapping in mappings
            }
            for output_id, output in (
                (output["local_id"], output) for output in outputs
            ):
                if output.get("kind") != "Experiment" or output_id not in mapping_by_output:
                    continue
                experiment = index.get(mapping_by_output[output_id]["id"])
                planned_claim_ids = {
                    mapping_by_output[dependency]["id"]
                    for dependency in output.get("depends_on_outputs", [])
                    if output_kinds.get(dependency) == "ScientificClaim"
                    and dependency in mapping_by_output
                }
                actual_claim_id = (
                    experiment.data.get("hypothesis", {}).get("claim_ref", {}).get("id")
                    if experiment is not None and experiment.kind == "Experiment"
                    else None
                )
                if planned_claim_ids and actual_claim_id not in planned_claim_ids:
                    issues.append(
                        ValidationIssue(
                            str(record.path),
                            "experiment-claim-binding",
                            f"{output_id} tests {actual_claim_id}, expected one of {', '.join(sorted(planned_claim_ids))}",
                            artifact_id,
                        )
                    )
            completion_statuses = {
                "LiteratureEvidence": {"ASSESSED", "VERIFIED"},
                "ScientificClaim": {"SUPPORTED", "VERIFIED", "IN_PAPER"},
                "Experiment": {"COMPLETED"},
                "Review": {"RESOLVED", "WAIVED"},
                "Decision": {"ACCEPTED"},
            }
            for package in packages:
                if package.get("status") != "DONE":
                    continue
                package_mappings = {
                    mapping["local_id"]: mapping["artifact_ref"]
                    for mapping in package.get("materialized_outputs", [])
                }
                missing = sorted(
                    output["local_id"]
                    for output in package.get("planned_outputs", [])
                    if output.get("required")
                    and output["local_id"] not in package_mappings
                )
                if missing:
                    issues.append(
                        ValidationIssue(
                            str(record.path),
                            "unfinished-required-output",
                            f"DONE work package {package['id']} has unmaterialized outputs: {', '.join(missing)}",
                            artifact_id,
                        )
                    )
                for output in package.get("planned_outputs", []):
                    if (
                        not output.get("required")
                        or output["local_id"] not in package_mappings
                    ):
                        continue
                    ref = package_mappings[output["local_id"]]
                    target = index.get(ref["id"])
                    allowed = completion_statuses.get(output["kind"], set())
                    if (
                        output.get("kind") == "ScientificClaim"
                        and output.get("verification_profile") == "EMPIRICAL"
                    ):
                        # The terminal scientific result belongs to the linked
                        # Experiment.  A preregistered empirical hypothesis may
                        # remain FORMALIZED when the package completes; this is
                        # strictly safer than marking it SUPPORTED/VERIFIED
                        # before synthesis evaluates the experiment evidence.
                        allowed = {*allowed, "FORMALIZED", "INVALIDATED"}
                    if target is not None and target.data.get("status") not in allowed:
                        issues.append(
                            ValidationIssue(
                                str(record.path),
                                "unfinished-materialized-output",
                                f"DONE work package {package['id']} output {output['local_id']} is {target.data.get('status')}",
                                artifact_id,
                            )
                        )
        return issues

    def _validate_claim_evidence(
        self, index: Mapping[str, ArtifactRecord], valid: set[str]
    ) -> list[ValidationIssue]:
        """Require accepted scientific evidence to be immutable and revision-pinned."""

        issues: list[ValidationIssue] = []
        for artifact_id in sorted(valid):
            record = index[artifact_id]
            if record.kind != "ScientificClaim" or record.data.get("status") not in {
                "SUPPORTED",
                "VERIFIED",
                "IN_PAPER",
            }:
                continue
            for position, evidence in enumerate(record.data.get("evidence", [])):
                source_ref = evidence.get("source_ref")
                source_resource = evidence.get("source_resource")
                if isinstance(source_ref, dict) and not isinstance(
                    source_ref.get("revision"), int
                ):
                    issues.append(
                        ValidationIssue(
                            str(record.path),
                            "unpinned-claim-evidence",
                            f"evidence[{position}] must pin source_ref.revision",
                            artifact_id,
                        )
                    )
                if isinstance(source_resource, dict) and not source_resource.get("sha256"):
                    issues.append(
                        ValidationIssue(
                            str(record.path),
                            "unhashed-claim-resource",
                            f"evidence[{position}] must pin source_resource.sha256",
                            artifact_id,
                        )
                    )
            for position, resource in enumerate(
                record.data.get("verification", {}).get("resource_refs", [])
            ):
                if not resource.get("sha256"):
                    issues.append(
                        ValidationIssue(
                            str(record.path),
                            "unhashed-verification-resource",
                            f"verification.resource_refs[{position}] must include sha256",
                            artifact_id,
                        )
                    )
            for position, review_ref in enumerate(
                record.data.get("verification", {}).get("review_refs", [])
            ):
                if not isinstance(review_ref.get("revision"), int):
                    issues.append(
                        ValidationIssue(
                            str(record.path),
                            "unpinned-verification-review",
                            f"verification.review_refs[{position}] must pin revision",
                            artifact_id,
                        )
                    )
        return issues

    def _validate_experiment_readiness(
        self,
        index: Mapping[str, ArtifactRecord],
        valid: set[str],
        invocation_overlay_ids: set[str] | None = None,
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        invocation_overlay_ids = invocation_overlay_ids or set()
        for artifact_id in sorted(valid):
            record = index[artifact_id]
            if record.kind != "Experiment" or record.data.get("status") not in {
                "READY",
                "RUNNING",
                "COMPLETED",
                "FAILED",
                "INVALIDATED",
            }:
                continue
            claim_ref = record.data.get("hypothesis", {}).get("claim_ref", {})
            if not isinstance(claim_ref.get("revision"), int):
                issues.append(
                    ValidationIssue(
                        str(record.path),
                        "unpinned-experiment-claim",
                        "READY or later Experiment must pin hypothesis.claim_ref.revision",
                        artifact_id,
                    )
                )
            protocol_lock = record.data.get("protocol_lock", {})
            interpretation = record.data.get("interpretation_plan", {})
            code_location = record.data.get("code_location", {})
            entrypoint = str(code_location.get("entrypoint", ""))
            parameters = record.data.get("configuration", {}).get("parameters", {})
            execution_argv = (
                parameters.get("execution_argv", [])
                if isinstance(parameters, Mapping)
                else []
            )
            # New invocation-policy checks are strict for a whole-workspace
            # audit and for the candidate artifacts in an overlay.  During an
            # unrelated transactional overlay, do not let a pre-existing
            # frozen protocol deadlock the very control event needed to retire
            # and replace it. Runtime execution still rejects that protocol.
            enforce_executable_argv = (
                record.data.get("status") not in {"FAILED", "INVALIDATED"}
                and (
                    not invocation_overlay_ids
                    or artifact_id in invocation_overlay_ids
                )
            )
            if enforce_executable_argv and (
                not isinstance(execution_argv, list)
                or not all(isinstance(item, str) for item in execution_argv)
            ):
                issues.append(
                    ValidationIssue(
                        str(record.path),
                        "invalid-execution-argv",
                        "configuration.parameters.execution_argv must be a list of strings",
                        artifact_id,
                    )
                )
            elif enforce_executable_argv:
                allowed_placeholders = {
                    "{run_output_dir}", "{repository}", "{workspace_root}"
                }
                embedded = [
                    item for item in execution_argv
                    if ("{" in item or "}" in item)
                    and item not in allowed_placeholders
                ]
                if embedded:
                    issues.append(
                        ValidationIssue(
                            str(record.path),
                            "embedded-execution-placeholder",
                            "execution_argv placeholders must be standalone tokens: "
                            + ", ".join(repr(item) for item in embedded),
                            artifact_id,
                        )
                    )
                if execution_argv:
                    first = Path(execution_argv[0]).name.lower()
                    entry_name = Path(entrypoint.split()[-1]).name.lower() if entrypoint else ""
                    if first.startswith("python") or (
                        entry_name and first == entry_name
                    ):
                        issues.append(
                            ValidationIssue(
                                str(record.path),
                                "duplicated-experiment-entrypoint",
                                "execution_argv contains only arguments after code_location.entrypoint; "
                                "do not repeat an interpreter or the entrypoint",
                                artifact_id,
                            )
                        )
            planned_seeds = set(interpretation.get("seeds", []))
            repetitions = int(interpretation.get("repetitions", 1))
            dataset_hashes = {
                dataset.get("sha256")
                for dataset in record.data.get("datasets", [])
                if dataset.get("sha256")
            }
            unhashed_datasets = [
                dataset.get("name", "<unnamed>")
                for dataset in record.data.get("datasets", [])
                if not dataset.get("sha256")
            ]
            if unhashed_datasets:
                issues.append(
                    ValidationIssue(
                        str(record.path),
                        "unhashed-experiment-dataset",
                        "READY or later Experiment datasets require sha256: "
                        + ", ".join(sorted(unhashed_datasets)),
                        artifact_id,
                    )
                )
            locked_fields = {
                key: record.data[key]
                for key in (
                    "hypothesis", "method", "baselines", "datasets", "metrics",
                    "configuration", "interpretation_plan", "code_location",
                )
            }
            protocol_sha256 = hashlib.sha256(
                json.dumps(
                    locked_fields,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if protocol_lock.get("sha256") != protocol_sha256:
                issues.append(
                    ValidationIssue(
                        str(record.path),
                        "protocol-lock-mismatch",
                        "protocol_lock.sha256 does not match the locked scientific fields; "
                        f"expected {protocol_sha256}, got {protocol_lock.get('sha256')!r}",
                        artifact_id,
                    )
                )
            parameters = record.data.get("configuration", {}).get("parameters", {})
            matrix = parameters.get("matrix", {}) if isinstance(parameters, dict) else {}
            matrix_seeds = matrix.get("seeds", []) if isinstance(matrix, dict) else []
            execution_argv = (
                parameters.get("execution_argv", [])
                if isinstance(parameters, dict) else []
            )
            # Some legacy locked protocols execute one deterministic matrix
            # driver whose *internal* scientific seeds are the preregistered
            # seeds.  The adapter still needs a stable wrapper seed for its run
            # ID, but that seed is not consumed by the driver.  Recognize only
            # this explicit, narrow shape; ordinary experiments continue to
            # require every run seed to be preregistered.
            legacy_matrix_wrapper_seed = (
                repetitions == 1
                and bool(planned_seeds)
                and isinstance(matrix_seeds, list)
                and set(matrix_seeds) == set(planned_seeds)
                and isinstance(execution_argv, list)
                and "{run_output_dir}" in {str(arg) for arg in execution_argv}
                and not any("{seed}" in str(arg) for arg in execution_argv)
            )
            for run in record.data.get("runs", []):
                if (
                    run.get("protocol_revision") != protocol_lock.get("revision")
                    or run.get("protocol_sha256") != protocol_lock.get("sha256")
                ):
                    issues.append(
                        ValidationIssue(
                            str(record.path),
                            "run-protocol-mismatch",
                            f"run {run.get('run_id')} does not pin the Experiment protocol lock",
                            artifact_id,
                        )
                    )
                if (
                    run.get("seed") not in planned_seeds
                    and not (legacy_matrix_wrapper_seed and run.get("seed") == 0)
                ):
                    issues.append(
                        ValidationIssue(
                            str(record.path),
                            "unregistered-run-seed",
                            f"run {run.get('run_id')} uses seed {run.get('seed')!r} outside the preregistration",
                            artifact_id,
                        )
                    )
                if not dataset_hashes.issubset(set(run.get("dataset_hashes", []))):
                    issues.append(
                        ValidationIssue(
                            str(record.path),
                            "run-dataset-mismatch",
                            f"run {run.get('run_id')} does not pin every preregistered dataset hash",
                            artifact_id,
                        )
                    )
                scientific = [
                    deviation
                    for deviation in run.get("deviations", [])
                    if deviation.get("category") == "SCIENTIFIC"
                ]
                if scientific and record.data.get("status") != "INVALIDATED":
                    issues.append(
                        ValidationIssue(
                            str(record.path),
                            "scientific-protocol-deviation",
                            f"run {run.get('run_id')} changed scientific protocol; create a replacement Experiment",
                            artifact_id,
                        )
                    )
            if record.data.get("status") == "COMPLETED":
                # A locked matrix driver is one outer execution whose inner
                # manifest owns the preregistered scientific seeds.  Once that
                # wrapper run is retained, requiring one adapter run per inner
                # seed is both impossible (the argv has no {seed}) and would
                # duplicate the frozen matrix.  Keep the exception identical
                # to the narrow seed-validation shape above; every ordinary
                # Experiment still requires its full seed/repetition census.
                wrapper_runs = list(record.data.get("runs", []))
                if legacy_matrix_wrapper_seed and wrapper_runs:
                    missing = []
                else:
                    counts = {
                        seed: sum(
                            1 for run in record.data.get("runs", [])
                            if run.get("seed") == seed
                        )
                        for seed in planned_seeds
                    }
                    missing = [
                        str(seed) for seed, count in counts.items()
                        if count < repetitions
                    ]
                if missing:
                    issues.append(
                        ValidationIssue(
                            str(record.path),
                            "missing-preregistered-runs",
                            "COMPLETED Experiment lacks required repetitions for seeds: "
                            + ", ".join(sorted(missing)),
                            artifact_id,
                        )
                    )
        return issues

    def _validate_claim_verification_profiles(
        self, index: Mapping[str, ArtifactRecord], valid: set[str]
    ) -> list[ValidationIssue]:
        """Require the Step 4 review set before a Claim can be VERIFIED."""

        profiles: dict[str, str] = {}
        for record in index.values():
            if record.id not in valid or record.kind != "ResearchPlan":
                continue
            for package in record.data.get("work_packages", []):
                planned = {
                    output["local_id"]: output
                    for output in package.get("planned_outputs", [])
                }
                for mapping in package.get("materialized_outputs", []):
                    ref = mapping.get("artifact_ref", {})
                    output = planned.get(mapping.get("local_id"), {})
                    if ref.get("kind") == "ScientificClaim":
                        profiles[str(ref.get("id"))] = output.get(
                            "verification_profile", "ADVERSARIAL"
                        )

        required = {
            "ADVERSARIAL": {"THEORY": {"PASS"}},
            "CORE_FORMAL": {
                "THEORY": {"PASS"},
                "LEAN": {"PASS"},
                "AXIOM_AUDIT": {"PASS"},
                "SEMANTIC_ALIGNMENT": {"PASS"},
            },
            "EMPIRICAL": {
                "EXPERIMENT": {"SUPPORTED"},
                "CONSISTENCY": {"PASS"},
            },
        }
        issues: list[ValidationIssue] = []
        for claim_id, profile in profiles.items():
            claim = index.get(claim_id)
            if (
                claim is None
                or claim.id not in valid
                or claim.data.get("status") not in {"VERIFIED", "IN_PAPER"}
            ):
                continue
            assessments: dict[str, set[str]] = {}
            target_revisions: set[int] = set()
            for ref in claim.data.get("verification", {}).get("review_refs", []):
                review = index.get(ref.get("id"))
                if review is None or review.kind != "Review":
                    continue
                target = review.data.get("target", {}).get("artifact_ref", {})
                assessment = review.data.get("assessment", {})
                scheme = str(assessment.get("scheme"))
                empirical_target_ok = False
                if profile == "EMPIRICAL" and scheme == "EXPERIMENT":
                    experiment = index.get(target.get("id"))
                    empirical_target_ok = bool(
                        experiment is not None
                        and experiment.kind == "Experiment"
                        and experiment.data.get("hypothesis", {}).get("claim_ref", {}).get("id") == claim_id
                        and isinstance(experiment.data.get("hypothesis", {}).get("claim_ref", {}).get("revision"), int)
                        and experiment.data.get("hypothesis", {}).get("claim_ref", {}).get("revision") < claim.data.get("revision")
                    )
                elif profile == "EMPIRICAL" and scheme == "CONSISTENCY":
                    empirical_target_ok = target.get("kind") == "Project" and target.get("id") == claim.data.get("project_id")
                if target.get("id") != claim_id and not empirical_target_ok:
                    issues.append(
                        ValidationIssue(
                            str(claim.path),
                            "verification-review-target",
                            f"{review.id} does not review {claim_id}",
                            claim_id,
                        )
                    )
                    continue
                if profile != "EMPIRICAL" and isinstance(target.get("revision"), int):
                    target_revisions.add(target["revision"])
                assessments.setdefault(str(assessment.get("scheme")), set()).add(
                    str(assessment.get("outcome"))
                )
            for scheme, outcomes in required.get(profile, {}).items():
                if not assessments.get(scheme, set()) & outcomes:
                    issues.append(
                        ValidationIssue(
                            str(claim.path),
                            "verification-profile",
                            f"{profile} requires {scheme} outcome in {sorted(outcomes)}",
                            claim_id,
                        )
                    )
            if profile != "EMPIRICAL" and len(target_revisions) != 1:
                issues.append(
                    ValidationIssue(
                        str(claim.path),
                        "verification-revision-mismatch",
                        "verification Reviews must target one frozen Claim revision",
                        claim_id,
                    )
                )
        return issues

    def _validate_reviews(
        self, index: Mapping[str, ArtifactRecord], valid: set[str]
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for artifact_id in sorted(valid):
            record = index[artifact_id]
            if record.kind != "Review":
                continue
            if record.data.get("status") in {"RESOLVED", "WAIVED"}:
                unresolved = [
                    issue.get("id")
                    for issue in record.data.get("issues", [])
                    if issue.get("status") in {"OPEN", "ACCEPTED"}
                ]
                if unresolved:
                    issues.append(ValidationIssue(str(record.path), "unresolved-review-issues", ", ".join(unresolved), artifact_id))
            assessment = record.data.get("assessment")
            if isinstance(assessment, dict):
                allowed_outcomes = {
                    "NOVELTY": {"OPEN", "PARTIAL", "KNOWN", "EQUIVALENT_KNOWN", "UNCERTAIN"},
                    "PROPOSAL_CORRECTNESS": {"PASS", "REVISION_REQUIRED", "INVALID"},
                    "THEORY": {"PASS", "PROOF_GAP", "ASSUMPTION_GAP", "FALSE_CLAIM", "RETHINK_CLAIM", "RETHINK_PLAN", "RETHINK_QUESTION"},
                    "LEAN": {"PASS", "LEAN_PROOF_GAP", "LIBRARY_GAP", "FORMALIZATION_GAP", "MATHEMATICAL_FAILURE"},
                    "AXIOM_AUDIT": {"PASS", "UNAPPROVED_AXIOM", "SORRY_FOUND"},
                    "SEMANTIC_ALIGNMENT": {"PASS", "MISMATCH"},
                    "EXPERIMENT": {"SUPPORTED", "CONTRADICTED", "INCONCLUSIVE"},
                    "COMPLETION": {"COMPLETE", "INCOMPLETE", "RETHINK_CLAIM", "RETHINK_PLAN", "RETHINK_QUESTION"},
                    "CONSISTENCY": {"PASS", "TARGETED_FOLLOWUP", "RETHINK_CLAIM", "RETHINK_PLAN", "RETHINK_QUESTION"},
                    "FINAL": {"PASS", "EDITORIAL_REVISION", "TARGETED_FOLLOWUP", "RETHINK_CLAIM", "RETHINK_PLAN", "RETHINK_QUESTION"},
                }
                scheme = assessment.get("scheme")
                if assessment.get("outcome") not in allowed_outcomes.get(scheme, set()):
                    issues.append(
                        ValidationIssue(
                            str(record.path),
                            "review-assessment-outcome",
                            f"{assessment.get('outcome')} is invalid for {scheme}",
                            artifact_id,
                        )
                    )
                resources = record.data.get("evidence_resources", [])
                if scheme in {"LEAN", "AXIOM_AUDIT"} and not resources:
                    issues.append(
                        ValidationIssue(
                            str(record.path),
                            "missing-formal-review-resource",
                            f"{scheme} Review must reference a build or audit resource",
                            artifact_id,
                        )
                    )
                for position, resource in enumerate(resources):
                    if not resource.get("sha256"):
                        issues.append(
                            ValidationIssue(
                                str(record.path),
                                "unhashed-review-resource",
                                f"evidence_resources[{position}] must include sha256",
                                artifact_id,
                            )
                        )
                    uri = resource.get("uri")
                    if not isinstance(uri, str):
                        continue
                    project_id = str(record.data.get("project_id", ""))
                    project_resources = (
                        self.root / "projects" / project_id / "resources"
                    ).resolve()
                    resource_path = (self.root / uri).resolve()
                    try:
                        resource_path.relative_to(project_resources)
                    except ValueError:
                        issues.append(
                            ValidationIssue(
                                str(record.path),
                                "review-resource-scope",
                                f"evidence_resources[{position}] must be inside the project's resources directory",
                                artifact_id,
                            )
                        )
                        continue
                    if not resource_path.is_file():
                        issues.append(
                            ValidationIssue(
                                str(record.path),
                                "missing-review-resource",
                                f"evidence_resources[{position}] does not exist: {uri}",
                                artifact_id,
                            )
                        )
                        continue
                    digest = hashlib.sha256(resource_path.read_bytes()).hexdigest()
                    if digest != resource.get("sha256"):
                        issues.append(
                            ValidationIssue(
                                str(record.path),
                                "review-resource-hash",
                                f"evidence_resources[{position}] sha256 does not match {uri}",
                                artifact_id,
                            )
                        )
        return issues

    def _validate_proposal_contracts(
        self, index: Mapping[str, ArtifactRecord], valid: set[str]
    ) -> list[ValidationIssue]:
        """Validate proposal-mode relationships that JSON Schema cannot express."""

        issues: list[ValidationIssue] = []
        projects = {
            artifact_id: record
            for artifact_id, record in index.items()
            if artifact_id in valid and record.kind == "Project"
        }

        for project_id, project in sorted(projects.items()):
            data = project.data
            if data.get("schema_version") not in {
                "research-artifact/v0.1.3",
                "research-artifact/v0.1.4",
            }:
                continue
            mode = data.get("workflow_mode")
            active = data.get("active_proposal")
            source = data.get("source_proposal_ref")
            if mode == "PROPOSAL_REVIEW":
                if not isinstance(active, Mapping) or source is not None:
                    issues.append(ValidationIssue(
                        str(project.path), "proposal-mode-contract",
                        "PROPOSAL_REVIEW requires active_proposal and forbids source_proposal_ref",
                        project_id,
                    ))
            elif mode == "FULL_RESEARCH":
                if active is not None:
                    issues.append(ValidationIssue(
                        str(project.path), "proposal-mode-contract",
                        "FULL_RESEARCH forbids active_proposal",
                        project_id,
                    ))
                if isinstance(source, Mapping):
                    proposal = index.get(str(source.get("id")))
                    if (
                        proposal is None
                        or proposal.kind != "ResearchProposal"
                        or proposal.data.get("status") != "APPROVED"
                        or source.get("revision") != proposal.data.get("revision")
                    ):
                        issues.append(ValidationIssue(
                            str(project.path), "invalid-source-proposal",
                            "source_proposal_ref must pin the current APPROVED proposal revision",
                            project_id,
                        ))
                if data.get("schema_version") == "research-artifact/v0.1.4":
                    self._validate_project_input_resources(project, issues)

        reviewer_for_scheme = {
            "NOVELTY": "LITERATURE",
            "PROPOSAL_CORRECTNESS": "PROPOSAL_CORRECTNESS",
        }
        for artifact_id in sorted(valid):
            record = index[artifact_id]
            if record.kind == "ResearchProposal":
                project = projects.get(str(record.data.get("project_id")))
                if project is None or project.data.get("workflow_mode") != "PROPOSAL_REVIEW":
                    issues.append(ValidationIssue(
                        str(record.path), "proposal-project-mode",
                        "ResearchProposal must belong to a PROPOSAL_REVIEW project",
                        artifact_id,
                    ))
                self._validate_proposal_resources(record, issues)
                for ref in record.data.get("question_refs", []):
                    target = index.get(str(ref.get("id")))
                    if target is not None and ref.get("revision") != target.data.get("revision"):
                        issues.append(ValidationIssue(
                            str(record.path), "stale-proposal-question",
                            f"question_refs must pin current revision: {ref.get('id')}", artifact_id,
                        ))
                plan_ref = record.data.get("plan_ref")
                if isinstance(plan_ref, Mapping):
                    plan = index.get(str(plan_ref.get("id")))
                    if plan is not None and plan_ref.get("revision") != plan.data.get("revision"):
                        issues.append(ValidationIssue(
                            str(record.path), "stale-proposal-plan",
                            "plan_ref must pin the current ResearchPlan revision", artifact_id,
                        ))
                review_target_revisions: set[int] = set()
                for ref in record.data.get("review_refs", []):
                    review = index.get(str(ref.get("id")))
                    target_ref = review.data.get("target", {}).get("artifact_ref", {}) if review else {}
                    if (
                        review is not None
                        and (
                            ref.get("revision") != review.data.get("revision")
                            or target_ref.get("id") != artifact_id
                        )
                    ):
                        issues.append(ValidationIssue(
                            str(record.path), "proposal-review-revision-mismatch",
                            f"review {ref.get('id')} must be current and target this proposal",
                            artifact_id,
                        ))
                    if isinstance(target_ref.get("revision"), int):
                        review_target_revisions.add(target_ref["revision"])
                if len(review_target_revisions) > 1:
                    issues.append(ValidationIssue(
                        str(record.path), "proposal-review-revision-mismatch",
                        "all current proposal Reviews must target the same content revision",
                        artifact_id,
                    ))
            elif record.kind == "Review" and record.data.get("schema_version") == "research-artifact/v0.1.3":
                scheme = record.data.get("assessment", {}).get("scheme")
                reviewer = record.data.get("reviewer", {}).get("reviewer_type")
                if reviewer_for_scheme.get(str(scheme)) != reviewer:
                    issues.append(ValidationIssue(
                        str(record.path), "proposal-reviewer-mismatch",
                        f"{scheme} requires reviewer_type {reviewer_for_scheme.get(str(scheme))}",
                        artifact_id,
                    ))
                target_ref = record.data.get("target", {}).get("artifact_ref", {})
                target = index.get(str(target_ref.get("id")))
                if target is None or target.kind != "ResearchProposal":
                    issues.append(ValidationIssue(
                        str(record.path), "proposal-review-target",
                        "v0.1.3 proposal Review must target a ResearchProposal",
                        artifact_id,
                    ))
                elif target_ref.get("revision") > target.data.get("revision", 0):
                    issues.append(ValidationIssue(
                        str(record.path), "proposal-review-target",
                        "Review cannot target a future proposal revision", artifact_id,
                    ))
        return issues

    def _validate_project_input_resources(
        self, project: ArtifactRecord, issues: list[ValidationIssue]
    ) -> None:
        """Require every v0.1.4 input to stay project-local and hash-pinned."""

        allowed = (
            self.root / "projects" / project.id / "resources" / "input"
        ).resolve()
        for position, resource in enumerate(project.data.get("input_resources", [])):
            uri = resource.get("uri")
            if not isinstance(uri, str):
                continue
            path = (self.root / uri).resolve()
            try:
                path.relative_to(allowed)
            except ValueError:
                issues.append(ValidationIssue(
                    str(project.path), "input-resource-scope",
                    f"input_resources[{position}] must be under {allowed.relative_to(self.root)}",
                    project.id,
                ))
                continue
            if not path.is_file():
                issues.append(ValidationIssue(
                    str(project.path), "missing-input-resource",
                    f"input_resources[{position}] does not exist: {uri}", project.id,
                ))
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != resource.get("sha256"):
                issues.append(ValidationIssue(
                    str(project.path), "input-resource-hash",
                    f"input_resources[{position}] sha256 does not match {uri}", project.id,
                ))

    def _validate_proposal_resources(
        self, proposal: ArtifactRecord, issues: list[ValidationIssue]
    ) -> None:
        resources: list[tuple[str, Mapping[str, Any]]] = []
        for field in ("source_document", "current_document", "rubric_source_document", "rubric_document", "source_map"):
            value = proposal.data.get(field)
            if isinstance(value, Mapping):
                resources.append((field, value))
        resources.extend(
            (f"change_log_refs[{position}]", value)
            for position, value in enumerate(proposal.data.get("change_log_refs", []))
            if isinstance(value, Mapping)
        )
        project_resources = (self.root / "projects" / str(proposal.data.get("project_id")) / "resources").resolve()
        for field, resource in resources:
            uri = resource.get("uri")
            if not isinstance(uri, str):
                continue
            path = (self.root / uri).resolve()
            try:
                path.relative_to(project_resources)
            except ValueError:
                issues.append(ValidationIssue(
                    str(proposal.path), "proposal-resource-scope",
                    f"{field} must be inside the project's resources directory", proposal.id,
                ))
                continue
            if not path.is_file():
                issues.append(ValidationIssue(
                    str(proposal.path), "missing-proposal-resource",
                    f"{field} does not exist: {uri}", proposal.id,
                ))
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != resource.get("sha256"):
                issues.append(ValidationIssue(
                    str(proposal.path), "proposal-resource-hash",
                    f"{field} sha256 does not match {uri}", proposal.id,
                ))

    def _validate_project_gates(
        self, index: Mapping[str, ArtifactRecord], valid: set[str]
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        for project_id in sorted(valid):
            project = index[project_id]
            if project.kind != "Project":
                continue
            data = project.data
            stage = data.get("stage")
            members = [
                record
                for record in index.values()
                if record.id in valid and record.data.get("project_id") == project_id
            ]
            active_questions = [record for record in members if record.kind == "ResearchQuestion" and record.data.get("status") == "ACTIVE"]
            if data.get("workflow_mode") == "PROPOSAL_REVIEW":
                proposal_ref = data.get("active_proposal")
                proposal = index.get(proposal_ref.get("id")) if isinstance(proposal_ref, dict) else None
                if proposal is None or proposal.kind != "ResearchProposal":
                    issues.append(ValidationIssue(str(project.path), "proposal-gate", "proposal review project requires an active ResearchProposal", project_id))
                if stage in {"LITERATURE", "PLANNING", "REVIEW", "WRITING", "COMPLETE"} and not active_questions:
                    issues.append(ValidationIssue(str(project.path), "proposal-gate", "structured proposal requires an ACTIVE ResearchQuestion", project_id))
                if stage in {"PLANNING", "REVIEW", "WRITING", "COMPLETE"} and not any(
                    record.kind == "LiteratureEvidence" and record.data.get("status") in {"ASSESSED", "VERIFIED"}
                    for record in members
                ):
                    issues.append(ValidationIssue(str(project.path), "proposal-gate", "proposal planning or later requires assessed literature", project_id))
                if stage == "COMPLETE" and (
                    proposal is None or proposal.data.get("status") != "APPROVED"
                ):
                    issues.append(ValidationIssue(str(project.path), "proposal-gate", "completed proposal review requires an APPROVED proposal", project_id))
                if data.get("status") == "COMPLETED" and stage != "COMPLETE":
                    issues.append(ValidationIssue(str(project.path), "proposal-gate", "Project status COMPLETED requires stage COMPLETE", project_id))
                continue
            if stage in {"LITERATURE", "PLANNING", "THEORY_EXPERIMENT", "REVIEW", "WRITING", "COMPLETE"} and not active_questions:
                issues.append(ValidationIssue(str(project.path), "stage-gate", "LITERATURE or later requires an ACTIVE ResearchQuestion", project_id))

            if stage in {"PLANNING", "THEORY_EXPERIMENT", "REVIEW", "WRITING", "COMPLETE"}:
                literature = [record for record in members if record.kind == "LiteratureEvidence" and record.data.get("status") in {"ASSESSED", "VERIFIED"}]
                documented_exception = any(
                    record.kind == "Decision"
                    and record.data.get("status") == "ACCEPTED"
                    and any(ref.get("id") in {question.id for question in active_questions} for ref in record.data.get("evidence_refs", []))
                    for record in members
                )
                if not literature and not documented_exception:
                    issues.append(ValidationIssue(str(project.path), "stage-gate", "PLANNING or later requires assessed literature or an accepted documented exception", project_id))

            active_plan_ref = data.get("active_plan")
            active_plan = index.get(active_plan_ref.get("id")) if isinstance(active_plan_ref, dict) else None
            if stage in {"THEORY_EXPERIMENT", "REVIEW", "WRITING", "COMPLETE"}:
                if active_plan is None or active_plan.data.get("status") not in {"APPROVED", "IN_PROGRESS", "COMPLETED"}:
                    issues.append(ValidationIssue(str(project.path), "stage-gate", "THEORY_EXPERIMENT or later requires an approved active plan", project_id))

            if stage in {"REVIEW", "WRITING", "COMPLETE"}:
                has_result = any(
                    (record.kind == "ScientificClaim" and record.data.get("status") in {"FORMALIZED", "SUPPORTED", "VERIFIED", "IN_PAPER"})
                    or (record.kind == "Experiment" and record.data.get("status") == "COMPLETED")
                    for record in members
                )
                if not has_result:
                    issues.append(ValidationIssue(str(project.path), "stage-gate", "REVIEW or later requires a formalized claim or completed experiment", project_id))

            if stage in {"WRITING", "COMPLETE"} and active_plan is not None:
                planned_claim_ids = [
                    mapping["artifact_ref"]["id"]
                    for package in active_plan.data.get("work_packages", [])
                    for mapping in package.get("materialized_outputs", [])
                    if mapping.get("artifact_ref", {}).get("kind") == "ScientificClaim"
                ]
                nonverified = [
                    claim_id
                    for claim_id in planned_claim_ids
                    if index.get(claim_id) is None or index[claim_id].data.get("status") not in {"VERIFIED", "IN_PAPER"}
                ]
                if nonverified:
                    issues.append(ValidationIssue(str(project.path), "stage-gate", f"WRITING requires verified planned claims: {', '.join(nonverified)}", project_id))
                blocker_targets = {
                    record.data["target"]["artifact_ref"]["id"]
                    for record in members
                    if record.kind == "Review"
                    for issue in record.data.get("issues", [])
                    if issue.get("severity") == "BLOCKER" and issue.get("status") in {"OPEN", "ACCEPTED"}
                }
                blocked = sorted(set(planned_claim_ids) & blocker_targets)
                if blocked:
                    issues.append(ValidationIssue(str(project.path), "stage-gate", f"WRITING has unresolved BLOCKER reviews: {', '.join(blocked)}", project_id))

            if stage == "COMPLETE":
                if not any(item.get("status") == "FINAL" for item in data.get("deliverables", [])):
                    issues.append(ValidationIssue(str(project.path), "stage-gate", "COMPLETE requires at least one FINAL deliverable", project_id))
                open_reviews = [
                    record.id
                    for record in members
                    if record.kind == "Review" and record.data.get("status") not in {"RESOLVED", "WAIVED"}
                ]
                if open_reviews:
                    issues.append(ValidationIssue(str(project.path), "stage-gate", f"COMPLETE has open reviews: {', '.join(open_reviews)}", project_id))
            if data.get("status") == "COMPLETED" and stage != "COMPLETE":
                issues.append(ValidationIssue(str(project.path), "stage-gate", "Project status COMPLETED requires stage COMPLETE", project_id))
        return issues

    def graph_edges(self) -> list[dict[str, Any]]:
        """Return the computed forward-reference graph."""

        index = self.index()
        edges: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, int | None]] = set()
        for source_id, record in sorted(index.items()):
            for ref in iter_artifact_refs(record.data):
                key = (source_id, ref["id"], ref["kind"], ref.get("revision"))
                if key in seen:
                    continue
                seen.add(key)
                edges.append(
                    {
                        "source": source_id,
                        "target": ref["id"],
                        "target_kind": ref["kind"],
                        "target_revision": ref.get("revision"),
                    }
                )
        return edges

    def mermaid_graph(self) -> str:
        """Render the computed artifact-reference graph as Mermaid."""

        index = self.index()
        lines = ["flowchart LR"]
        for artifact_id, record in sorted(index.items()):
            node = re.sub(r"[^A-Za-z0-9_]", "_", artifact_id)
            label = f"{artifact_id}<br/>{record.kind}"
            lines.append(f'    {node}["{label}"]')
        for edge in self.graph_edges():
            source = re.sub(r"[^A-Za-z0-9_]", "_", edge["source"])
            target = re.sub(r"[^A-Za-z0-9_]", "_", edge["target"])
            lines.append(f"    {source} --> {target}")
        return "\n".join(lines) + "\n"

    def transition(
        self,
        artifact_id: str,
        *,
        expected_revision: int,
        actor_type: str,
        actor_id: str,
        new_status: str | None = None,
        new_stage: str | None = None,
        session_id: str | None = None,
    ) -> ArtifactRecord:
        """Atomically transition one artifact using optimistic concurrency."""

        if (new_status is None) == (new_stage is None):
            raise ArtifactError("provide exactly one of new_status or new_stage")
        if actor_type not in {"human", "agent"}:
            raise ArtifactError("actor_type must be human or agent")

        record = self.get(artifact_id)
        current_revision = record.data.get("revision")
        if current_revision != expected_revision:
            raise RevisionConflict(
                f"{artifact_id} revision is {current_revision}, expected {expected_revision}"
            )

        candidate = copy.deepcopy(record.data)
        if new_status is not None:
            current_status = str(candidate.get("status"))
            if not transition_allowed(record.kind, current_status, new_status):
                raise ArtifactError(
                    f"invalid {record.kind} status transition: {current_status} -> {new_status}"
                )
            candidate["status"] = new_status
        else:
            if record.kind != "Project":
                raise ArtifactError("only Project artifacts have a stage")
            current_stage = str(candidate.get("stage"))
            if not stage_transition_allowed(current_stage, str(new_stage)):
                raise ArtifactError(
                    f"invalid Project stage transition: {current_stage} -> {new_stage}"
                )
            candidate["stage"] = new_stage

        candidate["revision"] = expected_revision + 1
        candidate["updated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        actor: dict[str, str] = {"actor_type": actor_type, "actor_id": actor_id}
        if session_id:
            actor["session_id"] = session_id
        candidate["provenance"]["updated_by"] = actor

        issues = self.validate(overrides={artifact_id: candidate})
        if issues:
            raise ArtifactValidationError(issues)

        absolute_path = self.root / record.path
        serialized = yaml.safe_dump(candidate, sort_keys=False, allow_unicode=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{absolute_path.name}.", dir=absolute_path.parent, text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(serialized)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, absolute_path)
        except Exception:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
            raise
        return ArtifactRecord(record.path, candidate)


def _has_cycle(graph: Mapping[str, Iterable[str]]) -> bool:
    state: dict[str, int] = {}

    def visit(node: str) -> bool:
        state[node] = 1
        for target in graph.get(node, []):
            if target not in graph:
                continue
            if state.get(target) == 1:
                return True
            if state.get(target, 0) == 0 and visit(target):
                return True
        state[node] = 2
        return False

    return any(state.get(node, 0) == 0 and visit(node) for node in graph)
