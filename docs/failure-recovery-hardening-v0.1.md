# Failure & Recovery Hardening v0.1

This document is the Step 8 release record for Research Harness v0.1.0. The
failure policy is fail closed: canonical YAML, the versioned orchestrator
checkpoint, immutable resources, and Git history are authoritative. Session or
conversation history is never used to infer scientific state.

## 1. Failure-injection matrix

The machine-readable matrix is
[`evals/failure-injection-v0.1.yaml`](../evals/failure-injection-v0.1.yaml).
It contains 70 cases: 9 runtime, 12 artifact/Git, 12 theory/Lean, 13 experiment,
10 scientific-integrity, 4 human-control, and 10 crash-point cases. The matrix
contains 54 S4 and 16 S3 cases and maps every injection to a deterministic unit,
integration, or scientific-evaluation case.

## 2. Tests implemented

- Runtime timeout, dependency loss, cancellation, crash/resume, context overflow,
  and sidecar restart.
- Invalid and stale proposals, dirty transaction targets, unrelated staged user
  files, commit rollback, PREPARED-journal recovery, checkpoint corruption, and
  non-blocking remote replication failure.
- Lean timeout and failure classification, plus all Step 5 proof, axiom, semantic,
  and library-gap adversarial cases.
- Dataset preflight, experiment timeout, hard crash, stable run-ID reuse, raw failed
  run retention, and all protocol-integrity adversarial cases.
- Distinct PAUSE, RESUME, CANCEL, BLOCKED, UNBLOCK, and final-approval semantics.
- All ten required crash points and a persisted Step 7 THEORY/EXPERIMENT/MIXED
  golden-path regression check.
- Final review rejects missing, stale, or unanchored revision-pinned manuscript
  traceability.

## 3. Failures observed

| Severity | Observed failure |
|---|---|
| S4 | Intentional pause/cancel abort in the TypeScript adapter could be recorded as `ACTION_FAILED`, clearing the pending action or exhausting retries. |
| S4 | An interrupted Experiment had no durable pre-execution journal and could rerun the same stable run ID. |
| S4 | PREPARED transaction recovery inspected only the latest commit and could roll back a transaction that had committed before a later unrelated commit. |
| S4 | Corrupt `state.yaml` had no Git-only fail-closed recovery path. |
| S4 | `UNBLOCK` did not require explicit human recovery authority. |
| S4 | Step 7 THEORY had reached final approval after a targeted Writer revision removed the theorem and emptied `paper/traceability.yaml`. |
| S3 | Model/tool timeouts and dependency errors escaped the Python controller instead of following bounded retry/block policy. |
| S3 | Lean timeout, syntax/formalization failure, and library-gap outcomes were not deterministically distinguished. |
| S3 | Dataset bytes were not checksum-validated immediately before experiment dispatch. |
| S3 | An exhausted action retry counter could leak across an explicit recovery or scope-changing transition. |

## 4. Root causes

- Cancellation and ordinary runtime failure shared one catch path.
- Experiment idempotence was inferred from final output existence rather than a
  RUNNING/COMMITTED journal written before execution.
- Git recovery searched only `HEAD` commit text rather than history for the exact
  action ID.
- Checkpoint loading had no quarantine and last-valid-Git reconstruction boundary.
- The state reducer allowed bare `UNBLOCK` and did not reset transient retry scope
  at explicit recovery/state-routing boundaries.
- Golden final review checked manuscript prose but did not require the separate
  revision-pinned traceability manifest.
- Tool adapters exposed raw subprocess exceptions instead of stable scientific
  failure classes.

## 5. Fixes applied

- Intentional abort leaves the persisted pending action intact; only non-abort
  failures emit `ACTION_FAILED`.
- Experiment execution writes a stable `run-state.json` before dispatch, validates
  local dataset checksums, retains timeout/interruption raw output, and never
  re-executes an existing run ID after restart.
- Scientific transaction recovery locates the exact action commit anywhere in Git
  history, and deterministically finishes or rolls back PREPARED journals.
- Corrupt checkpoints are quarantined, reconstructed only from the newest
  schema-valid Git checkpoint, and committed as `BLOCKED`; no session content is
  consulted.
- `UNBLOCK` requires a pinned same-project ACCEPTED Decision tagged `recovery`.
- Runtime conflicts rebuild context without consuming scientific retry; timeout and
  dependency failures obey bounded retry and then BLOCKED.
- Lean adapter reports `TOOL_TIMEOUT`, `TOOL_UNAVAILABLE`, `FORMALIZATION_GAP`,
  `LIBRARY_GAP`, or `LEAN_PROOF_GAP` without equating library failure with falsehood.
- Final manuscript validation requires exact statement presence and an anchored
  trace entry pinning artifact ID, kind, and current revision.
- The THEORY pilot was routed through targeted Writer revision and a new independent
  final-review session. Its Claim and Lean theorem remained unchanged.

## 6. Regression results

- Python unit/integration/hardening tests: 98 passed.
- Focused scientific evaluations: 46/46 passed; S4 failures = 0.
- DeepSeek TypeScript integration: build passed; all 6 contract cases in the
  public-loader integration suite passed.
- Canonical artifact validation: 92 artifacts valid.
- Real pinned Lean v4.19.0/mathlib checks: THEORY and MIXED sources passed; only the
  approved axioms `propext`, `Classical.choice`, and `Quot.sound` were reported.
- Step 7 golden paths: THEORY, EXPERIMENT, and MIXED remain at
  `WAITING_FINAL_APPROVAL`, with zero unresolved S3/S4 and zero accepted S4.

## 7. Remaining known limitations

- Recovery uses a local per-project file lock and Git transaction journal; it is not
  a distributed transaction protocol.
- An interrupted external process that continues outside the runtime's control may
  require operator reconciliation; the Harness fails closed and does not launch a
  duplicate run ID.
- External datasets and large raw outputs remain available only through their pinned
  URI/version/checksum; remote object-store availability is not guaranteed by Git.
- DeepSeek Harness remains pinned to `0.1.0-rc.8`; RuntimeAdapter compatibility must
  be retested before upgrading.
- The golden pilots are controlled and small. They do not validate open-ended,
  expensive, distributed, or formal-library-frontier research.

## 8. Final S3/S4 issue list

All S3/S4 findings listed in section 3 are resolved and covered by regression tests.
The repaired THEORY report retains `THEORY_MANUSCRIPT_TRACE_MISSING` as a historical
resolved S4. Unresolved S3: 0. Unresolved S4: 0.

## 9. Recommendation

`MVP_READY_WITH_LIMITATIONS`.

Research Harness v0.1.0 satisfies the tested MVP hardening criteria: no silent state
advancement, no tested transaction corruption, no duplicate experiment run after
resume, no Lean-policy bypass, explicit human recovery authority, retained negative
evidence, and zero unresolved S4 blockers. The recommendation remains bounded by the
known limitations above.
