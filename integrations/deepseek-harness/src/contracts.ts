export const SIDECAR_PROTOCOL = 'research-sidecar/v0.1' as const

export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue }

export interface ContextBundle {
  bundle_version: 'research-context/v0.1'
  project_id: string
  run_id: string
  action_id: string
  state: string
  role: string
  preset: CapabilityPreset
  input_git_commit: string
  artifacts: JsonValue[]
  direct_dependencies: JsonValue[]
  target_output_ids: string[]
  allowed_outputs: {
    artifact_kinds: string[]
    artifact_ids: string[]
    create_ids: string[]
    rules: Array<{ kind: string; operations: Array<'CREATE' | 'REVISE'>; fields: string[] }>
  }
  gate_contract: JsonValue
  limits: { max_artifacts: number; max_serialized_bytes: number }
  bundle_sha256: string
}

export type CapabilityPreset =
  | 'research-producer'
  | 'research-reviewer'
  | 'research-theory'
  | 'research-formal'
  | 'research-formal-reviewer'
  | 'research-experiment'
  | 'research-experiment-reviewer'
  | 'research-writer'

export interface StateAction {
  projectId: string
  runId: string
  actionId: string
  state: string
  role: string
  preset: CapabilityPreset
  targetOutputIds: string[]
  attempt: number
  cwd: string
  prompt: string
  allowedTools: string[]
  fieldPermissions: JsonValue[]
  completionContract: {
    conditions: string[]
    failure_classes: string[]
    forbidden_behaviors: string[]
  }
  modelTier: 'economy' | 'balanced' | 'frontier'
  modelAlias: string
  reasoningEffort: string
  qualityFloor: 'economy' | 'balanced' | 'frontier'
  routingReason: string
  estimatedCostPercent: number
}

export interface GateAssessment {
  gate_id: string
  gate_type: string
  verdict: string
  based_on: JsonValue[]
  target_refs: JsonValue[]
  target_output_ids: string[]
  review_refs: JsonValue[]
  rationale: string
  recorded_at: string
}

export interface HandlerSubmission {
  action_id: string
  bundle_sha256: string
  proposal_ids: string[]
  gate_assessment?: GateAssessment
  outcome: 'SUBMITTED' | 'NO_CHANGE' | 'FAILED'
  failure_classification?:
    | 'LOCAL_RETRY'
    | 'INCOMPLETE'
    | 'RETHINK_CLAIM'
    | 'RETHINK_PLAN'
    | 'RETHINK_QUESTION'
  usage?: {
    inputTokens: number
    outputTokens: number
    cacheReadTokens?: number
    cacheWriteTokens?: number
    reasoningTokens?: number
  }
}

export interface SidecarErrorValue {
  code: string
  message: string
  details: JsonValue
  retryable: boolean
}

export interface SidecarResponse<T> {
  protocol: typeof SIDECAR_PROTOCOL
  request_id: string
  ok: boolean
  result?: T
  error?: SidecarErrorValue
}

export interface RunOptions {
  signal?: AbortSignal
  maxActions?: number
  budgetPercent?: number
}

export interface ProjectInitOptions {
  workflowMode?: 'FULL_RESEARCH' | 'PROPOSAL_REVIEW'
  proposalPath?: string
  rubricPath?: string
  sourceMaterials?: string[]
}

export interface ResearchRunView { checkpoint: Record<string, JsonValue>; budget?: Record<string, JsonValue> }
export interface ResearchStop {
  reason: string
  checkpoint: Record<string, JsonValue>
  budget?: Record<string, JsonValue>
  decisionFile?: string
}
export interface ActionResult { action: StateAction; submission: HandlerSubmission }

export interface ResearchRuntime {
  init(projectId: string, objective: string, options?: ProjectInitOptions): Promise<ResearchRunView>
  inspect(projectId: string): Promise<ResearchRunView>
  run(projectId: string, options?: RunOptions): Promise<ResearchStop>
  advance(projectId: string, signal?: AbortSignal): Promise<ActionResult>
  pause(projectId: string, reason: string): Promise<void>
  resume(projectId: string): Promise<void>
  unblock(projectId: string, reason: string): Promise<void>
  cancel(projectId: string, reason: string): Promise<void>
  approve(projectId: string, reason?: string): Promise<void>
  approveBudget(projectId: string, additionalPercent: number): Promise<void>
  proposalDecisions(projectId: string, decisionsPath: string): Promise<Record<string, JsonValue>>
  convertProposal(projectId: string, derivedProjectId: string): Promise<Record<string, JsonValue>>
  recordEvent(projectId: string, type: string, payload: Record<string, JsonValue>): Promise<void>
}

export interface RuntimeAdapter {
  execute(action: StateAction, bundle: ContextBundle, signal?: AbortSignal): Promise<HandlerSubmission>
}
