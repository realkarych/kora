import assert from 'node:assert/strict'
import { once } from 'node:events'
import { inspectArtifacts } from './diagnostic-inspection.ts'

const root = process.argv[2]
assert(root)
const cancellation = new AbortController()
const stop = (): void => { cancellation.abort() }
process.once('SIGTERM', stop)
process.channel?.ref()
const inspected = async (): Promise<void> => {
  const stopped = once(process, 'SIGTERM')
  process.send?.({ event: 'inspecting' })
  await stopped
}

try {
  await assert.rejects(inspectArtifacts(root, 'excluded-marker', cancellation.signal, inspected), { name: 'AbortError' })
  assert(cancellation.signal.aborted)
} finally {
  process.removeListener('SIGTERM', stop)
  process.disconnect()
}
