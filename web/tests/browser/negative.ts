import assert from 'node:assert/strict'
import { fork, type ChildProcess, type ForkOptions } from 'node:child_process'
import { once } from 'node:events'
import { access, mkdtemp, readFile, readdir, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { Ownership } from './ownership.ts'
import { nativeResources, verifyReleased } from './resources.ts'

async function forkFixture(module: string, args: string[], options: ForkOptions): Promise<{ child: ChildProcess; temporary: string }> {
  const temporary = await mkdtemp(join(tmpdir(), 'kora-browser-fixture-'))
  try {
    const child = fork(module, args, { ...options, env: { ...new Ownership().environment(), ...options.env, TMPDIR: temporary } })
    await once(child, 'spawn')
    return { child, temporary }
  } catch (error) {
    await rm(temporary, { recursive: true, force: true })
    throw error
  }
}

async function temporaryIsRemoved(temporary: string): Promise<boolean> {
  try { await access(temporary); return false }
  catch (error) {
    if (!(error instanceof Error && 'code' in error && error.code === 'ENOENT')) throw error
    return true
  }
}

async function terminateOwned(roots: number[], output: string, temporary: string): Promise<void> {
  const discoveries = await Promise.allSettled([nativeResources(roots)])
  const discovery = discoveries[0]
  assert(discovery)
  const discovered = discovery.status === 'fulfilled' ? discovery.value.pids : []
  const pids = [...new Set([...roots, ...discovered])].reverse()
  const signals: { pid: number; code: string }[] = []
  for (const pid of pids.flatMap(value => [-value, value])) {
    try { process.kill(pid, 'SIGKILL'); signals.push({ pid, code: 'sent' }) }
    catch (error) {
      const code = error instanceof Error && 'code' in error && typeof error.code === 'string' ? error.code : 'UnknownError'
      signals.push({ pid, code })
    }
  }
  const releases = await Promise.allSettled([verifyReleased(pids)])
  const release = releases[0]
  assert(release)
  const listings = await Promise.allSettled([readdir(temporary)])
  const listing = listings[0]
  assert(listing)
  const temporaryBefore = listing.status === 'fulfilled' ? listing.value : null
  const removals = release.status === 'fulfilled' && release.value.processes === 0
    ? await Promise.allSettled([rm(temporary, { recursive: true, force: true })])
    : []
  const observations = await Promise.allSettled([temporaryIsRemoved(temporary)])
  const observation = observations[0]
  assert(observation)
  const temporaryRemoved = observation.status === 'fulfilled' ? observation.value : null
  const failures = [...discoveries, ...releases, ...listings, ...removals, ...observations].flatMap(result => {
    if (result.status === 'fulfilled') return []
    const error: unknown = result.reason
    return [error instanceof Error ? error.message.slice(0, 1024) : 'UnknownError']
  })
  const evidence = { roots, discovered, signals, temporaryBefore, temporaryRemoved, remaining: release.status === 'fulfilled' ? release.value : null, failures }
  await writeFile(output, JSON.stringify(evidence))
  assert.deepEqual(failures, [])
  assert(temporaryRemoved, 'Owned temporary directory remains')
  assert(signals.every(signal => ['sent', 'ESRCH', 'EPERM'].includes(signal.code)), 'Unexpected process signalling failure')
}

export async function failureChecks(root: string, output: string): Promise<unknown[]> {
  const results: unknown[] = []
  for (const scenario of ['startup', 'navigation', 'assertion', 'deadline', 'cancellation', 'acquisition']) {
    const destination = join(output, `failure-${scenario}.json`)
    const { child, temporary } = await forkFixture(fileURLToPath(new URL('./failure.ts', import.meta.url)), [scenario, root, destination], {
      stdio: ['ignore', 'pipe', 'pipe', 'ipc'], detached: true,
    })
    assert(child.pid)
    const owned = new Set<number>([child.pid])
    child.on('message', (message: unknown) => {
      assert(message !== null && typeof message === 'object' && 'pids' in message && Array.isArray(message.pids))
      for (const value of message.pids as unknown[]) {
        assert(typeof value === 'number' && Number.isInteger(value) && value > 0)
        owned.add(value)
      }
    })
    let captured = 0
    let stderr = ''
    const consume = (chunk: Buffer): void => {
      captured += chunk.byteLength
      if (captured <= 65536) stderr += chunk.toString('utf8')
    }
    child.stdout?.on('data', consume)
    child.stderr?.on('data', consume)
    const exited = once(child, 'close')
    const controller = new AbortController()
    const aborted = once(controller.signal, 'abort').then(() => { throw new Error('Failure fixture deadline exceeded') })
    const timer = setTimeout(() => { controller.abort() }, 30000)
    try {
      if (scenario === 'cancellation') {
        await Promise.race([once(child, 'message'), exited.then(() => { throw new Error('Cancellation child exited before readiness') }), aborted])
        child.kill('SIGTERM')
      }
      await Promise.race([exited, aborted])
      const payload: unknown = JSON.parse(await readFile(destination, 'utf8'))
      const result = { scenario, code: child.exitCode, stderr, captured, payload }
      results.push(result)
      await writeFile(join(output, `failure-${scenario}-parent.json`), JSON.stringify(result))
      assert.equal(child.exitCode, 0)
      assert.equal(stderr, '')
      assert.equal(captured, 0)
      await verifyReleased([...owned])
    } finally {
      clearTimeout(timer)
      await terminateOwned([...owned], join(output, `failure-${scenario}-termination.json`), temporary)
      await exited
    }
  }
  return results
}

export async function runnerCancellation(output: string, forced = false): Promise<void> {
  const directory = join(output, forced ? 'stalled-runner' : 'cancelled-runner')
  const { child, temporary } = await forkFixture(fileURLToPath(new URL('./run.ts', import.meta.url)), ['--smoke'], {
    stdio: ['ignore', 'ignore', 'pipe', 'ipc'], detached: true,
    env: { NODE_ENV: 'production', KORA_BROWSER_CANCEL_PROBE: '1', KORA_BROWSER_OUTPUT: directory },
  })
  const pid = child.pid
  assert(pid)
  const owned = [pid]
  const stderr: string[] = []
  let bytes = 0
  child.stderr?.on('data', (chunk: Buffer) => {
    bytes += chunk.byteLength
    if (bytes <= 65536) stderr.push(chunk.toString('utf8'))
  })
  const exited = once(child, 'close')
  const controller = new AbortController()
  const aborted = once(controller.signal, 'abort').then(() => 'deadline')
  let timer = setTimeout(() => { controller.abort() }, 30000)
  let deadline: boolean
  try {
    const values: unknown = await Promise.race([once(child, 'message'), exited.then(() => { throw new Error('Runner exited before cancellation readiness') }), aborted])
    assert(Array.isArray(values), 'Runner startup deadline exceeded')
    const message: unknown = values[0]
    assert(message !== null && typeof message === 'object' && 'pids' in message && Array.isArray(message.pids))
    for (const value of message.pids as unknown[]) {
      assert(typeof value === 'number' && Number.isInteger(value) && value > 0)
      owned.push(value)
    }
    const resources = await nativeResources(owned)
    owned.push(...resources.pids)
    await writeFile(join(output, forced ? 'runner-forced-ownership.json' : 'runner-ownership.json'), JSON.stringify({ owned }))
    clearTimeout(timer)
    timer = setTimeout(() => { controller.abort() }, forced ? 1000 : 30000)
    child.kill(forced ? 'SIGSTOP' : 'SIGTERM')
    deadline = await Promise.race([exited.then(() => false), aborted.then(() => true)])
  } finally {
    clearTimeout(timer)
    await terminateOwned(owned, join(output, forced ? 'runner-forced-termination.json' : 'runner-termination.json'), temporary)
    await exited
  }
  if (forced) {
    const remaining = await verifyReleased(owned)
    await writeFile(join(output, 'runner-forced-cancellation.json'), JSON.stringify({ code: child.exitCode, signal: child.signalCode, stderr, bytes, deadline, owned, remaining }))
    assert(deadline)
    assert.equal(child.signalCode, 'SIGKILL')
    assert.equal(bytes, 0)
    return
  }
  const directories = await readdir(directory)
  assert.equal(directories.length, 1)
  const run = directories[0]
  assert(run)
  const report: unknown = JSON.parse(await readFile(join(directory, run, 'report.json'), 'utf8'))
  await writeFile(join(output, 'runner-cancellation.json'), JSON.stringify({ code: child.exitCode, stderr, bytes, deadline, report }))
  assert(!deadline)
  assert.equal(child.exitCode, 1)
  assert.equal(bytes, 0)
  assert(report !== null && typeof report === 'object' && 'status' in report && report.status === 'failed')
}
