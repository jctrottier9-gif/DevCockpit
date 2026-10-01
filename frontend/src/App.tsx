import { useEffect, useState } from 'react'

type LoadState = 'loading' | 'ready' | 'error'

type Project = {
  project_id: string
  repository_full_name: string
  roadmap_issue_number: number
}

type WorkItem = {
  key: string
  type: string
  status: string
  parent: string
  lane: string
  title: string
}

type RoadmapResponse = {
  project: Project
  source: {
    status: 'available' | 'unavailable'
    issue_number?: number
    updated_at?: string | null
    code?: string
  }
  pipeline: null | {
    valid: boolean
    work_items: WorkItem[]
    diagnostics: Array<{ code: string; message: string; line_number: number | null }>
    active_ready_item: WorkItem | null
  }
}

function App() {
  const [state, setState] = useState<LoadState>('loading')
  const [roadmap, setRoadmap] = useState<RoadmapResponse | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()

    async function loadRoadmap() {
      try {
        const projectsResponse = await fetch('/api/projects', { signal: controller.signal })
        if (!projectsResponse.ok) {
          throw new Error('Unable to load configured projects')
        }
        const projectsPayload = (await projectsResponse.json()) as { projects: Project[] }
        const project = projectsPayload.projects[0]
        if (!project) {
          throw new Error('No project is configured')
        }

        const roadmapResponse = await fetch(
          '/api/projects/' + encodeURIComponent(project.project_id) + '/roadmap',
          { signal: controller.signal },
        )
        const roadmapPayload = (await roadmapResponse.json()) as RoadmapResponse
        setRoadmap(roadmapPayload)
        if (!roadmapResponse.ok) {
          setError(roadmapPayload.source.code ?? 'GitHub roadmap unavailable')
          setState('error')
          return
        }
        setState('ready')
      } catch (caught: unknown) {
        if (caught instanceof DOMException && caught.name === 'AbortError') {
          return
        }
        setError(caught instanceof Error ? caught.message : 'Unable to load roadmap')
        setState('error')
      }
    }

    void loadRoadmap()
    return () => controller.abort()
  }, [])

  const pipeline = roadmap?.pipeline
  const ready = pipeline?.active_ready_item

  return (
    <main className="shell">
      <section className="panel">
        <p className="eyebrow">DEVCOCKPIT</p>
        <h1>Canonical roadmap</h1>
        {roadmap ? (
          <div className="roadmap-summary">
            <div><span>Project</span><strong>{roadmap.project.project_id}</strong></div>
            <div><span>Repository</span><strong>{roadmap.project.repository_full_name}</strong></div>
            <div><span>Roadmap</span><strong>#{roadmap.project.roadmap_issue_number}</strong></div>
            <div><span>Pipeline</span><strong>{pipeline ? (pipeline.valid ? 'valid' : 'invalid') : 'unavailable'}</strong></div>
            <div><span>READY</span><strong>{ready?.key ?? 'none'}</strong></div>
          </div>
        ) : null}
        <div className={'health health--' + state} aria-live="polite">
          {state === 'loading' ? 'Reading GitHub roadmap…' : state === 'ready' ? 'Roadmap loaded' : error}
        </div>
        {pipeline && !pipeline.valid ? (
          <ul className="diagnostics">
            {pipeline.diagnostics.map((diagnostic) => (
              <li key={diagnostic.code + '-' + (diagnostic.line_number ?? 'global')}>
                {diagnostic.code}: {diagnostic.message}
              </li>
            ))}
          </ul>
        ) : null}
      </section>
    </main>
  )
}

export default App
