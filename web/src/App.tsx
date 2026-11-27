import { useEffect, useState } from 'react'
import { flushSync } from 'react-dom'
import { validRunEventsScope } from './api/runEvents'
import RunTimeline from './RunTimeline'

export default function App() {
  const [route, setRoute] = useState<{ path: string | null; generation: number }>(
    () => ({ path: window.location.pathname, generation: 0 }),
  )
  useEffect(() => {
    const events = ['popstate', 'pagehide', 'pageshow'] as const
    const navigate = (event: Event) => {
      if (event.type === 'pageshow' && event instanceof PageTransitionEvent && !event.persisted) return
      const path = event.type === 'pagehide' ? null : window.location.pathname
      flushSync(() => {
        setRoute(previous => ({ path, generation: previous.generation + 1 }))
      })
    }
    for (const event of events) window.addEventListener(event, navigate)
    return () => {
      for (const event of events) window.removeEventListener(event, navigate)
    }
  }, [])

  if (route.path === null) return null
  if (route.path.startsWith('/console/')) {
    const [prefix, namespace, run, extra] = route.path.slice('/console/'.length).split('/')
    if (prefix === 'runs' && namespace !== undefined && run !== undefined && extra === undefined
      && validRunEventsScope(namespace, run)) {
      return <RunTimeline namespace={namespace} run={run} navigation={route.generation} />
    }
    return <main className="run-timeline"><h1>Run history</h1><p>This run link is invalid.</p></main>
  }

  return (
    <main>
      <h1>kora</h1>
      <p>A control plane for coding-agent sessions.</p>
    </main>
  )
}
