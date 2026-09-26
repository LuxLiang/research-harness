# Artifact Schema v0.1.2

v0.1.2 is the Step 4 compatibility release. It retains all eight v0.1.1
artifact kinds and the planned-output/materialization split. The JSON Schemas
in `schemas/v0.1.2/` are authoritative.

Changes from v0.1.1:

- Planned ScientificClaim outputs require `verification_profile` with value
  `ADVERSARIAL`, `CORE_FORMAL`, or `EMPIRICAL`. Non-Claim outputs cannot carry
  the field.
- Review supports machine-readable `assessment` and immutable
  `evidence_resources`. Schemes are NOVELTY, THEORY, LEAN, AXIOM_AUDIT,
  SEMANTIC_ALIGNMENT, EXPERIMENT, COMPLETION, CONSISTENCY, and FINAL.
- Experiment requires a preregistered interpretation plan. READY and later
  revisions require a protocol lock; every run pins protocol, code,
  data, configuration, and environment and records deviations. The
  interpretation plan explicitly locks seeds/repetitions, hyperparameter
  budget, ablations, statistical procedure, exclusions, and all three outcome
  rules before results are visible.
- Claim evidence, verification reviews, and verification resources must be
  revision- or checksum-pinned.

Verification profiles are semantic gates:

- ADVERSARIAL requires a passing THEORY Review.
- CORE_FORMAL requires passing THEORY, LEAN, AXIOM_AUDIT, and
  SEMANTIC_ALIGNMENT Reviews against one frozen Claim revision.
- EMPIRICAL requires a supporting EXPERIMENT Review and passing CONSISTENCY
  Review.

Agent proposals cannot promote a Claim to VERIFIED or IN_PAPER. Those status
changes remain host-controlled deterministic transactions after the required
review set validates.
