import assert from 'node:assert/strict'
import type { Browser, BrowserContext, Page } from 'playwright'

export class Diagnostics {
  readonly events: { kind: string; detail: string }[] = []
  overflow = false

  add(kind: string, detail: string): void {
    if (this.events.length < 100) this.events.push({ kind, detail: detail.slice(0, 512) })
    else this.overflow = true
  }

  check(): void {
    assert(!this.overflow && this.events.length === 0, JSON.stringify(this.events))
  }
}

export async function contextFor(browser: Browser, origin: string, diagnostics: Diagnostics, width = 1280): Promise<BrowserContext> {
  const context = await browser.newContext({ viewport: { width, height: 900 }, serviceWorkers: 'block' })
  context.setDefaultTimeout(10000)
  context.setDefaultNavigationTimeout(10000)
  await context.route('**/*', async route => {
    if (new URL(route.request().url()).origin === origin) await route.continue()
    else {
      diagnostics.add('external_request', new URL(route.request().url()).protocol)
      await route.abort('blockedbyclient')
    }
  })
  await context.routeWebSocket('**/*', async socket => {
    const address = new URL(socket.url())
    address.protocol = address.protocol === 'ws:' ? 'http:' : 'https:'
    if (address.origin === origin) socket.connectToServer()
    else {
      diagnostics.add('external_websocket', address.protocol)
      await socket.close()
    }
  })
  context.on('page', page => {
    page.on('console', message => {
      if (message.type() === 'warning' || message.type() === 'error') diagnostics.add('console', message.text())
    })
    page.on('pageerror', error => { diagnostics.add('pageerror', error.message) })
    page.on('requestfailed', request => { diagnostics.add('requestfailed', request.failure()?.errorText ?? 'missing failure') })
    page.on('response', response => {
      if (response.status() >= 400) diagnostics.add('http', String(response.status()))
    })
  })
  return context
}

export async function ready(page: Page): Promise<void> {
  await page.getByRole('heading', { level: 1, name: 'kora', exact: true }).waitFor({ state: 'visible' })
  await page.evaluate(async () => { await document.fonts.ready })
  const loaded = await page.waitForFunction(() => document.readyState === 'complete' && performance.getEntriesByName('first-contentful-paint').length === 1)
  await loaded.dispose()
}

export async function semantics(page: Page): Promise<void> {
  assert.equal(await page.title(), 'kora')
  assert.equal(await page.locator('html').getAttribute('lang'), 'en')
  assert.equal(await page.getByRole('main').count(), 1)
  assert.equal(await page.getByRole('heading').count(), 1)
  assert.equal(await page.getByText('A control plane for coding-agent sessions.', { exact: true }).count(), 1)
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))
  assert.equal(await page.locator('button, a[href], input, select, textarea, [tabindex]').count(), 0)
  await page.keyboard.press('Tab')
  assert(await page.evaluate(() => document.activeElement === document.body))
}
