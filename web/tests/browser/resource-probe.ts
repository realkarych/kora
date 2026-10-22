import assert from 'node:assert/strict'
import { nativeResources, verifyReleased } from './resources.ts'

function permissionDenied(error: unknown): boolean {
  return error instanceof Error && 'code' in error && (error.code === 'EACCES' || error.code === 'EPERM')
}

const [scenario, rawPid] = process.argv.slice(2)
const pid = Number(rawPid)
assert(Number.isSafeInteger(pid) && pid > 0)
assert(scenario === 'released' || scenario === 'persistent-live' || scenario === 'temporary-denial' || scenario === 'persistent-denial')

if (scenario === 'released' || scenario === 'persistent-live') {
  const acquired = await nativeResources([pid])
  assert.deepEqual(acquired.pids, [pid])
  assert.equal(acquired.processes, 1)

  if (scenario === 'released') {
    process.kill(pid, 'SIGTERM')
    const released = await verifyReleased([pid])

    assert.deepEqual(released.pids, [])
    assert.equal(released.processes, 0)
    await assert.rejects(verifyReleased([0]), assert.AssertionError)
  } else {
    const started = performance.now()

    await assert.rejects(verifyReleased([pid]), { message: 'Owned descendants remain alive' })

    assert(performance.now() - started >= 5000, 'A live process must fail at the existing release deadline')
    process.kill(pid, 0)
  }
} else {
  await assert.rejects(nativeResources([pid]), permissionDenied, 'The real process must deny resource observation')
  if (scenario === 'temporary-denial') {
    let denials = 0
    const observe: typeof nativeResources = async roots => {
      try { return await nativeResources(roots) }
      catch (error) {
        if (permissionDenied(error)) {
          denials += 1
          if (denials === 1) process.kill(pid, 'SIGTERM')
        }
        throw error
      }
    }

    const released = await verifyReleased([pid], observe)

    assert(denials > 0, 'The release waiter must observe denial before the process exits')
    assert.deepEqual(released, { pids: [], rss: 0, descriptors: 0, processes: 0 })
  } else {
    const started = performance.now()

    await assert.rejects(verifyReleased([pid]), permissionDenied)

    assert(performance.now() - started >= 5000, 'Persistent denial must fail at the existing release deadline')
    process.kill(pid, 0)
    await assert.rejects(nativeResources([pid]), permissionDenied)
  }
}
process.stdout.write(`${scenario}\n`)
