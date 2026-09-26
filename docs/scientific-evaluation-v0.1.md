# Scientific Evaluation v0.1

Step 5 evaluates Skill behavior with structured ground-truth fixtures and
deterministic integrity checks. It does not score prose style or ask another
model which prompt it prefers.

The suite covers literature novelty, informal theory, Lean integrity and
semantic alignment, experiment protocol integrity, synthesis/writing
traceability, permission boundaries, controller authority, interruption
recovery, and THEORY/EXPERIMENT/MIXED synchronization. Cases live in
`evals/cases/`; executable evaluators live in `research_evals/`.

Severity is `S0` cosmetic, `S1` minor, `S2` scientific weakness, `S3` major
scientific error, and `S4` correctness/integrity blocker. An adversarial case
passes when the expected threat is detected with the expected severity. A
release fails if an expected blocker is missed, a new finding appears, a
finding severity changes unexpectedly, or the committed regression baseline
regresses. S4 evaluation failures must be zero; aggregate scores cannot waive
them.

Run the complete release gate with:

```bash
python -m research_evals --workspace . \
  --baseline evals/baselines/step5-v0.1.json
python -m unittest discover -s tests -v
```

Run a domain after changing a related Skill:

```bash
python -m research_evals --workspace . --domain lean
python -m research_evals --workspace . --domain experiment
```

Baseline changes are reviewable scientific-policy changes. Generate one only
from a fully passing suite, inspect the diff, and version it with its cases and
evaluator implementation.
