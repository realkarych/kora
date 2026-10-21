import assert from 'node:assert/strict'
import { fork } from 'node:child_process'
import type { ChildProcess } from 'node:child_process'
import { once } from 'node:events'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import type { Browser, BrowserServer } from 'playwright'
import { nativeResources, verifyReleased } from './resources.ts'
import type { NativeResources } from './resources.ts'
import { LocalProxy } from './proxy.ts'

export interface Cleanup {
  browserExit: number | null
  serverExit: number | null
  seconds: number
  remaining: NativeResources
  forced: boolean
  proxyDenied: number
  proxySockets: number
  failures: string[]
}

const activeOwners = new Set<Ownership>()

export async function closeOwners(): Promise<void> {
  await Promise.allSettled([...activeOwners].map(owner => owner.close()))
}

export class Ownership {
  server: ChildProcess | undefined
  browserServer: BrowserServer | undefined
  browser: Browser | undefined
  readonly pids = new Set<number>()
  readonly logs: string[] = []
  logBytes = 0
  overflow = false
  origin: string | undefined
  readonly proxy = new LocalProxy()
  readonly lifecycle = new EventTarget()
  private closing: Promise<Cleanup> | undefined
  private readonly acquiring = new Set<Promise<unknown>>()
  readonly traceDirectory: string | undefined

  constructor(traceDirectory?: string) {
    this.traceDirectory = traceDirectory
    activeOwners.add(this)
  }

  startServer(mode: 'production' | 'development', root: string, port = 0): Promise<string> {
    assert(!this.isClosing(), 'Owner is closing')
    const pending = this.acquireServer(mode, root, port)
    this.acquiring.add(pending)
    return pending.finally(() => { this.acquiring.delete(pending) })
  }

  private async acquireServer(mode: 'production' | 'development', root: string, port: number): Promise<string> {
    assert(this.server === undefined)
    const server = fork(fileURLToPath(new URL('./server.ts', import.meta.url)), [mode, root, String(port)], {
      stdio: ['ignore', 'pipe', 'pipe', 'ipc'],
      env: this.environment(),
    })
    this.server = server
    assert(server.pid)
    this.pids.add(server.pid)
    const capture = (chunk: Buffer): void => {
      this.logBytes += chunk.byteLength
      if (this.logBytes <= 65536) this.logs.push(chunk.toString('utf8'))
      else this.overflow = true
    }
    server.stdout?.on('data', capture)
    server.stderr?.on('data', capture)
    const controller = new AbortController()
    const timer = setTimeout(() => { controller.abort() }, 15000)
    try {
      const message = once(server, 'message', { signal: controller.signal })
      const exit = once(server, 'exit', { signal: controller.signal }).then(() => {
        throw new Error('Server exited before readiness')
      })
      const values: unknown = await Promise.race([message, exit])
      assert(Array.isArray(values))
      const value: unknown = values[0]
      assert(value !== null && typeof value === 'object' && 'origin' in value)
      assert(typeof value.origin === 'string')
      const origin = new URL(value.origin)
      assert(origin.hostname === '127.0.0.1' && origin.protocol === 'http:')
      this.origin = origin.origin
      return origin.origin
    } finally {
      clearTimeout(timer)
      controller.abort()
    }
  }

  environment(): Record<string, string> {
    const env: Record<string, string> = { NODE_ENV: 'test', LANG: 'C.UTF-8' }
    for (const key of ['PATH', 'HOME', 'TMPDIR', 'PLAYWRIGHT_BROWSERS_PATH']) {
      const value = process.env[key]
      if (value !== undefined) env[key] = value
    }
    return env
  }

  startBrowser(): Promise<Browser> {
    assert(!this.isClosing(), 'Owner is closing')
    const pending = this.acquireBrowser()
    this.acquiring.add(pending)
    return pending.finally(() => { this.acquiring.delete(pending) })
  }

  private async acquireBrowser(): Promise<Browser> {
    assert(this.browserServer === undefined)
    assert(this.origin)
    const proxy = await this.proxy.listen(this.origin)
    assert(!this.isClosing(), 'Owner is closing')
    const launching = chromium.launchServer({ headless: true, handleSIGTERM: false, handleSIGINT: false, handleSIGHUP: false, timeout: 15000, env: this.environment(), proxy: { server: proxy, bypass: '<-loopback>' }, tracesDir: this.traceDirectory })
    this.lifecycle.dispatchEvent(new Event('launching'))
    this.browserServer = await launching
    const pid = this.browserServer.process().pid
    assert(pid)
    this.pids.add(pid)
    assert(!this.isClosing(), 'Owner is closing')
    this.browser = await chromium.connect(this.browserServer.wsEndpoint(), { timeout: 15000 })
    return this.browser
  }

  async sample(): Promise<NativeResources> {
    const result = await nativeResources([...this.pids])
    for (const pid of result.pids) this.pids.add(pid)
    return result
  }

  private isClosing(): boolean {
    return this.closing !== undefined
  }

  close(): Promise<Cleanup> {
    this.closing ??= this.closeOnce()
    return this.closing
  }

  private async closeOnce(): Promise<Cleanup> {
    const start = performance.now()
    const failures: string[] = []
    const collect = (results: PromiseSettledResult<unknown>[]): void => {
      for (const result of results) {
        if (result.status === 'rejected') {
          const error: unknown = result.reason
          failures.push(error instanceof Error ? error.name : 'UnknownError')
        }
      }
    }
    await Promise.allSettled([...this.acquiring])
    collect(await Promise.allSettled([this.sample()]))
    let forced = false
    const browserProcess = this.browserServer?.process()
    const closeBrowser = async (): Promise<void> => {
      const timer = setTimeout(() => { forced = true; void this.browserServer?.kill() }, 5000)
      try {
        try { await this.browser?.close() }
        finally { await this.browserServer?.close() }
      } finally { clearTimeout(timer) }
    }
    const closeServer = async (): Promise<void> => {
      const server = this.server
      if (server && server.exitCode === null && server.signalCode === null) {
        const closed = once(server, 'close')
        if (server.connected) server.send({ event: 'stop' })
        else server.kill('SIGTERM')
        const timer = setTimeout(() => { forced = true; server.kill('SIGKILL') }, 5000)
        try { await closed } finally { clearTimeout(timer) }
      }
    }
    collect(await Promise.allSettled([closeBrowser(), closeServer(), this.proxy.close()]))
    collect(await Promise.allSettled([verifyReleased([...this.pids])]))
    const result = {
      browserExit: browserProcess?.exitCode ?? null,
      serverExit: this.server?.exitCode ?? null,
      seconds: (performance.now() - start) / 1000,
      remaining: await nativeResources([...this.pids]),
      forced,
      proxyDenied: this.proxy.denied,
      proxySockets: this.proxy.sockets.size,
      failures,
    }
    this.browser = undefined
    this.browserServer = undefined
    this.server = undefined
    activeOwners.delete(this)
    if (this.overflow) failures.push('DiagnosticOverflow')
    return result
  }
}

export function checkCleanup(cleanup: Cleanup): void {
  assert.deepEqual(cleanup.failures, [])
  assert(!cleanup.forced, 'Cleanup required forced termination')
  assert.equal(cleanup.proxySockets, 0)
  assert(cleanup.remaining.processes === null || cleanup.remaining.processes === 0)
}
