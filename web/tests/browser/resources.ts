import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { readFile, readdir } from 'node:fs/promises'
import { setImmediate } from 'node:timers/promises'

export interface NativeResources {
  pids: number[]
  rss: number | null
  descriptors: number | null
  processes: number | null
}

function missing(error: unknown): boolean {
  return error instanceof Error && 'code' in error && (error.code === 'ENOENT' || error.code === 'ESRCH')
}

async function children(pid: number): Promise<number[]> {
  try {
    const directory = `/proc/${String(pid)}/task`
    const descendants = new Set<number>()
    for (const thread of await readdir(directory)) {
      try {
        const raw = await readFile(`${directory}/${thread}/children`, 'utf8')
        if (raw.trim() !== '') for (const child of raw.trim().split(/\s+/u)) descendants.add(Number(child))
      } catch (error) {
        if (!missing(error)) throw error
      }
    }
    return [...descendants]
  } catch (error) {
    if (missing(error)) return []
    throw error
  }
}

export async function nativeResources(roots: number[]): Promise<NativeResources> {
  if (process.platform !== 'linux') {
    const { stdout } = await promisify(execFile)('/bin/ps', ['-Ao', 'pid=,ppid=,state='], { maxBuffer: 4 * 1024 * 1024 })
    const entries = stdout.trim().split('\n').map(line => {
      const fields = line.trim().split(/\s+/u)
      assert(fields.length === 3 && fields[2])
      return { pid: Number(fields[0]), parent: Number(fields[1]), live: !fields[2].startsWith('Z') }
    })
    const seen = new Set<number>()
    const pending = [...roots]
    while (pending.length > 0) {
      const pid = pending.pop()
      assert(pid)
      if (seen.has(pid)) continue
      const entry = entries.find(item => item.pid === pid && item.live)
      if (!entry) continue
      seen.add(pid)
      pending.push(...entries.filter(item => item.parent === pid && item.live).map(item => item.pid))
    }
    return { pids: [...seen].sort((a, b) => a - b), rss: null, descriptors: null, processes: seen.size }
  }
  const seen = new Set<number>()
  const pending = [...roots]
  let rss = 0
  let descriptors = 0
  while (pending.length > 0) {
    const pid = pending.pop()
    assert(pid)
    if (seen.has(pid)) continue
    try {
      const status = await readFile(`/proc/${String(pid)}/status`, 'utf8')
      const resident = /^VmRSS:\s+(\d+) kB$/mu.exec(status)?.[1]
      pending.push(...await children(pid))
      if (resident === undefined) continue
      const entries = await readdir(`/proc/${String(pid)}/fd`)
      seen.add(pid)
      rss += Number(resident) * 1024
      descriptors += entries.length
    } catch (error) {
      if (missing(error)) continue
      if (error instanceof Error && 'code' in error && (error.code === 'EACCES' || error.code === 'EPERM')) {
        try {
          const status = await readFile(`/proc/${String(pid)}/status`, 'utf8')
          if (/^State:\s+[ZXx]\b/mu.test(status)) continue
        } catch (observation) {
          if (missing(observation)) continue
          throw observation
        }
      }
      throw error
    }
  }
  return { pids: [...seen].sort((a, b) => a - b), rss, descriptors, processes: seen.size }
}

export async function verifyReleased(pids: number[], observe: typeof nativeResources = nativeResources): Promise<NativeResources> {
  const deadline = performance.now() + 5000
  for (;;) {
    try {
      const remaining = await observe(pids)
      if (remaining.processes === null || remaining.processes === 0) return remaining
      assert(performance.now() < deadline, 'Owned descendants remain alive')
    } catch (error) {
      if (!(error instanceof Error && 'code' in error && (error.code === 'EACCES' || error.code === 'EPERM')) || performance.now() >= deadline) throw error
    }
    await setImmediate()
  }
}
