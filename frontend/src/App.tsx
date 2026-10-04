import { useEffect, useRef, useState } from 'react'
import AttentionCenter from './AttentionCenter'
import CockpitDashboard from './CockpitDashboard'
import TechnicalSurfaces from './TechnicalSurfaces'
import type { CockpitOverview, Project } from './dashboardTypes'
import {
  ACTIVE_PROJECT_STORAGE_KEY,
  isCurrentProjectLoad,
  resolveActiveProjectId,
} from './projectWorkspace'

type LoadState = 'loading' | 'ready' | 'error'

function readPreferredProjectId() {
  try {
    return window.localStorage.getItem(ACTIVE_PROJECT_STORAGE_KEY)
  } catch {
    return null
  }
}

function persistPreferredProjectId(projectId: string) {
  try {
    window.localStorage.setItem(ACTIVE_PROJECT_STORAGE_KEY, projectId)
  } catch {
    // Navigation preference only; storage failures must not block the workspace.
  }
}

function App() {
  const [state, setState] = useState<LoadState>('loading')
  const [projects, setProjects] = useState<Project[]>([])
  const [activeProjectId, setActiveProjectId] = useState('')
  const [loadedProjectId, setLoadedProjectId] = useState<string | null>(null)
  const [overview, setOverview] = useState<CockpitOverview | null>(null)
  const [error, setError] = useState('')
  const [technicalOpen, setTechnicalOpen] = useState(false)
  const [technicalWorkItem, setTechnicalWorkItem] = useState('')
  const activeProjectIdRef = useRef('')
  const loadGenerationRef = useRef(0)

  function resetProjectView() {
    setLoadedProjectId(null)
    setOverview(null)
    setError('')
    setState('loading')
    setTechnicalOpen(false)
    setTechnicalWorkItem('')
  }

  function selectProject(projectId: string) {
    if (!projects.some(project => project.project_id === projectId)) return
    if (projectId === activeProjectId) return

    loadGenerationRef.current += 1
    activeProjectIdRef.current = projectId
    resetProjectView()
    setActiveProjectId(projectId)
    persistPreferredProjectId(projectId)
  }

  function openTechnical(sectionId: string) {
    setTechnicalOpen(true)
    window.requestAnimationFrame(() => {
      document.getElementById(sectionId)?.scrollIntoView({ behavior: 'smooth' })
    })
  }

  function openWorkItem(workItemId: string) {
    setTechnicalWorkItem(workItemId)
    setTechnicalOpen(true)
    window.requestAnimationFrame(() => {
      document.getElementById('technical-orchestration')?.scrollIntoView({ behavior: 'smooth' })
    })
  }

  useEffect(() => {
    const controller = new AbortController()

    async function loadProjects() {
      try {
        const response = await fetch('/api/projects', { signal: controller.signal })
        if (!response.ok) throw new Error('Unable to load configured projects')
        const payload = (await response.json()) as { projects: Project[] }
        const configuredProjects = payload.projects ?? []
        const initialProjectId = resolveActiveProjectId(configuredProjects, readPreferredProjectId())
        if (!initialProjectId) throw new Error('No project is configured')

        setProjects(configuredProjects)
        activeProjectIdRef.current = initialProjectId
        setActiveProjectId(initialProjectId)
        persistPreferredProjectId(initialProjectId)
      } catch (caught: unknown) {
        if (caught instanceof DOMException && caught.name === 'AbortError') return
        setError(caught instanceof Error ? caught.message : 'Unable to load projects')
        setState('error')
      }
    }

    void loadProjects()
    return () => controller.abort()
  }, [])

  useEffect(() => {
    if (!activeProjectId) return

    const controller = new AbortController()
    const projectId = activeProjectId
    const generation = ++loadGenerationRef.current
    activeProjectIdRef.current = projectId
    resetProjectView()

    async function loadCockpit() {
      try {
        const response = await fetch(
          '/api/projects/' + encodeURIComponent(projectId) + '/cockpit',
          { signal: controller.signal },
        )
        const payload = (await response.json()) as CockpitOverview & { detail?: unknown }

        if (!isCurrentProjectLoad(
          projectId,
          activeProjectIdRef.current,
          generation,
          loadGenerationRef.current,
        )) return

        if (!response.ok) {
          throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Cockpit overview unavailable')
        }
        if (payload.project?.project_id !== projectId) {
          throw new Error('Project context mismatch while loading ' + projectId)
        }

        setOverview(payload)
        setLoadedProjectId(projectId)
        setState('ready')
      } catch (caught: unknown) {
        if (caught instanceof DOMException && caught.name === 'AbortError') return
        if (!isCurrentProjectLoad(
          projectId,
          activeProjectIdRef.current,
          generation,
          loadGenerationRef.current,
        )) return
        setError(caught instanceof Error ? caught.message : 'Unable to load cockpit overview')
        setState('error')
      }
    }

    void loadCockpit()
    return () => controller.abort()
  }, [activeProjectId])

  const activeProject = projects.find(project => project.project_id === activeProjectId) ?? null
  const currentOverview = loadedProjectId === activeProjectId ? overview : null

  return <main className="shell">
    <div className="panel">
      <header className="app-header">
        <div>
          <p className="eyebrow">DEVCOCKPIT</p>
          <h1>Delivery cockpit</h1>
          <p className="app-subtitle">Supervision visuelle du roadmap, des rôles et du travail observable.</p>
        </div>
        <div className={'health health--' + state} aria-live="polite">
          {state === 'loading' ? 'Reading project context…' : state === 'ready' ? 'Project context loaded' : error}
        </div>
      </header>

      {projects.length > 0 && <section className="project-workspace" aria-label="Contexte projet">
        <label htmlFor="active-project">Projet actif
          <select
            id="active-project"
            value={activeProjectId}
            disabled={projects.length === 1}
            onChange={event => selectProject(event.target.value)}
          >
            {projects.map(project => <option key={project.project_id} value={project.project_id}>{project.project_id}</option>)}
          </select>
        </label>
        {activeProject && <p>{activeProject.repository_full_name} · roadmap #{activeProject.roadmap_issue_number}</p>}
      </section>}

      {activeProjectId && <AttentionCenter
        key={'attention:' + activeProjectId}
        projectId={activeProjectId}
        onOpenWorkItem={openWorkItem}
      />}

      {currentOverview && <CockpitDashboard
        key={'cockpit:' + activeProjectId}
        overview={currentOverview}
        onOpenWorkItem={openWorkItem}
        onOpenTechnical={openTechnical}
      />}

      {activeProject && <details
        id="technical-surfaces"
        className="technical-surfaces"
        open={technicalOpen}
        onToggle={event => setTechnicalOpen(event.currentTarget.open)}
      >
        <summary>
          <span>
            <strong>Vues techniques existantes</strong>
            <small>Scheduler, exécutions, Flow Analytics, Orchestration et réponses importées</small>
          </span>
          <span aria-hidden="true">⌄</span>
        </summary>
        {technicalOpen && <TechnicalSurfaces
          key={'technical:' + activeProject.project_id}
          project={activeProject}
          requestedWorkItem={technicalWorkItem}
        />}
      </details>}
    </div>
  </main>
}

export default App
