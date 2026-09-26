# Research Harness v0.1 Documentation

The documentation is organized by authority, not by implementation chronology.

## Start here

The library is installed separately from research data. See the repository
README for `workspace-init`, Python API usage, and the optional model-backed
runtime. Subsystem version numbers below identify contracts, not package releases.

- [Architecture v0.1](architecture-v0.1.md) — integrated system model,
  ownership boundaries, execution path, verification, cost, and recovery.
- [Repository README](../README.md) — product overview, quick start, and v0.1
  scope.

## Normative subsystem specifications

1. [Artifact Schema v0.1.2](artifact-schema-v0.1.2.md)
2. [Research Orchestrator v0.1](research-orchestrator-v0.1.md)
3. [Runtime Integration v0.1](runtime-integration-v0.1.md)
4. [Research Skills v0.1](research-skills-v0.1.md)
5. [Scientific Evaluation v0.1](scientific-evaluation-v0.1.md)
6. [Full MVP Wiring v0.1](full-mvp-wiring-v0.1.md)
7. [Cost Control + Model Routing v0.1](cost-control-model-routing-v0.1.md)

Later specifications refine compatible implementation details without
overriding the core authority rules: sessions are execution truth, artifacts
are scientific truth, and the deterministic orchestrator owns transitions.

## Validation and release evidence

- [Golden-path Pilots v0.1](golden-path-pilots-v0.1.md)
- [Failure & Recovery Hardening v0.1](failure-recovery-hardening-v0.1.md)
- `evals/` — adversarial cases, regression baseline, and failure matrix
- `tests/` — unit, integration, E2E, and failure-injection tests

Historical schema v0.1.1 and Step-oriented documents remain for auditability.
The integrated product name and current architecture version are **Research
Harness v0.1**.
