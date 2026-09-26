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

In a second terminal, export the same socket path and invoke the checked-in
wrapper from the library checkout (or use its absolute path from any directory):

```bash
export RESEARCH_HARNESS_SOCKET="$HOME/research-workspaces/my-study/.harness/research/cordis.sock"
./scripts/research-cordis init example --objective "Evaluate the proposed mechanism"
./scripts/research-cordis run example --budget-percent 10
./scripts/research-cordis status example
```

For proposal review:

```bash
./scripts/research-cordis init proposal-example --mode proposal-review \
  --proposal /path/to/proposal.md --objective "Review novelty and correctness"
./scripts/research-cordis run proposal-example --budget-percent 10
```

The socket stays inside the data workspace. Input files and all research outputs
stay there too. Framework schemas and contracts are loaded from the installed
Python package; skill presets are registered by the installer. Customize the
workspace routing configuration to use models available to your provider.
Git pushing is disabled by default; no remote is configured automatically.
