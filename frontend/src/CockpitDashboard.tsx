import { useEffect, useMemo, useState } from 'react'
import ContextDrawer from './ContextDrawer'
import DevPool from './DevPool'
import type {
  CockpitHorizonItem,
  CockpitOverview,
  CockpitRoleSummary,
} from './dashboardTypes'

type DrawerTarget =
  | { projectId: string; kind: 'role'; id: string }
  | { projectId: string; kind: 'work-item'; id: string }

const roleIcons: Record<string, string> = {
  PO: '◎',
  ARCH: '◇',
  REVIEWER: '✓',
  DEV_POOL: '⌘',
}

function StateBadge({ state }: { state: string }) {
  return <span className={'state-badge state-badge--' + state.toLowerCase()}>{state}</span>
}

function HorizonCard({
  label,
  item,
  onOpen,
}: {
  label: string
  item: CockpitHorizonItem | null
  onOpen: (item: CockpitHorizonItem) => void
}) {
  return <article className="horizon-card">
    <span className="horizon-label">{label}</span>
    {item ? <>
      <button type="button" className="horizon-open" onClick={() => onOpen(item)}>
        <strong>{item.key}</strong>
        <span>{item.title}</span>
      </button>
      <div className="horizon-meta">
        <span>{item.status}</span>
        <span>{item.scheduler_state ?? 'scheduler indisponible'}</span>
        {item.expected_role && <span>{item.expected_role}</span>}
      </div>
    </> : <p className="muted">Aucun élément projeté.</p>}
  </article>
}

function RoleCard({
  role,
  onOpen,
}: {
  role: CockpitRoleSummary
  onOpen: () => void
}) {
  return <article className="role-card-shell">
    <button type="button" className="role-card-open" onClick={onOpen}>
      <span className="role-icon" aria-hidden="true">{roleIcons[role.role]}</span>
      <span className="role-card-copy">
        <span className="role-card-heading">
          <strong>{role.label}</strong>
          <StateBadge state={role.state} />
        </span>
        <span className="role-headline">{role.headline}</span>
        <span className="role-detail">{role.detail}</span>
      </span>
    </button>
    <div className="role-card-counts">
      <span>{role.action_count} action</span>
      <span>{role.watch_count} à surveiller</span>
    </div>
  </article>
}

export default function CockpitDashboard({
  overview,
  onOpenWorkItem,
  onOpenTechnical,
}: {
  overview: CockpitOverview
  onOpenWorkItem: (workItemId: string) => void
  onOpenTechnical: (sectionId: string) => void
}) {
  const [drawer, setDrawer] = useState<DrawerTarget | null>(null)
  const projectId = overview.project.project_id

  useEffect(() => {
    setDrawer(null)
  }, [projectId])

  const selectedRole = useMemo(() => {
    if (!drawer || drawer.projectId !== projectId || drawer.kind !== 'role') return null
    return overview.roles.find(role => role.role === drawer.id) ?? null
  }, [drawer, overview.roles, projectId])

  const horizonItems = useMemo(
    () => [overview.horizons.now, ...overview.horizons.parallel, overview.horizons.next]
      .filter((item): item is CockpitHorizonItem => item !== null),
    [overview.horizons],
  )
  const selectedWorkItem = useMemo(() => {
    if (!drawer || drawer.projectId !== projectId || drawer.kind !== 'work-item') return null
    return horizonItems.find(item => item.key === drawer.id) ?? null
  }, [drawer, horizonItems, projectId])

  function openRole(role: string) {
    setDrawer({ projectId, kind: 'role', id: role })
  }

  function openWorkItem(item: CockpitHorizonItem) {
    setDrawer({ projectId, kind: 'work-item', id: item.key })
  }

  const sourceRows = Object.entries(overview.sources)

  return <section className="cockpit-dashboard" aria-labelledby="cockpit-dashboard-title">
    <div className="cockpit-section-heading">
      <div>
        <p className="eyebrow">COCKPIT HYBRIDE</p>
        <h2 id="cockpit-dashboard-title">Supervision par rôles</h2>
        <p>Vue synthèse backend observée {new Date(overview.observed_at).toLocaleString()}.</p>
      </div>
      <div className="source-statuses" aria-label="Disponibilité des sources">
        {sourceRows.map(([name, source]) => <span key={name} className={'source-chip source-chip--' + source.status}>
          {name} · {source.status}
        </span>)}
      </div>
    </div>

    {sourceRows.some(([, source]) => source.status === 'unavailable') && <div className="cockpit-warning" role="status">
      Une ou plusieurs sources sont indisponibles; les cartes concernées restent explicitement dégradées.
    </div>}

    <div className="role-grid">
      {overview.roles.map(role => <RoleCard key={role.role} role={role} onOpen={() => openRole(role.role)} />)}
      <article className="role-card-shell role-card-shell--dev">
        <button type="button" className="role-card-open" onClick={() => openRole('DEV_POOL')}>
          <span className="role-icon" aria-hidden="true">{roleIcons.DEV_POOL}</span>
          <span className="role-card-copy">
            <span className="role-card-heading">
              <strong>DEV Pool</strong>
              <StateBadge state="POOL" />
            </span>
            <span className="role-headline">{overview.dev_pool.active} slot(s) DEV occupé(s)</span>
            <span className="role-detail">
              Capacité {overview.dev_pool.capacity_used ?? '—'} / {overview.dev_pool.capacity_limit ?? '—'} · {overview.dev_pool.candidates} candidat(s)
            </span>
          </span>
        </button>
        {overview.dev_pool.items.length > 0 && <div className="dev-pool-compact" aria-label="Exécutions DEV">
          {overview.dev_pool.items.slice(0, 3).map(item => <button
            type="button"
            key={item.work_item_id}
            className="dev-pool-compact__item"
            onClick={() => onOpenWorkItem(item.work_item_id)}
          >
            <strong>{item.work_item_id}</strong>
            <span>{item.slot_state}</span>
            <small>
              {item.execution_state}
              {item.ci_state ? ' · CI ' + item.ci_state : ''}
              {item.watchdog_stale ? ' · STALE' : ''}
              {item.watchdog_relaunch_prepared ? ' · relance' : ''}
            </small>
          </button>)}
          {overview.dev_pool.items.length > 3 && <span className="dev-pool-compact__more">
            +{overview.dev_pool.items.length - 3} autre(s)
          </span>}
        </div>}
        <div className="role-card-counts">
          <span>{overview.dev_pool.waiting_for_capacity} attente capacité</span>
          <span>{overview.dev_pool.waiting_for_resource_lock} attente lock</span>
        </div>
      </article>
    </div>

    <section className="horizons" aria-labelledby="horizons-title">
      <div className="cockpit-section-heading cockpit-section-heading--compact">
        <div>
          <p className="eyebrow">TRAJECTOIRE</p>
          <h3 id="horizons-title">Maintenant · Parallèle · Ensuite</h3>
        </div>
      </div>
      <div className="horizon-grid">
        <HorizonCard label="Maintenant" item={overview.horizons.now} onOpen={openWorkItem} />
        <article className="horizon-card">
          <span className="horizon-label">Parallèle</span>
          {overview.horizons.parallel.length ? <div className="parallel-horizons">
            {overview.horizons.parallel.map(item => <button
              type="button"
              className="parallel-horizon"
              key={item.key}
              onClick={() => openWorkItem(item)}
            >
              <strong>{item.key}</strong>
              <span>{item.title}</span>
              <small>{item.status} · {item.scheduler_state ?? 'scheduler indisponible'}</small>
            </button>)}
          </div> : <p className="muted">Aucun travail parallèle projeté.</p>}
        </article>
        <HorizonCard label="Ensuite · perspective" item={overview.horizons.next} onOpen={openWorkItem} />
      </div>
    </section>

    {drawer?.kind === 'role' && drawer.id === 'DEV_POOL' && <ContextDrawer
      projectId={projectId}
      contextKind="role"
      contextId="DEV_POOL"
      eyebrow="POINT D’ENTRÉE DEV"
      title="DEV Pool"
      onClose={() => setDrawer(null)}
    >
      <DevPool
        projectId={projectId}
        onOpenWorkItem={workItemId => {
          setDrawer(null)
          onOpenWorkItem(workItemId)
        }}
      />
      <button type="button" onClick={() => { setDrawer(null); onOpenTechnical('technical-executions') }}>
        Voir les exécutions techniques
      </button>
    </ContextDrawer>}

    {selectedRole && <ContextDrawer
      projectId={projectId}
      contextKind="role"
      contextId={selectedRole.role}
      eyebrow="RÔLE"
      title={selectedRole.label}
      onClose={() => setDrawer(null)}
    >
      <StateBadge state={selectedRole.state} />
      <h3>{selectedRole.headline}</h3>
      <p>{selectedRole.detail}</p>
      <dl className="drawer-facts">
        <div><dt>Actions</dt><dd>{selectedRole.action_count}</dd></div>
        <div><dt>Surveillance</dt><dd>{selectedRole.watch_count}</dd></div>
        <div><dt>WorkItem</dt><dd>{selectedRole.primary_work_item_id ?? '—'}</dd></div>
      </dl>
      {selectedRole.primary_work_item_id === overview.horizons.now?.key && <button type="button" onClick={() => { setDrawer(null); onOpenWorkItem(selectedRole.primary_work_item_id!) }}>
        Ouvrir l’Orchestration MAIN
      </button>}
      <button
        type="button"
        onClick={() => { setDrawer(null); onOpenTechnical(selectedRole.role === 'REVIEWER' ? 'technical-executions' : 'technical-scheduler') }}
      >
        Voir les données techniques
      </button>
    </ContextDrawer>}

    {selectedWorkItem && <ContextDrawer
      projectId={projectId}
      contextKind="work-item"
      contextId={selectedWorkItem.key}
      eyebrow="WORKITEM"
      title={selectedWorkItem.key}
      onClose={() => setDrawer(null)}
    >
      <h3>{selectedWorkItem.title}</h3>
      <dl className="drawer-facts">
        <div><dt>Statut canonique</dt><dd>{selectedWorkItem.status}</dd></div>
        <div><dt>Lane</dt><dd>{selectedWorkItem.lane}</dd></div>
        <div><dt>Scheduler</dt><dd>{selectedWorkItem.scheduler_state ?? '—'}</dd></div>
        <div><dt>Rôle attendu</dt><dd>{selectedWorkItem.expected_role ?? '—'}</dd></div>
        <div><dt>Action projetée</dt><dd>{selectedWorkItem.next_action ?? '—'}</dd></div>
      </dl>
      {selectedWorkItem.expected_role === 'DEV'
        ? <button type="button" onClick={() => { setDrawer(null); onOpenWorkItem(selectedWorkItem.key) }}>
            Ouvrir l’Orchestration de {selectedWorkItem.key}
          </button>
        : <p className="drawer-note">Aucune Orchestration DEV n’est proposée pour ce rôle attendu.</p>}
      <p className="drawer-note">« Ensuite » reste une perspective de navigation et n’autorise jamais l’exécution.</p>
    </ContextDrawer>}
  </section>
}
