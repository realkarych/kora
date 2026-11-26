import { listRunEvents } from './generated/run-events'
import type { ApiError, ListRunEventsResponse, RunEvent } from './generated/run-events'
import { detachRunEventsBody, RunEventsFailure } from './runEventsBody'

const deadlineMs = 15_000
const originPattern = /^http:\/\/127\.0\.0\.1:([1-9][0-9]{0,4})$/u
const namespacePattern = /^[a-z][a-z0-9_]{0,63}$/u
const ulidPattern = /^[0-7][0-9A-HJKMNP-TV-Z]{25}$/u
const cursorPattern = /^[A-Za-z0-9_-]{1,8192}$/u
const credentialPattern = /^Bearer +([A-Za-z0-9._~+/-]+=*)$/iu
const requestIdPattern = /^(?:req_[a-zA-Z0-9_-]{1,252}|[0-9a-f]{32})$/u
const serverCodePattern = /^(?:API|IR|DSL|RT|POL)[0-9]{3}$/u
const unpairedSurrogate = /[\uD800-\uDFFF]/u
const pageKeys = ['items', 'observed_at', 'next_cursor'] satisfies (keyof ListRunEventsResponse)[]
const rowKeys = ['id', 'kind', 'at', 'actor', 'run_id', 'node_instance_id', 'attempt_id'] satisfies (keyof RunEvent)[]
const systemActorKeys = ['kind', 'component']
const tokenActorKeys = ['kind', 'owner', 'token_id']

type RunEventsRequest = {
  origin: string
  authorization: string
  namespace: string
  run: string
  limit?: number
  cursor?: string | null
  signal?: AbortSignal
  fetch: typeof fetch
}

type ErrorEnvelope = {
  error: Pick<ApiError, 'message' | 'retryable' | 'request_id'> & {
    code: string
    location: null
    details: Record<string, unknown>
  }
}

type FailureCode = NonNullable<RunEventsFailure['code']>
type FailureReason = Exclude<RunEventsFailure['reason'], 'cancelled'>

type RunEventsResult =
  | { kind: 'page'; page: ListRunEventsResponse; requestId: string | null }
  | {
    kind: 'api-error'
    status: number
    error: ErrorEnvelope
    responseText: string
    requestId: string | null
    errorRequestId: string | null
  }
  | { kind: 'client-error'; code: FailureCode; reason: FailureReason; requestId: string | null }
  | { kind: 'cancelled'; requestId: string | null }

type Destination = { url: URL; token: string }

type ErrorPolicy = { status: number; message?: string; mayRetry?: boolean }

const knownErrors: Partial<Record<string, ErrorPolicy>> = {
  API001: { status: 404 },
  API002: { status: 409, mayRetry: true },
  API003: { status: 422 },
  API004: { status: 409 },
  API005: { status: 403 },
  API006: { status: 409 },
  API007: { status: 401 },
  API008: { status: 409 },
  API009: { status: 401, message: 'Authentication is required.' },
  API010: { status: 500, message: 'Internal operation failure.', mayRetry: true },
  API011: { status: 503, message: 'Required service is unavailable.', mayRetry: true },
  API012: { status: 405, message: 'Request method is not allowed.' },
  API013: { status: 501, message: 'Request method is not implemented.' },
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  return Object.keys(value).length === keys.length && keys.every((key) => Object.hasOwn(value, key))
}

function exactMatch(pattern: RegExp, value: string): boolean {
  return pattern.exec(value)?.[0] === value
}

function validJsonUnicode(value: unknown): boolean {
  if (typeof value === 'string') return !unpairedSurrogate.test(value)
  if (Array.isArray(value)) return value.every(validJsonUnicode)
  if (isRecord(value)) {
    return Object.entries(value).every(([name, item]) => !unpairedSurrogate.test(name) && validJsonUnicode(item))
  }
  return true
}

function boundedText(value: unknown, limit: number): value is string {
  return typeof value === 'string' && value.length > 0 && value.length <= 2 * limit
    && Array.from(value).length <= limit
}

function prefixedId(value: unknown, prefix: string): value is string {
  return typeof value === 'string' && value.length === prefix.length + 26
    && value.startsWith(prefix) && ulidPattern.test(value.slice(prefix.length))
}

function instant(value: unknown): value is string {
  if (typeof value !== 'string' || !exactMatch(/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z$/u, value)
    || value.startsWith('0000')) return false

  const milliseconds = value.slice(0, 23) + 'Z'
  const date = new Date(milliseconds)
  return Number.isFinite(date.getTime()) && date.toISOString() === milliseconds
}

function identifier(value: unknown): value is string {
  return typeof value === 'string' && exactMatch(/^[!-~]{1,256}$/u, value)
}

function eventActor(value: unknown): value is RunEvent['actor'] {
  if (!isRecord(value)) return false
  const { kind, component, owner, token_id } = value
  return (kind === 'system' && component === 'scheduler' && hasExactKeys(value, systemActorKeys))
    || ((kind === 'user' || kind === 'service') && identifier(owner) && identifier(token_id)
      && hasExactKeys(value, tokenActorKeys))
}

function eventRow(value: unknown, run: string): value is RunEvent {
  if (!isRecord(value) || !hasExactKeys(value, rowKeys)) return false
  const { id, kind, at, actor, run_id, node_instance_id, attempt_id } = value
  return prefixedId(id, 'event_') && run_id === run && boundedText(kind, 255) && !kind.includes('\0')
    && instant(at) && eventActor(actor)
    && (node_instance_id === null || prefixedId(node_instance_id, 'node_'))
    && (attempt_id === null || prefixedId(attempt_id, 'att_'))
}

function cursor(value: unknown): value is string {
  return typeof value === 'string' && exactMatch(cursorPattern, value)
}

function eventPage(value: unknown, run: string, limit: number): value is ListRunEventsResponse {
  if (!isRecord(value) || !hasExactKeys(value, pageKeys)) return false
  const { items, observed_at, next_cursor } = value
  if (!Array.isArray(items) || items.length > limit || !instant(observed_at)
    || (next_cursor !== null && !cursor(next_cursor))) return false

  let previous: string | undefined
  for (const item of items as readonly unknown[]) {
    if (!eventRow(item, run) || (previous !== undefined && item.id >= previous)) return false
    previous = item.id
  }
  return true
}

function safeCorrelation(value: string | null, token: string): string | null {
  return value !== null && exactMatch(requestIdPattern, value) && !value.includes(token) ? value : null
}

function errorEnvelope(value: unknown, status: number): value is ErrorEnvelope {
  if (!isRecord(value) || !isRecord(value.error)) return false
  const { code, message, location, retryable, details, request_id } = value.error
  if (typeof code !== 'string' || !exactMatch(serverCodePattern, code) || !boundedText(message, 1024)
    || location !== null || typeof retryable !== 'boolean' || !isRecord(details)
    || !boundedText(request_id, 256)) return false

  const known = knownErrors[code]
  return known === undefined || (known.status === status
    && (known.message === undefined || known.message === message)
    && (!retryable || known.mayRetry === true))
}

function destination(input: RunEventsRequest): Destination {
  const origin = originPattern.exec(input.origin)
  const bearer = credentialPattern.exec(input.authorization)
  const { namespace, run, limit, cursor: position } = input
  const port = origin?.[1]
  const token = bearer?.[1]
  if (origin?.[0] !== input.origin || port === undefined || Number(port) > 65_535
    || bearer?.[0] !== input.authorization || token === undefined || token.length > 4096
    || !exactMatch(namespacePattern, namespace) || !prefixedId(run, 'run_')
    || (limit !== undefined && (!Number.isInteger(limit) || limit < 1 || limit > 100))
    || (position != null && !cursor(position))) throw new RunEventsFailure('destination_policy')

  const url = new URL('/api/v1/namespaces/' + namespace + '/runs/' + run + '/events', input.origin)
  if (limit !== undefined) url.searchParams.set('limit', String(limit))
  if (position != null) url.searchParams.set('cursor', position)
  return { url, token }
}

function localFailure(error: unknown, requestId: string | null): RunEventsResult {
  const failure = error instanceof RunEventsFailure ? error : new RunEventsFailure('schema')
  const { code, reason } = failure
  if (reason === 'cancelled') return { kind: 'cancelled', requestId }
  if (code === null) throw new Error('The run events failure classification was invalid.')
  return { kind: 'client-error', code, reason, requestId }
}

function runBudget(caller: AbortSignal | undefined) {
  const timeout = new AbortController()
  const expires = performance.now() + deadlineMs
  const signal = caller === undefined ? timeout.signal : AbortSignal.any([caller, timeout.signal])
  const timer = globalThis.setTimeout(() => { timeout.abort() }, deadlineMs)
  const check = () => {
    if (caller?.aborted) throw new RunEventsFailure('cancelled')
    if (timeout.signal.aborted || performance.now() >= expires) {
      timeout.abort()
      throw new RunEventsFailure('deadline')
    }
  }
  return { signal, check, release: () => { globalThis.clearTimeout(timer) } }
}

export async function readRunEvents(input: RunEventsRequest): Promise<RunEventsResult> {
  const budget = runBudget(input.signal)
  const exchange: { requestId: string | null; errorText: string | null } = {
    requestId: null,
    errorText: null,
  }
  try {
    budget.check()
    const { url, token } = destination(input)
    const fetchPage: typeof fetch = async (request, init) => {
      if (!(request instanceof Request) || init !== undefined || request.url !== url.href
        || request.method !== 'GET' || request.body !== null || request.redirect !== 'error'
        || request.credentials !== 'omit' || request.headers.get('authorization') !== input.authorization) {
        throw new RunEventsFailure('destination_policy')
      }
      budget.check()
      const requestFetch = input.fetch
      const response = await requestFetch(request).catch(() => { throw new RunEventsFailure('connection') })
      exchange.requestId = safeCorrelation(response.headers.get('x-request-id'), token)
      const detached = await detachRunEventsBody(response, budget.check)
      exchange.errorText = detached.errorText
      return detached.response
    }
    const result = await listRunEvents({
      baseUrl: input.origin,
      path: { ns: input.namespace, run: input.run },
      query: { limit: input.limit, cursor: input.cursor },
      headers: { Accept: 'application/json', Authorization: input.authorization },
      signal: budget.signal,
      redirect: 'error',
      credentials: 'omit',
      cache: 'no-store',
      fetch: fetchPage,
      parseAs: 'json',
      responseStyle: 'fields',
      throwOnError: false,
    })
    budget.check()
    const status = result.response?.status
    const { requestId, errorText } = exchange
    if (status === undefined) return localFailure(result.error, requestId)
    if (status !== 200) {
      if (errorText === null || !errorEnvelope(result.error, status) || !validJsonUnicode(result.error)) {
        throw new RunEventsFailure('status_envelope')
      }
      const errorRequestId = safeCorrelation(result.error.error.request_id, token)
      budget.check()
      return { kind: 'api-error', status, error: result.error, responseText: errorText, requestId, errorRequestId }
    }
    if (result.error !== undefined) throw new RunEventsFailure('json')
    if (!eventPage(result.data, input.run, input.limit ?? 100) || !validJsonUnicode(result.data)) {
      throw new RunEventsFailure('schema')
    }
    budget.check()
    return { kind: 'page', page: result.data, requestId }
  } catch (error) {
    try {
      budget.check()
    } catch (expired) {
      return localFailure(expired, exchange.requestId)
    }
    return localFailure(error, exchange.requestId)
  } finally {
    budget.release()
  }
}
