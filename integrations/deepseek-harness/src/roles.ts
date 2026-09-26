import type { CapabilityPreset } from './contracts.js'

export const ROLE_SKILLS: Record<string, { description: string; content: string }> = {
  'question-framing': {
    description: 'Formalize a precise, scoped, testable research question.',
    content: 'Frame the research problem using only the pinned context. Record gaps, hypotheses, contributions, and risks in a ResearchQuestion proposal. Do not choose workflow transitions.',
  },
  'literature-novelty': {
    description: 'Assess literature evidence, novelty, and feasibility.',
    content: 'Produce structured LiteratureEvidence or Review proposals. Distinguish baseline, extension, contradiction, and equivalence. Never treat a citation alone as evidence analysis.',
  },
  'research-planning': {
    description: 'Plan bounded theory and experiment work using planned outputs.',
    content: 'Produce a ResearchPlan proposal with planned_outputs. Do not create placeholder Claim or Experiment artifacts; those materialize only when execution starts.',
  },
  'theory-development': {
    description: 'Develop formal scientific claims and proof resources.',
    content: 'Develop only the target claims and dependencies in the pinned bundle. Record assumptions, evidence, verification status, and honest failure classifications.',
  },
  'lean-formalization': {
    description: 'Map a frozen core Claim faithfully into Lean.',
    content: 'Preserve assumptions, quantifiers, domains, constants, and conclusions exactly. Stage Lean and mapping resources; revise only Claim verification resource fields.',
  },
  'lean-verification': {
    description: 'Independently build Lean and audit axioms.',
    content: 'Run the locked toolchain, forbid sorry and unapproved axioms, and create LEAN and AXIOM_AUDIT Reviews without changing the target.',
  },
  'semantic-alignment-review': {
    description: 'Audit Claim, informal proof, and Lean semantics.',
    content: 'Compare assumptions, quantifiers, domains, rates, constants, and conclusion strength. Produce a Review only.',
  },
  'theory-verification': {
    description: 'Independently verify claims and proof evidence.',
    content: 'Review the exact claim revision and its direct dependencies. Produce a Review proposal; do not mutate the target or advance global state.',
  },
  'experiment-design': {
    description: 'Preregister a reproducible Experiment protocol.',
    content: 'Lock Claim, data, code, metrics, seeds, ablations, statistics, exclusions, and outcome rules before results are visible.',
  },
  'experiment-execution': {
    description: 'Execute a frozen Experiment faithfully.',
    content: 'Append pinned run records and preserve all raw, failed, negative, and contradictory outputs. Do not interpret outcomes.',
  },
  'experiment-verification': {
    description: 'Independently audit and reproduce an Experiment.',
    content: 'Audit protocol fidelity, fairness, statistics, leakage, and selective reporting. Create an EXPERIMENT Review only.',
  },
  'research-synthesis': {
    description: 'Build explicit Claim-Evidence relationships.',
    content: 'Merge joined theory, experiment, literature, and reviews while preserving contradictions and limitations.',
  },
  'completion-review': {
    description: 'Assess completion against Plan criteria.',
    content: 'Evaluate every preregistered success criterion and return a structured completion candidate without fail-open behavior.',
  },
  'consistency-review': {
    description: 'Check claim-evidence and revision consistency.',
    content: 'Check pinned claim revisions, contradictions, dependency closure, materialized outputs, and unresolved novelty issues. Identify the smallest follow-up target.',
  },
  'targeted-followup': {
    description: 'Plan the smallest work set that resolves blockers.',
    content: 'Preserve satisfied work and add only affected Literature, Theory, Experiment, or Formal planned outputs.',
  },
  'scientific-writing': {
    description: 'Write traceable scientific manuscripts from accepted evidence.',
    content: 'Write only from verified claims, completed experiments, and verified literature. Produce paper resources and traceability; do not propose ScientificClaim changes.',
  },
  'final-review': {
    description: 'Perform final scientific, reproducibility, and editorial review.',
    content: 'Review the pinned project revision and manuscript Git commit. Produce revision-pinned Review artifacts and a structured assessment; never approve by prose alone.',
  },
  'proposal-correctness-review': {
    description: 'Independently audit a frozen proposal for evidence-level correctness.',
    content: 'Separate asserted facts from hypotheses; check citations, logic, mathematics, statistics, identification, data, method fit, feasibility, and conclusion strength. Produce a revision-pinned PROPOSAL_CORRECTNESS Review only.',
  },
  'proposal-revision': {
    description: 'Apply authorized proposal changes as an immutable revision.',
    content: 'Preserve language, section order, and academic voice. Apply minor fixes and only Decision-authorized material changes, write an exact change log, and never overwrite an earlier proposal resource.',
  },
}

export const PRESET_POLICY: Record<CapabilityPreset, {
  sandbox: 'read-only' | 'workspace-write'
  mayWriteScratch: boolean
}> = {
  'research-producer': { sandbox: 'read-only', mayWriteScratch: false },
  'research-reviewer': { sandbox: 'read-only', mayWriteScratch: false },
  'research-theory': { sandbox: 'workspace-write', mayWriteScratch: true },
  'research-formal': { sandbox: 'workspace-write', mayWriteScratch: true },
  'research-formal-reviewer': { sandbox: 'workspace-write', mayWriteScratch: true },
  'research-experiment': { sandbox: 'workspace-write', mayWriteScratch: true },
  'research-experiment-reviewer': { sandbox: 'workspace-write', mayWriteScratch: true },
  'research-writer': { sandbox: 'workspace-write', mayWriteScratch: true },
}
