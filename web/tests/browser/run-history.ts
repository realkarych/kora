import assert from 'node:assert/strict'
import { execFile } from 'node:child_process'
import { once } from 'node:events'
import { mkdir, writeFile } from 'node:fs/promises'
import { createServer } from 'node:net'
import { join } from 'node:path'
import { createInterface } from 'node:readline'
import { promisify } from 'node:util'
import type { BrowserContext, Page, Request, Response } from 'playwright'
import { ArtifactLimit } from './artifacts.ts'
import { checkCleanup, childEnvironment, closeOwners, Ownership } from './ownership.ts'

type Document = Record<string, unknown>
type Theme = 'light' | 'dark'
type RouteClass = 'timeline' | 'asset' | 'event' | 'canonical' | 'signIn' | 'signOut'
type SignInStep = 'start' | 'open-page' | 'navigate' | 'redemption' | 'redirect-headers'
  | 'fetch-metadata' | 'document-headers' | 'cookies' | 'cookie-visibility'
  | 'event-response' | 'event-headers' | 'event-body' | 'event-parity' | 'latest-dom'
  | 'clean-page' | 'open-replay' | 'replay-navigation' | 'replay-response' | 'close-replay'
  | 'canonical-cookie' | 'complete'

const input = createInterface({ input: process.stdin, crlfDelay: Infinity })
const lines = input[Symbol.asyncIterator]()

function object(value: unknown): Document {
  assert(value !== null && typeof value === 'object' && !Array.isArray(value))
  return value as Document
}

function string(value: unknown): string {
  assert(typeof value === 'string')
  return value
}

async function receive(): Promise<Document> {
  const next = await lines.next()
  assert(next.done === false && Buffer.byteLength(next.value) <= 65536)
  const message: unknown = JSON.parse(next.value)
  return object(message)
}

function send(message: Document): void {
  process.stdout.write(`${JSON.stringify(message)}\n`)
}

const bootstrap = await receive()
const origin = string(bootstrap.origin)
const output = string(bootstrap.output)
const root = string(bootstrap.root)
const authority = string(bootstrap.authority)
const namespace = string(bootstrap.namespace)
const run = string(bootstrap.run_id)
const empty = string(bootstrap.empty_run)
const denied = string(bootstrap.denied_run)
const earliest = string(bootstrap.earliest)
const timeline = `/console/runs/${namespace}/${run}`
const eventPath = `/console/api/v1/namespaces/${namespace}/runs/${run}/events`
const knownScopes = [
  [namespace, run], [namespace, empty], ['installed_browser_denied', denied],
  [namespace, 'run_00000000000000000000000000'],
] as const
const knownEventPaths = new Set(knownScopes.map(([ns, run]) =>
  `/console/api/v1/namespaces/${ns}/runs/${run}/events`))
const knownTimelinePaths = new Set(knownScopes.map(([ns, run]) =>
  `/console/runs/${ns}/${run}`))
const canonical = object(bootstrap.page)
assert(Array.isArray(canonical.items))
const canonicalRows = (canonical.items as unknown[]).map(object)

const evidence = {
  status: 'failed',
  stage: 'certificate',
  checks: [] as string[],
  diagnostics: { pageError: 0, warning: 0, error: 0, validatedReadTerminations: 0 },
  nativeSignIn: {
    step: 'start' as SignInStep,
    failedStep: null as SignInStep | null,
    eventFinished: false,
    eventFailed: false,
  },
  routes: {} as Partial<Record<RouteClass, number[]>>,
}
const cancellation = new AbortController()
const stop = (): void => { cancellation.abort(); input.close(); void closeOwners() }
process.once('SIGTERM', stop)
process.once('SIGINT', stop)
const limits = new ArtifactLimit(output, stop)

function stage(next: string): void {
  evidence.checks.push(evidence.stage)
  evidence.stage = next
  assert(!cancellation.signal.aborted)
}

function signInStep(step: SignInStep): void {
  if (evidence.stage === 'native-sign-in') evidence.nativeSignIn.step = step
}

async function profile(name: string, trust: boolean): Promise<Ownership> {
  const home = join(root, name)
  const paths = {
    HOME: home,
    XDG_CONFIG_HOME: join(home, '.config'),
    XDG_DATA_HOME: join(home, '.local', 'share'),
    XDG_CACHE_HOME: join(home, '.cache'),
    XDG_RUNTIME_DIR: join(home, 'runtime'),
  }
  for (const path of Object.values(paths)) await mkdir(path, { recursive: true, mode: 0o700 })
  const database = join(paths.XDG_DATA_HOME, 'pki', 'nssdb')
  await mkdir(database, { recursive: true, mode: 0o700 })
  if (trust) {
    const certutil = async (args: string[]): Promise<void> => {
      await promisify(execFile)('/usr/bin/certutil', args, {
        env: { PATH: '/usr/bin:/bin', HOME: home, LANG: 'C.UTF-8' },
        timeout: 5000, maxBuffer: 8192,
      })
    }
    await certutil(['-N', '-d', `sql:${database}`, '--empty-password'])
    await certutil(['-A', '-d', `sql:${database}`, '-n', 'Kora local browser fixture',
      '-t', 'C,,', '-i', authority])
  }
  const owner = new Ownership(undefined, {
    browserEnvironment: { ...childEnvironment(), ...paths, TMPDIR: home },
  })
  owner.origin = origin
  return owner
}

async function checkUntrustedCertificate(): Promise<void> {
  const owner = await profile('untrusted-chromium', false)
  try {
    const browser = await owner.startBrowser()
    const context = await browser.newContext({ serviceWorkers: 'block' })
    try {
      const page = await context.newPage()
      let httpResponses = 0
      page.on('response', () => { httpResponses++ })
      await assert.rejects(page.goto(origin + timeline, { timeout: 10000 }), /ERR_CERT_AUTHORITY_INVALID/u)
      assert.equal(httpResponses, 0)
    } finally {
      await context.close()
    }
  } finally {
    checkCleanup(await owner.close())
  }
}

async function checkSecondAuthority(page: Page, owner: Ownership): Promise<void> {
  let connections = 0
  const listener = createServer(socket => { connections++; socket.destroy() })
  listener.listen(0, '127.0.0.1')
  await once(listener, 'listening')
  try {
    const address = listener.address()
    assert(address && typeof address !== 'string')
    const denied = owner.proxy.denied
    await assert.rejects(page.goto(`https://127.0.0.1:${String(address.port)}/`, {
      timeout: 10000,
    }), /ERR_TUNNEL_CONNECTION_FAILED/u)
    assert.equal(connections, 0)
    assert(owner.proxy.denied > denied)
  } finally {
    const closed = once(listener, 'close')
    listener.close()
    await closed
  }
}

const expectedRefusals = [
  'native-sign-in:signIn:401',
  'native-sign-in:canonical:401',
  'denied-and-missing:event:403',
  'denied-and-missing:event:404',
  'expired-authority:event:401',
  'revoked-authority:event:401',
].sort()
const expectedAborts = ['obsolete-read:event', 'sign-out:event']
const httpRefusals = new Set<string>()
const consoleRefusals = new Set<string>()
const requestAborts = new Set<string>()
const expectedRequests = new WeakMap<Request, string>()
const abortedRequests = new WeakSet<Request>()
const responseStatuses = new WeakMap<Request, number>()
const validatedReads = new WeakSet<Request>()
type DiagnosticContext = { phase: string; route: RouteClass | 'other'; status: number }
const postResponseAborts = new Map<Request, DiagnosticContext>()
const unexpected = new Map<string, DiagnosticContext>()

function rejectDiagnostic(kind: string, route: RouteClass | undefined, status = 0): void {
  unexpected.set(kind, { phase: evidence.stage, route: route ?? 'other', status })
}

function remember(received: Set<string>, receipt: string): void {
  if (received.has(receipt)) rejectDiagnostic('duplicate-diagnostic', undefined)
  received.add(receipt)
}

function routeClass(url: string): RouteClass | undefined {
  if (!url.startsWith(`${origin}/`)) return undefined
  const path = new URL(url).pathname
  if (/^\/api\/v1\/auth\/session\/[0-9a-f]{64}$/u.test(path)) return 'signIn'
  if (path === '/api/v1/auth/session') return 'signOut'
  if (path === eventPath.replace('/console', '')) return 'canonical'
  if (knownEventPaths.has(path)) return 'event'
  if (knownTimelinePaths.has(path)) return 'timeline'
  if (/^\/assets\/[A-Za-z0-9_.-]+\.(?:js|css|woff2)$/u.test(path)) return 'asset'
  return undefined
}

function watchSafeDiagnostics(page: Page): void {
  page.on('pageerror', () => {
    evidence.diagnostics.pageError++
    rejectDiagnostic('page-error', undefined)
  })
  page.on('console', message => {
    const type = message.type()
    if (type !== 'warning' && type !== 'error') return
    evidence.diagnostics[type]++
    const route = routeClass(message.location().url)
    const status = [401, 403, 404].find(value =>
      message.text().startsWith(`Failed to load resource: the server responded with a status of ${String(value)} (`))
    const receipt = `${evidence.stage}:${route ?? 'other'}:${String(status ?? 0)}`
    if (type === 'error' && expectedRefusals.includes(receipt)) remember(consoleRefusals, receipt)
    else rejectDiagnostic('console-diagnostic', route, status)
  })
  page.on('response', response => {
    const route = routeClass(response.url())
    const status = response.status()
    responseStatuses.set(response.request(), status)
    if (route === undefined) {
      rejectDiagnostic('http-route', route, status)
      return
    }
    const statuses = evidence.routes[route] ??= []
    if (statuses.length < 128) statuses.push(status)
    const expected = route === 'signIn' ? 303 : route === 'signOut' ? 204 : 200
    const receipt = `${evidence.stage}:${route}:${String(status)}`
    if (expectedRefusals.includes(receipt)) remember(httpRefusals, receipt)
    else if (status !== expected || route === 'canonical') rejectDiagnostic('http-status', route, status)
  })
  page.on('requestfinished', request => {
    if (evidence.stage === 'native-sign-in' && routeClass(request.url()) === 'event') {
      evidence.nativeSignIn.eventFinished = true
    }
  })
  page.on('requestfailed', request => {
    const route = routeClass(request.url())
    if (evidence.stage === 'native-sign-in' && route === 'event') evidence.nativeSignIn.eventFailed = true
    const receipt = expectedRequests.get(request)
    const status = responseStatuses.get(request)
    if (route !== 'event' || request.failure()?.errorText !== 'net::ERR_ABORTED') {
      rejectDiagnostic('request-failure', route, status)
    } else if (receipt !== undefined) {
      if (!abortedRequests.has(request)) {
        remember(requestAborts, receipt)
        abortedRequests.add(request)
      }
    } else if (status !== undefined && postResponseAborts.size < 128) {
      postResponseAborts.set(request, { phase: evidence.stage, route, status })
    } else rejectDiagnostic('request-failure', route, status)
  })
}

function checkDiagnostics(): void {
  for (const [request, event] of postResponseAborts) {
    if (validatedReads.has(request)) evidence.diagnostics.validatedReadTerminations++
    else unexpected.set('unvalidated-read-termination', event)
  }
  evidence.checks.push(...unexpected.keys())
  assert.deepEqual([...unexpected.values()], [])
  assert.deepEqual([...httpRefusals].sort(), expectedRefusals)
  assert.deepEqual([...consoleRefusals].sort(), expectedRefusals)
  assert.deepEqual([...requestAborts].sort(), expectedAborts)
  evidence.checks.push('expected-browser-diagnostics')
}

function waitEventPage(page: Page, path = eventPath): Promise<Response> {
  return page.waitForResponse(response => {
    const url = new URL(response.url())
    return url.origin === origin && url.pathname === path && response.request().method() === 'GET'
  })
}

const rows = (page: Page) => page.locator('.run-timeline ol > li')
const refresh = (page: Page) => page.getByRole('button', { name: 'Reload latest' })
const earlier = (page: Page) => page.getByRole('button', { name: 'Earlier events' })
const signOut = (page: Page) => page.getByRole('button', { name: 'Sign out' })

async function checkedResponse(response: Response): Promise<Document> {
  signInStep('event-headers')
  assert.equal(response.status(), 200)
  assert.match(string(await response.headerValue('cache-control')), /no-store/iu)
  signInStep('event-body')
  const content: unknown = await response.json()
  return object(content)
}

async function assertPageRows(page: Page, expected: Document[]): Promise<void> {
  const first = expected[0]
  if (first !== undefined) {
    await rows(page).first().getByText(string(first.id), { exact: true }).waitFor({ state: 'visible' })
  }
  assert.equal(await rows(page).count(), expected.length)
  for (const [index, event] of expected.entries()) {
    const row = rows(page).nth(index)
    const actor = object(event.actor)
    assert.equal(await row.locator('time').getAttribute('datetime'), event.at)
    assert.equal(await row.locator('strong').textContent(), event.kind)
    assert.equal(await row.locator('code').first().textContent(), event.id)
    for (const identity of [event.node_instance_id, event.attempt_id]) {
      if (identity !== null) assert.equal(await row.getByText(string(identity), { exact: true }).count(), 1)
    }
    if (actor.kind === 'system') {
      assert.equal(await row.getByText(`${string(actor.component)} (system)`, { exact: true }).count(), 1)
    } else {
      assert.equal(await row.locator('bdi').last().textContent(), actor.owner)
      assert((await row.locator('div').last().innerText()).includes(`(${string(actor.kind)})`))
      assert.equal(await row.locator('code').last().textContent(), actor.token_id)
    }
  }
}

async function latest(page: Page, response: Response, began: number): Promise<void> {
  const body = await checkedResponse(response)
  signInStep('event-parity')
  const query = new URL(response.url()).searchParams
  assert.equal(query.get('limit'), '20')
  assert(!query.has('cursor'))
  assert.deepEqual(body.items, canonicalRows)
  assert.equal(body.next_cursor, canonical.next_cursor)
  const observed = Date.parse(string(body.observed_at))
  assert(began <= observed && observed <= Date.now())
  signInStep('latest-dom')
  const observation = page.locator('time').filter({ hasText: string(body.observed_at) })
  await observation.waitFor({ state: 'visible' })
  assert.equal(await observation.getAttribute('datetime'), body.observed_at)
  await assertPageRows(page, canonicalRows)
  assert(await earlier(page).isVisible())
  validatedReads.add(response.request())
}

async function signIn(page: Page, context: BrowserContext, url: string): Promise<void> {
  assert.equal(new URL(url).origin, origin)
  assert.match(new URL(url).pathname, /^\/api\/v1\/auth\/session\/[0-9a-f]{64}$/u)
  const observed = waitEventPage(page)
  const redemption = page.waitForResponse(response => response.url() === url)
  const began = Date.now()
  signInStep('navigate')
  const navigation = await page.goto(url, { waitUntil: 'domcontentloaded' })
  signInStep('redemption')
  const response = await redemption
  signInStep('redirect-headers')
  assert.equal(response.status(), 303)
  assert.equal(await response.headerValue('location'), timeline)
  assert.equal(await response.headerValue('referrer-policy'), 'no-referrer')
  assert.match(string(await response.headerValue('cache-control')), /no-store/iu)
  const request = response.request()
  signInStep('fetch-metadata')
  const metadata = await Promise.all([
    'sec-fetch-site', 'sec-fetch-mode', 'sec-fetch-dest', 'sec-fetch-user',
  ].map(name => request.headerValue(name)))
  assert.deepEqual(metadata, ['none', 'navigate', 'document', '?1'])
  signInStep('document-headers')
  assert.equal(navigation?.status(), 200)
  assert.equal(await navigation.request().headerValue('referer'), null)
  assert.equal(page.url(), origin + timeline)
  signInStep('cookies')
  const cookies = await context.cookies(origin)
  const session = cookies.find(cookie => cookie.name === '__Host-kora_session')
  const csrf = cookies.find(cookie => cookie.name === '__Host-kora_csrf')
  assert(session && csrf && session.value !== csrf.value)
  send({ type: 'markers', values: [session.value, csrf.value] })
  for (const cookie of [session, csrf]) {
    assert.equal(cookie.domain, '127.0.0.1')
    assert.equal(cookie.path, '/')
    assert.equal(cookie.sameSite, 'Strict')
    assert(cookie.secure)
  }
  assert(session.httpOnly && !csrf.httpOnly)
  signInStep('cookie-visibility')
  assert.equal(await page.evaluate(() => document.cookie.includes('__Host-kora_session=')), false)
  signInStep('event-response')
  await latest(page, await observed, began)
  signInStep('clean-page')
  const rendered = await page.locator('body').innerText()
  assert(![url, new URL(url).pathname, session.value, csrf.value].some(marker => rendered.includes(marker)))
}

async function appearance(page: Page): Promise<void> {
  assert.equal(await page.title(), `Run history · ${namespace} · kora`)
  assert.equal(await page.locator('html').getAttribute('lang'), 'en')
  assert.equal(await page.getByRole('main').count(), 1)
  await page.getByRole('heading', { name: 'Run history', level: 1 }).waitFor()
  assert.equal(await page.getByText(string(bootstrap.inert_event), { exact: true }).count(), 1)
  assert.equal(await rows(page).locator('img').count(), 0)
  const loaded = await page.evaluate(async () => {
    await document.fonts.ready
    return [...document.fonts]
      .filter(font => font.status === 'loaded')
      .map(font => `${font.family.replaceAll('"', '')}:${font.weight === 'normal' ? '400' : font.weight}`)
      .sort()
  })
  assert.deepEqual(loaded, ['IBM Plex Mono:400', 'IBM Plex Sans:400', 'IBM Plex Sans:700'])
  evidence.checks.push('installed-fonts')
  for (const theme of ['light', 'dark'] as const satisfies readonly Theme[]) {
    await page.emulateMedia({ colorScheme: theme })
    for (const width of [1280, 768, 375]) {
      await page.setViewportSize({ width, height: 900 })
      const visual = await page.locator('.run-timeline').evaluate(element => ({
        foreground: getComputedStyle(element).color,
        background: getComputedStyle(element).backgroundColor,
        fits: document.documentElement.scrollWidth <= window.innerWidth,
      }))
      assert(visual.fits)
      assert.equal(visual.foreground, theme === 'light' ? 'rgb(26, 29, 33)' : 'rgb(231, 233, 238)')
      assert.equal(visual.background, theme === 'light' ? 'rgb(255, 255, 255)' : 'rgb(15, 17, 21)')
      await page.screenshot({ path: join(output, `timeline-${theme}-${String(width)}.png`), timeout: 5000 })
    }
  }
}

async function paginate(page: Page): Promise<void> {
  await refresh(page).focus()
  const signOutButton = signOut(page)
  await page.keyboard.press('Tab')
  assert(await signOutButton.evaluate(element => element === document.activeElement))
  await page.keyboard.press('Tab')
  assert(await earlier(page).evaluate(element => element === document.activeElement))
  const first = waitEventPage(page)
  await page.keyboard.press('Enter')
  const response = await first
  const query = new URL(response.url()).searchParams
  assert.equal(query.get('cursor'), canonical.next_cursor)
  const older = await checkedResponse(response)
  assert(Array.isArray(older.items))
  const items = (older.items as unknown[]).map(object)
  assert.equal(items.length, 1)
  assert.equal(items[0]?.id, earliest)
  assert.equal(older.next_cursor, null)
  await assertPageRows(page, items)
  assert.equal(await earlier(page).count(), 0)
  validatedReads.add(response.request())
  assert(await refresh(page).evaluate(element => element === document.activeElement))
  const latestPage = waitEventPage(page)
  const began = Date.now()
  await page.keyboard.press('Enter')
  await latest(page, await latestPage, began)
}

async function withHeldRead(
  page: Page,
  context: BrowserContext,
  start: () => Promise<unknown>,
  check: string,
  use: (release: () => Promise<void>) => Promise<void>,
): Promise<void> {
  const devtools = await context.newCDPSession(page)
  const deadline = new AbortController()
  const timer = setTimeout(() => { deadline.abort() }, 10000)
  try {
    await devtools.send('Fetch.enable', {
      patterns: [{ urlPattern: origin + eventPath + '*', requestStage: 'Response' }],
    })
    await devtools.send('Network.enable')
    const paused = new Promise<{ requestId: string; networkId: string }>((resolve, reject) => {
      devtools.once('Fetch.requestPaused', event => {
        if (event.responseStatusCode === 200 && event.networkId !== undefined) {
          resolve({ requestId: event.requestId, networkId: event.networkId })
        } else reject(new Error('Unexpected paused status'))
      })
    })
    const request = Promise.race([paused, once(deadline.signal, 'abort').then(() => {
      throw new Error('Pause deadline')
    })])
    void request.catch(() => undefined)
    await start()
    const { requestId, networkId } = await request
    clearTimeout(timer)
    evidence.checks.push(`${check}-response-paused`)
    await use(async () => {
      const finished = new Promise<void>(resolve => {
        devtools.on('Network.loadingFinished', event => { if (event.requestId === networkId) resolve() })
      })
      const timeout = AbortSignal.timeout(10000)
      await devtools.send('Fetch.continueResponse', { requestId })
      await Promise.race([finished, once(timeout, 'abort').then(() => {
        throw new Error('Delivery deadline')
      })])
    })
    await devtools.send('Fetch.disable')
  } finally {
    clearTimeout(timer)
    deadline.abort()
    await devtools.detach()
  }
}

async function cancelHeldRead(
  page: Page, context: BrowserContext, transition: () => Promise<void>, check: string,
): Promise<void> {
  const started = page.waitForRequest(request => request.method() === 'GET'
    && new URL(request.url()).pathname === eventPath)
  void started.catch(() => undefined)
  await withHeldRead(page, context, () => refresh(page).click(), check, async () => {
    const request = await started
    expectedRequests.set(request, `${evidence.stage}:event`)
    const aborted = page.waitForEvent('requestfailed', {
      predicate: pending => pending === request && pending.failure()?.errorText === 'net::ERR_ABORTED',
      timeout: 10000,
    })
    void aborted.catch(() => undefined)
    await transition()
    await aborted
    evidence.checks.push(`${check}-request-aborted`)
  })
  assert.equal(await rows(page).count(), 0)
}

async function checkObsoleteRead(page: Page, context: BrowserContext): Promise<void> {
  await cancelHeldRead(page, context, async () => {
    const emptyPage = `/console/runs/${namespace}/${empty}`
    const emptyEventPath = `/console/api/v1/namespaces/${namespace}/runs/${empty}/events`
    const newPage = waitEventPage(page, emptyEventPath)
    await page.evaluate(target => {
      const current = location.pathname
      history.replaceState(null, '', target)
      history.pushState(null, '', current)
      history.back()
    }, emptyPage)
    await page.waitForURL(origin + emptyPage)
    evidence.checks.push('obsolete-scope-navigated')
    const response = await newPage
    assert.equal(response.status(), 200)
    await page.getByRole('status').filter({ hasText: 'No events yet.' }).waitFor()
    validatedReads.add(response.request())
  }, 'obsolete')
}

async function checkRefusal(page: Page, ns: string, runId: string, status: number, message: string): Promise<void> {
  const response = waitEventPage(page, `/console/api/v1/namespaces/${ns}/runs/${runId}/events`)
  await page.goto(`${origin}/console/runs/${ns}/${runId}`, { waitUntil: 'domcontentloaded' })
  const refused = await response
  assert.equal(refused.status(), status)
  await page.getByRole('status').filter({ hasText: message }).waitFor()
  assert.equal(await rows(page).count(), 0)
  validatedReads.add(refused.request())
}

async function navigateLatest(page: Page, navigate: () => Promise<Response | null>): Promise<void> {
  const response = waitEventPage(page)
  const began = Date.now()
  assert.equal((await navigate())?.status(), 200)
  const events = await response
  assert.equal(events.status(), 200)
  assert.match(string(await events.headerValue('cache-control')), /no-store/iu)
  assert.equal(new URL(events.url()).searchParams.get('limit'), '20')
  await assertPageRows(page, canonicalRows)
  const observed = Date.parse(string(await page.locator('time').first().getAttribute('datetime')))
  assert(began <= observed && observed <= Date.now())
  assert(await earlier(page).isVisible())
  validatedReads.add(events.request())
}

function reload(page: Page): Promise<void> {
  return navigateLatest(page, () => page.reload({ waitUntil: 'domcontentloaded' }))
}

function openCurrentRun(page: Page): Promise<void> {
  return navigateLatest(page, () => page.goto(origin + timeline, { waitUntil: 'domcontentloaded' }))
}

async function loseAuthority(page: Page): Promise<void> {
  const response = waitEventPage(page)
  await refresh(page).click()
  const refused = await response
  assert.equal(refused.status(), 401)
  await page.getByRole('status').filter({ hasText: 'Sign in to view this run’s events.' }).waitFor()
  assert.equal(await rows(page).count(), 0)
  validatedReads.add(refused.request())
}

async function trustedJourney(): Promise<void> {
  const owner = await profile('trusted-chromium', true)
  try {
    const browser = await owner.startBrowser()
    const context = await browser.newContext({ serviceWorkers: 'block' })
    context.setDefaultTimeout(10000)
    context.setDefaultNavigationTimeout(10000)
    try {
      const outside = await context.newPage()
      await checkSecondAuthority(outside, owner)
      await outside.close()
      stage('native-sign-in')
      signInStep('open-page')
      const page = await context.newPage()
      watchSafeDiagnostics(page)
      const signInUrl = string(bootstrap.url)
      await signIn(page, context, signInUrl)
      signInStep('open-replay')
      const replay = await context.newPage()
      try {
        watchSafeDiagnostics(replay)
        signInStep('replay-navigation')
        const refused = await replay.goto(signInUrl, { waitUntil: 'domcontentloaded' })
        signInStep('replay-response')
        assert.equal(refused?.status(), 401)
        const body: unknown = await refused.json()
        assert.equal(object(object(body).error).code, 'API009')
        signInStep('close-replay')
      } finally {
        await replay.close()
      }
      signInStep('canonical-cookie')
      const cookieOnly = await page.evaluate(async path => {
        const response = await fetch(path)
        const body: unknown = await response.json()
        return { status: response.status, body }
      }, eventPath.replace('/console', ''))
      assert.equal(cookieOnly.status, 401)
      assert.equal(object(object(cookieOnly.body).error).code, 'API009')
      signInStep('complete')
      stage('responsive-appearance')
      await appearance(page)
      stage('pagination')
      await paginate(page)
      stage('reload')
      await reload(page)
      stage('obsolete-read')
      await checkObsoleteRead(page, context)
      stage('denied-and-missing')
      await checkRefusal(page, 'installed_browser_denied', denied, 403, 'You don’t have access')
      await checkRefusal(page, namespace, 'run_00000000000000000000000000', 404, 'Run not found.')
      stage('sign-out')
      await openCurrentRun(page)
      await reload(page)
      const background = await context.newPage()
      try {
        watchSafeDiagnostics(background)
        await background.bringToFront()
        const oldResponse = waitEventPage(background)
        void oldResponse.catch(() => undefined)
        await withHeldRead(
          background, context, () => background.goto(origin + timeline, { waitUntil: 'domcontentloaded' }),
          'shared-session', async release => {
            assert.equal(await rows(background).count(), 0)
            await page.bringToFront()
            await refresh(page).and(page.locator('[aria-disabled=false]')).waitFor()
            await cancelHeldRead(page, context, async () => {
              assert.equal(await rows(page).count(), canonicalRows.length)
              const logout = page.waitForResponse(response => routeClass(response.url()) === 'signOut'
                && response.request().method() === 'DELETE')
              await signOut(page).click()
              const signedOut = await logout
              assert.equal(signedOut.status(), 204)
              assert.equal(await signedOut.request().headerValue('origin'), origin)
              assert.match(string(await signedOut.request().headerValue('x-csrf-token')), /^[0-9a-f]{64}$/u)
              await page.getByRole('status').filter({ hasText: 'Sign in to view this run’s events.' }).waitFor()
            }, 'logout')
            assert(!(await context.cookies(origin)).some(cookie => cookie.name.startsWith('__Host-kora_')))
            await release()
            const response = await oldResponse
            assert.equal(response.status(), 200)
                    await background.getByRole('status').filter({ hasText: 'Sign in to view this run’s events.' }).waitFor()
            assert.equal(await rows(background).count(), 0)
            validatedReads.add(response.request())
            evidence.checks.push('shared-session-obsolete-response-refused')
          },
        )
      } finally {
        await background.close()
      }
      stage('expired-authority')
      const relogin = await context.newPage()
      await page.close()
      watchSafeDiagnostics(relogin)
      send({ type: 'relogin' })
      await signIn(relogin, context, string((await receive()).url))
      send({ type: 'expire' })
      const successor = string((await receive()).url)
      await loseAuthority(relogin)
      stage('revoked-authority')
      const another = await context.newPage()
      await relogin.close()
      watchSafeDiagnostics(another)
      await signIn(another, context, successor)
      send({ type: 'revoke' })
      assert.equal((await receive()).revoked, true)
      await loseAuthority(another)
      checkDiagnostics()
      send({ type: 'finish' })
    } catch (error) {
      if (evidence.stage === 'native-sign-in') evidence.nativeSignIn.failedStep = evidence.nativeSignIn.step
      throw error
    } finally {
      await context.close()
    }
  } finally {
    checkCleanup(await owner.close())
  }
}

try {
  await checkUntrustedCertificate()
  stage('proxy-destination')
  await trustedJourney()
  stage('complete')
  evidence.status = 'passed'
} catch (error) {
  const failure = error instanceof assert.AssertionError ? 'assertion-refused'
    : error instanceof Error && error.name === 'TimeoutError' ? 'operation-timed-out' : 'operation-refused'
  evidence.checks.push(failure)
  process.exitCode = 1
} finally {
  input.close()
  process.stdin.pause()
  await closeOwners()
  limits.close()
  if (cancellation.signal.aborted || limits.failed) {
    evidence.status = 'failed'
    process.exitCode = 1
  }
  await writeFile(join(output, 'report.json'), JSON.stringify(evidence, null, 2))
  process.removeListener('SIGTERM', stop)
  process.removeListener('SIGINT', stop)
}
