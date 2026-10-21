import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { createReadStream } from 'node:fs'
import { readFile, stat, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'
import { calibrateBatch, compare, measurementProtocol, parseBudgets, requireCandidateHeap, requireReleased } from './protocol.ts'
import type { Budgets } from './protocol.ts'
import { percentile, span } from './resources.ts'
import { fitHeapLimits, heapGates } from './invocation-heap.ts'
import type { HeapGrowth } from './invocation-heap.ts'
import { invocationGrowth, parseInvocationPoint, parseRetention } from './invocation.ts'
import type { InvocationPoint } from './invocation.ts'
import { classifyControl, controls } from './retention.ts'
import type { Control } from './retention.ts'
import { validateWorkflowTransition } from './invocation-workflow.ts'
import { verifyRecovery } from './retention-fixture.ts'

interface Job {
  slot: number
  kind: 'training' | 'holdout' | Control
  conclusion: 'success'
  name: string
  artifactName: string
  jobId: string
  artifactId: string
  archive: string
  sha256: string
  reportPath: string
}

interface Manifest {
  stage: 'training' | 'validation'
  checkoutBase: string
  frozenAggregate?: string
  runId: string
  attempt: string
  head: string
  base: string
  checkout: string
  jobs: Job[]
}

interface Dataset {
  report: Record<string, unknown>
  points: InvocationPoint[]
  growth: HeapGrowth
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
  assert(input.stage === 'training' || input.stage === 'validation', 'invalid-stage')
  assert(input.attempt === '1', 'unexpected-attempt')
  const count = input.stage === 'training' ? 5 : 9
  const jobs = array(input.jobs, count).map((value): Job => {
    const job = record(value)
    const slot = number(job.slot)
    assert(Number.isInteger(slot) && slot >= 1 && slot <= count)
    assert.equal(job.conclusion, 'success')
    const kind = input.stage === 'training' ? 'training' : slot <= 5 ? 'holdout' : controls[slot - 6]
    assert(kind, 'invalid-stage-slot')
    assert.equal(job.kind, kind)
    const label = kind === 'holdout' ? `holdout-${String(slot)}` : kind
    assert.equal(job.name, input.stage === 'training' ? `Console browser calibration (${String(slot)})` : `Console browser validation (${label})`)
    assert.equal(job.artifactName, input.stage === 'training' ? `console-browser-calibration-${String(slot)}` : `console-browser-validation-${label}`)
    return {
      slot, kind, conclusion: 'success', name: string(job.name, /^.{1,128}$/u), artifactName: string(job.artifactName, /^.{1,128}$/u), jobId: string(job.jobId, id), artifactId: string(job.artifactId, id),
      archive: string(job.archive, /^\/[^\0\r\n]+\.zip$/u),
      sha256: string(job.sha256, /^[a-f0-9]{64}$/u),
      reportPath: string(job.reportPath, /^run-[A-Za-z0-9]+\/report\.json$/u),
    }
  })
  for (const key of ['slot', 'jobId', 'artifactId', 'archive', 'sha256'] as const) assert.equal(new Set(jobs.map(job => job[key])).size, count, `Duplicate ${key}`)
  return { stage: input.stage, checkoutBase: string(input.checkoutBase, sha), ...(input.stage === 'validation' ? { frozenAggregate: string(input.frozenAggregate, /^[a-f0-9]{64}$/u) } : {}), runId: string(input.runId, id), attempt: string(input.attempt, id), head: string(input.head, sha), base: string(input.base, sha), checkout: string(input.checkout, sha), jobs }
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
  const training = manifest.stage === 'training'
  assert.equal(report.status, training ? 'awaiting five-runner batch aggregation' : job.kind === 'holdout' ? 'passed' : 'control validated')
  assert.equal(report.mode, training ? 'calibration' : job.kind === 'holdout' ? 'verification' : 'control')
  assert.equal(report.control, training || job.kind === 'holdout' ? undefined : job.kind)
  assert.equal(report.frozenAggregate, manifest.frozenAggregate)
  assert.equal(report.protocol, measurementProtocol)
  assert.equal(report.eligibleForCalibration, training)
  assert.equal(report.prHead, manifest.head)
  assert.equal(report.base, manifest.base)
  assert.equal(report.checkout, manifest.checkout)
  assert.deepEqual(report.checkoutParents, [manifest.checkoutBase, manifest.head])
  string(report.headTree, /^[a-f0-9]{40}$/u)
  assert.equal(report.checkoutTree, report.headTree)
  assert.equal(report.dirty, '')
  assert.equal(report.platform, 'linux')
  assert.equal(report.arch, 'x64')
  string(report.release, /^[^\r\n\0]{1,256}$/u)
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
  assert.equal(runner.image, 'ubuntu24')
  for (const key of ['name', 'image', 'imageVersion']) string(runner[key], /^.{1,256}$/u)
  for (const [key, expected] of Object.entries({ repetitions: 3, coldWarmup: 2, coldSamples: 20, lifecycleWarmup: 3, lifecycleSamples: 30 })) assert.equal(report[key], expected)
  const order: ['base' | 'head', number][] = training ? [['base', 0], ['head', 0], ['head', 1], ['base', 1], ['base', 2], ['head', 2]] : [['head', 0], ['base', 0], ['base', 1], ['head', 1], ['head', 2], ['base', 2]]
  assert.deepEqual(report.order, order)
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
  for (const key of ['runnerCancellation', 'artifactLimits', 'networkChecks', 'evidenceChecks', 'invocationChecks', 'retainedRunnerCancellation']) assert.equal(report[key], 'passed')
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
  const referenceInput = record(report.reference)
  const reference = { platform: string(referenceInput.platform, /^linux$/u), arch: string(referenceInput.arch, /^x64$/u), node: string(referenceInput.node, /^v[0-9.]+$/u), browser: string(referenceInput.browser, /^[0-9.]+$/u) }
  const chromium = browsers.filter(browser => browser.name === 'chromium')
  assert.equal(chromium.length, 1)
  assert.equal(chromium[0]?.browserVersion, reference.browser)
  assert.equal(reference.platform, report.platform)
  assert.equal(reference.arch, report.arch)
  assert.equal(reference.node, report.node)
  const summary = record(report.summaries)
  const base = summaries(summary.base)
  const head = summaries(summary.head)
  if (training) requireCandidateHeap([...base, ...head])
  else requireReleased([...base, ...head])
  const invocation = record(report.invocation)
  const points = array(invocation.points, 7).map(parseInvocationPoint)
  const growth = invocationGrowth(points, order)
  assert.deepEqual(invocation.growth, growth)
  const expectedPayload = job.kind === 'cycle-1' ? 1024 : job.kind === 'cycle-8' ? 8192 : job.kind === 'cycle-32' ? 32768 : job.kind === 'intertrial' ? 245760 : 0
  const perTrial = expectedPayload === 0 ? 0 : job.kind === 'intertrial' ? 1 : 30
  for (const [index, point] of points.entries()) assert.deepEqual(point.retention, { allocations: index * perTrial, arrays: index * perTrial, nominalBytes: index * perTrial * expectedPayload, released: false })
  const retention = record(report.retention)
  assert.deepEqual(retention.beforeRelease, points[6]?.retention)
  const releasedOwner = { allocations: 6 * perTrial, arrays: 0, nominalBytes: 0, released: true }
  assert.deepEqual(parseRetention(retention.afterRelease), releasedOwner)
  assert.deepEqual(parseRetention(report.retentionCleanup), releasedOwner)
  return { report, base, head, reference, points, growth }
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
  assert.equal(trial.protocol, measurementProtocol)
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
    if (key !== 'heapUsed') assert(growth <= 0)
    assert.equal(summary[`runner${key}ReleasedGrowth`], growth)
  }
}

export function validateFinalization(trial: Record<string, unknown>, pending: Record<string, unknown>, terminal: Record<string, unknown>, arm: 'base' | 'head', repeat: number): void {
  for (const input of [trial, pending, terminal]) {
    assert.equal(input.protocol, measurementProtocol)
    assert.equal(input.arm, arm)
    assert.equal(input.repeat, repeat)
  }
  assert.equal(pending.runnerAfter, undefined)
  assert.equal(pending.after, undefined)
  assert.equal(record(terminal.phase).phase, 'released')
  assert.deepEqual(trial, { ...pending, runnerAfter: terminal.runnerAfter, after: terminal.after, phases: [...array(pending.phases, 25), terminal.phase] }, 'Finalization changed raw evidence')
}

export function aggregate(datasets: Dataset[]): { budgets: Budgets; comparisons: ReturnType<typeof compare>[]; invocations: Record<string, boolean>[] } {
  assert.equal(datasets.length, 5)
  const first = datasets[0]
  assert(first)
  const runners = datasets.map(dataset => record(dataset.report.runner).name)
  assert.equal(new Set(runners).size, 5, 'Runner identities must be distinct')
  for (const dataset of datasets) {
    assert.deepEqual(dataset.reference, first.reference)
    assert.equal(dataset.report.protocol, measurementProtocol)
    assert.equal(record(dataset.report.runner).image, 'ubuntu24')
    for (const key of ['checkout', 'headTree', 'checkoutParents', 'prHead', 'base', 'node', 'platform', 'arch', 'lockSha256', 'playwright', 'browserDistribution', 'assets', 'viewport', 'throttling', 'cache']) assert.deepEqual(dataset.report[key], first.report[key], `Mismatched ${key}`)
    requireCandidateHeap([...dataset.base, ...dataset.head])
  }
  const budgets = calibrateBatch(datasets.map(dataset => dataset.base), first.reference)
  budgets.heap = fitHeapLimits(datasets.map(dataset => [...dataset.base, ...dataset.head].map(summary => {
    const value = summary.runnerheapUsedReleasedGrowth
    assert(value !== undefined)
    return value
  })), datasets.map(dataset => dataset.growth))
  budgets.absolute.runnerheapUsedReleasedGrowth = budgets.heap.terminal
  budgets.pairedMargin.runnerheapUsedReleasedGrowth = budgets.heap.terminal
  const invocations = datasets.map(dataset => heapGates(dataset.growth, budgets.heap))
  assert(invocations.every(gates => Object.values(gates).every(Boolean)), 'Training invocation gate failed')
  const comparisons = datasets.map(dataset => compare(dataset.base, dataset.head, budgets))
  assert(comparisons.every(comparison => Object.keys(comparison.gates).length === 336 && Object.values(comparison.gates).every(Boolean)), 'Calibration batch failed prospective gates')
  return { budgets, comparisons, invocations }
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
      required.push(`${trial}.json`, `${trial}-pending.json`, `${trial}-terminal.json`, `${trial}-summary.json`, `${trial}-baseline.heapsnapshot`, `${trial}-end.heapsnapshot`)
    }
  }
  for (const mode of ['production', 'development']) {
    required.push(`${mode}-smoke.json`)
    for (const width of [1280, 768, 375]) required.push(`${mode}-${String(width)}.png`, `${mode}-${String(width)}-terminal.png`, `${mode}-${String(width)}.zip`)
  }
  for (const name of [...scenarios.map(scenario => `failure-${scenario}`), 'runner', 'runner-forced', 'runner-retained-cancel', 'runner-retained-report-collision']) required.push(`${name}-termination.json`)
  required.push('retention-release.json', 'retention-cleanup.json', 'runner-retained-cancel-cancellation.json', 'runner-retained-report-collision-cancellation.json', ...Array.from({ length: 7 }, (_, index) => `invocation-${String(index)}.json`))
  const invocationEntries = entries.filter(entry => entry.startsWith(`${prefix}invocation-`) && !entry.slice(prefix.length).includes('/'))
  assert.deepEqual(invocationEntries.sort(), Array.from({ length: 7 }, (_, index) => `${prefix}invocation-${String(index)}.json`), 'Unexpected invocation checkpoint inventory')
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
    for (let repeat = 0; repeat < 3; repeat++) {
      const name = `${prefix}${arm}-${String(repeat)}`
      const trial = record(await readArchive(job, `${name}.json`, 4 * 1024 * 1024))
      const pending = record(await readArchive(job, `${name}-pending.json`, 4 * 1024 * 1024))
      const terminal = record(await readArchive(job, `${name}-terminal.json`, 65536))
      validateFinalization(trial, pending, terminal, arm, repeat)
      validateTrial(trial, dataset, arm, repeat)
      assert.deepEqual(await readArchive(job, `${name}-summary.json`, 65536), { arm, repeat, browserVersion: dataset.reference.browser, metrics: dataset[arm][repeat] })
    }
  }
  for (const name of [...scenarios.map(scenario => `failure-${scenario}`), 'runner', 'runner-forced', 'runner-retained-cancel', 'runner-retained-report-collision']) {
    const termination = record(await readArchive(job, `${prefix}${name}-termination.json`, 64 * 1024))
    array(termination.failures, 0)
    assert.equal(termination.ownershipConfirmed, true)
    assert.equal(termination.temporaryRemoved, true)
    const remaining = record(termination.remaining)
    array(remaining.pids, 0)
    for (const key of ['rss', 'descriptors', 'processes']) assert.equal(remaining[key], 0)
  }
  for (let sequence = 0; sequence < 7; sequence++) {
    const point = parseInvocationPoint(await readArchive(job, `${prefix}invocation-${String(sequence)}.json`, 4096))
    assert.deepEqual(point, dataset.points[sequence], 'Raw invocation does not match report')
  }
  assert.deepEqual(await readArchive(job, `${prefix}retention-release.json`, 4096), dataset.report.retention)
  assert.deepEqual(await readArchive(job, `${prefix}retention-cleanup.json`, 4096), dataset.report.retentionCleanup)
  for (const name of ['cancel', 'report-collision']) {
    const proof = record(await readArchive(job, `${prefix}runner-retained-${name}-cancellation.json`, 65536))
    assert.equal(proof.code, 1)
    assert.equal(proof.deadline, false)
    assert.deepEqual(proof.acquired, { allocations: 1, arrays: 1, nominalBytes: 8192, released: false })
    assert.deepEqual(proof.released, { allocations: 1, arrays: 0, nominalBytes: 0, released: true })
    if (name === 'cancel') {
      assert.equal(proof.bytes, 0)
      assert.equal(record(proof.report).status, 'failed')
      assert.deepEqual(record(proof.report).retentionCleanup, proof.released)
    } else {
      assert(number(proof.bytes) > 0 && number(proof.bytes) <= 65536)
      assert.equal(proof.failure, 'report-path-collision')
    }
  }
  return dataset
}

export function validateStageChanges(paths: string[]): void {
  const allowed = ['web/tests/browser/budgets.json', 'web/tests/browser/frozen-training.json', '.github/workflows/ci.yml', 'knowledge/research/console-browser-checks-2026-10-21.md', 'knowledge/research/browser-heap-tolerance-2026-10-22.md']
  assert(paths.every(path => allowed.includes(path)), 'Changed measurement inputs between stages')
}

async function validateStage(manifest: Manifest, datasets: Dataset[], frozenPath: string): Promise<unknown> {
  assert(manifest.stage === 'validation')
  const frozenBytes = await readFile(frozenPath)
  assert(frozenBytes.byteLength <= 4 * 1024 * 1024)
  assert.equal(createHash('sha256').update(frozenBytes).digest('hex'), manifest.frozenAggregate)
  const frozen = record(JSON.parse(frozenBytes.toString('utf8')))
  const training = parseManifest(frozen.manifest)
  assert(training.stage === 'training' && training.base === manifest.base && training.runId !== manifest.runId)
  const trainingDatasets: Dataset[] = []
  for (const job of training.jobs) trainingDatasets.push(await loadDataset(training, job))
  const rebuilt = aggregate(trainingDatasets)
  assert.deepEqual(frozen, { manifest: training, ...rebuilt }, 'Frozen aggregate does not match training evidence')
  const budgets = parseBudgets(frozen.budgets)
  await execute('git', ['merge-base', '--is-ancestor', training.head, manifest.head])
  const { stdout } = await execute('git', ['diff', '--name-only', training.head, manifest.head])
  validateStageChanges(stdout.trim().split('\n'))
  const trainingWorkflow = await execute('git', ['show', `${training.head}:.github/workflows/ci.yml`], { maxBuffer: 1024 * 1024 })
  const validationWorkflow = await execute('git', ['show', `${manifest.head}:.github/workflows/ci.yml`], { maxBuffer: 1024 * 1024 })
  validateWorkflowTransition(trainingWorkflow.stdout, validationWorkflow.stdout)
  assert.equal(new Set(datasets.map(dataset => record(dataset.report.runner).name)).size, 9, 'Runner identities must be distinct')
  const first = trainingDatasets[0]
  assert(first)
  const results: unknown[] = []
  for (const [index, dataset] of datasets.entries()) {
    const job = manifest.jobs[index]
    assert(job)
    for (const key of ['base', 'node', 'platform', 'arch', 'lockSha256', 'playwright', 'browserDistribution', 'assets', 'viewport', 'throttling', 'cache']) assert.deepEqual(dataset.report[key], first.report[key], 'Changed measurement inputs between stages')
    assert.deepEqual(dataset.reference, budgets.reference)
    const comparison = compare(dataset.base, dataset.head, budgets)
    const invocation = heapGates(dataset.growth, budgets.heap)
    assert.deepEqual(dataset.report.comparison, comparison)
    assert.deepEqual(dataset.report.invocationGates, invocation)
    if (job.kind === 'holdout') {
      assert(Object.values(comparison.gates).every(Boolean) && Object.values(invocation).every(Boolean), 'Holdout gate failed')
      results.push({ slot: job.slot, comparison, invocation })
    } else {
      assert(job.kind !== 'training')
      let recovery = { passed: true, gates: {} }
      if (job.kind === 'intertrial') {
        const prefix = job.reportPath.slice(0, -'report.json'.length)
        recovery = verifyRecovery(await readArchive(job, `${prefix}retention-recovery.json`, 65536), budgets.heap)
      }
      assert.deepEqual(dataset.report.recovery, recovery)
      const classification = classifyControl(job.kind, comparison.gates, invocation, true, recovery.passed)
      assert.deepEqual(dataset.report.controlResult, classification)
      assert(classification.valid, 'Invalid sensitivity control')
      results.push({ slot: job.slot, comparison, invocation, classification })
    }
  }
  return { frozenAggregate: manifest.frozenAggregate, results }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  let stage: 'arguments' | 'manifest' | 'archive' | 'aggregate' | 'output' = 'arguments'
  try {
    const verify = process.argv[2] === '--verify'
    assert.equal(process.argv.length, verify ? 6 : 4)
    const [manifestPath, outputPath, frozenPath] = process.argv.slice(verify ? 3 : 2)
    assert(manifestPath && outputPath)
    stage = 'manifest'
    const manifest = parseManifest(await readBounded(manifestPath, 64 * 1024))
    assert.equal(manifest.stage, verify ? 'validation' : 'training')
    stage = 'archive'
    const datasets: Dataset[] = []
    for (const job of manifest.jobs) datasets.push(await loadDataset(manifest, job))
    stage = 'aggregate'
    const result = verify && frozenPath ? await validateStage(manifest, datasets, frozenPath) : aggregate(datasets)
    stage = 'output'
    const output = JSON.stringify({ manifest, ...record(result) }, null, 2)
    assert(Buffer.byteLength(output) <= 4 * 1024 * 1024)
    await writeFile(outputPath, `${output}\n`, { flag: 'wx' })
    process.stdout.write(`${JSON.stringify({ event: 'browser.calibration.completed', level: 'INFO', jobs: datasets.length, stage: manifest.stage })}\n`)
  } catch (error) {
    const failure = error instanceof SyntaxError ? 'invalid-json' : error instanceof assert.AssertionError ? 'invalid-evidence' : 'operation-failed'
    process.exitCode = 1
    process.stderr.write(`${JSON.stringify({ event: 'browser.calibration.failed', level: 'ERROR', stage, failure })}\n`)
  }
}
