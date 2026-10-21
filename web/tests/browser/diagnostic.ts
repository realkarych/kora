import assert from 'node:assert/strict'
import { randomBytes } from 'node:crypto'
import { writeFileSync } from 'node:fs'
import { mkdir, mkdtemp, readFile, readdir } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { diagnosticChecks } from './diagnostic-checks.ts'
import { diagnosticChild } from './diagnostic-process.ts'
import { ArtifactLimit } from './artifacts.ts'
import { inspectArtifacts } from './diagnostic-inspection.ts'

const environmentKeys = ['PATH', 'HOME', 'TMPDIR', 'LANG', 'LC_ALL', 'PLAYWRIGHT_BROWSERS_PATH', 'KORA_BROWSER_BASE', 'KORA_BROWSER_REVISION', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT', 'GITHUB_JOB', 'RUNNER_NAME', 'ImageOS', 'ImageVersion']

export function diagnosticEnvironment(source: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  const environment: NodeJS.ProcessEnv = { NODE_ENV: 'production', GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null' }
  if (process.platform === 'darwin') {
    assert(process.getuid)
    environment.__CF_USER_TEXT_ENCODING = `0x${process.getuid().toString(16).toUpperCase()}:0:0`
  }
  for (const key of environmentKeys) if (source[key] !== undefined) environment[key] = source[key]
  return environment
}

async function run(): Promise<void> {
  assert.equal(process.argv.length, 2, 'Diagnostic launcher takes no arguments')
  const web = resolve(import.meta.dirname, '../..')
  const repository = resolve(web, '..')
  for (const root of [repository, web]) assert((await readdir(root)).every(name => name !== '.env' && !name.startsWith('.env.')), 'Diagnostic builds cannot load environment files')
  await diagnosticChecks()
  const parent = resolve(process.env.KORA_BROWSER_OUTPUT ?? join(repository, 'measurements/browser'))
  await mkdir(parent, { recursive: true })
  const output = await mkdtemp(join(parent, 'diagnostic-'))
  const marker = randomBytes(32).toString('hex')
  const environment = diagnosticEnvironment({ ...process.env, KORA_DIAGNOSTIC_EXCLUDED: marker })
  assert(!Object.values(environment).includes(marker))
  environment.KORA_BROWSER_OUTPUT = output
  environment.KORA_BROWSER_DIAGNOSTIC = 'collector-diagnostic-v1'
  const cancellation = new AbortController()
  const stop = (): void => { cancellation.abort() }
  process.once('SIGTERM', stop)
  process.once('SIGINT', stop)
  const limits = new ArtifactLimit(output, stop)
  const evidence: Record<string, unknown> = { protocol: 'collector-diagnostic-v1', eligibleForCalibration: false, environmentKeys: Object.keys({ ...environment, TMPDIR: '' }).sort(), diagnosticChecks: 'passed', status: 'running' }
  try {
    cancellation.signal.throwIfAborted()
    const result = await diagnosticChild(environment, output, cancellation.signal, marker)
    evidence.child = result
    assert(!result.interrupted && result.markerAbsent && result.captured <= 65536)
    for (const fixture of ['cancel', 'stall'] as const) {
      const directory = join(output, fixture)
      await mkdir(directory)
      const result = await diagnosticChild({ ...environment, KORA_BROWSER_OUTPUT: directory }, directory, cancellation.signal, marker, fixture)
      evidence[fixture] = result
      assert(result.interrupted && result.markerAbsent && result.captured <= 65536)
      if (fixture === 'cancel') assert.equal(result.code, 1)
      else assert.equal(result.signal, 'SIGKILL')
    }
    await inspectArtifacts(output, marker, cancellation.signal)
    const runs = (await readdir(output)).filter(name => name.startsWith('run-'))
    assert.equal(runs.length, 1)
    const report: unknown = JSON.parse(await readFile(join(output, runs[0] ?? '', 'report.json'), 'utf8'))
    assert(report !== null && typeof report === 'object' && 'diagnosticEnvironmentKeys' in report)
    assert.deepEqual(report.diagnosticEnvironmentKeys, evidence.environmentKeys)
    assert.equal(result.code, 0, 'Diagnostic child failed')
    cancellation.signal.throwIfAborted()
    evidence.excludedMarker = 'absent'
    evidence.status = 'diagnostic complete; performance unverified'
  } catch (error) {
    evidence.status = 'failed'
    throw error
  } finally {
    limits.close()
    evidence.artifactLimitFailed = limits.failed
    evidence.interrupted = cancellation.signal.aborted
    if (cancellation.signal.aborted) evidence.status = 'failed'
    writeFileSync(join(output, 'launcher.json'), JSON.stringify(evidence))
    process.removeListener('SIGTERM', stop)
    process.removeListener('SIGINT', stop)
    cancellation.signal.throwIfAborted()
  }
}

try {
  await run()
  process.stdout.write(`${JSON.stringify({ event: 'browser.diagnostic.completed', level: 'INFO', status: 'diagnostic only; performance unverified' })}\n`)
} catch {
  process.exitCode = 1
  process.stderr.write(`${JSON.stringify({ event: 'browser.diagnostic.failed', level: 'ERROR' })}\n`)
}
