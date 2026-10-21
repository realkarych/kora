import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { createReadStream } from 'node:fs'
import { readFile, stat, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'
import { calibrateBatch, compare, parseBudgets, requireReleased } from './protocol.ts'
import type { Budgets } from './protocol.ts'
import { percentile, span } from './resources.ts'

interface Job {
  slot: number
  jobId: string
  artifactId: string
  archive: string
  sha256: string
  reportPath: string
}

interface Manifest {
  runId: string
  attempt: string
  head: string
  base: string
  checkout: string
  jobs: Job[]
}

interface Dataset {
  report: Record<string, unknown>
  base: Record<string, number>[]
  head: Record<string, number>[]
  reference: Budgets['reference']
}

const execute = promisify(execFile)
const scenarios = ['startup', 'navigation', 'assertion', 'deadline', 'cancellation', 'acquisition']
const terminals = ['heapUsed', 'external', 'arrayBuffers']

function record(value: unknown): Record<string, unknown> {
  assert(value !== null && typeof value === 'object' && !Array.isArray(value))
  return Object.fromEntries(Object.entries(value))
}

function array(value: unknown, length: number): unknown[] {
  assert(Array.isArray(value) && value.length === length)
  return value as unknown[]
}

function string(value: unknown, pattern: RegExp): string {
  assert(typeof value === 'string' && pattern.test(value))
  return value
}

function number(value: unknown): number {
  assert(typeof value === 'number' && Number.isFinite(value))
  return value
}

export function parseManifest(value: unknown): Manifest {
  const input = record(value)
  const id = /^[1-9][0-9]{0,19}$/u
  const sha = /^[a-f0-9]{40}$/u
  const jobs = array(input.jobs, 5).map(value => {
    const job = record(value)
    const slot = number(job.slot)
    assert(Number.isInteger(slot) && slot >= 1 && slot <= 5)
    assert.equal(job.conclusion, 'success')
    assert.equal(job.name, `Console browser calibration (${String(slot)})`)
    assert.equal(job.artifactName, `console-browser-calibration-${String(slot)}`)
    return {
      slot, jobId: string(job.jobId, id), artifactId: string(job.artifactId, id),
      archive: string(job.archive, /^\/[^\0\r\n]+\.zip$/u),
      sha256: string(job.sha256, /^[a-f0-9]{64}$/u),
      reportPath: string(job.reportPath, /^run-[A-Za-z0-9]+\/report\.json$/u),
    }
  })
  for (const key of ['slot', 'jobId', 'artifactId', 'archive', 'sha256'] as const) assert.equal(new Set(jobs.map(job => job[key])).size, 5, `Duplicate ${key}`)
  return { runId: string(input.runId, id), attempt: string(input.attempt, id), head: string(input.head, sha), base: string(input.base, sha), checkout: string(input.checkout, sha), jobs }
}

function diagnostics(value: unknown): void {
  const input = record(value)
  array(input.events, 0)
  assert.equal(input.overflow, false)
}

function released(value: unknown): void {
  const input = record(value)
  const remaining = record(input.remaining)
  array(remaining.pids, 0)
  for (const key of ['rss', 'descriptors', 'processes']) assert.equal(remaining[key], 0)
  array(input.failures, 0)
  assert.equal(input.proxySockets, 0)
  assert.equal(input.forced, false)
}

function summaries(value: unknown): Record<string, number>[] {
  return array(value, 3).map(value => Object.fromEntries(Object.entries(record(value)).map(([key, value]) => [key, number(value)])))
}

export function validateDataset(value: unknown, manifest: Manifest, job: Job): Dataset {
  const report = record(value)
  assert.equal(report.status, 'calibrated; frozen budgets and independent verification still required')
  assert.equal(report.mode, 'calibration')
  assert.equal(report.prHead, manifest.head)
  assert.equal(report.base, manifest.base)
  assert.equal(report.checkout, manifest.checkout)
  assert.deepEqual(report.checkoutParents, [manifest.base, manifest.head])
  string(report.headTree, /^[a-f0-9]{40}$/u)
  assert.equal(report.checkoutTree, report.headTree)
  assert.equal(report.dirty, '')
  assert.equal(report.platform, 'linux')
  assert.equal(report.arch, 'x64')
  assert.equal(report.referenceCounters, 'available')
  assert.equal(report.artifactLimitFailed, false)
  assert(number(report.seconds) > 0 && number(report.seconds) < 600)
  assert(number(report.cpus) > 0 && number(report.totalMemory) > 0)
  assert(Array.isArray(report.cpuModels) && report.cpuModels.length > 0)
  for (const model of report.cpuModels) string(model, /^.{1,256}$/u)
  string(report.lockSha256, /^[a-f0-9]{64}$/u)
  assert.deepEqual(report.nodeFlags, ['--expose-gc'])
  const runner = record(report.runner)
  assert.equal(runner.runId, manifest.runId)
  assert.equal(runner.attempt, manifest.attempt)
  assert.equal(runner.job, 'browser')
  assert.equal(runner.slot, String(job.slot))
  for (const key of ['name', 'image', 'imageVersion']) string(runner[key], /^.{1,256}$/u)
  for (const [key, expected] of Object.entries({ repetitions: 3, coldWarmup: 2, coldSamples: 20, lifecycleWarmup: 3, lifecycleSamples: 30 })) assert.equal(report[key], expected)
  assert.deepEqual(report.order, [['base', 0], ['head', 0], ['head', 1], ['base', 1], ['base', 2], ['head', 2]])
  const assets = record(report.assets)
  assert.deepEqual(assets.head, report.headAssetsBeforeFixtures)
  for (const arm of ['base', 'head']) {
    const hashes = Object.values(record(assets[arm]))
    assert(hashes.length > 0)
    for (const hash of hashes) string(hash, /^[a-f0-9]{64}$/u)
  }
  for (const key of ['productionSmoke', 'developmentSmoke']) {
    const smoke = record(report[key])
    diagnostics(smoke.diagnostics)
    released(smoke.cleanup)
  }
  for (const key of ['runnerCancellation', 'artifactLimits', 'networkChecks']) assert.equal(report[key], 'passed')
  const failures = array(report.failureChecks, 6).map(record)
  assert.deepEqual(failures.map(failure => failure.scenario), scenarios)
  for (const failure of failures) {
    assert.equal(failure.code, 0)
    assert.equal(record(failure.payload).expected, true)
    released(record(failure.payload).cleanup)
  }
  const playwright = record(report.playwright)
  assert.equal(playwright.name, 'playwright')
  string(playwright.version, /^[0-9]+\.[0-9]+\.[0-9]+$/u)
  const distribution = record(report.browserDistribution)
  assert(Array.isArray(distribution.browsers) && distribution.browsers.length > 0 && distribution.browsers.length <= 32)
  const browsers = distribution.browsers.map(record)
  for (const browser of browsers) {
    string(browser.name, /^[a-z0-9-]{1,64}$/u)
    string(browser.revision, /^[0-9]{1,10}$/u)
  }
  assert.deepEqual(report.viewport, { width: 1280, height: 900 })
  assert.equal(report.throttling, 'none')
  assert.equal(report.cache, 'fresh context for cold loads; request routing disables HTTP cache')
  const proposed = parseBudgets(report.proposedBudgets)
  const chromium = browsers.filter(browser => browser.name === 'chromium')
  assert.equal(chromium.length, 1)
  assert.equal(chromium[0]?.browserVersion, proposed.reference.browser)
  assert.equal(proposed.reference.platform, report.platform)
  assert.equal(proposed.reference.arch, report.arch)
  assert.equal(proposed.reference.node, report.node)
  const summary = record(report.summaries)
  const base = summaries(summary.base)
  const head = summaries(summary.head)
  requireReleased([...base, ...head])
  return { report, base, head, reference: proposed.reference }
}

function summarizeRaw(trial: Record<string, unknown>): Record<string, number> {
  const loads = array(trial.loads, 20).map(record)
  const cycles = array(trial.cycles, 30).map(record)
  const allCycles = [...array(trial.warmCycles, 3).map(record), ...cycles]
  const phases = array(trial.phases, 26).map(record)
  const result: Record<string, number> = {}
  const growth = (key: string, values: number[]): void => {
    result[`${key}Span`] = span(values)
    result[`${key}LateSpan`] = span(values.slice(15))
    result[`${key}MedianGrowth`] = percentile(values.slice(-10), 0.5) - percentile(values.slice(0, 10), 0.5)
  }
  for (const key of ['load', 'fcp', 'lcp', 'cls', 'longTasks', 'transfer']) {
    const values = loads.map(load => number(load[key]))
    result[`${key}Median`] = percentile(values, 0.5)
    result[`${key}P95`] = percentile(values, 0.95)
  }
  for (const key of ['used', 'documents', 'nodes', 'listeners']) {
    growth(key, cycles.map(cycle => number(record(cycle.afterGC)[key])))
    result[`${key}Peak`] = Math.max(...allCycles.flatMap(cycle => [number(record(cycle.beforeGC)[key]), number(record(cycle.afterGC)[key])]))
  }
  for (const key of ['rss', 'descriptors', 'processes']) {
    growth(key, cycles.map(cycle => number(record(cycle.native)[key])))
    const peaks = allCycles.flatMap(cycle => ['active', 'beforeGCNative', 'native'].map(phase => number(record(cycle[phase])[key])))
    result[`${key}Peak`] = Math.max(...peaks, ...phases.map(phase => number(record(phase.native)[key])))
  }
  for (const key of terminals) {
    growth(`runner${key}`, cycles.map(cycle => number(record(cycle.runner)[key])))
    const points = [trial.runnerBefore, trial.runnerAfter, trial.runnerReleasedBeforeGC, ...allCycles.flatMap(cycle => [cycle.runnerBeforeGC, cycle.runner]), ...phases.map(phase => phase.runner)]
    result[`runner${key}Peak`] = Math.max(...points.map(point => number(record(point)[key])))
    result[`runner${key}ReleasedGrowth`] = number(record(trial.runnerAfter)[key]) - number(record(trial.runnerBefore)[key])
  }
  result.cleanupSeconds = number(record(trial.cleanup).seconds)
  return result
}

function validateTrial(value: unknown, dataset: Dataset, arm: 'base' | 'head', repeat: number): void {
  const trial = record(value)
  assert.equal(trial.arm, arm)
  assert.equal(trial.repeat, repeat)
  assert.equal(trial.browserVersion, dataset.reference.browser)
  assert.equal(trial.failure, undefined)
  diagnostics(trial.diagnostics)
  released(trial.cleanup)
  for (const load of [...array(trial.warmLoads, 2), ...array(trial.loads, 20)].map(record)) {
    for (const key of ['load', 'fcp', 'lcp', 'cls', 'longTasks', 'transfer']) assert(number(load[key]) >= 0)
  }
  assert.deepEqual(array(trial.warmCycles, 3).map(value => record(value).cycle), [-2, -1, 0])
  const phaseNames = ['before-server', 'server-ready', 'browser-ready', ...Array.from({ length: 22 }, (_, index) => `cold-load-${String(index - 2)}`), 'released']
  assert.deepEqual(array(trial.phases, 26).map(value => record(value).phase), phaseNames)
  const cycles = array(trial.cycles, 30).map(record)
  for (const [index, cycle] of cycles.entries()) {
    assert.equal(cycle.cycle, index + 1)
    assert.equal(cycle.contexts, 1)
    assert.equal(cycle.pages, 1)
  }
  const before = record(trial.before)
  const after = record(trial.after)
  assert(number(after.descriptors) <= number(before.descriptors))
  assert.equal(after.processes, before.processes)
  assert.deepEqual(after.pids, before.pids)
  const summary = dataset[arm][repeat]
  assert(summary)
  assert.deepEqual(summary, summarizeRaw(trial), 'Raw trial does not match summary')
  for (const key of terminals) {
    const growth = number(record(trial.runnerAfter)[key]) - number(record(trial.runnerBefore)[key])
    assert(growth <= 0)
    assert.equal(summary[`runner${key}ReleasedGrowth`], growth)
  }
}

export function aggregate(datasets: Dataset[]): { budgets: Budgets; comparisons: ReturnType<typeof compare>[] } {
  assert.equal(datasets.length, 5)
  const first = datasets[0]
  assert(first)
  const runners = datasets.map(dataset => record(dataset.report.runner).name)
  assert.equal(new Set(runners).size, 5, 'Runner identities must be distinct')
  for (const dataset of datasets) {
    assert.deepEqual(dataset.reference, first.reference)
    for (const key of ['image', 'imageVersion']) assert.equal(record(dataset.report.runner)[key], record(first.report.runner)[key], `Mismatched runner ${key}`)
    for (const key of ['checkout', 'headTree', 'checkoutParents', 'prHead', 'base', 'node', 'platform', 'arch', 'lockSha256', 'playwright', 'browserDistribution', 'assets', 'viewport', 'throttling', 'cache']) assert.deepEqual(dataset.report[key], first.report[key], `Mismatched ${key}`)
    requireReleased([...dataset.base, ...dataset.head])
  }
  const budgets = calibrateBatch(datasets.map(dataset => dataset.base), first.reference)
  const comparisons = datasets.map(dataset => compare(dataset.base, dataset.head, budgets))
  assert(comparisons.every(comparison => Object.keys(comparison.gates).length === 336 && Object.values(comparison.gates).every(Boolean)), 'Calibration batch failed prospective gates')
  return { budgets, comparisons }
}

async function readBounded(path: string, maximum: number): Promise<unknown> {
  assert((await stat(path)).size <= maximum, 'Input exceeded size bound')
  const value: unknown = JSON.parse(await readFile(path, 'utf8'))
  return value
}

async function readArchive(job: Job, entry: string, maximum: number): Promise<unknown> {
  assert(/^run-[A-Za-z0-9]+\/[A-Za-z0-9-]+\.json$/u.test(entry))
  const { stdout } = await execute('/usr/bin/unzip', ['-p', job.archive, entry], { maxBuffer: maximum, timeout: 30000 })
  const value: unknown = JSON.parse(stdout)
  return value
}

export function validateArchiveEntries(entries: string[], reportPath: string): void {
  assert.equal(new Set(entries).size, entries.length, 'Duplicate archive entries')
  assert.deepEqual(entries.filter(entry => /^run-[A-Za-z0-9]+\/report\.json$/u.test(entry)), [reportPath])
  const prefix = reportPath.slice(0, -'report.json'.length)
  const required: string[] = []
  for (const arm of ['base', 'head']) {
    for (let repeat = 0; repeat < 3; repeat++) {
      const trial = `${arm}-${String(repeat)}`
      required.push(`${trial}.json`, `${trial}-baseline.heapsnapshot`, `${trial}-end.heapsnapshot`)
    }
  }
  for (const mode of ['production', 'development']) {
    required.push(`${mode}-smoke.json`)
    for (const width of [1280, 768, 375]) required.push(`${mode}-${String(width)}.png`, `${mode}-${String(width)}-terminal.png`, `${mode}-${String(width)}.zip`)
  }
  for (const name of [...scenarios.map(scenario => `failure-${scenario}`), 'runner', 'runner-forced']) required.push(`${name}-termination.json`)
  for (const name of required) assert(entries.includes(`${prefix}${name}`), 'Missing required evidence entry')
}

async function loadDataset(manifest: Manifest, job: Job): Promise<Dataset> {
  assert((await stat(job.archive)).size <= 512 * 1024 * 1024, 'Archive exceeded size bound')
  const hash = createHash('sha256')
  const stream = createReadStream(job.archive)
  for await (const value of stream) {
    const chunk: unknown = value
    assert(Buffer.isBuffer(chunk))
    hash.update(chunk)
  }
  assert.equal(hash.digest('hex'), job.sha256, 'Archive digest mismatch')
  const { stdout } = await execute('/usr/bin/unzip', ['-Z1', job.archive], { maxBuffer: 512 * 1024, timeout: 30000 })
  const entries = stdout.trim().split('\n')
  validateArchiveEntries(entries, job.reportPath)
  const prefix = job.reportPath.slice(0, -'report.json'.length)
  const dataset = validateDataset(await readArchive(job, job.reportPath, 4 * 1024 * 1024), manifest, job)
  for (const arm of ['base', 'head'] as const) {
    for (let repeat = 0; repeat < 3; repeat++) validateTrial(await readArchive(job, `${prefix}${arm}-${String(repeat)}.json`, 4 * 1024 * 1024), dataset, arm, repeat)
  }
  for (const name of [...scenarios.map(scenario => `failure-${scenario}`), 'runner', 'runner-forced']) {
    const termination = record(await readArchive(job, `${prefix}${name}-termination.json`, 64 * 1024))
    array(termination.failures, 0)
    assert.equal(termination.ownershipConfirmed, true)
    assert.equal(termination.temporaryRemoved, true)
    const remaining = record(termination.remaining)
    array(remaining.pids, 0)
    for (const key of ['rss', 'descriptors', 'processes']) assert.equal(remaining[key], 0)
  }
  return dataset
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  let stage: 'arguments' | 'manifest' | 'archive' | 'aggregate' | 'output' = 'arguments'
  try {
    assert.equal(process.argv.length, 4)
    const [manifestPath, outputPath] = process.argv.slice(2)
    assert(manifestPath && outputPath)
    stage = 'manifest'
    const manifest = parseManifest(await readBounded(manifestPath, 64 * 1024))
    stage = 'archive'
    const datasets: Dataset[] = []
    for (const job of manifest.jobs) datasets.push(await loadDataset(manifest, job))
    stage = 'aggregate'
    const result = aggregate(datasets)
    stage = 'output'
    const output = JSON.stringify({ manifest, ...result }, null, 2)
    assert(Buffer.byteLength(output) <= 4 * 1024 * 1024)
    await writeFile(outputPath, `${output}\n`, { flag: 'wx' })
    process.stdout.write(`${JSON.stringify({ event: 'browser.calibration.completed', level: 'INFO', jobs: 5, baseSamples: 15, gates: 1680 })}\n`)
  } catch (error) {
    const failure = error instanceof SyntaxError ? 'invalid-json' : error instanceof assert.AssertionError ? 'invalid-evidence' : 'operation-failed'
    process.exitCode = 1
    process.stderr.write(`${JSON.stringify({ event: 'browser.calibration.failed', level: 'ERROR', stage, failure })}\n`)
  }
}
