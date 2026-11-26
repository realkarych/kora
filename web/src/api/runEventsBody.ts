const bodyLimit = 1024 * 1024
const maximumJsonDepth = 64
const utf8 = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true })

const failureCodes = {
  destination_policy: 'CLT001',
  connection: 'CLT001',
  deadline: 'CLT002',
  response_limit: 'CLT003',
  status_envelope: 'CLT003',
  json: 'CLT003',
  schema: 'CLT003',
  stream: 'CLT004',
  cancelled: null,
} as const

type Failure = keyof typeof failureCodes

export class RunEventsFailure extends Error {
  readonly reason: Failure
  readonly code: (typeof failureCodes)[Failure]

  constructor(reason: Failure) {
    super(reason)
    this.reason = reason
    this.code = failureCodes[reason]
  }
}

function stringEnd(text: string, start: number): number {
  for (let index = start + 1; index < text.length; index++) {
    if (text[index] === '"') return index
    if (text[index] === '\\') index++
  }
  return -1
}

function propertyFollows(text: string, index: number): boolean {
  for (let offset = index + 1; offset < text.length; offset++) {
    const character = text[offset]
    if (character === ' ' || character === '\t' || character === '\n' || character === '\r') continue
    return character === ':'
  }
  return false
}

function boundedJsonStructure(text: string): boolean {
  if (text.charCodeAt(0) === 0xfeff) return false

  const containers: (Set<string> | null)[] = []
  for (let index = 0; index < text.length; index++) {
    const character = text[index]
    if (character === '{' || character === '[') {
      if (containers.length >= maximumJsonDepth) return false
      containers.push(character === '{' ? new Set() : null)
    } else if (character === '}' || character === ']') {
      const container = containers.pop()
      if (container === undefined || (character === ']') !== (container === null)) return false
    } else if (character === '"') {
      const start = index
      index = stringEnd(text, start)
      if (index === -1) return false
      const properties = containers.at(-1)
      if (properties && propertyFollows(text, index)) {
        const key: unknown = JSON.parse(text.slice(start, index + 1))
        if (typeof key !== 'string' || properties.has(key)) return false
        properties.add(key)
      }
    }
  }
  return containers.length === 0
}

function admittedJsonText(body: Uint8Array<ArrayBuffer>): string | null {
  try {
    const text = utf8.decode(body)
    return boundedJsonStructure(text) ? text : null
  } catch (error) {
    if (error instanceof SyntaxError || error instanceof TypeError) return null
    throw error
  }
}

type DetachedBody = { response: Response; errorText: string | null }

export async function detachRunEventsBody(response: Response, check: () => void): Promise<DetachedBody> {
  const reader = response.body?.getReader()
  if (reader === undefined) throw new RunEventsFailure('status_envelope')

  let complete = false
  try {
    check()
    const { status } = response
    const contentType = response.headers.get('content-type')
    if ((status !== 200 && (status < 400 || status >= 600))
      || contentType === null || !/^application\/json(?:\s*;|$)/iu.test(contentType)) {
      throw new RunEventsFailure('status_envelope')
    }

    const bytes = new Uint8Array(bodyLimit)
    let used = 0
    for (;;) {
      const chunk = await reader.read().catch(() => { throw new RunEventsFailure('stream') })
      check()
      if (chunk.done) {
        complete = true
        break
      }
      if (chunk.value.byteLength > bodyLimit - used) throw new RunEventsFailure('response_limit')
      bytes.set(chunk.value, used)
      used += chunk.value.byteLength
    }
    if (used === 0) throw new RunEventsFailure('status_envelope')

    const body = bytes.subarray(0, used)
    const text = admittedJsonText(body)
    if (text === null) {
      throw new RunEventsFailure(status === 200 ? 'json' : 'status_envelope')
    }
    check()

    return {
      response: new Response(body, { status, headers: { 'Content-Type': 'application/json' } }),
      errorText: status === 200 ? null : text,
    }
  } finally {
    try {
      if (!complete) await reader.cancel().catch(() => undefined)
    } finally {
      reader.releaseLock()
    }
  }
}
