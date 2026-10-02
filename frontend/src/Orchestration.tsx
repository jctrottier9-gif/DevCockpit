import { useCallback, useEffect, useRef, useState } from 'react'

type Response = { response_id: string; text: string; imported_at: string }
type Dispatch = {
  dispatch_id: string
  agent_session: string
  prompt_text: string
  status: string
  delivery: null | { acknowledged: boolean }
}
type Source = Dispatch & { responses: Response[] }
type RoadmapApplicationSummary = {
  application_id: string
  status: string
  version: number
  revision: number
  last_remote_body_hash: string | null
}
type ProposalSummary = {
  proposal_id: string
  status: string
  version: number
  current_revision: number
  confirmed_revision: number | null
  confirmed_preview_digest: string | null
  applications: RoadmapApplicationSummary[]
}
type Handoff = {
  handoff_id: string
  target_role: 'ARCH' | 'PO'
  purpose: string
  status: string
  version: number
  question: string
  context: string
  indication: string
  predecessor_handoff_id: string | null
  context_decision_id: string | null
  resume_held_reason: string | null
  cancel_reason: string | null
  responses: Response[]
  request_dispatch: Dispatch
  resume_dispatch: Dispatch | null
  resume_role: 'ARCH' | 'DEV'
  decision: null | {
    decision_id: string
    summary: string
    effect: string
    decision_type: string
    accepted_by: string
    accepts_residual_writeback_risk: boolean
  }
  proposals: ProposalSummary[]
  actions: {
    accept: boolean
    cancel: boolean
    transfer_to_po: boolean
    create_proposal: boolean
  }
}
type HandoffKind = { target_role: 'ARCH' | 'PO'; purpose: string }
type View = {
  handoffs: Handoff[]
  dev_sources: Source[]
  allowed_handoff_kinds: HandoffKind[]
  transport_limitation: string
  actions: { create_handoff: boolean; automatic_dev_inhibited: boolean }
}
type PendingCommand = {
  content: string
  identities: Record<string, string>
}
type Preview = {
  preview_digest: string
  revision: number
  base_body: string
  proposed_body: string
  full_diff: string
  human_text_changes: string
  canonical_pipeline_changes: string
  old_ready: string | null
  new_ready: string | null
  added_items: unknown[]
  changed_items: unknown[]
  superseded_items: string[]
  replaces_relationships: unknown[]
  issue_mappings: Record<string, number>
  blocking_diagnostics: { code: string; message: string }[]
  allowed_actions: string[]
}

function Prompt({ value, label }: { value: Dispatch; label: string }) {
  return <details>
    <summary>
      {label} · {value.status} · {value.delivery?.acknowledged ? 'Accepté dans Firefox' : 'Non confirmé par Firefox'}
    </summary>
    <p>{value.agent_session}</p>
    <pre>{value.prompt_text}</pre>
  </details>
}

function parseOperations(raw: string): Record<string, unknown>[] {
  const value = JSON.parse(raw) as unknown
  if (!Array.isArray(value) || value.some(item => !item || typeof item !== 'object' || Array.isArray(item))) {
    throw new Error('Les opérations doivent être un tableau JSON d’objets structurés.')
  }
  return value as Record<string, unknown>[]
}

export default function Orchestration({ projectId, workItem }: { projectId: string; workItem: string }) {
  const [view, setView] = useState<View | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [creating, setCreating] = useState(false)
  const [sourceId, setSourceId] = useState('')
  const [sourceResponseId, setSourceResponseId] = useState('')
  const [question, setQuestion] = useState('')
  const [context, setContext] = useState('')
  const [actor, setActor] = useState('')
  const [targetRole, setTargetRole] = useState<'ARCH' | 'PO'>('ARCH')
  const [purpose, setPurpose] = useState('TECHNICAL_GUIDANCE')
  const pending = useRef<PendingCommand | null>(null)
  const base = `/api/projects/${encodeURIComponent(projectId)}/work-items/${encodeURIComponent(workItem)}`

  const refresh = useCallback(async () => {
    const result = await fetch(base + '/orchestration')
    if (!result.ok) throw new Error('Consultations indisponibles')
    setView(await result.json() as View)
  }, [base])

  useEffect(() => {
    void refresh().catch(e => setError(String(e)))
  }, [refresh])

  async function command(
    url: string,
    identityField: string,
    body: Record<string, unknown>,
    additionalIdentityFields: string[] = [],
  ): Promise<Record<string, unknown> | null> {
    const content = JSON.stringify({ url, body, additionalIdentityFields })
    if (pending.current?.content !== content) {
      const identities: Record<string, string> = { [identityField]: crypto.randomUUID() }
      for (const field of additionalIdentityFields) identities[field] = crypto.randomUUID()
      pending.current = { content, identities }
    }
    const identities = pending.current?.identities
    if (!identities) throw new Error('Unable to allocate command identities')
    setBusy(true)
    setError('')
    try {
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...body, ...identities }),
      })
      const payload = await response.json() as Record<string, unknown>
      if (!response.ok) {
        throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Commande invalide')
      }
      pending.current = null
      await refresh()
      return payload
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Commande non confirmée; réessayez avec le même contenu')
      return null
    } finally {
      setBusy(false)
    }
  }

  const kinds = view?.allowed_handoff_kinds ?? []
  const purposes = kinds.filter(kind => kind.target_role === targetRole)
  const source = view?.dev_sources.find(dispatch => dispatch.dispatch_id === sourceId)

  function changeRole(role: 'ARCH' | 'PO') {
    setTargetRole(role)
    setPurpose(kinds.find(kind => kind.target_role === role)?.purpose ?? (
      role === 'ARCH' ? 'TECHNICAL_GUIDANCE' : 'PRODUCT_CLARIFICATION'
    ))
  }

  return <section className="orchestration">
    <h2>Consultations · {workItem}</h2>
    <button disabled={busy} onClick={() => void refresh().catch(e => setError(String(e)))}>
      Actualiser les consultations et réponses
    </button>
    {error && <p role="alert">{error}</p>}
    {view?.actions.automatic_dev_inhibited &&
      <p>Préparation automatique des prompts DEV suspendue. La projection GitHub reste observable.</p>}
    <label>
      Votre nom (attribution des confirmations)
      <input value={actor} onChange={event => setActor(event.target.value)} />
    </label>

    {view?.actions.create_handoff &&
      <button disabled={busy} onClick={() => setCreating(!creating)}>Nouvelle consultation</button>}

    {creating && view?.actions.create_handoff && <form onSubmit={async event => {
      event.preventDefault()
      const result = await command(base + '/handoffs', 'creation_command_id', {
        source_dispatch_id: sourceId,
        source_response_id: sourceResponseId || null,
        question,
        context,
        created_by: actor,
        target_role: targetRole,
        purpose,
      })
      if (result) {
        setCreating(false)
        setQuestion('')
        setContext('')
      }
    }}>
      <label>
        Rôle
        <select value={targetRole} onChange={event => changeRole(event.target.value as 'ARCH' | 'PO')}>
          <option value="ARCH">Architecte</option>
          <option value="PO">Product Owner</option>
        </select>
      </label>
      <label>
        Purpose
        <select required value={purpose} onChange={event => setPurpose(event.target.value)}>
          {purposes.map(kind => <option key={kind.purpose} value={kind.purpose}>{kind.purpose}</option>)}
        </select>
      </label>
      <label>
        Prompt DEV source
        <select required value={sourceId} onChange={event => {
          setSourceId(event.target.value)
          setSourceResponseId('')
        }}>
          <option value="">Choisir le prompt source</option>
          {view.dev_sources.map(dispatch =>
            <option key={dispatch.dispatch_id} value={dispatch.dispatch_id}>
              {dispatch.agent_session} · {dispatch.dispatch_id}
            </option>)}
        </select>
      </label>
      {source && <Prompt value={source} label="Source sélectionnée" />}
      <label>
        Réponse DEV en contexte (facultative)
        <select value={sourceResponseId} onChange={event => setSourceResponseId(event.target.value)}>
          <option value="">Aucune réponse sélectionnée</option>
          {source?.responses.map(response =>
            <option key={response.response_id} value={response.response_id}>{response.response_id}</option>)}
        </select>
      </label>
      {source?.responses.filter(response => response.response_id === sourceResponseId)
        .map(response => <pre key={response.response_id}>{response.text}</pre>)}
      <label>Question<textarea required value={question} onChange={event => setQuestion(event.target.value)} /></label>
      <label>Contexte confirmé<textarea required value={context} onChange={event => setContext(event.target.value)} /></label>
      <button disabled={busy || !actor.trim()}>Confirmer la consultation</button>
    </form>}

    {view?.handoffs.map(handoff =>
      <Consultation
        key={handoff.handoff_id}
        handoff={handoff}
        actor={actor}
        busy={busy}
        command={command}
      />)}
    <p>{view?.transport_limitation}</p>
  </section>
}

function Consultation({
  handoff: h,
  actor,
  busy,
  command,
}: {
  handoff: Handoff
  actor: string
  busy: boolean
  command: (
    url: string,
    identityField: string,
    body: Record<string, unknown>,
    additionalIdentityFields?: string[],
  ) => Promise<Record<string, unknown> | null>
}) {
  const [responseId, setResponseId] = useState('')
  const [summary, setSummary] = useState('')
  const [effect, setEffect] = useState('')
  const [acceptWritebackRisk, setAcceptWritebackRisk] = useState(false)
  const [reason, setReason] = useState('')
  const [transferOpen, setTransferOpen] = useState(false)
  const [transferResponseId, setTransferResponseId] = useState('')
  const [transferQuestion, setTransferQuestion] = useState('')
  const [transferContext, setTransferContext] = useState('')
  const [transferPurpose, setTransferPurpose] = useState('PRODUCT_CLARIFICATION')
  const [proposalOpen, setProposalOpen] = useState(false)
  const [proposalOperations, setProposalOperations] = useState('[]')
  const decisionType = h.target_role === 'ARCH'
    ? 'ARCHITECTURE_GUIDANCE'
    : h.purpose === 'ROADMAP_REVIEW'
      ? 'SCOPE_DECISION'
      : 'PRODUCT_CLARIFICATION'

  return <article className="response-card">
    <h3>{h.target_role} · {h.purpose} · {h.status}</h3>
    <p>{h.indication}</p>
    <p><strong>Question :</strong> {h.question}</p>
    <p><strong>Contexte :</strong> {h.context}</p>
    {h.predecessor_handoff_id && <p>Précédent : {h.predecessor_handoff_id}</p>}
    {h.context_decision_id && <p>Decision de contexte : {h.context_decision_id}</p>}
    <Prompt value={h.request_dispatch} label={`Prompt ${h.target_role}`} />

    <h4>Réponses retournées</h4>
    {h.responses.length === 0 && <p>Aucune réponse retournée.</p>}
    {h.responses.map(response => <div key={response.response_id}>
      {h.actions.accept && <label>
        <input
          type="radio"
          name={h.handoff_id}
          checked={responseId === response.response_id}
          onChange={() => setResponseId(response.response_id)}
        />
        Choisir cette réponse · {new Date(response.imported_at).toLocaleString()}
      </label>}
      <pre>{response.text}</pre>
    </div>)}

    {h.actions.accept && <form onSubmit={event => {
      event.preventDefault()
      void command(`/api/handoffs/${h.handoff_id}/decisions`, 'acceptance_command_id', {
        expected_version: h.version,
        source_response_id: responseId,
        summary,
        decision_type: decisionType,
        effect,
        accepted_by: actor,
        accepts_residual_writeback_risk: acceptWritebackRisk,
      })
    }}>
      <label>
        Conclusion acceptée et contraintes retenues
        <textarea required value={summary} onChange={event => setSummary(event.target.value)} />
      </label>
      <label>
        Type de Decision
        <select value={decisionType} disabled>
          <option value={decisionType}>{decisionType}</option>
        </select>
      </label>
      <label>
        Effet confirmé
        <select required value={effect} onChange={event => setEffect(event.target.value)}>
          <option value="">Choisir l’effet</option>
          <option value="CONTINUE_IN_SCOPE">Poursuivre dans le scope autorisé</option>
          <option value="HOLD_FOR_AUTHORIZATION">Attendre une autorisation</option>
        </select>
      </label>
      {h.target_role === 'PO' && h.purpose === 'ROADMAP_REVIEW' && <label>
        <input
          type="checkbox"
          checked={acceptWritebackRisk}
          onChange={event => setAcceptWritebackRisk(event.target.checked)}
        />
        J’accepte explicitement pour MVP-3 la fenêtre de concurrence résiduelle entre le dernier GET GitHub et le PATCH du body.
      </label>}
      <p>Rôle repris si autorisé : <strong>{h.resume_role}</strong></p>
      <button disabled={busy || !actor.trim() || !responseId}>
        Accepter la conclusion
      </button>
    </form>}

    {h.actions.transfer_to_po && <>
      <button disabled={busy} onClick={() => setTransferOpen(!transferOpen)}>Consulter le PO</button>
      {transferOpen && <form onSubmit={event => {
        event.preventDefault()
        void command(`/api/handoffs/${h.handoff_id}/transfer-to-po`, 'transfer_command_id', {
          expected_version: h.version,
          question: transferQuestion,
          context: transferContext,
          created_by: actor,
          purpose: transferPurpose,
          source_response_id: h.status === 'OPEN' ? transferResponseId : null,
        })
      }}>
        {h.status === 'OPEN' && <label>
          Réponse ARCH utilisée comme contexte
          <select required value={transferResponseId} onChange={event => setTransferResponseId(event.target.value)}>
            <option value="">Choisir la réponse</option>
            {h.responses.map(response =>
              <option key={response.response_id} value={response.response_id}>{response.response_id}</option>)}
          </select>
        </label>}
        <label>
          Purpose PO
          <select value={transferPurpose} onChange={event => setTransferPurpose(event.target.value)}>
            <option value="PRODUCT_CLARIFICATION">PRODUCT_CLARIFICATION</option>
            <option value="ROADMAP_REVIEW">ROADMAP_REVIEW</option>
          </select>
        </label>
        <label>Question PO<textarea required value={transferQuestion} onChange={event => setTransferQuestion(event.target.value)} /></label>
        <label>Contexte confirmé<textarea required value={transferContext} onChange={event => setTransferContext(event.target.value)} /></label>
        <button disabled={busy || !actor.trim() || (h.status === 'OPEN' && !transferResponseId)}>
          Confirmer le transfert ARCH → PO
        </button>
      </form>}
    </>}

    {h.decision && <section>
      <h4>Decision acceptée</h4>
      <p>{h.decision.decision_type} · {h.decision.effect} · {h.decision.accepted_by}</p>
      <p>Risque writeback direct : <strong>{h.decision.accepts_residual_writeback_risk ? 'accepté explicitement' : 'non accepté'}</strong></p>
      <pre>{h.decision.summary}</pre>
      {h.resume_dispatch
        ? <Prompt value={h.resume_dispatch} label={`Reprise ${h.resume_role} préparée`} />
        : <p>Reprise retenue : {h.resume_held_reason}</p>}

      {h.actions.create_proposal && h.proposals.length === 0 && <>
        <button disabled={busy} onClick={() => setProposalOpen(!proposalOpen)}>
          Créer une proposition de redécoupage
        </button>
        {proposalOpen && <form onSubmit={event => {
          event.preventDefault()
          try {
            const operations = parseOperations(proposalOperations)
            void command(
              `/api/decisions/${h.decision!.decision_id}/roadmap-change-proposals`,
              'creation_command_id',
              { operations, created_by: actor },
              ['revision_command_id'],
            )
          } catch (error) {
            window.alert(error instanceof Error ? error.message : 'JSON invalide')
          }
        }}>
          <label>
            Opérations structurées
            <textarea
              required
              value={proposalOperations}
              onChange={event => setProposalOperations(event.target.value)}
            />
          </label>
          <button disabled={busy || !actor.trim()}>Créer la proposal DRAFT</button>
        </form>}
      </>}

      {h.proposals.map(proposal =>
        <ProposalEditor
          key={proposal.proposal_id}
          proposal={proposal}
          actor={actor}
          busy={busy}
          command={command}
        />)}
    </section>}

    {h.actions.cancel && <form onSubmit={event => {
      event.preventDefault()
      void command(`/api/handoffs/${h.handoff_id}/cancel`, 'cancellation_command_id', {
        expected_version: h.version,
        reason,
        cancelled_by: actor,
      })
    }}>
      <label>
        Motif d’annulation
        <input required value={reason} onChange={event => setReason(event.target.value)} />
      </label>
      <button disabled={busy || !actor.trim()}>Annuler la consultation</button>
    </form>}
    {h.cancel_reason && <p>Annulée : {h.cancel_reason}</p>}
  </article>
}

function ProposalEditor({
  proposal,
  actor,
  busy,
  command,
}: {
  proposal: ProposalSummary
  actor: string
  busy: boolean
  command: (
    url: string,
    identityField: string,
    body: Record<string, unknown>,
    additionalIdentityFields?: string[],
  ) => Promise<Record<string, unknown> | null>
}) {
  const [operations, setOperations] = useState('[]')
  const [preview, setPreview] = useState<Preview | null>(null)
  const [localError, setLocalError] = useState('')

  async function loadPreview() {
    setLocalError('')
    try {
      const response = await fetch(
        `/api/roadmap-change-proposals/${proposal.proposal_id}/revisions/${proposal.current_revision}/preview`
      )
      const payload = await response.json() as Preview & { detail?: string }
      if (!response.ok) throw new Error(payload.detail ?? 'Preview indisponible')
      setPreview(payload)
    } catch (error) {
      setLocalError(error instanceof Error ? error.message : 'Preview indisponible')
    }
  }

  return <section className="proposal">
    <h4>RoadmapChangeProposal · {proposal.status}</h4>
    <p>{proposal.proposal_id} · revision {proposal.current_revision} · version {proposal.version}</p>
    {localError && <p role="alert">{localError}</p>}
    <button disabled={busy} onClick={() => void loadPreview()}>Voir le preview déterministe</button>

    {proposal.status === 'DRAFT' && <form onSubmit={event => {
      event.preventDefault()
      try {
        void command(
          `/api/roadmap-change-proposals/${proposal.proposal_id}/revisions`,
          'revision_command_id',
          {
            expected_version: proposal.version,
            operations: parseOperations(operations),
            created_by: actor,
          },
        )
      } catch (error) {
        setLocalError(error instanceof Error ? error.message : 'JSON invalide')
      }
    }}>
      <label>
        Nouvelle révision — opérations structurées
        <textarea value={operations} onChange={event => setOperations(event.target.value)} />
      </label>
      <button disabled={busy || !actor.trim()}>Créer une nouvelle révision</button>
    </form>}

    {preview && <div className="preview">
      <p>Digest : <code>{preview.preview_digest}</code></p>
      <p>READY : {preview.old_ready ?? '—'} → {preview.new_ready ?? '—'}</p>
      {preview.blocking_diagnostics.length > 0 && <ul className="diagnostics">
        {preview.blocking_diagnostics.map(item =>
          <li key={item.code}>{item.code}: {item.message}</li>)}
      </ul>}
      <details><summary>Diff complet</summary><pre>{preview.full_diff}</pre></details>
      <details><summary>Texte humain</summary><pre>{preview.human_text_changes}</pre></details>
      <details><summary>Pipeline canonique</summary><pre>{preview.canonical_pipeline_changes}</pre></details>
      <details><summary>Avant</summary><pre>{preview.base_body}</pre></details>
      <details><summary>Après</summary><pre>{preview.proposed_body}</pre></details>
    </div>}
    <p><strong>Aucune écriture GitHub dans DC-041A.</strong> Application GitHub disponible dans DC-041B.</p>
  </section>
}
