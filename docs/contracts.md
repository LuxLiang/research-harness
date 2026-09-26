# Contracts

The JSON schemas in [`schemas/`](../schemas/) and per-skill `skill.yaml` files
in [`skills/`](../integrations/deepseek-harness/skills/) define exact fields,
enums, and permissions. This guide explains their shared rules.

## Artifact versions

| Contract | Scope |
| --- | --- |
| `schemas/v0.1.2/` | Base scientific artifact types |
| `schemas/v0.1.3/` | Proposal-review artifacts and related extensions |
| `schemas/v0.1.4/` | Full-research source input metadata |
| `schemas/orchestrator/v0.1/` | Checkpoints and gate assessments |
| `schemas/runtime/v0.1/` | Contexts, proposals, submissions, routing, budgets, traceability |
| `schemas/evals/v0.1/` | Evaluation cases |

These versions compose the current implementation; they are not package
release numbers. Schema IDs under `https://research-harness.local/schemas/`
are identifiers resolved locally, not network services.

The artifact types are Project, ResearchQuestion, ResearchPlan,
LiteratureEvidence, ScientificClaim, Experiment, Review, Decision, and
ResearchProposal. Their envelopes include identity, kind, schema version,
revision, and provenance; non-Project artifacts carry a project ID.

The semantic validator checks reference existence, kind, project ownership,
revision bounds, dependency cycles, and lifecycle rules in addition to JSON
shape. Planned output IDs are not accepted scientific evidence until the
controller materializes their artifacts.

## Scientific verification

Reviews bind to specific artifact revisions. Editing the reviewed scientific
content requires fresh verification. A claim's verification profile determines
its evidence requirements:

- `ADVERSARIAL`: passing theory review.
- `CORE_FORMAL`: theory, Lean, axiom-audit, and semantic-alignment evidence for
  the same frozen claim revision; no waiver for missing formal evidence.
- `EMPIRICAL`: supporting experiment evidence and consistency checks.

Proving, theory verification, Lean verification, and semantic review use
separate sessions. Formal checks reject `sorry`, `sorryAx`, and unapproved
axioms. A formalization gap is not evidence that the statement is false.
Only the controller promotes a claim after required checks pass.

An experiment locks its protocol before execution: code and data identity,
metrics, seeds, repetitions, exclusions, statistical methods, and outcome
rules. Execution appends pinned runs; independent verification applies the
preregistered rules. Failed, negative, contradictory, and inconclusive outcomes
remain part of the record.

Manuscript claims, results, novelty statements, figures, and tables must map to
accepted evidence through `paper/traceability.yaml`. Writers cannot create or
revise ScientificClaim artifacts. A valid traceability file does not itself
prove the truth of a manuscript.

## Skills and permissions

A skill is a scientific role; an agent session executes one controller-selected
skill. The controller handles routing, human gates, materialization, joins,
and completion. Models cannot self-assign authority.

| Phase | Skill roles |
| --- | --- |
| Discovery and planning | Question framing, literature/novelty, research planning |
| Theory | Development, verification, Lean formalization/verification, semantic alignment |
| Experiment | Design, execution, independent verification |
| Synthesis | Synthesis, completion review, targeted follow-up, consistency review |
| Publication | Scientific writing, final review |
| Proposal review | Correctness review, authorized revision, shared discovery roles |

Permissions combine skill, operation, artifact kind, and field differences.
Reviewers can create Reviews; experiment execution can append runs and status;
formalization can add permitted verification resources. Empty output allowlists
mean no authority. No model tool exposes canonical promotion.

Context bundles have explicit dependency and size bounds (by default 64
artifacts and 512 KiB); overflow fails rather than silently truncating input.
Staged proposals bind action, session, context hash, base revision, candidate,
and proposal hash. Validation occurs against the complete proposed overlay
before promotion. Writer transactions may change only declared paper resources
and the associated checkpoint.

## Runtime boundary

The optional integration uses a versioned JSONL Python sidecar and the host's
public agent, session, tool, prompt, skill, preset, sandbox, and subprocess APIs.
It does not replace the host's agent loop.

The host-only Unix operator socket uses `research-operator/v0.1`. The client
verifies protocol, runtime kind, and workspace identity before sending a
project command. Requests have matching response IDs and are not automatically
replayed after transport failure. The socket lives inside the workspace,
uses mode `0600`, and is not a model tool. See [runtime setup](../integrations/deepseek-harness/README.md).

## Evaluation boundary

Cases in `evals/cases/` specify expected findings and severities from S0
(cosmetic) to S4 (correctness or integrity blocker). Adversarial cases pass when
the expected threat is detected. Missing or unexpected findings and baseline
regressions fail the suite; aggregate scores cannot waive S4 failures.

These checks exercise deterministic integrity rules on fixtures. They do not
measure live model research quality. Baseline changes are policy changes and
must be reviewed alongside case and evaluator changes. See [contributing](../CONTRIBUTING.md)
for commands.
