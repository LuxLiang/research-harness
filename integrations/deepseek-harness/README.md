# DeepSeek Harness integration

Optional model-backed runtime for Research Harness. Requires Node.js 20+, npm,
a compatible `dsh` installation, an authenticated model provider, and the
Research Harness Python package installed in the selected Python environment.

From the library checkout, install and build the integration:

```bash
python -m pip install .
npm --prefix integrations/deepseek-harness ci
./scripts/install_dsh_plugin.sh web
```

Create a separate data workspace and start the host:

```bash
export RESEARCH_HARNESS_WORKSPACE="$HOME/research-workspaces/my-study"
research --workspace "$RESEARCH_HARNESS_WORKSPACE" workspace-init
export RESEARCH_HARNESS_PYTHON="$(command -v python)"
export RESEARCH_HARNESS_SOCKET="$RESEARCH_HARNESS_WORKSPACE/.harness/research/cordis.sock"
# Match this to a configured provider in the host.
export RESEARCH_HARNESS_PROVIDER="openai-codex"
dsh --profile web
```

In a second terminal, invoke the unified Python CLI from any directory:

```bash
export RESEARCH_HARNESS_WORKSPACE="$HOME/research-workspaces/my-study"
research --workspace "$RESEARCH_HARNESS_WORKSPACE" doctor --runtime cordis
research --workspace "$RESEARCH_HARNESS_WORKSPACE" init example --runtime cordis \
  --objective "Evaluate the proposed mechanism"
research --workspace "$RESEARCH_HARNESS_WORKSPACE" run example --runtime cordis \
  --budget-percent 10 --max-actions 2
research --workspace "$RESEARCH_HARNESS_WORKSPACE" status example --runtime cordis
```

For proposal review:

```bash
research --workspace "$RESEARCH_HARNESS_WORKSPACE" init proposal-example \
  --runtime cordis --mode proposal-review \
  --proposal /path/to/proposal.md --objective "Review novelty and correctness"
research --workspace "$RESEARCH_HARNESS_WORKSPACE" run proposal-example \
  --runtime cordis --budget-percent 10
```

The socket stays inside the data workspace. Input files and all research outputs
stay there too. Framework schemas and contracts are loaded from the installed
Python package; skill presets are registered by the installer. Customize the
workspace routing configuration to use models available to your provider.
Git pushing is disabled by default; no remote is configured automatically.

The Python client needs no Node installation of its own; the host still does.
Rebuild and restart older hosts to enable the `research-operator/v0.1` health
handshake. The client verifies that the host serves the selected workspace
before sending a project command. Host connectivity does not verify provider
authentication or model availability; `doctor` does not make a model call.

The original `scripts/research-cordis --socket PATH ...` wrapper is still
available. The unified `research` CLI preserves its earlier Python semantics:
`resume`, `unblock`, and budget increases continue the run; the legacy wrapper's
corresponding commands only perform the requested control operation.

Set `--timeout SECONDS` on the Python CLI for longer actions. A timeout or client
disconnect does not stop the host. Inspect status, or explicitly pause/cancel,
before retrying a mutation. Failed requests never fall back to a synthetic run.
