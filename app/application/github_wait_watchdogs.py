from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from hashlib import sha256

from app.domain.execution import CiState, ExecutionProjection, ExecutionState
from app.domain.project import Project


class GitHubWaitWatchdogKind(StrEnum):
    PR_NO_CI = "PR_NO_CI"
    CI_STALLED = "CI_STALLED"
    AUTO_MERGE_GRACE = "AUTO_MERGE_GRACE"


@dataclass(frozen=True, slots=True)
class GitHubWaitWatchdog:
    kind: GitHubWaitWatchdogKind
    last_activity_at: str
    threshold_seconds: float
    deadline_at: datetime
    due: bool
    evidence_identity: str
    recovery_state: str
    recovery_prepared: bool = False


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def parse_github_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return _as_utc(parsed)


def _latest_activity(*values: str | None) -> tuple[datetime, str] | None:
    parsed = [(item, value) for value in values if (item := parse_github_timestamp(value)) is not None]
    if not parsed:
        return None
    latest, raw = max(parsed, key=lambda item: item[0])
    return latest, raw


def _bounded_identity(raw: str) -> str:
    if len(raw) <= 180:
        return raw
    return sha256(raw.encode("utf-8")).hexdigest()


def project_execution_github_wait_watchdog(
    execution: ExecutionProjection,
    *,
    now: datetime,
    pr_no_ci_after_seconds: float,
    ci_stall_after_seconds: float,
    auto_merge_grace_seconds: float,
) -> GitHubWaitWatchdog | None:
    pull_request = execution.pull_request
    ci = execution.ci
    if pull_request is None or ci is None:
        return None

    current = _as_utc(now)
    kind: GitHubWaitWatchdogKind
    threshold: float
    activity: tuple[datetime, str] | None
    evidence_parts: list[str]

    if execution.state is ExecutionState.PR_OPEN and ci.state is CiState.NOT_OBSERVED:
        kind = GitHubWaitWatchdogKind.PR_NO_CI
        threshold = pr_no_ci_after_seconds
        activity = _latest_activity(pull_request.created_at, pull_request.updated_at)
        evidence_parts = [
            str(pull_request.number),
            pull_request.head_sha,
            pull_request.base_sha or "",
            pull_request.created_at or "",
            pull_request.updated_at or "",
        ]
    elif execution.state is ExecutionState.CI_RUNNING and ci.state is CiState.RUNNING:
        kind = GitHubWaitWatchdogKind.CI_STALLED
        threshold = ci_stall_after_seconds
        run_times = [
            value
            for run in ci.runs
            for value in (run.created_at, run.updated_at)
        ]
        activity = _latest_activity(*run_times)
        evidence_parts = [
            str(pull_request.number),
            pull_request.head_sha,
            *[
                f"{run.run_id}:{run.attempt}:{run.status}:{run.conclusion or ''}:{run.created_at or ''}:{run.updated_at or ''}"
                for run in sorted(ci.runs, key=lambda item: (item.run_id, item.attempt))
            ],
        ]
    elif (
        execution.state is ExecutionState.READY_TO_MERGE
        and pull_request.auto_merge_enabled
        and ci.state is CiState.GREEN
    ):
        kind = GitHubWaitWatchdogKind.AUTO_MERGE_GRACE
        threshold = auto_merge_grace_seconds
        run_times = [
            value
            for run in ci.runs
            for value in (run.created_at, run.updated_at)
        ]
        activity = _latest_activity(
            pull_request.created_at,
            pull_request.updated_at,
            *run_times,
        )
        evidence_parts = [
            str(pull_request.number),
            pull_request.head_sha,
            pull_request.base_sha or "",
            pull_request.updated_at or "",
            *[
                f"{run.run_id}:{run.attempt}:{run.status}:{run.conclusion or ''}:{run.updated_at or run.created_at or ''}"
                for run in sorted(ci.runs, key=lambda item: (item.run_id, item.attempt))
            ],
        ]
    else:
        return None

    if threshold <= 0 or activity is None:
        return None

    reference, raw_reference = activity
    deadline = reference + timedelta(seconds=threshold)
    due = current >= deadline
    return GitHubWaitWatchdog(
        kind=kind,
        last_activity_at=raw_reference,
        threshold_seconds=threshold,
        deadline_at=deadline,
        due=due,
        evidence_identity=_bounded_identity("|".join([kind.value, *evidence_parts])),
        recovery_state="DUE" if due else "WAITING",
    )


def watchdog_dispatch_prefix(project: Project, work_item_id: str) -> str:
    scope = sha256(
        f"{project.project_id}:{work_item_id}:DEV:GITHUB_WATCHDOG".encode("utf-8")
    ).hexdigest()[:24]
    return f"execution:github-watchdog:{scope}:"


def watchdog_dispatch_key(
    project: Project,
    work_item_id: str,
    watchdog: GitHubWaitWatchdog,
) -> str:
    evidence_hash = sha256(watchdog.evidence_identity.encode("utf-8")).hexdigest()
    return (
        f"{watchdog_dispatch_prefix(project, work_item_id)}"
        f"{watchdog.kind.value}:{evidence_hash}:v1"
    )


def with_recovery(
    watchdog: GitHubWaitWatchdog,
    *,
    prepared: bool,
    state: str,
) -> GitHubWaitWatchdog:
    return replace(
        watchdog,
        recovery_prepared=prepared,
        recovery_state=state,
    )


def build_pr_no_ci_follow_up(
    project: Project,
    execution: ExecutionProjection,
    watchdog: GitHubWaitWatchdog,
) -> str:
    work_item = execution.work_item
    pull_request = execution.pull_request
    if (
        work_item is None
        or pull_request is None
        or watchdog.kind is not GitHubWaitWatchdogKind.PR_NO_CI
    ):
        raise ValueError("PR-no-CI watchdog follow-up requires matching execution evidence")
    return f"""La PR #{pull_request.number} pour {work_item.key} est ouverte sur le head {pull_request.head_sha}, mais aucun workflow pull_request n'est observé au-delà du délai de garde.

Repository : {project.repository_full_name}
WorkItem : {work_item.key} — {work_item.title}
PR : #{pull_request.number}
GitHub : {pull_request.url or 'non disponible'}
Head SHA observé : {pull_request.head_sha}
Dernière activité GitHub pertinente : {watchdog.last_activity_at}
Seuil : {round(watchdog.threshold_seconds)} secondes

Reprends la même session DEV uniquement pour diagnostiquer pourquoi la CI n'a pas démarré.

- relis l'état courant GitHub avant toute action;
- vérifie les déclencheurs/workflows et la branche existante;
- ne considère jamais l'absence de workflow comme une CI verte;
- ne lance pas aveuglément une CI concurrente si GitHub montre qu'une exécution a démarré depuis cette preuve;
- corrige uniquement ce qui relève de {work_item.key} si une correction de code/configuration est réellement nécessaire;
- si une nouvelle preuve GitHub existe déjà, suis cette preuve plutôt que ce watchdog devenu stale;
- après toute correction poussée, rapporte le nouveau head SHA puis ARRÊTE ton tour DEV;
- ne reste pas à poller la CI et ne commence pas la tranche suivante.
"""


def build_ci_stall_follow_up(
    project: Project,
    execution: ExecutionProjection,
    watchdog: GitHubWaitWatchdog,
) -> str:
    work_item = execution.work_item
    pull_request = execution.pull_request
    if (
        work_item is None
        or pull_request is None
        or watchdog.kind is not GitHubWaitWatchdogKind.CI_STALLED
    ):
        raise ValueError("CI-stall watchdog follow-up requires matching execution evidence")
    return f"""La CI de la PR #{pull_request.number} pour {work_item.key} reste en cours sans progression GitHub observable au-delà du seuil configuré.

Repository : {project.repository_full_name}
WorkItem : {work_item.key} — {work_item.title}
PR : #{pull_request.number}
GitHub : {pull_request.url or 'non disponible'}
Head SHA observé : {pull_request.head_sha}
Dernière activité workflow pertinente : {watchdog.last_activity_at}
Seuil : {round(watchdog.threshold_seconds)} secondes

Reprends la même session DEV uniquement pour diagnostiquer cette attente CI.

- relis les workflows/runs actuels sur GitHub avant toute action;
- si le run a progressé, changé de tentative, terminé ou si le head a bougé, considère ce watchdog comme stale;
- ne lance pas aveuglément un deuxième workflow concurrent;
- n'effectue une relance GitHub ou une correction que si l'état courant la justifie et sans contourner CI, review ou protections;
- reste strictement dans le scope de {work_item.key};
- si tu pousses une correction, rapporte le nouveau head SHA puis ARRÊTE ton tour DEV;
- ne reste pas à poller GitHub et ne commence pas la tranche suivante.
"""
