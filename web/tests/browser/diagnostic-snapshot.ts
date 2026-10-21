import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import { closeSync, openSync, readdirSync, statSync, writeSync } from 'node:fs'
import { join } from 'node:path'
import type { Readable } from 'node:stream'
import { getHeapSnapshot } from 'node:v8'

export const shardLimit = 32 * 1024 * 1024
export const directoryLimit = 512 * 1024 * 1024
const reservedBytes = 4 * 1024 * 1024 + 2 * 65536

export function artifactBytes(root: string): number {
  return readdirSync(root, { recursive: true, withFileTypes: true }).reduce((total, entry) => {
    assert(!entry.isSymbolicLink(), 'Diagnostic artifacts cannot contain symbolic links')
    return total + (entry.isFile() ? statSync(join(entry.parentPath, entry.name)).size : 0)
  }, 0)
}

export class SnapshotShards {
  readonly root: string
  readonly name: string
  readonly budgetRoot: string
  readonly shards: { name: string; bytes: number; sha256: string }[] = []
  descriptor: number | undefined
  bytes = 0
  hash = createHash('sha256')

  constructor(root: string, name: string, budgetRoot = root) {
    assert(/^[a-z][a-z0-9-]{0,63}$/u.test(name))
    this.root = root
    this.budgetRoot = budgetRoot
    this.name = name
  }

  finishShard(): void {
    if (this.descriptor === undefined) return
    closeSync(this.descriptor)
    this.descriptor = undefined
    this.shards.push({ name: `${this.name}-${String(this.shards.length)}.heapsnapshot.part`, bytes: this.bytes, sha256: this.hash.digest('hex') })
    this.bytes = 0
    this.hash = createHash('sha256')
  }

  write(buffer: Buffer): void {
    assert(artifactBytes(this.budgetRoot) + buffer.byteLength <= directoryLimit - reservedBytes, 'Diagnostic cumulative artifact limit exceeded')
    let offset = 0
    while (offset < buffer.byteLength) {
      this.descriptor ??= openSync(join(this.root, `${this.name}-${String(this.shards.length)}.heapsnapshot.part`), 'wx', 0o600)
      const end = Math.min(buffer.byteLength, offset + shardLimit - this.bytes)
      const piece = buffer.subarray(offset, end)
      const written = writeSync(this.descriptor, piece)
      assert(written > 0, 'Diagnostic write made no progress')
      this.hash.update(piece.subarray(0, written))
      this.bytes += written
      offset += written
      if (this.bytes === shardLimit) this.finishShard()
    }
  }

  finish(complete: boolean): void {
    this.finishShard()
    const payload = Buffer.from(JSON.stringify({ protocol: 'collector-diagnostic-v1', name: this.name, complete, shards: this.shards }))
    assert(payload.byteLength <= 65536)
    assert(artifactBytes(this.budgetRoot) + payload.byteLength <= directoryLimit - 4 * 1024 * 1024 - 65536)
    const descriptor = openSync(join(this.root, `${this.name}.manifest.json`), 'wx', 0o600)
    try { assert.equal(writeSync(descriptor, payload), payload.byteLength) }
    finally { closeSync(descriptor) }
  }
}

export async function writeSnapshot(stream: Readable, root: string, name: string, signal: AbortSignal, budgetRoot = root): Promise<void> {
  const writer = new SnapshotShards(root, name, budgetRoot)
  let complete = false
  const abort = (): void => { stream.destroy(new Error('Diagnostic snapshot interrupted')) }
  signal.addEventListener('abort', abort, { once: true })
  try {
    signal.throwIfAborted()
    for await (const chunk of stream) {
      signal.throwIfAborted()
      assert(Buffer.isBuffer(chunk), 'Unexpected snapshot chunk')
      writer.write(chunk)
    }
    complete = true
  } finally {
    signal.removeEventListener('abort', abort)
    stream.destroy()
    writer.finish(complete)
  }
}

export async function diagnosticSnapshot(root: string, name: string, signal: AbortSignal): Promise<void> {
  signal.throwIfAborted()
  await writeSnapshot(getHeapSnapshot(), root, name, signal, process.env.KORA_BROWSER_OUTPUT ?? root)
}
