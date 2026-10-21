import assert from 'node:assert/strict'
import type { getHeapCodeStatistics } from 'node:v8'
import type { Cleanup } from './ownership.ts'
import type { Diagnostics, Heap, Load } from './page.ts'
import { percentile, span } from './resources.ts'
import type { NativeResources } from './resources.ts'
import { measurementProtocol } from './protocol.ts'

export interface Cycle {
  cycle: number
  active: NativeResources
  beforeGCNative: NativeResources
  beforeGC: Heap
  afterGC: Heap
  native: NativeResources
  owned: NativeResources
  runnerBeforeGC: NodeJS.MemoryUsage
  runner: NodeJS.MemoryUsage
  runnerCode: ReturnType<typeof getHeapCodeStatistics>
  contexts: number
  pages: number
}

export interface Trial {
  protocol: typeof measurementProtocol
  arm: 'base' | 'head'
  repeat: number
  phases: { phase: string; native: NativeResources; runner: NodeJS.MemoryUsage }[]
  runnerBefore: NodeJS.MemoryUsage
  runnerAfter?: NodeJS.MemoryUsage
  runnerReleasedBeforeGC?: NodeJS.MemoryUsage
  retainedEvidenceBytes?: number
  warmLoads: Load[]
  warmCycles: Cycle[]
  after?: NativeResources
  loads: Load[]
  cycles: Cycle[]
  browserVersion: string
  diagnostics: Pick<Diagnostics, 'events' | 'overflow'>
  before: NativeResources
  cleanup?: Cleanup
  failure?: string
  serverOutput?: string[]
}

export function summarize(trial: Trial): Record<string, number> {
  const result: Record<string, number> = {}
  const allCycles = [...trial.warmCycles, ...trial.cycles]
  assert(trial.runnerAfter && trial.runnerReleasedBeforeGC)
  const runnerMeasurements = [trial.runnerBefore, trial.runnerAfter, trial.runnerReleasedBeforeGC, ...allCycles.flatMap(cycle => [cycle.runnerBeforeGC, cycle.runner]), ...trial.phases.map(phase => phase.runner)]
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
    result[`${metric}Peak`] = Math.max(...allCycles.flatMap(cycle => [cycle.beforeGC[metric], cycle.afterGC[metric]]))
  }
  for (const metric of ['rss', 'descriptors', 'processes'] as const) {
    const values = trial.cycles.map(cycle => cycle.native[metric])
    if (values.some(value => value === null)) continue
    const numbers = values.filter(value => value !== null)
    result[`${metric}Span`] = span(numbers)
    result[`${metric}LateSpan`] = span(numbers.slice(15))
    result[`${metric}MedianGrowth`] = percentile(numbers.slice(-10), 0.5) - percentile(numbers.slice(0, 10), 0.5)
    const active = allCycles.flatMap(cycle => [cycle.active[metric], cycle.beforeGCNative[metric], cycle.native[metric]]).filter(value => value !== null)
    const phases = trial.phases.map(phase => phase.native[metric]).filter(value => value !== null)
    result[`${metric}Peak`] = Math.max(...numbers, ...active, ...phases)
  }
  for (const metric of ['heapUsed', 'external', 'arrayBuffers'] as const) {
    const values = trial.cycles.map(cycle => cycle.runner[metric])
    result[`runner${metric}Peak`] = Math.max(...runnerMeasurements.map(point => point[metric]))
    assert(trial.runnerAfter)
    result[`runner${metric}ReleasedGrowth`] = trial.runnerAfter[metric] - trial.runnerBefore[metric]
    result[`runner${metric}Span`] = span(values)
    result[`runner${metric}LateSpan`] = span(values.slice(15))
    result[`runner${metric}MedianGrowth`] = percentile(values.slice(-10), 0.5) - percentile(values.slice(0, 10), 0.5)
  }
  assert(trial.cleanup)
  result.cleanupSeconds = trial.cleanup.seconds
  return result
}
