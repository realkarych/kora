import assert from 'node:assert/strict'
import { collectRunner } from './invocation-gc.ts'
import { heapGates, heapGrowth } from './invocation-heap.ts'
import type { HeapLimits } from './invocation-heap.ts'
import { closedRecord, parseMemory, parseRetention } from './invocation.ts'
import { Retention } from './retention.ts'
import { writeEvidence } from './trial-evidence.ts'

interface RecoveryPoint {
  memory: NodeJS.MemoryUsage
  retention: ReturnType<Retention['evidence']>
}

export function verifyRecovery(value: unknown, limits: HeapLimits): void {
  assert(Array.isArray(value) && value.length === 8, 'invalid-recovery-observations')
  const points = (value as unknown[]).map(value => {
    const point = closedRecord(value, ['memory', 'retention'])
    return { memory: parseMemory(point.memory), retention: parseRetention(point.retention) }
  })
  const baseline = points[0]
  const first = points[1]
  const last = points[6]
  const released = points[7]
  assert(baseline && first && last && released, 'incomplete-recovery-observations')
  assert(baseline.retention.arrays === 0 && baseline.retention.nominalBytes === 0 && baseline.retention.allocations === 0 && !baseline.retention.released, 'invalid-recovery-baseline')
  for (const point of points.slice(1, 7)) assert(point.retention.arrays === 1 && point.retention.nominalBytes === 245760 && point.retention.allocations === 1 && !point.retention.released, 'invalid-recovery-held')
  assert(released.retention.arrays === 0 && released.retention.nominalBytes === 0 && released.retention.allocations === 1 && released.retention.released, 'invalid-recovery-release')
  assert(Object.values(heapGates(heapGrowth(points.slice(1, 7).map(point => point.memory.heapUsed)), limits)).every(Boolean), 'bounded-plateau-failed')
  assert(first.memory.heapUsed - baseline.memory.heapUsed >= 245760 - limits.terminal, 'bounded-allocation-not-observed')
  assert(last.memory.heapUsed - released.memory.heapUsed >= 245760 - limits.terminal, 'bounded-release-not-observed')
  assert(released.memory.heapUsed - baseline.memory.heapUsed <= limits.terminal, 'bounded-heap-recovery-failed')
  assert(released.memory.external <= baseline.memory.external && released.memory.arrayBuffers <= baseline.memory.arrayBuffers, 'bounded-buffer-recovery-failed')
}

export async function recoveryFixture(root: string, primary: Retention, limits: HeapLimits): Promise<void> {
  const prior = primary.evidence()
  assert(prior.released && prior.arrays === 0 && prior.nominalBytes === 0, 'primary-retention-not-released')
  const owner = new Retention('intertrial')
  const points: RecoveryPoint[] = Array.from({ length: 8 }, () => ({ memory: { rss: 0, heapTotal: 0, heapUsed: 0, external: 0, arrayBuffers: 0 }, retention: { allocations: 0, arrays: 0, nominalBytes: 0, released: false } }))
  let captured = 0
  const capture = async (): Promise<void> => {
    await collectRunner()
    const point = points[captured]
    assert(point, 'recovery-observation-limit')
    Object.assign(point.memory, process.memoryUsage())
    Object.assign(point.retention, owner.evidence())
    captured++
  }
  try {
    await capture()
    owner.allocate('finalization')
    for (let index = 0; index < 6; index++) await capture()
    owner.release()
    await capture()
  } finally {
    owner.release()
    writeEvidence(root, 'retention-recovery.json', points.slice(0, captured), 65536)
  }
  verifyRecovery(points, limits)
}
