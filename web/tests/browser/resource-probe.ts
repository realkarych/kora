import assert from 'node:assert/strict'
import { readSync, writeSync } from 'node:fs'
import { admitResources, nativeResources, verifyReleased } from './resources.ts'

function permissionDenied(error: unknown): boolean {
  return error instanceof Error && 'code' in error && (error.code === 'EACCES' || error.code === 'EPERM')
}

const [scenario, rawPid, writeDescriptor, readDescriptor] = process.argv.slice(2)
const pid = Number(rawPid)
assert(scenario)
assert(Number.isSafeInteger(pid) && pid > 0)
assert(['released', 'persistent-live', 'temporary-denial', 'persistent-denial', 'temporary-admission', 'persistent-admission'].includes(scenario))

if (scenario.endsWith('admission')) {
  let firstDenial: unknown
  let accepted = false
  const observe: typeof nativeResources = async roots => {
    try { return await nativeResources(roots) }
    catch (error) {
      assert(permissionDenied(error))
      if (firstDenial === undefined) {
        firstDenial = error
        if (scenario === 'temporary-admission') {
          assert.equal(writeSync(Number(writeDescriptor), 'r'), 1)
          const response = Buffer.alloc(9)
          let received = 0
          while (received < response.length) {
            const count = readSync(Number(readDescriptor), response, received, response.length - received, null)
            assert(count > 0)
            received += count
          }
          assert.equal(response.toString(), 'readable\n')
          process.kill(pid, 0)
        }
      }
      throw error
    }
  }
  const admission = admitResources([pid], performance.now() + 5000, () => { process.kill(pid, 0) }, resources => {
    assert.deepEqual(resources.pids, [pid])
    assert.equal(resources.processes, 1)
    assert(resources.descriptors !== null && resources.descriptors >= 3)
    accepted = true
  }, observe)

  if (scenario === 'temporary-admission') {
    await admission

    assert(accepted)
    assert(firstDenial instanceof Error)
    process.kill(pid, 0)
    process.kill(pid, 'SIGTERM')
  } else {
    await assert.rejects(admission, error => error === firstDenial && permissionDenied(error))

    assert(!accepted)
    process.kill(pid, 0)
  }
} else if (scenario === 'released' || scenario === 'persistent-live') {
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
