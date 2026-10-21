import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { createHash } from 'node:crypto'
import { cp, mkdir, readFile, readdir, symlink, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { promisify } from 'node:util'
import { build } from 'vite'

const execute = promisify(execFile)
export const webRoot = resolve(import.meta.dirname, '../..')
export const repository = dirname(webRoot)

export async function git(args: string[]): Promise<string> {
  const result = await execute('git', args, { cwd: repository, maxBuffer: 16 * 1024 * 1024, timeout: 30000 })
  return result.stdout.trim()
}

export async function buildBase(revision: string, temporary: string): Promise<string> {
  assert(/^[a-f0-9]{40}$/u.test(revision))
  const root = join(temporary, 'base', 'web')
  await mkdir(root, { recursive: true })
  const tree = await git(['ls-tree', '-r', revision, '--', 'web'])
  for (const entry of tree.split('\n')) {
    const match = /^(100644|100755) blob ([a-f0-9]{40})\t(web\/[^\n]+)$/u.exec(entry)
    assert(match?.[2] && match[3], 'Unsupported base tree entry')
    const relative = match[3].slice(4)
    assert(!relative.split('/').includes('..'))
    const destination = join(root, relative)
    await mkdir(dirname(destination), { recursive: true })
    const result = await execute('git', ['cat-file', 'blob', match[2]], { cwd: repository, encoding: 'buffer', maxBuffer: 16 * 1024 * 1024, timeout: 30000 })
    await writeFile(destination, result.stdout)
  }
  await symlink(join(webRoot, 'node_modules'), join(root, 'node_modules'), 'dir')
  await cp(join(webRoot, 'vite.config.ts'), join(root, 'vite.config.ts'))
  await build({ root, configFile: join(root, 'vite.config.ts'), logLevel: 'error' })
  return root
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
