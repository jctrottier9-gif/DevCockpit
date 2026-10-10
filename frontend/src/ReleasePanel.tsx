import { useEffect, useMemo, useState } from 'react'
import { useCockpitRefreshVersion } from './CockpitRefreshContext'

type VersionedItem = {
  work_item_id: string
  issue_url: string
  mode: 'RELEASE' | 'HOTFIX' | 'FORWARD_PORT'
  roadmap_status: string
  correction_id: string | null
  linked_work_item: string | null
  release_id: string | null
  release_branch: string | null
  release_origin_sha: string | null
  release_state: string | null
  source_kind: string
  requested_ref: string
  resolved_ref: string
  source_sha: string
  target_branch: string
  accepted_base_sha: string
  work_branch: string
  starting_sha: string
  fingerprint: string
  integrated_commits: string[]
  source_hotfix_pr: number | null
  forward_port_method: string | null
  completion_policy: string
  delivery_state: string
  ci_state: string
  pr: null | {
    number: number
    url: string | null
    head_sha: string
    base_branch: string
    merge_commit_sha: string | null
    merged: boolean
    mergeable_state: string | null
  }
  validations: {
    run_id: number
    name: string
    status: string
    conclusion: string | null
    head_sha: string
    url: string | null
  }[]
  artifact: {
    status: string
    tag: string | null
    source_sha: string | null
    image: string | null
    digest: string | null
    validation_check: string | null
  }
  deployment: string
  sql_compatibility: string
  diagnostics: string[]
}

type ReleasePanelProjection = {
  project: { project_id: string; repository_full_name: string }
  observed_at: string
  source: { status: string; code?: string; revision?: string }
  items: VersionedItem[]
  diagnostics: string[]
}

function shortSha(value: string | null) {
  return value ? value.slice(0, 12) : '—'
}

function VersionDelivery({
  item,
  onOpenWorkItem,
}: {
  item: VersionedItem
  onOpenWorkItem: (id: string) => void
}) {
  return <article className="role-detail-card release-delivery">
    <div className="role-detail-card__heading">
      <div>
        <strong>{item.mode} · {item.work_item_id}</strong>
        <span>Roadmap {item.roadmap_status} · cible {item.target_branch}</span>
      </div>
      <span className={'state-badge state-badge--' + item.delivery_state.toLowerCase()}>
        {item.delivery_state}
      </span>
    </div>
    <dl className="drawer-facts release-facts">
      <div><dt>Source demandée</dt><dd>{item.source_kind} · {item.requested_ref}</dd></div>
      <div><dt>Ref résolue</dt><dd><code>{item.resolved_ref}</code></dd></div>
      <div><dt>Source exacte</dt><dd title={item.source_sha}><code>{item.source_sha}</code></dd></div>
      <div><dt>Branche travail</dt><dd><code>{item.work_branch}</code></dd></div>
      <div><dt>Base PR</dt><dd>{item.target_branch}</dd></div>
      <div><dt>Base acceptée</dt><dd title={item.accepted_base_sha}><code>{shortSha(item.accepted_base_sha)}</code></dd></div>
      <div><dt>CI observée</dt><dd>{item.ci_state}</dd></div>
      <div><dt>PR</dt><dd>{item.pr
        ? <a href={item.pr.url ?? item.issue_url} target="_blank" rel="noreferrer">
            #{item.pr.number} · {item.pr.merged ? 'fusionnée' : 'ouverte'}
          </a>
        : 'Non prouvée'}</dd></div>
      {item.pr && <>
        <div><dt>Head PR</dt><dd title={item.pr.head_sha}><code>{shortSha(item.pr.head_sha)}</code></dd></div>
        <div><dt>Merge SHA</dt><dd title={item.pr.merge_commit_sha ?? undefined}>
          <code>{shortSha(item.pr.merge_commit_sha)}</code>
        </dd></div>
      </>}
      {item.mode !== 'FORWARD_PORT' && <>
        <div><dt>Release maintenue</dt><dd>{item.release_branch ?? '—'} · {item.release_state ?? '—'}</dd></div>
        <div><dt>Origine figée</dt><dd title={item.release_origin_sha ?? undefined}>
          <code>{shortSha(item.release_origin_sha)}</code>
        </dd></div>
      </>}
      {item.mode === 'FORWARD_PORT' && <>
        <div><dt>Hotfix source</dt><dd>{item.linked_work_item} · PR #{item.source_hotfix_pr ?? '—'}</dd></div>
        <div><dt>Méthode</dt><dd>{item.forward_port_method ?? '—'}</dd></div>
        <div><dt>Commits intégrés</dt><dd>
          {item.integrated_commits.length
            ? item.integrated_commits.map(sha => <code key={sha} title={sha}>{shortSha(sha)} </code>)
            : '—'}
        </dd></div>
      </>}
      {item.mode === 'HOTFIX' && <>
        <div><dt>Publication</dt><dd>{item.artifact.status}</dd></div>
        <div><dt>Version / tag</dt><dd>{item.artifact.tag ?? 'Non vérifié'}</dd></div>
        <div><dt>Source image</dt><dd><code>{shortSha(item.artifact.source_sha)}</code></dd></div>
        <div><dt>Image Docker</dt><dd><code>{item.artifact.image ?? 'Non attestée'}</code></dd></div>
        <div><dt>Digest</dt><dd><code>{item.artifact.digest ?? 'Non attesté'}</code></dd></div>
        <div><dt>Validation release</dt><dd>{item.artifact.validation_check ?? 'Non attestée'}</dd></div>
      </>}
      <div><dt>Déploiement</dt><dd>{item.deployment}</dd></div>
      <div><dt>SQL Server</dt><dd>{item.sql_compatibility} · validation externe requise</dd></div>
    </dl>
    {item.validations.length > 0 && <div className="workflow-list" aria-label={'Validations ' + item.work_item_id}>
      {item.validations.map(run => <p key={run.run_id}>
        {run.url
          ? <a href={run.url} target="_blank" rel="noreferrer">{run.name} #{run.run_id}</a>
          : run.name + ' #' + run.run_id}
        {' · '}{run.status}{run.conclusion ? ' / ' + run.conclusion : ''}
        {' · head '}{shortSha(run.head_sha)}
      </p>)}
    </div>}
    {item.diagnostics.length > 0 && <p className="cockpit-warning" role="status">
      {item.diagnostics.join(' · ')}
    </p>}
    <div className="attention-actions">
      <a href={item.issue_url} target="_blank" rel="noreferrer">Contrat / issue GitHub</a>
      <button type="button" onClick={() => onOpenWorkItem(item.work_item_id)}>
        Ouvrir Orchestration
      </button>
    </div>
  </article>
}

export default function ReleasePanel({
  projectId,
  onOpenWorkItem,
}: {
  projectId: string
  onOpenWorkItem: (id: string) => void
}) {
  const refreshVersion = useCockpitRefreshVersion()
  const [panel, setPanel] = useState<ReleasePanelProjection | null>(null)
  const [error, setError] = useState('')
  const [release, setRelease] = useState('all')
  const [query, setQuery] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    setPanel(null)
    setRelease('all')
    setQuery('')
    setError('')

    async function load() {
      try {
        const response = await fetch(
          '/api/projects/' + encodeURIComponent(projectId) + '/release-panel',
          { signal: controller.signal },
        )
        const payload = await response.json() as ReleasePanelProjection
        if (!response.ok) throw new Error(payload.source?.code ?? 'Release panel indisponible')
        if (payload.project?.project_id !== projectId) {
          throw new Error('Contexte projet incohérent pour les releases')
        }
        if (!controller.signal.aborted) setPanel(payload)
      } catch (caught: unknown) {
        if (caught instanceof DOMException && caught.name === 'AbortError') return
        if (!controller.signal.aborted) {
          setError(caught instanceof Error ? caught.message : 'Release panel indisponible')
        }
      }
    }
    void load()
    return () => controller.abort()
  }, [projectId, refreshVersion])

  const releases = useMemo(() => {
    if (!panel) return []
    return Array.from(new Set(panel.items.map(item => item.release_id).filter(
      (id): id is string => Boolean(id),
    ))).sort()
  }, [panel])

  const rows = useMemo(() => {
    if (!panel) return []
    const releaseByCorrection = new Map(
      panel.items.filter(item => item.correction_id && item.release_id)
        .map(item => [item.correction_id!, item.release_id!]),
    )
    const normalized = query.trim().toLowerCase()
    return panel.items.filter(item => {
      const itemRelease = item.release_id ?? releaseByCorrection.get(item.correction_id ?? '') ?? null
      const matchesRelease = release === 'all' || itemRelease === release
      const matchesRef = !normalized || [
        item.work_item_id, item.requested_ref, item.resolved_ref, item.source_sha,
        item.correction_id ?? '', item.work_branch, item.artifact.tag ?? '',
      ].some(value => value.toLowerCase().includes(normalized))
      return matchesRelease && matchesRef
    })
  }, [panel, release, query])

  return <section id="release-supervision" className="release-supervision" aria-labelledby="release-title">
    <div className="cockpit-section-heading">
      <div>
        <p className="eyebrow">MAINTAINED RELEASES</p>
        <h3 id="release-title">Releases · hotfix · forward-port</h3>
        <p>Deux circuits GitHub indépendants; un merge ne prouve ni publication ni déploiement.</p>
      </div>
      {panel && <small>Observé {new Date(panel.observed_at).toLocaleString()}</small>}
    </div>
    {error && <p role="alert" className="panel-error">{error}</p>}
    {!panel && !error && <p className="muted">Lecture des contrats et preuves GitHub…</p>}
    {panel && <>
      {panel.source.status !== 'available' && <p className="cockpit-warning" role="alert">
        Roadmap invalide : {panel.diagnostics.join(', ')}
      </p>}
      {panel.items.length === 0
        ? <p className="muted">Aucun contrat de livraison versionnée accepté n’est configuré pour ce projet.</p>
        : <>
          <div className="release-filters">
            <label htmlFor="release-select">Release maintenue
              <select id="release-select" value={release} onChange={event => setRelease(event.target.value)}>
                <option value="all">Toutes les releases</option>
                {releases.map(id => <option value={id} key={id}>{id}</option>)}
              </select>
            </label>
            <label htmlFor="release-query">Ref, tag, SHA ou WorkItem
              <input id="release-query" value={query} onChange={event => setQuery(event.target.value)}
                placeholder="Filtrer la provenance vérifiée" />
            </label>
          </div>
          <div className="release-cards">
            {rows.length
              ? rows.sort((a, b) =>
                  (a.correction_id ?? a.release_id ?? '').localeCompare(b.correction_id ?? b.release_id ?? '')
                  || ({ RELEASE: 0, HOTFIX: 1, FORWARD_PORT: 2 }[a.mode]
                    - { RELEASE: 0, HOTFIX: 1, FORWARD_PORT: 2 }[b.mode]))
                .map(item => <VersionDelivery key={item.work_item_id} item={item} onOpenWorkItem={onOpenWorkItem} />)
              : <p className="muted">Aucune livraison ne correspond à la sélection.</p>}
          </div>
        </>}
      <p className="drawer-note">
        Source : contrat accepté, roadmap canonique et GitHub. Les validations SQL Server,
        les attestations du registre Docker et la décision de déploiement restent externes.
        Une preuve manquante n’est jamais considérée comme acquise.
      </p>
    </>}
  </section>
}
