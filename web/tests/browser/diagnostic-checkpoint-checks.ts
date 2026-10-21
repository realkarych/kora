import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, readdirSync, rmSync, truncateSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Readable } from 'node:stream'
import { setImmediate } from 'node:timers/promises'
import { getHeapCodeStatistics } from 'node:v8'
import { checkpointBoundaries, diagnosticPlan, persistCheckpoint, verifyCheckpoints } from './diagnostic-checkpoint.ts'
import { inspectRetainedArtifacts } from './diagnostic-inspection.ts'
import { artifactBytes, directoryLimit, writeSnapshot } from './diagnostic-snapshot.ts'
import { measurementProtocol } from './protocol.ts'
import { readEvidence, writeEvidence } from './trial-evidence.ts'

export async function checkpointChecks(): Promise<void> {
  const root = mkdtempSync(join(tmpdir(), 'kora-checkpoint-checks-'))
  try {
    const memory = process.memoryUsage()
    const code = getHeapCodeStatistics()
    const path = join(root, 'head-0-checkpoint-terminal.json')

    persistCheckpoint(root, 'head', 0, 'terminal', memory, code)

    assert.deepEqual(readEvidence(path), { protocol: measurementProtocol, diagnosticPlan, arm: 'head', repeat: 0, boundary: 'terminal', memory, code })
    const checkpoint = readFileSync(path)
    assert(checkpoint.byteLength <= 65536)

    assert.throws(() => { persistCheckpoint(root, 'head', 0, 'terminal', memory, code) }, { code: 'EEXIST' })
    assert.throws(() => { persistCheckpoint(root, 'head', 3, 'terminal', memory, code) })
    assert.throws(() => { persistCheckpoint(root, 'base', 0, 'before', { ...memory, heapUsed: Number.NaN }, code) })
    const unexpectedCode = { ...code, unexpected: 1 }
    assert.throws(() => { persistCheckpoint(root, 'base', 0, 'before', memory, unexpectedCode) })

    assert.deepEqual(readFileSync(path), checkpoint)
    assert.deepEqual(readdirSync(root), ['head-0-checkpoint-terminal.json'])

    const reserve = join(root, 'capacity')
    writeFileSync(reserve, '')
    truncateSync(reserve, directoryLimit - 4 * 1024 * 1024 - 2 * 65536 - artifactBytes(root))

    assert.throws(() => { persistCheckpoint(root, 'base', 0, 'before', memory, code) }, /cumulative bound/u)

    assert(!readdirSync(root).includes('base-0-checkpoint-before.json'))
    rmSync(reserve)

    const cancellation = new AbortController()
    const partial = join(root, 'interrupted-0.heapsnapshot.part')
    async function* chunks(): AsyncGenerator<Buffer> {
      yield Buffer.from('partial snapshot')
      const deadline = performance.now() + 5000
      while (!readdirSync(root).includes('interrupted-0.heapsnapshot.part') && performance.now() < deadline) await setImmediate()
      assert(readdirSync(root).includes('interrupted-0.heapsnapshot.part'))
      cancellation.abort()
      yield Buffer.from('not persisted')
    }
    const stream = Readable.from(chunks(), { objectMode: false, highWaterMark: 1 })

    const [workload] = await Promise.allSettled([writeSnapshot(stream, root, 'interrupted', cancellation.signal)])
    const inspection = await inspectRetainedArtifacts(root, 'excluded-marker')

    assert.equal(workload.status, 'rejected')
    assert(stream.destroyed)
    assert.deepEqual(inspection, { status: 'passed', reason: 'complete' })
    assert.deepEqual(readFileSync(path), checkpoint)
    assert.equal(readFileSync(partial, 'utf8'), 'partial snapshot')
    const manifest = readEvidence(join(root, 'interrupted.manifest.json'))
    assert(manifest !== null && typeof manifest === 'object' && 'complete' in manifest && manifest.complete === false)

    writeFileSync(join(root, 'rejected.part'), 'excluded-marker')

    const rejected = await inspectRetainedArtifacts(root, 'excluded-marker')

    assert.deepEqual(rejected, { status: 'failed', reason: 'rejected' })
    assert.equal(workload.status, 'rejected')
    rmSync(join(root, 'rejected.part'))

    const interrupted = new AbortController()
    const scanned = (): Promise<void> => { interrupted.abort(); return Promise.resolve() }

    const unverified = await inspectRetainedArtifacts(root, 'excluded-marker', interrupted.signal, scanned)

    assert.deepEqual(unverified, { status: 'unverified', reason: 'interrupted' })
    assert.equal(workload.status, 'rejected')
    assert.deepEqual(readFileSync(path), checkpoint)
  } finally { rmSync(root, { recursive: true, force: true }) }
}

export async function checkpointEvidenceChecks(): Promise<void> {
  const root = mkdtempSync(join(tmpdir(), 'kora-checkpoint-evidence-'))
  try {
    const memory = process.memoryUsage()
    const code = getHeapCodeStatistics()
    for (const arm of ['base', 'head'] as const) for (let repeat = 0; repeat < 3; repeat++) {
      for (const boundary of checkpointBoundaries) persistCheckpoint(root, arm, repeat, boundary, memory, code)
      writeEvidence(root, `${arm}-${String(repeat)}.json`, { runnerBefore: memory, runnerReleasedBeforeGC: memory, runnerAfter: memory }, 65536)
    }
    await writeSnapshot(Readable.from([Buffer.from('{}')]), root, 'node-terminal', new AbortController().signal)

    verifyCheckpoints(root)

    const path = join(root, 'head-0-checkpoint-terminal.json')
    const checkpoint = readFileSync(path, 'utf8')
    writeFileSync(path, checkpoint.replace(diagnosticPlan, 'unregistered-diagnostic'))

    assert.throws(() => { verifyCheckpoints(root) })

    writeFileSync(path, checkpoint)
    const manifestPath = join(root, 'node-terminal.manifest.json')
    const manifest = readFileSync(manifestPath, 'utf8')
    writeFileSync(manifestPath, manifest.replace('"complete":true', '"complete":false'))

    assert.throws(() => { verifyCheckpoints(root) })

    writeFileSync(manifestPath, manifest)
    writeFileSync(join(root, 'node-terminal-0.heapsnapshot.part'), '[]')

    assert.throws(() => { verifyCheckpoints(root) })
  } finally { rmSync(root, { recursive: true, force: true }) }
}
