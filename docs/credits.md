# Credits and integration baseline

## Research workflow design

The skill design adapts mechanisms documented in
[pAI/MSc (PoggioAI/PoggioAI_MSc)](https://github.com/PoggioAI/PoggioAI_MSc)
at revision `b2118e6f9cc07009da5f22b5816907e1f90cfe03`:

| Research Harness role | Design source |
| --- | --- |
| Question framing | Practical Compass, Rigor & Novelty, Narrative Architect, persona synthesis |
| Literature/novelty | LiteratureReviewAgent and adversarial literature prompt |
| Planning | BrainstormAgent, FormalizeGoalsAgent, ResearchPlanWriteupAgent |
| Theory development/verification | MathLiterature, MathProposer, MathProver, MathRigorousVerifier, MathEmpiricalVerifier |
| Experiment roles | ExperimentDesign, Experimentation, ExperimentVerification |
| Synthesis/completion | TrackMerge, FormalizeResults, verify_completion |
| Consistency/follow-up | DualityCheck, FormalizeGoals follow-up, literature loop |
| Writing/final review | ResourcePreparation, Writeup, Proofreading, ReviewerAgent |

References: [persona prompts](https://github.com/PoggioAI/PoggioAI_MSc/blob/b2118e6f9cc07009da5f22b5816907e1f90cfe03/consortium/prompts/persona_instructions.py),
[literature prompt](https://github.com/PoggioAI/PoggioAI_MSc/blob/b2118e6f9cc07009da5f22b5816907e1f90cfe03/consortium/prompts/literature_review_instructions.py),
[math primer](https://github.com/PoggioAI/PoggioAI_MSc/blob/b2118e6f9cc07009da5f22b5816907e1f90cfe03/MATH_RESEARCH_PRIMER.md),
[workflow graph](https://github.com/PoggioAI/PoggioAI_MSc/blob/b2118e6f9cc07009da5f22b5816907e1f90cfe03/consortium/graph.py),
and [reviewer prompt](https://github.com/PoggioAI/PoggioAI_MSc/blob/b2118e6f9cc07009da5f22b5816907e1f90cfe03/consortium/prompts/reviewer_instructions.py).

Research Harness uses typed artifacts and deterministic gates for handoffs and
routing. It does not inherit fail-open completion, stall-based forced
completion, fixed percentage completion gates, tree search, or campaign mode.
Lean formalization and semantic alignment are framework extensions informed
by proof-transcription traceability principles.

## Model runtime

The optional integration targets **DeepSeek Harness 0.1.0-rc.8**, revision
`141eb6fef83422698aef7a981029e843e8161534`, using its public
[Cordis plugin framework](https://github.com/deepseek-ai/deepseek-harness/blob/141eb6fef83422698aef7a981029e843e8161534/docs/user/develop/framework/index.md)
and host services. This is a compatibility baseline, not a claim of support
for every later release. Dependency versions are pinned in the integration's
package metadata and lockfile.
