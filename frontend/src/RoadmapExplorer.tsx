import { useEffect, useMemo, useRef, useState } from 'react'
import { useCockpitRefreshVersion } from './CockpitRefreshContext'
import type {
  RoadmapExplorerIssueDetail,
  RoadmapExplorerItem,
  RoadmapExplorerResponse,
} from './dashboardTypes'

type LoadState = 'loading' | 'ready' | 'error'
type IssueLoadState = 'idle' | 'loading' | 'ready' | 'error'

function ItemButton({
  item,
  selected,
  onSelect,
}: {
  item: RoadmapExplorerItem
  selected: boolean
  onSelect: () => void
}) {
  return <button
    type="button"
    className={'roadmap-explorer-item' + (selected ? ' roadmap-explorer-item--selected' : '')}
    onClick={onSelect}
  >
    <span className="roadmap-explorer-item__heading">
      <strong>{item.key}</strong>
      <span>{item.status}</span>
      <span>{item.lane}</span>
    </span>
    <span>{item.title}</span>
    <small>
      {item.scheduler_state ?? 'scheduler indisponible'}
      {item.depends_on.length ? ' · dépend de ' + item.depends_on.join(', ') : ''}
    </small>
  </button>
}

function Section({
  label,
  keys,
  itemByKey,
  selectedKey,
  onSelect,
  open,
}: {
  label: string
  keys: string[]
  itemByKey: Map<string, RoadmapExplorerItem>
  selectedKey: string | null
  onSelect: (key: string) => void
  open?: boolean
}) {
  return <details className="roadmap-explorer-section" open={open}>
    <summary>
      <strong>{label}</strong>
      <span>{keys.length}</span>
    </summary>
    {keys.length ? <div className="roadmap-explorer-list">
      {keys.map(key => {
        const item = itemByKey.get(key)
        return item ? <ItemButton
          key={key}
          item={item}
          selected={selectedKey === key}
          onSelect={() => onSelect(key)}
        /> : null
      })}
    </div> : <p className="muted">Aucun WorkItem dans cette section.</p>}
  </details>
}

export default function RoadmapExplorer({
  projectId,
  initialWorkItemId,
}: {
  projectId: string
  initialWorkItemId?: string | null
}) {
  const refreshVersion = useCockpitRefreshVersion()
  const [state, setState] = useState<LoadState>('loading')
  const [explorer, setExplorer] = useState<RoadmapExplorerResponse | null>(null)
  const [error, setError] = useState('')
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [issueState, setIssueState] = useState<IssueLoadState>('idle')
  const [issueDetail, setIssueDetail] = useState<RoadmapExplorerIssueDetail | null>(null)
  const [issueError, setIssueError] = useState('')
  const projectRef = useRef(projectId)
  const explorerRef = useRef<RoadmapExplorerResponse | null>(null)
  const explorerGenerationRef = useRef(0)
  const issueGenerationRef = useRef(0)
  const issueControllerRef = useRef<AbortController | null>(null)

  useEffect(() => {
    projectRef.current = projectId
    const controller = new AbortController()
    const generation = ++explorerGenerationRef.current
    const hadExplorer = explorerRef.current !== null
    if (!hadExplorer) setState('loading')
    setError('')

    async function load() {
      try {
        const response = await fetch(
          '/api/projects/' + encodeURIComponent(projectId) + '/roadmap-explorer',
          { signal: controller.signal },
        )
        const payload = await response.json() as RoadmapExplorerResponse & { detail?: unknown }
        if (
          controller.signal.aborted
          || generation !== explorerGenerationRef.current
          || projectRef.current !== projectId
        ) return
        if (!response.ok) {
          throw new Error(
            typeof payload.detail === 'string'
              ? payload.detail
              : 'Roadmap Explorer indisponible',
          )
        }
        if (payload.project?.project_id !== projectId) {
          throw new Error('Project context mismatch while loading Roadmap Explorer')
        }
        explorerRef.current = payload
        setExplorer(payload)
        const preferred = initialWorkItemId && payload.items.some(item => item.key === initialWorkItemId)
          ? initialWorkItemId
          : payload.horizons.now
        setSelectedKey(current => current && payload.items.some(item => item.key === current)
          ? current
          : preferred ?? payload.items[0]?.key ?? null)
        setState('ready')
      } catch (caught: unknown) {
        if (caught instanceof DOMException && caught.name === 'AbortError') return
        if (
          generation !== explorerGenerationRef.current
          || projectRef.current !== projectId
        ) return
        setError(caught instanceof Error ? caught.message : 'Roadmap Explorer indisponible')
        setState(hadExplorer ? 'ready' : 'error')
      }
    }

    void load()
    return () => controller.abort()
  }, [projectId, initialWorkItemId, refreshVersion])

  useEffect(() => () => issueControllerRef.current?.abort(), [])

  const itemByKey = useMemo(
    () => new Map<string, RoadmapExplorerItem>(
      explorer?.items.map(item => [item.key, item] as const) ?? [],
    ),
    [explorer],
  )
  const selectedItem = selectedKey ? itemByKey.get(selectedKey) ?? null : null

  async function loadIssue(issueNumber: number) {
    issueControllerRef.current?.abort()
    const controller = new AbortController()
    issueControllerRef.current = controller
    const generation = ++issueGenerationRef.current
    const requestedProject = projectId
    setIssueState('loading')
    setIssueDetail(null)
    setIssueError('')

    try {
      const response = await fetch(
        '/api/projects/' + encodeURIComponent(projectId)
          + '/roadmap-explorer/issues/' + encodeURIComponent(String(issueNumber)),
        { signal: controller.signal },
      )
      const payload = await response.json() as RoadmapExplorerIssueDetail & { detail?: unknown }
      if (
        controller.signal.aborted
        || generation !== issueGenerationRef.current
        || projectRef.current !== requestedProject
      ) return
      if (!response.ok) {
        const apiDetail = payload.detail
        throw new Error(
          typeof apiDetail === 'string'
            ? apiDetail
            : 'Issue GitHub indisponible',
        )
      }
      setIssueDetail(payload)
      setIssueState('ready')
    } catch (caught: unknown) {
      if (caught instanceof DOMException && caught.name === 'AbortError') return
      if (
        generation !== issueGenerationRef.current
        || projectRef.current !== requestedProject
      ) return
      setIssueError(caught instanceof Error ? caught.message : 'Issue GitHub indisponible')
      setIssueState('error')
    }
  }

  function selectItem(key: string) {
    issueGenerationRef.current += 1
    issueControllerRef.current?.abort()
    setSelectedKey(key)
    setIssueState('idle')
    setIssueDetail(null)
    setIssueError('')
  }

  if (state === 'loading') return <p className="muted">Chargement du Roadmap Explorer…</p>
  if (state === 'error') return <div className="roadmap-explorer-warning" role="alert">{error}</div>
  if (!explorer) return null

  const diagnostics = [
    ...explorer.pipeline.diagnostics,
    ...explorer.scheduler.diagnostics,
    ...explorer.issue_mapping_diagnostics,
  ]

  return <section className="roadmap-explorer">
    <header className="roadmap-explorer-header">
      <div>
        <p className="eyebrow">ROADMAP EXPLORER</p>
        <h3>Pipeline V{explorer.pipeline.version ?? '—'}</h3>
        <p>
          Projection backend observée {new Date(explorer.observed_at).toLocaleString()}.
          « Ensuite » reste une perspective et n’autorise jamais l’exécution.
        </p>
      </div>
      <a href={explorer.source.url} target="_blank" rel="noreferrer">
        Roadmap #{explorer.source.issue_number} ↗
      </a>
    </header>

    {!explorer.pipeline.valid && <div className="roadmap-explorer-warning" role="alert">
      Pipeline invalide : le scheduler reste fail-closed.
    </div>}

    {error && <div className="roadmap-explorer-warning" role="status">{error}</div>}

    {diagnostics.length > 0 && <details className="roadmap-explorer-diagnostics" open={!explorer.pipeline.valid}>
      <summary>Diagnostics ({diagnostics.length})</summary>
      <ul>
        {diagnostics.map((diagnostic, index) => <li key={diagnostic.code + ':' + index}>
          <strong>{diagnostic.code}</strong> — {diagnostic.message}
        </li>)}
      </ul>
    </details>}

    <div className="roadmap-explorer-sections">
      <Section
        label="Maintenant"
        keys={explorer.horizons.now ? [explorer.horizons.now] : []}
        itemByKey={itemByKey}
        selectedKey={selectedKey}
        onSelect={selectItem}
        open
      />
      <Section
        label="Parallèle"
        keys={explorer.horizons.parallel}
        itemByKey={itemByKey}
        selectedKey={selectedKey}
        onSelect={selectItem}
        open
      />
      <Section
        label="Ensuite · perspective"
        keys={explorer.horizons.next ? [explorer.horizons.next] : []}
        itemByKey={itemByKey}
        selectedKey={selectedKey}
        onSelect={selectItem}
        open
      />
      <Section
        label="Plus tard"
        keys={explorer.horizons.later}
        itemByKey={itemByKey}
        selectedKey={selectedKey}
        onSelect={selectItem}
      />
      <Section
        label="Historique"
        keys={explorer.horizons.history}
        itemByKey={itemByKey}
        selectedKey={selectedKey}
        onSelect={selectItem}
      />
    </div>

    {selectedItem && <article className="roadmap-explorer-detail">
      <div className="roadmap-explorer-detail__heading">
        <div>
          <strong>{selectedItem.key}</strong>
          <h4>{selectedItem.title}</h4>
        </div>
        <span>{selectedItem.status} · {selectedItem.lane}</span>
      </div>

      <dl className="drawer-facts">
        <div><dt>Scheduler</dt><dd>{selectedItem.scheduler_state ?? '—'} · {selectedItem.scheduler_reason ?? '—'}</dd></div>
        <div><dt>Dépendances</dt><dd>{selectedItem.depends_on.length ? selectedItem.depends_on.join(', ') : '—'}</dd></div>
        <div><dt>Bloquantes</dt><dd>{selectedItem.unsatisfied_dependencies.length ? selectedItem.unsatisfied_dependencies.join(', ') : '—'}</dd></div>
        <div><dt>Rôle attendu</dt><dd>{selectedItem.expected_role ?? '—'}</dd></div>
        <div><dt>Action projetée</dt><dd>{selectedItem.next_action ?? '—'}</dd></div>
      </dl>

      <div className="roadmap-issue-links">
        <div>
          <span>Issue de livraison</span>
          {selectedItem.work_issue ? <>
            <button type="button" onClick={() => void loadIssue(selectedItem.work_issue!.number)}>
              Consulter #{selectedItem.work_issue.number}
            </button>
            <a href={selectedItem.work_issue.url} target="_blank" rel="noreferrer">GitHub ↗</a>
          </> : <strong>Mapping absent ou ambigu</strong>}
        </div>
        <div>
          <span>Issue parent</span>
          <button type="button" onClick={() => void loadIssue(selectedItem.parent_issue.number)}>
            Consulter #{selectedItem.parent_issue.number}
          </button>
          <a href={selectedItem.parent_issue.url} target="_blank" rel="noreferrer">GitHub ↗</a>
        </div>
      </div>

      {!selectedItem.work_issue && <p className="drawer-note">
        L’absence de mapping documentaire n’affecte jamais l’éligibilité du pipeline.
      </p>}
    </article>}

    {issueState === 'loading' && <p className="muted">Chargement de l’issue GitHub…</p>}
    {issueState === 'error' && <div className="roadmap-explorer-warning" role="alert">{issueError}</div>}
    {issueState === 'ready' && issueDetail && <details className="roadmap-issue-detail" open>
      <summary>
        <strong>#{issueDetail.number} — {issueDetail.title}</strong>
        <span>{issueDetail.state}</span>
      </summary>
      <div className="roadmap-issue-detail__meta">
        <span>MAJ {issueDetail.updated_at ? new Date(issueDetail.updated_at).toLocaleString() : 'inconnue'}</span>
        <a href={issueDetail.url} target="_blank" rel="noreferrer">Ouvrir sur GitHub ↗</a>
      </div>
      <pre className="roadmap-issue-body">{issueDetail.body || 'Issue sans description.'}</pre>
    </details>}
  </section>
}
