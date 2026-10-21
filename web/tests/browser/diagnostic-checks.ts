import assert from 'node:assert/strict'
import { fork } from 'node:child_process'
import { createHash } from 'node:crypto'
import { once } from 'node:events'
import { closeSync, mkdtempSync, openSync, readFileSync, readdirSync, rmSync, truncateSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Readable } from 'node:stream'
import { inspectArtifacts, MarkerScanner } from './diagnostic-inspection.ts'
import { artifactBytes, directoryLimit, shardLimit, SnapshotShards, writeSnapshot } from './diagnostic-snapshot.ts'

export async function diagnosticChecks(): Promise<void> {
  const root = mkdtempSync(join(tmpdir(), 'kora-snapshot-checks-'))
  try {
    const marker = 'excluded-environment-value'
    const scanner = new MarkerScanner(marker)

    scanner.consume(Buffer.from(marker.slice(0, 8)))
    scanner.consume(Buffer.from(marker.slice(8)))

    assert(scanner.found)

    writeFileSync(join(root, 'marker-0.part'), marker.slice(0, 8))
    writeFileSync(join(root, 'marker-1.part'), marker.slice(8))

    await assert.rejects(inspectArtifacts(root, marker, new AbortController().signal), /Excluded environment marker/u)

    rmSync(join(root, 'marker-0.part'))
    rmSync(join(root, 'marker-1.part'))

    const input = Buffer.alloc(shardLimit + 17, 65)
    const writer = new SnapshotShards(root, 'split')

    writer.write(input)
    writer.finish(true)

    assert.deepEqual(writer.shards.map(shard => shard.bytes), [shardLimit, 17])
    for (const shard of writer.shards) assert.equal(createHash('sha256').update(readFileSync(join(root, shard.name))).digest('hex'), shard.sha256)
    assert.deepEqual(Buffer.concat(writer.shards.map(shard => readFileSync(join(root, shard.name)))), input)

    const child = fork(join(import.meta.dirname, 'diagnostic-inspection-fixture.ts'), [root], { execArgv: [], env: {}, stdio: ['ignore', 'ignore', 'inherit', 'ipc'] })
    const closed = once(child, 'close')
    const deadline = new AbortController()
    const timer = setTimeout(() => { deadline.abort() }, 5000)
    try {
      await Promise.race([once(child, 'message'), closed.then(() => { throw new Error('Inspection fixture exited before readiness') }), once(deadline.signal, 'abort').then(() => { throw new Error('Inspection fixture deadline exceeded') })])

      child.kill('SIGTERM')
      await Promise.race([closed, once(deadline.signal, 'abort').then(() => { throw new Error('Inspection cancellation deadline exceeded') })])

      assert.equal(child.exitCode, 0)
    } finally { clearTimeout(timer); child.kill('SIGKILL'); await closed }

    const reserve = join(root, 'capacity')
    writeFileSync(reserve, '')
    truncateSync(reserve, directoryLimit - 4 * 1024 * 1024 - 2 * 65536 - artifactBytes(root))
    const overflow = new SnapshotShards(root, 'overflow')
    const before = artifactBytes(root)

    assert.throws(() => { overflow.write(Buffer.from([1])) }, /cumulative artifact limit/u)
    overflow.finish(false)

    assert(artifactBytes(root) - before <= 65536)
    assert.equal(overflow.shards.length, 0)
    assert(!readdirSync(root).includes('overflow-0.heapsnapshot.part'))
    rmSync(reserve)

    const occupied = join(root, 'occupied-0.heapsnapshot.part')
    const descriptor = openSync(occupied, 'wx')
    closeSync(descriptor)
    const collision = new SnapshotShards(root, 'occupied')

    assert.throws(() => { collision.write(Buffer.from([1])) }, { code: 'EEXIST' })
    collision.finish(false)

    assert.equal(readFileSync(occupied).byteLength, 0)

    const controller = new AbortController()
    function* interrupted(): Generator<Buffer> {
      yield Buffer.from('partial')
      controller.abort()
      yield Buffer.from('forbidden')
    }
    const stream = Readable.from(interrupted(), { objectMode: false, highWaterMark: 1 })

    await assert.rejects(writeSnapshot(stream, root, 'cancelled', controller.signal))

    assert(stream.destroyed)
    const partial: unknown = JSON.parse(readFileSync(join(root, 'cancelled.manifest.json'), 'utf8'))
    assert(partial !== null && typeof partial === 'object' && 'complete' in partial && partial.complete === false)
    assert(artifactBytes(root) <= directoryLimit)
  } finally { rmSync(root, { recursive: true, force: true }) }
}
