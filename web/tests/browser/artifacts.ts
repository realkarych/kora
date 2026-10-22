import assert from 'node:assert/strict'
import { statSync, watch, readdirSync, truncateSync, writeFileSync } from 'node:fs'
import type { FSWatcher } from 'node:fs'
import { join } from 'node:path'

export const directoryLimit = 512 * 1024 * 1024

export class ArtifactLimit {
  readonly root: string
  readonly watcher: FSWatcher
  readonly oversized = new Set<string>()
  failed = false
  readonly stop: () => void

  constructor(root: string, stop: () => void) {
    this.root = root
    this.stop = stop
    this.watcher = watch(root, { recursive: true }, () => { this.check() })
    this.watcher.on('error', () => { this.fail() })
  }

  fail(): void {
    if (this.failed) return
    this.failed = true
    this.stop()
  }

  check(): void {
    let bytes = 0
    for (const entry of readdirSync(this.root, { recursive: true, withFileTypes: true })) {
      if (!entry.isFile()) continue
      const path = join(entry.parentPath, entry.name)
      try {
        const size = statSync(path).size
        bytes += size
        if (size > 32 * 1024 * 1024) { this.oversized.add(path); this.fail() }
      } catch (error) {
        if (!(error instanceof Error && 'code' in error && error.code === 'ENOENT')) throw error
      }
    }
    if (bytes > directoryLimit) this.fail()
  }

  close(): void {
    this.watcher.close()
    this.check()
    let remaining = directoryLimit - 4 * 1024 * 1024 - 65536
    const files = readdirSync(this.root, { recursive: true, withFileTypes: true }).filter(entry => entry.isFile())
    files.sort((left, right) => Number(right.name.endsWith('.json')) - Number(left.name.endsWith('.json')))
    for (const entry of files) {
      const path = join(entry.parentPath, entry.name)
      const size = statSync(path).size
      const retained = Math.min(size, remaining, 32 * 1024 * 1024)
      if (retained < size) { this.fail(); this.oversized.add(path); truncateSync(path, retained) }
      remaining -= retained
    }
    if (this.failed) {
      const truncated = [...this.oversized].slice(0, 100).map(path => path.slice(0, 512))
      const serialize = (): string => JSON.stringify({ failed: true, truncatedCount: this.oversized.size, truncated })
      while (Buffer.byteLength(serialize()) > 65536 && truncated.length > 0) truncated.pop()
      const marker = serialize()
      assert(Buffer.byteLength(marker) <= 65536)
      writeFileSync(join(this.root, 'artifact-limit.json'), marker)
    }
  }
}
