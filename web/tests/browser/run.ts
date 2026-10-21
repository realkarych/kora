import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { once } from 'node:events'
import { mkdir, mkdtemp, readFile, rm } from 'node:fs/promises'
import { writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { arch, cpus, platform, release, tmpdir, totalmem } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { getHeapCodeStatistics } from 'node:v8'
import { build } from 'vite'
import { assetHashes, buildBase, git, repository, webRoot } from './build.ts'
import { failureChecks, runnerCancellation } from './negative.ts'
import { collectRunner, measure, smoke } from './measure.ts'
import type { Trial } from './trial.ts'
import { finalizeTrial, persistTerminal, persistTrial } from './trial-evidence.ts'
import type { Terminal, TrialSummary } from './trial-evidence.ts'
import { nativeResources } from './resources.ts'
import { evidenceChecks } from './trial-evidence-checks.ts'
import { calibrate, compare, measurementProtocol, parseBudgets, requireReleased } from './protocol.ts'
import { protocolChecks } from './protocol-checks.ts'
import { networkChecks } from './network.ts'
import { ArtifactLimit } from './artifacts.ts'
import { closeOwners } from './ownership.ts'
import { artifactLimits } from './limits.ts'
import { diagnosticSnapshot } from './diagnostic-snapshot.ts'
import { diagnosticPlan, persistCheckpoint, verifyCheckpoints } from './diagnostic-checkpoint.ts'

const diagnostic = process.argv.includes('--diagnostic')
if (diagnostic) assert.equal(process.env.KORA_BROWSER_DIAGNOSTIC, diagnosticPlan, 'Use the sanitized diagnostic launcher')
const calibration = process.argv.includes('--calibrate')
const smokeOnly = process.argv.includes('--smoke')
const cancelProbe = process.env.KORA_BROWSER_CANCEL_PROBE === '1'
assert(process.argv.slice(2).every(argument => ['--calibrate', '--smoke', '--diagnostic'].includes(argument)))
assert(Number(calibration) + Number(smokeOnly) + Number(diagnostic) <= 1)
const outputRoot = resolve(process.env.KORA_BROWSER_OUTPUT ?? join(repository, 'measurements', 'browser'))
await mkdir(outputRoot, { recursive: true })
const output = await mkdtemp(join(outputRoot, 'run-'))
const evidenceRoot = diagnostic ? outputRoot : output
const temporary = await mkdtemp(join(tmpdir(), 'kora-browser-'))
const cancellation = new AbortController()
const stop = (): void => { cancellation.abort(); void closeOwners() }
const limits = new ArtifactLimit(output, stop)
process.once('SIGTERM', stop)
process.once('SIGINT', stop)
const trials: TrialSummary[] = []
const report: Record<string, unknown> = {
  output, protocol: measurementProtocol, status: 'running', mode: diagnostic ? 'collector-diagnostic-v1' : calibration ? 'calibration' : smokeOnly ? 'smoke' : 'verification',
  diagnosticEnvironmentKeys: diagnostic ? Object.keys(process.env).sort() : undefined,
  eligibleForCalibration: !diagnostic, diagnosticPlan: diagnostic ? diagnosticPlan : undefined,
  command: process.argv.slice(1), nodeFlags: process.execArgv, node: process.version, platform: platform(), release: release(), arch: arch(), cpus: cpus().length,
  cpuModels: [...new Set(cpus().map(cpu => cpu.model))], totalMemory: totalmem(),
  runner: { runId: process.env.GITHUB_RUN_ID, attempt: process.env.GITHUB_RUN_ATTEMPT, job: process.env.GITHUB_JOB, slot: process.env.KORA_BROWSER_CALIBRATION_SLOT, name: process.env.RUNNER_NAME, image: process.env.ImageOS, imageVersion: process.env.ImageVersion },
  viewport: { width: 1280, height: 900 }, throttling: 'none', cache: 'fresh context for cold loads; request routing disables HTTP cache',
  repetitions: 3, coldWarmup: 2, coldSamples: 20, lifecycleWarmup: 3, lifecycleSamples: 30,
  statisticalUnit: 'browser process pair', referenceCounters: process.platform === 'linux' ? 'available' : 'unverified',
}
const save = (): void => {
  const data = JSON.stringify(report, null, 2)
  assert(Buffer.byteLength(data) <= 4 * 1024 * 1024, 'Report exceeded 4 MiB bound')
  writeFileSync(join(output, 'report.json'), data)
}
const saveTrial = (trial: Trial): void => {
  persistTrial(output, trial, evidenceRoot)
}
const started = performance.now()
try {
  const checkout = await git(['rev-parse', 'HEAD'])
  const prHead = process.env.KORA_BROWSER_REVISION ?? checkout
  assert(/^[a-f0-9]{40}$/u.test(prHead), 'Invalid PR revision')
  const revisions = (await git(['rev-list', '--parents', '-n', '1', checkout])).split(' ')
  assert(revisions.includes(prHead), 'PR revision does not match checkout or its merge parents')
  report.checkout = checkout
  report.checkoutParents = revisions.slice(1)
  report.checkoutTree = await git(['rev-parse', `${checkout}^{tree}`])
  report.headTree = await git(['rev-parse', `${prHead}^{tree}`])
  report.prHead = prHead
  report.dirty = await git(['status', '--porcelain', '--untracked-files=normal'])
  if (!smokeOnly) assert.equal(report.dirty, '', 'Reference measurements require a clean committed tree')
  const require = createRequire(import.meta.url)
  const playwrightPackage = require.resolve('playwright/package.json')
  const corePackage = createRequire(playwrightPackage).resolve('playwright-core/package.json')
  const metadata: unknown = JSON.parse(await readFile(playwrightPackage, 'utf8'))
  const distribution: unknown = JSON.parse(await readFile(join(dirname(corePackage), 'browsers.json'), 'utf8'))
  report.playwright = metadata
  report.browserDistribution = distribution
  report.lockSha256 = createHash('sha256').update(await readFile(join(webRoot, 'pnpm-lock.yaml'))).digest('hex')
  assert(process.env.NODE_ENV === undefined || process.env.NODE_ENV === 'production', 'Browser measurements require a production build environment')
  if (!cancelProbe) await build({ root: webRoot, configFile: join(webRoot, 'vite.config.ts'), logLevel: 'error' })
  const headAssets = await assetHashes(webRoot)
  report.headAssetsBeforeFixtures = headAssets
  const readyForCancellation = async (pids: number[]): Promise<void> => {
    if (diagnostic) process.send?.({ event: 'owned', pids })
    if (!cancelProbe) return
    const cancelled = once(process, 'SIGTERM')
    process.send?.({ event: 'ready', pids })
    await cancelled
    assert(!cancellation.signal.aborted, 'Browser runner interrupted')
  }
  report.productionSmoke = await smoke(webRoot, 'production', output, readyForCancellation)
  report.developmentSmoke = await smoke(webRoot, 'development', output)
  report.failureChecks = await failureChecks(webRoot, output)
  await runnerCancellation(output)
  await runnerCancellation(output, true)
  report.runnerCancellation = 'passed'
  await artifactLimits(webRoot, output)
  report.artifactLimits = 'passed'
  await networkChecks(output)
  report.networkChecks = 'passed'
  assert.deepEqual(await assetHashes(webRoot), headAssets, 'Browser fixtures changed the measured head assets')
  save()
  if (!smokeOnly) {
    const base = process.env.KORA_BROWSER_BASE
    assert(base && /^[a-f0-9]{40}$/u.test(base), 'Set KORA_BROWSER_BASE to the exact comparison commit')
    report.base = base
    const baseRoot = await buildBase(base, temporary)
    report.assets = { base: await assetHashes(baseRoot), head: await assetHashes(webRoot) }
    const order: ['base' | 'head', number][] = calibration || diagnostic
      ? [['base', 0], ['head', 0], ['head', 1], ['base', 1], ['base', 2], ['head', 2]]
      : [['head', 0], ['base', 0], ['base', 1], ['head', 1], ['head', 2], ['base', 2]]
    report.order = order
    save()
    for (const [arm, repeat] of order) {
      assert(!cancellation.signal.aborted, 'Browser runner interrupted')
      try {
        await measure(arm === 'base' ? baseRoot : webRoot, arm, repeat, output, saveTrial, diagnostic ? cancellation.signal : undefined)
      } finally {
        await collectRunner()
        const runnerAfter = process.memoryUsage()
        if (diagnostic) {
          persistCheckpoint(output, arm, repeat, 'terminal', runnerAfter, getHeapCodeStatistics(), evidenceRoot)
          if (arm === 'head' && repeat === 0) await diagnosticSnapshot(output, 'node-terminal', cancellation.signal)
        }
        const after = await nativeResources([process.pid])
        const phase = { phase: 'released', native: await nativeResources([process.pid]), runner: process.memoryUsage() }
        const terminal: Terminal = { protocol: measurementProtocol, arm, repeat, runnerAfter, after, phase }
        persistTerminal(output, terminal, evidenceRoot)
        trials.push(finalizeTrial(output, terminal, cancellation.signal, evidenceRoot))
      }
    }
    assert.equal(trials.length, 6)
    evidenceChecks(output, evidenceRoot)
    report.evidenceChecks = 'passed'
    const grouped = (arm: 'base' | 'head'): Record<string, number>[] => trials.filter(trial => trial.arm === arm).sort((a, b) => a.repeat - b.repeat).map(trial => trial.metrics)
    const baseSummary = grouped('base')
    const headSummary = grouped('head')
    report.summaries = { base: baseSummary, head: headSummary }
    const browser = trials[0]?.browserVersion
    assert(browser && trials.every(trial => trial.browserVersion === browser))
    const reference = { platform: process.platform, arch: process.arch, node: process.version, browser }
    if (diagnostic) {
      verifyCheckpoints(output)
      report.checkpoints = 'passed'
      protocolChecks()
      report.status = 'diagnostic complete; calibration and performance unverified'
    } else if (calibration) {
      assert.equal(process.platform, 'linux', 'Reference calibration requires Linux')
      assert.equal(process.arch, 'x64', 'Reference calibration requires x86_64')
      requireReleased([...baseSummary, ...headSummary])
      const proposed = calibrate(baseSummary, reference)
      protocolChecks()
      report.proposedBudgets = proposed
      report.status = 'calibrated; frozen budgets and independent verification still required'
    } else {
      const input: unknown = JSON.parse(await readFile(join(import.meta.dirname, 'budgets.json'), 'utf8'))
      const budgets = parseBudgets(input)
      protocolChecks()
      assert.deepEqual(reference, budgets.reference, 'Reference environment mismatch')
      const comparison = compare(baseSummary, headSummary, budgets)
      report.comparison = comparison
      save()
      assert(Object.values(comparison.gates).every(Boolean), 'Browser measurement gate failed')
      report.status = 'passed'
    }
  } else report.status = 'smoke passed; performance unverified'
} catch (error) {
  report.status = 'failed'
  report.failure = error instanceof Error ? { name: error.name, message: error.message.slice(0, 2048) } : { name: 'UnknownError' }
  process.exitCode = 1
} finally {
  await closeOwners()
  limits.close()
  if (cancellation.signal.aborted || limits.failed) { report.status = 'failed'; process.exitCode = 1 }
  report.artifactLimitFailed = limits.failed
  report.seconds = (performance.now() - started) / 1000
  await rm(temporary, { recursive: true, force: true })
  save()
  process.removeListener('SIGTERM', stop)
  process.removeListener('SIGINT', stop)
  process.stdout.write(`${JSON.stringify({ event: 'browser.checks.completed', level: process.exitCode === 1 ? 'ERROR' : 'INFO', status: report.status, seconds: report.seconds })}\n`)
  if (process.connected) process.disconnect()
}
