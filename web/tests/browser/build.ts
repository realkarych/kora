import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { readFile, readdir } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { promisify } from 'node:util'

const execute = promisify(execFile)
export const webRoot = resolve(import.meta.dirname, '../..')
export const repository = dirname(webRoot)

export async function git(args: string[]): Promise<string> {
  const result = await execute('git', args, { cwd: repository, maxBuffer: 16 * 1024 * 1024, timeout: 30000 })
  return result.stdout.trim()
}

export async function assetHashes(root: string): Promise<Record<string, string>> {
  const directory = resolve(root, '../src/kora/api/static')
  const result: Record<string, string> = {}
  for (const entry of await readdir(directory, { recursive: true, withFileTypes: true })) {
    if (!entry.isFile()) continue
    const path = join(entry.parentPath, entry.name)
    const bytes = await readFile(path)
    result[path.slice(directory.length + 1)] = createHash('sha256').update(bytes).digest('hex')
  }
  return result
}
