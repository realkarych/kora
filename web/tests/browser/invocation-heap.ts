import assert from 'node:assert/strict'

export interface HeapGrowth {
  full: number
  late: number
  slope: number
}

export interface HeapLimits extends HeapGrowth {
  terminal: number
}

export const heapCeiling = 60 * 1024
export const slopeCeiling = 30 * 1024

function median(values: number[]): number {
  assert(values.length > 0 && values.length % 2 === 1, 'invalid-heap-sample-count')
  assert(values.every(Number.isFinite), 'invalid-heap-number')
  const result = [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)]
  assert(result !== undefined, 'invalid-heap-sample-count')
  return result
}

export function fitHeap(values: number[], ceiling?: number): number {
  assert(values.length === 5, 'invalid-heap-cluster-count')
  const center = median(values)
  const deviation = median(values.map(value => Math.abs(value - center)))
  const limit = Math.ceil(Math.max(0, Math.max(...values) + 3 * deviation) / 4096) * 4096
  assert(Number.isSafeInteger(limit) && (ceiling === undefined || limit <= ceiling), 'heap-budget-exceeds-ceiling')
  return limit
}

export function heapGrowth(values: number[]): HeapGrowth {
  assert(values.length === 6 && values.every(value => Number.isSafeInteger(value) && value >= 0), 'invalid-invocation-heap')
  let full = 0
  let late = 0
  const slopes: number[] = []
  for (const [index, before] of values.entries()) {
    for (let next = index + 1; next < values.length; next++) {
      const after = values[next]
      assert(after !== undefined, 'invalid-invocation-heap')
      full = Math.max(full, after - before)
      if (index >= 3) {
        late = Math.max(late, after - before)
        slopes.push((after - before) / (next - index))
      }
    }
  }
  return { full, late, slope: median(slopes) }
}

export function heapGates(growth: HeapGrowth, limits: HeapLimits): Record<string, boolean> {
  return { 'invocation-full': growth.full <= limits.full, 'invocation-late': growth.late <= limits.late, 'invocation-slope': Math.max(0, growth.slope) <= limits.slope }
}

export function fitHeapLimits(terminals: number[][], growth: HeapGrowth[]): HeapLimits {
  assert(terminals.length === 5 && growth.length === 5, 'invalid-heap-cluster-count')
  const maxima = terminals.map(values => {
    assert(values.length === 6 && values.every(Number.isFinite), 'invalid-terminal-heaps')
    return Math.max(...values)
  })
  return {
    terminal: fitHeap(maxima, heapCeiling), full: fitHeap(growth.map(value => value.full)),
    late: fitHeap(growth.map(value => value.late), heapCeiling), slope: fitHeap(growth.map(value => value.slope), slopeCeiling),
  }
}

export function parseHeapLimits(value: unknown): HeapLimits {
  assert(value !== null && typeof value === 'object' && !Array.isArray(value), 'invalid-heap-limits')
  assert(Object.keys(value).sort().join(',') === 'full,late,slope,terminal', 'invalid-heap-limits')
  const field = (name: string, ceiling?: number): number => {
    const number: unknown = Object.getOwnPropertyDescriptor(value, name)?.value
    assert(typeof number === 'number' && Number.isSafeInteger(number) && number >= 0 && number % 4096 === 0, 'invalid-heap-limit')
    assert(ceiling === undefined || number <= ceiling, 'heap-budget-exceeds-ceiling')
    return number
  }
  return { terminal: field('terminal', heapCeiling), full: field('full'), late: field('late', heapCeiling), slope: field('slope', slopeCeiling) }
}
