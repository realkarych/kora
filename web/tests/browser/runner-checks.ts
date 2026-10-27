import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, readdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { childEnvironment } from './ownership.ts'
import { parseRunnerMode } from './runner-mode.ts'

export function runnerChecks(): void {
  const invalid = [['--calibrate'], ['--diagnostic'], ['--control=cycle-8'], ['--smoke', '--smoke'], ['--calibrate', '--smoke'], ['--argument-marker-secret']]
  const spawned = [['--argument-marker-secret'], ['--smoke', '--smoke'], ['--smoke', '--calibrate']]

  assert.equal(parseRunnerMode([]), 'checks')
  assert.equal(parseRunnerMode(['--smoke']), 'smoke')
  for (const arguments_ of invalid) assert.throws(() => parseRunnerMode(arguments_), { message: 'invalid-browser-mode' })

  const root = mkdtempSync(join(tmpdir(), 'kora-browser-checks-'))
  try {
    for (const arguments_ of spawned) {
      const result = spawnSync(process.execPath, [fileURLToPath(new URL('./run.ts', import.meta.url)), ...arguments_], {
        env: { ...childEnvironment(), TMPDIR: root, KORA_BROWSER_OUTPUT: join(root, 'output') }, encoding: 'utf8', timeout: 10000, maxBuffer: 65536,
      })

      assert.equal(result.error, undefined)
      assert.equal(result.status, 1)
      assert.equal(result.stdout, '')
      assert(result.stderr.includes('invalid-browser-mode'))
      assert(!`${result.stdout}${result.stderr}`.includes('argument-marker-secret'))
      assert.deepEqual(readdirSync(root), [])
    }
  } finally { rmSync(root, { recursive: true, force: true }) }
}
