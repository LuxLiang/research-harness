# Working on Research Harness

This repository is a reusable framework library. Keep user research in a
separate workspace. Do not add project artifacts, manuscripts, experimental
results, runtime logs, credentials, or machine-specific paths to this checkout.
Fictional regression fixtures belong in `tests/fixtures/`.

Read `README.md` for installation and invocation. To use the framework for a
study, initialize an external workspace with
`research --workspace /path/to/workspace workspace-init`. Use the Python API
or the optional DeepSeek Harness integration there. The default Python CLI
runtime is synthetic and must not be represented as real scientific execution.

When changing framework code, preserve deterministic transition authority,
revision-pinned evidence, and recoverable transactions. Follow
`CONTRIBUTING.md` for tests and release checks. Keep schemas and skill contracts
in their canonical source directories; package builds bundle them automatically.
