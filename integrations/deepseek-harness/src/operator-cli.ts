#!/usr/bin/env node

import { createConnection } from 'node:net'
import { randomUUID } from 'node:crypto'
import { resolve } from 'node:path'

interface Parsed {
  socket: string
  command: string
  projectId: string
  options: Map<string, string | true | Array<string | true>>
}

function usage(): never {
  throw new Error(
    'usage: research-cordis [--socket PATH] COMMAND PROJECT [options]\n' +
    'commands: init, status, run, pause, resume, unblock, plan-revision, cancel, approve, budget, proposal-decisions, convert',
  )
}

function parse(argv: string[]): Parsed {
  const values = [...argv]
  let socket = process.env.RESEARCH_HARNESS_SOCKET
  if (values[0] === '--socket') {
    values.shift()
    socket = values.shift()
  }
  if (socket === undefined || socket.length === 0) {
    throw new Error('--socket or RESEARCH_HARNESS_SOCKET is required')
  }
  const command = values.shift()
  const project = values.shift()
  if (command === undefined || project === undefined) usage()
  const options = new Map<string, string | true | Array<string | true>>()
  while (values.length > 0) {
    const key = values.shift()!
    if (!key.startsWith('--')) usage()
    const next = values[0]
    const item = next === undefined || next.startsWith('--') ? true : values.shift()!
    const previous = options.get(key)
    options.set(key, previous === undefined ? item : Array.isArray(previous) ? [...previous, item] : [previous, item])
  }
  const projectId = project.startsWith('proj-') ? project : `proj-${project}`
  return { socket: resolve(socket), command, projectId, options }
}

function value(options: Map<string, string | true | Array<string | true>>, key: string, required = false): string | undefined {
  const found = options.get(key)
  if (Array.isArray(found)) throw new Error(`${key} may only be specified once`)
  if (found === true || (required && found === undefined)) {
    throw new Error(`${key} requires a value`)
  }
  return found as string | undefined
}

function values(options: Map<string, string | true | Array<string | true>>, key: string): string[] {
  const found = options.get(key)
  if (found === undefined) return []
  const items = Array.isArray(found) ? found : [found]
  if (items.some((item) => item === true)) throw new Error(`${key} requires a value`)
  return items as string[]
}

function numeric(options: Map<string, string | true | Array<string | true>>, key: string): number | undefined {
  const found = value(options, key)
  if (found === undefined) return undefined
  const parsed = Number(found)
  if (!Number.isFinite(parsed)) throw new Error(`${key} must be a number`)
  return parsed
}

function request(parsed: Parsed): Record<string, unknown> {
  const base = { project_id: parsed.projectId }
  switch (parsed.command) {
    case 'init': {
      const mode = value(parsed.options, '--mode') ?? 'full-research'
      if (!['full-research', 'proposal-review'].includes(mode)) {
        throw new Error('--mode must be full-research or proposal-review')
      }
      return { method: 'init', params: {
        ...base,
        objective: value(parsed.options, '--objective') ?? 'Evaluate a traceable scientific workflow.',
        options: {
          workflowMode: mode.replace('-', '_').toUpperCase(),
          ...value(parsed.options, '--proposal') === undefined ? {} : {
            proposalPath: resolve(value(parsed.options, '--proposal')!),
          },
          ...value(parsed.options, '--rubric') === undefined ? {} : {
            rubricPath: resolve(value(parsed.options, '--rubric')!),
          },
          ...values(parsed.options, '--source-material').length === 0 ? {} : {
            sourceMaterials: values(parsed.options, '--source-material').map((path) => resolve(path)),
          },
        },
      } }
    }
    case 'status': return { method: 'status', params: base }
    case 'run': return { method: 'run', params: {
      ...base,
      ...numeric(parsed.options, '--budget-percent') === undefined ? {} : {
        budget_percent: numeric(parsed.options, '--budget-percent'),
      },
      ...numeric(parsed.options, '--max-actions') === undefined ? {} : {
        max_actions: numeric(parsed.options, '--max-actions'),
      },
    } }
    case 'pause':
    case 'unblock':
    case 'plan-revision':
    case 'cancel': return {
      method: parsed.command,
      params: { ...base, reason: value(parsed.options, '--reason', true) },
    }
    case 'resume': return { method: 'resume', params: base }
    case 'approve': return {
      method: 'approve',
      params: { ...base, ...value(parsed.options, '--reason') === undefined ? {} : {
        reason: value(parsed.options, '--reason'),
      } },
    }
    case 'budget': return {
      method: 'budget',
      params: { ...base, additional_percent: numeric(parsed.options, '--additional-percent') },
    }
    case 'proposal-decisions': return {
      method: 'proposal-decisions',
      params: { ...base, decisions_path: resolve(value(parsed.options, '--file', true)!) },
    }
    case 'convert': {
      const derived = value(parsed.options, '--project', true)!
      return {
        method: 'convert',
        params: { ...base, derived_project_id: derived.startsWith('proj-') ? derived : `proj-${derived}` },
      }
    }
    default: return usage()
  }
}

async function call(socketPath: string, payload: Record<string, unknown>): Promise<unknown> {
  return await new Promise((resolveResult, reject) => {
    const socket = createConnection(socketPath)
    socket.setEncoding('utf8')
    let buffer = ''
    socket.once('connect', () => {
      socket.write(`${JSON.stringify({ id: randomUUID(), ...payload })}\n`)
    })
    socket.on('data', chunk => { buffer += chunk })
    socket.once('error', reject)
    socket.once('end', () => {
      try {
        const response = JSON.parse(buffer) as { ok: boolean; result?: unknown; error?: string }
        if (!response.ok) reject(new Error(response.error ?? 'operator request failed'))
        else resolveResult(response.result)
      } catch (error) {
        reject(error)
      }
    })
  })
}

async function main(): Promise<void> {
  const parsed = parse(process.argv.slice(2))
  const payload = request(parsed)
  const result = await call(parsed.socket, payload)
  process.stdout.write(`${JSON.stringify(result, null, 2)}\n`)
}

void main().catch(error => {
  process.stderr.write(`research-cordis: ${error instanceof Error ? error.message : String(error)}\n`)
  process.exitCode = 2
})
