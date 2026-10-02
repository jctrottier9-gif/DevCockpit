import { useCallback, useEffect, useState } from 'react'

type AttentionState = 'ACTION' | 'WATCH' | 'CLEAR'

type AttentionItem = {
  stable_key: string
  level: 'ACTION' | 'WATCH'
  kind: string
  title: string
  reason: string
  project_id: string
  work_item_id: string | null
  role: string | null
  agent_session: string | null
  pr_number: number | null
  pr_url: string | null
  primary_action: {
    kind: string
    label: string
    target: string
    work_item_id: string | null
    href: string | null
    dispatch_id: string | null
    handoff_id: string | null
    proposal_id: string | null
    application_id: string | null
  }
  context: Record<string, unknown> | null
}

type AttentionResponse = {
  state: AttentionState
  counts: {
    action: number
    watch: number
  }
  items: AttentionItem[]
}

export default function AttentionCenter({
  projectId,
  onOpenWorkItem,
}: {
  projectId: string
  onOpenWorkItem: (workItemId: string) => void
}) {
  const [projection, setProjection] = useState<AttentionResponse | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    setError('')
    try {
      const response = await fetch(
        '/api/projects/' + encodeURIComponent(projectId) + '/attention',
        { signal },
      )
      const payload = await response.json() as AttentionResponse & { detail?: unknown }
      if (!response.ok) {
        throw new Error(
          typeof payload.detail === 'string'
            ? payload.detail
            : 'Attention Center indisponible',
        )
      }
      setProjection(payload)
    } catch (caught: unknown) {
      if (caught instanceof DOMException && caught.name === 'AbortError') return
      setError(caught instanceof Error ? caught.message : 'Attention Center indisponible')
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    const controller = new AbortController()
    void refresh(controller.signal)
    return () => controller.abort()
  }, [refresh])

  const state = projection?.state ?? 'CLEAR'

  return <section className={'attention-center attention-center--' + state.toLowerCase()} aria-live="polite">
    <div className="attention-header">
      <div>
        <p className="attention-kicker">ATTENTION CENTER</p>
        <h2>{loading ? 'Actualisation…' : state}</h2>
        {projection && <p>
          {projection.counts.action} action · {projection.counts.watch} à surveiller
        </p>}
      </div>
      <button type="button" disabled={loading} onClick={() => void refresh()}>
        Actualiser
      </button>
    </div>

    {error && <p role="alert" className="diagnostics">{error}</p>}

    {!loading && !error && projection?.state === 'CLEAR' &&
      <p className="attention-clear">Aucune action ni surveillance requise maintenant.</p>}

    {projection && projection.items.length > 0 && <div className="attention-list">
      {projection.items.map(item => <article className="attention-item" key={item.stable_key}>
        <div className="attention-item-heading">
          <strong>{item.level}</strong>
          <span>{item.role ?? 'SYSTEM'}{item.work_item_id ? ' · ' + item.work_item_id : ''}</span>
        </div>
        <h3>{item.title}</h3>
        <p>{item.reason}</p>
        {item.agent_session && <p className="attention-meta">{item.agent_session}</p>}
        {item.context?.surface && <p className="attention-meta">
          Surface: {String(item.context.surface)} · mode: {String(item.context.requested_mode ?? '—')}
          {' · '}détenteur: {String(item.context.holder_work_item_id ?? '—')}
          {' · '}session: {String(item.context.holder_agent_session ?? '—')}
        </p>}
        <div className="attention-actions">
          {item.primary_action.href
            ? <a href={item.primary_action.href} target="_blank" rel="noreferrer">
                {item.primary_action.label}
              </a>
            : item.primary_action.work_item_id && item.primary_action.target === 'orchestration'
              ? <button type="button" onClick={() => onOpenWorkItem(item.primary_action.work_item_id!)}>
                  {item.primary_action.label}
                </button>
              : <strong>{item.primary_action.label}</strong>}
          {item.pr_number && !item.primary_action.href && item.pr_url &&
            <a href={item.pr_url} target="_blank" rel="noreferrer">PR #{item.pr_number}</a>}
        </div>
      </article>)}
    </div>}
  </section>
}
