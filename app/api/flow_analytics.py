from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.application.flow_analytics import FlowAnalyticsSourceError, read_project_flow_analytics
from app.application.roadmaps import RoadmapSourceError


def _diagnostic_payload(item) -> dict[str, object]:
    return {
        "code": item.code,
        "message": item.message,
        "work_item_id": item.work_item_id,
        "pr_number": item.pr_number,
    }


def _metric_payload(metric) -> dict[str, object]:
    return {
        "seconds": metric.median_seconds,
        "observations": metric.observation_count,
    }


def build_flow_analytics_router(*, project_catalog, roadmap_reader, analytics_reader):
    router = APIRouter(tags=["analytics"])

    @router.get("/api/projects/{project_id}/analytics")
    def read_analytics(project_id: str):
        project = project_catalog.get(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")
        try:
            projection = read_project_flow_analytics(
                project,
                roadmap_reader=roadmap_reader,
                analytics_reader=analytics_reader,
            )
        except (RoadmapSourceError, FlowAnalyticsSourceError) as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "code": exc.code,
                    "message": "Flow Analytics source is unavailable.",
                },
            ) from exc

        analytics = projection.analytics
        aggregates = analytics.aggregates
        return {
            "project": {
                "project_id": project.project_id,
                "repository_full_name": project.repository_full_name,
                "roadmap_issue_number": project.roadmap_issue_number,
            },
            "source": {
                "status": "available",
                "repository_full_name": projection.issue.repository_full_name,
                "issue_number": projection.issue.issue_number,
                "updated_at": projection.issue.updated_at,
            },
            "aggregates": {
                "delivery_count": aggregates.delivery_count,
                "merged_delivery_count": aggregates.merged_delivery_count,
                "docs_only_excluded_count": aggregates.docs_only_excluded_count,
                "merged_at_range": {
                    "start": aggregates.merged_at_range_start,
                    "end": aggregates.merged_at_range_end,
                },
                "median_commit_to_pr": _metric_payload(aggregates.median_commit_to_pr),
                "median_pr_to_green_ci": _metric_payload(aggregates.median_pr_to_green_ci),
                "median_green_ci_to_merge": _metric_payload(aggregates.median_green_ci_to_merge),
                "median_total_observable_duration": _metric_payload(
                    aggregates.median_total_observable_duration
                ),
                "ci_attempt_count_total": aggregates.ci_attempt_count_total,
                "ci_red_attempt_count_total": aggregates.ci_red_attempt_count_total,
                "recovered_after_red_count": aggregates.recovered_after_red_count,
                "recovered_after_red_observations": (
                    aggregates.recovered_after_red_observation_count
                ),
            },
            "deliveries": [
                {
                    "work_item": {
                        "key": item.work_item_id,
                        "title": item.work_item_title,
                    },
                    "pr": {
                        "number": item.pr_number,
                        "title": item.pr_title,
                        "url": item.pr_url,
                    },
                    "first_commit_at": item.first_commit_at,
                    "pr_created_at": item.pr_created_at,
                    "first_green_ci_at": item.first_green_ci_at,
                    "merged_at": item.merged_at,
                    "durations": {
                        "commit_to_pr_seconds": item.commit_to_pr_seconds,
                        "pr_to_green_ci_seconds": item.pr_to_green_ci_seconds,
                        "green_ci_to_merge_seconds": item.green_ci_to_merge_seconds,
                        "total_observable_duration_seconds": (
                            item.total_observable_duration_seconds
                        ),
                    },
                    "ci": {
                        "attempt_count": item.ci_attempt_count,
                        "red_attempt_count": item.ci_red_attempt_count,
                        "recovered_after_red": item.recovered_after_red,
                        "attempts": [
                            {
                                "run_id": run.run_id,
                                "name": run.name,
                                "attempt": run.attempt,
                                "head_sha": run.head_sha,
                                "status": run.status,
                                "conclusion": run.conclusion,
                                "created_at": run.created_at,
                                "completed_at": run.completed_at,
                                "url": run.url,
                            }
                            for run in item.ci_attempts
                        ],
                    },
                    "missing_data": list(item.missing_data),
                    "diagnostics": [
                        _diagnostic_payload(diagnostic)
                        for diagnostic in item.diagnostics
                    ],
                }
                for item in analytics.deliveries
            ],
            "exclusions": [
                {
                    "work_item_id": item.work_item_id,
                    "pr_number": item.pr_number,
                    "reason": item.reason,
                    "detail": item.detail,
                }
                for item in analytics.exclusions
            ],
            "diagnostics": [
                _diagnostic_payload(item)
                for item in analytics.diagnostics
            ],
        }

    return router
