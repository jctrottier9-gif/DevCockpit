import { useEffect, useRef, useState } from 'react'
import AttentionCenter from './AttentionCenter'
import FlowAnalytics from './FlowAnalytics'
import Orchestration from './Orchestration'
import {
  ACTIVE_PROJECT_STORAGE_KEY,
  isCurrentProjectLoad,
  resolveActiveProjectId,
} from './projectWorkspace'

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
  replaces?: string | null
  depends_on?: string[]
}

type Diagnostic = {
  code: string
  message: string
  line_number?: number | null
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
    diagnostics: Diagnostic[]
    active_ready_item: WorkItem | null
  }
}

type ImportedResponse = {
  response_id: string
  delivery_id: string
  session: string
  project_id: string
  work_item_id: string
  role: string
  imported_at: string
  text: string
}

type SchedulerItem = {
  key: string
  canonical_status: string
  dependencies: string[]
  unsatisfied_dependencies: string[]
  scheduler_state: string
  reason: string
  expected_role: string | null
  next_action: string
}

type SchedulerResponse = {
  source: { status: 'available' | 'unavailable'; code?: string }
  scheduler: null | {
    valid: boolean
    pipeline_version: number | null
    executable_candidates: string[]
    diagnostics: Diagnostic[]
    work_items: SchedulerItem[]
  }
}

type ParallelExecutionItem = {
  role: string
  agent_session: string
  scheduler: {
    state: string
    reason: string
    dependencies: string[]
    unsatisfied_dependencies: string[]
  }
  slot_state: string
  active: boolean
  waiting_for_capacity: boolean
  waiting_for_resource_lock: boolean
  inhibition_reason: string | null
  resource_locks: {
    required: { surface: string; mode: string }[]
    held: {
      lock_id: string
      surface: string
      mode: string
      state: string
      work_item_id: string
      agent_session: string
      lease_expires_at: string
      version: number
      released_at: string | null
      release_reason: string | null
    }[]
    records: {
      lock_id: string
      surface: string
      mode: string
      state: string
      work_item_id: string
      agent_session: string
      lease_expires_at: string
      version: number
      released_at: string | null
      release_reason: string | null
    }[]
    conflict: null | {
      surface: string
      requested_mode: string
      holder_work_item_id: string
      holder_agent_session: string
      holder_mode: string
      holder_state: string
      reason: string
    }
    recovery_state: string | null
  }
  work_item: WorkItem | null
  execution_state: string
  next_action: string
  branch: string | null
  pull_request: null | {
    number: number
    title: string
    url: string | null
    mergeable: boolean | null
    merged: boolean
  }
  head_sha: string | null
  ci: null | {
    state: string
    observed_runs: number
    failed_jobs: string[]
  }
  diagnostics: Diagnostic[]
}

type ParallelExecutionsResponse = {
  project: Project
  source: { status: 'available' | 'unavailable'; code?: string }
  capacity: null | {
    limit: number
    used: number
    available: number
  }
  executable_candidates: string[]
  executions: ParallelExecutionItem[]
}

const actionLabels: Record<string, string> = {
  START_DEV: 'Démarrer DEV',
  WAIT_FOR_PR: 'Attendre la PR',
  WAIT: 'Attendre',
  FIX_CI: 'Corriger la CI',
  MERGE_PR: 'PR prête à fusionner',
  RECONCILE_ROADMAP: 'Réconcilier le roadmap',
  RESOLVE_BLOCKER: 'Résoudre le blocage',
  NONE: 'Aucune action',
}

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
  const [roadmap, setRoadmap] = useState<RoadmapResponse | null>(null)
  const [executions, setExecutions] = useState<ParallelExecutionsResponse | null>(null)
  const [scheduler, setScheduler] = useState<SchedulerResponse | null>(null)
  const [responses, setResponses] = useState<ImportedResponse[]>([])
  const [error, setError] = useState('')
  const [orchestrationKey, setOrchestrationKey] = useState('')
  const activeProjectIdRef = useRef('')
  const loadGenerationRef = useRef(0)

  function resetProjectView() {
    setLoadedProjectId(null)
    setRoadmap(null)
    setExecutions(null)
    setScheduler(null)
    setResponses([])
    setOrchestrationKey('')
    setError('')
    setState('loading')
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

  useEffect(() => {
    const controller = new AbortController()

    async function loadProjects() {
      try {
        const projectsResponse = await fetch('/api/projects', { signal: controller.signal })
        if (!projectsResponse.ok) {
          throw new Error('Unable to load configured projects')
        }

        const projectsPayload = (await projectsResponse.json()) as { projects: Project[] }
        const configuredProjects = projectsPayload.projects ?? []
        const initialProjectId = resolveActiveProjectId(
          configuredProjects,
          readPreferredProjectId(),
        )
        if (!initialProjectId) {
          throw new Error('No project is configured')
        }

        setProjects(configuredProjects)
        activeProjectIdRef.current = initialProjectId
        setActiveProjectId(initialProjectId)
        persistPreferredProjectId(initialProjectId)
      } catch (caught: unknown) {
        if (caught instanceof DOMException && caught.name === 'AbortError') {
          return
        }
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

    async function loadProject() {
      try {
        const encodedProject = encodeURIComponent(projectId)
        const [roadmapResponse, executionsResponse, schedulerResponse, responsesResponse] = await Promise.all([
          fetch('/api/projects/' + encodedProject + '/roadmap', { signal: controller.signal }),
          fetch('/api/projects/' + encodedProject + '/executions', { signal: controller.signal }),
          fetch('/api/projects/' + encodedProject + '/scheduler', { signal: controller.signal }),
          fetch('/api/projects/' + encodedProject + '/responses', { signal: controller.signal }),
        ])
        const roadmapPayload = (await roadmapResponse.json()) as RoadmapResponse
        const executionsPayload = (await executionsResponse.json()) as ParallelExecutionsResponse
        const schedulerPayload = (await schedulerResponse.json()) as SchedulerResponse
        const responsesPayload = (await responsesResponse.json()) as { responses: ImportedResponse[] }

        if (!isCurrentProjectLoad(
          projectId,
          activeProjectIdRef.current,
          generation,
          loadGenerationRef.current,
        )) {
          return
        }

        if (
          roadmapPayload.project?.project_id !== projectId
          || executionsPayload.project?.project_id !== projectId
          || (responsesPayload.responses ?? []).some(response => response.project_id !== projectId)
        ) {
          throw new Error('Project context mismatch while loading ' + projectId)
        }

        setRoadmap(roadmapPayload)
        setExecutions(executionsPayload)
        setScheduler(schedulerPayload)
        setResponses(responsesPayload.responses ?? [])
        setLoadedProjectId(projectId)

        if (!roadmapResponse.ok) {
          setError(roadmapPayload.source.code ?? 'GitHub roadmap unavailable')
          setState('error')
          return
        }
        if (!executionsResponse.ok) {
          setError(executionsPayload.source.code ?? 'GitHub execution projections unavailable')
          setState('error')
          return
        }
        if (!schedulerResponse.ok) {
          setError(schedulerPayload.source.code ?? 'Scheduler projection unavailable')
          setState('error')
          return
        }
        if (!responsesResponse.ok) {
          setError('Imported ChatGPT responses unavailable')
          setState('error')
          return
        }
        setState('ready')
      } catch (caught: unknown) {
        if (caught instanceof DOMException && caught.name === 'AbortError') {
          return
        }
        if (!isCurrentProjectLoad(
          projectId,
          activeProjectIdRef.current,
          generation,
          loadGenerationRef.current,
        )) {
          return
        }
        setError(caught instanceof Error ? caught.message : 'Unable to load project')
        setState('error')
      }
    }

    void loadProject()
    return () => controller.abort()
  }, [activeProjectId])

  const activeProject = projects.find(project => project.project_id === activeProjectId) ?? null
  const currentRoadmap = loadedProjectId === activeProjectId ? roadmap : null
  const currentExecutions = loadedProjectId === activeProjectId ? executions : null
  const currentScheduler = loadedProjectId === activeProjectId ? scheduler : null
  const currentResponses = loadedProjectId === activeProjectId ? responses : []
  const pipeline = currentRoadmap?.pipeline
  const ready = pipeline?.active_ready_item
  const primaryExecution = currentExecutions?.executions.find(item => item.active)
    ?? currentExecutions?.executions[0]
    ?? null
  const displayedWorkItem = primaryExecution?.work_item ?? ready

  return (
    <main className="shell">
      <section className="panel">
        <p className="eyebrow">DEVCOCKPIT</p>
        <h1>Execution projection</h1>

        {projects.length > 0 && <section className="project-workspace" aria-label="Contexte projet">
          <label htmlFor="active-project">Projet actif
            <select
              id="active-project"
              value={activeProjectId}
              disabled={projects.length === 1}
              onChange={event => selectProject(event.target.value)}
            >
              {projects.map(project => <option key={project.project_id} value={project.project_id}>
                {project.project_id}
              </option>)}
            </select>
          </label>
          {activeProject && <p>
            {activeProject.repository_full_name} · roadmap #{activeProject.roadmap_issue_number}
          </p>}
        </section>}

        {activeProjectId && <AttentionCenter
          key={'attention:' + activeProjectId}
          projectId={activeProjectId}
          onOpenWorkItem={(workItemId) => {
            setOrchestrationKey(workItemId)
            window.requestAnimationFrame(() => {
              document.getElementById('orchestration')?.scrollIntoView({ behavior: 'smooth' })
            })
          }}
        />}
        {activeProject ? (
          <div className="roadmap-summary">
            <div><span>Projet</span><strong>{activeProject.project_id}</strong></div>
            <div><span>Repository</span><strong>{activeProject.repository_full_name}</strong></div>
            <div><span>Roadmap</span><strong>#{activeProject.roadmap_issue_number}</strong></div>
            <div><span>Pipeline</span><strong>{pipeline ? (pipeline.valid ? 'valid' : 'invalid') : 'unavailable'}</strong></div>
            <div><span>WorkItem</span><strong>{displayedWorkItem?.key ?? 'none'}</strong></div>
            <div><span>État</span><strong>{primaryExecution?.execution_state ?? 'unavailable'}</strong></div>
            <div><span>Branche</span><strong>{primaryExecution?.branch ?? '—'}</strong></div>
            <div><span>PR</span><strong>{primaryExecution?.pull_request ? '#' + primaryExecution.pull_request.number : '—'}</strong></div>
            <div><span>CI</span><strong>{primaryExecution?.ci?.state ?? '—'}</strong></div>
            <div><span>Action</span><strong>{primaryExecution ? (actionLabels[primaryExecution.next_action] ?? primaryExecution.next_action) : '—'}</strong></div>
          </div>
        ) : null}
        <div className={'health health--' + state} aria-live="polite">
          {state === 'loading' ? 'Reading project context…' : state === 'ready' ? 'Project context loaded' : error}
        </div>
        {activeProjectId && <FlowAnalytics key={'analytics:' + activeProjectId} projectId={activeProjectId} />}
        {pipeline && !pipeline.valid ? (
          <ul className="diagnostics">
            {pipeline.diagnostics.map((diagnostic) => (
              <li key={diagnostic.code + '-' + (diagnostic.line_number ?? 'global')}>
                {diagnostic.code}: {diagnostic.message}
              </li>
            ))}
          </ul>
        ) : null}
        {primaryExecution && primaryExecution.diagnostics.length > 0 ? (
          <ul className="diagnostics">
            {primaryExecution.diagnostics.map((diagnostic) => (
              <li key={diagnostic.code}>
                {diagnostic.code}: {diagnostic.message}
              </li>
            ))}
          </ul>
        ) : null}
        {currentExecutions?.capacity && <section className="responses">
          <h2>Exécutions DEV parallèles</h2>
          <p>
            DEV capacity: {currentExecutions.capacity.used} / {currentExecutions.capacity.limit}
            {' · '}disponible: {currentExecutions.capacity.available}
          </p>
          <div className="response-list">
            {currentExecutions.executions.map(item => <article className="response-card" key={item.work_item?.key ?? item.agent_session}>
              <div className="response-meta">
                <strong>{item.work_item?.key ?? '—'} · {item.slot_state}</strong>
                <span>{item.agent_session}</span>
                <span>Scheduler: {item.scheduler.state} · {item.scheduler.reason}</span>
                <span>Execution: {item.execution_state} · {actionLabels[item.next_action] ?? item.next_action}</span>
                <span>PR: {item.pull_request ? '#' + item.pull_request.number : '—'} · CI: {item.ci?.state ?? '—'}</span>
                <span>
                  {item.active
                    ? 'Actif'
                    : item.waiting_for_capacity
                      ? 'En attente de capacité'
                      : item.waiting_for_resource_lock
                        ? 'En attente de ResourceLock'
                        : item.inhibition_reason ?? 'Éligible'}
                </span>
                <span>
                  Surfaces requises: {item.resource_locks.required.map(lock => lock.surface + ' [' + lock.mode + ']').join(', ') || '—'}
                </span>
                <span>
                  Surfaces détenues: {item.resource_locks.held.map(lock => lock.surface + ' [' + lock.mode + ']').join(', ') || '—'}
                </span>
                {item.resource_locks.conflict && <span>
                  Conflit: {item.resource_locks.conflict.surface} [{item.resource_locks.conflict.requested_mode}]
                  {' · '}détenu par {item.resource_locks.conflict.holder_work_item_id}
                  {' · '}{item.resource_locks.conflict.reason}
                </span>}
                {item.resource_locks.recovery_state && <span>
                  Recovery: {item.resource_locks.recovery_state}
                </span>}
              </div>
            </article>)}
          </div>
        </section>}
        {currentScheduler?.scheduler && <section className="responses">
          <h2>Scheduler déterministe</h2>
          <p>Pipeline V{currentScheduler.scheduler.pipeline_version ?? '—'} · candidats : {currentScheduler.scheduler.executable_candidates.join(', ') || 'aucun'}</p>
          {currentScheduler.scheduler.diagnostics.length > 0 && <ul className="diagnostics">
            {currentScheduler.scheduler.diagnostics.map(d => <li key={d.code + '-' + (d.line_number ?? 'global')}>{d.code}: {d.message}</li>)}
          </ul>}
          <div className="response-list">
            {currentScheduler.scheduler.work_items.map(item => <article className="response-card" key={item.key}>
              <div className="response-meta">
                <strong>{item.key} · {item.canonical_status}</strong>
                <span>{item.scheduler_state} · {item.reason}</span>
                <span>Dépend de : {item.dependencies.join(', ') || '—'}</span>
                <span>Non satisfaites : {item.unsatisfied_dependencies.join(', ') || '—'}</span>
                <span>Rôle : {item.expected_role ?? '—'} · Action : {item.next_action}</span>
              </div>
            </article>)}
          </div>
        </section>}
        {currentRoadmap && pipeline && <>
          <label>WorkItem à consulter<select value={orchestrationKey || displayedWorkItem?.key || ''} onChange={e => setOrchestrationKey(e.target.value)}>
            <option value="">Choisir un WorkItem</option>
            {pipeline.work_items.map(w => <option key={w.key} value={w.key}>{w.key} · {w.title}</option>)}
          </select></label>
          {(orchestrationKey || displayedWorkItem?.key) && <div id="orchestration"><Orchestration
            key={activeProjectId + ':' + (orchestrationKey || displayedWorkItem?.key)}
            projectId={activeProjectId} workItem={orchestrationKey || displayedWorkItem!.key} /></div>}
        </>}
        <section className="responses">
          <h2>Réponses ChatGPT importées</h2>
          {currentResponses.length === 0 ? (
            <p className="responses-empty">Aucune réponse retournée.</p>
          ) : (
            <div className="response-list">
              {currentResponses.map((response) => (
                <article className="response-card" key={response.response_id}>
                  <div className="response-meta">
                    <strong>{response.work_item_id} · {response.role}</strong>
                    <span>{response.session}</span>
                    <span>{new Date(response.imported_at).toLocaleString()}</span>
                    <span>delivery_id: {response.delivery_id}</span>
                  </div>
                  <pre>{response.text}</pre>
                </article>
              ))}
            </div>
          )}
        </section>
      </section>
    </main>
  )
}

export default App
