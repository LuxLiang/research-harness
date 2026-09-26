import test from 'node:test'
import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'
import type {
  ActionResult,
  JsonValue,
  ProjectInitOptions,
  ResearchRunView,
  ResearchRuntime,
  ResearchStop,
  RunOptions,
} from '../contracts.js'
import { ResearchOperatorServer } from '../operator-server.js'
import { DECISION_STATES, DIRECT_EVENTS, HUMAN_STATES } from '../controller.js'

class RuntimeStub implements ResearchRuntime {
  calls: Array<{ method: string; args: unknown[] }> = []

  async init(projectId: string, objective: string, options?: ProjectInitOptions): Promise<ResearchRunView> {
    this.calls.push({ method: 'init', args: [projectId, objective, options] })
    return { checkpoint: { state: 'PROPOSAL_STRUCTURING' } }
  }
  async inspect(projectId: string): Promise<ResearchRunView> {
    this.calls.push({ method: 'status', args: [projectId] })
    return { checkpoint: { state: 'DONE' } }
  }
  async run(projectId: string, options?: RunOptions): Promise<ResearchStop> {
    this.calls.push({ method: 'run', args: [projectId, options] })
    return { reason: 'ACTION_LIMIT', checkpoint: { state: 'PROPOSAL_STRUCTURING' } }
  }
  async advance(): Promise<ActionResult> { throw new Error('unused') }
  async pause(projectId: string, reason: string): Promise<void> { this.calls.push({ method: 'pause', args: [projectId, reason] }) }
  async resume(projectId: string): Promise<void> { this.calls.push({ method: 'resume', args: [projectId] }) }
  async unblock(projectId: string, reason: string): Promise<void> { this.calls.push({ method: 'unblock', args: [projectId, reason] }) }
  async cancel(projectId: string, reason: string): Promise<void> { this.calls.push({ method: 'cancel', args: [projectId, reason] }) }
  async approve(projectId: string, reason?: string): Promise<void> { this.calls.push({ method: 'approve', args: [projectId, reason] }) }
  async approveBudget(projectId: string, value: number): Promise<void> { this.calls.push({ method: 'budget', args: [projectId, value] }) }
  async proposalDecisions(projectId: string, path: string): Promise<Record<string, JsonValue>> {
    this.calls.push({ method: 'proposal-decisions', args: [projectId, path] })
    return { all_authorized: true }
  }
  async convertProposal(projectId: string, derived: string): Promise<Record<string, JsonValue>> {
    this.calls.push({ method: 'convert', args: [projectId, derived] })
    return { project_id: derived }
  }
  async recordEvent(
    projectId: string,
    type: string,
    payload: Record<string, JsonValue>,
  ): Promise<void> {
    this.calls.push({ method: 'record-event', args: [projectId, type, payload] })
  }
}

test('operator health identifies workspace without executing a research action', async () => {
  const runtime = new RuntimeStub()
  const server = new ResearchOperatorServer(runtime, 'operator.sock', '/tmp/research-health')
  const response = await server.dispatchLine(JSON.stringify({ id: 'health-1', method: 'health', params: {} }))
  assert.deepEqual(response, {
    id: 'health-1', ok: true,
    result: { protocol: 'research-operator/v0.1', runtime: 'cordis', workspace: '/tmp/research-health' },
  })
  assert.deepEqual(runtime.calls, [])
})

test('Python CLI interoperates with the Node operator over a real socket', async () => {
  const root = await mkdtemp(join(tmpdir(), 'rh-bridge-'))
  const runtime = new RuntimeStub()
  const server = new ResearchOperatorServer(runtime, 'operator.sock', root)
  try {
    await server.start()
    const { stdout } = await promisify(execFile)(
      process.env.RESEARCH_HARNESS_TEST_PYTHON ?? 'python3',
      ['-m', 'research_artifacts.mvp_cli', '--workspace', root,
       '--runtime', 'cordis', '--socket', server.socketPath, 'status', 'example'],
      { cwd: fileURLToPath(new URL('../../../../', import.meta.url)), timeout: 15000 },
    )
    assert.equal(JSON.parse(stdout).checkpoint.state, 'DONE')
    assert.deepEqual(runtime.calls, [{ method: 'status', args: ['proj-example'] }])
  } finally {
    await server.dispose()
    await rm(root, { recursive: true, force: true })
  }
})

test('proposal states have deterministic production controller mappings', () => {
  assert.equal(DIRECT_EVENTS.PROPOSAL_STRUCTURING, 'PROPOSAL_STRUCTURED')
  assert.equal(DIRECT_EVENTS.PROPOSAL_NOVELTY_REVIEW, 'PROPOSAL_NOVELTY_REVIEWED')
  assert.equal(DIRECT_EVENTS.PROPOSAL_PLAN_RECONSTRUCTION, 'PROPOSAL_PLAN_VALIDATED')
  assert.equal(DIRECT_EVENTS.PROPOSAL_CORRECTNESS_REVIEW, 'PROPOSAL_CORRECTNESS_REVIEWED')
  assert.equal(DIRECT_EVENTS.PROPOSAL_REVISION, 'PROPOSAL_REVISION_COMPLETED')
  assert.equal(HUMAN_STATES.has('PROPOSAL_FINAL_APPROVAL'), true)
  assert.equal(DECISION_STATES.has('PROPOSAL_WAITING_DECISIONS'), true)
})

test('operator exposes a bounded plan revision request', async () => {
  const root = '/tmp/research-operator-plan-revision-contract'
  const socketPath = `${root}/runtime/operator.sock`
  const runtime = new RuntimeStub()
  const server = new ResearchOperatorServer(runtime, socketPath, root)

  const response = await server.dispatchLine(JSON.stringify({
    id: 'plan-revision-1',
    method: 'plan-revision',
    params: { project_id: 'proj-audit', reason: 'analysis mixes dataset hashes' },
  }))

  assert.equal(response.ok, true)
  assert.deepEqual(runtime.calls.at(-1), {
    method: 'record-event',
    args: [
      'proj-audit',
      'PLAN_REVISION_REQUIRED',
      { reason: 'analysis mixes dataset hashes' },
    ],
  })
})

test('operator bridge carries proposal init and conversion', async () => {
  const root = '/tmp/research-operator-contract'
  const socketPath = `${root}/runtime/operator.sock`
  const runtime = new RuntimeStub()
  const server = new ResearchOperatorServer(runtime, socketPath, root)
  const initialized = await server.dispatchLine(JSON.stringify({
      id: 'init-1', method: 'init', params: {
        project_id: 'proj-review', objective: 'Review proposal',
        options: {
          workflowMode: 'PROPOSAL_REVIEW',
          proposalPath: '/tmp/proposal.md',
          rubricPath: '/tmp/rubric.md',
          sourceMaterials: ['/tmp/theory.md'],
        },
      },
  }))
  assert.equal(initialized.ok, true)
  assert.deepEqual(runtime.calls[0], {
      method: 'init',
      args: ['proj-review', 'Review proposal', {
        workflowMode: 'PROPOSAL_REVIEW',
        proposalPath: '/tmp/proposal.md', rubricPath: '/tmp/rubric.md',
        sourceMaterials: ['/tmp/theory.md'],
      }],
  })
  const converted = await server.dispatchLine(JSON.stringify({
      id: 'convert-1', method: 'convert', params: {
        project_id: 'proj-review', derived_project_id: 'proj-derived',
      },
  }))
  assert.deepEqual(converted.result, { project_id: 'proj-derived' })
})
