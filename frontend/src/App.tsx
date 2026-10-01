import { useEffect, useState } from 'react'

type HealthState = 'checking' | 'online' | 'offline'

function App() {
  const [health, setHealth] = useState<HealthState>('checking')

  useEffect(() => {
    const controller = new AbortController()

    fetch('/api/health', { signal: controller.signal })
      .then((response) => {
        if (!response.ok) {
          throw new Error('Backend health check failed')
        }
        setHealth('online')
      })
      .catch((error: unknown) => {
        if (error instanceof DOMException && error.name === 'AbortError') {
          return
        }
        setHealth('offline')
      })

    return () => controller.abort()
  }, [])

  return (
    <main className="shell">
      <section className="panel">
        <p className="eyebrow">DEVCOCKPIT</p>
        <h1>Development orchestration cockpit</h1>
        <p className="summary">
          The application foundation is ready. Roadmap orchestration, prompt dispatch,
          GitHub projections, and browser transport will be added by later slices.
        </p>
        <div className={'health health--' + health} aria-live="polite">
          Backend: {health}
        </div>
      </section>
    </main>
  )
}

export default App
