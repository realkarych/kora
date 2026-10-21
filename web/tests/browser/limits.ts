import assert from 'node:assert/strict'
import { mkdir, mkdtemp, readFile, readdir, rm, stat, truncate, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { setImmediate } from 'node:timers/promises'
import { ArtifactLimit } from './artifacts.ts'
import { checkCleanup, Ownership } from './ownership.ts'

export async function artifactLimits(root: string, output: string): Promise<void> {
  const evidence: unknown[] = []
  try {
    for (const mode of ['file', 'directory', 'reserve', 'escaped-paths']) {
      const directory = await mkdtemp(join(tmpdir(), 'kora-artifact-limit-'))
      const owner = new Ownership()
      let stops = 0
      const limit = new ArtifactLimit(directory, () => { stops++; void owner.close() })
      try {
        await owner.startServer('production', root)
        await owner.startBrowser()
        const nested = mode === 'escaped-paths' ? join(directory, '\t'.repeat(220)) : directory
        await mkdir(nested, { recursive: true })
        const count = mode === 'file' ? 1 : mode === 'reserve' ? 16 : mode === 'escaped-paths' ? 100 : 17
        for (let index = 0; index < count; index++) {
          const path = join(nested, `${mode === 'escaped-paths' ? '\t'.repeat(220) : 'sparse-'}${String(index)}`)
          await writeFile(path, '')
          await truncate(path, 32 * 1024 * 1024 + (mode === 'file' || mode === 'escaped-paths' ? 1 : mode === 'reserve' && index === 0 ? -1 : 0))
        }
        if (mode === 'reserve') limit.close()
        const deadline = performance.now() + 5000
        while (!limit.failed && performance.now() < deadline) await setImmediate()
        assert(limit.failed, 'Artifact limit failed to stop work')
        const cleanup = await owner.close()
        limit.close()
        const marker: unknown = JSON.parse(await readFile(join(directory, 'artifact-limit.json'), 'utf8'))
        const files = (await readdir(directory, { recursive: true, withFileTypes: true })).filter(file => file.isFile())
        const sizes = await Promise.all(files.map(async file => (await stat(join(file.parentPath, file.name))).size))
        const markerBytes = (await stat(join(directory, 'artifact-limit.json'))).size
        assert(markerBytes <= 65536)
        const total = sizes.reduce((sum, value) => sum + value, 0)
        evidence.push({ mode, stops, marker, markerBytes, sizes, total, cleanup })
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
