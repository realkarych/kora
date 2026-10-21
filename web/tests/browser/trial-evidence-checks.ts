import assert from 'node:assert/strict'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, unlinkSync, symlinkSync, truncateSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { directoryLimit } from './diagnostic-snapshot.ts'
import { validateFinalization } from './calibration.ts'
import { finalizeTrial, persistTerminal, persistTrial, readEvidence, writeEvidence } from './trial-evidence.ts'
import type { Terminal } from './trial-evidence.ts'
import { parseTrial } from './trial-parser.ts'
import { summarize } from './trial.ts'

function record(value: unknown): Record<string, unknown> {
  assert(value !== null && typeof value === 'object' && !Array.isArray(value))
  return Object.fromEntries(Object.entries(value))
}

export function evidenceChecks(output: string, budgetRoot = output): void {
  const completed = parseTrial(readEvidence(join(output, 'base-0.json')))
  const pending = parseTrial(readEvidence(join(output, 'base-0-pending.json')))
  const phase = completed.phases.at(-1)
  assert(phase && completed.runnerAfter && completed.after)
  const terminal: Terminal = { protocol: completed.protocol, arm: 'base', repeat: 0, runnerAfter: completed.runnerAfter, after: completed.after, phase }
  const root = join(output, 'evidence-checks')
  mkdirSync(root)
  const fixture = (name: string): string => {
    const path = join(root, name)
    mkdirSync(path)
    persistTrial(path, pending, budgetRoot)
    persistTerminal(path, terminal, budgetRoot)
    return path
  }
  const success = fixture('success')

  const summary = finalizeTrial(success, terminal, new AbortController().signal, budgetRoot)
  const actual = readEvidence(join(success, 'base-0.json'))

  assert.deepEqual(summary, { arm: completed.arm, repeat: completed.repeat, browserVersion: completed.browserVersion, metrics: summarize(completed) })
  assert.deepEqual(actual, completed)
  validateFinalization(record(actual), record(readEvidence(join(success, 'base-0-pending.json'))), record(readEvidence(join(success, 'base-0-terminal.json'))), 'base', 0)
  assert.throws(() => { validateFinalization({ ...record(actual), loads: [] }, record(pending), record(terminal), 'base', 0) })
  assert.throws(() => { persistTrial(success, pending) })
  assert.throws(() => { persistTerminal(success, terminal) })
  assert(!existsSync(join(success, 'base-0-complete.json')))

  const cancelled = fixture('cancelled')
  const controller = new AbortController()
  controller.abort()

  assert.throws(() => finalizeTrial(cancelled, terminal, controller.signal, budgetRoot))

  assert(!existsSync(join(cancelled, 'base-0.json')))
  assert.deepEqual(readEvidence(join(cancelled, 'base-0-pending.json')), pending)
  assert.deepEqual(readEvidence(join(cancelled, 'base-0-terminal.json')), terminal)

  const collision = fixture('collision')
  mkdirSync(join(collision, 'base-0.json'))

  assert.throws(() => finalizeTrial(collision, terminal, new AbortController().signal, budgetRoot))

  assert.deepEqual(readEvidence(join(collision, 'base-0-complete.json')), completed)
  assert.deepEqual(readEvidence(join(collision, 'base-0-pending.json')), pending)
  assert.deepEqual(readEvidence(join(collision, 'base-0-terminal.json')), terminal)

  const malformed = fixture('malformed')
  writeFileSync(join(malformed, 'base-0-pending.json'), '{')

  assert.throws(() => finalizeTrial(malformed, terminal, new AbortController().signal, budgetRoot))

  assert.equal(readFileSync(join(malformed, 'base-0-pending.json'), 'utf8'), '{')
  assert(!existsSync(join(malformed, 'base-0.json')))
  assert.deepEqual(readEvidence(join(malformed, 'base-0-terminal.json')), terminal)

  const bounded = join(root, 'bounded')
  mkdirSync(bounded)
  const oversized = join(bounded, 'oversized.json')
  writeFileSync(oversized, '')
  truncateSync(oversized, 4 * 1024 * 1024 + 1)
  const linked = join(bounded, 'linked.json')
  symlinkSync(join(success, 'base-0.json'), linked)

  assert.throws(() => readEvidence(oversized))
  assert.throws(() => readEvidence(linked))
  assert.throws(() => readEvidence(bounded))
  assert.throws(() => { writeEvidence(bounded, 'too-large.json', 'a'.repeat(64), 32) })

  assert(!existsSync(join(bounded, 'too-large.json')))
  unlinkSync(linked)

  const isolated = mkdtempSync(join(tmpdir(), 'kora-evidence-bounds-'))
  try {
    const current = join(isolated, 'current')
    mkdirSync(current)
    const historical = join(isolated, 'historical.json')
    writeFileSync(historical, '')
    truncateSync(historical, directoryLimit)

    writeEvidence(current, 'current.json', { preserved: true }, 65536)

    assert.deepEqual(readEvidence(join(current, 'current.json')), { preserved: true })
    assert.throws(() => { writeEvidence(current, 'outer-bound.json', {}, 65536, isolated) })
    assert(!existsSync(join(current, 'outer-bound.json')))
  } finally { rmSync(isolated, { recursive: true, force: true }) }
}
