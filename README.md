# Research Harness

A reusable library for artifact-driven, recoverable scientific workflows.
Python owns artifact validation, workflow transitions, budgets, and Git
transactions. The optional DeepSeek Harness integration executes model-backed
actions. Research projects live in **separate workspaces**, outside this library.

## Install

Requires Python 3.11+ and Git. From a checkout:

```bash
python -m pip install .
```

For development, use `python -m pip install -e '.[dev]'`. The Python package
includes schemas, default configuration, skill contracts, and evaluation cases;
a research workspace does not need a copy of the source repository.

## Use from the command line

Create a new, empty workspace outside the checkout:

```bash
research --workspace "$HOME/research-workspaces/example" workspace-init
research --workspace "$HOME/research-workspaces/example" init example \
  --objective "Evaluate a reproducible research workflow"
research --workspace "$HOME/research-workspaces/example" status example
```

`workspace-init` creates an independent Git repository and an editable
`config/model-routing.v0.1.yaml`. It refuses to overwrite a nonempty directory.
Scientific artifacts are stored under `projects/`; ephemeral execution state is
stored under `.harness/` and excluded from Git.

The Python CLI's `run` command uses a **synthetic runtime for deterministic
integration testing**. It does not perform real model-backed research:

```bash
research --workspace "$HOME/research-workspaces/example" run example --budget-percent 100
```

For real model-backed research, use the optional
[DeepSeek Harness integration](integrations/deepseek-harness/README.md), which
provides `research-cordis` and an operator socket. The synthetic and production
controllers must not run concurrently against the same project.

Available workflows are `full-research` and `proposal-review`. The former accepts
repeatable `--source-material` inputs; the latter accepts `--proposal` and an
optional `--rubric`. Inputs are copied into the workspace and hash-pinned.
Use `research --help` and `research init --help` for commands and options.

## Use from Python

```python
from pathlib import Path
from research_artifacts import (
    ArtifactWorkspace,
    MVPController,
    SyntheticSkillRuntime,
    initialize_workspace,
)

workspace = initialize_workspace(Path.home() / "research-workspaces" / "api-example")
controller = MVPController(workspace, SyntheticSkillRuntime(workspace))
controller.init("example", "Check a reproducible workflow")
print(controller.status("proj-example"))
assert ArtifactWorkspace(workspace).validate() == []
```

To integrate another executor, supply an implementation of
`research_artifacts.mvp.SkillRuntime`: its `execute(action, bundle)` method
returns the structured action result. Artifact validation, gates, and scientific
transactions remain controlled by the framework.

## Layout

| Directory | Purpose |
| --- | --- |
| `research_artifacts/` | Public Python API, CLI, validation, orchestration, recovery |
| `research_evals/` | Deterministic scientific quality evaluations |
| `schemas/` | Versioned artifact and runtime contracts |
| `config/` | Default model routing configuration |
| `integrations/deepseek-harness/` | Optional TypeScript runtime plugin, skills, presets |
| `evals/` | Synthetic adversarial cases and regression baselines |
| `tests/` | Framework tests and fictional fixtures |
| `docs/` | Architecture and subsystem specifications |

No real research projects, experiment outputs, manuscripts, vendored research
repositories, credentials, or machine-specific paths belong in this checkout.
The generic `proj-demo` fixture and small known-result pilots are test inputs,
not research outputs. Production workspace content is never included in releases.

## Validate

```bash
python -m unittest discover -s tests -p 'test_*.py'
research-evals
python -m build

cd integrations/deepseek-harness
npm ci
npm run build
npm test
```

See [development guidance](CONTRIBUTING.md),
[architecture](docs/architecture-v0.1.md), and the
[documentation index](docs/README.md). Framework schemas and skill contracts may
be overridden in a workspace using the same relative paths; otherwise packaged
defaults are used. Model aliases in the default routing configuration must be
adapted to the executor's available models.
