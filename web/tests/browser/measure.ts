import assert from 'node:assert/strict'
import { closeSync, openSync, writeSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import type { CDPSession } from 'playwright'
import { checkCleanup, Ownership } from './ownership.ts'
import type { Cleanup } from './ownership.ts'
import { contextFor, Diagnostics, heap, ready, semantics } from './page.ts'
import type { Heap, Load } from './page.ts'
import { nativeResources, percentile, span } from './resources.ts'
import type { NativeResources } from './resources.ts'

export interface Cycle {
  cycle: number
  beforeGC: Heap
  afterGC: Heap
  native: NativeResources
  runner: NodeJS.MemoryUsage
  contexts: number
  pages: number
}

export interface Trial {
  arm: 'base' | 'head'
  repeat: number
  warmLoads: Load[]
  warmCycles: Cycle[]
  after?: NativeResources
  loads: Load[]
  cycles: Cycle[]
  browserVersion: string
  diagnostics: Diagnostics
  before: NativeResources
  cleanup?: Cleanup
  failure?: string
}

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

export async function measure(root: string, arm: 'base' | 'head', repeat: number, artifacts: string, save: (trial: Trial) => void): Promise<Trial> {
  const owner = new Ownership(artifacts)
  const diagnostics = new Diagnostics()
  const trial: Trial = { arm, repeat, warmLoads: [], warmCycles: [], loads: [], cycles: [], browserVersion: '', diagnostics, before: await nativeResources([process.pid]) }
  try {
    const origin = await owner.startServer('production', root)
    const browser = await owner.startBrowser()
    trial.browserVersion = browser.version()
    for (let iteration = -2; iteration < 20; iteration++) {
      const context = await contextFor(browser, origin, diagnostics)
      const page = await context.newPage()
      try {
        await page.goto(origin)
        const load = await ready(page)
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
        try {
          const temporaryPage = await temporary.newPage()
          await temporaryPage.goto(origin)
          await ready(temporaryPage)
        } finally { await temporary.close() }
        const beforeGC = await heap(session)
        await session.send('HeapProfiler.collectGarbage')
        const afterGC = await heap(session)
        const point = { cycle: cycle + 1, beforeGC, afterGC, native: await owner.sample(), runner: process.memoryUsage(), contexts: browser.contexts().length, pages: context.pages().length }
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
      trial.cleanup = await owner.close()
      trial.after = await nativeResources([process.pid])
    } finally { save(trial) }
  }
  assert(trial.cleanup)
  checkCleanup(trial.cleanup)
  return trial
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
      try {
        await page.goto(origin)
        await ready(page)
        await semantics(page)
        await page.reload()
        await ready(page)
        await semantics(page)
        await page.screenshot({ path: join(artifacts, `${mode}-${String(width)}.png`) })
        diagnostics.check()
      } finally {
        try {
          await page.screenshot({ path: join(artifacts, `${mode}-${String(width)}-terminal.png`), timeout: 5000 })
          await context.tracing.stop({ path: join(artifacts, `${mode}-${String(width)}.zip`) })
        } finally { await context.close() }
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

export function summarize(trial: Trial): Record<string, number> {
  const result: Record<string, number> = {}
  for (const metric of ['load', 'fcp', 'lcp', 'cls', 'longTasks', 'transfer'] as const) {
    const values = trial.loads.map(load => load[metric])
    result[`${metric}Median`] = percentile(values, 0.5)
    result[`${metric}P95`] = percentile(values, 0.95)
  }
  for (const metric of ['used', 'documents', 'nodes', 'listeners'] as const) {
    const values = trial.cycles.map(cycle => cycle.afterGC[metric])
    result[`${metric}Span`] = span(values)
    result[`${metric}LateSpan`] = span(values.slice(15))
    result[`${metric}MedianGrowth`] = percentile(values.slice(-10), 0.5) - percentile(values.slice(0, 10), 0.5)
    result[`${metric}Peak`] = Math.max(...trial.cycles.map(cycle => cycle.beforeGC[metric]))
  }
  for (const metric of ['rss', 'descriptors', 'processes'] as const) {
    const values = trial.cycles.map(cycle => cycle.native[metric])
    if (values.some(value => value === null)) continue
    const numbers = values.filter(value => value !== null)
    result[`${metric}Span`] = span(numbers)
    result[`${metric}LateSpan`] = span(numbers.slice(15))
    result[`${metric}MedianGrowth`] = percentile(numbers.slice(-10), 0.5) - percentile(numbers.slice(0, 10), 0.5)
    result[`${metric}Peak`] = Math.max(...numbers)
  }
  assert(trial.cleanup)
  result.cleanupSeconds = trial.cleanup.seconds
  return result
}
