import assert from 'node:assert/strict'
import { constants, closeSync, fstatSync, linkSync, openSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { artifactBytes, directoryLimit } from './diagnostic-snapshot.ts'
import { checkCleanup } from './ownership.ts'
import { measurementProtocol } from './protocol.ts'
import type { NativeResources } from './resources.ts'
import { parseTrial } from './trial-parser.ts'
import { summarize } from './trial.ts'
import type { Trial } from './trial.ts'

export interface Terminal {
  protocol: typeof measurementProtocol
  arm: 'base' | 'head'
  repeat: number
  runnerAfter: NodeJS.MemoryUsage
  after: NativeResources
  phase: Trial['phases'][number]
}

export interface TrialSummary {
  arm: 'base' | 'head'
  repeat: number
  browserVersion: string
  metrics: Record<string, number>
}

function prefix(arm: 'base' | 'head', repeat: number): string {
  assert(['base', 'head'].includes(arm) && Number.isInteger(repeat) && repeat >= 0 && repeat < 3)
  return `${arm}-${String(repeat)}`
}

export function writeEvidence(root: string, name: string, value: unknown, maximum: number, budgetRoot = root): void {
  assert(/^[a-z][a-z0-9-]+\.json$/u.test(name))
  const data = JSON.stringify(value, null, 2)
  assert(Buffer.byteLength(data) <= maximum, 'Trial evidence exceeded file bound')
  assert(artifactBytes(budgetRoot) + Buffer.byteLength(data) <= directoryLimit - 4 * 1024 * 1024 - 2 * 65536, 'Trial evidence exceeded cumulative bound')
  writeFileSync(join(root, name), data, { flag: 'wx', mode: 0o600 })
}

export function readEvidence(path: string, maximum = 4 * 1024 * 1024): unknown {
  const descriptor = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
  try {
    const stats = fstatSync(descriptor)
    assert(stats.isFile() && stats.size <= maximum, 'Invalid trial evidence file')
    const value: unknown = JSON.parse(readFileSync(descriptor, 'utf8'))
    return value
  } finally { closeSync(descriptor) }
}

export function persistTrial(root: string, trial: Trial, budgetRoot = root): void {
  writeEvidence(root, `${prefix(trial.arm, trial.repeat)}-pending.json`, trial, 4 * 1024 * 1024, budgetRoot)
}

export function persistTerminal(root: string, terminal: Terminal, budgetRoot = root): void {
  writeEvidence(root, `${prefix(terminal.arm, terminal.repeat)}-terminal.json`, terminal, 65536, budgetRoot)
}

export function finalizeTrial(root: string, terminal: Terminal, signal: AbortSignal, budgetRoot = root): void {
  signal.throwIfAborted()
  const name = prefix(terminal.arm, terminal.repeat)
  assert.equal(terminal.protocol, measurementProtocol)
  assert.deepEqual(readEvidence(join(root, `${name}-terminal.json`)), terminal)
  const input = readEvidence(join(root, `${name}-pending.json`))
  const trial = parseTrial(input)
  assert.deepEqual(trial, input, 'Trial parser changed raw evidence')
  assert.equal(trial.arm, terminal.arm)
  assert.equal(trial.repeat, terminal.repeat)
  assert.equal(trial.runnerAfter, undefined)
  assert.equal(trial.after, undefined)
  assert.equal(terminal.phase.phase, 'released')
  trial.runnerAfter = terminal.runnerAfter
  trial.after = terminal.after
  trial.phases.push(terminal.phase)
  writeEvidence(root, `${name}-complete.json`, trial, 4 * 1024 * 1024, budgetRoot)
  signal.throwIfAborted()
  assert.equal(trial.failure, undefined)
  assert(trial.cleanup)
  checkCleanup(trial.cleanup)
  assert.deepEqual(trial.diagnostics, { events: [], overflow: false })
  assert.equal(trial.warmLoads.length, 2)
  assert.equal(trial.loads.length, 20)
  assert.equal(trial.warmCycles.length, 3)
  assert.equal(trial.cycles.length, 30)
  assert.equal(trial.phases.length, 26)
  if (trial.after.descriptors !== null && trial.before.descriptors !== null) assert(trial.after.descriptors <= trial.before.descriptors)
  assert.equal(trial.after.processes, trial.before.processes)
  if (process.platform === 'linux') assert.deepEqual(trial.after.pids, trial.before.pids)
  const metrics = summarize(trial)
  linkSync(join(root, `${name}-complete.json`), join(root, `${name}.json`))
  unlinkSync(join(root, `${name}-complete.json`))
  writeEvidence(root, `${name}-summary.json`, { arm: trial.arm, repeat: trial.repeat, browserVersion: trial.browserVersion, metrics }, 65536, budgetRoot)
}

export function readSummary(root: string, arm: 'base' | 'head', repeat: number): TrialSummary {
  const input = readEvidence(join(root, `${prefix(arm, repeat)}-summary.json`), 65536)
  assert(input !== null && typeof input === 'object' && !Array.isArray(input), 'invalid-summary')
  assert(Object.keys(input).sort().join(',') === 'arm,browserVersion,metrics,repeat', 'invalid-summary')
  assert('arm' in input && input.arm === arm && 'repeat' in input && input.repeat === repeat, 'invalid-summary-identity')
  assert('browserVersion' in input && typeof input.browserVersion === 'string' && /^[0-9.]{1,64}$/u.test(input.browserVersion), 'invalid-summary-browser')
  assert('metrics' in input && input.metrics !== null && typeof input.metrics === 'object' && !Array.isArray(input.metrics), 'invalid-summary-metrics')
  const metrics: Record<string, number> = {}
  for (const [key, value] of Object.entries(input.metrics)) {
    assert(typeof value === 'number' && Number.isFinite(value), 'invalid-summary-number')
    metrics[key] = value
  }
  return { arm, repeat, browserVersion: input.browserVersion, metrics }
}
