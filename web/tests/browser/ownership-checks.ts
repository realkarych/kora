import assert from 'node:assert/strict'
import { setImmediate } from 'node:timers/promises'
import { checkCleanup, Ownership, scheduleClose } from './ownership.ts'
import type { Cleanup } from './ownership.ts'
import { nativeResources } from './resources.ts'

export async function ownershipChecks(root: string): Promise<{ scenario: string, cleanup: Cleanup }[]> {
  const results: { scenario: string, cleanup: Cleanup }[] = []
  for (const scenario of ['readable', 'late-snapshot']) {
    const targets: number[] = []
    let observedAt = 0
    const owner = new Ownership(undefined, {
      observe: async pids => {
        observedAt = performance.now()
        const resources = await nativeResources(pids)
        if (scenario === 'late-snapshot') {
          await new Promise<void>(resolve => { scheduleClose(observedAt + 5000, resolve) })
          while (performance.now() < observedAt + 5000) await setImmediate()
        }
        return resources
      },
      schedule: (deadline, callback) => {
        targets.push(deadline)
        return scheduleClose(deadline, callback)
      },
    })
    try {
      await owner.startServer('production', root)
      await owner.startBrowser()
      const started = performance.now()

      const cleanup = await owner.close()

      assert.equal(targets.length, 2)
      assert(targets[0] !== undefined && targets[0] >= started + 5000 && targets[0] <= observedAt + 5000)
      assert.equal(targets[1], targets[0])
      assert.equal(cleanup.proxySockets, 0)
      assert.equal(cleanup.remaining.processes, 0)
      if (scenario === 'readable') checkCleanup(cleanup)
      else {
        assert(cleanup.forced)
        assert(cleanup.failures.some(failure => failure.stage === 'sample' && failure.message === 'Ownership admission deadline expired'))
        assert.throws(() => { checkCleanup(cleanup) })
      }
      results.push({ scenario, cleanup })
    } finally { await owner.close() }
  }
  return results
}
