import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { closeSync, constants, fstatSync, openSync, readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { getHeapCodeStatistics } from 'node:v8'
import { measurementProtocol } from './protocol.ts'
import { readEvidence, writeEvidence } from './trial-evidence.ts'

import { diagnosticPlan } from './diagnostic-plan.ts'

export { diagnosticPlan } from './diagnostic-plan.ts'
export const checkpointBoundaries = ['before', 'natural', 'terminal'] as const
type Boundary = typeof checkpointBoundaries[number]
type Code = ReturnType<typeof getHeapCodeStatistics>

function record(value: unknown): Record<string, unknown> {
  assert(value !== null && typeof value === 'object' && !Array.isArray(value))
  return value as Record<string, unknown>
}

function counters(value: unknown, keys: readonly string[]): void {
  const input = record(value)
  assert.deepEqual(Object.keys(input).sort(), [...keys].sort())
  for (const field of keys) assert(typeof input[field] === 'number' && Number.isFinite(input[field]) && input[field] >= 0)
}

export function persistCheckpoint(root: string, arm: 'base' | 'head', repeat: number, boundary: Boundary, memory: NodeJS.MemoryUsage, code: Code, budgetRoot = root): void {
  assert(['base', 'head'].includes(arm))
  assert(Number.isInteger(repeat) && repeat >= 0 && repeat < 3)
  assert(checkpointBoundaries.includes(boundary))
  counters(memory, ['rss', 'heapTotal', 'heapUsed', 'external', 'arrayBuffers'])
  counters(code, ['code_and_metadata_size', 'bytecode_and_metadata_size', 'external_script_source_size', 'cpu_profiler_metadata_size'])
  writeEvidence(root, `${arm}-${String(repeat)}-checkpoint-${boundary}.json`, { protocol: measurementProtocol, diagnosticPlan, arm, repeat, boundary, memory, code }, 65536, budgetRoot)
}

export function checkpointMemory(root: string, arm: 'base' | 'head', repeat: number, boundary: Boundary, budgetRoot = root): NodeJS.MemoryUsage {
  const memory = process.memoryUsage()
  const code = getHeapCodeStatistics()
  persistCheckpoint(root, arm, repeat, boundary, memory, code, budgetRoot)
  return memory
}

export function verifyCheckpoints(root: string): void {
  const names = readdirSync(root)
  assert.equal(names.filter(name => name.includes('-checkpoint-')).length, 18)
  for (const arm of ['base', 'head'] as const) for (let repeat = 0; repeat < 3; repeat++) {
    const trial = record(readEvidence(join(root, `${arm}-${String(repeat)}.json`)))
    for (const boundary of checkpointBoundaries) {
      const checkpoint = record(readEvidence(join(root, `${arm}-${String(repeat)}-checkpoint-${boundary}.json`)))
      assert.equal(checkpoint.protocol, measurementProtocol)
      assert.equal(checkpoint.diagnosticPlan, diagnosticPlan)
      assert.equal(checkpoint.arm, arm)
      assert.equal(checkpoint.repeat, repeat)
      assert.equal(checkpoint.boundary, boundary)
      assert.deepEqual(checkpoint.memory, trial[boundary === 'before' ? 'runnerBefore' : boundary === 'natural' ? 'runnerReleasedBeforeGC' : 'runnerAfter'])
      counters(checkpoint.code, ['code_and_metadata_size', 'bytecode_and_metadata_size', 'external_script_source_size', 'cpu_profiler_metadata_size'])
    }
  }
  assert.deepEqual(names.filter(name => name.startsWith('node-') && name.endsWith('.manifest.json')), ['node-terminal.manifest.json'])
  const manifest = record(readEvidence(join(root, 'node-terminal.manifest.json')))
  assert.equal(manifest.protocol, 'collector-diagnostic-v1')
  assert.equal(manifest.diagnosticPlan, diagnosticPlan)
  assert.equal(manifest.name, 'node-terminal')
  assert.equal(manifest.complete, true)
  assert(Array.isArray(manifest.shards) && manifest.shards.length > 0)
  assert.equal(names.filter(name => name.startsWith('node-') && name.endsWith('.heapsnapshot.part')).length, manifest.shards.length)
  for (const [index, value] of (manifest.shards as unknown[]).entries()) {
    const shard = record(value)
    const name = `node-terminal-${String(index)}.heapsnapshot.part`
    assert.equal(shard.name, name)
    const descriptor = openSync(join(root, name), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
    try {
      const stats = fstatSync(descriptor)
      assert(stats.isFile() && stats.size > 0 && stats.size <= 32 * 1024 * 1024)
      assert.equal(stats.size, shard.bytes)
      const bytes = readFileSync(descriptor)
      assert.equal(bytes.byteLength, stats.size)
      assert.equal(createHash('sha256').update(bytes).digest('hex'), shard.sha256)
    } finally { closeSync(descriptor) }
  }
}
