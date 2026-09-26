import type { Context } from '@deepseek-ai/cordis'
import { defineTool } from '@deepseek-ai/dsh-tools'
import type { ToolDefinition } from '@deepseek-ai/dsh-tools'
import type { SidecarClient } from './sidecar-client.js'

const output = {
  schema: { type: 'json' as const },
  render(_args: unknown, value: unknown) {
    return [{ type: 'text' as const, text: JSON.stringify(value, null, 2) }]
  },
}

export interface ToolScope {
  projectId: string
  actionId: string
  bundleSha256: string
  proposerSessionId: string
  allowedTools: readonly string[]
  allowGateAssessment: boolean
  proposalWritePolicy: 'SOURCE_MAP_ONLY' | 'REVISION' | 'DENY'
}

export function assertGateAssessmentAllowed(
  allowGateAssessment: boolean,
  gateAssessmentJson: unknown,
): void {
  if (!allowGateAssessment && gateAssessmentJson !== undefined) {
    throw new Error(
      'this action has no gate_contract; omit gate_assessment_json and submit only proposal_ids_json plus outcome',
    )
  }
}

export function assertProposalWriteAllowed(
  policy: ToolScope['proposalWritePolicy'],
  relativePath: string,
): void {
  const sourceMap = /^source-maps\/proposal-r[0-9]{3}\.json$/
  const revision = /^(?:revisions\/rev-[0-9]{3}\.md|changes\/rev-[0-9]{3}\.ya?ml|source-maps\/proposal-r[0-9]{3}\.json)$/
  if (policy === 'SOURCE_MAP_ONLY' && sourceMap.test(relativePath)) return
  if (policy === 'REVISION' && revision.test(relativePath)) return
  throw new Error(
    `proposal resource path is not allowed for this action: ${relativePath}; `
    + 'question-framing must use source-maps/proposal-rNNN.json',
  )
}

function parseJsonObject(text: string, label: string): Record<string, unknown> {
  const value = JSON.parse(text) as unknown
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new TypeError(`${label} must be a JSON object`)
  }
  return value as Record<string, unknown>
}

function parseJsonArray(text: string, label: string): unknown[] {
  const value = JSON.parse(text) as unknown
  if (!Array.isArray(value)) throw new TypeError(`${label} must be a JSON array`)
  return value
}

export const RESEARCH_TOOL_NAMES = [
  'research_artifact_read',
  'research_artifact_query',
  'research_artifact_resolve',
  'research_artifact_validate',
  'research_artifact_propose',
  'research_action_submit',
  'research_lean_verify',
  'research_experiment_run',
  'resource_read',
  'resource_write',
  'academic_search',
  'citation_neighborhood',
  'academic_source_read',
  'research_proposal_write',
  'manuscript_read',
  'paper_filesystem',
  'citation_validate',
] as const

export function registerResearchTools(ctx: Context, sidecar: SidecarClient, scope: ToolScope): void {
  const register = (definition: ToolDefinition) => {
    if (scope.allowedTools.includes(definition.name)) ctx.tools.register(definition)
  }
  register(defineTool({
    name: 'manuscript_read',
    description: 'Read a bounded UTF-8 file from the current project paper/ tree and return its SHA-256; use pagination until next_offset_chars is null.',
    parameters: {
      relative_path: { type: 'string', required: true },
      offset_chars: { type: 'integer' },
      max_chars: { type: 'integer' },
    },
    output,
    execute: async (args: any, exec) => sidecar.call('paper.manuscript_read', {
      project_id: scope.projectId, action_id: scope.actionId,
      relative_path: args.relative_path,
      ...args.offset_chars === undefined ? {} : { offset_chars: args.offset_chars },
      max_chars: Math.min(args.max_chars ?? 100_000, 100_000),
    }, exec.signal),
  }))
  register(defineTool({
    name: 'paper_filesystem',
    description: 'Writer-only READ, WRITE, or LIST access restricted to the current project paper/ tree.',
    parameters: {
      operation: { type: 'string', enum: ['READ', 'WRITE', 'LIST'], required: true },
      relative_path: { type: 'string', required: true },
      content: { type: 'string' },
    },
    output,
    execute: async (args: any, exec) => sidecar.call('paper.filesystem', {
      project_id: scope.projectId, action_id: scope.actionId,
      operation: args.operation, relative_path: args.relative_path,
      ...args.content === undefined ? {} : { content: args.content },
    }, exec.signal),
  }))
  register(defineTool({
    name: 'citation_validate',
    description: 'Validate the current Writer traceability file and resolve every revision-pinned source reference before submission.',
    parameters: {},
    output,
    execute: async (_args: any, exec) => sidecar.call('artifact.validate_resource_action', {
      project_id: scope.projectId, action_id: scope.actionId,
    }, exec.signal),
  }))
  register(defineTool({
    name: 'resource_read',
    description: 'Read up to 20,000 characters from a SHA-256-pinned project resource; continue at next_offset_chars until it is null.',
    parameters: {
      uri: { type: 'string', required: true },
      offset_chars: { type: 'integer' },
      max_chars: { type: 'integer' },
    },
    output,
    execute: async (args: any, exec) => sidecar.call('resource.read', {
      project_id: scope.projectId, action_id: scope.actionId, uri: args.uri,
      ...args.offset_chars === undefined ? {} : { offset_chars: args.offset_chars },
      max_chars: Math.min(args.max_chars ?? 20_000, 20_000),
    }, exec.signal),
  }))
  register(defineTool({
    name: 'resource_write',
    description: 'Create one immutable action-scoped proof resource and return its URI and SHA-256 for inclusion in the ScientificClaim.',
    parameters: {
      relative_path: { type: 'string', required: true },
      content: { type: 'string', required: true },
      media_type: { type: 'string' },
    },
    output,
    execute: async (args: any, exec) => sidecar.call('resource.write', {
      project_id: scope.projectId, action_id: scope.actionId,
      relative_path: args.relative_path, content: args.content,
      ...args.media_type === undefined ? {} : { media_type: args.media_type },
    }, exec.signal),
  }))
  register(defineTool({
    name: 'academic_search',
    description: 'Search OpenAlex and verify available DOI metadata through Crossref; responses are immutably captured.',
    parameters: { query: { type: 'string', required: true }, limit: { type: 'integer' } },
    output,
    execute: async (args: any, exec) => sidecar.call('academic.search', {
      project_id: scope.projectId, action_id: scope.actionId, query: args.query,
      ...args.limit === undefined ? {} : { limit: args.limit },
    }, exec.signal),
  }))
  register(defineTool({
    name: 'citation_neighborhood',
    description: 'Read the references and/or citing works around an OpenAlex work ID.',
    parameters: {
      openalex_id: { type: 'string', required: true },
      direction: { type: 'string', enum: ['references', 'citations', 'both'] },
      limit: { type: 'integer' },
    },
    output,
    execute: async (args: any, exec) => sidecar.call('academic.citation_neighborhood', {
      project_id: scope.projectId, action_id: scope.actionId, openalex_id: args.openalex_id,
      ...args.direction === undefined ? {} : { direction: args.direction },
      ...args.limit === undefined ? {} : { limit: args.limit },
    }, exec.signal),
  }))
  register(defineTool({
    name: 'academic_source_read',
    description: 'Fetch and immutably capture up to 10 MB of an HTTPS open academic source; PDFs receive deterministic page-marked text for resource_read and failures return SOURCE_UNVERIFIED.',
    parameters: { url: { type: 'string', required: true }, max_bytes: { type: 'integer' } },
    output,
    execute: async (args: any, exec) => sidecar.call('academic.source_read', {
      project_id: scope.projectId, action_id: scope.actionId, url: args.url,
      ...args.max_bytes === undefined ? {} : { max_bytes: args.max_bytes },
    }, exec.signal),
  }))
  register(defineTool({
    name: 'research_proposal_write',
    description: 'Create one immutable numbered proposal revision, change-log, or revision-pinned source-map resource.',
    parameters: {
      relative_path: { type: 'string', required: true },
      content: { type: 'string', required: true },
      media_type: { type: 'string' },
    },
    output,
    execute: async (args: any, exec) => {
      assertProposalWriteAllowed(scope.proposalWritePolicy, args.relative_path)
      return await sidecar.call('proposal.write', {
        project_id: scope.projectId, action_id: scope.actionId,
        relative_path: args.relative_path, content: args.content,
        ...args.media_type === undefined ? {} : { media_type: args.media_type },
      }, exec.signal)
    },
  }))
  register(defineTool({
    name: 'research_artifact_read',
    description: 'Read one canonical scientific artifact by stable ID.',
    parameters: { artifact_id: { type: 'string', required: true } },
    output,
    execute: async (args: any, exec) => sidecar.call('artifact.read', {
      artifact_id: args.artifact_id,
    }, exec.signal),
  }))
  register(defineTool({
    name: 'research_artifact_query',
    description: 'Query canonical artifact metadata within the current project.',
    parameters: {
      kind: { type: 'string' },
      status: { type: 'string' },
    },
    output,
    execute: async (args: any, exec) => sidecar.call('artifact.query', {
      project_id: scope.projectId,
      ...args.kind === undefined ? {} : { kind: args.kind },
      ...args.status === undefined ? {} : { status: args.status },
    }, exec.signal),
  }))
  register(defineTool({
    name: 'research_artifact_resolve',
    description: 'Resolve revision-pinned artifact references supplied as a JSON array.',
    parameters: { refs_json: { type: 'string', required: true } },
    output,
    execute: async (args: any, exec) => sidecar.call('artifact.resolve', {
      refs: parseJsonArray(args.refs_json, 'refs_json'),
    }, exec.signal),
  }))
  register(defineTool({
    name: 'research_artifact_validate',
    description: 'Validate this action\'s staged proposals without promoting them.',
    parameters: { proposal_ids_json: { type: 'string', required: true } },
    output,
    execute: async (args: any, exec) => sidecar.call('artifact.validate_action', {
      project_id: scope.projectId,
      action_id: scope.actionId,
      proposal_ids: parseJsonArray(args.proposal_ids_json, 'proposal_ids_json'),
    }, exec.signal),
  }))
  register(defineTool({
    name: 'research_artifact_propose',
    description: 'Stage a complete CREATE or REVISE candidate in quarantine. This never mutates canonical artifacts.',
    parameters: {
      proposal_id: { type: 'string', required: true },
      operation: { type: 'string', enum: ['CREATE', 'REVISE'], required: true },
      artifact_id: { type: 'string', required: true },
      kind: { type: 'string', required: true },
      base_revision: { oneOf: [{ type: 'integer' }, { type: 'null' }], required: true },
      candidate_json: { type: 'string', required: true },
    },
    output,
    execute: async (args: any, exec) => sidecar.call(
      args.operation === 'CREATE' ? 'artifact.stage_create' : 'artifact.stage_revision',
      {
        proposal_id: args.proposal_id,
        project_id: scope.projectId,
        action_id: scope.actionId,
        artifact_id: args.artifact_id,
        kind: args.kind,
        base_revision: args.base_revision,
        bundle_sha256: scope.bundleSha256,
        proposer_session_id: scope.proposerSessionId,
        candidate: parseJsonObject(args.candidate_json, 'candidate_json'),
      },
      exec.signal,
    ),
  }))
  register(defineTool({
    name: 'research_action_submit',
    description: 'Submit the structured result for this action. Natural-language final text cannot advance research state.',
    parameters: {
      proposal_ids_json: { type: 'string', required: true },
      outcome: { type: 'string', enum: ['SUBMITTED', 'NO_CHANGE', 'FAILED'], required: true },
      gate_assessment_json: { type: 'string' },
      failure_classification: {
        type: 'string',
        enum: ['LOCAL_RETRY', 'INCOMPLETE', 'RETHINK_CLAIM', 'RETHINK_PLAN', 'RETHINK_QUESTION'],
      },
    },
    output,
    execute: async (args: any, exec) => {
      assertGateAssessmentAllowed(scope.allowGateAssessment, args.gate_assessment_json)
      return await sidecar.call('artifact.submit_action', {
        project_id: scope.projectId,
        action_id: scope.actionId,
        bundle_sha256: scope.bundleSha256,
        proposal_ids: parseJsonArray(args.proposal_ids_json, 'proposal_ids_json'),
        outcome: args.outcome,
        ...args.gate_assessment_json === undefined ? {} : {
          gate_assessment: parseJsonObject(args.gate_assessment_json, 'gate_assessment_json'),
        },
        ...args.failure_classification === undefined ? {} : {
          failure_classification: args.failure_classification,
        },
      }, exec.signal)
    },
  }))
  register(defineTool({
    name: 'research_lean_verify',
    description: 'Run deterministic pinned Lean build and axiom checks. Only this result can establish Lean PASS.',
    parameters: {
      project_dir: { type: 'string', required: true },
      source_paths_json: { type: 'string', required: true },
      lean_version: { type: 'string', required: true },
      mathlib_commit: { type: 'string', required: true },
      allowed_axioms_json: { type: 'string', required: true },
      report_path: { type: 'string', required: true },
    },
    output,
    execute: async (args: any, exec) => sidecar.call('tool.lean_verify', {
      project_dir: args.project_dir,
      source_paths: parseJsonArray(args.source_paths_json, 'source_paths_json'),
      lean_version: args.lean_version,
      mathlib_commit: args.mathlib_commit,
      allowed_axioms: parseJsonArray(args.allowed_axioms_json, 'allowed_axioms_json'),
      report_path: args.report_path,
    }, exec.signal),
  }))
  register(defineTool({
    name: 'research_experiment_run',
    description: 'Execute one stable run from a frozen Experiment protocol without interpreting results.',
    parameters: {
      experiment_id: { type: 'string', required: true },
      seed: { type: 'integer', required: true },
      repetition: { type: 'integer', required: true },
      run_root: { type: 'string', required: true },
    },
    output,
    // A preregistered matrix driver may legitimately exceed the short
    // controller-RPC timeout. The Python adapter still enforces its own
    // one-hour subprocess timeout and persists an interrupted run journal.
    execute: async (args: any, exec) => sidecar.call(
      'tool.experiment_run', args, exec.signal, 3_900_000,
    ),
  }))
}
