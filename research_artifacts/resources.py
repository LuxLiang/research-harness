"""Locate framework resources independently of a user's research workspace."""

from pathlib import Path


def resource_path(workspace: str | Path, relative: str | Path) -> Path:
    """Prefer explicit workspace overrides, then installed or checkout defaults.

    Only framework-owned paths should pass through this function. Research
    artifacts always resolve directly against the workspace.
    """
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("resource path must be relative and cannot contain '..'")
    package = Path(__file__).resolve().parent
    candidates = (
        Path(workspace).resolve() / relative,
        package / "_data" / relative,
        package.parent / relative,
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"framework resource not found: {relative}")
