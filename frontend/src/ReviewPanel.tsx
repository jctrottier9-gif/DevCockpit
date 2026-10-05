import { useEffect, useRef, useState } from 'react'
import type { ReviewPanelResponse } from './dashboardTypes'

function stateClass(value: string | null) {
  return (value ?? 'unknown').toLowerCase().replace(/[^a-z0-9_-]/g, '-')
}

export default function ReviewPanel({ projectId }: { projectId: string }) {
  const [panel, setPanel] = useState<ReviewPanelResponse | null>(null)
  const [error, setError] = useState('')
  const generationRef = useRef(0)

  useEffect(() => {
    const controller = new AbortController()
    const generation = ++generationRef.current
    setPanel(null)
    setError('')

    async function load() {
      try {
        const response = await fetch(
          '/api/projects/' + encodeURIComponent(projectId) + '/review-panel',
          { signal: controller.signal },
        )
        const payload = (await response.json()) as ReviewPanelResponse & { detail?: unknown }
        if (generation !== generationRef.current) return
        if (!response.ok) throw new Error('Reviewer panel unavailable')
        if (payload.project?.project_id !== projectId) {
          throw new Error('Project context mismatch while loading Reviewer panel')
        }
        setPanel(payload)
      } catch (caught: unknown) {
        if (caught instanceof DOMException && caught.name === 'AbortError') return
        if (generation !== generationRef.current) return
        setError(caught instanceof Error ? caught.message : 'Reviewer panel unavailable')
      }
    }

    void load()
    return () => {
      generationRef.current += 1
      controller.abort()
    }
  }, [projectId])

  if (error) return <p className="panel-error">{error}</p>
  if (!panel) return <p className="muted">Chargement des PR et validations GitHub…</p>

  return <section className="role-detail-panel" aria-label="Supervision Reviewer">
    <div className="role-detail-heading">
      <div>
        <p className="eyebrow">REVIEW PANEL</p>
        <h3>PR · CI · jobs</h3>
      </div>
      <small>Observé {new Date(panel.observed_at).toLocaleString()}</small>
    </div>

    {!panel.complete && <div className="cockpit-warning" role="status">
      Résultats GitHub partiels : la limite de pagination a été atteinte.
    </div>}
    {panel.diagnostics.length > 0 && <ul className="panel-diagnostics">
      {panel.diagnostics.map((item, index) => <li key={item.code + ':' + index}>
        {item.work_item_id ? item.work_item_id + ' · ' : ''}{item.code} — {item.message}
      </li>)}
    </ul>}

    {panel.pull_requests.length === 0
      ? <p className="muted">Aucune PR ouverte fortement corrélée à un WorkItem actif.</p>
      : <div className="role-detail-list">
        {panel.pull_requests.map(pr => <article key={pr.work_item_id + ':' + pr.number} className="role-detail-card review-pr">
          <div className="role-detail-card__heading">
            <div>
              <strong>{pr.work_item_id} · PR #{pr.number}</strong>
              <span>{pr.work_item_title} · {pr.lane}</span>
            </div>
            <span className={'state-badge state-badge--' + stateClass(pr.ci_state)}>CI {pr.ci_state}</span>
          </div>

          <p><strong>{pr.title}</strong></p>
          <dl className="drawer-facts">
            <div><dt>Head SHA</dt><dd><code>{pr.head_sha}</code></dd></div>
            <div><dt>Branche</dt><dd>{pr.branch}</dd></div>
            <div><dt>Mergeable</dt><dd>{pr.mergeable === null ? 'inconnu' : pr.mergeable ? 'oui' : 'non'}</dd></div>
            <div><dt>Auto-merge</dt><dd>{pr.auto_merge_enabled ? 'armé' : 'non observé'}</dd></div>
          </dl>
          {pr.url && <p><a href={pr.url} target="_blank" rel="noreferrer">Ouvrir la PR sur GitHub</a></p>}

          <div className="workflow-list">
            {pr.workflows.length === 0
              ? <p className="muted">Aucun workflow pull_request observé pour le head courant.</p>
              : pr.workflows.map(run => <article key={run.run_id + ':' + run.attempt} className="workflow-card">
                <div className="workflow-heading">
                  <div>
                    <strong>{run.name}</strong>
                    <span>run {run.run_id} · tentative {run.attempt}</span>
                  </div>
                  <span className={'job-state job-state--' + stateClass(run.conclusion ?? run.status)}>
                    {run.status}{run.conclusion ? ' · ' + run.conclusion : ''}
                  </span>
                </div>
                <p className="workflow-head"><code>{run.head_sha}</code></p>
                {run.url && <a href={run.url} target="_blank" rel="noreferrer">Workflow GitHub</a>}
                {!run.jobs_complete && <p className="cockpit-warning">Jobs partiels pour cette tentative.</p>}
                <div className="job-list">
                  {run.jobs.map(job => <div key={job.job_id} className={'job-row job-row--' + stateClass(job.conclusion ?? job.status)}>
                    <span>
                      <strong>{job.name}</strong>
                      <small>{job.status}{job.conclusion ? ' · ' + job.conclusion : ''}</small>
                    </span>
                    {job.url && <a href={job.url} target="_blank" rel="noreferrer">Détail</a>}
                  </div>)}
                </div>
              </article>)}
          </div>
        </article>)}
      </div>}
  </section>
}
