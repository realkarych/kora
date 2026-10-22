import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { once } from 'node:events'
import { mkdir, mkdtemp, readFile } from 'node:fs/promises'
import { writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { arch, platform, release } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { build } from 'vite'
import { assetHashes, git, repository, webRoot } from './build.ts'
import { failureChecks, runnerCancellation } from './negative.ts'
import { smoke } from './smoke.ts'
import { runnerChecks } from './runner-checks.ts'
import { parseRunnerMode } from './runner-mode.ts'
import { networkChecks } from './network.ts'
import { ArtifactLimit } from './artifacts.ts'
import { closeOwners } from './ownership.ts'
import { artifactLimits } from './limits.ts'
const mode = parseRunnerMode(process.argv.slice(2))
const cancelProbe = process.env.KORA_BROWSER_CANCEL_PROBE === '1'
const outputRoot = resolve(process.env.KORA_BROWSER_OUTPUT ?? join(repository, 'measurements', 'browser'))
await mkdir(outputRoot, { recursive: true })
const output = await mkdtemp(join(outputRoot, 'run-'))
const cancellation = new AbortController()
const stop = (): void => { cancellation.abort(); void closeOwners() }
const limits = new ArtifactLimit(output, stop)
process.once('SIGTERM', stop)
process.once('SIGINT', stop)
const report: Record<string, unknown> = {
  output, protocol: 'console-functional-v1', status: 'running', mode,
  node: process.version, platform: platform(), release: release(), arch: arch(),
  runner: { runId: process.env.GITHUB_RUN_ID, attempt: process.env.GITHUB_RUN_ATTEMPT, job: process.env.GITHUB_JOB, image: process.env.ImageOS, imageVersion: process.env.ImageVersion },
  viewports: [1280, 768, 375], referenceCounters: process.platform === 'linux' ? 'available' : 'unverified',
}
const save = (): void => {
  const data = JSON.stringify(report, null, 2)
  assert(Buffer.byteLength(data) <= 4 * 1024 * 1024, 'Report exceeded 4 MiB bound')
  writeFileSync(join(output, 'report.json'), data)
}
const started = performance.now()
try {
  runnerChecks()
  report.preflightChecks = 'passed'
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
  const require = createRequire(import.meta.url)
  const playwrightPackage = require.resolve('playwright/package.json')
  const corePackage = createRequire(playwrightPackage).resolve('playwright-core/package.json')
  const metadata: unknown = JSON.parse(await readFile(playwrightPackage, 'utf8'))
  const distribution: unknown = JSON.parse(await readFile(join(dirname(corePackage), 'browsers.json'), 'utf8'))
  report.playwright = metadata
  report.browserDistribution = distribution
  report.lockSha256 = createHash('sha256').update(await readFile(join(webRoot, 'pnpm-lock.yaml'))).digest('hex')
  assert(process.env.NODE_ENV === undefined || process.env.NODE_ENV === 'production', 'Browser checks require a production build environment')
  if (!cancelProbe) await build({ root: webRoot, configFile: join(webRoot, 'vite.config.ts'), logLevel: 'error' })
  const headAssets = await assetHashes(webRoot)
  report.assets = headAssets
  const readyForCancellation = async (pids: number[]): Promise<void> => {
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
  await runnerCancellation(output, false, true)
  report.reportCollision = 'passed'
  report.runnerCancellation = 'passed'
  await artifactLimits(webRoot, output)
  report.artifactLimits = 'passed'
  await networkChecks(output)
  report.networkChecks = 'passed'
  assert.deepEqual(await assetHashes(webRoot), headAssets, 'Browser fixtures changed the production assets')
  report.status = 'passed'
} catch (error) {
  report.status = 'failed'
  report.failure = { code: error instanceof SyntaxError ? 'invalid-json' : error instanceof assert.AssertionError ? 'invalid-evidence' : 'operation-failed' }
  process.exitCode = 1
} finally {
  await closeOwners()
  limits.close()
  if (cancellation.signal.aborted || limits.failed) { report.status = 'failed'; process.exitCode = 1 }
  report.artifactLimitFailed = limits.failed
  report.seconds = (performance.now() - started) / 1000
  save()
  process.removeListener('SIGTERM', stop)
  process.removeListener('SIGINT', stop)
  process.stdout.write(`${JSON.stringify({ event: 'browser.checks.completed', level: process.exitCode === 1 ? 'ERROR' : 'INFO', status: report.status, seconds: report.seconds })}\n`)
  if (process.connected) process.disconnect()
}
