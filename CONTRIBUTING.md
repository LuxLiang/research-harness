# Contributing

Keep research data in an independent workspace. Commit only framework source,
contracts, documentation, and small fictional fixtures. Do not commit live
projects, model checkpoints, credentials, runtime logs, or private machine paths.

## Development checks

Use Python 3.11+, Git, and a POSIX environment that permits local Unix sockets:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m unittest discover -s tests -p 'test_*.py'
research-evals --baseline evals/baselines/v0.1.json
```

For integration changes, also use Node.js 20+ and npm:

```bash
cd integrations/deepseek-harness
npm ci
npm run build
npm test
```

Operator tests use stub controllers and local sockets, not real models. Node
tests also exercise the Python bridge; set `RESEARCH_HARNESS_TEST_PYTHON` to the
installed Python interpreter if it is not available as `python3`.

Preserve deterministic transitions, revision-pinned evidence, independent
verification, and recoverable transactions. Keep canonical resources in
`schemas/`, `config/`, `evals/`, and the integration's `skills/` and `presets/`.
Builds bundle them automatically; do not edit generated copies.

## Package verification

Build an sdist and wheel, then test the installed wheel outside the checkout.
Run this from the repository root with development dependencies installed:

```bash
export RELEASE_CHECK="$(mktemp -d)"
python -m build --outdir "$RELEASE_CHECK/dist"
python -m venv "$RELEASE_CHECK/venv"
"$RELEASE_CHECK/venv/bin/pip" install "$RELEASE_CHECK"/dist/*.whl
cd "$RELEASE_CHECK"
"$RELEASE_CHECK/venv/bin/research" --workspace "$RELEASE_CHECK/workspace" workspace-init
"$RELEASE_CHECK/venv/bin/research" --workspace "$RELEASE_CHECK/workspace" doctor
"$RELEASE_CHECK/venv/bin/research" --workspace "$RELEASE_CHECK/workspace" init demo
"$RELEASE_CHECK/venv/bin/research" --workspace "$RELEASE_CHECK/workspace" status demo
"$RELEASE_CHECK/venv/bin/research-evals"
```

GitHub Actions runs Python tests, baseline evaluation, wheel smoke checks, and
TypeScript integration tests. Synthetic tests do not establish live provider
compatibility or scientific performance. Report model-backed checks separately.

Before a release, synchronize `pyproject.toml`, `VERSION`,
`research_artifacts/__init__.py`, and the integration's package and lockfile
versions. Review evaluation baseline changes as scientific-policy changes.
