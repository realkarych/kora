import assert from 'node:assert/strict'
import { setImmediate } from 'node:timers/promises'

export async function collectRunner(): Promise<void> {
  assert(globalThis.gc, 'Runner requires --expose-gc')
  globalThis.gc()
  await setImmediate()
  globalThis.gc()
  await setImmediate()
}
