import { useCallback, useEffect, useRef, useState } from 'react'

type Response = { response_id: string; text: string; imported_at: string }
type Dispatch = { dispatch_id: string; agent_session: string; prompt_text: string; status: string; delivery: null | { acknowledged: boolean } }
type Source = Dispatch & { responses: Response[] }
type Handoff = {
  handoff_id: string; status: string; version: number; question: string; context: string;
  indication: string; resume_held_reason: string | null; cancel_reason: string | null;
  responses: Response[]; request_dispatch: Dispatch; resume_dispatch: Dispatch | null;
  decision: null | { decision_id: string; summary: string; effect: string; accepted_by: string };
  actions: { accept: boolean; cancel: boolean };
}
type View = { handoffs: Handoff[]; dev_sources: Source[]; transport_limitation: string;
  actions: { create_handoff: boolean; automatic_dev_inhibited: boolean } }

function Prompt({ value, label }: { value: Dispatch; label: string }) {
  return <details><summary>{label} · {value.status} · {value.delivery?.acknowledged ? 'Accepté dans Firefox' : 'Non confirmé par Firefox'}</summary>
    <p>{value.agent_session}</p><pre>{value.prompt_text}</pre></details>
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
  // Keep the identity after a failed/uncertain request. Changed content is a new command.
  const pending = useRef<{ content: string; id: string } | null>(null)
  const base = `/api/projects/${encodeURIComponent(projectId)}/work-items/${encodeURIComponent(workItem)}`
  const refresh = useCallback(async () => {
    const result = await fetch(base + '/orchestration')
    if (!result.ok) throw new Error('Consultations indisponibles')
    setView(await result.json() as View)
  }, [base])
  useEffect(() => { void refresh().catch(e => setError(String(e))) }, [refresh])

  async function command(url: string, identityField: string, body: Record<string, unknown>) {
    const content = JSON.stringify({ url, body })
    if (pending.current?.content !== content) pending.current = { content, id: crypto.randomUUID() }
    setBusy(true); setError('')
    try {
      const response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...body, [identityField]: pending.current.id }) })
      const payload = await response.json()
      if (!response.ok) throw new Error(typeof payload.detail === 'string' ? payload.detail : 'Commande invalide')
      pending.current = null
      await refresh()
      return true
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Commande non confirmée; réessayez avec le même contenu')
      return false
    } finally { setBusy(false) }
  }

  const source = view?.dev_sources.find(d => d.dispatch_id === sourceId)
  return <section className="orchestration">
    <h2>Consultation Architecte · {workItem}</h2>
    <button disabled={busy} onClick={() => void refresh().catch(e => setError(String(e)))}>Actualiser les consultations et réponses</button>
    {error && <p role="alert">{error}</p>}
    {view?.actions.automatic_dev_inhibited && <p>Préparation automatique des prompts DEV suspendue. La CI reste observable.</p>}
    <label>Votre nom (attribution de la confirmation)<input value={actor} onChange={e => setActor(e.target.value)} /></label>
    {view?.actions.create_handoff && <button disabled={busy} onClick={() => setCreating(!creating)}>Consulter l’Architecte</button>}
    {creating && view?.actions.create_handoff && <form onSubmit={async e => {
      e.preventDefault()
      if (await command(base + '/handoffs', 'creation_command_id', {
        source_dispatch_id: sourceId, source_response_id: sourceResponseId || null,
        question, context, created_by: actor,
      })) { setCreating(false); setQuestion(''); setContext('') }
    }}>
      <label>Prompt DEV source<select required value={sourceId} onChange={e => { setSourceId(e.target.value); setSourceResponseId('') }}>
        <option value="">Choisir le prompt source</option>
        {view.dev_sources.map(d => <option key={d.dispatch_id} value={d.dispatch_id}>{d.agent_session} · {d.dispatch_id}</option>)}
      </select></label>
      {source && <Prompt value={source} label="Source sélectionnée" />}
      <label>Réponse DEV en contexte (facultative)<select value={sourceResponseId} onChange={e => setSourceResponseId(e.target.value)}>
        <option value="">Aucune réponse sélectionnée</option>
        {source?.responses.map(r => <option key={r.response_id} value={r.response_id}>{r.response_id}</option>)}
      </select></label>
      {source?.responses.filter(r => r.response_id === sourceResponseId).map(r => <pre key={r.response_id}>{r.text}</pre>)}
      <label>Question<textarea required value={question} onChange={e => setQuestion(e.target.value)} /></label>
      <label>Contexte confirmé<textarea required value={context} onChange={e => setContext(e.target.value)} /></label>
      <button disabled={busy || !actor.trim()}>Confirmer la consultation</button>
    </form>}
    {view?.handoffs.map(h => <Consultation key={h.handoff_id} handoff={h} actor={actor} busy={busy} command={command} />)}
    <p>{view?.transport_limitation}</p>
  </section>
}

function Consultation({ handoff: h, actor, busy, command }: { handoff: Handoff; actor: string; busy: boolean;
  command: (url: string, identityField: string, body: Record<string, unknown>) => Promise<boolean> }) {
  const [responseId, setResponseId] = useState('')
  const [summary, setSummary] = useState('')
  const [effect, setEffect] = useState('')
  const [reason, setReason] = useState('')
  return <article className="response-card">
    <h3>{h.status} · {h.indication}</h3><p>{h.question}</p><p>{h.context}</p>
    <Prompt value={h.request_dispatch} label="Prompt ARCH" />
    <h4>Réponses retournées</h4>
    {h.responses.length === 0 && <p>Aucune réponse retournée.</p>}
    {h.responses.map(r => <div key={r.response_id}>
      {h.actions.accept && <label><input type="radio" name={h.handoff_id} checked={responseId === r.response_id}
        onChange={() => setResponseId(r.response_id)} />Choisir cette réponse · {new Date(r.imported_at).toLocaleString()}</label>}
      <pre>{r.text}</pre>
    </div>)}
    {h.actions.accept && <form onSubmit={e => { e.preventDefault(); void command(`/api/handoffs/${h.handoff_id}/decisions`, 'acceptance_command_id', {
      expected_version: h.version, source_response_id: responseId, summary, effect, accepted_by: actor,
    }) }}>
      <label>Conclusion acceptée et contraintes retenues<textarea required value={summary} onChange={e => setSummary(e.target.value)} /></label>
      <label>Effet confirmé<select required value={effect} onChange={e => setEffect(e.target.value)}>
        <option value="">Choisir l’effet</option><option value="CONTINUE_IN_SCOPE">Poursuivre dans le scope autorisé</option>
        <option value="HOLD_FOR_AUTHORIZATION">Attendre une autorisation (ADR, scope, PO ou roadmap)</option>
      </select></label>
      <button disabled={busy || !actor.trim() || !responseId}>Accepter la conclusion et préparer la reprise si autorisée</button>
    </form>}
    {h.decision && <section><h4>Decision acceptée</h4><p>{h.decision.effect} · {h.decision.accepted_by}</p><pre>{h.decision.summary}</pre>
      {h.resume_dispatch ? <Prompt value={h.resume_dispatch} label="Reprise DEV préparée" /> : <p>Reprise retenue : {h.resume_held_reason}</p>}
    </section>}
    {h.actions.cancel && <form onSubmit={e => { e.preventDefault(); void command(`/api/handoffs/${h.handoff_id}/cancel`, 'cancellation_command_id', {
      expected_version: h.version, reason, cancelled_by: actor,
    }) }}><label>Motif d’annulation<input required value={reason} onChange={e => setReason(e.target.value)} /></label>
      <button disabled={busy || !actor.trim()}>Annuler la consultation</button></form>}
    {h.cancel_reason && <p>Annulée : {h.cancel_reason}</p>}
  </article>
}
