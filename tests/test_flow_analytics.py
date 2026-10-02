from app.domain.execution import PullRequestEvidence, WorkflowRunEvidence
from app.domain.flow_analytics import (
    CommitEvidence,
    FlowAnalyticsEvidence,
    FlowDeliveryEvidence,
    classify_docs_only,
    derive_flow_analytics,
)
from app.domain.roadmap import WorkItem, WorkItemStatus, WorkItemType


WORK = WorkItem(
    "DC-061",
    WorkItemType.WORK,
    WorkItemStatus.READY,
    "#1",
    "MAIN",
    "Flow Analytics",
)


def pr(*, number=35, created="2026-10-02T10:00:00Z", merged="2026-10-02T11:00:00Z"):
    return PullRequestEvidence(
        number=number,
        title="DC-061 — Flow Analytics",
        body="Work-Item: DC-061",
        branch="dc-061-flow-analytics",
        head_sha="green-sha",
        state="closed" if merged else "open",
        merged=merged is not None,
        mergeable=None,
        url=f"https://github.example/pr/{number}",
        created_at=created,
        updated_at=merged or created,
        merged_at=merged,
    )


def run(
    run_id,
    *,
    sha,
    conclusion,
    attempt=1,
    completed="2026-10-02T10:30:00Z",
):
    return WorkflowRunEvidence(
        run_id=run_id,
        name="CI",
        status="completed",
        conclusion=conclusion,
        attempt=attempt,
        head_sha=sha,
        url=f"https://github.example/actions/{run_id}",
        created_at="2026-10-02T10:20:00Z",
        updated_at=completed,
    )


def delivery(**overrides):
    values = {
        "work_item": WORK,
        "pull_request": pr(),
        "commits": (
            CommitEvidence("first-sha", "2026-10-02T09:00:00Z"),
            CommitEvidence("green-sha", "2026-10-02T10:15:00Z"),
        ),
        "workflow_runs": (
            run(1, sha="first-sha", conclusion="failure", completed="2026-10-02T10:10:00Z"),
            run(2, sha="green-sha", conclusion="success", completed="2026-10-02T10:30:00Z"),
        ),
        "changed_files": ("app/domain/flow_analytics.py",),
    }
    values.update(overrides)
    return FlowDeliveryEvidence(**values)


def test_complete_delivery_calculates_exact_observable_durations():
    projection = derive_flow_analytics(FlowAnalyticsEvidence((delivery(),)))
    item = projection.deliveries[0]

    assert item.first_commit_at == "2026-10-02T09:00:00Z"
    assert item.pr_created_at == "2026-10-02T10:00:00Z"
    assert item.first_green_ci_at == "2026-10-02T10:30:00Z"
    assert item.merged_at == "2026-10-02T11:00:00Z"
    assert item.commit_to_pr_seconds == 3600
    assert item.pr_to_green_ci_seconds == 1800
    assert item.green_ci_to_merge_seconds == 1800
    assert item.total_observable_duration_seconds == 7200
    assert item.ci_attempt_count == 2
    assert item.ci_red_attempt_count == 1
    assert item.recovered_after_red is True
    assert item.missing_data == ()


def test_red_red_green_counts_attempts_and_uses_first_fully_green_ci():
    projection = derive_flow_analytics(
        FlowAnalyticsEvidence(
            (
                delivery(
                    workflow_runs=(
                        run(10, sha="first-sha", conclusion="failure", completed="2026-10-02T10:05:00Z"),
                        run(10, sha="first-sha", conclusion="failure", attempt=2, completed="2026-10-02T10:10:00Z"),
                        run(11, sha="green-sha", conclusion="success", completed="2026-10-02T10:25:00Z"),
                    )
                ),
            )
        )
    )
    item = projection.deliveries[0]

    assert item.ci_attempt_count == 3
    assert item.ci_red_attempt_count == 2
    assert item.recovered_after_red is True
    assert item.first_green_ci_at == "2026-10-02T10:25:00Z"


def test_partial_delivery_keeps_independent_metrics_and_never_invents_bounds():
    projection = derive_flow_analytics(
        FlowAnalyticsEvidence(
            (
                delivery(
                    commits=(),
                    workflow_runs=(),
                    pull_request=pr(merged=None),
                ),
            )
        )
    )
    item = projection.deliveries[0]

    assert item.first_commit_at is None
    assert item.pr_created_at == "2026-10-02T10:00:00Z"
    assert item.first_green_ci_at is None
    assert item.merged_at is None
    assert item.commit_to_pr_seconds is None
    assert item.pr_to_green_ci_seconds is None
    assert item.ci_attempt_count == 0
    assert item.recovered_after_red is False
    assert "first_commit_at" in item.missing_data
    assert "first_green_ci_at" in item.missing_data
    assert "merged_at" in item.missing_data


def test_incomplete_ci_history_represents_recovery_as_unknown():
    projection = derive_flow_analytics(
        FlowAnalyticsEvidence(
            (
                delivery(
                    ci_history_complete=False,
                    workflow_runs=(
                        run(10, sha="first-sha", conclusion="failure"),
                    ),
                ),
            )
        )
    )

    assert projection.deliveries[0].recovered_after_red is None
    assert "recovered_after_red" in projection.deliveries[0].missing_data


def test_docs_only_is_deterministic_and_conservative():
    assert classify_docs_only(("docs/flow-analytics.md", "README.md"), complete=True) is True
    assert classify_docs_only(("docs/flow-analytics.md", "app/main.py"), complete=True) is False
    assert classify_docs_only((), complete=True) is None
    assert classify_docs_only(("docs/flow-analytics.md",), complete=False) is None

    projection = derive_flow_analytics(
        FlowAnalyticsEvidence(
            (
                delivery(changed_files=("docs/flow-analytics.md", "README.md")),
                delivery(
                    work_item=WorkItem(
                        "DC-060", WorkItemType.WORK, WorkItemStatus.DONE, "#1", "MAIN", "Attention"
                    ),
                    pull_request=PullRequestEvidence(
                        number=34,
                        title="DC-060 — Attention",
                        body="Work-Item: DC-060",
                        branch="dc-060-attention",
                        head_sha="green-sha",
                        state="closed",
                        merged=True,
                        mergeable=None,
                        url="https://github.example/pr/34",
                        created_at="2026-10-02T10:00:00Z",
                        updated_at="2026-10-02T11:00:00Z",
                        merged_at="2026-10-02T11:00:00Z",
                    ),
                    changed_files=("app/main.py",),
                ),
            )
        )
    )

    assert [item.work_item_id for item in projection.deliveries] == ["DC-060"]
    assert projection.aggregates.docs_only_excluded_count == 1
    assert projection.exclusions[0].reason == "DOCS_ONLY"


def test_medians_exclude_only_missing_observations_and_expose_counts():
    second_work = WorkItem(
        "DC-060", WorkItemType.WORK, WorkItemStatus.DONE, "#1", "MAIN", "Attention"
    )
    second = delivery(
        work_item=second_work,
        pull_request=PullRequestEvidence(
            number=34,
            title="DC-060 — Attention",
            body="Work-Item: DC-060",
            branch="dc-060-attention",
            head_sha="second-green",
            state="closed",
            merged=True,
            mergeable=None,
            url="https://github.example/pr/34",
            created_at="2026-10-02T12:00:00Z",
            updated_at="2026-10-02T13:00:00Z",
            merged_at="2026-10-02T13:00:00Z",
        ),
        commits=(CommitEvidence("second-first", "2026-10-02T10:00:00Z"),),
        workflow_runs=(),
    )

    projection = derive_flow_analytics(FlowAnalyticsEvidence((delivery(), second)))
    aggregates = projection.aggregates

    assert aggregates.delivery_count == 2
    assert aggregates.merged_delivery_count == 2
    assert aggregates.median_commit_to_pr.observation_count == 2
    assert aggregates.median_commit_to_pr.median_seconds == 5400
    assert aggregates.median_pr_to_green_ci.observation_count == 1
    assert aggregates.median_pr_to_green_ci.median_seconds == 1800
    assert aggregates.median_green_ci_to_merge.observation_count == 1
    assert aggregates.median_total_observable_duration.observation_count == 2
    assert aggregates.merged_at_range_start == "2026-10-02T11:00:00Z"
    assert aggregates.merged_at_range_end == "2026-10-02T13:00:00Z"


def test_same_evidence_produces_same_delivery_order():
    first = derive_flow_analytics(FlowAnalyticsEvidence((delivery(),)))
    second = derive_flow_analytics(FlowAnalyticsEvidence((delivery(),)))

    assert first == second
