import assert from 'node:assert/strict'
import { calibrate, calibrateBatch, compare, measurementMetrics, measurementProtocol, parseBudgets } from './protocol.ts'
import { aggregate, parseManifest, validateArchiveEntries, validateDataset, validateStageChanges } from './calibration.ts'

export function protocolChecks(): void {
  const fixture = Object.fromEntries(measurementMetrics.map(key => [key, 0]))
  const template = calibrate([fixture, fixture, fixture], { platform: 'linux', arch: 'x64', node: 'v24.22.0', browser: '156.0.8078.4' })
  const metric = 'usedMedianGrowth'
  const summary = (growth: number): Record<string, number> => ({ ...Object.fromEntries(Object.keys(template.absolute).map(key => [key, 0])), [metric]: growth })
  const releases = [-7000000, -200000, -280000].map(summary)
  const quiescent = [0, -100000, -250000].map(summary)

  const releaseBudgets = calibrate(releases, template.reference)
  const released = compare(releases, quiescent, releaseBudgets)
  const retained = compare(releases, [1, 0, 0].map(summary), releaseBudgets)

  assert.equal(releaseBudgets.absolute[metric], 0)
  assert.equal(releaseBudgets.pairedMargin[metric], 0)
  assert(Object.values(released.gates).every(Boolean))
  assert.equal(retained.gates[`${metric}-absolute-head-0`], false)
  assert.equal(retained.gates[`${metric}-paired`], false)

  const zero = summary(0)
  const zeros = [zero, zero, zero]

  const zeroBudgets = calibrate(zeros, template.reference)

  for (const [key, value] of Object.entries(zeroBudgets.absolute)) {
    assert.equal(value, 0)
    assert.equal(zeroBudgets.pairedMargin[key], 0)
  }
  const variable = (value: number): Record<string, number> => ({ ...zero, loadMedian: value })
  const base = [10, 12, 14].map(variable)
  const medianRegression = [16, 22, 24].map(variable)
  const individualRegression = [11, 13, 31].map(variable)

  const variableBudgets = calibrate(base, template.reference)
  const medianResult = compare(base, medianRegression, variableBudgets)
  const individualResult = compare(base, individualRegression, variableBudgets)

  assert.equal(variableBudgets.absolute.loadMedian, 22)
  assert.equal(variableBudgets.pairedMargin.loadMedian, 8)
  assert.equal(medianResult.gates['loadMedian-paired'], false)
  assert.equal(individualResult.gates['loadMedian-paired'], false)

  const clusters = [40, 50, 40, 50, 40].map(value => [value, value, value].map(variable))

  const batch = calibrateBatch(clusters, template.reference)
  const pairedRegression = compare(clusters[0] ?? [], [51, 51, 51].map(variable), batch)

  assert.equal(batch.absolute.loadMedian, 70)
  assert.equal(batch.pairedMargin.loadMedian, 10)
  assert.equal(Object.keys(batch.absolute).length, 33)
  assert.equal(pairedRegression.gates['loadMedian-paired'], false)
  assert.throws(() => calibrateBatch(clusters.slice(1), template.reference))
  assert.throws(() => calibrateBatch([...clusters.slice(1), [zero, zero]], template.reference))
  const extraBudget = { ...batch, absolute: { ...batch.absolute, extra: 0 } }
  const missingBudget = { ...batch, absolute: Object.fromEntries(Object.entries(batch.absolute).filter(([key]) => key !== 'loadMedian')) }

  assert.throws(() => parseBudgets(extraBudget))
  assert.throws(() => parseBudgets(missingBudget))
  assert.deepEqual(parseBudgets(batch), batch)

  const manifest = {
    stage: 'training', checkoutBase: 'b'.repeat(40), runId: '123', attempt: '1', head: 'a'.repeat(40), base: 'b'.repeat(40), checkout: 'c'.repeat(40),
    jobs: [1, 2, 3, 4, 5].map(slot => ({ slot, kind: 'training', jobId: String(slot), artifactId: String(slot + 10), archive: `/tmp/job-${String(slot)}.zip`, sha256: String(slot).repeat(64), reportPath: 'run-example/report.json', conclusion: 'success', name: `Console browser calibration (${String(slot)})`, artifactName: `console-browser-calibration-${String(slot)}` })),
  }
  const incomplete = { ...manifest, jobs: manifest.jobs.slice(1) }
  const duplicate = { ...manifest, jobs: [...manifest.jobs.slice(1), manifest.jobs[1]] }
  const failed = { ...manifest, jobs: manifest.jobs.map(job => ({ ...job, conclusion: 'cancelled' })) }
  const datasets = clusters.map((base, index) => ({ report: { protocol: measurementProtocol, runner: { name: String(index), image: 'ubuntu24', imageVersion: String(index) }, lockSha256: 'a'.repeat(64) }, base, head: base, reference: template.reference }))

  const parsed = parseManifest(manifest)
  const aggregated = aggregate(datasets)

  assert.deepEqual(parseManifest(parsed), parsed)
  assert.throws(() => parseManifest({ ...manifest, attempt: '2' }))
  assert.throws(() => parseManifest({ ...manifest, jobs: manifest.jobs.map(job => ({ ...job, kind: 'cycle-8' })) }))
  validateStageChanges(['web/tests/browser/budgets.json', '.github/workflows/ci.yml'])
  assert.throws(() => { validateStageChanges(['web/tests/browser/measure.ts']) })
  assert.throws(() => { validateStageChanges(['web/tests/browser/protocol.ts']) })
  assert.equal(parsed.jobs.length, 5)
  assert.equal(aggregated.comparisons.length, 5)
  assert(aggregated.comparisons.every(result => Object.keys(result.gates).length === 231 && Object.values(result.gates).every(Boolean)))
  assert.throws(() => parseManifest(incomplete))
  assert.throws(() => parseManifest(duplicate))
  assert.throws(() => parseManifest(failed))
  assert.throws(() => aggregate(datasets.slice(1)))
  const duplicateDataset = datasets[1]
  assert(duplicateDataset)
  assert.throws(() => aggregate([...datasets.slice(1), duplicateDataset]))
  assert.throws(() => aggregate(datasets.map((dataset, index) => index === 0 ? { ...dataset, report: { ...dataset.report, lockSha256: 'b'.repeat(64) } } : dataset)))

  const job = parsed.jobs[0]
  assert(job)
  const cleanup = { remaining: { pids: [], rss: 0, descriptors: 0, processes: 0 }, failures: [], proxySockets: 0, forced: false }
  const report = {
    protocol: measurementProtocol, eligibleForCalibration: true,
    status: 'awaiting five-runner batch aggregation', mode: 'calibration',
    prHead: parsed.head, base: parsed.base, checkout: parsed.checkout, checkoutParents: [parsed.base, parsed.head],
    checkoutTree: 'd'.repeat(40), headTree: 'd'.repeat(40), dirty: '', platform: 'linux', arch: 'x64', release: '6.17.0-parser-fixture',
    node: template.reference.node, referenceCounters: 'available', artifactLimitFailed: false,
    seconds: 100, cpus: 4, totalMemory: 1000000000, cpuModels: ['parser-fixture'], lockSha256: 'e'.repeat(64), nodeFlags: [],
    runner: { runId: parsed.runId, attempt: parsed.attempt, job: 'browser', slot: String(job.slot), name: 'runner-1', image: 'ubuntu24', imageVersion: 'fixture' },
    repetitions: 3, coldWarmup: 2, coldSamples: 20, lifecycleWarmup: 3, lifecycleSamples: 30,
    order: [['base', 0], ['head', 0], ['head', 1], ['base', 1], ['base', 2], ['head', 2]],
    assets: { base: { asset: 'e'.repeat(64) }, head: { asset: 'e'.repeat(64) } }, headAssetsBeforeFixtures: { asset: 'e'.repeat(64) },
    productionSmoke: { diagnostics: { events: [], overflow: false }, cleanup }, developmentSmoke: { diagnostics: { events: [], overflow: false }, cleanup },
    reportCollision: 'passed', preflightChecks: 'passed', evidenceChecks: 'passed', runnerCancellation: 'passed', artifactLimits: 'passed', networkChecks: 'passed',
    failureChecks: ['startup', 'navigation', 'assertion', 'deadline', 'cancellation', 'acquisition'].map(scenario => ({ scenario, code: 0, payload: { expected: true, cleanup } })),
    playwright: { name: 'playwright', version: '1.64.1' }, browserDistribution: { browsers: [{ name: 'chromium', revision: '1248', browserVersion: template.reference.browser }] },
    viewport: { width: 1280, height: 900 }, throttling: 'none', cache: 'fresh context for cold loads; request routing disables HTTP cache',
    reference: template.reference, summaries: { base: zeros, head: zeros },
  }
  const prefix = 'run-fixture/'
  const evidence = ['report.json', 'runner-report-collision-cancellation.json', 'cancelled-runner/run-nested/report.json', ...['base', 'head'].flatMap(arm => [0, 1, 2].flatMap(repeat => [`${arm}-${String(repeat)}.json`, `${arm}-${String(repeat)}-pending.json`, `${arm}-${String(repeat)}-terminal.json`, `${arm}-${String(repeat)}-summary.json`, `${arm}-${String(repeat)}-baseline.heapsnapshot`, `${arm}-${String(repeat)}-end.heapsnapshot`])), ...['production', 'development'].flatMap(mode => [`${mode}-smoke.json`, ...[1280, 768, 375].flatMap(width => [`${mode}-${String(width)}.png`, `${mode}-${String(width)}-terminal.png`, `${mode}-${String(width)}.zip`])]), ...['startup', 'navigation', 'assertion', 'deadline', 'cancellation', 'acquisition'].map(scenario => `failure-${scenario}-termination.json`), 'runner-termination.json', 'runner-forced-termination.json', 'runner-report-collision-termination.json'].map(name => `${prefix}${name}`)

  const validated = validateDataset(report, parsed, job)
  validateArchiveEntries(evidence, `${prefix}report.json`)

  assert.equal(validated.base.length, 3)
  for (const release of [undefined, null, '', 7, 'x'.repeat(257), 'kernel\nextra']) assert.throws(() => validateDataset({ ...report, release }, parsed, job))
  assert.throws(() => parseBudgets({ ...template, protocol: 'collector-internal-v1' }))
  for (const protocol of [undefined, 'collector-internal-v1', 'collector-diagnostic-v1', 'invocation-heap-v3']) assert.throws(() => validateDataset({ ...report, protocol }, parsed, job))
  assert.throws(() => validateDataset({ ...report, mode: 'collector-diagnostic-v1' }, parsed, job))
  assert.throws(() => validateDataset({ ...report, mode: 'control', control: 'cycle-8' }, parsed, job))
  assert.throws(() => validateDataset({ ...report, frozenAggregate: 'a'.repeat(64) }, parsed, job))
  assert.throws(() => validateDataset({ ...report, eligibleForCalibration: false }, parsed, job))
  assert.throws(() => aggregate(datasets.map((dataset, index) => index === 0 ? { ...dataset, report: { ...dataset.report, protocol: 'collector-internal-v1' } } : dataset)))
  assert.throws(() => aggregate(datasets.map((dataset, index) => index === 0 ? { ...dataset, report: { ...dataset.report, runner: { name: 'other', image: 'ubuntu22' } } } : dataset)))
  for (const key of ['playwright', 'browserDistribution', 'viewport', 'throttling', 'cache']) assert.throws(() => validateDataset({ ...report, [key]: undefined }, parsed, job))
  assert.throws(() => { validateArchiveEntries(evidence.filter(entry => !entry.endsWith('base-0-baseline.heapsnapshot')), `${prefix}report.json`) })
  assert.throws(() => { validateArchiveEntries([...evidence, `${prefix}report.json`], `${prefix}report.json`) })
}
