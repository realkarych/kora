import assert from 'node:assert/strict'
import { once } from 'node:events'
import { createServer } from 'node:http'
import type { RequestListener, ServerResponse } from 'node:http'
import type { AddressInfo, Socket } from 'node:net'
import { gzipSync } from 'node:zlib'
import { readRunEvents } from '../../src/api/runEvents.ts'

type Identity = Readonly<{
  origin: string
  authorization: string
  namespace: string
  run: string
}>

const wireAuthorization = 'Bearer private-run-events-wire-credential'
const wireContent = 'private-run-events-wire-content'
const requestId = 'req_wire_header'
const errorId = 'req_wire_body'
const observedAt = '2026-11-26T00:00:00.000000Z'
const byteLimit = 1024 * 1024
const wireSelectors = {
  authorization: wireAuthorization,
  namespace: 'run_events',
  run: 'run_01JCE8H63M86YBS2P0NN4R3JDH',
}
const route = '/api/v1/namespaces/' + wireSelectors.namespace + '/runs/' + wireSelectors.run + '/events'

const systemEvent = {
  id: 'event_01JCE8H63M86YBS2P0NN4R3JDH',
  kind: 'run.succeeded',
  at: observedAt,
  actor: { kind: 'system', component: 'scheduler' },
  run_id: wireSelectors.run,
  node_instance_id: null,
  attempt_id: null,
}
const page = JSON.stringify({ items: [], observed_at: observedAt, next_cursor: null })
const errorBody = {
  error: { request_id: errorId, code: 'API901', message: wireContent, location: null, retryable: false, details: {} },
}
const standardError = { error: { ...errorBody.error, code: 'API009', message: 'Authentication is required.' } }
const unsafeIdentityError = {
  error: { ...errorBody.error, request_id: 'req_' + wireAuthorization.slice('Bearer '.length) },
}
let failureStage = 'input'

async function privateInput(): Promise<Record<string, unknown>> {
  let size = 0
  const chunks: Buffer[] = []
  for await (const chunk of process.stdin as AsyncIterable<Buffer>) {
    size += chunk.byteLength
    assert(size <= 16_384)
    chunks.push(chunk)
  }
  const data: unknown = JSON.parse(Buffer.concat(chunks).toString('utf8'))
  assert(data !== null && typeof data === 'object' && !Array.isArray(data))
  return data as Record<string, unknown>
}

function stringField(input: Record<string, unknown>, key: string): string {
  const value = input[key]
  assert(typeof value === 'string')
  return value
}

async function installed(input: Record<string, unknown>): Promise<void> {
  const options = {
    origin: stringField(input, 'origin'),
    namespace: stringField(input, 'namespace'),
    run: stringField(input, 'run'),
    limit: 1,
    fetch: globalThis.fetch,
  }
  const authorization = stringField(input, 'authorization')
  const result = await readRunEvents({ ...options, authorization })
  failureStage = 'installed-first-' + result.kind
  const expectation = input.expected

  if (typeof expectation === 'string') {
    assert(result.kind === 'api-error')
    assert.equal(result.error.error.code, expectation)
    assert(result.requestId !== null)
    assert.equal(result.error.error.request_id, result.requestId)
    return
  }

  assert.equal(result.kind, 'page')
  assert(result.page.next_cursor !== null && result.requestId !== null)
  const next = await readRunEvents({ ...options, authorization, cursor: result.page.next_cursor })
  failureStage = 'installed-continuation-' + next.kind
  assert.equal(next.kind, 'page')
  assert(next.requestId !== null && next.requestId !== result.requestId)
  assert.equal(next.page.next_cursor, null)
  assert.deepEqual([result.page.items[0]?.id, next.page.items[0]?.id], expectation)
}

async function withListener(
  handle: RequestListener,
  exercise: (origin: string) => Promise<void>,
): Promise<void> {
  const sockets = new Set<Socket>()
  const server = createServer(handle)
  server.on('connection', (socket) => {
    sockets.add(socket)
    socket.once('close', () => sockets.delete(socket))
  })
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  const port = (server.address() as AddressInfo).port
  try {
    await exercise('http://127.0.0.1:' + port.toString())
  } finally {
    const close = once(server, 'close')
    server.close()
    for (const socket of sockets) socket.destroy()
    await close
  }
}

function send(response: ServerResponse, bytes: string | Buffer, status = 200): void {
  response.writeHead(status, { 'Content-Type': 'application/json', 'X-Request-ID': requestId })
  response.end(bytes)
}

function wireOptions(origin: string): Identity & { fetch: typeof globalThis.fetch } {
  return { ...wireSelectors, origin, fetch: globalThis.fetch }
}

function regexInputContains(marker: string): boolean {
  const input: unknown = Reflect.get(RegExp, 'input')
  return typeof input !== 'string' || input.includes(marker)
}

function captureNativeBody() {
  const bodies: ReadableStream<Uint8Array>[] = []
  const fetch: typeof globalThis.fetch = async (input, init) => {
    const response = await globalThis.fetch(input, init)
    assert(response.body !== null)
    bodies.push(response.body)
    return response
  }
  return { bodies, fetch }
}

function timeoutCount(): number {
  return process.getActiveResourcesInfo().filter((type) => type === 'Timeout').length
}

async function releaseCallTimer(): Promise<void> {
  const caller = new AbortController()
  let dispatched = false
  const fetch: typeof globalThis.fetch = (input, init) => {
    dispatched = true
    return globalThis.fetch(input, init)
  }
  const before = timeoutCount()

  const pending = readRunEvents({ ...wireOptions('http://127.0.0.1:1'), fetch, signal: caller.signal })
  const during = timeoutCount()
  caller.abort()
  const outcome = await pending

  assert.equal(during, before + 1)
  assert.equal(outcome.kind, 'cancelled')
  assert.equal(dispatched, false)
  assert.equal(timeoutCount(), before)
}

async function destinations(): Promise<void> {
  const seen: (string | undefined)[] = []
  const authorization = 'bEaReR  ' + wireAuthorization.slice('Bearer '.length) + '=='
  let expectedAuthorizationSent = false
  await withListener((request, response) => {
    seen.push(request.url)
    expectedAuthorizationSent = request.headers.authorization === authorization
    response.writeHead(302, { location: '/other' }).end()
  }, async (origin) => {
    const options = wireOptions(origin)
    const wrong = await readRunEvents({ ...options, origin: origin.replace('127.0.0.1', '127.1') })
    const retained = regexInputContains(wireAuthorization)
    const malformed = await readRunEvents({ ...options, authorization: authorization + 'a' })
    const redirected = await readRunEvents({ ...options, authorization, limit: 1, cursor: 'YXNkZg' })

    assert(wrong.kind === 'client-error' && wrong.reason === 'destination_policy')
    assert(!retained, 'Destination refusal retained authorization.')
    assert(malformed.kind === 'client-error' && malformed.reason === 'destination_policy')
    assert(redirected.kind === 'client-error')
    assert.deepEqual(seen, [route + '?limit=1&cursor=YXNkZg'])
    assert(expectedAuthorizationSent)
  })
}

function errorWithDetails(detailsText: string): string {
  return JSON.stringify(errorBody).replace('"details":{}', '"details":' + detailsText)
}

async function validPageAndErrors(): Promise<void> {
  const requests: boolean[] = []
  const largeErrorText = errorWithDetails('{ "count": 9007199254740993 }')
  await withListener((request, response) => {
    requests.push(request.method === 'GET' && request.headers.authorization === wireAuthorization)
    switch (request.url) {
      case route + '?limit=1':
        send(response, page.padEnd(byteLimit, ' '))
        break
      case route + '?limit=2':
        send(response, JSON.stringify(errorBody), 409)
        break
      case route + '?limit=3':
        send(response, JSON.stringify(standardError), 409)
        break
      case route + '?limit=4':
        response.writeHead(401, { 'Content-Type': 'application/json', 'X-Request-ID': 'invalid header identity' })
        response.end(JSON.stringify(standardError))
        break
      case route + '?limit=5':
        send(response, JSON.stringify(unsafeIdentityError), 409)
        break
      default:
        send(response, largeErrorText, 409)
    }
  }, async (origin) => {
    const options = wireOptions(origin)
    const observed = captureNativeBody()
    const exact = await readRunEvents({ ...options, limit: 1, fetch: observed.fetch })
    assert.equal(observed.bodies[0]?.locked, false)
    const additive = await readRunEvents({ ...options, limit: 2 })
    const mismatched = await readRunEvents({ ...options, limit: 3 })
    const invalidHeader = await readRunEvents({ ...options, limit: 4 })
    const invalidBodyIdentity = await readRunEvents({ ...options, limit: 5 })
    const large = await readRunEvents({ ...options, limit: 6 })

    assert.deepEqual(requests, [true, true, true, true, true, true])
    assert(exact.kind === 'page')
    assert.equal(exact.requestId, requestId)
    assert(additive.kind === 'api-error')
    assert.equal(additive.requestId, requestId)
    assert.equal(additive.errorRequestId, errorId)
    assert.deepEqual(additive.error, errorBody)
    assert(mismatched.kind === 'client-error' && mismatched.code === 'CLT003')
    assert(!('responseText' in mismatched))
    assert(invalidHeader.kind === 'api-error')
    assert.equal(invalidHeader.requestId, null)
    assert.equal(invalidHeader.errorRequestId, errorId)
    assert.deepEqual(invalidHeader.error, standardError)
    assert(invalidBodyIdentity.kind === 'api-error')
    assert.equal(invalidBodyIdentity.requestId, requestId)
    assert.equal(invalidBodyIdentity.errorRequestId, null)
    assert.deepEqual(invalidBodyIdentity.error, unsafeIdentityError)
    assert(large.kind === 'api-error')
    assert.equal(large.error.error.code, 'API901')
    assert.equal(large.responseText, largeErrorText)
  })
}

async function exactPageShape(): Promise<void> {
  const token = {
    ...systemEvent,
    id: 'event_01JCE8H63M86YBS2P0NN4R3JDK',
    actor: { kind: 'user', owner: 'person', token_id: 'token' },
  }
  const valid = { items: [token, systemEvent], observed_at: observedAt, next_cursor: null }
  const extra = { unexpected: wireContent }
  const pages = [
    { ...valid, ...extra },
    { ...valid, items: [token, { ...systemEvent, ...extra }] },
    { ...valid, items: [token, { ...systemEvent, actor: { ...systemEvent.actor, ...extra } }] },
    { ...valid, items: [{ ...token, actor: { ...token.actor, ...extra } }, systemEvent] },
  ]
  await withListener((request, response) => {
    request.resume()
    const selector = new URL(request.url ?? '/', 'http://127.0.0.1').searchParams.get('limit')
    send(response, JSON.stringify(pages[Number(selector) - 2] ?? valid))
  }, async (origin) => {
    const options = wireOptions(origin)
    const accepted = await readRunEvents({ ...options, limit: 100 })

    assert(accepted.kind === 'page')
    assert.deepEqual(accepted.page, valid)
    for (const index of pages.keys()) {
      const rejected = await readRunEvents({ ...options, limit: index + 2 })
      assert(rejected.kind === 'client-error' && rejected.reason === 'schema')
      assert(!('responseText' in rejected))
    }
  })
}

async function exactJsonNumbers(): Promise<void> {
  const largeInteger = '1' + '0'.repeat(309)
  const beyondIntegerBound = 1n << 14_000n
  const cases = [
    { number: largeInteger, admitted: true },
    { number: (beyondIntegerBound - 1n).toString(), admitted: true },
    { number: beyondIntegerBound.toString(), admitted: false },
    { number: '-1.25e2', admitted: true },
    { number: '1e309', admitted: false },
    { number: largeInteger + '.0', admitted: false },
  ]
  for (const { number, admitted } of cases) {
    const observeRetention = number === '1e309'
    const marker = wireContent + '-overflow'
    const errorText = errorWithDetails('{"count":' + number + '}')
    const body = observeRetention ? errorText.replace(wireContent, marker) : errorText
    await withListener((request, response) => {
      request.resume()
      send(response, body, 409)
    }, async (origin) => {
      const result = await readRunEvents(wireOptions(origin))
      if (observeRetention) {
        assert(!regexInputContains(marker), 'Numeric refusal retained response content.')
      }

      if (admitted) {
        assert(result.kind === 'api-error')
        assert.equal(result.error.error.code, 'API901')
        assert.equal(result.requestId, requestId)
        assert.equal(result.errorRequestId, errorId)
        assert.equal(result.responseText, body)
      } else {
        assert(result.kind === 'client-error' && result.reason === 'status_envelope')
        assert(!('responseText' in result))
      }
    })
  }
}

async function exactJsonUnicode(): Promise<void> {
  const marker = wireContent + '-unicode'
  const cases = [
    { details: { value: marker + '\ud800' }, admitted: false },
    { details: { [marker + '\udc00']: true }, admitted: false },
    { details: { value: marker + '\ud83d\ude00' }, admitted: true },
  ]

  for (const { details, admitted } of cases) {
    const body = errorWithDetails(JSON.stringify(details))
    await withListener((request, response) => {
      request.resume()
      send(response, body, 409)
    }, async (origin) => {
      const result = await readRunEvents(wireOptions(origin))

      assert(!regexInputContains(marker), 'Unicode validation retained response content.')
      if (admitted) {
        assert(result.kind === 'api-error')
        assert.equal(result.responseText, body)
      } else {
        assert(result.kind === 'client-error' && result.reason === 'status_envelope')
        assert(!('responseText' in result))
      }
    })
  }
}

async function invalidFrames(): Promise<void> {
  const duplicate = page.slice(0, -1) + ',"n\\u0065xt_cursor":null}'
  const validEventPage = JSON.stringify({ items: [systemEvent], observed_at: observedAt, next_cursor: null })
  const [prefix, suffix] = validEventPage.split(systemEvent.kind)
  assert(prefix !== undefined && suffix !== undefined)
  const malformed = Buffer.concat([Buffer.from(prefix), Buffer.from([0xc3, 0x28]), Buffer.from(suffix)])
  const frames = [Buffer.from(duplicate), malformed]
  for (const frame of frames) {
    await withListener((request, response) => {
      request.resume()
      send(response, frame)
    }, async (origin) => {
      const result = await readRunEvents(wireOptions(origin))

      assert(result.kind === 'client-error' && result.reason === 'json')
    })
  }

  for (const depth of [64, 65]) {
    const nested = '['.repeat(depth - 3) + '0' + ']'.repeat(depth - 3)
    const body = errorWithDetails('{"nested":' + nested + '}')
    await withListener((request, response) => {
      request.resume()
      send(response, body, 409)
    }, async (origin) => {
      const result = await readRunEvents(wireOptions(origin))

      if (depth === 64) {
        assert(result.kind === 'api-error')
        assert.equal(result.responseText, body)
      } else {
        assert(result.kind === 'client-error' && result.code === 'CLT003')
        assert(!('responseText' in result))
      }
    })
  }
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
  let resolve: (value: T) => void = () => { throw new Error('barrier was not initialized') }
  const promise = new Promise<T>((settle) => {
    resolve = settle
  })
  return { promise, resolve }
}

async function oversized(status: number, zip: boolean): Promise<void> {
  const alreadyOver = Buffer.from(page.padEnd(byteLimit + 1, ' '))
  const body = zip ? gzipSync(alreadyOver) : alreadyOver
  if (zip) assert(body.length < byteLimit)
  const { promise, resolve } = deferred<boolean>()
  await withListener((request, response) => {
    request.resume()
    response.once('close', () => { resolve(response.writableEnded) })
    response.writeHead(status, {
      'Content-Type': 'application/json',
      ...(zip ? { 'Content-Encoding': 'gzip' } : {}),
    })
    response.write(body)
  }, async (origin) => {
    const observed = captureNativeBody()
    const result = await readRunEvents({ ...wireOptions(origin), fetch: observed.fetch })
    const ended = await Promise.race([promise, abortAfter(3_000)])

    assert(result.kind === 'client-error' && result.reason === 'response_limit')
    assert.equal(observed.bodies[0]?.locked, false)
    assert.equal(ended, false)
  })
}

function abortAfter(milliseconds: number): Promise<never> {
  const signal = AbortSignal.timeout(milliseconds)
  return new Promise((_, reject) => {
    signal.addEventListener('abort', () => { reject(new Error('native response stayed open')) }, { once: true })
  })
}

async function withIncompleteResponse(
  initialBody: string,
  exercise: (origin: string) => Promise<void>,
): Promise<void> {
  const closed = deferred<boolean>()
  await withListener((request, response) => {
    request.resume()
    response.once('close', () => { closed.resolve(response.writableEnded) })
    response.writeHead(200, { 'Content-Type': 'application/json', 'X-Request-ID': requestId })
    response.flushHeaders()
    response.write(initialBody)
  }, async (origin) => {
    await exercise(origin)
    const ended = await Promise.race([closed.promise, abortAfter(3_000)])
    assert.equal(ended, false)
  })
}

async function cancelPendingRead(): Promise<void> {
  const started = deferred<undefined>()
  const caller = new AbortController()
  const adaptedBodies: ReadableStream<Uint8Array>[] = []
  let nativeReader: ReadableStreamDefaultReader<Uint8Array> | undefined
  let readPending = false

  try {
    await withIncompleteResponse('', async (origin) => {
      const fetch: typeof globalThis.fetch = async (input, init) => {
        const response = await globalThis.fetch(input, init)
        assert(response.body !== null)
        const reader = response.body.getReader()
        nativeReader = reader
        const body = new ReadableStream<Uint8Array>({
          async pull(controller) {
            const read = reader.read()
            readPending = true
            started.resolve(undefined)
            try {
              const chunk = await read
              if (chunk.done) controller.close()
              else controller.enqueue(chunk.value)
            } finally {
              readPending = false
            }
          },
          cancel() {
            return reader.cancel()
          },
        }, { highWaterMark: 0 })
        adaptedBodies.push(body)
        return new Response(body, { status: response.status, headers: response.headers })
      }
      const result = readRunEvents({ ...wireOptions(origin), fetch, signal: caller.signal })
      await Promise.race([started.promise, abortAfter(3_000)])
      assert(readPending)
      caller.abort()
      const outcome = await result

      assert.equal(outcome.kind, 'cancelled')
      assert.equal(readPending, false)
      assert.equal(adaptedBodies[0]?.locked, false)
    })
  } finally {
    caller.abort()
    nativeReader?.releaseLock()
  }
}

async function rejectLateResponse(): Promise<void> {
  let returnedAfterAbort = false
  await withIncompleteResponse(page, async (origin) => {
    const fetch: typeof globalThis.fetch = async (input, init) => {
      const response = await globalThis.fetch(input, init)
      assert(input instanceof Request)
      if (!input.signal.aborted) await once(input.signal, 'abort')
      returnedAfterAbort = input.signal.aborted
      return response
    }
    const outcome = await readRunEvents({ ...wireOptions(origin), fetch })

    assert(returnedAfterAbort)
    assert(outcome.kind === 'client-error' && outcome.code === 'CLT002')
  })
}

async function wire(): Promise<void> {
  failureStage = 'call-timer'
  await releaseCallTimer()
  failureStage = 'destination'
  await destinations()
  failureStage = 'valid-page-and-errors'
  await validPageAndErrors()
  failureStage = 'exact-page-shape'
  await exactPageShape()
  failureStage = 'json-numbers'
  await exactJsonNumbers()
  failureStage = 'json-unicode'
  await exactJsonUnicode()
  failureStage = 'invalid-frames'
  await invalidFrames()
  failureStage = 'compressed-response-limit'
  await oversized(200, true)
  failureStage = 'error-response-limit'
  await oversized(409, false)
  failureStage = 'caller-cancellation'
  await cancelPendingRead()
  failureStage = 'operation-deadline'
  await rejectLateResponse()
}

async function proxyProbe(input: Record<string, unknown>): Promise<void> {
  const response = await globalThis.fetch(stringField(input, 'origin') + '/healthz', {
    redirect: 'manual',
    signal: AbortSignal.timeout(3_000),
  }).catch(() => null)
  assert(response === null || response.status === 502)
  await response?.body?.cancel()
}

try {
  const input = await privateInput()
  const mode = stringField(input, 'mode')
  if (mode === 'installed') await installed(input)
  else if (mode === 'wire') await wire()
  else if (mode === 'proxy-probe') {
    failureStage = 'proxy-control'
    await proxyProbe(input)
  }
  else throw new Error('unknown run events test mode')
  process.stdout.write('run-events-api-ok\n')
} catch {
  process.stderr.write('run-events-api-failed ' + failureStage + '\n')
  process.exitCode = 1
}
