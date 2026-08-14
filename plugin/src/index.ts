/**
 * keysmith managed system-role entrypoint for DeepSeek Harness.
 *
 * Registers a managed Markdown file (system-role.md) as a system-prompt
 * section, so its content rides the harness's true system message pipeline
 * (`ctx.systemPrompt` -> `renderPrompt` -> request system slot) rather than
 * the user-role AGENTS.md channel.
 *
 * The section text is a provider function evaluated at every prompt assembly,
 * so editing the managed file takes effect on the next agent step without
 * reinstalling or restarting.
 *
 * The package is deployed as a single self-contained bundle (dist/plugin.cjs,
 * built by `pnpm build:bundle`): the deepseek-keysmith installer copies that
 * artifact into the Harness home and mounts it through the home patch layer,
 * where no node_modules are available — so all runtime dependencies
 * (schemastery's standard-schema object) are inlined by esbuild.
 * @module @deepseek-ai/dsh-keysmith
 */

import { readFileSync } from 'node:fs'
import type { Context } from '@deepseek-ai/cordis'
import z from '@deepseek-ai/schemastery'
// Type-only import pulls the @deepseek-ai/cordis Context augmentation that
// declares `ctx.systemPrompt` (the SystemPrompt service).
import type {} from '@deepseek-ai/dsh-system-prompt'

/** Stable Cordis plugin name (dsh convention: kebab-case, no colons). */
export const name = 'keysmith'

/** The prompt registry this plugin contributes to. */
export const inject = ['systemPrompt']

/** System-prompt section slot this plugin owns. */
export const SECTION_NAME = 'managed:keysmith'
/** Section order: after the deployment persona (0), before tool guidance (100+). */
export const SECTION_ORDER = 5

/** Plugin config: absolute path to the managed system-role.md. */
export interface Config {
  /** Absolute path of the managed Markdown system-role file. */
  systemFile: string
}

/** Runtime schema for the keysmith plugin. */
export const Config: z<Config> = z.object({
  systemFile: z.string().required(),
})

/**
 * Register the managed file as the `managed:keysmith` prompt section.
 * @param ctx - the mounting context (home patch layer; global scope).
 * @param config - the managed system-role file path.
 */
export function apply(ctx: Context, config: Config): void {
  ctx.effect(() => ctx.systemPrompt.section({
    name: SECTION_NAME,
    order: SECTION_ORDER,
    text: () => {
      try {
        return readFileSync(config.systemFile, 'utf8').trim()
      } catch {
        return ''
      }
    },
  }), 'keysmith.systemRole()')
}
