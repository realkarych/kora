import { statSync, watch, readdirSync, truncateSync, writeFileSync } from 'node:fs'
import type { FSWatcher } from 'node:fs'
import { join } from 'node:path'

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
    if (bytes > 512 * 1024 * 1024) this.fail()
  }

  close(): void {
    this.watcher.close()
    this.check()
    for (const path of this.oversized) truncateSync(path, 32 * 1024 * 1024)
    if (this.failed) writeFileSync(join(this.root, 'artifact-limit.json'), JSON.stringify({ failed: true, truncated: [...this.oversized] }))
  }
}
