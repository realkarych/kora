import assert from 'node:assert/strict'
import { once } from 'node:events'
import { createServer } from 'node:http'
import { writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { checkCleanup, Ownership } from './ownership.ts'
import { contextFor, Diagnostics } from './page.ts'

export async function networkChecks(output: string): Promise<void> {
  let destinationRequests = 0
  let workers = 0
  const denied = createServer((_request, response) => { destinationRequests++; response.end('denied destination') })
  denied.on('connect', (_request, socket) => { destinationRequests++; socket.destroy() })
  denied.on('upgrade', (_request, socket) => { destinationRequests++; socket.destroy() })
  denied.listen(0, '127.0.0.1')
  await once(denied, 'listening')
  const deniedAddress = denied.address()
  assert(deniedAddress && typeof deniedAddress !== 'string')
  const deniedOrigin = `http://127.0.0.1:${String(deniedAddress.port)}`
  const allowed = createServer((request, response) => {
    if (request.url === '/redirect') { response.writeHead(302, { location: deniedOrigin }); response.end() }
    else if (request.url === '/worker.js') { workers++; response.setHeader('content-type', 'application/javascript'); response.end('self.addEventListener("install", () => self.skipWaiting())') }
    else { response.setHeader('content-type', 'text/html'); response.end('<!doctype html><html><body>local</body></html>') }
  })
  allowed.listen(0, '127.0.0.1')
  await once(allowed, 'listening')
  const address = allowed.address()
  assert(address && typeof address !== 'string')
  const origin = `http://127.0.0.1:${String(address.port)}`
  const owner = new Ownership()
  owner.origin = origin
  const diagnostics = new Diagnostics()
  const evidence: Record<string, unknown> = {}
  try {
    const browser = await owner.startBrowser()
    const context = await contextFor(browser, origin, diagnostics)
    const page = await context.newPage()
    await page.goto(origin)
    diagnostics.check()
    const direct = await page.evaluate(async target => {
      try { await fetch(target); return 'unexpected success' }
      catch (error) { return error instanceof TypeError ? 'blocked' : 'unexpected error' }
    }, deniedOrigin)
    assert.equal(direct, 'blocked')
    assert(diagnostics.events.some(event => event.kind === 'external_request'))
    const redirected = await page.evaluate(async () => {
      const response = await fetch('/redirect')
      return response.status
    }).catch((error: unknown) => {
      assert(error instanceof Error && error.message.includes('Failed to fetch'))
      return 0
    })
    assert.equal(redirected, 0)
    const websocket = await page.evaluate(target => new Promise<string>(resolve => {
      const socket = new WebSocket(target.replace('http:', 'ws:'))
      socket.onopen = () => { socket.close(); resolve('unexpected success') }
      socket.onerror = () => { resolve('blocked') }
      socket.onclose = () => { resolve('blocked') }
    }), deniedOrigin)
    assert.equal(websocket, 'blocked')
    assert(diagnostics.events.some(event => event.kind === 'external_websocket'))
    const secure = await page.evaluate(async target => {
      try { await fetch(target.replace('http:', 'https:')); return 'unexpected success' }
      catch (error) { return error instanceof TypeError ? 'blocked' : 'unexpected error' }
    }, deniedOrigin)
    assert.equal(secure, 'blocked')
    const proxyContext = await browser.newContext({ serviceWorkers: 'block' })
    try {
      const proxyPage = await proxyContext.newPage()
      await assert.rejects(proxyPage.goto(deniedOrigin.replace('http:', 'https:')), /net::ERR_TUNNEL_CONNECTION_FAILED/u)
    } finally { await proxyContext.close() }
    await page.evaluate(async () => {
      await navigator.serviceWorker.register('/worker.js')
    })
    assert.equal(context.serviceWorkers().length, 0)
    assert.equal(workers, 0)
    assert(diagnostics.events.some(event => event.kind === 'console' && event.detail === 'Service Worker registration blocked by Playwright'))
    assert.equal(destinationRequests, 0)
    assert(owner.proxy.denied > 0)
    assert(!diagnostics.overflow)
    const blocked = (protocol: string): { kind: string; detail: string }[] => [
      { kind: 'external_request', detail: protocol },
      { kind: 'requestfailed', detail: 'net::ERR_BLOCKED_BY_CLIENT.Inspector' },
      { kind: 'console', detail: 'Failed to load resource: net::ERR_BLOCKED_BY_CLIENT.Inspector' },
    ]
    assert.deepEqual(diagnostics.events, [
      ...blocked('http:'),
      { kind: 'console', detail: `Access to fetch at '${deniedOrigin}/' (redirected from '${origin}/redirect') from origin '${origin}' has been blocked by CORS policy: No 'Access-Control-Allow-Origin' header is present on the requested resource.` },
      { kind: 'requestfailed', detail: 'net::ERR_FAILED' },
      { kind: 'console', detail: 'Failed to load resource: net::ERR_FAILED' },
      { kind: 'external_websocket', detail: 'http:' },
      ...blocked('https:'),
      { kind: 'console', detail: 'Service Worker registration blocked by Playwright' },
    ])
    evidence.results = { direct, redirected, websocket, secure, workers, destinationRequests, diagnostics: diagnostics.events, proxyDenied: owner.proxy.denied }
    await context.close()
  } catch (error) {
    evidence.failure = error instanceof Error ? error.message.slice(0, 2048) : 'UnknownError'
    throw error
  } finally {
    const cleanup = await owner.close()
    evidence.cleanup = cleanup
    const closed = [once(allowed, 'close'), once(denied, 'close')]
    allowed.closeAllConnections()
    denied.closeAllConnections()
    allowed.close()
    denied.close()
    await Promise.all(closed)
    await writeFile(join(output, 'network.json'), JSON.stringify(evidence, null, 2))
    checkCleanup(cleanup)
  }
}
