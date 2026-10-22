import assert from 'node:assert/strict'
import { writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { checkCleanup, Ownership } from './ownership.ts'
import type { Cleanup } from './ownership.ts'
import { contextFor, Diagnostics, ready, semantics } from './page.ts'

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
