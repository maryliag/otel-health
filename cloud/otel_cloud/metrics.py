"""
Push OpenTelemetry community health metrics to Grafana Cloud via OTLP/HTTP.

All data is exported as observable gauges.  A single call to ``push_metrics``
registers the gauges, triggers one export cycle, and shuts down the provider.
"""

import base64
import logging

from opentelemetry.metrics import Observation
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

logger = logging.getLogger(__name__)


def _basic_auth(instance_id: str, api_key: str) -> str:
    raw = f"{instance_id}:{api_key}"
    return base64.b64encode(raw.encode()).decode()


def push_metrics(
    *,
    otlp_endpoint: str,
    instance_id: str,
    api_key: str,
    stats: dict,
    activity_records: list[dict],
    pr_cycle_records: list[dict],
    repo_activity_records: list[dict],
) -> None:
    """Construct all gauges and export once via OTLP to Grafana Cloud."""

    resource = Resource.create({"service.name": "otel-health"})
    exporter = OTLPMetricExporter(
        endpoint=f"{otlp_endpoint}/v1/metrics",
        headers={"Authorization": f"Basic {_basic_auth(instance_id, api_key)}"},
    )
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=5_000)
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    meter = provider.get_meter("otel-health", version="0.1.0")

    summary = stats["summary"]
    by_repo = stats["by_repo"]
    by_user = stats["by_user"]

    # ------------------------------------------------------------------
    # 1. Summary gauges (no labels)
    # ------------------------------------------------------------------
    _SUMMARY_METRICS = {
        "otel_health_total_repos": "total_repos",
        "otel_health_triagers_deduped": "total_triagers_deduped",
        "otel_health_approvers_deduped": "total_approvers_deduped",
        "otel_health_maintainers_deduped": "total_maintainers_deduped",
        "otel_health_unique_users": "total_unique_users",
        "otel_health_avg_triagers_per_repo": "avg_triagers_per_repo",
        "otel_health_avg_approvers_per_repo": "avg_approvers_per_repo",
        "otel_health_avg_maintainers_per_repo": "avg_maintainers_per_repo",
        "otel_health_avg_groups_per_user": "avg_groups_per_user",
    }

    for metric_name, key in _SUMMARY_METRICS.items():
        value = summary[key]

        def _cb(_options, v=value):
            yield Observation(v)

        meter.create_observable_gauge(metric_name, callbacks=[_cb])

    # ------------------------------------------------------------------
    # 2. Per-repo gauges from collector (with repo label)
    # ------------------------------------------------------------------
    _REPO_COLLECTOR_FIELDS = {
        "otel_health_repo_triagers": "triagers",
        "otel_health_repo_approvers": "approvers",
        "otel_health_repo_maintainers": "maintainers",
        "otel_health_repo_total_roles": "total",
    }

    for metric_name, field in _REPO_COLLECTOR_FIELDS.items():

        def _cb(_options, f=field):
            for row in by_repo:
                yield Observation(row[f], {"repo": row["repo"]})

        meter.create_observable_gauge(metric_name, callbacks=[_cb])

    # ------------------------------------------------------------------
    # 3. Per-repo weekly contributors (from activity, 1-week window)
    # ------------------------------------------------------------------
    def _weekly_contributors_cb(_options):
        for rec in activity_records:
            yield Observation(rec["unique_contributors"], {"repo": rec["repo"]})

    meter.create_observable_gauge(
        "otel_health_repo_weekly_contributors", callbacks=[_weekly_contributors_cb]
    )

    # ------------------------------------------------------------------
    # 4. Per-repo PR cycle time (from pr_cycle_time, 1-week window)
    # ------------------------------------------------------------------
    def _pr_cycle_cb(_options):
        for rec in pr_cycle_records:
            yield Observation(rec["avg_days_to_close"], {"repo": rec["repo"]})

    meter.create_observable_gauge(
        "otel_health_repo_avg_pr_cycle_days", callbacks=[_pr_cycle_cb]
    )

    # ------------------------------------------------------------------
    # 5. Per-repo 30-day activity (from repo_activity_30d)
    # ------------------------------------------------------------------
    _REPO_ACTIVITY_FIELDS = {
        "otel_health_repo_issues_opened_30d": "issues_opened",
        "otel_health_repo_prs_opened_30d": "prs_opened",
        "otel_health_repo_issues_closed_30d": "issues_closed",
        "otel_health_repo_prs_closed_30d": "prs_closed",
        "otel_health_repo_issues_per_triager": "issues_per_triager",
        "otel_health_repo_prs_per_approver": "prs_per_approver",
        "otel_health_repo_prs_per_maintainer": "prs_per_maintainer",
    }

    for metric_name, field in _REPO_ACTIVITY_FIELDS.items():

        def _cb(_options, f=field):
            for rec in repo_activity_records:
                val = rec[f]
                if val is not None:
                    yield Observation(val, {"repo": rec["repo"]})

        meter.create_observable_gauge(metric_name, callbacks=[_cb])

    # ------------------------------------------------------------------
    # 6. Per-user group counts (from collector by_user)
    # ------------------------------------------------------------------
    _USER_FIELDS = {
        "otel_health_user_triager_groups": "triager_group_count",
        "otel_health_user_approver_groups": "approver_group_count",
        "otel_health_user_maintainer_groups": "maintainer_group_count",
        "otel_health_user_total_groups": "total_groups",
    }

    for metric_name, field in _USER_FIELDS.items():

        def _cb(_options, f=field):
            for user in by_user:
                yield Observation(user[f], {"username": user["username"]})

        meter.create_observable_gauge(metric_name, callbacks=[_cb])

    # ------------------------------------------------------------------
    # Export and shut down
    # ------------------------------------------------------------------
    logger.info("Flushing metrics to Grafana Cloud...")
    provider.force_flush(timeout_millis=30_000)
    provider.shutdown()
    logger.info("Metrics pushed successfully.")
