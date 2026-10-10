"""Safe agent handoff for version-pinned WorkItems.

DC-075A defines the contract; DC-075B/C guard their own mutations.
"""
from __future__ import annotations

from app.domain.delivery_context import DeliveryContext, DeliveryMode


def dev_target_instructions(context: DeliveryContext | None = None) -> str:
    if context is None:
        return (
            "\nContrat de base DEV : mode NORMAL (héritage sans contexte versionné). "
            "Résous le vrai main GitHub courant, vérifie le dépôt, la branche de travail "
            "et la base de PR main immédiatement avant chaque modification. "
            "N'utilise jamais ce fallback pour RELEASE/HOTFIX/FORWARD_PORT.\n"
        )
    return f"""
Contrat de livraison accepté V{context.schema_version} — {context.mode.value} :
- Repository : {context.repository_full_name} (GitHub repository_id={context.repository_id})
- Issue source de l'acceptation : #{context.delivery_issue_number} ; empreinte du body : {context.accepted_issue_body_sha256}
- WorkItem : {context.work_item_id} ; correction fonctionnelle : {context.correction_id or 'non applicable'}
- Ref demandée : {context.ref_kind.value}:{context.requested_ref}
- Ref résolue : {context.resolved_ref} ; commit initial immuable : {context.source_sha}
- Branche DEV attendue : {context.expected_work_branch} ; SHA initial : {context.starting_sha}
- Base PR obligatoire : {context.expected_pr_base} ; SHA observé : {context.observed_pr_base_sha}
- Release : {context.release_id or 'aucune'} / {context.release_branch or 'aucune'} ; SHA d'origine : {context.release_origin_sha or 'aucun'}
- WorkItem lié : {context.linked_work_item or 'aucun'} ; hotfix PR source : {context.source_hotfix_pr or 'aucune'}
- Commits intégrés à reporter : {', '.join(context.integrated_commits) or 'aucun'} ; méthode : {context.forward_port_method or 'non applicable'}
- Empreinte immuable du contrat : {context.fingerprint()}
Avant de modifier : relis l'issue source, vérifie owner/name ET repository_id,
ref qualifiée, SHA GitHub résolu, branche d'origine, cible PR par NOM et SHA,
ancêtre/commits et éventuelle provenance forward-port. Après toute reprise,
CI rouge, watchdog, synchronisation ou réconciliation, répète les vérifications.
En cas d'écart, ambiguïté, ref absente ou déplacée : ARRÊTE et signale le blocage.
N'improvise pas un rebase de cible, un fallback vers main ni un changement de WorkItem.
Les preuves merge / artefact testable / validation / déploiement sont distinctes.
Aucun déploiement ni rollback SQL implicite.
""" + (
        "\nFORWARD_PORT : vérifie que le hotfix lié est réellement livré et son "
        "commit intégré (squash, rebase ou merge) sur la release. Prépare la "
        "branche depuis le vrai main accepté; reporte uniquement le delta "
        "par git cherry-pick -x pour les commits linéaires, ou adaptation "
        "documentée pour un merge/conflit. Le cherry-pick du merge entier "
        "et le merge de toute la release sont interdits. Si le correctif "
        "est déjà présent, documente une équivalence pour décision humaine, "
        "n'invente pas de commit vide. Crée une seconde PR vers main, "
        "CI/review/merge/reconciliation indépendants; arrête sur ref stale.\n"
        if context.mode is DeliveryMode.FORWARD_PORT else ""
    )


def release_automation_allowed(context: DeliveryContext | None) -> bool:
    """Only NORMAL, HOTFIX and linked FORWARD_PORT can enter execution.

    RELEASE preparation remains explicit. GitHub readers and finalizers
    enforce the independent accepted target, provenance, protection and CI.
    """
    return context is None or context.mode in {
        DeliveryMode.NORMAL, DeliveryMode.HOTFIX, DeliveryMode.FORWARD_PORT,
    }
