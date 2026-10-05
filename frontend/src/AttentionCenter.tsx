import { useCallback, useEffect, useState } from 'react'
import InteractionStatus from './InteractionStatus'
import type { InteractionSummary } from './dashboardTypes'

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
  const [redelivering, setRedelivering] = useState<string | null>(null)
  const [authorizingGate, setAuthorizingGate] = useState<string | null>(null)
  const [notice, setNotice] = useState('')

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

  async function redeliver(dispatchId: string) {
    setRedelivering(dispatchId)
    setError('')
    setNotice('')
    try {
      const response = await fetch(
        '/api/prompt-dispatches/' + encodeURIComponent(dispatchId) + '/redeliver',
        { method: 'POST' },
      )
      const payload = await response.json() as {
        status?: string
        delivery_id?: string
        detail?: string | { code?: string; message?: string }
      }
      if (!response.ok) {
        const detail = payload.detail
        throw new Error(
          typeof detail === 'string'
            ? detail
            : detail?.message ?? detail?.code ?? 'Renvoi au companion impossible',
        )
      }
      setNotice('Prompt renvoyé au companion Firefox.')
      await refresh()
    } catch (caught: unknown) {
      setError(caught instanceof Error ? caught.message : 'Renvoi au companion impossible')
    } finally {
      setRedelivering(null)
    }
  }

  async function authorizeArchitectureGate(workItemId: string) {
    const confirmed = window.confirm(
      'Autoriser explicitement DevCockpit à préparer le prompt ARCH pour ' + workItemId + ' ?',
    )
    if (!confirmed) return

    setAuthorizingGate(workItemId)
    setError('')
    setNotice('')
    try {
      const response = await fetch(
        '/api/projects/' + encodeURIComponent(projectId)
          + '/architecture-gates/' + encodeURIComponent(workItemId) + '/authorize',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ confirm: true }),
        },
      )
      const payload = await response.json() as {
        status?: string
        dispatch_id?: string
        detail?: string | { code?: string; message?: string }
      }
      if (!response.ok) {
        const detail = payload.detail
        throw new Error(
          typeof detail === 'string'
            ? detail
            : detail?.message ?? detail?.code ?? 'Autorisation de la gate impossible',
        )
      }
      setNotice('Gate architecturale autorisée; le prompt ARCH est maintenant préparé.')
      await refresh()
    } catch (caught: unknown) {
      setError(
        caught instanceof Error ? caught.message : 'Autorisation de la gate impossible',
      )
    } finally {
      setAuthorizingGate(null)
    }
  }

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
    {notice && <p role="status" className="attention-clear">{notice}</p>}

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
        {item.context?.interaction && <p className="attention-meta">
          <InteractionStatus interaction={item.context.interaction as InteractionSummary} compact />
        </p>}
        {item.context && typeof item.context.surface === 'string' && <p className="attention-meta">
          Surface: {String(item.context.surface)} · mode: {String(item.context.requested_mode ?? '—')}
          {' · '}détenteur: {String(item.context.holder_work_item_id ?? '—')}
          {' · '}session: {String(item.context.holder_agent_session ?? '—')}
        </p>}
        <div className="attention-actions">
          {item.primary_action.href
            ? <a href={item.primary_action.href} target="_blank" rel="noreferrer">
                {item.primary_action.label}
              </a>
            : item.primary_action.work_item_id && item.primary_action.target === 'architecture_gate'
              ? <button
                  type="button"
                  disabled={authorizingGate === item.primary_action.work_item_id}
                  onClick={() => void authorizeArchitectureGate(item.primary_action.work_item_id!)}
                >
                  {authorizingGate === item.primary_action.work_item_id
                    ? 'Autorisation…'
                    : item.primary_action.label}
                </button>
              : item.primary_action.work_item_id && item.primary_action.target === 'orchestration'
                ? <button type="button" onClick={() => onOpenWorkItem(item.primary_action.work_item_id!)}>
                    {item.primary_action.label}
                  </button>
                : <strong>{item.primary_action.label}</strong>}
          {item.primary_action.dispatch_id
            && item.context?.delivery_acknowledged === true
            && item.context?.automatic_resend_allowed === true &&
            <button
              type="button"
              disabled={redelivering === item.primary_action.dispatch_id}
              onClick={() => void redeliver(item.primary_action.dispatch_id!)}
            >
              {redelivering === item.primary_action.dispatch_id
                ? 'Renvoi…'
                : 'Renvoyer au companion'}
            </button>}
          {item.pr_number && !item.primary_action.href && item.pr_url &&
            <a href={item.pr_url} target="_blank" rel="noreferrer">PR #{item.pr_number}</a>}
        </div>
      </article>)}
    </div>}
  </section>
}
