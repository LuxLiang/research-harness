import type { Context } from '@deepseek-ai/cordis'
import { readFile } from 'node:fs/promises'
import { createUserMessage } from '@deepseek-ai/dsh-llm'
import type {} from '@deepseek-ai/dsh-agent-presets'
import type {} from '@deepseek-ai/dsh-session-persistence'
import type {} from '@deepseek-ai/dsh-skill'
import { setSandboxMode } from '@deepseek-ai/dsh-sandbox-policy'
import { setApprovalPolicy } from '@deepseek-ai/dsh-user-approval'
import { installModelSelection, type AgentHandle } from '@deepseek-ai/dsh-agent'
import type { ContextBundle, HandlerSubmission, RuntimeAdapter, StateAction } from './contracts.js'
import { PRESET_POLICY, ROLE_SKILLS } from './roles.js'
import { researchSessionId } from './session-id.js'
import type { SidecarClient } from './sidecar-client.js'
import { registerResearchTools } from './tools.js'

declare module '@deepseek-ai/dsh-llm' {
  interface MessageSourceMap {
    'research-action': {
      readonly kind: 'research-action'
      readonly projectId: string
      readonly actionId: string
    }
  }
}

export function mandatorySkillContract(
  skillContent: string,
  sharedContracts: readonly { name: string; content: string }[] = [],
): string {
  const shared = sharedContracts.map(item =>
    `\n\n--- BEGIN REQUIRED SHARED POLICY: ${item.name} ---\n${item.content}\n--- END REQUIRED SHARED POLICY: ${item.name} ---`,
  ).join('')
  return `You MUST follow this registered research skill contract in full:\n\n${skillContent}${shared}`
}

export const SHARED_CONTRACT_NAMES = [
  'artifact-boundaries.md',
  'failure-taxonomy.md',
  'scientific-evaluation-policy.md',
  'theory-verification-policy.md',
] as const

export class DeepSeekRuntimeAdapter implements RuntimeAdapter {
  constructor(
    private readonly ctx: Context,
    private readonly sidecar: SidecarClient,
    private readonly agentOptions: { provider?: string; model?: string } = {},
  ) {}

  async execute(
    action: StateAction,
    bundle: ContextBundle,
    signal?: AbortSignal,
  ): Promise<HandlerSubmission> {
    if (bundle.action_id !== action.actionId || bundle.project_id !== action.projectId) {
      throw new Error('state action does not match its context bundle')
    }
    if (bundle.role !== action.role || bundle.preset !== action.preset) {
      throw new Error('controller role/preset differs from the bounded context policy')
    }
    const skill = ROLE_SKILLS[action.role]
    if (skill === undefined) throw new Error(`unregistered research role: ${action.role}`)
    const skillContent = await readFile(
      new URL(`../skills/${action.role}/SKILL.md`, import.meta.url),
      'utf8',
    )
    const sharedContracts = await Promise.all(SHARED_CONTRACT_NAMES.map(async name => ({
      name,
      content: await readFile(new URL(`../skills/_shared/${name}`, import.meta.url), 'utf8'),
    })))
    const policy = PRESET_POLICY[action.preset]
    const sessionId = researchSessionId({
      projectId: action.projectId,
      runId: action.runId,
      actionId: action.actionId,
      role: action.role,
      attempt: action.attempt,
    })
    const setup = async (agentCtx: Context) => {
      if (this.agentOptions.provider !== undefined) {
        agentCtx.effect(() => installModelSelection(agentCtx, {
          current: {
            provider: this.agentOptions.provider!, model: action.modelAlias,
            reasoningEffort: action.reasoningEffort as never,
          },
          assembled: undefined,
        }), 'research.model-selection')
      }
      await this.ctx.agentPresets.mount(agentCtx, action.preset)
      // Cordis 4 requires service reads to occur inside a fiber that explicitly
      // declares those services. Agent setup receives a scoped Context rather
      // than inheriting the controller fiber's injection authorization.
      await agentCtx.inject(['skills', 'systemPrompt', 'tools'], scopedCtx => {
        scopedCtx.skills.register({
          name: action.role,
          description: skill.description,
          content: skillContent,
          source: 'bundled',
          provider: 'research-harness',
          invocation: { modelInvocable: true, userInvocable: false },
        })
        scopedCtx.systemPrompt.section({
          name: 'research:authority-boundary',
          order: 20,
          text: 'Canonical research YAML and the pinned context bundle are scientific truth. Session history is execution truth only. You may stage proposals and submit structured results, but you cannot promote artifacts or choose global state transitions.',
        })
        // A research role is a mandatory execution contract, not an optional
        // model-selected helper. Register it for provenance/introspection and
        // also inject the full content into the system prompt so an agent
        // cannot skip the schema, evidence, or submission rules by declining
        // to invoke the skill.
        scopedCtx.systemPrompt.section({
          name: `research:role-contract:${action.role}`,
          order: 30,
          text: mandatorySkillContract(skillContent, sharedContracts),
        })
        scopedCtx.systemPrompt.context({
          name: `research:context:${action.actionId}`,
          order: 50,
          text: JSON.stringify(bundle),
        })
        registerResearchTools(scopedCtx, this.sidecar, {
          projectId: action.projectId,
          actionId: action.actionId,
          bundleSha256: bundle.bundle_sha256,
          proposerSessionId: sessionId,
          allowedTools: action.allowedTools,
          allowGateAssessment: bundle.gate_contract !== null,
          proposalWritePolicy: action.role === 'question-framing'
            ? 'SOURCE_MAP_ONLY'
            : action.role === 'proposal-revision' ? 'REVISION' : 'DENY',
        })
      })
      // Canonical promotion is deliberately absent. Inherited filesystem and
      // shell capabilities remain bounded by the preset, action cwd, sandbox,
      // and approval=never; tools.restrict() is not an authority boundary.
    }

    const persisted = await this.ctx.sessionPersistence.list(signal)
    let handle: AgentHandle
    if (persisted.some(header => header.id === sessionId)) {
      handle = await this.ctx.agents.resume({
        resumeSessionId: sessionId,
        agentOptions: { ...this.agentOptions, model: action.modelAlias },
        signal,
        setup,
      })
    } else {
      handle = await this.ctx.agents.create({
        sessionId,
        meta: { cwd: action.cwd, agentPreset: action.preset },
        agentOptions: { ...this.agentOptions, model: action.modelAlias },
        signal,
        setup,
      })
    }

    const cancel = () => handle.agent.cancel({ kind: 'user' })
    signal?.addEventListener('abort', cancel, { once: true })
    try {
      setApprovalPolicy(handle.agent.session, 'never')
      setSandboxMode(handle.agent.session, policy.sandbox)
      handle.agent.followup(createUserMessage({
        content: [{ type: 'text', text: action.prompt }],
        source: {
          kind: 'research-action',
          projectId: action.projectId,
          actionId: action.actionId,
        },
      }))
      await handle.agent.whenIdle()
      const flushed = await this.ctx.sessions.flush(handle.agent.session)
      if (!flushed) throw new Error('research action session could not be persisted')
      let submission = await this.sidecar.call<Record<string, unknown> | null>(
        'artifact.load_submission',
        { project_id: action.projectId, action_id: action.actionId },
        signal,
      )
      // Execution agents occasionally stage the required append-only terminal
      // run and then end without the final submit tool call.  The host may
      // recover exactly one such proposal: artifact.submit_action performs the
      // full permission, revision, protocol-immutability, and schema checks.
      // Never apply this convenience to design, review, or theory roles.
      if (submission === null && action.role === 'experiment-execution') {
        const staged = await this.sidecar.call<Array<{ proposal_id?: string }>>(
          'artifact.list_proposals',
          { project_id: action.projectId, action_id: action.actionId },
          signal,
        )
        const proposalIds = staged
          .map(item => item.proposal_id)
          .filter((item): item is string => typeof item === 'string')
        if (proposalIds.length === 1) {
          submission = await this.sidecar.call<Record<string, unknown>>(
            'artifact.submit_action',
            {
              project_id: action.projectId,
              action_id: action.actionId,
              bundle_sha256: bundle.bundle_sha256,
              proposal_ids: proposalIds,
              outcome: 'SUBMITTED',
            },
            signal,
          )
        }
      }
      if (submission === null) {
        throw new Error('handler ended without research_action_submit')
      }
      const usage = { inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0, reasoningTokens: 0 }
      let sawUsage = false
      for (const event of handle.agent.session.events) {
        if (event.type !== 'assistant/message' || event.data.usage === undefined) continue
        sawUsage = true
        usage.inputTokens += event.data.usage.inputTokens
        usage.outputTokens += event.data.usage.outputTokens
        usage.cacheReadTokens += event.data.usage.cacheReadTokens ?? 0
        usage.cacheWriteTokens += event.data.usage.cacheWriteTokens ?? 0
        usage.reasoningTokens += event.data.usage.reasoningTokens ?? 0
      }
      return {
        action_id: String(submission.action_id),
        bundle_sha256: String(submission.bundle_sha256),
        proposal_ids: submission.proposal_ids as string[],
        outcome: submission.outcome as HandlerSubmission['outcome'],
        ...submission.gate_assessment === undefined ? {} : {
          gate_assessment: submission.gate_assessment as HandlerSubmission['gate_assessment'],
        },
        ...submission.failure_classification === undefined ? {} : {
          failure_classification: submission.failure_classification as HandlerSubmission['failure_classification'],
        },
        ...(sawUsage ? { usage } : {}),
      }
    } finally {
      signal?.removeEventListener('abort', cancel)
      await handle.dispose()
    }
  }
}
