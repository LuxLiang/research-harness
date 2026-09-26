# Usage

Install from a checkout with `python -m pip install .` in a Python 3.11+
virtual environment. Git and POSIX filesystem locking are required. The wheel
bundles schemas, routing defaults, skills, presets, and evaluation cases.
No source checkout is needed in a research workspace.

## Workspaces and workflows

Initialize a new, empty directory outside the library:

```bash
export RESEARCH_WORKSPACE="$HOME/research-workspaces/my-study"
research --workspace "$RESEARCH_WORKSPACE" workspace-init
research --workspace "$RESEARCH_WORKSPACE" doctor
```

The workspace has its own Git history, `projects/`, and editable
`config/model-routing.v0.1.yaml`. `.harness/` holds ignored execution state.
Inputs are copied into the workspace and pinned by checksum. Markdown and
PDFs with extractable text are supported; scanned PDFs need a text version.

The following examples use a running [model host](../integrations/deepseek-harness/README.md).
Use a fresh project for real research, separate from any synthetic demo.

```bash
# Full research; --source-material can be repeated or omitted.
research --workspace "$RESEARCH_WORKSPACE" init study --runtime cordis \
  --objective "Assess the proposed mechanism" \
  --source-material /path/to/background.md

# Proposal review; --rubric is optional.
research --workspace "$RESEARCH_WORKSPACE" init review --runtime cordis \
  --mode proposal-review --proposal /path/to/proposal.md \
  --rubric /path/to/rubric.md

research --workspace "$RESEARCH_WORKSPACE" run study --runtime cordis \
  --budget-percent 10 --max-actions 2
research --workspace "$RESEARCH_WORKSPACE" status study --runtime cordis
```

Full research proceeds through framing, literature, planning, theory and/or
experiments, synthesis, checks, writing, and final approval. Proposal review
structures and reviews an input proposal, handles decisions and authorized
revisions, and can create a derived full-research project after approval.
Neither workflow guarantees autonomous completion; human gates can stop a run.

## Runtime selection and diagnostics

| Runtime | Purpose | Requirement |
| --- | --- | --- |
| `synthetic` (default) | Deterministic workflow tests | Installed Python package |
| `cordis` | Model-backed execution via the optional plugin | Running compatible host and configured provider |

Cordis is the plugin framework used by the host, not a model. Model aliases in
routing configuration must match the host's available models.

`doctor` emits JSON and exits with 0 on success or 2 on failure. It checks the
workspace Git repository, commit identity, framework resources, and routing
configuration. With `--runtime cordis`, it also checks the operator protocol
and host workspace identity. It is read-only and does not test credentials or
make model calls. A failed model-runtime request never falls back to synthetic.
Synthetic mutations are refused while the configured operator socket exists.

Socket selection, in priority order:

1. `--socket PATH`
2. `RESEARCH_HARNESS_SOCKET`
3. `WORKSPACE/.harness/research/cordis.sock`

Relative socket paths resolve inside the workspace. `--timeout SECONDS`
bounds the client wait (default 3600). A timeout does not stop the host.
Check status before retrying; requests are never automatically replayed.

## Human controls and budgets

Use `research COMMAND --help` for exact options. Project commands support
`--runtime cordis`; keep the same runtime throughout a project's lifetime.

| Command | Effect |
| --- | --- |
| `status` | Inspect the current checkpoint |
| `pause` / `resume` | Stop a live action while retaining resumable state / continue |
| `cancel` | Terminate and archive the project |
| `budget` | Approve additional estimated budget; can keep execution paused |
| `unblock` | Record a recovery reason and continue after fixing the condition |
| `approve` | Submit human approval at the applicable gate |
| `plan-revision` | Request a plan revision through the model host |
| `proposal-decisions` | Apply a structured decision file |
| `convert` | Create a full-research project from an approved proposal |

`--budget-percent` accepts a percentage of the framework's estimated full-run
budget. It is not a provider quota, token ceiling, or monetary spending cap.
A noninteractive run that needs budget approval returns a budget-wait state
instead of reading stdin. An action limit bounds actions, not model cost.

## Python API

```python
from pathlib import Path
from research_artifacts import (
    ArtifactWorkspace,
    MVPController,
    SyntheticSkillRuntime,
    initialize_workspace,
)

workspace = initialize_workspace(Path.home() / "research-workspaces" / "api-demo")
controller = MVPController(workspace, SyntheticSkillRuntime(workspace))
controller.init("demo", "Test a traceable workflow")
print(controller.status("proj-demo"))
assert ArtifactWorkspace(workspace).validate() == []
```

This example is synthetic. Another executor can implement
`research_artifacts.mvp.SkillRuntime.execute(action, bundle)` and return a
structured action result. Validation, evidence gates, and transactions remain
framework responsibilities. For the existing model integration, use the host
and unified CLI rather than constructing `SyntheticSkillRuntime`.

## Resource overrides

Resources resolve in order: workspace override, installed package, source
checkout. An override uses the same relative path as its canonical resource,
for example `config/model-routing.v0.1.yaml`. Schemas, skills, and routing
changes affect validation or execution authority; review them accordingly.
Keep research outputs and credentials out of the library checkout.
