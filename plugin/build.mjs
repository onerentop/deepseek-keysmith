// Build the keysmith plugin as a single self-contained CommonJS bundle
// (dist/plugin.cjs). The deepseek-keysmith installer deploys this artifact
// into the Harness home (~/.dsh/keysmith/plugin.cjs), where no node_modules
// exist — so every runtime dependency (schemastery) is inlined, and only
// node: builtins stay external.
import { build } from 'esbuild'
import { mkdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)))

await build({
  entryPoints: [resolve(root, 'src/index.ts')],
  outfile: resolve(root, 'dist/plugin.cjs'),
  bundle: true,
  format: 'cjs',
  platform: 'node',
  target: 'node22',
  external: ['node:*'],
  sourcemap: false,
  minify: false,
  logLevel: 'info',
})

mkdirSync(resolve(root, 'dist'), { recursive: true })
console.log('built dist/plugin.cjs')
