import assert from 'node:assert/strict'
import { createReadStream } from 'node:fs'
import { readdir } from 'node:fs/promises'
import { join } from 'node:path'

export class MarkerScanner {
  readonly patterns: Buffer[]
  readonly overlap: number
  tail = Buffer.alloc(0)
  found = false

  constructor(marker: string) {
    assert(marker.length > 0)
    this.patterns = [Buffer.from(marker), Buffer.from(marker, 'utf16le')]
    this.overlap = Math.max(...this.patterns.map(pattern => pattern.byteLength)) - 1
  }

  consume(chunk: Buffer): void {
    const bytes = Buffer.concat([this.tail, chunk])
    this.found ||= this.patterns.some(pattern => bytes.includes(pattern))
    this.tail = Buffer.from(bytes.subarray(-this.overlap))
  }
}

export async function inspectArtifacts(output: string, marker: string, signal: AbortSignal, firstChunk?: () => Promise<void>): Promise<void> {
  signal.throwIfAborted()
  const scanner = new MarkerScanner(marker)
  const entries = await readdir(output, { recursive: true, withFileTypes: true })
  entries.sort((left, right) => join(left.parentPath, left.name).localeCompare(join(right.parentPath, right.name), 'en', { numeric: true }))
  for (const entry of entries) {
    signal.throwIfAborted()
    assert(!entry.isSymbolicLink())
    if (!entry.isFile()) continue
    for await (const chunk of createReadStream(join(entry.parentPath, entry.name), { signal })) {
      signal.throwIfAborted()
      assert(Buffer.isBuffer(chunk))
      scanner.consume(chunk)
      if (firstChunk) { await firstChunk(); firstChunk = undefined }
      signal.throwIfAborted()
      assert(!scanner.found, 'Excluded environment marker entered artifacts')
    }
  }
  signal.throwIfAborted()
}

export interface ArtifactInspection {
  status: 'passed' | 'failed' | 'unverified'
  reason: 'complete' | 'rejected' | 'interrupted'
}

export async function inspectRetainedArtifacts(output: string, marker: string, signal: AbortSignal = AbortSignal.timeout(30000), firstChunk?: () => Promise<void>): Promise<ArtifactInspection> {
  const [inspection] = await Promise.allSettled([inspectArtifacts(output, marker, signal, firstChunk)])
  assert(inspection)
  if (signal.aborted) return { status: 'unverified', reason: 'interrupted' }
  if (inspection.status === 'rejected') return { status: 'failed', reason: 'rejected' }
  return { status: 'passed', reason: 'complete' }
}
