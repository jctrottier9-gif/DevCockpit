import { useEffect, useState } from 'react'
import InteractionStatus from './InteractionStatus'
import type { ParallelExecutionItem, ParallelExecutionsResponse } from './dashboardTypes'

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

function compactSha(value: string | null) {
  return value ? value.slice(0, 10) : '—'
}

function executionWaitingLabel(item: ParallelExecutionItem) {
  if (item.active) return 'Slot actif'
  if (item.waiting_for_capacity) return 'Attente capacité'
  if (item.waiting_for_resource_lock) return 'Attente ResourceLock'
  if (item.inhibition_reason) return 'Inhibé · ' + item.inhibition_reason
  return 'Sélectionné'
}

function DevExecutionCard({
  item,
  onOpenWorkItem,
}: {
  item: ParallelExecutionItem
  onOpenWorkItem: (workItemId: string) => void
}) {
  const key = item.work_item?.key
  const watchdog = item.watchdog

  return <article className="dev-execution-card">
    <div className="dev-execution-card__header">
      <div>
        <p className="eyebrow">DEV EXECUTION</p>
        <h3>{key ?? 'WorkItem inconnu'}</h3>
        <p className="dev-session">{item.agent_session}</p>
      </div>
      <span className={'state-badge state-badge--' + item.slot_state.toLowerCase()}>
        {item.slot_state}
      </span>
    </div>

    <div className="dev-execution-summary">
      <span>{executionWaitingLabel(item)}</span>
      <span>Scheduler · {item.scheduler.state}</span>
      <span>Execution · {item.execution_state}</span>
      <span>Action · {actionLabels[item.next_action] ?? item.next_action}</span>
    </div>

    <dl className="drawer-facts dev-execution-facts">
      <div><dt>Branche</dt><dd>{item.branch ?? '—'}</dd></div>
      <div><dt>Head</dt><dd title={item.head_sha ?? undefined}>{compactSha(item.head_sha)}</dd></div>
      <div>
        <dt>PR</dt>
        <dd>
          {item.pull_request
            ? item.pull_request.url
              ? <a href={item.pull_request.url} target="_blank" rel="noreferrer">#{item.pull_request.number}</a>
              : '#' + item.pull_request.number
            : '—'}
        </dd>
      </div>
      <div><dt>CI</dt><dd>{item.ci?.state ?? 'non observée'}</dd></div>
      <div><dt>Interaction</dt><dd><InteractionStatus interaction={item.interaction} compact /></dd></div>
      <div>
        <dt>Réponse importée</dt>
        <dd>{item.interaction?.imported_response_available ? 'Disponible' : 'Aucune réponse importée'}</dd>
      </div>
    </dl>

    <div className="dev-execution-subsection">
      <strong>ResourceLocks</strong>
      <p>
        Requis : {item.resource_locks.required.map(lock => lock.surface + ' [' + lock.mode + ']').join(', ') || 'aucun'}
      </p>
      <p>
        Détenus : {item.resource_locks.held.map(lock => lock.surface + ' [' + lock.mode + ']').join(', ') || 'aucun'}
      </p>
      {item.resource_locks.conflict && <p className="dev-execution-alert">
        Conflit {item.resource_locks.conflict.surface} avec {item.resource_locks.conflict.holder_work_item_id}
        {' · '}{item.resource_locks.conflict.reason}
      </p>}
      {item.resource_locks.recovery_state && <p>Recovery : {item.resource_locks.recovery_state}</p>}
    </div>

    <div className="dev-execution-subsection">
      <strong>Watchdog</strong>
      {watchdog ? <>
        <p>Dernière activité branche : {new Date(watchdog.branch_last_activity_at).toLocaleString()}</p>
        <p>Seuil : {Math.round(watchdog.threshold_seconds / 60)} min</p>
        <p>Échéance : {watchdog.deadline_at ? new Date(watchdog.deadline_at).toLocaleString() : 'non déterminée'}</p>
        <p>
          État : {watchdog.stale_due ? 'stale détecté' : 'dans la fenêtre'}
          {watchdog.relaunch_prepared ? ' · relance préparée' : ''}
        </p>
      </> : <p>Non applicable avec les preuves actuelles.</p>}
    </div>

    {item.diagnostics.length > 0 && <ul className="diagnostics">
      {item.diagnostics.map(diagnostic => <li key={diagnostic.code}>{diagnostic.code}: {diagnostic.message}</li>)}
    </ul>}

    {key && <button type="button" onClick={() => onOpenWorkItem(key)}>
      Ouvrir l’Orchestration de {key}
    </button>}
  </article>
}

export default function DevPool({
  projectId,
  onOpenWorkItem,
}: {
  projectId: string
  onOpenWorkItem: (workItemId: string) => void
}) {
  const [projection, setProjection] = useState<ParallelExecutionsResponse | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const controller = new AbortController()
    setProjection(null)
    setError('')
    setLoading(true)

    async function load() {
      try {
        const response = await fetch(
          '/api/projects/' + encodeURIComponent(projectId) + '/executions',
          { signal: controller.signal },
        )
        const payload = await response.json() as ParallelExecutionsResponse & { detail?: unknown }
        if (!response.ok) {
          throw new Error(
            payload.source?.code
            ?? (typeof payload.detail === 'string' ? payload.detail : 'DEV Pool indisponible'),
          )
        }
        if (payload.project?.project_id !== projectId) {
          throw new Error('Project context mismatch while loading ' + projectId)
        }
        setProjection(payload)
      } catch (caught: unknown) {
        if (caught instanceof DOMException && caught.name === 'AbortError') return
        setError(caught instanceof Error ? caught.message : 'DEV Pool indisponible')
      } finally {
        if (!controller.signal.aborted) setLoading(false)
      }
    }

    void load()
    return () => controller.abort()
  }, [projectId])

  if (loading) return <p className="muted">Lecture du DEV Pool…</p>
  if (error) return <p role="alert">{error}</p>
  if (!projection) return <p className="muted">Projection DEV indisponible.</p>

  return <section className="dev-pool-detail" aria-label="DEV Pool parallèle">
    {projection.capacity && <dl className="drawer-facts">
      <div><dt>Capacité</dt><dd>{projection.capacity.used} / {projection.capacity.limit}</dd></div>
      <div><dt>Libre</dt><dd>{projection.capacity.available}</dd></div>
      <div><dt>Candidats</dt><dd>{projection.executions.length}</dd></div>
    </dl>}
    <p className="drawer-note">
      « Slot actif » décrit l’occupation de capacité DevCockpit; ce n’est pas une preuve que ChatGPT génère actuellement.
    </p>
    <div className="dev-execution-list">
      {projection.executions.length
        ? projection.executions.map(item => <DevExecutionCard
            key={item.work_item?.key ?? item.agent_session}
            item={item}
            onOpenWorkItem={onOpenWorkItem}
          />)
        : <p className="muted">Aucune exécution DEV projetée.</p>}
    </div>
  </section>
}
