# Theory verification policy

- Use a separate session for development, adversarial verification, Lean verification, and semantic alignment.
- `ADVERSARIAL` requires an accepting THEORY Review.
- `CORE_FORMAL` requires accepting THEORY, LEAN, AXIOM_AUDIT, and SEMANTIC_ALIGNMENT Reviews against one frozen pre-verification Claim revision.
- Forbid `sorry`, `sorryAx`, and axioms outside the project allowlist. CORE_FORMAL has no waiver.
- Treat library and formalization gaps as targeted gaps, not proof that the claim is false.
- Create a replacement Claim after any substantive statement, assumption, quantifier, domain, rate, constant, or conclusion change; restart verification.
