import assert from 'node:assert/strict'
import { fork } from 'node:child_process'
import { once } from 'node:events'
import { readFile, readdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { Ownership } from './ownership.ts'
import { nativeResources, verifyReleased } from './resources.ts'

async function terminateOwned(roots: number[], output: string): Promise<void> {
  const discovered = await nativeResources(roots)
  const pids = [...new Set([...roots, ...discovered.pids])].reverse()
  const signals: { pid: number; code: string }[] = []
  for (const pid of pids.flatMap(value => [-value, value])) {
    try { process.kill(pid, 'SIGKILL'); signals.push({ pid, code: 'sent' }) }
    catch (error) {
      const code = error instanceof Error && 'code' in error && typeof error.code === 'string' ? error.code : 'UnknownError'
      signals.push({ pid, code })
    }
  }
  const evidence = { roots, discovered, signals, remaining: await nativeResources(pids) }
  await writeFile(output, JSON.stringify(evidence))
  evidence.remaining = await verifyReleased(pids)
  await writeFile(output, JSON.stringify(evidence))
  assert(signals.every(signal => ['sent', 'ESRCH', 'EPERM'].includes(signal.code)), 'Unexpected process signalling failure')
}

export async function failureChecks(root: string, output: string): Promise<unknown[]> {
  const results: unknown[] = []
  for (const scenario of ['startup', 'navigation', 'assertion', 'deadline', 'cancellation', 'acquisition']) {
    const destination = join(output, `failure-${scenario}.json`)
    const child = fork(fileURLToPath(new URL('./failure.ts', import.meta.url)), [scenario, root, destination], {
      stdio: ['ignore', 'pipe', 'pipe', 'ipc'], env: new Ownership().environment(), detached: true,
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
      await terminateOwned([...owned], join(output, `failure-${scenario}-termination.json`))
      await exited
    }
  }
  return results
}

export async function runnerCancellation(output: string, forced = false): Promise<void> {
  const directory = join(output, forced ? 'stalled-runner' : 'cancelled-runner')
  const child = fork(fileURLToPath(new URL('./run.ts', import.meta.url)), ['--smoke'], {
    stdio: ['ignore', 'ignore', 'pipe', 'ipc'], detached: true,
    env: { ...new Ownership().environment(), KORA_BROWSER_CANCEL_PROBE: '1', KORA_BROWSER_OUTPUT: directory },
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
    await terminateOwned(owned, join(output, forced ? 'runner-forced-termination.json' : 'runner-termination.json'))
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
