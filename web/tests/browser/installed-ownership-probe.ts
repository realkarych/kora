import assert from 'node:assert/strict'
import { once } from 'node:events'
import { Ownership } from './ownership.ts'

const mode = process.argv[2]
assert(mode === 'overflow' || mode === 'forced')
const owner = new Ownership()
owner.origin = 'https://127.0.0.1:1'
try {
  await owner.startBrowser()
  const pid = owner.browserServer?.process().pid
  assert(pid !== undefined)
  const input = once(process.stdin, 'data')
  process.stdout.write(`${String(pid)}\n`)
  await input
  if (mode === 'overflow') process.stderr.write(Buffer.alloc(65537, 120))
  await new Promise<void>(resolve => { process.stdin.once('end', resolve) })
} finally {
  await owner.close()
}
