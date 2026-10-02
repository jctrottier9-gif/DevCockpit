import { useEffect, useState } from 'react'
import Orchestration from './Orchestration'

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

type ExecutionResponse = {
  project: Project
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

function App() {
  const [state, setState] = useState<LoadState>('loading')
  const [roadmap, setRoadmap] = useState<RoadmapResponse | null>(null)
  const [execution, setExecution] = useState<ExecutionResponse | null>(null)
  const [scheduler, setScheduler] = useState<SchedulerResponse | null>(null)
  const [responses, setResponses] = useState<ImportedResponse[]>([])
  const [error, setError] = useState('')
  const [orchestrationKey, setOrchestrationKey] = useState('')

  useEffect(() => {
    const controller = new AbortController()

    async function loadProject() {
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

        const encodedProject = encodeURIComponent(project.project_id)
        const [roadmapResponse, executionResponse, schedulerResponse, responsesResponse] = await Promise.all([
          fetch('/api/projects/' + encodedProject + '/roadmap', { signal: controller.signal }),
          fetch('/api/projects/' + encodedProject + '/execution', { signal: controller.signal }),
          fetch('/api/projects/' + encodedProject + '/scheduler', { signal: controller.signal }),
          fetch('/api/projects/' + encodedProject + '/responses', { signal: controller.signal }),
        ])
        const roadmapPayload = (await roadmapResponse.json()) as RoadmapResponse
        const executionPayload = (await executionResponse.json()) as ExecutionResponse
        const schedulerPayload = (await schedulerResponse.json()) as SchedulerResponse
        const responsesPayload = (await responsesResponse.json()) as { responses: ImportedResponse[] }
        setRoadmap(roadmapPayload)
        setExecution(executionPayload)
        setScheduler(schedulerPayload)
        setResponses(responsesPayload.responses ?? [])

        if (!roadmapResponse.ok) {
          setError(roadmapPayload.source.code ?? 'GitHub roadmap unavailable')
          setState('error')
          return
        }
        if (!executionResponse.ok) {
          setError('GitHub execution projection unavailable')
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
        setError(caught instanceof Error ? caught.message : 'Unable to load project')
        setState('error')
      }
    }

    void loadProject()
    return () => controller.abort()
  }, [])

  const pipeline = roadmap?.pipeline
  const ready = pipeline?.active_ready_item
  const displayedWorkItem = execution?.work_item ?? ready

  return (
    <main className="shell">
      <section className="panel">
        <p className="eyebrow">DEVCOCKPIT</p>
        <h1>Execution projection</h1>
        {roadmap ? (
          <div className="roadmap-summary">
            <div><span>Projet</span><strong>{roadmap.project.project_id}</strong></div>
            <div><span>Repository</span><strong>{roadmap.project.repository_full_name}</strong></div>
            <div><span>Roadmap</span><strong>#{roadmap.project.roadmap_issue_number}</strong></div>
            <div><span>Pipeline</span><strong>{pipeline ? (pipeline.valid ? 'valid' : 'invalid') : 'unavailable'}</strong></div>
            <div><span>WorkItem</span><strong>{displayedWorkItem?.key ?? 'none'}</strong></div>
            <div><span>État</span><strong>{execution?.execution_state ?? 'unavailable'}</strong></div>
            <div><span>Branche</span><strong>{execution?.branch ?? '—'}</strong></div>
            <div><span>PR</span><strong>{execution?.pull_request ? '#' + execution.pull_request.number : '—'}</strong></div>
            <div><span>CI</span><strong>{execution?.ci?.state ?? '—'}</strong></div>
            <div><span>Action</span><strong>{execution ? (actionLabels[execution.next_action] ?? execution.next_action) : '—'}</strong></div>
          </div>
        ) : null}
        <div className={'health health--' + state} aria-live="polite">
          {state === 'loading' ? 'Reading GitHub execution…' : state === 'ready' ? 'Execution loaded' : error}
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
        {execution && execution.diagnostics.length > 0 ? (
          <ul className="diagnostics">
            {execution.diagnostics.map((diagnostic) => (
              <li key={diagnostic.code}>
                {diagnostic.code}: {diagnostic.message}
              </li>
            ))}
          </ul>
        ) : null}
        {scheduler?.scheduler && <section className="responses">
          <h2>Scheduler déterministe</h2>
          <p>Pipeline V{scheduler.scheduler.pipeline_version ?? '—'} · candidats : {scheduler.scheduler.executable_candidates.join(', ') || 'aucun'}</p>
          {scheduler.scheduler.diagnostics.length > 0 && <ul className="diagnostics">
            {scheduler.scheduler.diagnostics.map(d => <li key={d.code + '-' + (d.line_number ?? 'global')}>{d.code}: {d.message}</li>)}
          </ul>}
          <div className="response-list">
            {scheduler.scheduler.work_items.map(item => <article className="response-card" key={item.key}>
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
        {roadmap && pipeline && <>
          <label>WorkItem à consulter<select value={orchestrationKey || displayedWorkItem?.key || ''} onChange={e => setOrchestrationKey(e.target.value)}>
            <option value="">Choisir un WorkItem</option>
            {pipeline.work_items.map(w => <option key={w.key} value={w.key}>{w.key} · {w.title}</option>)}
          </select></label>
          {(orchestrationKey || displayedWorkItem?.key) && <Orchestration
            key={roadmap.project.project_id + (orchestrationKey || displayedWorkItem?.key)}
            projectId={roadmap.project.project_id} workItem={orchestrationKey || displayedWorkItem!.key} />}
        </>}
        <section className="responses">
          <h2>Réponses ChatGPT importées</h2>
          {responses.length === 0 ? (
            <p className="responses-empty">Aucune réponse retournée.</p>
          ) : (
            <div className="response-list">
              {responses.map((response) => (
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
