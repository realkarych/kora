import assert from 'node:assert/strict'
import type { Cleanup } from './ownership.ts'
import { measurementProtocol } from './protocol.ts'
import type { NativeResources } from './resources.ts'
import type { Cycle, Trial } from './trial.ts'

function record(value: unknown): Record<string, unknown> {
  assert(value !== null && typeof value === 'object' && !Array.isArray(value))
  return Object.fromEntries(Object.entries(value))
}

function array(value: unknown): unknown[] {
  assert(Array.isArray(value))
  return value as unknown[]
}

function number(value: unknown): number {
  assert(typeof value === 'number' && Number.isFinite(value))
  return value
}

function string(value: unknown): string {
  assert(typeof value === 'string')
  return value
}

function boolean(value: unknown): boolean {
  assert(typeof value === 'boolean')
  return value
}

function nullable(value: unknown): number | null {
  return value === null ? null : number(value)
}

function numbers<K extends string>(value: unknown, keys: readonly K[]): Record<K, number> {
  const input = record(value)
  return Object.fromEntries(keys.map(key => [key, number(input[key])])) as Record<K, number>
}

function native(value: unknown): NativeResources {
  const input = record(value)
  return { pids: array(input.pids).map(number), rss: nullable(input.rss), descriptors: nullable(input.descriptors), processes: nullable(input.processes) }
}

function memory(value: unknown): NodeJS.MemoryUsage {
  return numbers(value, ['rss', 'heapTotal', 'heapUsed', 'external', 'arrayBuffers'])
}

function cycle(value: unknown): Cycle {
  const input = record(value)
  const heapKeys = ['used', 'total', 'documents', 'nodes', 'listeners'] as const
  return {
    cycle: number(input.cycle), active: native(input.active), beforeGCNative: native(input.beforeGCNative),
    beforeGC: numbers(input.beforeGC, heapKeys), afterGC: numbers(input.afterGC, heapKeys),
    native: native(input.native), owned: native(input.owned), runnerBeforeGC: memory(input.runnerBeforeGC), runner: memory(input.runner),
    runnerCode: numbers(input.runnerCode, ['code_and_metadata_size', 'bytecode_and_metadata_size', 'external_script_source_size', 'cpu_profiler_metadata_size']),
    contexts: number(input.contexts), pages: number(input.pages),
  }
}

function cleanup(value: unknown): Cleanup {
  const input = record(value)
  return {
    browserExit: nullable(input.browserExit), serverExit: nullable(input.serverExit), seconds: number(input.seconds), remaining: native(input.remaining),
    forced: boolean(input.forced), proxyDenied: number(input.proxyDenied), proxySockets: number(input.proxySockets),
    failures: array(input.failures).map(value => {
      const failure = record(value)
      return { stage: string(failure.stage), name: string(failure.name), message: string(failure.message), code: failure.code === null ? null : string(failure.code) }
    }),
  }
}

export function parseTrial(value: unknown): Trial {
  const input = record(value)
  assert.equal(input.protocol, measurementProtocol)
  assert(input.arm === 'base' || input.arm === 'head')
  const diagnostics = record(input.diagnostics)
  const loads = (value: unknown): Trial['loads'] => array(value).map(item => numbers(item, ['load', 'fcp', 'lcp', 'cls', 'longTasks', 'transfer']))
  return {
    protocol: measurementProtocol, arm: input.arm, repeat: number(input.repeat), browserVersion: string(input.browserVersion),
    runnerBefore: memory(input.runnerBefore), before: native(input.before),
    phases: array(input.phases).map(value => {
      const phase = record(value)
      return { phase: string(phase.phase), native: native(phase.native), runner: memory(phase.runner) }
    }),
    warmLoads: loads(input.warmLoads), loads: loads(input.loads), warmCycles: array(input.warmCycles).map(cycle), cycles: array(input.cycles).map(cycle),
    diagnostics: { overflow: boolean(diagnostics.overflow), events: array(diagnostics.events).map(value => {
      const event = record(value)
      return { kind: string(event.kind), detail: string(event.detail) }
    }) },
    ...(input.runnerAfter === undefined ? {} : { runnerAfter: memory(input.runnerAfter) }),
    ...(input.runnerReleasedBeforeGC === undefined ? {} : { runnerReleasedBeforeGC: memory(input.runnerReleasedBeforeGC) }),
    ...(input.after === undefined ? {} : { after: native(input.after) }),
    ...(input.retainedEvidenceBytes === undefined ? {} : { retainedEvidenceBytes: number(input.retainedEvidenceBytes) }),
    ...(input.cleanup === undefined ? {} : { cleanup: cleanup(input.cleanup) }),
    ...(input.failure === undefined ? {} : { failure: string(input.failure) }),
    ...(input.serverOutput === undefined ? {} : { serverOutput: array(input.serverOutput).map(string) }),
  }
}
