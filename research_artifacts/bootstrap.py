"""Create an independent, version-controlled research data workspace."""

from pathlib import Path
import shutil
import subprocess

from .resources import resource_path
from .workspace import ArtifactError


def initialize_workspace(root: str | Path) -> Path:
    """Initialize a new or empty directory; never import an existing Git repo."""
    root = Path(root).expanduser().resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ArtifactError(f"workspace must be a new or empty directory: {root}")
    if shutil.which("git") is None:
        raise ArtifactError("Git is required to initialize a research workspace")
    root.mkdir(parents=True, exist_ok=True)
    (root / "projects").mkdir()
    (root / "projects/.gitkeep").touch()
    (root / "config").mkdir()
    shutil.copyfile(
        resource_path(root, "config/model-routing.v0.1.yaml"),
        root / "config/model-routing.v0.1.yaml",
    )
    (root / ".gitignore").write_text(
        ".harness/\n__pycache__/\n*.py[cod]\n.venv/\n.env\n"
        "**/raw_outputs/\n**/checkpoints/\n**/datasets/\n", encoding="utf-8",
    )
    (root / "README.md").write_text(
        "# Research workspace\n\n"
        "Research Harness stores project artifacts in `projects/` and local "
        "runtime state in `.harness/`. Framework code is installed separately.\n",
        encoding="utf-8",
    )

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=root, text=True, capture_output=True, check=True,
        )

    git("init", "-b", "main")
    # Give automated scientific transactions a local identity when the user
    # has not configured one. Never change global Git configuration.
    for key, default in (
        ("user.name", "Research Harness"),
        ("user.email", "research-harness@localhost"),
    ):
        configured = subprocess.run(
            ["git", "config", "--get", key], cwd=root, capture_output=True, text=True,
        )
        if not configured.stdout.strip():
            git("config", key, default)
    git("add", ".gitignore", "README.md", "config", "projects/.gitkeep")
    git("commit", "-m", "Initialize research workspace")
    return root
