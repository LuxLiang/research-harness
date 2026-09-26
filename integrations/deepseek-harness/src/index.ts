import type { Context } from '@deepseek-ai/cordis'
import { ResearchController, type ControllerConfig } from './controller.js'

export const name = 'research-harness-runtime'
export const inject = ResearchController.inject
export const Config = ResearchController.Config
export type Config = ControllerConfig

export function apply(ctx: Context, config: Config): void {
  ctx.plugin(ResearchController, config)
}

export * from './contracts.js'
export * from './controller.js'
export * from './runtime-adapter.js'
export * from './session-id.js'
export * from './sidecar-client.js'
export * from './operator-server.js'
export { RESEARCH_TOOL_NAMES } from './tools.js'
