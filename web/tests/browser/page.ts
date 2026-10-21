import assert from 'node:assert/strict'
import type { Browser, BrowserContext, CDPSession, Page } from 'playwright'
import { numberField } from './resources.ts'

interface PaintState {
  lcp: number
  cls: number
  longTasks: number
}

declare global {
  interface Window {
    koraBrowserMeasurement: PaintState
  }
}

export interface Load {
  load: number
  fcp: number
  lcp: number
  cls: number
  longTasks: number
  transfer: number
}

export interface Heap {
  used: number
  total: number
  documents: number
  nodes: number
  listeners: number
}

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
  await context.addInitScript(() => {
    window.koraBrowserMeasurement = { lcp: 0, cls: 0, longTasks: 0 }
    new PerformanceObserver(list => {
      for (const entry of list.getEntries()) window.koraBrowserMeasurement.lcp = entry.startTime
    }).observe({ type: 'largest-contentful-paint', buffered: true })
    new PerformanceObserver(list => {
      for (const entry of list.getEntries()) {
        if ('hadRecentInput' in entry && !entry.hadRecentInput && 'value' in entry && typeof entry.value === 'number') {
          window.koraBrowserMeasurement.cls += entry.value
        }
      }
    }).observe({ type: 'layout-shift', buffered: true })
    new PerformanceObserver(list => {
      window.koraBrowserMeasurement.longTasks += list.getEntries().length
    }).observe({ type: 'longtask', buffered: true })
  })
  return context
}

export async function ready(page: Page): Promise<Load> {
  await page.getByRole('heading', { level: 1, name: 'kora', exact: true }).waitFor({ state: 'visible' })
  await page.evaluate(async () => { await document.fonts.ready })
  const paintReady = await page.waitForFunction(() => window.koraBrowserMeasurement.lcp > 0 && performance.getEntriesByName('first-contentful-paint').length === 1)
  await paintReady.dispose()
  return page.evaluate(() => {
    const navigation = performance.getEntriesByType('navigation')[0]
    const fcp = performance.getEntriesByName('first-contentful-paint')[0]
    if (!(navigation instanceof PerformanceNavigationTiming) || !fcp) throw new Error('Missing navigation or paint')
    const transfer = performance.getEntriesByType('resource').reduce((total, entry) => {
      if (!(entry instanceof PerformanceResourceTiming)) throw new Error('Invalid resource timing')
      return total + entry.transferSize
    }, navigation.transferSize)
    return { load: navigation.loadEventEnd, fcp: fcp.startTime, ...window.koraBrowserMeasurement, transfer }
  })
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

export async function heap(session: CDPSession): Promise<Heap> {
  const performanceMetrics: unknown = await session.send('Performance.getMetrics')
  assert(performanceMetrics !== null && typeof performanceMetrics === 'object' && 'metrics' in performanceMetrics)
  assert(Array.isArray(performanceMetrics.metrics))
  const rows: unknown[] = performanceMetrics.metrics
  const values = new Map<string, number>()
  for (const row of rows) {
    assert(row !== null && typeof row === 'object' && 'name' in row && typeof row.name === 'string')
    values.set(row.name, numberField(row, 'value'))
  }
  const used = values.get('JSHeapUsedSize')
  const total = values.get('JSHeapTotalSize')
  assert(used !== undefined && total !== undefined)
  const dom: unknown = await session.send('Memory.getDOMCounters')
  return { used, total, documents: numberField(dom, 'documents'), nodes: numberField(dom, 'nodes'), listeners: numberField(dom, 'jsEventListeners') }
}
