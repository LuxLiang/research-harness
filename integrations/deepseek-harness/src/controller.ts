import { mkdir } from 'node:fs/promises'
import { join } from 'node:path'
import type { Context } from '@deepseek-ai/cordis'
import { Service } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
import type {
  ActionResult,
  ContextBundle,
  HandlerSubmission,
  JsonValue,
  ProjectInitOptions,
  ResearchRunView,
  ResearchRuntime,
  ResearchStop,
  RunOptions,
  StateAction,
} from './contracts.js'
import { DeepSeekRuntimeAdapter } from './runtime-adapter.js'
import { SidecarClient, SidecarError } from './sidecar-client.js'
import { ResearchOperatorServer } from './operator-server.js'

interface Checkpoint extends Record<string, JsonValue> {
  project_id: string
  run_id: string
  checkpoint_seq: number
  status: string
  state: string
  pending_action: null | { action_id: string; input_git_commit: string; started_at: string }
  retry_counters: Record<string, number>
  branch_states: { theory: string; experiment: string }
  skill_progress: Record<string, {
    track: 'theory' | 'experiment'
    required_skills: string[]
    completed_skills: string[]
    status: string
  }>
}

export interface ControllerConfig {
  workspace: string
  python?: string
  provider?: string
  model?: string
  push?: boolean
  pushAttempts?: number
  sidecarTimeoutMs?: number
  operatorSocket?: string
}

declare module '@deepseek-ai/cordis' {
  interface Context { research: ResearchController }
}

export const DIRECT_EVENTS: Record<string, string> = {
  PROPOSAL_STRUCTURING: 'PROPOSAL_STRUCTURED',
  PROPOSAL_NOVELTY_REVIEW: 'PROPOSAL_NOVELTY_REVIEWED',
  PROPOSAL_PLAN_RECONSTRUCTION: 'PROPOSAL_PLAN_VALIDATED',
  PROPOSAL_CORRECTNESS_REVIEW: 'PROPOSAL_CORRECTNESS_REVIEWED',
  PROPOSAL_REVISION: 'PROPOSAL_REVISION_COMPLETED',
  DISCOVERY_QUESTION: 'QUESTION_ACTIVATED',
  DISCOVERY_LITERATURE: 'LITERATURE_READY',
  PLANNING_DRAFT: 'PLAN_VALIDATED',
  EXECUTION_MATERIALIZE: 'OUTPUTS_MATERIALIZED',
  SYNTHESIS_BUILD: 'SYNTHESIS_COMPLETED',
  FOLLOWUP_TARGETED: 'FOLLOWUP_PLANNED',
  FOLLOWUP_CLAIM_RETHINK: 'CLAIM_REPLACED',
  WRITING_DRAFT: 'WRITING_COMPLETED',
  WRITING_TARGETED_REVISION: 'REVISION_COMPLETED',
}

export const HUMAN_STATES = new Set(['WRITING_FINAL_APPROVAL', 'PROPOSAL_FINAL_APPROVAL'])
export const DECISION_STATES = new Set(['PROPOSAL_WAITING_DECISIONS'])
const STOP_STATES = new Set(['DONE', 'BLOCKED', 'PAUSED_BY_USER', 'CANCELLED_BY_USER'])

class HumanRequiredError extends Error {}
class BudgetRequiredError extends Error {}

export function shouldRecordActionFailure(signal: AbortSignal): boolean {
  return !signal.aborted
}

export function hasUnexpectedGateAssessment(
  gateContract: JsonValue,
  submission: HandlerSubmission | null,
): boolean {
  return gateContract === null && submission?.gate_assessment !== undefined
}

export function gateSubmissionInstruction(gateContract: JsonValue): string {
  if (gateContract === null) {
    return 'This action has no gate_contract. You MUST omit gate_assessment_json from research_action_submit.'
  }
  return [
    `This is a gate action constrained by this pinned gate_contract: ${JSON.stringify(gateContract)}.`,
    'The gate_contract constrains gate_type and allowed verdicts; it is not the submission payload shape.',
    'Submit gate_assessment_json as a complete GateResult with exactly these fields:',
    'gate_id, gate_type, verdict, based_on, target_refs, target_output_ids, review_refs, rationale, recorded_at.',
    'Pin the staged Review in review_refs and based_on, pin the reviewed target when one exists, and use an ISO-8601 recorded_at value.',
    'You MUST call research_action_submit even when the scientific verdict is INCOMPLETE or a typed rethink.',
    'For COMPLETION, COMPLETE is forbidden when the staged Review has any OPEN or ACCEPTED BLOCKER/MAJOR issue; use INCOMPLETE and identify every affected planned output in target_output_ids (or a concrete target artifact in target_refs).',
  ].join(' ')
}

export function hasValidProposalSourceMap(
  projectId: string,
  value: JsonValue | undefined,
): boolean {
  if (value === null || value === undefined || typeof value !== 'object' || Array.isArray(value)) return false
  const ref = value as Record<string, JsonValue>
  return ref.media_type === 'application/json'
    && typeof ref.sha256 === 'string'
    && /^[0-9a-f]{64}$/.test(ref.sha256)
    && typeof ref.uri === 'string'
    && new RegExp(`^projects/${projectId}/resources/proposal/source-maps/proposal-r[0-9]{3}\\.json$`).test(ref.uri)
}

export function requireSingleFollowupPlanRef(
  refs: Array<{ id: string; kind: string; revision: number }>,
): { id: string; kind: 'ResearchPlan'; revision: number } {
  const plans = refs.filter(ref => ref.kind === 'ResearchPlan')
  if (plans.length !== 1 || refs.length !== 1) {
    throw new Error('FOLLOWUP_TARGETED must stage exactly one successor ResearchPlan')
  }
  const plan = plans[0]
  if (!plan.id || !Number.isInteger(plan.revision) || plan.revision < 1) {
    throw new Error('FOLLOWUP_TARGETED successor ResearchPlan ref is invalid')
  }
  return { id: plan.id, kind: 'ResearchPlan', revision: plan.revision }
}

export class ResearchController extends Service implements ResearchRuntime {
  static inject = [
    'agents', 'agentPresets', 'sessions', 'sessionPersistence', 'subprocess',
    'systemPrompt', 'tools', 'skills', 'sandboxPolicy', 'approval',
  ]
  static Config: z<ControllerConfig> = z.object({
    workspace: z.string().required(),
    python: z.string().default('python3'),
    provider: z.string(),
    model: z.string(),
    push: z.boolean().default(false),
    pushAttempts: z.number().default(3),
    sidecarTimeoutMs: z.number().default(300_000),
    operatorSocket: z.string(),
  })

  private readonly sidecar: SidecarClient
  private readonly adapter: DeepSeekRuntimeAdapter
  private readonly tails = new Map<string, Promise<unknown>>()
  private readonly active = new Map<string, AbortController>()

  constructor(ctx: Context, readonly config: ControllerConfig) {
    super(ctx, 'research')
    this.sidecar = new SidecarClient(
      ctx, config.workspace, config.python, config.sidecarTimeoutMs,
    )
    this.adapter = new DeepSeekRuntimeAdapter(ctx, this.sidecar, {
      provider: config.provider,
      model: config.model,
    })
    ctx.effect(() => () => { void this.sidecar.dispose() }, 'research.sidecar')
    if (config.operatorSocket !== undefined) {
      const operator = new ResearchOperatorServer(
        this, config.operatorSocket, config.workspace,
      )
      ctx.effect(async () => {
        await operator.start()
        return async () => await operator.dispose()
      }, 'research.operator')
    }
  }

  async inspect(projectId: string): Promise<ResearchRunView> {
    try {
      const checkpoint = await this.sidecar.call<Checkpoint>(
        'orchestrator.inspect', { project_id: projectId },
      )
      const budget = await this.sidecar.call<Record<string, JsonValue>>(
        'budget.inspect', { project_id: projectId },
      )
      return { checkpoint, budget }
    } catch (error) {
      if (!(error instanceof SidecarError) || !['ARTIFACT_ERROR', 'VALIDATION_FAILED'].includes(error.code)) {
        throw error
      }
      const recovered = await this.sidecar.call<{ checkpoint: Checkpoint }>(
        'orchestrator.fail_closed_recover', {
          project_id: projectId,
          reason: `checkpoint corruption detected: ${error.message}`,
        },
      )
      return { checkpoint: recovered.checkpoint }
    }
  }

  async init(
    projectId: string,
    objective: string,
    options: ProjectInitOptions = {},
  ): Promise<ResearchRunView> {
    const workflowMode = options.workflowMode ?? 'FULL_RESEARCH'
    const result = await this.sidecar.call<{ checkpoint: Checkpoint }>(
      'project.init', {
        project_id: projectId,
        objective,
        workflow_mode: workflowMode,
        ...options.proposalPath === undefined ? {} : { proposal_path: options.proposalPath },
        ...options.rubricPath === undefined ? {} : { rubric_path: options.rubricPath },
        ...options.sourceMaterials === undefined ? {} : { source_materials: options.sourceMaterials },
        commit: true,
      },
    )
    return { checkpoint: result.checkpoint }
  }

  async run(projectId: string, options: RunOptions = {}): Promise<ResearchStop> {
    let view = await this.inspect(projectId)
    if (view.budget?.approved !== true) {
      if (options.budgetPercent === undefined) {
        return { reason: 'WAITING_HUMAN:BUDGET', checkpoint: view.checkpoint, budget: view.budget }
      }
      await this.sidecar.call('budget.approve_initial', {
        project_id: projectId,
        run_id: String(view.checkpoint.run_id),
        budget_percent: options.budgetPercent,
      }, options.signal)
    } else if (view.budget.status === 'WAITING_HUMAN_BUDGET') {
      return { reason: 'WAITING_HUMAN:BUDGET', checkpoint: view.checkpoint, budget: view.budget }
    }
    const maxActions = options.maxActions ?? Number.POSITIVE_INFINITY
    for (let count = 0; count < maxActions; count += 1) {
      const checkpoint = (await this.inspect(projectId)).checkpoint as Checkpoint
      if (STOP_STATES.has(checkpoint.state)) return { reason: checkpoint.state, checkpoint }
      if (DECISION_STATES.has(checkpoint.state)) {
        const template = await this.sidecar.call<{ path: string }>(
          'proposal.write_decision_template', { project_id: projectId }, options.signal,
        )
        return {
          reason: 'WAITING_HUMAN:PROPOSAL_DECISIONS',
          checkpoint,
          decisionFile: template.path,
        }
      }
      if (HUMAN_STATES.has(checkpoint.state)) return { reason: 'WAITING_FINAL_APPROVAL', checkpoint }
      try {
        await this.advance(projectId, options.signal)
      } catch (error) {
        if (error instanceof HumanRequiredError) {
          const waiting = (await this.inspect(projectId)).checkpoint as Checkpoint
          return { reason: 'WAITING_HUMAN', checkpoint: waiting }
        }
        if (error instanceof BudgetRequiredError) {
          view = await this.inspect(projectId)
          return { reason: 'WAITING_HUMAN:BUDGET', checkpoint: view.checkpoint, budget: view.budget }
        }
        throw error
      }
    }
    const checkpoint = (await this.inspect(projectId)).checkpoint
    return { reason: 'ACTION_LIMIT', checkpoint }
  }

  async advance(projectId: string, signal?: AbortSignal): Promise<ActionResult> {
    return await this.serial(projectId, async () => {
      const localAbort = new AbortController()
      const forwardAbort = () => localAbort.abort(signal?.reason)
      signal?.addEventListener('abort', forwardAbort, { once: true })
      this.active.set(projectId, localAbort)
      try {
        for (;;) {
          let checkpoint = await this.sidecar.call<Checkpoint>(
            'orchestrator.inspect', { project_id: projectId }, localAbort.signal,
          )
          if (
            STOP_STATES.has(checkpoint.state)
            || HUMAN_STATES.has(checkpoint.state)
            || DECISION_STATES.has(checkpoint.state)
          ) {
            throw new Error(`research workflow cannot dispatch in ${checkpoint.state}`)
          }
          if (await this.advanceControllerState(checkpoint, localAbort.signal)) continue

          let pending = checkpoint.pending_action
          if (pending === null) {
            const head = await this.gitHead(localAbort.signal)
            const actionId = `${checkpoint.run_id}-${checkpoint.checkpoint_seq + 1}-${checkpoint.state.toLowerCase().replaceAll('_', '-')}`
            const begun = await this.sidecar.call<{ checkpoint: Checkpoint }>(
              'orchestrator.begin_action',
              {
                project_id: projectId,
                action_id: actionId,
                input_git_commit: head,
                expected_seq: checkpoint.checkpoint_seq,
              },
              localAbort.signal,
            )
            checkpoint = begun.checkpoint
            pending = checkpoint.pending_action
          }
          if (pending === null) throw new Error('controller failed to persist pending action')
          const compiled = await this.sidecar.call<{
            action: Record<string, JsonValue>
            bundle: ContextBundle
          }>(
            'action.compile',
            {
              project_id: projectId,
              action_id: pending.action_id,
              input_git_commit: pending.input_git_commit,
            },
            localAbort.signal,
          )
          const bundle = compiled.bundle
          const compiledAction = compiled.action
          const persistedSubmission = await this.sidecar.call<HandlerSubmission | null>(
            'artifact.load_submission',
            { project_id: projectId, action_id: pending.action_id },
            localAbort.signal,
          )
          if (hasUnexpectedGateAssessment(bundle.gate_contract, persistedSubmission)) {
            const reason = 'persisted non-gate action submission contains gate_assessment'
            await this.commitEvent(checkpoint, {
              type: 'ACTION_FAILED',
              payload: {
                action_id: pending.action_id,
                retry_key: 'action',
                retryable: true,
                reason,
              },
            })
            throw new Error(reason)
          }
          let reservation: Record<string, JsonValue>
          try {
            reservation = await this.sidecar.call<Record<string, JsonValue>>(
              'budget.reserve', {
                project_id: projectId,
                action: { ...compiledAction, attempt: checkpoint.retry_counters.action ?? 0 },
              }, localAbort.signal,
            )
          } catch (error) {
            if (error instanceof SidecarError && error.code === 'BUDGET_APPROVAL_REQUIRED') {
              throw new BudgetRequiredError(error.message)
            }
            throw error
          }
          const track = typeof compiledAction.track === 'string' ? compiledAction.track : undefined
          const cwd = this.actionCwd(projectId, pending.action_id, bundle.preset)
          await mkdir(cwd, { recursive: true })
          const action: StateAction = {
            projectId,
            runId: checkpoint.run_id,
            actionId: pending.action_id,
            state: checkpoint.state,
            role: bundle.role,
            preset: bundle.preset,
            targetOutputIds: bundle.target_output_ids,
            attempt: checkpoint.retry_counters.action ?? 0,
            cwd,
            prompt: this.promptFor(
              bundle,
              compiledAction.completion_contract as StateAction['completionContract'],
            ),
            allowedTools: compiledAction.allowed_tools as string[],
            fieldPermissions: compiledAction.field_permissions as JsonValue[],
            completionContract: compiledAction.completion_contract as StateAction['completionContract'],
            modelTier: reservation.model_tier as StateAction['modelTier'],
            modelAlias: String(reservation.model_alias),
            reasoningEffort: String(reservation.reasoning_effort),
            qualityFloor: reservation.quality_floor as StateAction['qualityFloor'],
            routingReason: String(reservation.routing_reason),
            estimatedCostPercent: Number(reservation.estimated_cost_percent),
          }
          let submission: HandlerSubmission
          try {
            submission = await this.adapter.execute(action, bundle, localAbort.signal)
            await this.sidecar.call('budget.consume', {
              project_id: projectId, action_id: action.actionId,
              ...(submission.usage === undefined ? {} : { usage: submission.usage }),
            }, localAbort.signal)
          } catch (error) {
            if (!(error instanceof SidecarError && error.code === 'BUDGET_APPROVAL_REQUIRED')) {
              await this.sidecar.call('budget.consume', {
                project_id: projectId, action_id: action.actionId,
              }).catch(() => undefined)
            }
            // Pause/cancel owns the control event.  Recording ACTION_FAILED on
            // an intentional abort would clear the pending action and can even
            // turn PAUSE into BLOCKED before USER_PAUSE is committed.
            if (shouldRecordActionFailure(localAbort.signal)) {
              await this.commitEvent(checkpoint, {
                type: 'ACTION_FAILED',
                payload: {
                  action_id: action.actionId,
                  retry_key: 'action',
                  retryable: true,
                  reason: error instanceof Error ? error.message : String(error),
                },
              })
            }
            throw error
          }
          try {
            await this.acceptSubmission(checkpoint, action, submission, track, localAbort.signal)
          } catch (error) {
            if (shouldRecordActionFailure(localAbort.signal)) {
              await this.commitEvent(checkpoint, {
                type: 'ACTION_FAILED',
                payload: {
                  action_id: action.actionId,
                  retry_key: 'action',
                  retryable: true,
                  reason: error instanceof Error ? error.message : String(error),
                },
              })
            }
            throw error
          }
          return { action, submission }
        }
      } finally {
        this.active.delete(projectId)
        signal?.removeEventListener('abort', forwardAbort)
      }
    })
  }

  async pause(projectId: string, reason: string): Promise<void> {
    this.active.get(projectId)?.abort(new Error(reason))
    await this.serial(projectId, async () => {
      const checkpoint = (await this.inspect(projectId)).checkpoint as Checkpoint
      await this.commitEvent(checkpoint, { type: 'USER_PAUSE', payload: { reason } })
    })
  }

  async resume(projectId: string): Promise<void> {
    await this.serial(projectId, async () => {
      const checkpoint = (await this.inspect(projectId)).checkpoint as Checkpoint
      await this.commitEvent(checkpoint, { type: 'RESUME', payload: {} })
    })
  }

  async unblock(projectId: string, reason: string): Promise<void> {
    await this.serial(projectId, async () => {
      const checkpoint = (await this.inspect(projectId)).checkpoint as Checkpoint
      if (checkpoint.state !== 'BLOCKED') throw new Error(`unblock is invalid in ${checkpoint.state}`)
      const decision = await this.sidecar.call<{ id: string; revision: number }>(
        'artifact.record_recovery_decision', {
          project_id: projectId,
          reason,
          expected_git_commit: await this.gitHead(),
          actor_id: 'researcher',
        },
      )
      const current = (await this.inspect(projectId)).checkpoint as Checkpoint
      await this.commitEvent(current, {
        type: 'UNBLOCK',
        payload: {
          reason,
          recovery_decision_ref: {
            id: decision.id, kind: 'Decision', revision: decision.revision,
          },
        },
      })
    })
  }

  async cancel(projectId: string, reason: string): Promise<void> {
    this.active.get(projectId)?.abort(new Error(reason))
    await this.serial(projectId, async () => {
      const checkpoint = (await this.inspect(projectId)).checkpoint as Checkpoint
      await this.commitEvent(checkpoint, { type: 'USER_CANCEL', payload: { reason } })
    })
  }

  async approve(projectId: string, reason = 'Human final approval'): Promise<void> {
    await this.serial(projectId, async () => {
      const checkpoint = (await this.inspect(projectId)).checkpoint as Checkpoint
      if (checkpoint.state === 'PLANNING_APPROVAL') {
        await this.sidecar.call('artifact.approve_plan', {
          project_id: projectId,
          expected_git_commit: await this.gitHead(),
          expected_checkpoint_seq: checkpoint.checkpoint_seq,
          actor_id: 'researcher',
          allow_blockers: true,
          commit: true,
        })
        return
      }
      if (checkpoint.state === 'PROPOSAL_FINAL_APPROVAL') {
        await this.commitEvent(checkpoint, {
          type: 'PROPOSAL_FINAL_APPROVED', payload: { reason },
        })
        return
      }
      if (checkpoint.state !== 'WRITING_FINAL_APPROVAL') {
        throw new Error(`approval is invalid in ${checkpoint.state}`)
      }
      await this.commitEvent(checkpoint, { type: 'FINAL_APPROVED', payload: { reason } })
    })
  }

  async approveBudget(projectId: string, additionalPercent: number): Promise<void> {
    await this.serial(projectId, async () => {
      await this.sidecar.call('budget.increase', {
        project_id: projectId, additional_percent: additionalPercent,
        actor_id: 'researcher',
      })
    })
  }

  async proposalDecisions(
    projectId: string,
    decisionsPath: string,
  ): Promise<Record<string, JsonValue>> {
    return await this.serial(projectId, async () => {
      const result = await this.sidecar.call<Record<string, JsonValue>>(
        'proposal.record_decisions', {
          project_id: projectId,
          decisions_path: decisionsPath,
          expected_git_commit: await this.gitHead(),
          actor_id: 'researcher',
        },
      )
      if (result.all_authorized === true) {
        const checkpoint = (await this.inspect(projectId)).checkpoint as Checkpoint
        const decisions = Array.isArray(result.decisions) ? result.decisions : []
        const decisionRefs = decisions.flatMap(value => {
          if (value === null || typeof value !== 'object' || Array.isArray(value)) return []
          const item = value as Record<string, JsonValue>
          if (typeof item.id !== 'string' || typeof item.revision !== 'number') return []
          return [{ id: item.id, kind: 'Decision', revision: item.revision }]
        })
        await this.commitEvent(checkpoint, {
          type: 'PROPOSAL_DECISIONS_RECORDED',
          payload: { decision_refs: decisionRefs },
        })
      }
      return result
    })
  }

  async convertProposal(
    projectId: string,
    derivedProjectId: string,
  ): Promise<Record<string, JsonValue>> {
    return await this.serial(projectId, async () => await this.sidecar.call(
      'proposal.convert', {
        project_id: projectId,
        derived_project_id: derivedProjectId,
        expected_git_commit: await this.gitHead(),
        commit: true,
      },
    ))
  }

  async recordEvent(
    projectId: string,
    type: string,
    payload: Record<string, JsonValue>,
  ): Promise<void> {
    await this.serial(projectId, async () => {
      const checkpoint = (await this.inspect(projectId)).checkpoint as Checkpoint
      await this.commitEvent(checkpoint, { type, payload })
    })
  }

  private async acceptSubmission(
    checkpoint: Checkpoint,
    action: StateAction,
    submission: HandlerSubmission,
    track: string | undefined,
    signal: AbortSignal,
  ): Promise<void> {
    if (submission.action_id !== action.actionId) throw new Error('submission action ID mismatch')
    if (submission.outcome !== 'SUBMITTED') {
      await this.commitEvent(checkpoint, {
        type: 'ACTION_FAILED',
        payload: {
          action_id: action.actionId,
          retry_key: 'action',
          retryable: submission.failure_classification === 'LOCAL_RETRY',
          reason: submission.failure_classification ?? submission.outcome,
        },
      })
      return
    }
    let event: Record<string, unknown>
    if (submission.gate_assessment !== undefined) {
      // The scientific transaction validates the gate only after installing
      // its staged Review overlay; pre-promotion validation would see a
      // dangling review_ref and incorrectly reject a valid atomic action.
      event = {
        type: 'GATE_RECORDED',
        payload: { action_id: action.actionId, gate: submission.gate_assessment },
      }
    } else if (checkpoint.state === 'EXECUTION_TRACKS') {
      if (track === undefined) throw new Error('track action has no selected track')
      if (action.targetOutputIds.length !== 1) {
        throw new Error('track Skill action has no target planned output')
      }
      const validation = await this.sidecar.call<{
        artifacts: string[]
        assessments: Array<{
          artifact_id: string
          scheme: string
          outcome: string
          target_ref?: { id: string; kind: string; revision: number }
        }>
      }>(
        'artifact.validate_action',
        {
          project_id: checkpoint.project_id,
          action_id: action.actionId,
          proposal_ids: submission.proposal_ids,
        },
        signal,
      )
      const theoryOutcomes = validation.assessments
        .filter(item => item.scheme === 'THEORY')
        .map(item => item.outcome)
      const eventType = action.role === 'theory-verification'
        && theoryOutcomes.length > 0
        && !theoryOutcomes.includes('PASS')
        ? 'SKILL_REVISION_REQUIRED'
        : 'SKILL_COMPLETED'
      event = {
        type: eventType,
        payload: {
          action_id: action.actionId,
          output_id: action.targetOutputIds[0],
          skill_id: action.role,
          artifact_ids: validation.artifacts,
          verified_target_ref: action.role === 'theory-verification'
            ? validation.assessments.find(item => item.scheme === 'THEORY')?.target_ref
            : undefined,
        },
      }
    } else if (checkpoint.state === 'FOLLOWUP_TARGETED') {
      const validation = await this.sidecar.call<{
        artifact_refs: Array<{ id: string; kind: string; revision: number }>
      }>(
        'artifact.validate_action',
        {
          project_id: checkpoint.project_id,
          action_id: action.actionId,
          proposal_ids: submission.proposal_ids,
        },
        signal,
      )
      event = {
        type: 'FOLLOWUP_PLANNED',
        payload: {
          action_id: action.actionId,
          plan_ref: requireSingleFollowupPlanRef(validation.artifact_refs),
        },
      }
    } else {
      const type = DIRECT_EVENTS[checkpoint.state]
      if (type === undefined) throw new Error(`no deterministic completion event for ${checkpoint.state}`)
      event = { type, payload: { action_id: action.actionId } }
    }
    const head = await this.gitHead(signal)
    const promoted = await this.sidecar.call<{ push_pending: boolean; push_error?: string }>(
      'artifact.promote_action',
      {
        project_id: checkpoint.project_id,
        action_id: action.actionId,
        expected_git_commit: head,
        expected_checkpoint_seq: checkpoint.checkpoint_seq,
        event,
        commit: true,
        push: this.config.push ?? false,
        push_attempts: this.config.pushAttempts ?? 3,
      },
      signal,
    )
    // A local Git commit is the scientific commit point. Remote replication is
    // best-effort and never invalidates an accepted local transition.
  }

  private async advanceControllerState(checkpoint: Checkpoint, signal: AbortSignal): Promise<boolean> {
    if (checkpoint.state === 'PROPOSAL_NOVELTY_REVIEW') {
      const proposals = await this.sidecar.call<Array<Record<string, JsonValue>>>(
        'artifact.query',
        { project_id: checkpoint.project_id, kind: 'ResearchProposal' },
        signal,
      )
      if (proposals.length !== 1 || typeof proposals[0].id !== 'string') {
        throw new Error('proposal review project must have exactly one ResearchProposal')
      }
      const loaded = await this.sidecar.call<Record<string, JsonValue>>(
        'artifact.read', { artifact_id: proposals[0].id }, signal,
      )
      const proposal = loaded.artifact
      if (proposal === null || typeof proposal !== 'object' || Array.isArray(proposal)) {
        throw new Error('ResearchProposal payload is invalid')
      }
      if (!hasValidProposalSourceMap(
        checkpoint.project_id,
        (proposal as Record<string, JsonValue>).source_map,
      )) {
        await this.commitEvent(checkpoint, {
          type: 'PROPOSAL_STRUCTURING_REQUIRED',
          payload: { reason: 'active proposal revision has no valid JSON field-level source_map' },
        })
        return true
      }
    }
    if (checkpoint.state === 'PROPOSAL_REVIEW_GATE') {
      const gate = await this.sidecar.call<Record<string, JsonValue>>(
        'proposal.evaluate_gate', { project_id: checkpoint.project_id }, signal,
      )
      await this.commitEvent(checkpoint, { type: 'GATE_RECORDED', payload: { gate } })
      const current = (await this.inspect(checkpoint.project_id)).checkpoint as Checkpoint
      if (current.state === 'PROPOSAL_WAITING_DECISIONS') {
        await this.sidecar.call(
          'proposal.write_decision_template',
          { project_id: checkpoint.project_id },
          signal,
        )
      }
      return true
    }
    if (checkpoint.state === 'PLANNING_APPROVAL') {
      const head = await this.gitHead(signal)
      try {
        await this.sidecar.call('artifact.approve_plan', {
          project_id: checkpoint.project_id,
          expected_git_commit: head,
          expected_checkpoint_seq: checkpoint.checkpoint_seq,
          commit: true,
        }, signal)
      } catch (error) {
        if (error instanceof SidecarError && error.code === 'VALIDATION_FAILED') {
          const details = error.details as { blocking_reviews?: unknown } | null
          if (details?.blocking_reviews !== undefined) throw new HumanRequiredError(error.message)
        }
        throw error
      }
      return true
    }
    if (checkpoint.state === 'PLANNING_ROUTING') {
      const head = await this.gitHead(signal)
      const route = await this.sidecar.call<{ snapshot: Record<string, unknown> }>(
        'orchestrator.prepare_route',
        { project_id: checkpoint.project_id, git_commit: head },
        signal,
      )
      await this.commitEvent(checkpoint, { type: 'ROUTE_SELECTED', payload: route })
      return true
    }
    if (checkpoint.state === 'EXECUTION_MATERIALIZE') {
      const head = await this.gitHead(signal)
      const result = await this.sidecar.call<{ push_pending: boolean; push_error?: string }>(
        'artifact.materialize_outputs',
        {
          project_id: checkpoint.project_id,
          action_id: `${checkpoint.run_id}-${checkpoint.checkpoint_seq + 1}-materialize`,
          expected_git_commit: head,
          expected_checkpoint_seq: checkpoint.checkpoint_seq,
          commit: true,
          push: this.config.push ?? false,
          push_attempts: this.config.pushAttempts ?? 3,
        },
        signal,
      )
      return true
    }
    if (checkpoint.state === 'EXECUTION_TRACKS') {
      for (const track of ['theory', 'experiment'] as const) {
        const status = checkpoint.branch_states[track]
        const next = this.nextControllerTrackStatus(checkpoint, track, status)
        if (next !== undefined) {
          await this.commitEvent(checkpoint, {
            type: 'TRACK_ADVANCED', payload: { track, status: next },
          })
          return true
        }
      }
      if (this.allTracksComplete(checkpoint)) {
        await this.commitEvent(checkpoint, { type: 'TRACKS_JOINED', payload: {} })
        return true
      }
    }
    if (checkpoint.state === 'EXECUTION_JOIN') {
      await this.commitEvent(checkpoint, { type: 'JOIN_COMPLETED', payload: {} })
      return true
    }
    return false
  }

  private async commitEvent(
    checkpoint: Checkpoint,
    event: Record<string, unknown>,
    replicate = true,
  ): Promise<void> {
    const head = await this.gitHead()
    await this.sidecar.call('orchestrator.commit_event', {
      project_id: checkpoint.project_id,
      event,
      expected_git_commit: head,
      expected_seq: checkpoint.checkpoint_seq,
      commit: true,
    })
    if (replicate) await this.ensureRemote()
  }

  private async ensureRemote(signal?: AbortSignal): Promise<void> {
    if (!(this.config.push ?? false)) return
    const result = await this.sidecar.call<{
      push_pending: boolean
      push_error?: string
    }>('workspace.push', { attempts: this.config.pushAttempts ?? 3 }, signal)
    // Replication failures remain observable but do not stop scientific work.
  }

  private async gitHead(signal?: AbortSignal): Promise<string> {
    const value = await this.sidecar.call<{ git_commit: string }>('workspace.git_head', {}, signal)
    return value.git_commit
  }

  private selectedTrack(checkpoint: Checkpoint): string | undefined {
    const theory = checkpoint.branch_states.theory
    if (!['NOT_SELECTED', 'COMPLETED', 'WAIVED'].includes(theory)) {
      return 'theory'
    }
    const experiment = checkpoint.branch_states.experiment
    if (!['NOT_SELECTED', 'COMPLETED', 'WAIVED'].includes(experiment)) return 'experiment'
    return undefined
  }

  private nextControllerTrackStatus(
    checkpoint: Checkpoint,
    track: 'theory' | 'experiment',
    status: string,
  ): string | undefined {
    if (status === 'PENDING') return track === 'theory' ? 'DEVELOP' : 'PREPARE'
    const skillsByPhase: Record<string, Set<string>> = {
      'theory:DEVELOP': new Set(['theory-development']),
      'theory:VERIFY': new Set([
        'theory-verification', 'lean-formalization', 'lean-verification',
        'semantic-alignment-review',
      ]),
      'experiment:PREPARE': new Set(['experiment-design']),
      'experiment:RUN': new Set(['experiment-execution']),
      'experiment:ASSESS': new Set(['experiment-verification']),
    }
    const phaseSkills = skillsByPhase[`${track}:${status}`]
    if (phaseSkills === undefined) return undefined
    const relevant = Object.values(checkpoint.skill_progress).filter(item =>
      item.track === track && item.required_skills.some(skill => phaseSkills.has(skill)),
    )
    const phaseComplete = relevant.length > 0 && relevant.every(item =>
      item.required_skills
        .filter(skill => phaseSkills.has(skill))
        .every(skill => item.completed_skills.includes(skill)),
    )
    if (!phaseComplete) return undefined
    if (track === 'theory') return status === 'DEVELOP' ? 'VERIFY' : 'COMPLETED'
    if (status === 'PREPARE') return 'RUN'
    if (status === 'RUN') return 'ASSESS'
    if (status === 'ASSESS') return 'COMPLETED'
    return undefined
  }

  private allTracksComplete(checkpoint: Checkpoint): boolean {
    const selected = Object.values(checkpoint.branch_states).filter(status => status !== 'NOT_SELECTED')
    return selected.length > 0 && selected.every(status => ['COMPLETED', 'WAIVED'].includes(status))
  }

  private actionCwd(projectId: string, actionId: string, preset: string): string {
    if (preset === 'research-writer') return join(this.config.workspace, 'projects', projectId, 'paper')
    return join(this.config.workspace, '.harness', 'research', 'actions', projectId, actionId)
  }

  private promptFor(
    bundle: ContextBundle,
    completion: StateAction['completionContract'],
  ): string {
    const productionContract = bundle.role === 'scientific-writing'
      ? 'Write only paper-scoped resources, validate paper/traceability.yaml, then submit with no artifact proposals.'
      : 'Stage complete artifact proposals, validate them, then call research_action_submit.'
    const gateContract = gateSubmissionInstruction(bundle.gate_contract)
    return [
      `Execute research action ${bundle.action_id} for state ${bundle.state}.`,
      `Use the registered ${bundle.role} skill and only the pinned context bundle.`,
      `Completion contract: ${JSON.stringify(completion)}.`,
      productionContract,
      gateContract,
      'Your natural-language final response is not a workflow event.',
    ].join('\n')
  }

  private async serial<T>(projectId: string, operation: () => Promise<T>): Promise<T> {
    const previous = this.tails.get(projectId) ?? Promise.resolve()
    let release!: () => void
    const current = new Promise<void>(resolve => { release = resolve })
    const tail = previous.then(() => current)
    this.tails.set(projectId, tail)
    await previous
    try { return await operation() } finally {
      release()
      if (this.tails.get(projectId) === tail) this.tails.delete(projectId)
    }
  }
}
