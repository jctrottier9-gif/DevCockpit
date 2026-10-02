import { useCallback, useEffect, useState } from 'react'

type Metric = {
  seconds: number | null
  observations: number
}

type FlowDiagnostic = {
  code: string
  message: string
  work_item_id: string | null
  pr_number: number | null
}

type FlowAttempt = {
  run_id: number
  name: string
  attempt: number
  head_sha: string
  status: string
  conclusion: string | null
  created_at: string | null
  completed_at: string | null
  url: string | null
}

type FlowDelivery = {
  work_item: { key: string; title: string }
  pr: { number: number; title: string; url: string | null }
  first_commit_at: string | null
  pr_created_at: string | null
  first_green_ci_at: string | null
  merged_at: string | null
  durations: {
    commit_to_pr_seconds: number | null
    pr_to_green_ci_seconds: number | null
    green_ci_to_merge_seconds: number | null
    total_observable_duration_seconds: number | null
  }
  ci: {
    attempt_count: number
    red_attempt_count: number
    recovered_after_red: boolean | null
    attempts: FlowAttempt[]
  }
  missing_data: string[]
  diagnostics: FlowDiagnostic[]
}

type FlowAnalyticsResponse = {
  aggregates: {
    delivery_count: number
    merged_delivery_count: number
    docs_only_excluded_count: number
    merged_at_range: { start: string | null; end: string | null }
    median_commit_to_pr: Metric
    median_pr_to_green_ci: Metric
    median_green_ci_to_merge: Metric
    median_total_observable_duration: Metric
    ci_attempt_count_total: number
    ci_red_attempt_count_total: number
    recovered_after_red_count: number
    recovered_after_red_observations: number
  }
  deliveries: FlowDelivery[]
  exclusions: {
    work_item_id: string
    pr_number: number
    reason: string
    detail: string
  }[]
  diagnostics: FlowDiagnostic[]
}

function formatDuration(seconds: number | null) {
  if (seconds === null) return '— · non observé'
  const totalMinutes = Math.round(seconds / 60)
  const days = Math.floor(totalMinutes / 1440)
  const hours = Math.floor((totalMinutes % 1440) / 60)
  const minutes = totalMinutes % 60
  const parts = []
  if (days) parts.push(days + ' j')
  if (hours) parts.push(hours + ' h')
  if (minutes || parts.length === 0) parts.push(minutes + ' min')
  return parts.join(' ')
}

function formatTimestamp(value: string | null) {
  return value ? new Date(value).toLocaleString() : '— · non observé'
}

function metricLabel(metric: Metric) {
  return formatDuration(metric.seconds) + ' · n=' + metric.observations
}

function recoveryLabel(value: boolean | null) {
  if (value === null) return 'donnée indisponible'
  return value ? 'oui · rouge puis vert observé' : 'non observée'
}

export default function FlowAnalytics({ projectId }: { projectId: string }) {
  const [projection, setProjection] = useState<FlowAnalyticsResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const refresh = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    setError('')
    try {
      const response = await fetch(
        '/api/projects/' + encodeURIComponent(projectId) + '/analytics',
        { signal },
      )
      const payload = await response.json() as FlowAnalyticsResponse & { detail?: unknown }
      if (!response.ok) {
        const detail = payload.detail
        if (detail && typeof detail === 'object' && 'message' in detail) {
          throw new Error(String((detail as { message: unknown }).message))
        }
        throw new Error('Flow Analytics indisponible')
      }
      setProjection(payload)
    } catch (caught: unknown) {
      if (caught instanceof DOMException && caught.name === 'AbortError') return
      setError(caught instanceof Error ? caught.message : 'Flow Analytics indisponible')
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [projectId])

  useEffect(() => {
    const controller = new AbortController()
    void refresh(controller.signal)
    return () => controller.abort()
  }, [refresh])

  const aggregates = projection?.aggregates

  return <section className="flow-analytics" aria-live="polite">
    <div className="attention-header">
      <div>
        <p className="attention-kicker">FLOW ANALYTICS</p>
        <h2>Historique observable de livraison</h2>
        <p>GitHub uniquement · aucune date estimée · aucune note de performance</p>
      </div>
      <button type="button" disabled={loading} onClick={() => void refresh()}>
        Actualiser
      </button>
    </div>

    {loading && <p>Lecture de l'historique GitHub…</p>}
    {error && <p role="alert" className="diagnostics">{error}</p>}

    {aggregates && <>
      <div className="flow-summary">
        <div><span>Livraisons observées</span><strong>{aggregates.delivery_count}</strong></div>
        <div><span>Fusionnées</span><strong>{aggregates.merged_delivery_count}</strong></div>
        <div><span>Docs-only exclues</span><strong>{aggregates.docs_only_excluded_count}</strong></div>
        <div><span>Tentatives CI</span><strong>{aggregates.ci_attempt_count_total}</strong></div>
        <div><span>Tentatives rouges</span><strong>{aggregates.ci_red_attempt_count_total}</strong></div>
        <div>
          <span>Récupérations après rouge</span>
          <strong>{aggregates.recovered_after_red_count} / {aggregates.recovered_after_red_observations}</strong>
        </div>
      </div>

      <div className="flow-metrics">
        <article><span>Médiane commit → PR</span><strong>{metricLabel(aggregates.median_commit_to_pr)}</strong></article>
        <article><span>Médiane PR → CI verte</span><strong>{metricLabel(aggregates.median_pr_to_green_ci)}</strong></article>
        <article><span>Médiane CI verte → merge</span><strong>{metricLabel(aggregates.median_green_ci_to_merge)}</strong></article>
        <article><span>Médiane durée observable</span><strong>{metricLabel(aggregates.median_total_observable_duration)}</strong></article>
      </div>

      <p className="flow-range">
        Plage des merges observés : {formatTimestamp(aggregates.merged_at_range.start)} → {formatTimestamp(aggregates.merged_at_range.end)}
      </p>
    </>}

    {projection && projection.deliveries.length === 0 && !loading && !error &&
      <p className="responses-empty">Aucune livraison DEV observable pour ce projet.</p>}

    {projection && projection.deliveries.length > 0 && <div className="flow-deliveries">
      {projection.deliveries.map(item => <article className="flow-delivery" key={item.work_item.key + '-' + item.pr.number}>
        <div className="flow-delivery-heading">
          <div>
            <strong>{item.work_item.key}</strong>
            <span>{item.work_item.title}</span>
          </div>
          {item.pr.url
            ? <a href={item.pr.url} target="_blank" rel="noreferrer">PR #{item.pr.number}</a>
            : <span>PR #{item.pr.number}</span>}
        </div>

        <div className="flow-timeline">
          <div><span>Premier commit</span><strong>{formatTimestamp(item.first_commit_at)}</strong></div>
          <div><span>Création PR</span><strong>{formatTimestamp(item.pr_created_at)}</strong></div>
          <div><span>Première CI verte</span><strong>{formatTimestamp(item.first_green_ci_at)}</strong></div>
          <div><span>Merge</span><strong>{formatTimestamp(item.merged_at)}</strong></div>
        </div>

        <div className="flow-duration-row">
          <span>commit → PR: <strong>{formatDuration(item.durations.commit_to_pr_seconds)}</strong></span>
          <span>PR → vert: <strong>{formatDuration(item.durations.pr_to_green_ci_seconds)}</strong></span>
          <span>vert → merge: <strong>{formatDuration(item.durations.green_ci_to_merge_seconds)}</strong></span>
          <span>total: <strong>{formatDuration(item.durations.total_observable_duration_seconds)}</strong></span>
        </div>

        <p>
          CI : {item.ci.attempt_count} tentative(s) · {item.ci.red_attempt_count} rouge(s)
          {' · '}récupération après rouge : {recoveryLabel(item.ci.recovered_after_red)}
        </p>

        {item.missing_data.length > 0 && <p className="flow-missing">
          Données indisponibles : {item.missing_data.join(', ')}
        </p>}

        {item.diagnostics.length > 0 && <ul className="diagnostics">
          {item.diagnostics.map((diagnostic, index) =>
            <li key={diagnostic.code + '-' + index}>{diagnostic.code}: {diagnostic.message}</li>)}
        </ul>}

        {item.ci.attempts.length > 0 && <details>
          <summary>Historique CI observable</summary>
          <div className="flow-attempts">
            {item.ci.attempts.map(attempt => <div key={attempt.run_id + '-' + attempt.attempt}>
              <span>{attempt.name} · run {attempt.run_id} · tentative {attempt.attempt}</span>
              <strong>{attempt.status} / {attempt.conclusion ?? 'conclusion indisponible'}</strong>
              <span>{formatTimestamp(attempt.completed_at)}</span>
              {attempt.url && <a href={attempt.url} target="_blank" rel="noreferrer">GitHub Actions</a>}
            </div>)}
          </div>
        </details>}
      </article>)}
    </div>}

    {projection && projection.exclusions.length > 0 && <details>
      <summary>Livraisons docs-only exclues ({projection.exclusions.length})</summary>
      <ul>
        {projection.exclusions.map(item =>
          <li key={item.work_item_id + '-' + item.pr_number}>{item.work_item_id} · PR #{item.pr_number} · {item.detail}</li>)}
      </ul>
    </details>}

    {projection && projection.diagnostics.length > 0 && <ul className="diagnostics">
      {projection.diagnostics.map((diagnostic, index) =>
        <li key={diagnostic.code + '-' + index}>
          {diagnostic.work_item_id ? diagnostic.work_item_id + ' · ' : ''}{diagnostic.code}: {diagnostic.message}
        </li>)}
    </ul>}
  </section>
}
