from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from research_artifacts.academic_sources import AcademicSourceTools
from research_artifacts.runtime_types import RuntimeValidationError


ROOT = Path(__file__).resolve().parents[1]


class _Response:
    def __init__(self, value: object, *, url: str = "https://example.org/source", content_type: str = "application/json"):
        self.raw = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
        self.url = url
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, amount: int = -1) -> bytes:
        return self.raw if amount < 0 else self.raw[:amount]

    def geturl(self) -> str:
        return self.url


class AcademicSourceToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        shutil.copytree(ROOT / "schemas", self.root / "schemas")
        shutil.copytree(ROOT / "config", self.root / "config")
        skills = self.root / "integrations/deepseek-harness/skills"
        skills.parent.mkdir(parents=True)
        shutil.copytree(ROOT / "integrations/deepseek-harness/skills", skills)
        (self.root / "projects").mkdir()
        (self.root / "projects/proj-tools/resources").mkdir(parents=True)
        self.tools = AcademicSourceTools(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _openalex(*, title: str = "Pinned Evidence", year: int = 2026, doi: str | None = "10.1000/test") -> dict:
        return {"results": [{
            "id": "https://openalex.org/W123", "display_name": title,
            "publication_year": year,
            "doi": f"https://doi.org/{doi}" if doi else None,
        }]}

    @staticmethod
    def _crossref(*, title: str = "Pinned Evidence", year: int = 2026) -> dict:
        return {"message": {"title": [title], "published": {"date-parts": [[year, 1, 1]]}}}

    def test_search_captures_openalex_and_crossref_with_hashes(self) -> None:
        with mock.patch(
            "research_artifacts.academic_sources.AcademicSourceTools._open_public",
            side_effect=[_Response(self._openalex()), _Response(self._crossref())],
        ):
            result = self.tools.academic_search(
                project_id="proj-tools", action_id="search-1", query="pinned evidence", limit=5,
            )
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["request"], {"query": "pinned evidence", "limit": 5})
        self.assertEqual(result["metadata_conflicts"], [])
        for capture in [result, *result["crossref_verifications"]]:
            resource = self.root / capture["resource"]["uri"]
            self.assertEqual(hashlib.sha256(resource.read_bytes()).hexdigest(), capture["resource"]["sha256"])
            self.assertTrue((self.root / capture["metadata_resource"]["uri"]).is_file())

    def test_metadata_conflict_and_missing_doi_fail_closed(self) -> None:
        with mock.patch(
            "research_artifacts.academic_sources.AcademicSourceTools._open_public",
            side_effect=[_Response(self._openalex()), _Response(self._crossref(title="Different", year=2025))],
        ):
            conflict = self.tools.academic_search(
                project_id="proj-tools", action_id="search-conflict", query="conflict",
            )
        self.assertEqual(conflict["status"], "SOURCE_UNVERIFIED")
        self.assertEqual(set(conflict["metadata_conflicts"][0]["fields"]), {"title", "year"})

        with mock.patch(
            "research_artifacts.academic_sources.AcademicSourceTools._open_public",
            return_value=_Response(self._openalex(doi=None)),
        ):
            missing = self.tools.academic_search(
                project_id="proj-tools", action_id="search-no-doi", query="no doi",
            )
        self.assertEqual(missing["status"], "SOURCE_UNVERIFIED")
        self.assertEqual(missing["unverified_results"], ["https://openalex.org/W123"])

        with mock.patch(
            "research_artifacts.academic_sources.AcademicSourceTools._open_public",
            return_value=_Response({"results": []}),
        ):
            empty = self.tools.academic_search(
                project_id="proj-tools", action_id="search-empty", query="unknown contribution",
            )
        self.assertEqual(empty["status"], "SOURCE_UNVERIFIED")
        self.assertIn("not novelty evidence", empty["reason"])

    def test_network_and_invalid_payload_fail_closed(self) -> None:
        for failure in (urllib.error.URLError("offline"), TimeoutError("slow")):
            with self.subTest(failure=type(failure).__name__), mock.patch(
                "research_artifacts.academic_sources.AcademicSourceTools._open_public", side_effect=failure,
            ):
                result = self.tools.academic_search(
                    project_id="proj-tools", action_id="search-failure", query="evidence",
                )
                self.assertEqual(result["status"], "SOURCE_UNVERIFIED")
        with mock.patch(
            "research_artifacts.academic_sources.AcademicSourceTools._open_public",
            return_value=_Response(b"not-json"),
        ):
            malformed = self.tools.academic_search(
                project_id="proj-tools", action_id="search-json", query="evidence",
            )
        self.assertEqual(malformed["status"], "SOURCE_UNVERIFIED")

    def test_source_read_rejects_ssrf_and_redirects(self) -> None:
        forbidden = [
            "http://example.org/paper", "https://user:secret@example.org/paper",
            "https://localhost/paper", "https://127.0.0.1/paper", "https://[::1]/paper",
            "https://metadata.google.internal/computeMetadata/v1/",
        ]
        for url in forbidden:
            with self.subTest(url=url), self.assertRaises(RuntimeValidationError):
                self.tools.source_read(project_id="proj-tools", action_id="read-1", url=url)
        with mock.patch(
            "research_artifacts.academic_sources.AcademicSourceTools._open_public",
            return_value=_Response(b"secret", url="https://127.0.0.1/internal", content_type="text/plain"),
        ), self.assertRaises(RuntimeValidationError):
            self.tools.source_read(
                project_id="proj-tools", action_id="read-redirect", url="https://example.org/paper",
            )

    def test_source_read_exposes_current_action_text_before_artifact_pinning(self) -> None:
        with mock.patch(
            "research_artifacts.academic_sources.AcademicSourceTools._open_public",
            return_value=_Response(
                b"Theorem 1. A pinned open-source statement.\n",
                content_type="text/plain",
            ),
        ):
            captured = self.tools.source_read(
                project_id="proj-tools", action_id="read-current", url="https://example.org/paper.txt",
            )
        self.assertEqual(captured["text_status"], "VERIFIED")
        text_resource = captured["text_resource"]
        read = self.tools.resource_read(
            project_id="proj-tools",
            action_id="read-current",
            uri=text_resource["uri"],
            max_chars=100,
        )
        self.assertIn("Theorem 1", read["content"])
        with self.assertRaises(RuntimeValidationError):
            self.tools.resource_read(
                project_id="proj-tools",
                action_id="different-action",
                uri=text_resource["uri"],
            )

    def test_scope_limits_and_immutable_proposal_write(self) -> None:
        with self.assertRaises(RuntimeValidationError):
            self.tools.academic_search(project_id="../escape", action_id="a", query="x")
        with self.assertRaises(RuntimeValidationError):
            self.tools.citation_neighborhood(
                project_id="proj-tools", action_id="a", openalex_id="not-a-work",
            )
        written = self.tools.proposal_write(
            project_id="proj-tools", action_id="revision-1",
            relative_path="revisions/rev-099.md", content="immutable\n",
        )
        self.assertTrue((self.root / written["uri"]).is_file())
        with self.assertRaises(RuntimeValidationError):
            self.tools.proposal_write(
                project_id="proj-tools", action_id="revision-2",
                relative_path="revisions/rev-099.md", content="overwrite\n",
            )
        source_map = self.tools.proposal_write(
            project_id="proj-tools", action_id="structuring-1",
            relative_path="source-maps/proposal-r001.json",
            content='{"fields": {"problem_definition": ["section:Introduction"]}}\n',
            media_type="application/json",
        )
        self.assertTrue((self.root / source_map["uri"]).is_file())
        with self.assertRaises(RuntimeValidationError):
            self.tools.proposal_write(
                project_id="proj-tools", action_id="structuring-2",
                relative_path="source-maps/proposal-r001.json", content="{}\n",
                media_type="application/json",
            )
        with self.assertRaises(RuntimeValidationError):
            self.tools.proposal_write(
                project_id="proj-tools", action_id="revision-3",
                relative_path="../project.yaml", content="escape\n",
            )

    def test_resource_read_supports_complete_character_pagination(self) -> None:
        relative = "projects/proj-tools/resources/proposal/revisions/rev-001.md"
        resource = self.root / relative
        resource.parent.mkdir(parents=True)
        original = "alpha-αβγ-" * 31
        resource.write_text(original, encoding="utf-8")
        digest = hashlib.sha256(resource.read_bytes()).hexdigest()
        pinned = SimpleNamespace(data={"resource": {"uri": relative, "sha256": digest}})

        chunks = []
        offset = 0
        with mock.patch.object(self.tools.workspace, "query", return_value=[pinned]):
            while True:
                result = self.tools.resource_read(
                    project_id="proj-tools", uri=relative, offset_chars=offset, max_chars=37,
                )
                chunks.append(result["content"])
                self.assertEqual(result["offset_chars"], offset)
                self.assertEqual(result["total_chars"], len(original))
                if result["next_offset_chars"] is None:
                    self.assertFalse(result["truncated"])
                    break
                self.assertTrue(result["truncated"])
                offset = result["next_offset_chars"]

            self.assertEqual("".join(chunks), original)
            with self.assertRaises(RuntimeValidationError):
                self.tools.resource_read(
                    project_id="proj-tools", uri=relative, offset_chars=-1,
                )

    def test_action_scoped_resource_write_is_immutable(self) -> None:
        result = self.tools.resource_write(
            project_id="proj-tools",
            action_id="action-proof-1",
            relative_path="informal-proof.md",
            content="# Proof\n",
        )
        self.assertEqual(result["media_type"], "text/markdown")
        self.assertTrue(result["uri"].endswith("/action-proof-1/informal-proof.md"))
        with self.assertRaises(RuntimeValidationError):
            self.tools.resource_write(
                project_id="proj-tools",
                action_id="action-proof-1",
                relative_path="informal-proof.md",
                content="replacement",
            )

    def test_paper_tools_are_bounded_to_current_project(self) -> None:
        written = self.tools.paper_filesystem(
            project_id="proj-tools", action_id="writer-1", operation="WRITE",
            relative_path="manuscript/draft.md", content="# Frozen draft\n\nTheorem.\n",
        )
        self.assertEqual(written["path"], "paper/manuscript/draft.md")
        read = self.tools.manuscript_read(
            project_id="proj-tools", action_id="review-1",
            relative_path="paper/manuscript/draft.md", max_chars=9,
        )
        self.assertEqual(read["content"], "# Frozen ")
        self.assertTrue(read["truncated"])
        self.assertEqual(len(read["sha256"]), 64)
        listing = self.tools.paper_filesystem(
            project_id="proj-tools", action_id="writer-1", operation="LIST",
            relative_path=".",
        )
        self.assertEqual(listing["files"], ["manuscript/draft.md"])
        for escaped in ("../project.yaml", "/tmp/out.md", "paper/../../outside.md"):
            with self.subTest(escaped=escaped), self.assertRaises(RuntimeValidationError):
                self.tools.manuscript_read(
                    project_id="proj-tools", action_id="review-1", relative_path=escaped,
                )
        with self.assertRaises(RuntimeValidationError):
            self.tools.resource_write(
                project_id="proj-tools",
                action_id="action-proof-1",
                relative_path="../escape.md",
                content="escape",
            )


if __name__ == "__main__":
    unittest.main()
