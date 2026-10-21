import assert from 'node:assert/strict'
import { readFile, readdir } from 'node:fs/promises'
import { setImmediate } from 'node:timers/promises'

export interface NativeResources {
  pids: number[]
  rss: number | null
  descriptors: number | null
  processes: number | null
}

function missing(error: unknown): boolean {
  return error instanceof Error && 'code' in error && error.code === 'ENOENT'
}

async function children(pid: number): Promise<number[]> {
  try {
    const raw = await readFile(`/proc/${String(pid)}/task/${String(pid)}/children`, 'utf8')
    return raw.trim() === '' ? [] : raw.trim().split(/\s+/u).map(Number)
  } catch (error) {
    if (missing(error)) return []
    throw error
  }
}

export async function nativeResources(roots: number[]): Promise<NativeResources> {
  if (process.platform !== 'linux') {
    return { pids: roots, rss: null, descriptors: null, processes: null }
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
      if (resident === undefined) continue
      const entries = await readdir(`/proc/${String(pid)}/fd`)
      seen.add(pid)
      rss += Number(resident) * 1024
      descriptors += entries.length
      pending.push(...await children(pid))
    } catch (error) {
      if (!missing(error)) throw error
    }
  }
  return { pids: [...seen].sort((a, b) => a - b), rss, descriptors, processes: seen.size }
}

export async function verifyReleased(pids: number[]): Promise<NativeResources> {
  const deadline = performance.now() + 5000
  let remaining = await nativeResources(pids)
  while (remaining.processes !== null && remaining.processes > 0 && performance.now() < deadline) {
    await setImmediate()
    remaining = await nativeResources(pids)
  }
  assert(remaining.processes === null || remaining.processes === 0, 'Owned descendants remain alive')
  return remaining
}

export function numberField(value: unknown, key: string): number {
  assert(value !== null && typeof value === 'object' && key in value)
  const result: unknown = Reflect.get(value, key)
  assert(typeof result === 'number' && Number.isFinite(result))
  return result
}

export function span(values: number[]): number {
  assert(values.length > 0 && values.every(Number.isFinite))
  return Math.max(...values) - Math.min(...values)
}

export function percentile(values: number[], fraction: number): number {
  assert(values.length > 0)
  const sorted = [...values].sort((a, b) => a - b)
  const value = sorted[Math.ceil(sorted.length * fraction) - 1]
  assert(value !== undefined)
  return value
}
