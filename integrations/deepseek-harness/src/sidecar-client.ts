import { createInterface } from 'node:readline'
import { randomUUID } from 'node:crypto'
import type { Context } from '@deepseek-ai/cordis'
import type { SubprocessHandle } from '@deepseek-ai/dsh-subprocess'
import { SIDECAR_PROTOCOL, type JsonValue, type SidecarResponse } from './contracts.js'

export class SidecarError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly details: unknown,
    readonly retryable: boolean,
  ) {
    super(message)
  }
}

export class SidecarClient {
  private handle: SubprocessHandle | undefined
  private readonly pending = new Map<string, {
    resolve: (value: unknown) => void
    reject: (error: Error) => void
  }>()

  constructor(
    private readonly ctx: Context,
    private readonly workspace: string,
    private readonly python = 'python3',
    private readonly requestTimeoutMs = 300_000,
  ) {}

  async start(): Promise<void> {
    if (this.handle !== undefined) return
    const executable = await this.ctx.subprocess.resolveExecutable(this.python)
    const handle = this.ctx.subprocess.spawn({
      argv: [executable, '-m', 'research_artifacts.sidecar', this.workspace],
      cwd: this.workspace,
      stdio: { stdin: 'pipe', stdout: 'pipe', stderr: 'inherit' },
      graceMs: 5_000,
    })
    if (handle.stdin === undefined || handle.stdout === undefined) {
      handle.terminate()
      throw new Error('research sidecar requires piped stdin/stdout')
    }
    this.handle = handle
    const lines = createInterface({ input: handle.stdout })
    lines.on('line', line => this.receive(line))
    void handle.done.then(outcome => {
      const error = new Error(`research sidecar exited (${outcome.exitCode ?? outcome.signal})`)
      for (const pending of this.pending.values()) pending.reject(error)
      this.pending.clear()
      this.handle = undefined
      lines.close()
    }, error => {
      for (const pending of this.pending.values()) pending.reject(error as Error)
      this.pending.clear()
      this.handle = undefined
      lines.close()
    })
    await this.call('health', {})
    await this.call('transaction.recover', {})
  }

  async call<T = JsonValue>(
    method: string,
    params: Record<string, unknown>,
    signal?: AbortSignal,
    timeoutMs?: number,
  ): Promise<T> {
    if (this.handle === undefined) await this.start()
    const handle = this.handle
    if (handle?.stdin === undefined) throw new Error('research sidecar is not running')
    if (signal?.aborted) throw signal.reason
    const requestId = randomUUID()
    const response = new Promise<T>((resolve, reject) => {
      this.pending.set(requestId, {
        resolve: value => resolve(value as T),
        reject,
      })
    })
    const abort = () => {
      const waiter = this.pending.get(requestId)
      if (waiter !== undefined) {
        this.pending.delete(requestId)
        handle.terminate()
        const error = signal?.reason instanceof Error ? signal.reason : new Error('cancelled')
        // Reject only after the old process exits. This prevents a pause/cancel
        // caller from starting a replacement sidecar that the old `done`
        // callback could mistake for its own generation and tear down.
        void handle.waitForExit().then(
          () => waiter.reject(error), () => waiter.reject(error),
        )
      }
    }
    signal?.addEventListener('abort', abort, { once: true })
    const timeout = setTimeout(() => {
      const waiter = this.pending.get(requestId)
      if (waiter !== undefined) {
        this.pending.delete(requestId)
        const error = new SidecarError(
          'TIMEOUT', `research sidecar request timed out: ${method}`, null, true,
        )
        handle.terminate()
        void handle.waitForExit().then(
          () => waiter.reject(error), () => waiter.reject(error),
        )
      }
    }, timeoutMs ?? this.requestTimeoutMs)
    handle.stdin.write(`${JSON.stringify({
      protocol: SIDECAR_PROTOCOL,
      request_id: requestId,
      method,
      params,
    })}\n`)
    try {
      return await response
    } finally {
      clearTimeout(timeout)
      signal?.removeEventListener('abort', abort)
    }
  }

  async dispose(): Promise<void> {
    const handle = this.handle
    if (handle === undefined) return
    try { await this.call('shutdown', {}) } catch { /* termination below is authoritative */ }
    // The exit callback may clear this.handle while the graceful shutdown call
    // is in flight. Keep the original handle so disposal is race-free.
    handle.terminate()
    await handle.waitForExit()
    if (this.handle === handle) this.handle = undefined
  }

  private receive(line: string): void {
    let response: SidecarResponse<unknown>
    try { response = JSON.parse(line) as SidecarResponse<unknown> } catch { return }
    const pending = this.pending.get(response.request_id)
    if (pending === undefined) return
    this.pending.delete(response.request_id)
    if (response.ok) pending.resolve(response.result)
    else {
      const error = response.error
      pending.reject(new SidecarError(
        error?.code ?? 'INTERNAL',
        error?.message ?? 'research sidecar failed',
        error?.details,
        error?.retryable ?? false,
      ))
    }
  }
}
