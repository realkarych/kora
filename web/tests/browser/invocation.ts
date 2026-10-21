import assert from 'node:assert/strict'
import { join } from 'node:path'
import { measurementProtocol } from './protocol.ts'
import { readEvidence, writeEvidence } from './trial-evidence.ts'
import { heapGrowth } from './invocation-heap.ts'
import type { HeapGrowth } from './invocation-heap.ts'
import type { Retention } from './retention.ts'

export interface InvocationPoint {
  protocol: typeof measurementProtocol
  sequence: number
  arm: 'base' | 'head' | 'start'
  repeat: number
  memory: NodeJS.MemoryUsage
  retention: ReturnType<Retention['evidence']>
}

export function closedRecord(value: unknown, keys: string[]): Record<string, unknown> {
  assert(value !== null && typeof value === 'object' && !Array.isArray(value), 'invalid-invocation-record')
  assert(Object.keys(value).sort().join(',') === [...keys].sort().join(','), 'invalid-invocation-keys')
  return Object.fromEntries(Object.entries(value))
}

function integer(value: unknown): number {
  assert(typeof value === 'number' && Number.isSafeInteger(value) && value >= 0, 'invalid-invocation-counter')
  return value
}

export function parseMemory(value: unknown): NodeJS.MemoryUsage {
  const input = closedRecord(value, ['rss', 'heapTotal', 'heapUsed', 'external', 'arrayBuffers'])
  return { rss: integer(input.rss), heapTotal: integer(input.heapTotal), heapUsed: integer(input.heapUsed), external: integer(input.external), arrayBuffers: integer(input.arrayBuffers) }
}

export function parseRetention(value: unknown): ReturnType<Retention['evidence']> {
  const input = closedRecord(value, ['allocations', 'arrays', 'nominalBytes', 'released'])
  assert(typeof input.released === 'boolean', 'invalid-retention-state')
  const result = { allocations: integer(input.allocations), arrays: integer(input.arrays), nominalBytes: integer(input.nominalBytes), released: input.released }
  assert(result.allocations <= 180 && result.arrays <= result.allocations && result.nominalBytes <= 5898240, 'invalid-retention-state')
  assert(!result.released || (result.arrays === 0 && result.nominalBytes === 0), 'invalid-retention-release')
  return result
}

export function parseInvocationPoint(value: unknown): InvocationPoint {
  const input = closedRecord(value, ['protocol', 'sequence', 'arm', 'repeat', 'memory', 'retention'])
  assert(input.protocol === measurementProtocol, 'invalid-invocation-protocol')
  assert(input.arm === 'base' || input.arm === 'head' || input.arm === 'start', 'invalid-invocation-arm')
  const sequence = integer(input.sequence)
  const repeat = integer(input.repeat)
  assert(sequence <= 6 && repeat <= 2, 'invalid-invocation-identity')
  return { protocol: measurementProtocol, sequence, arm: input.arm, repeat, memory: parseMemory(input.memory), retention: parseRetention(input.retention) }
}

export function invocationGrowth(points: InvocationPoint[], order: ['base' | 'head', number][]): HeapGrowth {
  assert(points.length === 7 && order.length === 6, 'incomplete-invocation')
  for (const [sequence, point] of points.entries()) {
    const identity = sequence === 0 ? ['start', 0] : order[sequence - 1]
    assert(identity && point.sequence === sequence && point.arm === identity[0] && point.repeat === identity[1], 'invalid-invocation-order')
  }
  return heapGrowth(points.slice(1).map(point => point.memory.heapUsed))
}

export function persistInvocation(root: string, sequence: number, arm: InvocationPoint['arm'], repeat: number, memory: NodeJS.MemoryUsage, retention: ReturnType<Retention['evidence']>, budgetRoot = root): void {
  const point = parseInvocationPoint({ protocol: measurementProtocol, sequence, arm, repeat, memory, retention })
  writeEvidence(root, `invocation-${String(sequence)}.json`, point, 4096, budgetRoot)
}

export function readInvocation(root: string): InvocationPoint[] {
  return Array.from({ length: 7 }, (_, sequence) => parseInvocationPoint(readEvidence(join(root, `invocation-${String(sequence)}.json`), 4096)))
}
