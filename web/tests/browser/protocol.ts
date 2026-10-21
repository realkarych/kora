import assert from 'node:assert/strict'
import { percentile, span } from './resources.ts'
import { heapCeiling, parseHeapLimits } from './invocation-heap.ts'
import type { HeapLimits } from './invocation-heap.ts'

export const measurementProtocol = 'invocation-heap-v3'

export interface Budgets {
  protocol: typeof measurementProtocol
  reference: { platform: string; arch: string; node: string; browser: string }
  heap: HeapLimits
  absolute: Record<string, number>
  pairedMargin: Record<string, number>
}

const floors: Record<string, number> = {
  loadMedian: 0, loadP95: 0, fcpMedian: 0, fcpP95: 0, lcpMedian: 0, lcpP95: 0,
  usedSpan: 0, usedLateSpan: 0, usedMedianGrowth: 0,
  rssSpan: 0, rssLateSpan: 0, rssMedianGrowth: 0,
  cleanupSeconds: 0,
  runnerheapUsedSpan: 0, runnerheapUsedLateSpan: 0, runnerheapUsedMedianGrowth: 0,
  runnerexternalSpan: 2, runnerexternalLateSpan: 2, runnerexternalMedianGrowth: 2,
  runnerarrayBuffersSpan: 2, runnerarrayBuffersLateSpan: 2, runnerarrayBuffersMedianGrowth: 2,
  runnerheapUsedPeak: 0, runnerheapUsedReleasedGrowth: 0,
  runnerexternalPeak: 0, runnerexternalReleasedGrowth: 0,
  runnerarrayBuffersPeak: 0, runnerarrayBuffersReleasedGrowth: 0,
  usedPeak: 0, rssPeak: 0,
  documentsSpan: 0, documentsLateSpan: 0, documentsMedianGrowth: 0,
  nodesSpan: 0, nodesLateSpan: 0, nodesMedianGrowth: 0,
  listenersSpan: 0, listenersLateSpan: 0, listenersMedianGrowth: 0,
  descriptorsSpan: 0, descriptorsLateSpan: 0, descriptorsMedianGrowth: 0,
  processesSpan: 0, processesLateSpan: 0, processesMedianGrowth: 0,
  clsP95: 0, longTasksP95: 0, transferP95: 0,
}

export const measurementMetrics = Object.keys(floors)

const terminalMetrics = ['runnerexternalReleasedGrowth', 'runnerarrayBuffersReleasedGrowth']

export function requireReleased(summaries: Record<string, number>[]): void {
  for (const summary of summaries) {
    for (const metric of terminalMetrics) {
      const value = summary[metric]
      assert(value !== undefined && Number.isFinite(value) && value <= 0, `Positive terminal retention: ${metric}`)
    }
  }
}

export function requireCandidateHeap(summaries: Record<string, number>[]): void {
  requireReleased(summaries)
  for (const summary of summaries) {
    const value = summary.runnerheapUsedReleasedGrowth
    assert(value !== undefined && Number.isFinite(value) && value <= heapCeiling, 'heap-budget-exceeds-ceiling')
  }
}

function estimate(base: Record<string, number>[], reference: Budgets['reference']): Budgets {
  requireCandidateHeap(base)
  const absolute: Record<string, number> = {}
  const pairedMargin: Record<string, number> = {}
  for (const [metric, floor] of Object.entries(floors)) {
    const values = base.map(summary => {
      const value = summary[metric]
      assert(value !== undefined && Number.isFinite(value))
      return Math.max(0, value)
    })
    const maximum = Math.max(...values)
    const margin = Math.max(floor, span(values) * 2, maximum * 0.2)
    pairedMargin[metric] = metric === 'runnerheapUsedReleasedGrowth' ? 0 : margin
    absolute[metric] = metric === 'runnerheapUsedReleasedGrowth' ? 0 : maximum + margin
  }
  return { protocol: measurementProtocol, reference, heap: { terminal: 0, full: 0, late: 0, slope: 0 }, absolute, pairedMargin }
}

export function calibrate(base: Record<string, number>[], reference: Budgets['reference']): Budgets {
  assert.equal(base.length, 3)
  return estimate(base, reference)
}

export function calibrateBatch(groups: Record<string, number>[][], reference: Budgets['reference']): Budgets {
  assert.equal(groups.length, 5)
  const within = groups.map(group => calibrate(group, reference))
  const pooled = estimate(groups.flat(), reference)
  for (const metric of Object.keys(pooled.pairedMargin)) {
    pooled.pairedMargin[metric] = Math.max(...within.map(budgets => {
      const margin = budgets.pairedMargin[metric]
      assert(margin !== undefined)
      return margin
    }))
  }
  return pooled
}

export function parseBudgets(value: unknown): Budgets {
  assert(value !== null && typeof value === 'object' && 'reference' in value && 'absolute' in value && 'pairedMargin' in value)
  assert('protocol' in value && value.protocol === measurementProtocol, 'Incompatible browser measurement protocol')
  assert(Object.keys(value).sort().join(',') === 'absolute,heap,pairedMargin,protocol,reference', 'Invalid budget keys')
  assert('heap' in value)
  const heap = parseHeapLimits(value.heap)
  const reference = value.reference
  assert(reference !== null && typeof reference === 'object')
  assert('platform' in reference && typeof reference.platform === 'string')
  assert('arch' in reference && typeof reference.arch === 'string')
  assert('node' in reference && typeof reference.node === 'string')
  assert('browser' in reference && typeof reference.browser === 'string')
  const numbers = (input: unknown): Record<string, number> => {
    assert(input !== null && typeof input === 'object')
    const output: Record<string, number> = {}
    for (const [key, item] of Object.entries(input)) {
      assert(typeof item === 'number' && Number.isFinite(item) && item >= 0)
      output[key] = item
    }
    assert.deepEqual(Object.keys(output).sort(), Object.keys(floors).sort())
    for (const metric of terminalMetrics) assert.equal(output[metric], 0, `Terminal budget must be zero: ${metric}`)
    assert.equal(output.runnerheapUsedReleasedGrowth, heap.terminal, 'Terminal heap budget mismatch')
    return output
  }
  return { protocol: measurementProtocol, heap, reference: { platform: reference.platform, arch: reference.arch, node: reference.node, browser: reference.browser }, absolute: numbers(value.absolute), pairedMargin: numbers(value.pairedMargin) }
}

export function compare(base: Record<string, number>[], head: Record<string, number>[], budgets: Budgets): { gates: Record<string, boolean>; paired: Record<string, number[]>; variability: Record<string, number> } {
  assert.equal(base.length, 3)
  assert.equal(head.length, 3)
  const gates: Record<string, boolean> = {}
  const paired: Record<string, number[]> = {}
  const variability: Record<string, number> = {}
  for (const [metric, limit] of Object.entries(budgets.absolute)) {
    const baseValues: number[] = []
    const headValues: number[] = []
    for (let index = 0; index < 3; index++) {
      const before = base[index]?.[metric]
      const after = head[index]?.[metric]
      assert(before !== undefined && after !== undefined)
      baseValues.push(Math.max(0, before))
      headValues.push(Math.max(0, after))
      gates[`${metric}-absolute-base-${String(index)}`] = before <= limit
      gates[`${metric}-absolute-head-${String(index)}`] = after <= limit
    }
    const differences = headValues.map((value, index) => value - (baseValues[index] ?? NaN))
    paired[metric] = differences
    variability[`${metric}-base`] = span(baseValues)
    variability[`${metric}-head`] = span(headValues)
    const margin = budgets.pairedMargin[metric]
    assert(margin !== undefined)
    gates[`${metric}-paired`] = percentile(differences, 0.5) <= margin && Math.max(...differences) <= margin * 2
  }
  return { gates, paired, variability }
}
