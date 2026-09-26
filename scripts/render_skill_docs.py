#!/usr/bin/env python3
"""Render operational SKILL.md files from validated research skill contracts."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml


THEORY_POLICY = {
    "theory-development", "theory-verification", "lean-formalization",
    "lean-verification", "semantic-alignment-review",
}
EXPERIMENT_POLICY = {
    "experiment-design", "experiment-execution", "experiment-verification",
}


def _frontmatter(path: Path) -> tuple[str, str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, flags=re.DOTALL)
    if not match:
        raise ValueError(f"missing YAML frontmatter: {path}")
    value = yaml.safe_load(match.group(1))
    if set(value) != {"name", "description"}:
        raise ValueError(f"frontmatter must contain only name and description: {path}")
    return str(value["name"]), str(value["description"])


def _bullets(values: list[str], *, code: bool = False) -> str:
    return "\n".join(
        f"- `{value}`" if code else f"- {value}" for value in values
    )


def render(path: Path) -> str:
    contract = yaml.safe_load((path / "skill.yaml").read_text(encoding="utf-8"))
    name, description = _frontmatter(path / "SKILL.md")
    if name != contract["id"]:
        raise ValueError(f"frontmatter/contract ID mismatch: {path}")
    references = [
        "Read `../_shared/artifact-boundaries.md`, `../_shared/failure-taxonomy.md`, and `../_shared/scientific-evaluation-policy.md` before acting.",
    ]
    if name in THEORY_POLICY:
        references.append("Also read `../_shared/theory-verification-policy.md` and apply it without waiver.")
    if name in EXPERIMENT_POLICY:
        references.append("Also read `../_shared/experiment-integrity-policy.md` and preserve the protocol lock.")
    permissions = contract["permissions"]
    sections = [
        "---", f"name: {name}", f"description: {description}", "---", "",
        f"# {name}", "", *references,
        "Use only the revision-pinned ContextBundle. Treat session history as execution provenance, never scientific truth.",
        "", "## Purpose", "", contract["purpose"],
        "", "## Inputs", "", _bullets(contract["inputs"]),
        "", "## Outputs", "", _bullets(contract["outputs"]),
        "", "## Tools", "", _bullets(contract["tools"], code=True),
        "", "## Procedure", "",
        "\n".join(f"{number}. {step}" for number, step in enumerate(contract["procedure"], 1)),
        "", "## Completion Conditions", "", _bullets(contract["completion_conditions"]),
        "", "## Failure Classes", "", _bullets(contract["failure_classes"], code=True),
        "", "Report the narrowest applicable class. Do not choose a workflow transition; submit evidence for deterministic GateService mapping.",
        "", "## Non-goals", "", _bullets(contract["non_goals"]),
        "", "## Permissions", "",
        f"- Profile: `{permissions['profile']}`",
        f"- Sandbox: `{permissions['sandbox']}`",
        f"- Operations: {', '.join(f'`{item}`' for item in permissions['operations']) or 'none'}",
        f"- Artifact kinds: {', '.join(f'`{item}`' for item in permissions['artifact_kinds']) or 'none'}",
        f"- Field policy: {permissions['field_policy']}",
        "- Canonical promotion: forbidden; only the host controller may accept proposals.",
        "", "## Provenance", "",
        "Record all of the following in the proposal, Review, or immutable resource manifest:",
        "", _bullets(contract["provenance"], code=True),
        "", "## Forbidden Behaviors", "", _bullets(contract["forbidden_behaviors"]),
        "- Inferring accepted science from conversation history or final prose.",
        "- Directly modifying canonical artifacts or selecting the next global state.",
        "", "## Submission", "",
        "Validate every candidate, stage only allowed proposals/resources, and call `research_action_submit` with the current `action_id` and `bundle_sha256`. A natural-language final answer has no transition authority.",
        "",
    ]
    return "\n".join(sections)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("skills_root", type=Path)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    mismatches: list[Path] = []
    for path in sorted(args.skills_root.glob("*/skill.yaml")):
        directory = path.parent
        rendered = render(directory)
        target = directory / "SKILL.md"
        if args.check:
            if target.read_text(encoding="utf-8") != rendered:
                mismatches.append(target)
        else:
            target.write_text(rendered, encoding="utf-8")
    if mismatches:
        for path in mismatches:
            print(f"out of date: {path}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
