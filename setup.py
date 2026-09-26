"""Bundle canonical framework resources into wheels without duplicate sources."""

from pathlib import Path
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class BuildWithResources(build_py):
    def run(self):
        super().run()
        source = Path(__file__).parent
        destination = Path(self.build_lib) / "research_artifacts" / "_data"
        if destination.exists():
            shutil.rmtree(destination)
        for directory in (
            "schemas", "config", "evals",
            "integrations/deepseek-harness/skills",
            "integrations/deepseek-harness/presets",
        ):
            for path in (source / directory).rglob("*"):
                if path.is_file() and path.suffix in {".json", ".yaml", ".yml", ".md"}:
                    target = destination / path.relative_to(source)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(path, target)


setup(cmdclass={"build_py": BuildWithResources})
