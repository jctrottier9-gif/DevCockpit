from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import median

from app.domain.execution import (
    PullRequestEvidence,
    WorkflowRunEvidence,
    summarize_ci,
    CiState,
)
from app.domain.roadmap import WorkItem


_FAILURE_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
)
_DOC_ROOT_NAMES = frozenset({"readme", "agents", "contributing", "changelog", "license"})
_DOC_ROOT_SUFFIXES = frozenset({".md", ".mdx", ".rst", ".txt"})


@dataclass(frozen=True, slots=True)
class FlowDiagnostic:
    code: str
    message: str
    work_item_id: str | None = None
    pr_number: int | None = None


@dataclass(frozen=True, slots=True)
class CommitEvidence:
    sha: str
    committed_at: str | None


@dataclass(frozen=True, slots=True)
class FlowDeliveryEvidence:
    work_item: WorkItem
    pull_request: PullRequestEvidence
    commits: tuple[CommitEvidence, ...] = ()
    workflow_runs: tuple[WorkflowRunEvidence, ...] = ()
    changed_files: tuple[str, ...] = ()
    commits_complete: bool = True
    ci_history_complete: bool = True
    changed_files_complete: bool = True
    diagnostics: tuple[FlowDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class FlowAnalyticsEvidence:
    deliveries: tuple[FlowDeliveryEvidence, ...] = ()
    diagnostics: tuple[FlowDiagnostic, ...] = ()


@dataclass(frozen=True, slots=True)
class FlowCiAttemptProjection:
    run_id: int
    name: str
    attempt: int
    head_sha: str
    status: str
    conclusion: str | None
    created_at: str | None
    completed_at: str | None
    url: str | None


@dataclass(frozen=True, slots=True)
class FlowDeliveryProjection:
    work_item_id: str
    work_item_title: str
    pr_number: int
    pr_title: str
    pr_url: str | None
    first_commit_at: str | None
    pr_created_at: str | None
    first_green_ci_at: str | None
    merged_at: str | None
    commit_to_pr_seconds: float | None
    pr_to_green_ci_seconds: float | None
    green_ci_to_merge_seconds: float | None
    total_observable_duration_seconds: float | None
    ci_attempt_count: int
    ci_red_attempt_count: int
    recovered_after_red: bool | None
    ci_attempts: tuple[FlowCiAttemptProjection, ...]
    missing_data: tuple[str, ...]
    diagnostics: tuple[FlowDiagnostic, ...]


@dataclass(frozen=True, slots=True)
class MetricAggregate:
    median_seconds: float | None
    observation_count: int


@dataclass(frozen=True, slots=True)
class FlowAggregates:
    delivery_count: int
    merged_delivery_count: int
    docs_only_excluded_count: int
    merged_at_range_start: str | None
    merged_at_range_end: str | None
    median_commit_to_pr: MetricAggregate
    median_pr_to_green_ci: MetricAggregate
    median_green_ci_to_merge: MetricAggregate
    median_total_observable_duration: MetricAggregate
    ci_attempt_count_total: int
    ci_red_attempt_count_total: int
    recovered_after_red_count: int
    recovered_after_red_observation_count: int


@dataclass(frozen=True, slots=True)
class FlowExclusion:
    work_item_id: str
    pr_number: int
    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class FlowAnalyticsProjection:
    deliveries: tuple[FlowDeliveryProjection, ...]
    aggregates: FlowAggregates
    exclusions: tuple[FlowExclusion, ...]
    diagnostics: tuple[FlowDiagnostic, ...]


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        return None
    return value.astimezone(timezone.utc)


def _render_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _duration_seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None or end < start:
        return None
    return (end - start).total_seconds()


def _is_root_document(path: str) -> bool:
    normalized = path.strip().replace("\\", "/")
    if "/" in normalized or not normalized:
        return False
    lower = normalized.lower()
    stem, dot, suffix = lower.rpartition(".")
    extension = f".{suffix}" if dot else ""
    return stem in _DOC_ROOT_NAMES and extension in _DOC_ROOT_SUFFIXES


def classify_docs_only(
    changed_files: tuple[str, ...],
    *,
    complete: bool,
) -> bool | None:
    """Return True only when GitHub file evidence deterministically proves docs-only."""
    if not complete or not changed_files:
        return None
    for path in changed_files:
        normalized = path.strip().replace("\\", "/")
        if normalized.startswith("docs/"):
            continue
        if _is_root_document(normalized):
            continue
        return False
    return True


def _first_green_ci_at(
    runs: tuple[WorkflowRunEvidence, ...],
) -> tuple[datetime | None, tuple[FlowDiagnostic, ...]]:
    diagnostics: list[FlowDiagnostic] = []
    candidates: list[datetime] = []
    by_head: dict[str, list[WorkflowRunEvidence]] = {}
    for run in runs:
        by_head.setdefault(run.head_sha, []).append(run)

    for head_sha, head_runs in by_head.items():
        latest_by_run: dict[int, WorkflowRunEvidence] = {}
        for run in head_runs:
            current = latest_by_run.get(run.run_id)
            if current is None or run.attempt > current.attempt:
                latest_by_run[run.run_id] = run
        current_runs = tuple(latest_by_run[key] for key in sorted(latest_by_run))
        ci = summarize_ci(current_runs, head_sha=head_sha)
        if ci.state is not CiState.GREEN:
            continue
        completion_times = [_parse_timestamp(run.updated_at) for run in current_runs]
        if any(item is None for item in completion_times):
            diagnostics.append(
                FlowDiagnostic(
                    code="GREEN_CI_COMPLETION_TIME_MISSING",
                    message=(
                        f"A fully green CI was observable on {head_sha}, but at least one "
                        "workflow completion timestamp is missing."
                    ),
                )
            )
            continue
        candidates.append(max(item for item in completion_times if item is not None))

    return (min(candidates) if candidates else None, tuple(diagnostics))


def derive_flow_delivery(
    evidence: FlowDeliveryEvidence,
) -> FlowDeliveryProjection:
    diagnostics = list(evidence.diagnostics)
    pr = evidence.pull_request

    commit_times = [
        parsed
        for commit in evidence.commits
        if (parsed := _parse_timestamp(commit.committed_at)) is not None
    ]
    first_commit = min(commit_times) if commit_times else None
    if evidence.commits and not commit_times:
        diagnostics.append(
            FlowDiagnostic(
                "COMMIT_TIMESTAMPS_UNAVAILABLE",
                "PR commits were observed but none had a usable GitHub commit timestamp.",
                evidence.work_item.key,
                pr.number,
            )
        )
    if not evidence.commits_complete:
        diagnostics.append(
            FlowDiagnostic(
                "COMMIT_HISTORY_INCOMPLETE",
                "GitHub commit history for this delivery could not be read completely.",
                evidence.work_item.key,
                pr.number,
            )
        )

    pr_created = _parse_timestamp(pr.created_at)
    merged = _parse_timestamp(pr.merged_at)
    first_green, green_diagnostics = _first_green_ci_at(evidence.workflow_runs)
    diagnostics.extend(
        FlowDiagnostic(
            item.code,
            item.message,
            evidence.work_item.key,
            pr.number,
        )
        for item in green_diagnostics
    )

    commit_to_pr = _duration_seconds(first_commit, pr_created)
    pr_to_green = _duration_seconds(pr_created, first_green)
    green_to_merge = _duration_seconds(first_green, merged)
    total = _duration_seconds(first_commit, merged)

    chronological_pairs = (
        ("commit_to_pr", first_commit, pr_created),
        ("pr_to_green_ci", pr_created, first_green),
        ("green_ci_to_merge", first_green, merged),
        ("total_observable_duration", first_commit, merged),
    )
    for metric, start, end in chronological_pairs:
        if start is not None and end is not None and end < start:
            diagnostics.append(
                FlowDiagnostic(
                    "NON_MONOTONIC_GITHUB_TIMESTAMPS",
                    f"GitHub timestamps for {metric} are not chronological; the duration is unavailable.",
                    evidence.work_item.key,
                    pr.number,
                )
            )

    attempts = tuple(
        FlowCiAttemptProjection(
            run_id=run.run_id,
            name=run.name,
            attempt=run.attempt,
            head_sha=run.head_sha,
            status=run.status,
            conclusion=run.conclusion,
            created_at=_render_timestamp(_parse_timestamp(run.created_at)),
            completed_at=_render_timestamp(_parse_timestamp(run.updated_at)),
            url=run.url,
        )
        for run in sorted(
            evidence.workflow_runs,
            key=lambda item: (
                _parse_timestamp(item.created_at) or datetime.min.replace(tzinfo=timezone.utc),
                item.run_id,
                item.attempt,
            ),
        )
    )
    red_runs = tuple(
        run
        for run in evidence.workflow_runs
        if run.status == "completed" and run.conclusion in _FAILURE_CONCLUSIONS
    )

    recovered: bool | None
    if not evidence.ci_history_complete:
        recovered = None
    elif not red_runs:
        recovered = False
    elif first_green is None:
        recovered = False
    else:
        red_times = [_parse_timestamp(run.updated_at) for run in red_runs]
        if any(item is None for item in red_times):
            recovered = None
        else:
            recovered = any(
                item is not None and item < first_green
                for item in red_times
            )

    missing: list[str] = []
    for name, value in (
        ("first_commit_at", first_commit),
        ("pr_created_at", pr_created),
        ("first_green_ci_at", first_green),
        ("merged_at", merged),
    ):
        if value is None:
            missing.append(name)
    for name, value in (
        ("commit_to_pr", commit_to_pr),
        ("pr_to_green_ci", pr_to_green),
        ("green_ci_to_merge", green_to_merge),
        ("total_observable_duration", total),
    ):
        if value is None:
            missing.append(name)
    if recovered is None:
        missing.append("recovered_after_red")

    return FlowDeliveryProjection(
        work_item_id=evidence.work_item.key,
        work_item_title=evidence.work_item.title,
        pr_number=pr.number,
        pr_title=pr.title,
        pr_url=pr.url,
        first_commit_at=_render_timestamp(first_commit),
        pr_created_at=_render_timestamp(pr_created),
        first_green_ci_at=_render_timestamp(first_green),
        merged_at=_render_timestamp(merged),
        commit_to_pr_seconds=commit_to_pr,
        pr_to_green_ci_seconds=pr_to_green,
        green_ci_to_merge_seconds=green_to_merge,
        total_observable_duration_seconds=total,
        ci_attempt_count=len(evidence.workflow_runs),
        ci_red_attempt_count=len(red_runs),
        recovered_after_red=recovered,
        ci_attempts=attempts,
        missing_data=tuple(missing),
        diagnostics=tuple(diagnostics),
    )


def _metric(values: list[float]) -> MetricAggregate:
    return MetricAggregate(
        median_seconds=float(median(values)) if values else None,
        observation_count=len(values),
    )


def derive_flow_analytics(evidence: FlowAnalyticsEvidence) -> FlowAnalyticsProjection:
    deliveries: list[FlowDeliveryProjection] = []
    exclusions: list[FlowExclusion] = []
    diagnostics = list(evidence.diagnostics)

    for delivery in evidence.deliveries:
        docs_only = classify_docs_only(
            delivery.changed_files,
            complete=delivery.changed_files_complete,
        )
        if docs_only is True:
            exclusions.append(
                FlowExclusion(
                    work_item_id=delivery.work_item.key,
                    pr_number=delivery.pull_request.number,
                    reason="DOCS_ONLY",
                    detail="All observed changed files are deterministic documentation paths.",
                )
            )
            continue
        if docs_only is None:
            diagnostics.append(
                FlowDiagnostic(
                    "DOCS_ONLY_CLASSIFICATION_UNAVAILABLE",
                    "Changed-file evidence is incomplete; the delivery is retained conservatively.",
                    delivery.work_item.key,
                    delivery.pull_request.number,
                )
            )
        deliveries.append(derive_flow_delivery(delivery))

    merged_times = sorted(
        parsed
        for item in deliveries
        if (parsed := _parse_timestamp(item.merged_at)) is not None
    )
    recovered_observed = [
        item.recovered_after_red
        for item in deliveries
        if item.recovered_after_red is not None
    ]

    aggregates = FlowAggregates(
        delivery_count=len(deliveries),
        merged_delivery_count=sum(item.merged_at is not None for item in deliveries),
        docs_only_excluded_count=len(exclusions),
        merged_at_range_start=_render_timestamp(merged_times[0]) if merged_times else None,
        merged_at_range_end=_render_timestamp(merged_times[-1]) if merged_times else None,
        median_commit_to_pr=_metric([
            value
            for item in deliveries
            if (value := item.commit_to_pr_seconds) is not None
        ]),
        median_pr_to_green_ci=_metric([
            value
            for item in deliveries
            if (value := item.pr_to_green_ci_seconds) is not None
        ]),
        median_green_ci_to_merge=_metric([
            value
            for item in deliveries
            if (value := item.green_ci_to_merge_seconds) is not None
        ]),
        median_total_observable_duration=_metric([
            value
            for item in deliveries
            if (value := item.total_observable_duration_seconds) is not None
        ]),
        ci_attempt_count_total=sum(item.ci_attempt_count for item in deliveries),
        ci_red_attempt_count_total=sum(item.ci_red_attempt_count for item in deliveries),
        recovered_after_red_count=sum(value is True for value in recovered_observed),
        recovered_after_red_observation_count=len(recovered_observed),
    )
    return FlowAnalyticsProjection(
        deliveries=tuple(deliveries),
        aggregates=aggregates,
        exclusions=tuple(exclusions),
        diagnostics=tuple(diagnostics),
    )
