import assert from 'node:assert/strict'
import { closeSync, openSync, writeSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { getHeapCodeStatistics } from 'node:v8'
import type { CDPSession } from 'playwright'
import { collectRunner } from './invocation-gc.ts'
import type { Retention } from './retention.ts'
export { collectRunner } from './invocation-gc.ts'
import { checkCleanup, Ownership } from './ownership.ts'
import type { Cleanup } from './ownership.ts'
import { contextFor, Diagnostics, heap, ready, semantics } from './page.ts'
import { nativeResources } from './resources.ts'
import type { NativeResources } from './resources.ts'
import type { Trial } from './trial.ts'
import { measurementProtocol } from './protocol.ts'
import { checkpointMemory, persistCheckpoint } from './diagnostic-checkpoint.ts'

export async function snapshot(session: CDPSession, path: string): Promise<void> {
  const descriptor = openSync(path, 'wx')
  let bytes = 0
  let overflow = false
  const consume = (event: { chunk: string }): void => {
    const buffer = Buffer.from(event.chunk)
    bytes += buffer.byteLength
    if (bytes <= 32 * 1024 * 1024) writeSync(descriptor, buffer)
    else overflow = true
  }
  session.on('HeapProfiler.addHeapSnapshotChunk', consume)
  try { await session.send('HeapProfiler.takeHeapSnapshot', { reportProgress: false }) }
  finally { session.off('HeapProfiler.addHeapSnapshotChunk', consume); closeSync(descriptor) }
  assert(!overflow, 'Heap snapshot exceeded 32 MiB bound')
}

export async function measure(root: string, arm: 'base' | 'head', repeat: number, artifacts: string, save: (trial: Trial) => void, retention: Retention, diagnostic?: AbortSignal): Promise<void> {
  await collectRunner()
  const owner = new Ownership(artifacts)
  const diagnostics = new Diagnostics()
  const trial: Trial = { protocol: measurementProtocol, arm, repeat, runnerBefore: diagnostic ? checkpointMemory(artifacts, arm, repeat, 'before', process.env.KORA_BROWSER_OUTPUT ?? artifacts) : process.memoryUsage(), phases: [], warmLoads: [], warmCycles: [], loads: [], cycles: [], browserVersion: '', diagnostics, before: await nativeResources([process.pid]) }
  const phase = async (name: string): Promise<void> => {
    trial.phases.push({ phase: name, native: await nativeResources([process.pid]), runner: process.memoryUsage() })
  }
  try {
    await phase('before-server')
    const origin = await owner.startServer('production', root)
    await phase('server-ready')
    const browser = await owner.startBrowser()
    if (diagnostic) process.send?.({ event: 'owned', pids: [...owner.pids] })
    await phase('browser-ready')
    trial.browserVersion = browser.version()
    for (let iteration = -2; iteration < 20; iteration++) {
      const context = await contextFor(browser, origin, diagnostics)
      const page = await context.newPage()
      try {
        await page.goto(origin)
        const load = await ready(page)
        await phase(`cold-load-${String(iteration)}`)
        if (iteration >= 0) trial.loads.push(load)
        else trial.warmLoads.push(load)
        diagnostics.check()
      } finally { await context.close() }
      assert.equal(browser.contexts().length, 0)
    }
    const context = await contextFor(browser, origin, diagnostics)
    try {
      const page = await context.newPage()
      const session = await context.newCDPSession(page)
      await session.send('Performance.enable')
      await page.goto(origin)
      await ready(page)
      for (let cycle = -3; cycle < 30; cycle++) {
        await page.reload()
        await ready(page)
        const temporary = await contextFor(browser, origin, diagnostics)
        let active: NativeResources
        try {
          const temporaryPage = await temporary.newPage()
          await temporaryPage.goto(origin)
          await ready(temporaryPage)
          active = await nativeResources([process.pid])
        } finally { await temporary.close() }
        if (cycle >= 0) retention.allocate('cycle')
        const beforeGCNative = await nativeResources([process.pid])
        const beforeGC = await heap(session)
        const runnerBeforeGC = process.memoryUsage()
        await session.send('HeapProfiler.collectGarbage')
        const afterGC = await heap(session)
        await collectRunner()
        const runner = process.memoryUsage()
        const runnerCode = getHeapCodeStatistics()
        const point = { cycle: cycle + 1, active, beforeGCNative, beforeGC, afterGC, native: await nativeResources([process.pid]), runnerBeforeGC, owned: await owner.sample(), runner, runnerCode, contexts: browser.contexts().length, pages: context.pages().length }
        if (cycle >= 0) trial.cycles.push(point)
        else trial.warmCycles.push(point)
        if (cycle === -1 || cycle === 29) await snapshot(session, join(artifacts, `${arm}-${String(repeat)}-${cycle === -1 ? 'baseline' : 'end'}.heapsnapshot`))
        diagnostics.check()
        assert.equal(browser.contexts().length, 1)
        assert.equal(context.pages().length, 1)
      }
      await session.detach()
    } finally { await context.close() }
    assert.equal(browser.contexts().length, 0)
  } catch (error) {
    trial.failure = error instanceof Error ? error.message.slice(0, 2048) : 'Non-error failure'
    throw error
  } finally {
    try {
      trial.serverOutput = owner.logs
      trial.cleanup = await owner.close()
      trial.runnerReleasedBeforeGC = process.memoryUsage()
      if (diagnostic) persistCheckpoint(artifacts, arm, repeat, 'natural', trial.runnerReleasedBeforeGC, getHeapCodeStatistics(), process.env.KORA_BROWSER_OUTPUT ?? artifacts)
      trial.retainedEvidenceBytes = Buffer.byteLength(JSON.stringify(trial))
    } finally { save(trial) }
  }
  assert(trial.cleanup)
  checkCleanup(trial.cleanup)
}

export async function smoke(root: string, mode: 'production' | 'development', artifacts: string, onReady?: (pids: number[]) => Promise<void>): Promise<{ diagnostics: Diagnostics; cleanup: Cleanup }> {
  const owner = new Ownership(artifacts)
  const diagnostics = new Diagnostics()
  let cleanup: Cleanup
  const evidence: Record<string, unknown> = { diagnostics }
  try {
    const origin = await owner.startServer(mode, root)
    const browser = await owner.startBrowser()
    await onReady?.([...owner.pids])
    for (const width of [1280, 768, 375]) {
      const context = await contextFor(browser, origin, diagnostics, width)
      await context.tracing.start({ screenshots: false, snapshots: false, sources: false })
      const page = await context.newPage()
      let primaryFailure = false
      try {
        await page.goto(origin)
        await ready(page)
        await semantics(page)
        await page.reload()
        await ready(page)
        await semantics(page)
        await page.screenshot({ path: join(artifacts, `${mode}-${String(width)}.png`) })
        diagnostics.check()
      } catch (error) {
        primaryFailure = true
        throw error
      } finally {
        const captured = await Promise.allSettled([
          page.screenshot({ path: join(artifacts, `${mode}-${String(width)}-terminal.png`), timeout: 5000 }),
          context.tracing.stop({ path: join(artifacts, `${mode}-${String(width)}.zip`) }),
        ])
        const closed = await Promise.allSettled([context.close()])
        const failures = [...captured, ...closed].flatMap(result => {
          if (result.status === 'fulfilled') return []
          const error: unknown = result.reason
          return [error instanceof Error ? error.message.slice(0, 1024) : 'UnknownError']
        })
        evidence[`capture-${String(width)}`] = failures
        if (!primaryFailure) assert.deepEqual(failures, [])
      }
    }
  } catch (error) {
    evidence.failure = error instanceof Error ? error.message.slice(0, 2048) : 'UnknownError'
    throw error
  } finally {
    cleanup = await owner.close()
    evidence.cleanup = cleanup
    writeFileSync(join(artifacts, `${mode}-smoke.json`), JSON.stringify(evidence, null, 2))
  }
  checkCleanup(cleanup)
  return { diagnostics, cleanup }
}
