import assert from 'node:assert/strict'
import { fork } from 'node:child_process'
import { once } from 'node:events'
import { readFile, readdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { Ownership } from './ownership.ts'
import { nativeResources, verifyReleased } from './resources.ts'

function terminateGroup(pid: number): void {
  try { process.kill(-pid, 'SIGTERM') }
  catch (error) {
    if (!(error instanceof Error && 'code' in error && error.code === 'ESRCH')) throw error
  }
}

export async function failureChecks(root: string, output: string): Promise<unknown[]> {
  const results: unknown[] = []
  for (const scenario of ['startup', 'navigation', 'assertion', 'deadline', 'cancellation']) {
    const destination = join(output, `failure-${scenario}.json`)
    const child = fork(fileURLToPath(new URL('./failure.ts', import.meta.url)), [scenario, root, destination], {
      stdio: ['ignore', 'pipe', 'pipe', 'ipc'], env: new Ownership().environment(), detached: true,
    })
    assert(child.pid)
    const pid = child.pid
    const owned = new Set<number>([pid])
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
    let timedOut = false
    const timer = setTimeout(() => {
      timedOut = true
      controller.abort()
      void nativeResources([pid]).then(resources => {
        for (const value of resources.pids) owned.add(value)
        process.kill(-pid, 'SIGKILL')
      })
    }, 30000)
    try {
      if (scenario === 'cancellation') {
        await Promise.race([once(child, 'message', { signal: controller.signal }), exited.then(() => { throw new Error('Cancellation child exited before readiness') })])
        child.kill('SIGTERM')
      }
      await exited
      const payload: unknown = JSON.parse(await readFile(destination, 'utf8'))
      const result = { scenario, code: child.exitCode, stderr, captured, timedOut, payload }
      results.push(result)
      await writeFile(join(output, `failure-${scenario}-parent.json`), JSON.stringify(result))
      assert.equal(child.exitCode, 0)
      assert.equal(stderr, '')
      assert.equal(captured, 0)
      await verifyReleased([...owned])
    } finally {
      clearTimeout(timer)
      controller.abort()
      terminateGroup(pid)
      await verifyReleased([...owned])
    }
  }
  return results
}

export async function runnerCancellation(output: string): Promise<void> {
  const directory = join(output, 'cancelled-runner')
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
  const timer = setTimeout(() => { controller.abort(); terminateGroup(pid) }, 30000)
  try {
    const values: unknown = await Promise.race([once(child, 'message', { signal: controller.signal }), exited.then(() => { throw new Error('Runner exited before cancellation readiness') })])
    assert(Array.isArray(values))
    const message: unknown = values[0]
    assert(message !== null && typeof message === 'object' && 'pids' in message && Array.isArray(message.pids))
    for (const value of message.pids as unknown[]) {
      assert(typeof value === 'number' && Number.isInteger(value) && value > 0)
      owned.push(value)
    }
    const resources = await nativeResources(owned)
    owned.push(...resources.pids)
    child.kill('SIGTERM')
    await exited
    const directories = await readdir(directory)
    assert.equal(directories.length, 1)
    const run = directories[0]
    assert(run)
    const report: unknown = JSON.parse(await readFile(join(directory, run, 'report.json'), 'utf8'))
    await writeFile(join(output, 'runner-cancellation.json'), JSON.stringify({ code: child.exitCode, stderr, bytes, report }))
    assert.equal(child.exitCode, 1)
    assert.equal(bytes, 0)
    assert(report !== null && typeof report === 'object' && 'status' in report && report.status === 'failed')
    await verifyReleased(owned)
  } finally {
    clearTimeout(timer)
    controller.abort()
    terminateGroup(pid)
    await verifyReleased(owned)
  }
}
