import { createHash } from 'node:crypto'
import { SessionId } from '@deepseek-ai/dsh-session'

export function researchSessionId(parts: {
  projectId: string
  runId: string
  actionId: string
  role: string
  attempt: number
}) {
  const stable = [parts.projectId, parts.runId, parts.actionId, parts.role, String(parts.attempt)].join('\0')
  return SessionId(`research-${createHash('sha256').update(stable).digest('hex').slice(0, 40)}`)
}
