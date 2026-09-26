import { chmod, lstat, mkdir, unlink } from 'node:fs/promises'
import { createServer, type Server, type Socket } from 'node:net'
import { dirname, isAbsolute, resolve } from 'node:path'
import type { JsonValue, ProjectInitOptions, ResearchRuntime } from './contracts.js'

interface OperatorRequest {
  id: string
  method: string
  params: Record<string, JsonValue>
}

function object(value: JsonValue | undefined, label: string): Record<string, JsonValue> {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new TypeError(`${label} must be an object`)
  }
  return value
}

function string(value: JsonValue | undefined, label: string): string {
  if (typeof value !== 'string' || value.length === 0) {
    throw new TypeError(`${label} must be a non-empty string`)
  }
  return value
}

function number(value: JsonValue | undefined, label: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new TypeError(`${label} must be a finite number`)
  }
  return value
}

export class ResearchOperatorServer {
  readonly socketPath: string
  private server: Server | undefined

  constructor(
    private readonly runtime: ResearchRuntime,
    socketPath: string,
    workspace: string,
  ) {
    const candidate = isAbsolute(socketPath) ? socketPath : resolve(workspace, socketPath)
    const root = resolve(workspace)
    if (candidate !== root && !candidate.startsWith(`${root}/`)) {
      throw new Error('operatorSocket must be inside the configured workspace')
    }
    this.socketPath = candidate
  }

  async start(): Promise<void> {
    if (process.platform === 'win32') {
      throw new Error('research operator Unix socket is currently supported on POSIX only')
    }
    try {
      const existing = await lstat(this.socketPath)
      throw new Error(
        existing.isSocket()
          ? `operator socket already exists: ${this.socketPath}; verify no host is running before removing it`
          : `operatorSocket path exists and is not a socket: ${this.socketPath}`,
      )
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
    }
    await mkdir(dirname(this.socketPath), { recursive: true })
    this.server = createServer(socket => this.handle(socket))
    await new Promise<void>((resolveStart, reject) => {
      const server = this.server!
      const onError = (error: Error) => reject(error)
      server.once('error', onError)
      server.listen(this.socketPath, () => {
        server.off('error', onError)
        resolveStart()
      })
    })
    await chmod(this.socketPath, 0o600)
  }

  async dispose(): Promise<void> {
    const server = this.server
    this.server = undefined
    if (server !== undefined) {
      await new Promise<void>((resolveClose, reject) => {
        server.close(error => error === undefined ? resolveClose() : reject(error))
      })
    }
    try {
      const current = await lstat(this.socketPath)
      if (current.isSocket()) await unlink(this.socketPath)
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
    }
  }

  private handle(socket: Socket): void {
    socket.setEncoding('utf8')
    let buffer = ''
    let settled = false
    socket.on('data', chunk => {
      if (settled) return
      buffer += chunk
      if (buffer.length > 1_000_000) {
        settled = true
        this.reply(socket, { id: null, ok: false, error: 'operator request exceeds 1 MB' })
        return
      }
      const newline = buffer.indexOf('\n')
      if (newline < 0) return
      settled = true
      void this.dispatchLine(buffer.slice(0, newline)).then(
        value => this.reply(socket, value),
        error => this.reply(socket, {
          id: null, ok: false,
          error: error instanceof Error ? error.message : String(error),
        }),
      )
    })
  }

  private reply(socket: Socket, value: unknown): void {
    socket.end(`${JSON.stringify(value)}\n`)
  }

  async dispatchLine(line: string): Promise<Record<string, unknown>> {
    const parsed = JSON.parse(line) as unknown
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new TypeError('operator request must be an object')
    }
    const request = parsed as OperatorRequest
    if (typeof request.id !== 'string' || typeof request.method !== 'string') {
      throw new TypeError('operator request requires string id and method')
    }
    const params = object(request.params, 'params')
    let result: unknown
    try {
      result = await this.dispatch(request.method, params)
    } catch (error) {
      return {
        id: request.id,
        ok: false,
        error: error instanceof Error ? error.message : String(error),
      }
    }
    return { id: request.id, ok: true, result }
  }

  private async dispatch(method: string, params: Record<string, JsonValue>): Promise<unknown> {
    const projectId = string(params.project_id, 'project_id')
    switch (method) {
      case 'init': {
        const options = object(params.options ?? {}, 'options') as unknown as ProjectInitOptions
        return await this.runtime.init(projectId, string(params.objective, 'objective'), options)
      }
      case 'status': return await this.runtime.inspect(projectId)
      case 'run': return await this.runtime.run(projectId, {
        ...params.budget_percent === undefined ? {} : {
          budgetPercent: number(params.budget_percent, 'budget_percent'),
        },
        ...params.max_actions === undefined ? {} : {
          maxActions: number(params.max_actions, 'max_actions'),
        },
      })
      case 'pause': return await this.runtime.pause(projectId, string(params.reason, 'reason'))
      case 'resume': return await this.runtime.resume(projectId)
      case 'unblock': return await this.runtime.unblock(projectId, string(params.reason, 'reason'))
      case 'plan-revision': return await this.runtime.recordEvent(
        projectId,
        'PLAN_REVISION_REQUIRED',
        { reason: string(params.reason, 'reason') },
      )
      case 'cancel': return await this.runtime.cancel(projectId, string(params.reason, 'reason'))
      case 'approve': return await this.runtime.approve(
        projectId,
        typeof params.reason === 'string' ? params.reason : undefined,
      )
      case 'budget': return await this.runtime.approveBudget(
        projectId, number(params.additional_percent, 'additional_percent'),
      )
      case 'proposal-decisions': return await this.runtime.proposalDecisions(
        projectId, string(params.decisions_path, 'decisions_path'),
      )
      case 'convert': return await this.runtime.convertProposal(
        projectId, string(params.derived_project_id, 'derived_project_id'),
      )
      default: throw new Error(`unknown research operator method: ${method}`)
    }
  }
}
