import assert from 'node:assert/strict'
import { once } from 'node:events'
import { createServer, request } from 'node:http'
import type { Socket } from 'node:net'
import { connect } from 'node:net'

export class LocalProxy {
  readonly sockets = new Set<Socket>()
  readonly server = createServer()
  denied = 0

  async listen(origin: string): Promise<string> {
    const allowed = new URL(origin)
    assert(allowed.protocol === 'http:' && allowed.port !== '' && allowed.hostname === '127.0.0.1' && allowed.username === '' && allowed.password === '' && allowed.href === `${origin}/`)
    const permitted = (raw: string | undefined): URL | undefined => {
      if (raw === undefined) return undefined
      try {
        const target = new URL(raw)
        if (target.protocol === 'ws:') target.protocol = 'http:'
        return target.protocol === 'http:' && target.origin === origin && target.username === '' && target.password === '' ? target : undefined
      } catch (error) {
        if (error instanceof TypeError) return undefined
        throw error
      }
    }
    this.server.on('connection', socket => { this.track(socket) })
    this.server.on('connect', (incoming, socket, head) => {
      if (incoming.url !== allowed.host) {
        this.denied++
        socket.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
        return
      }
      const upstream = connect({ host: allowed.hostname, port: Number(allowed.port) }, () => {
        socket.write('HTTP/1.1 200 Connection Established\r\n\r\n')
        upstream.write(head)
        socket.pipe(upstream).pipe(socket)
      })
      this.track(upstream)
      upstream.on('error', () => { socket.destroy() })
      socket.on('error', () => { upstream.destroy() })
    })
    this.server.on('request', (incoming, outgoing) => {
      const target = permitted(incoming.url)
      if (!target) {
        this.denied++
        outgoing.writeHead(403)
        outgoing.end()
        return
      }
      const upstream = request({ protocol: allowed.protocol, hostname: allowed.hostname, port: allowed.port, path: target.pathname + target.search, method: incoming.method, headers: { ...incoming.headers, host: allowed.host } }, response => {
        outgoing.writeHead(response.statusCode ?? 502, response.headers)
        response.pipe(outgoing)
      })
      upstream.on('socket', socket => { this.track(socket) })
      upstream.on('error', () => { outgoing.destroy() })
      incoming.on('aborted', () => { upstream.destroy() })
      incoming.pipe(upstream)
    })
    this.server.on('upgrade', (incoming, downstream, head) => {
      const target = permitted(incoming.url)
      if (!target) {
        this.denied++
        downstream.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
        return
      }
      const upstream = request({ protocol: allowed.protocol, hostname: allowed.hostname, port: allowed.port, path: target.pathname + target.search, headers: { ...incoming.headers, host: allowed.host } })
      upstream.on('socket', socket => { this.track(socket) })
      upstream.on('upgrade', (response, socket, received) => {
        downstream.write(`HTTP/1.1 ${String(response.statusCode)} Switching Protocols\r\n`)
        for (let index = 0; index < response.rawHeaders.length; index += 2) {
          const name = response.rawHeaders[index]
          const value = response.rawHeaders[index + 1]
          assert(name !== undefined && value !== undefined)
          downstream.write(`${name}: ${value}\r\n`)
        }
        downstream.write('\r\n')
        downstream.write(received)
        socket.write(head)
        downstream.pipe(socket).pipe(downstream)
      })
      upstream.on('error', () => { downstream.destroy() })
      upstream.end()
    })
    this.server.listen(0, '127.0.0.1')
    await once(this.server, 'listening')
    const address = this.server.address()
    assert(address && typeof address !== 'string')
    return `http://127.0.0.1:${String(address.port)}`
  }

  track(socket: Socket): void {
    if (this.sockets.has(socket)) return
    this.sockets.add(socket)
    socket.once('close', () => { this.sockets.delete(socket) })
  }

  async close(): Promise<void> {
    if (!this.server.listening) return
    const closed = once(this.server, 'close')
    this.server.close()
    const socketsClosed = [...this.sockets].map(async socket => {
      const completion = once(socket, 'close')
      socket.destroy()
      await completion
    })
    await Promise.all(socketsClosed)
    await closed
    assert.equal(this.sockets.size, 0)
  }
}
