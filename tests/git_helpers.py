"""Small committed code repositories for execution-adapter fixtures."""
import subprocess
from pathlib import Path


def committed_code(root: Path) -> str:
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], text=True,
                              capture_output=True, check=True).stdout.strip()
    git('init', '-b', 'main')
    git('add', '--all')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '--allow-empty', '-m', 'Pinned code fixture')
    return git('rev-parse', 'HEAD')
