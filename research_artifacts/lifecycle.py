"""Lifecycle declarations for Artifact Schema v0.1.2."""

from __future__ import annotations

from collections.abc import Mapping, Set


STATUS_TRANSITIONS: Mapping[str, Mapping[str, Set[str]]] = {
    "Project": {
        "ACTIVE": {"BLOCKED", "PAUSED", "COMPLETED", "ARCHIVED"},
        "BLOCKED": {"ACTIVE", "PAUSED", "ARCHIVED"},
        "PAUSED": {"ACTIVE", "ARCHIVED"},
        "COMPLETED": {"ARCHIVED"},
        "ARCHIVED": set(),
    },
    "ResearchQuestion": {
        "DRAFT": {"ACTIVE", "CLOSED"},
        "ACTIVE": {"ANSWERED", "REFRAMED", "CLOSED"},
        "ANSWERED": {"CLOSED"},
        "REFRAMED": set(),
        "CLOSED": set(),
    },
    "ResearchPlan": {
        "DRAFT": {"APPROVED", "SUPERSEDED"},
        "APPROVED": {"IN_PROGRESS", "BLOCKED", "SUPERSEDED"},
        "IN_PROGRESS": {"BLOCKED", "COMPLETED", "SUPERSEDED"},
        "BLOCKED": {"IN_PROGRESS", "SUPERSEDED"},
        "COMPLETED": {"SUPERSEDED"},
        "SUPERSEDED": set(),
    },
    "LiteratureEvidence": {
        "CANDIDATE": {"ASSESSED", "DISMISSED", "RETRACTED"},
        "ASSESSED": {"VERIFIED", "DISMISSED", "RETRACTED"},
        "VERIFIED": {"DISMISSED", "RETRACTED"},
        "DISMISSED": set(),
        "RETRACTED": set(),
    },
    "ScientificClaim": {
        # One accepted theory-development proposal may both formalize a Claim
        # and attach its initial support; there is no separate global state.
        "IDEA": {"FORMALIZED", "SUPPORTED"},
        "FORMALIZED": {"SUPPORTED", "FAILED", "INVALIDATED"},
        "SUPPORTED": {"FORMALIZED", "VERIFIED", "FAILED", "INVALIDATED"},
        "VERIFIED": {"SUPPORTED", "IN_PAPER", "INVALIDATED"},
        "IN_PAPER": {"VERIFIED", "INVALIDATED"},
        "FAILED": set(),
        "INVALIDATED": set(),
    },
    "Experiment": {
        # experiment-design owns preregistration and may atomically produce the
        # first READY protocol from a materialized DRAFT shell.
        "DRAFT": {"PLANNED", "READY"},
        "PLANNED": {"READY", "FAILED"},
        "READY": {"RUNNING", "FAILED"},
        "RUNNING": {"COMPLETED", "FAILED"},
        "COMPLETED": {"INVALIDATED"},
        "FAILED": {"READY"},
        "INVALIDATED": set(),
    },
    "Review": {
        "OPEN": {"IN_RESOLUTION", "RESOLVED", "WAIVED"},
        "IN_RESOLUTION": {"RESOLVED", "WAIVED"},
        "RESOLVED": set(),
        "WAIVED": set(),
    },
    "Decision": {
        "PROPOSED": {"ACCEPTED", "REJECTED"},
        "ACCEPTED": {"SUPERSEDED", "REVERSED"},
        "REJECTED": set(),
        "SUPERSEDED": set(),
        "REVERSED": set(),
    },
    "ResearchProposal": {
        "IMPORTED": {"STRUCTURED", "REJECTED"},
        "STRUCTURED": {"UNDER_REVIEW", "REVISION_REQUIRED", "READY_FOR_APPROVAL", "REJECTED"},
        "UNDER_REVIEW": {"REVISION_REQUIRED", "READY_FOR_APPROVAL", "REJECTED"},
        "REVISION_REQUIRED": {"STRUCTURED", "UNDER_REVIEW", "REJECTED"},
        "READY_FOR_APPROVAL": {"REVISION_REQUIRED", "APPROVED", "REJECTED"},
        "APPROVED": set(),
        "REJECTED": set(),
    },
}


PROJECT_STAGE_TRANSITIONS: Mapping[str, Set[str]] = {
    "QUESTION": {"LITERATURE"},
    "LITERATURE": {"PLANNING"},
    "PLANNING": {"THEORY_EXPERIMENT"},
    "THEORY_EXPERIMENT": {"REVIEW"},
    "REVIEW": {"QUESTION", "LITERATURE", "PLANNING", "THEORY_EXPERIMENT", "WRITING"},
    "WRITING": {"QUESTION", "REVIEW", "PLANNING", "THEORY_EXPERIMENT", "COMPLETE"},
    "COMPLETE": set(),
}


def transition_allowed(kind: str, current: str, target: str) -> bool:
    """Return whether a status transition is declared by v0.1.2."""

    return target == current or target in STATUS_TRANSITIONS.get(kind, {}).get(current, set())


def stage_transition_allowed(current: str, target: str) -> bool:
    """Return whether a project-stage transition is declared by v0.1.2."""

    return target == current or target in PROJECT_STAGE_TRANSITIONS.get(current, set())
