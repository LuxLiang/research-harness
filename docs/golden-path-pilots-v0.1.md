# Step 7 — Golden-Path Research Pilot Report

Date: 2026-08-21. Recommendation: `READY_FOR_HARDENING`.

Three corrected controlled pilots reached `WRITING_FINAL_APPROVAL`
(`WAITING_FINAL_APPROVAL`). An earlier MIXED attempt is deliberately retained as
`CANCELLED_BY_USER / FAILED_SUPERSEDED`: it accepted a Claim/Lean semantic mismatch
that a post-run audit classified S4. The corrected MIXED v2 did not reuse that Claim.
Across the three accepted pilots there are zero accepted or unresolved S4 failures.

## 1. Pilot definitions and rationale

| Route | Project | Controlled problem | Rationale |
|---|---|---|---|
| THEORY | `proj-golden-theory-variance` | Prove `(x-m)^2+(y-m)^2=(x-y)^2/2`, where `m=(x+y)/2` | A meaningful elementary identity with mature `ring` support; tests exact Claim, informal proof, Lean, and manuscript alignment without a library-frontier confounder. |
| EXPERIMENT | `proj-golden-experiment-summation` | Compare `sum` and `math.fsum` on four frozen cancellation cases using a Decimal-precision-200 oracle | A small real numerical experiment that stresses protocol locking, exact reference results, negative baselines, raw-run preservation, and writing integrity. |
| MIXED v2 | `proj-golden-mixed-bernoulli-variance-v2` | Formally prove `p(1-p)/2+(1-p)p/2=p(1-p)` over reals, then independently test a separate Bernoulli variance hypothesis at `p={0.2,0.5,0.8}` | Separates the CORE_FORMAL algebraic Claim from the EMPIRICAL Claim while keeping both tracks on one frozen Plan revision. |

The sources are known baselines, not novelty claims: Python `math.fsum`
documentation, mathlib `ring` documentation, and arXiv:1809.03774 on sample
variance. Literature artifacts include locators, immutable search logs, source
identifiers, and explicit `KNOWN`/non-novel assessments.

## 2. Exact workflow execution trace

Every checkpoint, Git commit, retry, pause, block, and gate is listed in each
project's `resources/pilot-report.yaml`. The accepted paths condense to:

```text
THEORY
QUESTION → LITERATURE → FEASIBILITY(PASS) → PLAN → MATERIALIZE
→ theory-development → lean-formalization → theory-verification
→ lean-verification → semantic-alignment-review → JOIN → SYNTHESIS
→ COMPLETION(COMPLETE) → CONSISTENCY(PASS) → WRITING
→ FINAL(PASS) → WAITING_FINAL_APPROVAL

EXPERIMENT
QUESTION → LITERATURE → FEASIBILITY(PASS) → PLAN → MATERIALIZE
→ experiment-design → experiment-execution → experiment-verification
→ JOIN → SYNTHESIS → COMPLETION(COMPLETE) → CONSISTENCY(PASS)
→ WRITING → FINAL(REVISION_REQUIRED) → TARGETED_REVISION
→ FINAL(PASS) → WAITING_FINAL_APPROVAL

MIXED v2
QUESTION → LITERATURE → PAUSE/RESUME → FEASIBILITY(PASS) → PLAN
→ MATERIALIZE → THEORY[development + 4 independent formal checks]
→ EXPERIMENT[design + execution + verification] → JOIN → SYNTHESIS
→ COMPLETION(COMPLETE) → CONSISTENCY(PASS) → WRITING
→ FINAL(REVISION_REQUIRED) → TARGETED_REVISION
→ FINAL(REVISION_REQUIRED) → TARGETED_REVISION
→ FINAL(PASS) → PAUSE/RESUME → WAITING_FINAL_APPROVAL
```

All MIXED v2 track actions used the same ResearchPlan revision 3; Plan revision 4
appeared only after Join/Synthesis. The theory branch completed before experiment
retries and was not replayed. All action sessions were isolated and unique. Resume
continued persisted actions from their frozen input commits rather than creating a
new conversation-derived state.

The archived first MIXED attempt followed the same broad path to final approval,
but its post-run semantic audit found that one Claim combined algebraic and
probabilistic meanings while Lean proved only the algebraic clause. It was cancelled,
not patched in place or waved through.

## 3. Scientific artifacts produced

| Project | Q | Literature | Plan | Claims | Experiments | Reviews | Decisions |
|---|---:|---:|---:|---:|---:|---:|---:|
| THEORY | 1 | 1 | 1 | 1 | 0 | 10 | 4 |
| EXPERIMENT | 1 | 1 | 1 | 1 | 1 | 7 | 3 |
| MIXED v2 | 1 | 1 | 1 | 2 | 1 | 12 | 6 |
| Archived MIXED v1 | 1 | 1 | 1 | 1 | 1 | 11 | 5 |

The formal pilots also contain immutable informal proofs, Lean source, semantic
mappings, environment manifests, clean-build reports, and axiom audits. Experiment
pilots contain code/data/protocol locks, three stable run IDs, raw and failed-output
references, independent recomputation reports, manuscripts, and traceability maps.
The workspace validates 90 canonical artifacts.

## 4. Gate and review outcomes

- Accepted pilots: all feasibility gates `PASS`, completion gates `COMPLETE`,
  consistency gates `PASS`, and final gates eventually `PASS`.
- CORE_FORMAL Claims: THEORY, LEAN, AXIOM_AUDIT, and SEMANTIC_ALIGNMENT reviews all
  pass on the frozen Claim lineage.
- Lean 4.19.0 with mathlib commit
  `c44e0c8ee63ca166450922a373c7409c5d26b00b`: clean builds, no `sorry`/`sorryAx`,
  and only approved reported axioms `propext`, `Classical.choice`, `Quot.sound`.
- EXPERIMENT: `SUPPORTED`; mean naive absolute error `0.5`, `fsum` error `0.0`,
  three strict-improvement cases per run, with all three seeds/raw outputs retained.
- MIXED v2 Experiment: `SUPPORTED`; per-seed maximum errors approximately `0.00375`,
  `0.00130`, `0.001525`, below the preregistered `0.01` limit. The biased baseline
  mean error around `0.0951` is retained as negative evidence.
- Contradictions and negative evidence are valid scientific outcomes and were never
  mapped to runtime failure.

## 5. Human interventions

- Selected the three small known-result problems and immutable benchmark inputs.
- Registered preselected inputs, exact protocols, Python environment, and Lean/mathlib
  environment through Accepted Decisions when isolated Skills correctly rejected
  prompt-only or unpinned facts.
- Explicitly unblocked persisted checkpoints only after the relevant contract or
  missing immutable input was fixed.
- Audited the first MIXED pilot, classified its Claim/Lean mismatch S4, cancelled it,
  and started a corrected v2 definition.
- Inspected final-review blockers and fixed controller/Writer implementation defects;
  no proof, result, threshold, protocol, or Claim statement was altered to pass a gate.
- No canonical YAML was manually edited to drive a transition, and no individual
  research Skill agent was manually invoked.

## 6. Errors discovered

| Severity | Error | Recovery |
|---|---|---|
| S3 | Pilot definitions/protocol/environment existed only in runtime prompts | Failed closed; added revision-pinned resources and Accepted Decisions. |
| S4 | Audit/executor authority wording conflicted with absent proposal tools | Failed closed; made audit and host-handler responsibilities explicit. |
| S3 | ActionCompiler discarded safe Skill-specific search tools | Preserved declared academic search, citation-neighborhood, and resource-read tools. |
| Runtime | Conda `LD_LIBRARY_PATH` polluted Lean's clang | Removed it for the pinned Lean process; clean build then passed. |
| S3 | Retry counter accumulated across distinct successful actions | Reset action retry after every accepted action-scoped event. |
| S4 | EXPERIMENT manuscript described an empirical hypothesis as Lean-proved | Final review now receives pinned manuscript content and enforces Claim-type fidelity. |
| S4 | First MIXED Claim had broader probabilistic meaning than its Lean theorem | Cancelled/archived v1; v2 separates CORE_FORMAL and EMPIRICAL Claims. |
| S3 | Mixed final checker treated coexistence of formal and empirical Claims as overclaiming | Scoped the check to the theorem section and exact empirical statement. |
| S4 | Targeted Writer dropped an already `IN_PAPER` theorem | Next final gate blocked it; Writer now retains `VERIFIED` and `IN_PAPER` formal Claims. |
| Runtime | Interrupted pending action could not resume | Controller now resumes the stable action ID and frozen input commit. |
| Runtime | `status` briefly failed between Skill completion and `TRACK_ADVANCED` | ActionCompiler now reports that checkpoint as a controller-only transition. |

## 7. Skill/evaluation regressions

All listed defects became deterministic or end-to-end regression coverage. Final
verification results:

- Python: 71 tests passed.
- Scientific adversarial evaluation: 46/46 cases passed; S4 failures = 0.
- DeepSeek integration: TypeScript build passed; public-contract integration test passed.
- Artifact validation: 90/90 canonical artifacts valid.

Pilot reports preserve all observed S3/S4 findings after repair. For the three
accepted pilots, accepted S4 failures = 0 and unresolved S3/S4 = 0. The archived
MIXED v1 report records accepted S4 = 1 and `FAILED_SUPERSEDED`; it is not counted
as a successful pilot.

## 8. Fixes made

- Added the golden-pilot runner, CLI, versioned reports, real Codex Skill audits,
  execution metrics, and deterministic Lean/experiment adapters.
- Grounded all pilot inputs and protocols in canonical Decisions/resources.
- Added immutable source snapshots to bounded audit contexts, including resource
  refs embedded in Decision JSON.
- Enforced pinned Lean/mathlib and Python environments, protocol hashes, run IDs,
  raw-output checksums, and independent recomputation.
- Split MIXED v2 into a CORE_FORMAL algebraic Claim and a dependent EMPIRICAL Claim.
- Preserved the common planning snapshot through branch completion and delayed Plan
  mutation until Join.
- Made Writer and final-review checks Claim-type/status aware and revision-safe.
- Added pending-action resume, per-action retry reset, and transient status handling.
- Reconstructed token/runtime metrics from runtime-owned session summaries across
  crash/resume invocations without treating those logs as scientific truth.

## 9. Final pilot result

| Route | Final state | Sessions | Tokens | Runtime | Accepted / unresolved S4 |
|---|---|---:|---:|---:|---:|
| THEORY | `WAITING_FINAL_APPROVAL` | 19 | 430,137 | 735.630 s | 0 / 0 |
| EXPERIMENT | `WAITING_FINAL_APPROVAL` | 20 | 472,588 | 659.938 s | 0 / 0 |
| MIXED v2 | `WAITING_FINAL_APPROVAL` | 27 | 701,295 | 1,123.737 s | 0 / 0 |
| Archived MIXED v1 | `CANCELLED_BY_USER` | 19 | 466,986 | 792.576 s | 1 / 0 (superseded) |

Cost was not exposed by the runtime. Remaining S2 weaknesses concern literature
locator depth, presentation of historical revision lineage, statistical wording,
and manuscript depth. They are hardening inputs, not unresolved integrity blockers.

## 10. Recommendation

`READY_FOR_HARDENING`.

This recommendation is limited to the controlled MVP boundary. The accepted pilots
demonstrate fail-closed deterministic routing, independent formal/protocol checks,
negative-evidence retention, crash/pause resume, and artifact-traceable writing. They
do not establish performance on novel, open-ended, large-scale, expensive, or
formal-library-frontier research problems.
