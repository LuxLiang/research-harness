# Scientific Evaluation Policy

Evaluate scientific behavior with deterministic ground-truth fixtures and
adversarial integrity cases, not writing style or model preference.

Use these severities:

- `S0`: cosmetic only.
- `S1`: minor defect with no scientific effect.
- `S2`: scientific weakness that limits confidence.
- `S3`: major scientific error requiring revision.
- `S4`: correctness or integrity blocker.

Treat fabricated citations, false verified theorems, forbidden Lean axioms,
semantic weakening, protocol manipulation, hidden contradiction, and
unsupported manuscript claims as `S4`. A caught adversarial S4 threat is a
passing evaluation case; an S4 evaluation failure means the expected blocker
was not detected. Accept a Skill release only when S4 evaluation failures are
zero. Never offset an S4 regression with an average score.

Run the relevant domain cases for every Skill change and the complete suite
before release. Compare with the committed baseline. Reject missing formerly
passing cases, newly failing cases, and increased finding severity unless the
baseline is deliberately reviewed and versioned.
