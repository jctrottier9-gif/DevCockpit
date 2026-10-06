import type { InteractionSummary } from './dashboardTypes'

const stateLabels: Record<string, string> = {
  PROMPT_PREPARED: 'Prompt préparé',
  PROMPT_CANCELLED: 'Prompt annulé',
  FIREFOX_ACKNOWLEDGED: 'Reçu par Firefox',
  QUEUED: 'En file companion',
  ROUTING: 'Routage ChatGPT',
  WAITING_READY: 'ChatGPT pas prêt',
  SEND_ARMED: 'Envoi armé',
  SENT_CONFIRMED: 'Envoi confirmé',
  RETRYABLE_FAILURE: 'Reprise bornée après échec',
  BLOCKED: 'Envoi bloqué',
  AMBIGUOUS: 'Envoi ambigu',
  RESPONSE_IMPORTED: 'Réponse importée',
}

export function interactionStateLabel(interaction: InteractionSummary | null | undefined) {
  if (!interaction) return 'Aucune interaction observée'
  return stateLabels[interaction.state] ?? interaction.state
}

export default function InteractionStatus({
  interaction,
  compact = false,
}: {
  interaction: InteractionSummary | null | undefined
  compact?: boolean
}) {
  if (!interaction) {
    return <span className="muted">Aucune interaction observée.</span>
  }

  return <span className="interaction-status">
    <strong>{interactionStateLabel(interaction)}</strong>
    {!interaction.imported_response_available && interaction.state === 'SENT_CONFIRMED' &&
      <span> · aucune réponse importée</span>}
    {interaction.manual_send_required &&
      <span> · lancement manuel Firefox requis (onglet ChatGPT en Work mode)</span>}
    {interaction.state === 'AMBIGUOUS' &&
      <span> · vérifier avant toute reprise; aucun renvoi automatique</span>}
    {interaction.send_error_code && <span> · {interaction.send_error_code}</span>}
    {!compact && <>
      <span> · session {interaction.agent_session}</span>
      {interaction.delivery_id && <span> · delivery {interaction.delivery_id}</span>}
    </>}
  </span>
}
