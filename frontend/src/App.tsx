import { useEffect, useRef, useState } from 'react'
import AttentionCenter from './AttentionCenter'
import CockpitDashboard from './CockpitDashboard'
import { CockpitRefreshProvider } from './CockpitRefreshContext'
import ContextDrawer from './ContextDrawer'
import Orchestration from './Orchestration'
import TechnicalSurfaces from './TechnicalSurfaces'
import {
  classifyCockpitRefreshFailure,
  createCockpitRefreshLoop,
  resolveCockpitRefreshInterval,
  type CockpitRefreshLoop,
} from './cockpitRefresh'
import type { CockpitOverview, Project } from './dashboardTypes'
import {
  ACTIVE_PROJECT_STORAGE_KEY,
  isCurrentProjectLoad,
  resolveActiveProjectId,
} from './projectWorkspace'
import {
  createThemeController,
  readThemePreference,
  THEME_MEDIA_QUERY,
  type ThemeController,
  type ThemePreference,
} from './theme'

type LoadState = 'loading' | 'ready' | 'error'

const cockpitRefreshIntervalMs = resolveCockpitRefreshInterval(
  (import.meta as ImportMeta & { env?: Record<string, string | undefined> }).env
    ?.VITE_COCKPIT_REFRESH_INTERVAL_MS,
)

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
  const [orchestrationWorkItem, setOrchestrationWorkItem] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [lastRefreshAt, setLastRefreshAt] = useState<string | null>(null)
  const [refreshError, setRefreshError] = useState('')
  const [surfaceRefreshVersion, setSurfaceRefreshVersion] = useState(0)
  const [themePreference, setThemePreference] = useState<ThemePreference>(() =>
    readThemePreference(window.localStorage),
  )
  const activeProjectIdRef = useRef('')
  const loadedProjectIdRef = useRef<string | null>(null)
  const loadGenerationRef = useRef(0)
  const refreshLoopRef = useRef<CockpitRefreshLoop | null>(null)
  const themeControllerRef = useRef<ThemeController | null>(null)

  function resetProjectView() {
    loadedProjectIdRef.current = null
    setLoadedProjectId(null)
    setOverview(null)
    setError('')
    setRefreshError('')
    setLastRefreshAt(null)
    setRefreshing(false)
    setSurfaceRefreshVersion(0)
    setState('loading')
    setTechnicalOpen(false)
    setOrchestrationWorkItem('')
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
    setOrchestrationWorkItem(workItemId)
  }

  function requestRefreshNow() {
    void refreshLoopRef.current?.refreshNow()
  }

  useEffect(() => {
    const mediaQuery = window.matchMedia(THEME_MEDIA_QUERY)
    const controller = createThemeController({
      storage: window.localStorage,
      mediaQuery,
      applyTheme: snapshot => {
        document.documentElement.dataset.theme = snapshot.resolved
        document.documentElement.dataset.themePreference = snapshot.preference
        document.documentElement.style.colorScheme = snapshot.resolved
        setThemePreference(snapshot.preference)
      },
    })
    themeControllerRef.current = controller
    controller.start()

    return () => {
      controller.stop()
      if (themeControllerRef.current === controller) themeControllerRef.current = null
    }
  }, [])

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

    const projectId = activeProjectId
    const generation = ++loadGenerationRef.current
    activeProjectIdRef.current = projectId
    resetProjectView()

    let disposed = false
    let controller: AbortController | null = null

    async function refreshCockpit() {
      controller = new AbortController()
      const hasCurrentSnapshot = loadedProjectIdRef.current === projectId

      if (hasCurrentSnapshot) {
        setRefreshing(true)
        setSurfaceRefreshVersion(version => version + 1)
      } else {
        setState('loading')
        setError('')
      }
      setRefreshError('')

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

        loadedProjectIdRef.current = projectId
        setOverview(payload)
        setLoadedProjectId(projectId)
        setLastRefreshAt(new Date().toISOString())
        setError('')
        setRefreshError('')
        setState('ready')
      } catch (caught: unknown) {
        if (caught instanceof DOMException && caught.name === 'AbortError') return
        if (!isCurrentProjectLoad(
          projectId,
          activeProjectIdRef.current,
          generation,
          loadGenerationRef.current,
        )) return

        const message = caught instanceof Error ? caught.message : 'Unable to load cockpit overview'
        if (classifyCockpitRefreshFailure(projectId, loadedProjectIdRef.current) === 'stale') {
          setRefreshError(message)
          setState('ready')
        } else {
          setError(message)
          setState('error')
        }
      } finally {
        if (
          !disposed
          && isCurrentProjectLoad(
            projectId,
            activeProjectIdRef.current,
            generation,
            loadGenerationRef.current,
          )
        ) {
          setRefreshing(false)
        }
      }
    }

    const loop = createCockpitRefreshLoop({
      intervalMs: cockpitRefreshIntervalMs,
      refresh: refreshCockpit,
      initialVisible: !document.hidden,
    })
    refreshLoopRef.current = loop

    const handleVisibilityChange = () => {
      loop.setVisible(!document.hidden)
    }
    const handleFocus = () => {
      loop.notifyFocus()
    }

    document.addEventListener('visibilitychange', handleVisibilityChange)
    window.addEventListener('focus', handleFocus)

    loop.start()
    void loop.refreshNow()

    return () => {
      disposed = true
      controller?.abort()
      loop.stop()
      if (refreshLoopRef.current === loop) refreshLoopRef.current = null
      document.removeEventListener('visibilitychange', handleVisibilityChange)
      window.removeEventListener('focus', handleFocus)
    }
  }, [activeProjectId])

  const activeProject = projects.find(project => project.project_id === activeProjectId) ?? null
  const currentOverview = loadedProjectId === activeProjectId ? overview : null
  const freshnessLabel = refreshing
    ? 'Actualisation en cours…'
    : refreshError
      ? 'Données conservées · dernière actualisation en erreur'
      : lastRefreshAt
        ? 'Dernière actualisation réussie ' + new Date(lastRefreshAt).toLocaleString()
        : 'En attente de la première observation'

  return <CockpitRefreshProvider version={surfaceRefreshVersion}>
    <main className="shell">
      <a className="skip-link" href="#cockpit-content">Aller au cockpit</a>
      <div id="cockpit-content" className="panel" tabIndex={-1}>
        <header className="app-header">
          <div>
            <p className="eyebrow">DEVCOCKPIT</p>
            <h1>Delivery cockpit</h1>
            <p className="app-subtitle">Supervision visuelle du roadmap, des rôles et du travail observable.</p>
          </div>
          <div className="app-header-actions">
            <label className="theme-control" htmlFor="theme-preference">
              <span>Thème</span>
              <select
                id="theme-preference"
                aria-label="Préférence de thème"
                value={themePreference}
                onChange={event => themeControllerRef.current?.setPreference(event.target.value as ThemePreference)}
              >
                <option value="system">Système</option>
                <option value="light">Clair</option>
                <option value="dark">Sombre</option>
              </select>
            </label>
            <div className={'health health--' + state} aria-live="polite">
              {state === 'loading' ? 'Reading project context…' : state === 'ready' ? 'Project context loaded' : error}
            </div>
          </div>
        </header>

        {state !== 'ready' && <section
          className={'cockpit-load-state cockpit-load-state--' + state}
          role={state === 'error' ? 'alert' : 'status'}
          aria-live="polite"
          aria-busy={state === 'loading'}
        >
          <strong>{state === 'loading' ? 'Chargement du cockpit…' : 'Cockpit indisponible'}</strong>
          <span>{state === 'loading'
            ? (activeProjectId ? 'Lecture des projections du projet actif.' : 'Lecture des projets configurés.')
            : error}</span>
          {state === 'error' && activeProjectId && <button
            type="button"
            onClick={requestRefreshNow}
          >
            Réessayer
          </button>}
        </section>}

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
          {activeProjectId && <div className="source-statuses" aria-live="polite">
            <span className={'source-chip source-chip--' + (refreshError ? 'unavailable' : 'available')}>
              {freshnessLabel}
            </span>
            {refreshError && <span className="source-chip source-chip--unavailable">{refreshError}</span>}
            <button type="button" disabled={refreshing} onClick={requestRefreshNow}>
              {refreshing ? 'Actualisation…' : 'Actualiser maintenant'}
            </button>
          </div>}
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

        {activeProject && orchestrationWorkItem && <ContextDrawer
          projectId={activeProject.project_id}
          contextKind="work-item"
          contextId={orchestrationWorkItem}
          eyebrow="ORCHESTRATION"
          title={'Orchestration · ' + orchestrationWorkItem}
          onClose={() => setOrchestrationWorkItem('')}
        >
          <p className="drawer-note">
            Vue opérationnelle principale du WorkItem. Les diagnostics bruts restent disponibles séparément.
          </p>
          <Orchestration
            key={activeProject.project_id + ':' + orchestrationWorkItem}
            projectId={activeProject.project_id}
            workItem={orchestrationWorkItem}
          />
        </ContextDrawer>}

        {activeProject && <details
          id="technical-surfaces"
          className="technical-surfaces"
          open={technicalOpen}
          onToggle={event => setTechnicalOpen(event.currentTarget.open)}
        >
          <summary>
            <span>
              <strong>Diagnostics techniques</strong>
              <small>Données brutes scheduler/exécutions, Flow Analytics et réponses importées</small>
            </span>
            <span aria-hidden="true">⌄</span>
          </summary>
          {technicalOpen && <TechnicalSurfaces
            key={'technical:' + activeProject.project_id}
            project={activeProject}
          />}
        </details>}
      </div>
    </main>
  </CockpitRefreshProvider>
}

export default App
