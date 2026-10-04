import { useEffect, useRef, useState } from 'react'
import FlowAnalytics from './FlowAnalytics'
import Orchestration from './Orchestration'
import { isCurrentProjectLoad } from './projectWorkspace'
import type {
  ImportedResponse,
  ParallelExecutionsResponse,
  Project,
  RoadmapResponse,
  SchedulerResponse,
} from './dashboardTypes'

type LoadState = 'loading' | 'ready' | 'error'

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

export default function TechnicalSurfaces({
  project,
  requestedWorkItem,
}: {
  project: Project
  requestedWorkItem: string
}) {
  const [state, setState] = useState<LoadState>('loading')
  const [roadmap, setRoadmap] = useState<RoadmapResponse | null>(null)
  const [executions, setExecutions] = useState<ParallelExecutionsResponse | null>(null)
  const [scheduler, setScheduler] = useState<SchedulerResponse | null>(null)
  const [responses, setResponses] = useState<ImportedResponse[]>([])
  const [error, setError] = useState('')
  const [orchestrationKey, setOrchestrationKey] = useState(requestedWorkItem)
  const activeProjectIdRef = useRef(project.project_id)
  const loadGenerationRef = useRef(0)

  useEffect(() => {
    setOrchestrationKey(requestedWorkItem)
    if (requestedWorkItem) {
      window.requestAnimationFrame(() => {
        document.getElementById('orchestration')?.scrollIntoView({ behavior: 'smooth' })
      })
    }
  }, [requestedWorkItem])

  useEffect(() => {
    const controller = new AbortController()
    const projectId = project.project_id
    const generation = ++loadGenerationRef.current
    activeProjectIdRef.current = projectId
    setState('loading')
    setRoadmap(null)
    setExecutions(null)
    setScheduler(null)
    setResponses([])
    setError('')

    async function loadTechnicalDetails() {
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
        )) return

        if (
          roadmapPayload.project?.project_id !== projectId
          || executionsPayload.project?.project_id !== projectId
          || (schedulerPayload.project && schedulerPayload.project.project_id !== projectId)
          || (responsesPayload.responses ?? []).some(response => response.project_id !== projectId)
        ) {
          throw new Error('Project context mismatch while loading ' + projectId)
        }

        setRoadmap(roadmapPayload)
        setExecutions(executionsPayload)
        setScheduler(schedulerPayload)
        setResponses(responsesPayload.responses ?? [])

        if (!roadmapResponse.ok) throw new Error(roadmapPayload.source.code ?? 'GitHub roadmap unavailable')
        if (!executionsResponse.ok) throw new Error(executionsPayload.source.code ?? 'GitHub execution projections unavailable')
        if (!schedulerResponse.ok) throw new Error(schedulerPayload.source.code ?? 'Scheduler projection unavailable')
        if (!responsesResponse.ok) throw new Error('Imported ChatGPT responses unavailable')
        setState('ready')
      } catch (caught: unknown) {
        if (caught instanceof DOMException && caught.name === 'AbortError') return
        if (!isCurrentProjectLoad(
          projectId,
          activeProjectIdRef.current,
          generation,
          loadGenerationRef.current,
        )) return
        setError(caught instanceof Error ? caught.message : 'Unable to load technical project details')
        setState('error')
      }
    }

    void loadTechnicalDetails()
    return () => {
      controller.abort()
      loadGenerationRef.current += 1
    }
  }, [project.project_id])

  const pipeline = roadmap?.pipeline
  const ready = pipeline?.active_ready_item
  const primaryExecution = executions?.executions.find(item => item.active)
    ?? executions?.executions[0]
    ?? null
  const displayedWorkItem = primaryExecution?.work_item ?? ready

  return <div className="technical-surface-stack">
    <div className={'health health--' + state} aria-live="polite">
      {state === 'loading' ? 'Lecture des détails techniques…' : state === 'ready' ? 'Détails techniques chargés' : error}
    </div>

    <section className="roadmap-summary" aria-label="Résumé technique">
      <div><span>Projet</span><strong>{project.project_id}</strong></div>
      <div><span>Repository</span><strong>{project.repository_full_name}</strong></div>
      <div><span>Roadmap</span><strong>#{project.roadmap_issue_number}</strong></div>
      <div><span>Pipeline</span><strong>{pipeline ? (pipeline.valid ? 'valid' : 'invalid') : 'unavailable'}</strong></div>
      <div><span>WorkItem</span><strong>{displayedWorkItem?.key ?? 'none'}</strong></div>
      <div><span>État</span><strong>{primaryExecution?.execution_state ?? 'unavailable'}</strong></div>
      <div><span>Branche</span><strong>{primaryExecution?.branch ?? '—'}</strong></div>
      <div><span>PR</span><strong>{primaryExecution?.pull_request ? '#' + primaryExecution.pull_request.number : '—'}</strong></div>
      <div><span>CI</span><strong>{primaryExecution?.ci?.state ?? '—'}</strong></div>
      <div><span>Action</span><strong>{primaryExecution ? (actionLabels[primaryExecution.next_action] ?? primaryExecution.next_action) : '—'}</strong></div>
    </section>

    <div id="technical-analytics"><FlowAnalytics projectId={project.project_id} /></div>

    {pipeline && !pipeline.valid && <ul className="diagnostics">
      {pipeline.diagnostics.map(diagnostic => <li key={diagnostic.code + '-' + (diagnostic.line_number ?? 'global')}>
        {diagnostic.code}: {diagnostic.message}
      </li>)}
    </ul>}

    {primaryExecution && primaryExecution.diagnostics.length > 0 && <ul className="diagnostics">
      {primaryExecution.diagnostics.map(diagnostic => <li key={diagnostic.code}>{diagnostic.code}: {diagnostic.message}</li>)}
    </ul>}

    {executions?.capacity && <section className="responses" id="technical-executions">
      <h2>Exécutions DEV parallèles</h2>
      <p>
        DEV capacity: {executions.capacity.used} / {executions.capacity.limit}
        {' · '}disponible: {executions.capacity.available}
      </p>
      <div className="response-list">
        {executions.executions.map(item => <article className="response-card" key={item.work_item?.key ?? item.agent_session}>
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
            <span>Surfaces requises: {item.resource_locks.required.map(lock => lock.surface + ' [' + lock.mode + ']').join(', ') || '—'}</span>
            <span>Surfaces détenues: {item.resource_locks.held.map(lock => lock.surface + ' [' + lock.mode + ']').join(', ') || '—'}</span>
            {item.resource_locks.conflict && <span>
              Conflit: {item.resource_locks.conflict.surface} [{item.resource_locks.conflict.requested_mode}]
              {' · '}détenu par {item.resource_locks.conflict.holder_work_item_id}
              {' · '}{item.resource_locks.conflict.reason}
            </span>}
            {item.resource_locks.recovery_state && <span>Recovery: {item.resource_locks.recovery_state}</span>}
          </div>
        </article>)}
      </div>
    </section>}

    {scheduler?.scheduler && <section className="responses" id="technical-scheduler">
      <h2>Scheduler déterministe</h2>
      <p>Pipeline V{scheduler.scheduler.pipeline_version ?? '—'} · candidats : {scheduler.scheduler.executable_candidates.join(', ') || 'aucun'}</p>
      {scheduler.scheduler.diagnostics.length > 0 && <ul className="diagnostics">
        {scheduler.scheduler.diagnostics.map(diagnostic => <li key={diagnostic.code + '-' + (diagnostic.line_number ?? 'global')}>
          {diagnostic.code}: {diagnostic.message}
        </li>)}
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

    {pipeline && <section className="orchestration-picker" id="technical-orchestration">
      <label>WorkItem à consulter
        <select value={orchestrationKey || displayedWorkItem?.key || ''} onChange={event => setOrchestrationKey(event.target.value)}>
          <option value="">Choisir un WorkItem</option>
          {pipeline.work_items.map(workItem => <option key={workItem.key} value={workItem.key}>{workItem.key} · {workItem.title}</option>)}
        </select>
      </label>
      {(orchestrationKey || displayedWorkItem?.key) && <div id="orchestration">
        <Orchestration
          key={project.project_id + ':' + (orchestrationKey || displayedWorkItem?.key)}
          projectId={project.project_id}
          workItem={orchestrationKey || displayedWorkItem!.key}
        />
      </div>}
    </section>}

    <section className="responses" id="technical-responses">
      <h2>Réponses ChatGPT importées</h2>
      {responses.length === 0 ? <p className="responses-empty">Aucune réponse retournée.</p> : <div className="response-list">
        {responses.map(response => <article className="response-card" key={response.response_id}>
          <div className="response-meta">
            <strong>{response.work_item_id} · {response.role}</strong>
            <span>{response.session}</span>
            <span>{new Date(response.imported_at).toLocaleString()}</span>
            <span>delivery_id: {response.delivery_id}</span>
          </div>
          <pre>{response.text}</pre>
        </article>)}
      </div>}
    </section>
  </div>
}
