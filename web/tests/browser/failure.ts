import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { once } from 'node:events'
import { writeFile } from 'node:fs/promises'
import { checkCleanup, Ownership } from './ownership.ts'
import { contextFor, Diagnostics, ready } from './page.ts'

const scenario = process.argv[2]
const root = process.argv[3]
const output = process.argv[4]
assert(root && output)
assert(['startup', 'navigation', 'assertion', 'deadline', 'cancellation'].includes(scenario ?? ''))
const owner = new Ownership()
const blocker = createServer()
let expected = false
let failure: string | undefined
try {
  if (scenario === 'startup') {
    blocker.listen(0, '127.0.0.1')
    await once(blocker, 'listening')
    const address = blocker.address()
    assert(address && typeof address !== 'string')
    await owner.startServer('production', root, address.port)
  } else {
    const origin = await owner.startServer('production', root)
    const browser = await owner.startBrowser()
    const diagnostics = new Diagnostics()
    const context = await contextFor(browser, origin, diagnostics)
    const page = await context.newPage()
    await page.goto(origin)
    await ready(page)
    if (scenario === 'assertion') assert.fail('Expected assertion path')
    if (scenario === 'deadline') await page.getByText('Absent deadline target', { exact: true }).waitFor({ timeout: 1 })
    if (scenario === 'navigation') {
      assert(owner.server)
      const stopped = once(owner.server, 'exit')
      owner.server.send({ event: 'stop' })
      await stopped
      await page.goto(origin)
    }
    if (scenario === 'cancellation') {
      const cancelled = once(process, 'SIGTERM')
      process.send?.({ event: 'ready', pids: [...owner.pids] })
      await cancelled
      throw new Error('Expected cancellation path')
    }
  }
} catch (error) {
  assert(error instanceof Error)
  failure = error.message.slice(0, 1024)
  const patterns: Record<string, RegExp> = {
    startup: /Server exited before readiness/u,
    navigation: /net::ERR_EMPTY_RESPONSE/u,
    assertion: /Expected assertion path/u,
    deadline: /Timeout 1ms exceeded/u,
    cancellation: /Expected cancellation path/u,
  }
  const pattern = scenario === undefined ? undefined : patterns[scenario]
  expected = pattern !== undefined && pattern.test(error.message)
} finally {
  const cleanup = await owner.close()
  if (blocker.listening) await new Promise<void>((resolve, reject) => { blocker.close(error => { if (error) reject(error); else resolve() }) })
  await writeFile(output, JSON.stringify({ scenario, expected, failure, cleanup, logs: owner.logs }))
  checkCleanup(cleanup)
}
assert(expected, 'Failure did not match its intended scenario')
if (process.connected) process.disconnect()
