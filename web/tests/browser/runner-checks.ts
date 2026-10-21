import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { childEnvironment } from './ownership.ts'
import { parseRunnerMode } from './runner-mode.ts'
import { validateWorkflowTransition, validationWorkflow } from './workflow.ts'

export function runnerChecks(): void {
  const workflow = 'permissions: read\nactions/checkout@pinned\nname: Console browser calibration (${{ matrix.slot }})\n        slot: [1, 2, 3, 4, 5]\nrun: pnpm --dir web run test:browser --calibrate\nname: console-browser-calibration-${{ matrix.slot }}'

  const validation = validationWorkflow(workflow)

  validateWorkflowTransition(workflow, validation)
  for (const changed of [validation.replace('permissions: read', 'permissions: write'), validation.replace('checkout@pinned', 'checkout@other'), validation + '\nrun: bypass-checks', validation.replace('slot: [1, 2, 3, 4, 5]', 'slot: [1]')]) assert.throws(() => { validateWorkflowTransition(workflow, changed) })
  assert.equal(parseRunnerMode([]), 'verification')
  assert.equal(parseRunnerMode(['--calibrate']), 'calibration')
  assert.equal(parseRunnerMode(['--smoke']), 'smoke')
  for (const arguments_ of [['--diagnostic'], ['--control=cycle-8'], ['--smoke', '--smoke'], ['--calibrate', '--smoke']]) assert.throws(() => parseRunnerMode(arguments_), { message: 'invalid-browser-mode' })

  const root = mkdtempSync(join(tmpdir(), 'kora-browser-checks-'))
  try {
    const argumentsFailure = spawnSync(process.execPath, [fileURLToPath(new URL('./run.ts', import.meta.url)), '--argument-marker-secret'], { env: childEnvironment(), encoding: 'utf8', timeout: 10000, maxBuffer: 65536 })

    assert.equal(argumentsFailure.error, undefined)
    assert.equal(argumentsFailure.status, 1)
    assert(!`${argumentsFailure.stdout}${argumentsFailure.stderr}`.includes('argument-marker-secret'))

    const manifest = join(root, 'manifest.json')
    const destination = join(root, 'aggregate.json')
    const jobs = Array.from({ length: 5 }, (_, index) => ({ slot: index + 1, kind: 'training', conclusion: 'success', name: 'assertion-marker-secret' }))
    for (const [content, marker, failure] of [['{"json-marker-secret":', 'json-marker-secret', 'invalid-json'], [JSON.stringify({ stage: 'training', attempt: '1', jobs }), 'assertion-marker-secret', 'invalid-evidence']]) {
      assert(content && marker && failure)
      writeFileSync(manifest, content)

      const result = spawnSync(process.execPath, [fileURLToPath(new URL('./calibration.ts', import.meta.url)), manifest, destination], { env: childEnvironment(), encoding: 'utf8', timeout: 10000, maxBuffer: 65536 })

      assert.equal(result.error, undefined)
      assert.equal(result.status, 1)
      assert.equal(result.stdout, '')
      assert(!result.stderr.includes(marker))
      const payload: unknown = JSON.parse(result.stderr)
      assert(payload !== null && typeof payload === 'object' && 'failure' in payload)
      assert.equal(payload.failure, failure)
    }
  } finally { rmSync(root, { recursive: true, force: true }) }
}
