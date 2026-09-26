import test from 'node:test'
import assert from 'node:assert/strict'
import { Context, Service } from '@deepseek-ai/cordis'
import Loader from '@deepseek-ai/cordis-plugin-loader'
import { researchSessionId } from '../session-id.js'
import { assertGateAssessmentAllowed, assertProposalWriteAllowed, RESEARCH_TOOL_NAMES } from '../tools.js'
import { PRESET_POLICY, ROLE_SKILLS } from '../roles.js'
import {
  gateSubmissionInstruction,
  hasUnexpectedGateAssessment,
  hasValidProposalSourceMap,
  requireSingleFollowupPlanRef,
  shouldRecordActionFailure,
} from '../controller.js'
import { mandatorySkillContract, SHARED_CONTRACT_NAMES } from '../runtime-adapter.js'

test('research session IDs are deterministic per action attempt', () => {
  const input = {
    projectId: 'proj-demo',
    runId: 'run-demo',
    actionId: 'action-demo',
    role: 'theory-development',
    attempt: 0,
  }
  assert.equal(researchSessionId(input), researchSessionId(input))
  assert.notEqual(researchSessionId(input), researchSessionId({ ...input, attempt: 1 }))
})

test('research skill content is a mandatory system contract', () => {
  const content = 'schema_version: research-artifact/v0.1.3'
  const section = mandatorySkillContract(content, [{
    name: 'artifact-boundaries.md', content: 'Canonical promotion is forbidden.',
  }])
  assert.match(section, /MUST follow/)
  assert.match(section, /research-artifact\/v0\.1\.3/)
  assert.match(section, /BEGIN REQUIRED SHARED POLICY: artifact-boundaries\.md/)
  assert.match(section, /Canonical promotion is forbidden/)
})

test('runtime injects the no-waiver theory verification policy', () => {
  assert.ok(SHARED_CONTRACT_NAMES.includes('theory-verification-policy.md'))
})

test('model tool registry excludes canonical promotion and transitions', () => {
  assert.deepEqual([...RESEARCH_TOOL_NAMES].sort(), [
    'academic_search',
    'academic_source_read',
    'citation_neighborhood',
    'citation_validate',
    'manuscript_read',
    'paper_filesystem',
    'research_action_submit',
    'research_artifact_propose',
    'research_artifact_query',
    'research_artifact_read',
    'research_artifact_resolve',
    'research_artifact_validate',
    'research_experiment_run',
    'research_lean_verify',
    'research_proposal_write',
    'resource_read',
    'resource_write',
  ])
  assert.equal(RESEARCH_TOOL_NAMES.some(name => name.includes('promote')), false)
  assert.equal(RESEARCH_TOOL_NAMES.some(name => name.includes('transition')), false)
})

test('MVP roles and write-capability presets are closed registries', () => {
  assert.equal(Object.keys(ROLE_SKILLS).length, 19)
  assert.deepEqual(
    Object.entries(PRESET_POLICY)
      .filter(([, policy]) => policy.sandbox === 'workspace-write')
      .map(([name]) => name)
      .sort(),
    [
      'research-experiment', 'research-experiment-reviewer',
      'research-formal', 'research-formal-reviewer',
      'research-theory', 'research-writer',
    ],
  )
})

test('deprecated combined roles cannot be selected', () => {
  assert.equal(ROLE_SKILLS['experiment-research'], undefined)
  assert.equal(ROLE_SKILLS['synthesis-completion'], undefined)
  assert.equal(ROLE_SKILLS['scientific-writing']?.content.includes('Claim'), true)
})

test('intentional pause/cancel abort does not become ACTION_FAILED', () => {
  const controller = new AbortController()
  assert.equal(shouldRecordActionFailure(controller.signal), true)
  controller.abort(new Error('user pause'))
  assert.equal(shouldRecordActionFailure(controller.signal), false)
})

test('non-gate actions fail closed on gate assessment payloads', () => {
  assert.doesNotThrow(() => assertGateAssessmentAllowed(false, undefined))
  assert.throws(
    () => assertGateAssessmentAllowed(false, '{}'),
    /no gate_contract.*omit gate_assessment_json/,
  )
  assert.doesNotThrow(() => assertGateAssessmentAllowed(true, '{}'))
  assert.equal(hasUnexpectedGateAssessment(null, null), false)
  assert.equal(hasUnexpectedGateAssessment(null, {
    action_id: 'action', bundle_sha256: 'bundle', outcome: 'SUBMITTED', proposal_ids: [],
    gate_assessment: {} as never,
  }), true)
  assert.equal(hasUnexpectedGateAssessment({}, {
    action_id: 'action', bundle_sha256: 'bundle', outcome: 'SUBMITTED', proposal_ids: [],
    gate_assessment: {} as never,
  }), false)
})

test('gate prompt requires a complete GateResult rather than the compact contract', () => {
  const instruction = gateSubmissionInstruction({
    gate_type: 'COMPLETION', allowed_verdicts: ['COMPLETE', 'INCOMPLETE'],
  })
  for (const field of [
    'gate_id', 'gate_type', 'verdict', 'based_on', 'target_refs',
    'target_output_ids', 'review_refs', 'rationale', 'recorded_at',
  ]) assert.match(instruction, new RegExp(`\\b${field}\\b`))
  assert.match(instruction, /not the submission payload shape/)
  assert.match(instruction, /MUST call research_action_submit/)
  assert.match(instruction, /COMPLETE is forbidden/)
  assert.match(instruction, /target_output_ids/)
})

test('proposal structuring can write only canonical JSON source maps', () => {
  assert.doesNotThrow(() => assertProposalWriteAllowed(
    'SOURCE_MAP_ONLY', 'source-maps/proposal-r003.json',
  ))
  assert.throws(() => assertProposalWriteAllowed(
    'SOURCE_MAP_ONLY', 'revisions/rev-002.md',
  ), /not allowed/)
  assert.equal(hasValidProposalSourceMap('proj-review', {
    uri: 'projects/proj-review/resources/proposal/source-maps/proposal-r003.json',
    sha256: 'a'.repeat(64), media_type: 'application/json', description: 'map',
  }), true)
  assert.equal(hasValidProposalSourceMap('proj-review', {
    uri: 'projects/proj-review/resources/proposal/revisions/rev-002.md',
    sha256: 'a'.repeat(64), media_type: 'text/markdown', description: 'map',
  }), false)
})

test('targeted follow-up emits one revision-pinned successor plan ref', () => {
  assert.deepEqual(requireSingleFollowupPlanRef([
    { id: 'plan-successor', kind: 'ResearchPlan', revision: 1 },
  ]), { id: 'plan-successor', kind: 'ResearchPlan', revision: 1 })
  assert.throws(() => requireSingleFollowupPlanRef([]), /exactly one successor/)
  assert.throws(() => requireSingleFollowupPlanRef([
    { id: 'plan-successor', kind: 'ResearchPlan', revision: 1 },
    { id: 'review-extra', kind: 'Review', revision: 1 },
  ]), /exactly one successor/)
})

test('real Cordis loader resolves exports and activates service injection', async () => {
  const ctx = new Context()
  await ctx.plugin(Loader, { baseUrl: import.meta.url })
  const required = [
    'agents', 'agentPresets', 'sessions', 'sessionPersistence', 'subprocess',
    'systemPrompt', 'tools', 'skills', 'sandboxPolicy', 'approval',
  ]
  for (const serviceName of required) {
    const Stub = class extends Service {
      constructor(serviceCtx: Context) {
        super(serviceCtx, serviceName as never)
      }
    }
    await ctx.plugin(Stub)
  }
  const entryId = await ctx.loader.create({
    name: '../index.js',
    config: { workspace: process.cwd(), push: false },
  })
  await ctx.loader.await()
  assert.ok(ctx.loader.resolve(entryId).fiber)
  assert.ok(ctx.get('research'))
  await ctx.fiber.dispose()
})
