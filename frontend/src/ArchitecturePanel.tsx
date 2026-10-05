import { useEffect, useRef, useState } from 'react'
import type {
  ArchitectureAdrDetail,
  ArchitectureGatePanelItem,
  ArchitecturePanelResponse,
} from './dashboardTypes'

export default function ArchitecturePanel({ projectId }: { projectId: string }) {
  const [panel, setPanel] = useState<ArchitecturePanelResponse | null>(null)
  const [error, setError] = useState('')
  const [authorizing, setAuthorizing] = useState('')
  const [adrDetail, setAdrDetail] = useState<ArchitectureAdrDetail | null>(null)
  const [adrLoading, setAdrLoading] = useState('')
  const generationRef = useRef(0)

  async function loadPanel(signal?: AbortSignal) {
    const generation = ++generationRef.current
    const response = await fetch(
      '/api/projects/' + encodeURIComponent(projectId) + '/architecture-panel',
      { signal },
    )
    const payload = (await response.json()) as ArchitecturePanelResponse & { detail?: unknown }
    if (generation !== generationRef.current) return
    if (!response.ok) throw new Error('Architecture panel unavailable')
    if (payload.project?.project_id !== projectId) {
      throw new Error('Project context mismatch while loading architecture panel')
    }
    setPanel(payload)
    setError('')
  }

  useEffect(() => {
    const controller = new AbortController()
    setPanel(null)
    setAdrDetail(null)
    setError('')
    void loadPanel(controller.signal).catch(caught => {
      if (caught instanceof DOMException && caught.name === 'AbortError') return
      setError(caught instanceof Error ? caught.message : 'Architecture panel unavailable')
    })
    return () => {
      generationRef.current += 1
      controller.abort()
    }
  }, [projectId])

  async function authorize(gate: ArchitectureGatePanelItem) {
    if (!gate.can_authorize) return
    if (!window.confirm('Autoriser explicitement la gate ' + gate.work_item_id + ' ?')) return

    setAuthorizing(gate.work_item_id)
    setError('')
    try {
      const response = await fetch(
        '/api/projects/' + encodeURIComponent(projectId)
          + '/architecture-gates/' + encodeURIComponent(gate.work_item_id) + '/authorize',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ confirm: true }),
        },
      )
      if (!response.ok) throw new Error('Architecture gate authorization failed')
      await loadPanel()
    } catch (caught: unknown) {
      setError(caught instanceof Error ? caught.message : 'Architecture gate authorization failed')
    } finally {
      setAuthorizing('')
    }
  }

  async function openAdr(gate: ArchitectureGatePanelItem, adrId: string) {
    const key = gate.work_item_id + ':' + adrId
    setAdrLoading(key)
    setAdrDetail(null)
    setError('')
    try {
      const response = await fetch(
        '/api/projects/' + encodeURIComponent(projectId)
          + '/architecture-panel/gates/' + encodeURIComponent(gate.work_item_id)
          + '/adrs/' + encodeURIComponent(adrId),
      )
      const payload = (await response.json()) as ArchitectureAdrDetail & { detail?: unknown }
      if (!response.ok) throw new Error('ADR detail unavailable')
      setAdrDetail(payload)
    } catch (caught: unknown) {
      setError(caught instanceof Error ? caught.message : 'ADR detail unavailable')
    } finally {
      setAdrLoading('')
    }
  }

  if (error && !panel) return <p className="panel-error">{error}</p>
  if (!panel) return <p className="muted">Chargement des gates d’architecture…</p>

  return <section className="role-detail-panel" aria-label="Gates Architecte">
    <div className="role-detail-heading">
      <div>
        <p className="eyebrow">ARCHITECTURE PANEL</p>
        <h3>Gates et ADR explicites</h3>
      </div>
      <small>Observé {new Date(panel.observed_at).toLocaleString()}</small>
    </div>

    {error && <p className="panel-error" role="status">{error}</p>}
    {panel.diagnostics.length > 0 && <ul className="panel-diagnostics">
      {panel.diagnostics.map((item, index) => <li key={item.code + ':' + index}>
        {item.work_item_id ? item.work_item_id + ' · ' : ''}{item.code} — {item.message}
      </li>)}
    </ul>}

    {panel.gates.length === 0
      ? <p className="muted">Aucune gate ARCH dans le roadmap canonique.</p>
      : <div className="role-detail-list">
        {panel.gates.map(gate => <article key={gate.work_item_id} className="role-detail-card">
          <div className="role-detail-card__heading">
            <div>
              <strong>{gate.work_item_id}</strong>
              <span>{gate.title}</span>
            </div>
            <span className={'state-badge state-badge--' + gate.status.toLowerCase()}>{gate.status}</span>
          </div>

          <dl className="drawer-facts">
            <div><dt>Scheduler</dt><dd>{gate.scheduler_state ?? '—'}</dd></div>
            <div><dt>Lane</dt><dd>{gate.lane}</dd></div>
            <div><dt>Parent</dt><dd>{gate.parent}</dd></div>
            <div>
              <dt>Autorisation</dt>
              <dd>
                {gate.authorization
                  ? 'accordée · ' + gate.authorization.dispatch_id
                  : gate.human_authorization_required
                    ? 'humaine requise'
                    : 'non requise dans cet état'}
              </dd>
            </div>
          </dl>

          {gate.human_authorization_required && <div className="architecture-authorization">
            <strong>Autorisation humaine requise</strong>
            <p>La gate READY est éligible seulement. La consultation de ce panneau ne crée aucun prompt; après autorisation, le lancement ASTRA reste manuel dans le companion sur un onglet ChatGPT déjà en Work mode.</p>
            <button
              type="button"
              disabled={!gate.can_authorize || authorizing === gate.work_item_id}
              onClick={() => void authorize(gate)}
            >
              {authorizing === gate.work_item_id ? 'Autorisation…' : 'Autoriser la gate'}
            </button>
          </div>}

          {gate.authorization && <p className="drawer-note">
            PromptDispatch d’autorisation corrélé : {gate.authorization.agent_session} · {gate.authorization.status}. Lancement ASTRA manuel dans le companion (onglet Work mode).
          </p>}

          <div className="github-links">
            {gate.work_issue_url && <a href={gate.work_issue_url} target="_blank" rel="noreferrer">
              Issue WorkItem #{gate.work_issue_number}
            </a>}
            {gate.parent_issue_url && <a href={gate.parent_issue_url} target="_blank" rel="noreferrer">
              Parent #{gate.parent_issue_number}
            </a>}
          </div>

          <div className="adr-list">
            <strong>ADR explicitement référencés</strong>
            {gate.adrs.length === 0
              ? <p className="muted">Aucun ADR explicite trouvé dans les issues corrélées.</p>
              : gate.adrs.map(adr => <div key={adr.adr_id} className="adr-row">
                <span><strong>{adr.adr_id}</strong> · {adr.title}</span>
                <span>
                  <button
                    type="button"
                    disabled={adrLoading === gate.work_item_id + ':' + adr.adr_id}
                    onClick={() => void openAdr(gate, adr.adr_id)}
                  >
                    Lire
                  </button>
                  <a href={adr.url} target="_blank" rel="noreferrer">GitHub</a>
                </span>
              </div>)}
          </div>
        </article>)}
      </div>}

    {adrDetail && <article className="adr-detail">
      <div className="role-detail-card__heading">
        <strong>{adrDetail.reference.adr_id}</strong>
        <a href={adrDetail.reference.url} target="_blank" rel="noreferrer">Ouvrir sur GitHub</a>
      </div>
      <pre>{adrDetail.content}</pre>
    </article>}
  </section>
}
