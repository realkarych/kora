import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { once } from 'node:events'
import { createInterface } from 'node:readline'
import { setImmediate } from 'node:timers/promises'
import { checkCleanup, Ownership, scheduleClose } from './ownership.ts'
import { nativeResources } from './resources.ts'
import type { NativeResources } from './resources.ts'

const [scenario, python, fixture] = process.argv.slice(2)
assert(python && fixture)
assert(scenario)
assert(['readable', 'temporary-denial', 'persistent-denial', 'root-exit', 'late-snapshot'].includes(scenario))
const denied = scenario !== 'readable' && scenario !== 'late-snapshot'
const held = spawn(python, [fixture, denied ? 'denied' : 'readable'], { stdio: ['pipe', 'pipe', 'pipe'] })
const closed = once(held, 'close')
const lines = createInterface({ input: held.stdout })
const targets: number[] = []
let firstDenial: unknown
let acquired: NativeResources | undefined
let deadline: number | undefined
let observations = 0
try {
  assert.deepEqual(await once(lines, 'line'), ['ready'])
  assert(held.pid)
  const pid = held.pid
  const observe: typeof nativeResources = async roots => {
    observations += 1
    deadline ??= performance.now() + 5000
    try {
      const result = await nativeResources(roots)
      if (scenario === 'late-snapshot') {
        await new Promise<void>(resolve => { scheduleClose(deadline ?? 0, resolve) })
        while (performance.now() < deadline) await setImmediate()
      }
      acquired = result
      return result
    } catch (error) {
      assert(error instanceof Error && 'code' in error && (error.code === 'EACCES' || error.code === 'EPERM'))
      if (firstDenial === undefined) {
        firstDenial = error
        if (scenario === 'temporary-denial') {
          const restored = once(lines, 'line')
          held.stdin.write('r')
          assert.deepEqual(await restored, ['readable'])
          assert.equal(held.exitCode, null)
          assert.equal(held.signalCode, null)
        }
        if (scenario === 'root-exit') {
          const exited = once(held, 'exit')
          held.kill('SIGTERM')
          await exited
        }
      }
      throw error
    }
  }
  const owner = new Ownership(undefined, {
    server: held,
    observe,
    schedule: (target, callback) => {
      targets.push(target)
      return scheduleClose(target, callback)
    },
  })
  const started = performance.now()

  const cleanup = await owner.close()

  assert.strictEqual(await owner.close(), cleanup)
  assert.equal(cleanup.remaining.processes, 0)
  assert.equal(cleanup.proxySockets, 0)
  assert.equal(held.exitCode === null && held.signalCode === null, false)
  if (scenario !== 'root-exit') assert(targets.length > 0)
  assert(targets.every(target => target === targets[0]))
  if (scenario !== 'root-exit') {
    assert(targets[0] !== undefined && targets[0] >= started + 5000)
    assert(deadline !== undefined && targets[0] <= deadline)
  }
  if (scenario === 'readable' || scenario === 'temporary-denial') {
    checkCleanup(cleanup)
    assert(acquired && acquired.pids.includes(pid))
    assert.equal(acquired.processes, 1)
    if (process.platform === 'linux') assert(acquired.descriptors !== null && acquired.descriptors >= 3)
    if (scenario === 'temporary-denial') {
      assert(firstDenial instanceof Error)
      assert(observations > 1)
    }
  } else {
    assert.equal(cleanup.failures.length, 1)
    assert.equal(cleanup.failures[0]?.stage, 'sample')
    if (scenario === 'late-snapshot') {
      assert.equal(cleanup.failures[0].message, 'Ownership admission deadline expired')
      assert(cleanup.forced)
      assert(acquired?.pids.includes(pid))
    } else {
      assert(firstDenial instanceof Error && 'code' in firstDenial)
      assert.equal(cleanup.failures[0].code, firstDenial.code)
      assert.equal(cleanup.failures[0].message, firstDenial.message.slice(0, 512))
      if (scenario === 'persistent-denial') assert(cleanup.forced)
      if (scenario === 'root-exit') assert.equal(observations, 1)
    }
  }
  await closed
} finally {
  lines.close()
  if (held.exitCode === null && held.signalCode === null) {
    held.kill('SIGKILL')
  }
  await closed
}
process.stdout.write(`${scenario}\n`)
