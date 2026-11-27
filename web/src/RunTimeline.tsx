import { useCallback, useLayoutEffect, useRef, useState } from 'react'
import type { ListRunEventsResponse, RunEvent } from './api/generated/run-events'
import { readBrowserRunEvents } from './api/runEvents'

type Scope = { namespace: string; run: string }
type HistoryIssue = 'signed-out' | 'denied' | 'missing' | 'invalid-response' | 'unavailable'
type View = { kind: 'loading'; page?: ListRunEventsResponse; label?: string; reload?: boolean }
  | { kind: 'ready'; page: ListRunEventsResponse } | { kind: HistoryIssue }
type Session = 'active' | 'signing-out' | 'signed-out' | 'signout-failed'

const messages = {
  'signed-out': 'Sign in to view this run’s events.',
  denied: 'You don’t have access to this run’s events.',
  missing: 'Run not found.',
  'invalid-response': 'The server returned an invalid event page.',
  unavailable: 'Run events are unavailable.',
  'signing-out': 'Signing out…',
  'signout-failed': 'Could not confirm sign-out.',
}

function TimelineStatus({ kind, run, namespace }: Scope & { kind: keyof typeof messages }) {
  return <div role="status">
    <p>{messages[kind]}</p>
    {kind === 'signed-out' && <code>kora open {run} --namespace {namespace}</code>}
  </div>
}

function displayText(value: string): string {
  return Array.from(value, character => /[\p{Cc}\p{Bidi_Control}\p{Zl}\p{Zp}]/u.test(character)
    ? '\\u' + character.charCodeAt(0).toString(16).padStart(4, '0') : character).join('')
}

function EventRow({ event }: { event: RunEvent }) {
  const { actor } = event
  return <li>
    <time dateTime={event.at}>{event.at}</time>
    <div>
      <strong><bdi>{displayText(event.kind)}</bdi></strong>
      <code>{event.id}</code>
      {event.node_instance_id !== null && <code>{event.node_instance_id}</code>}
      {event.attempt_id !== null && <code>{event.attempt_id}</code>}
    </div>
    <div>{actor.kind === 'system'
      ? <span>{actor.component} (system)</span>
      : <><bdi>{actor.owner}</bdi> ({actor.kind})<code>{actor.token_id}</code></>}</div>
  </li>
}

function History({ namespace, run, signOut }: Scope & { signOut: () => void }) {
  const [view, setView] = useState<View>({ kind: 'loading' })
  const owned = useRef<AbortController | null>(null)
  const session = useRef<{ marker: string | undefined; cursor: string | null }>({ marker: undefined, cursor: null })
  const earlier = useRef<HTMLButtonElement>(null)
  const latest = useRef<HTMLButtonElement>(null)

  const load = useCallback(async (cursor: string | null) => {
    if (owned.current !== null) return
    const admitted = csrfToken()
    const sameSession = admitted === session.current.marker
    session.current = { marker: admitted, cursor: sameSession ? cursor : null }
    if (admitted === undefined) { setView({ kind: 'signed-out' }); return }
    const request = new AbortController()
    owned.current = request
    setView(previous => ({ kind: 'loading', page: sameSession && 'page' in previous ? previous.page : undefined,
      reload: previous.kind !== 'loading' }))
    const timer = window.setTimeout(() => {
      if (owned.current === request) setView(view => view.kind === 'loading' ? { ...view, label: 'Loading events…' } : view)
    }, 1000)
    request.signal.addEventListener('abort', () => { window.clearTimeout(timer) }, { once: true })
    const result = await readBrowserRunEvents({ namespace, run, cursor: session.current.cursor, limit: 20, signal: request.signal })
    window.clearTimeout(timer)
    if (request.signal.aborted || owned.current !== request) return
    owned.current = null
    if (admitted !== csrfToken()) { setView({ kind: 'signed-out' }); return }

    if (result.kind === 'page') {
      if (result.page.next_cursor === null && document.activeElement === earlier.current) latest.current?.focus()
      setView({ kind: 'ready', page: result.page })
    } else {
      const code = result.kind === 'api-error' ? result.error.error.code : null
      if (document.activeElement === earlier.current) latest.current?.focus()
      if (code === 'API009') setView({ kind: 'signed-out' })
      else if (code === 'API005') setView({ kind: 'denied' })
      else if (code === 'API001') setView({ kind: 'missing' })
      else if (result.kind === 'client-error' && result.code === 'CLT003') setView({ kind: 'invalid-response' })
      else setView({ kind: 'unavailable' })
    }
  }, [namespace, run])

  useLayoutEffect(() => {
    let current = true
    const abort = () => { owned.current?.abort(); owned.current = null }
    const focus = () => {
      if (session.current.marker !== csrfToken()) abort()
      void load(session.current.cursor)
    }
    queueMicrotask(() => { if (current) void load(null) })
    window.addEventListener('focus', focus)
    return () => { current = false; abort(); window.removeEventListener('focus', focus) }
  }, [load])

  const busy = view.kind === 'loading'
  const page = 'page' in view ? view.page : undefined
  const cursor = page?.next_cursor
  const retry = view.kind === 'unavailable' || view.kind === 'invalid-response' || (busy && view.reload)

  return <>
    {view.kind !== 'signed-out' && (page !== undefined || !busy || retry) && <div className="timeline-actions">
      {(page !== undefined || retry) && <button type="button" ref={latest} aria-disabled={busy}
        onClick={() => { if (!busy) void load(null) }}>Reload latest</button>}
      <button type="button" onClick={signOut}>Sign out</button>
    </div>}
    {busy && page !== undefined && view.label && <p role="status">{view.label}</p>}
    {page !== undefined ? <>
      <p className="timeline-observed">Newest first. Observed <time dateTime={page.observed_at}>{page.observed_at}</time></p>
      {page.items.length === 0
        ? <p role="status">No events yet.</p>
        : <ol aria-busy={busy} role="list">{page.items.map(event => <EventRow key={event.id} event={event} />)}</ol>}
      {cursor != null && <button type="button" ref={earlier} aria-disabled={busy}
        onClick={() => { if (!busy) void load(cursor) }}>Earlier events</button>}
    </> : view.kind === 'loading' || view.kind === 'ready'
      ? <p className="timeline-loading" role="status">{view.kind === 'loading' && view.label}</p>
      : <TimelineStatus kind={view.kind} namespace={namespace} run={run} />}
  </>
}

function csrfToken(): string | undefined {
  const tokens = document.cookie.split(';').map(cookie => cookie.trim())
    .filter(cookie => cookie.startsWith('__Host-kora_csrf='))
  const token = tokens[0]?.slice('__Host-kora_csrf='.length)
  return tokens.length === 1 && token?.length === 64 ? token : undefined
}

async function endSession(controller: AbortController): Promise<boolean> {
  const token = csrfToken()
  if (token === undefined) return false
  const timer = window.setTimeout(() => { controller.abort() }, 15_000)
  try {
    const response = await window.fetch('/api/v1/auth/session', {
      method: 'DELETE',
      headers: { 'X-CSRF-Token': token },
      credentials: 'same-origin',
      mode: 'same-origin',
      redirect: 'error',
      cache: 'no-store',
      signal: controller.signal,
    })
    await response.body?.cancel()
    return response.status === 204 || response.status === 401
  } catch {
    return false
  } finally {
    window.clearTimeout(timer)
  }
}

export default function RunTimeline({ namespace, run, navigation }: Scope & { navigation: number }) {
  const [session, setSession] = useState<Session>('active')
  const logout = useRef<AbortController | null>(null)
  useLayoutEffect(() => {
    document.title = `Run history · ${namespace} · kora`
    return () => { document.title = 'kora'; logout.current?.abort(); logout.current = null }
  }, [namespace])

  async function signOut() {
    if (logout.current !== null) return
    const controller = new AbortController()
    logout.current = controller
    setSession('signing-out')
    const ended = await endSession(controller)
    if (logout.current !== controller) return
    logout.current = null
    setSession(ended ? 'signed-out' : 'signout-failed')
  }

  return <main className="run-timeline">
    <h1>Run history</h1>
    <dl className="timeline-scope">
      <div><dt>Namespace</dt><dd><code>{namespace}</code></dd></div>
      <div><dt>Run</dt><dd><code>{run}</code></dd></div>
    </dl>
    {session === 'active'
      ? <History key={navigation} namespace={namespace} run={run} signOut={() => { void signOut() }} />
      : <TimelineStatus kind={session} namespace={namespace} run={run} />}
    {session === 'signout-failed' && <button type="button" onClick={() => { void signOut() }}>Sign out</button>}
  </main>
}
