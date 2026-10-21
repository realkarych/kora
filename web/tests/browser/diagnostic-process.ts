import assert from 'node:assert/strict'
import { fork } from 'node:child_process'
import { once } from 'node:events'
import { mkdtemp, readFile, readdir, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { nativeResources } from './resources.ts'
import { terminateOwned } from './negative.ts'
import { MarkerScanner } from './diagnostic-inspection.ts'

export async function diagnosticChild(environment: NodeJS.ProcessEnv, output: string, signal: AbortSignal, marker: string, fixture?: 'cancel' | 'stall'): Promise<{ code: number | null; signal: NodeJS.Signals | null; captured: number; interrupted: boolean; markerAbsent: boolean }> {
  const temporary = await mkdtemp(join(tmpdir(), 'kora-diagnostic-child-'))
  const child = fork(join(import.meta.dirname, 'run.ts'), [fixture ? '--smoke' : '--diagnostic'], {
    execArgv: ['--expose-gc'], env: { ...environment, TMPDIR: temporary, ...(fixture ? { KORA_BROWSER_CANCEL_PROBE: '1' } : {}) },
    stdio: ['ignore', 'pipe', 'pipe', 'ipc'], detached: true,
  })
  const closed = once(child, 'close')
  if (child.pid === undefined) {
    try { await closed } finally { await rm(temporary, { recursive: true, force: true }) }
    throw new Error('Diagnostic child did not spawn')
  }
  const owned = new Set([child.pid])
  let ownershipConfirmed = false
  let captured = 0
  let interrupted = false
  const cancellation = new AbortController()
  const stop = (): void => { interrupted = true; cancellation.abort() }
  signal.addEventListener('abort', stop, { once: true })
  const stdout = new MarkerScanner(marker)
  const stderr = new MarkerScanner(marker)
  const consume = (scanner: MarkerScanner, chunk: Buffer): void => {
    captured += chunk.byteLength
    scanner.consume(chunk)
    if (captured > 65536 || scanner.found) stop()
  }
  child.stdout?.on('data', (chunk: Buffer) => { consume(stdout, chunk) })
  child.stderr?.on('data', (chunk: Buffer) => { consume(stderr, chunk) })
  child.on('message', (message: unknown) => {
    assert(message !== null && typeof message === 'object' && 'pids' in message && Array.isArray(message.pids))
    for (const pid of message.pids as unknown[]) {
      assert(typeof pid === 'number' && Number.isInteger(pid) && pid > 0)
      owned.add(pid)
    }
    ownershipConfirmed = fixture !== undefined
    if (fixture) stop()
  })
  let timer = setTimeout(stop, 300000)
  try {
    if (signal.aborted) stop()
    const aborted = cancellation.signal.aborted ? Promise.resolve() : once(cancellation.signal, 'abort').then(() => undefined)
    const cancelled = await Promise.race([closed.then(() => false), aborted.then(() => true)])
    if (cancelled) {
      const inventory = await nativeResources([...owned])
      for (const pid of inventory.pids) owned.add(pid)
      child.kill(fixture === 'stall' ? 'SIGSTOP' : 'SIGTERM')
      clearTimeout(timer)
      const deadline = new AbortController()
      timer = setTimeout(() => { deadline.abort() }, fixture === 'stall' ? 1000 : 30000)
      await Promise.race([closed, once(deadline.signal, 'abort')])
    }
    if (!fixture && child.exitCode === 0) {
      const runs = (await readdir(output)).filter(name => name.startsWith('run-'))
      assert.equal(runs.length, 1)
      const report: unknown = JSON.parse(await readFile(join(output, runs[0] ?? '', 'report.json'), 'utf8'))
      assert(report !== null && typeof report === 'object' && 'status' in report && report.status === 'diagnostic complete; calibration and performance unverified')
      ownershipConfirmed = true
    }
  } finally {
    clearTimeout(timer)
    signal.removeEventListener('abort', stop)
    await terminateOwned([...owned], join(output, fixture ? `launcher-${fixture}-termination.json` : 'launcher-termination.json'), temporary, ownershipConfirmed)
    await closed
  }
  return { code: child.exitCode, signal: child.signalCode, captured, interrupted, markerAbsent: !stdout.found && !stderr.found }
}
