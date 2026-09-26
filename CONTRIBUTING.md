# Contributing

Keep framework source and research data separate. Do not add live projects,
manuscripts, experiment scripts for a particular study, model checkpoints,
credentials, runtime traces, or private machine paths. Use small, fictional
fixtures under `tests/fixtures/` for regression tests. Run research from an
independent workspace initialized with `research --workspace PATH workspace-init`.

Install Python development dependencies with `python -m pip install -e '.[dev]'`.
Run `python -m unittest discover -s tests -p 'test_*.py'` and `research-evals`.
For integration changes, run `npm ci`, `npm run build`, and `npm test` inside
`integrations/deepseek-harness/`.

Canonical schemas, skill contracts, presets, routing defaults, and evaluation
cases remain in their top-level source directories. The package build copies
these into `research_artifacts/_data/`. Do not edit generated build directories.
Test the built wheel from outside the checkout as well as the editable install:

```bash
python -m build
python -m venv /tmp/research-harness-wheel-check
/tmp/research-harness-wheel-check/bin/pip install dist/*.whl
cd /tmp
/tmp/research-harness-wheel-check/bin/research --workspace /tmp/example-workspace workspace-init
/tmp/research-harness-wheel-check/bin/research --workspace /tmp/example-workspace init example
/tmp/research-harness-wheel-check/bin/research --workspace /tmp/example-workspace status example
/tmp/research-harness-wheel-check/bin/research-evals
```

Use new temporary paths for each smoke test. A workspace initializer never
reuses a nonempty directory. Synchronize release versions in `pyproject.toml`,
`VERSION`, `research_artifacts/__init__.py`, and the TypeScript package metadata.
