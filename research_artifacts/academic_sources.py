"""Fail-closed, auditable resource and academic-source tools."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .runtime_types import RuntimeValidationError, atomic_write_json, atomic_write_text
from .workspace import ArtifactWorkspace


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class _PublicHTTPSRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject a redirect before urllib can issue a request to a private target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        AcademicSourceTools._validate_public_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class AcademicSourceTools:
    """Bounded HTTP discovery plus immutable evidence capture."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.workspace = ArtifactWorkspace(self.root)

    def resource_read(
        self, *, project_id: str, uri: str, action_id: str | None = None,
        offset_chars: int = 0, max_chars: int = 100_000,
    ) -> dict[str, Any]:
        self._validate_scope(project_id, "resource-read")
        if int(offset_chars) < 0:
            raise RuntimeValidationError("offset_chars must be non-negative")
        if not 1 <= int(max_chars) <= 1_000_000:
            raise RuntimeValidationError("max_chars must be between 1 and 1,000,000")
        offset_chars = int(offset_chars)
        max_chars = int(max_chars)
        resources = (self.root / "projects" / project_id / "resources").resolve()
        path = (self.root / uri).resolve()
        try:
            path.relative_to(resources)
        except ValueError as exc:
            raise RuntimeValidationError("resource_read is restricted to the current project's resources") from exc
        if not path.is_file():
            raise RuntimeValidationError(f"resource does not exist: {uri}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        referenced = False

        def visit(value: Any) -> None:
            nonlocal referenced
            if isinstance(value, Mapping):
                if value.get("uri") == uri and value.get("sha256") == digest:
                    referenced = True
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        for record in self.workspace.query(project_id=project_id):
            visit(record.data)
        action_resource = False
        if action_id is not None:
            self._validate_scope(project_id, action_id)
            action_directory = (
                self.root / "projects" / project_id / "resources" / "academic" / action_id
            ).resolve()
            try:
                path.relative_to(action_directory)
                action_resource = True
            except ValueError:
                pass
        if not referenced and not action_resource:
            raise RuntimeValidationError("resource is not pinned by a current project artifact")
        raw = path.read_bytes()
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            return {
                "uri": uri, "sha256": digest, "size": len(raw), "content": None,
                "offset_chars": offset_chars, "next_offset_chars": None,
                "total_chars": None, "truncated": False,
            }
        total_chars = len(decoded)
        end = min(offset_chars + max_chars, total_chars)
        content = decoded[offset_chars:end]
        truncated = end < total_chars
        return {
            "uri": uri, "sha256": digest, "size": len(raw), "content": content,
            "offset_chars": offset_chars,
            "next_offset_chars": end if truncated else None,
            "total_chars": total_chars,
            "truncated": truncated,
        }

    def manuscript_read(
        self, *, project_id: str, action_id: str, relative_path: str,
        offset_chars: int = 0, max_chars: int = 100_000,
    ) -> dict[str, Any]:
        """Read a bounded, frozen-paper file without exposing the workspace broadly."""

        self._validate_scope(project_id, action_id)
        path, normalized = self._paper_path(project_id, relative_path)
        if not path.is_file():
            raise RuntimeValidationError(f"manuscript file does not exist: paper/{normalized}")
        if int(offset_chars) < 0:
            raise RuntimeValidationError("offset_chars must be non-negative")
        if not 1 <= int(max_chars) <= 1_000_000:
            raise RuntimeValidationError("max_chars must be between 1 and 1,000,000")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RuntimeValidationError("manuscript_read supports UTF-8 paper files only") from exc
        start = int(offset_chars)
        end = min(start + int(max_chars), len(decoded))
        return {
            "path": f"paper/{normalized}",
            "sha256": digest,
            "size": len(raw),
            "content": decoded[start:end],
            "offset_chars": start,
            "next_offset_chars": end if end < len(decoded) else None,
            "total_chars": len(decoded),
            "truncated": end < len(decoded),
        }

    def paper_filesystem(
        self, *, project_id: str, action_id: str, operation: str,
        relative_path: str = ".", content: str | None = None,
    ) -> dict[str, Any]:
        """Provide the Writer a narrow, auditable filesystem rooted at paper/."""

        self._validate_scope(project_id, action_id)
        operation = str(operation).upper()
        if operation == "LIST":
            paper = (self.root / "projects" / project_id / "paper").resolve()
            path, normalized = self._paper_path(project_id, relative_path, allow_directory=True)
            if not path.exists() or not path.is_dir():
                raise RuntimeValidationError(f"paper directory does not exist: paper/{normalized}")
            files = [
                str(item.relative_to(paper))
                for item in sorted(path.rglob("*")) if item.is_file()
            ]
            return {"operation": "LIST", "path": f"paper/{normalized}", "files": files}
        if operation == "READ":
            return self.manuscript_read(
                project_id=project_id, action_id=action_id, relative_path=relative_path,
            )
        if operation != "WRITE":
            raise RuntimeValidationError("paper_filesystem operation must be READ, WRITE, or LIST")
        if content is None:
            raise RuntimeValidationError("paper_filesystem WRITE requires content")
        if len(content.encode("utf-8")) > 2_000_000:
            raise RuntimeValidationError("paper file exceeds the 2 MB write limit")
        path, normalized = self._paper_path(project_id, relative_path)
        if path.suffix.lower() not in {".md", ".tex", ".bib", ".yaml", ".yml", ".json", ".csv"}:
            raise RuntimeValidationError("paper_filesystem WRITE uses a restricted text extension")
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, content)
        return {
            "operation": "WRITE", "path": f"paper/{normalized}",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        }

    def _paper_path(
        self, project_id: str, relative_path: str, *, allow_directory: bool = False,
    ) -> tuple[Path, str]:
        paper = (self.root / "projects" / project_id / "paper").resolve()
        raw = str(relative_path).strip().replace("\\", "/")
        if raw.startswith("paper/"):
            raw = raw[len("paper/"):]
        if allow_directory and raw in {"", "."}:
            return paper, "."
        if not raw or raw.startswith("/"):
            raise RuntimeValidationError("paper path must be relative to paper/")
        path = (paper / raw).resolve()
        try:
            normalized = str(path.relative_to(paper))
        except ValueError as exc:
            raise RuntimeValidationError("paper path escapes the current project's paper directory") from exc
        if normalized == "." and not allow_directory:
            raise RuntimeValidationError("paper path must name a file")
        return path, normalized

    def academic_search(self, *, project_id: str, action_id: str, query: str, limit: int = 10) -> dict[str, Any]:
        self._validate_scope(project_id, action_id)
        if not query.strip() or not 1 <= int(limit) <= 25:
            raise RuntimeValidationError("academic_search requires a query and limit between 1 and 25")
        url = "https://api.openalex.org/works?" + urllib.parse.urlencode({"search": query, "per-page": int(limit)})
        result = self._capture_json(project_id, action_id, "search", url, {"query": query, "limit": int(limit)})
        if result.get("status") != "VERIFIED":
            return result
        works = result.get("response", {}).get("results", [])
        if not isinstance(works, list) or not works:
            result.update({
                "status": "SOURCE_UNVERIFIED",
                "reason": "OpenAlex returned no works; absence of results is not novelty evidence",
                "crossref_verifications": [],
                "metadata_conflicts": [],
                "unverified_results": [],
            })
            return result
        verifications = []
        conflicts = []
        unverified = []
        for work in works[:5] if isinstance(works, list) else []:
            doi = work.get("doi") if isinstance(work, Mapping) else None
            if not isinstance(doi, str) or not doi:
                if isinstance(work, Mapping):
                    unverified.append(work.get("id") or work.get("display_name") or "unknown-work")
                continue
            normalized = doi.removeprefix("https://doi.org/")
            crossref_url = "https://api.crossref.org/works/" + urllib.parse.quote(normalized, safe="")
            verification = self._capture_json(
                project_id, action_id, "crossref", crossref_url,
                {"doi": normalized, "purpose": "metadata-verification"},
            )
            verifications.append(verification)
            if verification.get("status") == "VERIFIED" and isinstance(work, Mapping):
                message = verification.get("response", {}).get("message", {})
                openalex_title = self._normalized_title(work.get("display_name") or work.get("title"))
                crossref_titles = message.get("title", []) if isinstance(message, Mapping) else []
                crossref_title = self._normalized_title(crossref_titles[0] if crossref_titles else None)
                openalex_year = work.get("publication_year")
                date_parts = message.get("published", {}).get("date-parts", []) if isinstance(message, Mapping) else []
                crossref_year = date_parts[0][0] if date_parts and date_parts[0] else None
                fields = []
                if not openalex_title or not crossref_title:
                    fields.append("title_missing")
                elif openalex_title != crossref_title:
                    fields.append("title")
                if not openalex_year or not crossref_year:
                    fields.append("year_missing")
                elif int(openalex_year) != int(crossref_year):
                    fields.append("year")
                if fields:
                    conflicts.append({"doi": normalized, "fields": fields})
        result["crossref_verifications"] = verifications
        result["metadata_conflicts"] = conflicts
        result["unverified_results"] = unverified
        if conflicts or unverified or any(item.get("status") != "VERIFIED" for item in verifications):
            result["status"] = "SOURCE_UNVERIFIED"
        return result

    @staticmethod
    def _normalized_title(value: Any) -> str:
        if not isinstance(value, str):
            return ""
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

    def citation_neighborhood(self, *, project_id: str, action_id: str, openalex_id: str, direction: str = "both", limit: int = 10) -> dict[str, Any]:
        self._validate_scope(project_id, action_id)
        if not 1 <= int(limit) <= 25:
            raise RuntimeValidationError("citation neighborhood limit must be between 1 and 25")
        identifier = openalex_id.rsplit("/", 1)[-1]
        if not re.fullmatch(r"W[0-9]+", identifier) or direction not in {"references", "citations", "both"}:
            raise RuntimeValidationError("citation_neighborhood requires an OpenAlex W-id and valid direction")
        urls = []
        if direction in {"references", "both"}:
            urls.append(("references", f"https://api.openalex.org/works/{identifier}"))
        if direction in {"citations", "both"}:
            urls.append(("citations", "https://api.openalex.org/works?" + urllib.parse.urlencode({"filter": f"cites:{identifier}", "per-page": min(int(limit), 25)})))
        results = [self._capture_json(project_id, action_id, f"neighborhood-{name}", url, {"openalex_id": identifier, "direction": name}) for name, url in urls]
        return {"status": "VERIFIED" if all(item["status"] == "VERIFIED" for item in results) else "SOURCE_UNVERIFIED", "openalex_id": identifier, "results": results}

    def source_read(self, *, project_id: str, action_id: str, url: str, max_bytes: int = 10_000_000) -> dict[str, Any]:
        self._validate_scope(project_id, action_id)
        if not 1 <= int(max_bytes) <= 10_000_000:
            raise RuntimeValidationError("max_bytes must be between 1 and 10,000,000")
        self._validate_public_https(url)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "research-harness/0.1 (evidence review)"})
            with self._open_public(request, timeout=20) as response:
                raw = response.read(max_bytes + 1)
                final_url = response.geturl()
                self._validate_public_https(final_url)
                content_type = response.headers.get_content_type()
            if len(raw) > max_bytes:
                raise RuntimeValidationError("academic source exceeds maximum captured size")
            captured = self._persist_bytes(
                project_id, action_id, "source", final_url, raw, content_type
            )
            extracted: str | None = None
            if content_type == "application/pdf" or raw.startswith(b"%PDF"):
                try:
                    from .proposal_review import _pdf_markdown

                    with tempfile.TemporaryDirectory() as directory:
                        pdf_path = Path(directory) / "source.pdf"
                        pdf_path.write_bytes(raw)
                        extracted = _pdf_markdown(pdf_path)
                except RuntimeValidationError as exc:
                    return {
                        **captured,
                        "text_status": "SOURCE_UNVERIFIED",
                        "text_reason": str(exc),
                    }
            elif content_type.startswith("text/") or content_type in {
                "application/xml", "application/xhtml+xml"
            }:
                try:
                    extracted = raw.decode("utf-8")
                except UnicodeDecodeError:
                    return {
                        **captured,
                        "text_status": "SOURCE_UNVERIFIED",
                        "text_reason": "open academic text is not valid UTF-8",
                    }
            if extracted is None:
                return {
                    **captured,
                    "text_status": "SOURCE_UNVERIFIED",
                    "text_reason": f"unsupported academic source media type: {content_type}",
                }
            text_capture = self._persist_bytes(
                project_id,
                action_id,
                "source-text",
                final_url,
                extracted.encode("utf-8"),
                "text/markdown" if content_type == "application/pdf" else "text/plain",
            )
            return {
                **captured,
                "text_status": "VERIFIED",
                "text_resource": text_capture["resource"],
                "text_metadata_resource": text_capture["metadata_resource"],
            }
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return {"status": "SOURCE_UNVERIFIED", "url": url, "accessed_at": _now(), "reason": str(exc)}

    def proposal_write(self, *, project_id: str, action_id: str, relative_path: str, content: str, media_type: str = "text/markdown") -> dict[str, Any]:
        self._validate_scope(project_id, action_id)
        if not re.fullmatch(
            r"(?:revisions/rev-[0-9]{3}\.md|changes/rev-[0-9]{3}\.ya?ml|"
            r"source-maps/proposal-r[0-9]{3}\.json)",
            relative_path,
        ):
            raise RuntimeValidationError(
                "proposal writes are restricted to numbered revisions, change logs, or source maps"
            )
        path = (self.root / "projects" / project_id / "resources" / "proposal" / relative_path).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError as exc:
            raise RuntimeValidationError("proposal resources are immutable and cannot be overwritten") from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            if path.exists():
                path.unlink()
            raise
        return {"uri": str(path.relative_to(self.root)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "media_type": media_type, "description": f"Proposal output from {action_id}"}

    def resource_write(
        self,
        *,
        project_id: str,
        action_id: str,
        relative_path: str,
        content: str,
        media_type: str = "text/markdown",
    ) -> dict[str, Any]:
        """Create one immutable, action-scoped scientific proof resource."""

        self._validate_scope(project_id, action_id)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", relative_path):
            raise RuntimeValidationError(
                "resource_write relative_path must be one plain file name"
            )
        path = (
            self.root / "projects" / project_id / "resources" / "proofs"
            / action_id / relative_path
        ).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError as exc:
            raise RuntimeValidationError(
                "scientific resources are immutable and cannot be overwritten"
            ) from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            if path.exists():
                path.unlink()
            raise
        return {
            "uri": str(path.relative_to(self.root)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "media_type": media_type,
            "description": f"Immutable scientific resource from {action_id}",
        }

    @staticmethod
    def _validate_scope(project_id: str, action_id: str) -> None:
        if not re.fullmatch(r"proj-[a-z0-9][a-z0-9-]*", project_id):
            raise RuntimeValidationError("invalid academic tool project_id")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", action_id):
            raise RuntimeValidationError("invalid academic tool action_id")

    @staticmethod
    def _validate_public_https(url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise RuntimeValidationError("academic_source_read only accepts public HTTPS URLs without credentials")
        hostname = parsed.hostname.lower().rstrip(".")
        if hostname in {"localhost", "metadata.google.internal"} or hostname.endswith((".localhost", ".local", ".internal")):
            raise RuntimeValidationError("local or internal source URLs are forbidden")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return
        if not address.is_global:
            raise RuntimeValidationError("non-public source IP addresses are forbidden")

    @staticmethod
    def _open_public(request: urllib.request.Request, *, timeout: int):
        AcademicSourceTools._validate_public_https(request.full_url)
        opener = urllib.request.build_opener(_PublicHTTPSRedirectHandler())
        return opener.open(request, timeout=timeout)

    def _capture_json(self, project_id: str, action_id: str, kind: str, url: str, request_meta: Mapping[str, Any]) -> dict[str, Any]:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "research-harness/0.1 (academic discovery)"})
            with self._open_public(request, timeout=20) as response:
                raw = response.read(5_000_001)
            if len(raw) > 5_000_000:
                raise RuntimeValidationError("academic response exceeds maximum captured size")
            value = json.loads(raw)
            capture = {
                "url": url, "accessed_at": _now(), "request": dict(request_meta),
                "response": value, "response_sha256": hashlib.sha256(raw).hexdigest(),
            }
            persisted = self._persist_bytes(
                project_id, action_id, kind, url,
                json.dumps(capture, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                "application/json",
            )
            return {**persisted, "request": dict(request_meta), "response": value}
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            return {"status": "SOURCE_UNVERIFIED", "url": url, "request": dict(request_meta), "accessed_at": _now(), "reason": str(exc)}

    def _persist_bytes(self, project_id: str, action_id: str, kind: str, url: str, raw: bytes, media_type: str) -> dict[str, Any]:
        digest = hashlib.sha256(raw).hexdigest()
        accessed_at = _now()
        directory = self.root / "projects" / project_id / "resources" / "academic" / action_id
        data_path = directory / f"{kind}-{digest[:16]}.bin"
        meta_path = directory / f"{kind}-{digest[:16]}.json"
        if not data_path.exists():
            data_path.parent.mkdir(parents=True, exist_ok=True)
            data_path.write_bytes(raw)
        if not meta_path.exists():
            atomic_write_json(meta_path, {"url": url, "accessed_at": accessed_at, "sha256": digest, "media_type": media_type, "size": len(raw)})
        metadata_digest = hashlib.sha256(meta_path.read_bytes()).hexdigest()
        return {
            "status": "VERIFIED", "url": url, "accessed_at": accessed_at,
            "resource": {"uri": str(data_path.relative_to(self.root)), "sha256": digest, "media_type": media_type, "description": "Captured academic source response"},
            "metadata_resource": {"uri": str(meta_path.relative_to(self.root)), "sha256": metadata_digest, "media_type": "application/json", "description": "Academic source access and version metadata"},
        }
