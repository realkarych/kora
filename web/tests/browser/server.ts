import assert from 'node:assert/strict'
import { createServer, preview } from 'vite'

const mode = process.argv[2]
const root = process.argv[3]
const port = Number(process.argv[4] ?? '0')
assert(mode === 'development' || mode === 'production')
assert(root)
const options = { host: '127.0.0.1', port, strictPort: true }
const server = mode === 'development'
  ? await createServer({ root, server: options, logLevel: 'error' })
  : await preview({ root, preview: options, logLevel: 'error' })
if ('listen' in server) await server.listen()
const address = server.httpServer?.address()
assert(address && typeof address !== 'string')
assert(process.connected)
process.send?.({ event: 'ready', origin: `http://127.0.0.1:${String(address.port)}` })
const stop = (): void => { void server.close().then(() => { process.disconnect() }) }
process.once('message', message => {
  assert(message !== null && typeof message === 'object' && 'event' in message && message.event === 'stop')
  stop()
})
process.once('SIGTERM', stop)
process.once('SIGINT', stop)
process.once('disconnect', () => { void server.close() })
