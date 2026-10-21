import assert from 'node:assert/strict'
import { mkdtemp, readFile, readdir, rm, stat, truncate, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { setImmediate } from 'node:timers/promises'
import { ArtifactLimit } from './artifacts.ts'
import { checkCleanup, Ownership } from './ownership.ts'

export async function artifactLimits(root: string, output: string): Promise<void> {
  const evidence: unknown[] = []
  try {
    for (const mode of ['file', 'directory', 'reserve']) {
      const directory = await mkdtemp(join(tmpdir(), 'kora-artifact-limit-'))
      const owner = new Ownership()
      let stops = 0
      const limit = new ArtifactLimit(directory, () => { stops++; void owner.close() })
      try {
        await owner.startServer('production', root)
        await owner.startBrowser()
        const count = mode === 'file' ? 1 : mode === 'reserve' ? 16 : 17
        for (let index = 0; index < count; index++) {
          const path = join(directory, `sparse-${String(index)}`)
          await writeFile(path, '')
          await truncate(path, 32 * 1024 * 1024 + (mode === 'file' ? 1 : mode === 'reserve' && index === 0 ? -1 : 0))
        }
        if (mode === 'reserve') limit.close()
        const deadline = performance.now() + 5000
        while (!limit.failed && performance.now() < deadline) await setImmediate()
        assert(limit.failed, 'Artifact limit failed to stop work')
        const cleanup = await owner.close()
        limit.close()
        const marker: unknown = JSON.parse(await readFile(join(directory, 'artifact-limit.json'), 'utf8'))
        const files = await readdir(directory)
        const sizes = await Promise.all(files.map(async file => (await stat(join(directory, file))).size))
        const total = sizes.reduce((sum, value) => sum + value, 0)
        evidence.push({ mode, stops, marker, sizes, total, cleanup })
        assert.equal(stops, 1)
        assert(Math.max(...sizes) <= 32 * 1024 * 1024)
        assert(total <= 512 * 1024 * 1024)
        checkCleanup(cleanup)
      } finally {
        await owner.close()
        limit.watcher.close()
        await rm(directory, { recursive: true, force: true })
      }
    }
  } finally {
    await writeFile(join(output, 'artifact-limits.json'), JSON.stringify(evidence, null, 2))
  }
}
