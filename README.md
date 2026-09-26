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
research --workspace "$HOME/research-workspaces/example" doctor
research --workspace "$HOME/research-workspaces/example" init example \
  --objective "Evaluate a reproducible research workflow"
research --workspace "$HOME/research-workspaces/example" status example
```

`workspace-init` creates an independent Git repository and an editable
`config/model-routing.v0.1.yaml`. It refuses to overwrite a nonempty directory.
Scientific artifacts are stored under `projects/`; ephemeral execution state is
stored under `.harness/` and excluded from Git.

By default, `run` uses a **synthetic runtime for deterministic integration
testing** and prints that fact to stderr. It does not perform real model-backed
research. You can also select it explicitly:

```bash
research --workspace "$HOME/research-workspaces/example" run example \
  --runtime synthetic --budget-percent 100 --max-actions 2
```

For real model-backed research, use the optional
[DeepSeek Harness integration](integrations/deepseek-harness/README.md), which
provides a model-backed host and an operator socket. Once the host is running,
the same installed Python CLI can control it directly:

```bash
research --workspace "$HOME/research-workspaces/example" doctor --runtime cordis
research --workspace "$HOME/research-workspaces/example" run example \
  --runtime cordis --budget-percent 10 --max-actions 2
research --workspace "$HOME/research-workspaces/example" status example --runtime cordis
```

Use a fresh project for real research; do not use a project already populated
by synthetic execution. The synthetic and production controllers must not run
concurrently against the same project. The CLI refuses synthetic mutations when
the configured operator socket path exists. All project commands accept
`--runtime cordis`; the existing `research-cordis` wrapper remains supported.

`doctor` emits JSON and exits with code 0 when checks pass, or 2 when a check
fails. It checks Python, the independent Git workspace and commit identity,
framework resources, routing configuration, and (for Cordis) the host protocol
and workspace identity. It is read-only and **does not test model credentials
or inference**. Selecting Cordis never silently falls back to synthetic work.

`--socket PATH` overrides `RESEARCH_HARNESS_SOCKET`, which overrides the default
`WORKSPACE/.harness/research/cordis.sock`. Relative socket paths resolve inside
the selected workspace. `--timeout SECONDS` bounds operator responses (default
3600 seconds). A timeout does not cancel the host; inspect `status` before
retrying. Requests are not automatically replayed after connection failures.

`--budget-percent` is a framework estimate, not a provider quota or monetary
spending cap. Noninteractive runs without a required budget return the
controller's budget-wait state instead of prompting on stdin.

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
