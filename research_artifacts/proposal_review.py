"""Deterministic proposal import and human-decision contracts."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

import yaml

from .runtime_types import RuntimeValidationError, atomic_write_text


SUPPORTED_PROPOSAL_SUFFIXES = {".md", ".markdown", ".pdf"}
DECISION_ACTIONS = {"ACCEPT", "REJECT", "EDIT"}
MAX_INPUT_BYTES = 25_000_000
MAX_NORMALIZED_CHARS = 5_000_000


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary, target)
    except Exception:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


def _pdf_markdown(path: Path) -> str:
    executable = shutil.which("pdftotext")
    info_executable = shutil.which("pdfinfo")
    if executable is None or info_executable is None:
        raise RuntimeValidationError(
            "PDF import requires pdftotext and pdfinfo (Poppler); provide Markdown when unavailable"
        )
    locale_env = dict(os.environ)
    locale_env["LC_ALL"] = "C"
    try:
        info = subprocess.run(
            [info_executable, str(path)], text=True, capture_output=True,
            check=False, timeout=30, env=locale_env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeValidationError(
            "PDF metadata inspection failed; provide a text-layer PDF or Markdown"
        ) from exc
    if info.returncode != 0:
        detail = info.stderr.strip() or "PDF metadata inspection failed"
        raise RuntimeValidationError(
            f"cannot import PDF: {detail}; provide a text-layer PDF or Markdown"
        )
    metadata = {
        key.strip().lower(): value.strip()
        for line in info.stdout.splitlines() if ":" in line
        for key, value in [line.split(":", 1)]
    }
    if metadata.get("encrypted", "no").lower().startswith("yes"):
        raise RuntimeValidationError("encrypted PDFs are not accepted; provide decrypted Markdown")
    try:
        expected_pages = int(metadata["pages"])
    except (KeyError, ValueError) as exc:
        raise RuntimeValidationError("PDF page count is unavailable; provide Markdown") from exc
    if expected_pages < 1:
        raise RuntimeValidationError("PDF has no pages; provide Markdown")
    try:
        completed = subprocess.run(
            [executable, "-layout", "-enc", "UTF-8", str(path), "-"],
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
            env=locale_env,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeValidationError(
            "PDF text extraction failed; provide a text-layer PDF or Markdown"
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or "PDF text extraction failed"
        raise RuntimeValidationError(
            f"cannot import PDF: {detail}; provide a text-layer PDF or Markdown"
        )
    pages = completed.stdout.split("\f")
    while pages and not pages[-1].strip():
        pages.pop()
    if len(pages) != expected_pages:
        raise RuntimeValidationError(
            "PDF text extraction did not preserve every page; provide Markdown"
        )
    usable = [page for page in pages if len(re.sub(r"\s+", "", page)) >= 40]
    total_characters = sum(len(re.sub(r"\s+", "", page)) for page in pages)
    if not pages or total_characters < 200 or len(usable) / len(pages) < 0.70:
        raise RuntimeValidationError(
            "PDF has no reliable text layer; OCR is intentionally disabled, provide Markdown"
        )
    if len(completed.stdout) > MAX_NORMALIZED_CHARS:
        raise RuntimeValidationError("PDF extracted text exceeds the proposal size limit")
    rendered = []
    for number, page in enumerate(pages, 1):
        rendered.append(f"<!-- proposal-page: {number} -->\n\n{page.strip()}\n")
    return "\n".join(rendered).strip() + "\n"


def _read_normalized(path: Path) -> str:
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise RuntimeValidationError("proposal or rubric exceeds the 25 MB input limit")
    if path.suffix.lower() == ".pdf":
        return _pdf_markdown(path)
    try:
        value = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeValidationError("proposal Markdown must be UTF-8") from exc
    if len(re.sub(r"\s+", "", value)) < 50:
        raise RuntimeValidationError("proposal document is too short to review")
    if len(value) > MAX_NORMALIZED_CHARS:
        raise RuntimeValidationError("proposal or rubric exceeds the normalized text size limit")
    return value.rstrip() + "\n"


def _media_type(path: Path) -> str:
    return "application/pdf" if path.suffix.lower() == ".pdf" else "text/markdown"


def _resource(root: Path, path: Path, description: str) -> dict[str, str]:
    return {
        "uri": str(path.relative_to(root)),
        "sha256": _sha256(path),
        "media_type": _media_type(path),
        "description": description,
    }


def _language(text: str) -> str:
    cjk = len(re.findall(r"[\u3400-\u9fff]", text))
    letters = len(re.findall(r"[A-Za-z]", text))
    return "zh" if cjk > letters * 0.15 else "en"


def section_index(text: str) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    page = 1
    used: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), 1):
        page_match = re.fullmatch(r"<!-- proposal-page: (\d+) -->", line.strip())
        if page_match:
            page = int(page_match.group(1))
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        if not heading:
            continue
        title = heading.group(1).strip()
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or f"section-{len(sections)+1}"
        base = slug[:48]
        slug = base
        counter = 2
        while slug in used:
            slug = f"{base}-{counter}"
            counter += 1
        used.add(slug)
        sections.append({"id": slug, "title": title, "locator": f"page {page}, line {line_number}"})
    if not sections:
        sections.append({"id": "document", "title": "Proposal", "locator": "page 1, line 1"})
    return sections


def import_proposal_resources(
    root: Path,
    project_id: str,
    proposal_path: str | Path,
    rubric_path: str | Path | None = None,
) -> dict[str, Any]:
    """Copy immutable inputs and create a normalized Markdown revision."""

    if not re.fullmatch(r"proj-[a-z0-9][a-z0-9-]*", project_id):
        raise RuntimeValidationError("invalid proposal project_id")
    source = Path(proposal_path).expanduser().resolve(strict=True)
    if not source.is_file() or source.suffix.lower() not in SUPPORTED_PROPOSAL_SUFFIXES:
        raise RuntimeValidationError("proposal must be a .md, .markdown, or .pdf file")
    project_root = root / "projects" / project_id
    resource_root = project_root / "resources" / "proposal"
    source_target = resource_root / "source" / f"original{source.suffix.lower()}"
    revision_target = resource_root / "revisions" / "rev-001.md"
    if source_target.exists() or revision_target.exists():
        raise RuntimeValidationError(f"proposal resources already exist for {project_id}")
    normalized = _read_normalized(source)
    rubric = None
    rubric_normalized = None
    if rubric_path is not None:
        rubric = Path(rubric_path).expanduser().resolve(strict=True)
        if not rubric.is_file() or rubric.suffix.lower() not in SUPPORTED_PROPOSAL_SUFFIXES:
            raise RuntimeValidationError("rubric must be a .md, .markdown, or .pdf file")
        rubric_normalized = _read_normalized(rubric)
    rubric_resource = None
    rubric_source_resource = None
    rubric_target = None
    rubric_text_target = None
    created: list[Path] = []
    try:
        _atomic_copy(source, source_target)
        created.append(source_target)
        atomic_write_text(revision_target, normalized)
        created.append(revision_target)
        if rubric is not None:
            rubric_target = resource_root / "rubric" / f"rubric{rubric.suffix.lower()}"
            _atomic_copy(rubric, rubric_target)
            created.append(rubric_target)
            rubric_source_resource = _resource(root, rubric_target, "Immutable original proposal review rubric")
            rubric_text_target = resource_root / "rubric" / "rubric.md"
            atomic_write_text(rubric_text_target, str(rubric_normalized))
            created.append(rubric_text_target)
            rubric_resource = _resource(root, rubric_text_target, "Normalized proposal review rubric")
    except Exception:
        for path in reversed(created):
            if path.exists():
                path.unlink()
        raise

    return {
        "source_document": _resource(root, source_target, "Immutable original proposal"),
        "current_document": _resource(root, revision_target, "Normalized proposal revision 1"),
        "rubric_document": rubric_resource,
        "rubric_source_document": rubric_source_resource,
        "language": _language(normalized),
        "section_index": section_index(normalized),
        "created_paths": created,
    }


def load_decisions(path: str | Path, expected_project: str) -> dict[str, Any]:
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, Mapping):
        raise RuntimeValidationError("proposal decisions must be a YAML mapping")
    if value.get("project_id") != expected_project:
        raise RuntimeValidationError("proposal decisions project_id does not match")
    decisions = value.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise RuntimeValidationError("proposal decisions must contain a non-empty decisions list")
    seen: set[str] = set()
    for item in decisions:
        if not isinstance(item, Mapping):
            raise RuntimeValidationError("each proposal decision must be a mapping")
        issue_id = str(item.get("issue_id", ""))
        action = str(item.get("action", ""))
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", issue_id) or issue_id in seen:
            raise RuntimeValidationError(f"invalid or duplicate issue_id: {issue_id!r}")
        seen.add(issue_id)
        if action not in DECISION_ACTIONS:
            raise RuntimeValidationError(f"invalid decision action for {issue_id}: {action!r}")
        if action == "REJECT" and not str(item.get("reason", "")).strip():
            raise RuntimeValidationError(f"REJECT requires reason for {issue_id}")
        if action == "EDIT" and not str(item.get("instruction", "")).strip():
            raise RuntimeValidationError(f"EDIT requires instruction for {issue_id}")
    return dict(value)
