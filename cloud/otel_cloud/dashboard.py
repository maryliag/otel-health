"""
Grafana Cloud dashboard provisioning for OTel Community Health.

Builds the dashboard JSON (PromQL-based) and pushes it via the Grafana
HTTP API.  The dashboard uid is fixed so repeated calls update in place.
"""

import logging

import requests

logger = logging.getLogger(__name__)

DASHBOARD_UID = "otel-health-cloud"
DASHBOARD_TITLE = "OTel Community Health"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stat_panel(
    panel_id: int,
    title: str,
    expr: str,
    grid_x: int,
    grid_y: int,
    grid_w: int,
    ds_uid: str,
    color: str = "green",
) -> dict:
    return {
        "id": panel_id,
        "type": "stat",
        "title": title,
        "gridPos": {"h": 5, "w": grid_w, "x": grid_x, "y": grid_y},
        "options": {"colorMode": "background", "graphMode": "none", "reduceOptions": {"calcs": ["lastNotNull"]}},
        "fieldConfig": {"defaults": {"color": {"fixedColor": color, "mode": "fixed"}, "thresholds": {"mode": "absolute", "steps": [{"color": color, "value": None}]}}, "overrides": []},
        "targets": [
            {
                "datasource": {"type": "prometheus", "uid": ds_uid},
                "expr": f"last_over_time({expr}[1w])",
                "refId": "A",
                "instant": True,
            }
        ],
    }


def _timeseries_panel(
    panel_id: int,
    title: str,
    expr: str,
    grid_y: int,
    ds_uid: str,
    unit: str = "",
) -> dict:
    field_defaults = {
        "custom": {
            "spanNulls": True,
            "lineWidth": 1,
            "fillOpacity": 0,
            "pointSize": 5,
            "showPoints": "always",
        }
    }
    if unit:
        field_defaults["unit"] = unit
    return {
        "id": panel_id,
        "type": "timeseries",
        "title": title,
        "gridPos": {"h": 14, "w": 24, "x": 0, "y": grid_y},
        "options": {"legend": {"calcs": [], "displayMode": "list", "placement": "bottom"}},
        "fieldConfig": {"defaults": field_defaults, "overrides": []},
        "targets": [
            {
                "datasource": {"type": "prometheus", "uid": ds_uid},
                "expr": expr,
                "legendFormat": "{{repo}}",
                "refId": "A",
            }
        ],
    }


def _table_panel(
    panel_id: int,
    title: str,
    queries: list[dict],
    grid_y: int,
    ds_uid: str,
    overrides: list[dict] | None = None,
    sort_field: str = "",
    sort_desc: bool = True,
    exclude_fields: list[str] | None = None,
) -> dict:
    targets = []
    for i, q in enumerate(queries):
        # Wrap in last_over_time to find the most recent sample within 1 week,
        # avoiding Prometheus staleness (5m) when data is pushed weekly.
        raw_expr = q["expr"]
        expr = f"last_over_time({raw_expr}[1w])"
        targets.append(
            {
                "datasource": {"type": "prometheus", "uid": ds_uid},
                "expr": expr,
                "refId": chr(65 + i),
                "instant": True,
                "format": "table",
            }
        )

    exclude = {"Time": True, "__name__": True, "job": True, "instance": True, "service_name": True}
    if exclude_fields:
        for f in exclude_fields:
            exclude[f] = True

    transformations = [
        {"id": "organize", "options": {"excludeByName": exclude}},
        {"id": "merge", "options": {}},
    ]

    panel = {
        "id": panel_id,
        "type": "table",
        "title": title,
        "gridPos": {"h": 16, "w": 24, "x": 0, "y": grid_y},
        "options": {
            "cellHeight": "sm",
            "showHeader": True,
            "footer": {"countRows": False, "fields": "", "reducer": ["sum"], "show": False},
        },
        "fieldConfig": {
            "defaults": {"custom": {"align": "left", "cellOptions": {"type": "auto"}, "inspect": False}},
            "overrides": overrides or [],
        },
        "targets": targets,
        "transformations": transformations,
    }

    if sort_field:
        panel["options"]["sortBy"] = [{"desc": sort_desc, "displayName": sort_field}]

    return panel


# ---------------------------------------------------------------------------
# Dashboard builder
# ---------------------------------------------------------------------------


def build_dashboard(ds_uid: str) -> dict:
    """Return the full Grafana dashboard model for PromQL-based panels."""

    job = "otel-health"
    j = f'{{job="{job}"}}'

    panels = []

    # ---- Row 1: stat panels y=0 (4 panels, w=6 each) ----
    panels.append(_stat_panel(1, "Unique Triagers", f"otel_health_triagers_deduped{j}", 0, 0, 6, ds_uid, color="blue"))
    panels.append(_stat_panel(2, "Unique Approvers", f"otel_health_approvers_deduped{j}", 6, 0, 6, ds_uid, color="green"))
    panels.append(_stat_panel(3, "Unique Maintainers", f"otel_health_maintainers_deduped{j}", 12, 0, 6, ds_uid, color="orange"))
    panels.append(_stat_panel(14, "Unique Contributors with Status", f"otel_health_unique_users{j}", 18, 0, 6, ds_uid, color="dark-purple"))

    # ---- Row 2: stat panels y=5 (5 panels, w=5/5/5/5/4) ----
    panels.append(_stat_panel(4, "Total Repositories", f"otel_health_total_repos{j}", 0, 5, 5, ds_uid, color="purple"))
    panels.append(_stat_panel(5, "Avg Triagers / Repo", f"otel_health_avg_triagers_per_repo{j}", 5, 5, 5, ds_uid, color="#5794F2"))
    panels.append(_stat_panel(6, "Avg Approvers / Repo", f"otel_health_avg_approvers_per_repo{j}", 10, 5, 5, ds_uid, color="#73BF69"))
    panels.append(_stat_panel(7, "Avg Maintainers / Repo", f"otel_health_avg_maintainers_per_repo{j}", 15, 5, 5, ds_uid, color="#FF9830"))
    panels.append(_stat_panel(12, "Avg Groups / User", f"otel_health_avg_groups_per_user{j}", 20, 5, 4, ds_uid))

    # ---- 30-Day Repository Activity table y=10 ----
    repo_activity_queries = [
        {"expr": f"otel_health_repo_issues_opened_30d{j}"},
        {"expr": f"otel_health_repo_issues_closed_30d{j}"},
        {"expr": f"otel_health_repo_prs_opened_30d{j}"},
        {"expr": f"otel_health_repo_prs_closed_30d{j}"},
        {"expr": f"otel_health_repo_issues_per_triager{j}"},
        {"expr": f"otel_health_repo_prs_per_approver{j}"},
        {"expr": f"otel_health_repo_prs_per_maintainer{j}"},
    ]
    repo_activity_overrides = [
        {
            "matcher": {"id": "byName", "options": "repo"},
            "properties": [
                {"id": "displayName", "value": "Repository"},
                {"id": "links", "value": [{"title": "Open on GitHub", "url": "https://github.com/open-telemetry/${__value.text}", "targetBlank": True}]},
            ],
        },
        {"matcher": {"id": "byName", "options": "Value #A"}, "properties": [{"id": "displayName", "value": "Issues Opened"}]},
        {"matcher": {"id": "byName", "options": "Value #B"}, "properties": [{"id": "displayName", "value": "Issues Closed"}]},
        {"matcher": {"id": "byName", "options": "Value #C"}, "properties": [{"id": "displayName", "value": "PRs Opened"}]},
        {"matcher": {"id": "byName", "options": "Value #D"}, "properties": [{"id": "displayName", "value": "PRs Closed"}]},
        {"matcher": {"id": "byName", "options": "Value #E"}, "properties": [{"id": "displayName", "value": "Issues / Triager"}]},
        {"matcher": {"id": "byName", "options": "Value #F"}, "properties": [{"id": "displayName", "value": "PRs / Approver"}]},
        {"matcher": {"id": "byName", "options": "Value #G"}, "properties": [{"id": "displayName", "value": "PRs / Maintainer"}]},
    ]
    panels.append(_table_panel(16, "30-Day Repository Activity", repo_activity_queries, 10, ds_uid, overrides=repo_activity_overrides, sort_field="PRs Opened"))

    # ---- Repository Details table y=26 ----
    repo_detail_queries = [
        {"expr": f"otel_health_repo_triagers{j}"},
        {"expr": f"otel_health_repo_approvers{j}"},
        {"expr": f"otel_health_repo_maintainers{j}"},
        {"expr": f"otel_health_repo_total_roles{j}"},
    ]
    repo_detail_overrides = [
        {
            "matcher": {"id": "byName", "options": "repo"},
            "properties": [
                {"id": "displayName", "value": "Repository"},
                {"id": "links", "value": [{"title": "Open on GitHub", "url": "https://github.com/open-telemetry/${__value.text}", "targetBlank": True}]},
            ],
        },
        {"matcher": {"id": "byName", "options": "Value #A"}, "properties": [{"id": "displayName", "value": "Triagers"}]},
        {"matcher": {"id": "byName", "options": "Value #B"}, "properties": [{"id": "displayName", "value": "Approvers"}]},
        {"matcher": {"id": "byName", "options": "Value #C"}, "properties": [{"id": "displayName", "value": "Maintainers"}]},
        {"matcher": {"id": "byName", "options": "Value #D"}, "properties": [{"id": "displayName", "value": "Total"}]},
    ]
    panels.append(_table_panel(10, "Repository Details", repo_detail_queries, 26, ds_uid, overrides=repo_detail_overrides, sort_field="Total"))

    # ---- Weekly Unique Contributors timeseries y=42 ----
    panels.append(_timeseries_panel(11, "Weekly Unique Contributors by Repository", f"otel_health_repo_weekly_contributors{j}", 42, ds_uid))

    user_queries = [
        {"expr": f"otel_health_user_triager_groups{j}"},
        {"expr": f"otel_health_user_approver_groups{j}"},
        {"expr": f"otel_health_user_maintainer_groups{j}"},
        {"expr": f"otel_health_user_total_groups{j}"},
    ]
    user_overrides = [
        {"matcher": {"id": "byName", "options": "username"}, "properties": [{"id": "displayName", "value": "Username"}, {"id": "links", "value": [{"title": "Open on GitHub", "url": "https://github.com/${__value.text}", "targetBlank": True}]}]},
        {"matcher": {"id": "byName", "options": "Value #A"}, "properties": [{"id": "displayName", "value": "Triager Groups"}]},
        {"matcher": {"id": "byName", "options": "Value #B"}, "properties": [{"id": "displayName", "value": "Approver Groups"}]},
        {"matcher": {"id": "byName", "options": "Value #C"}, "properties": [{"id": "displayName", "value": "Maintainer Groups"}]},
        {"matcher": {"id": "byName", "options": "Value #D"}, "properties": [{"id": "displayName", "value": "Total Groups"}]},
    ]
    # ---- Role Counts Over Time timeseries y=56 ----
    panels.append({
        "id": 17,
        "type": "timeseries",
        "title": "Unique Role Counts Over Time",
        "gridPos": {"h": 14, "w": 24, "x": 0, "y": 56},
        "options": {"legend": {"calcs": ["lastNotNull"], "displayMode": "list", "placement": "bottom"}},
        "fieldConfig": {
            "defaults": {
                "custom": {"spanNulls": True, "lineWidth": 2, "fillOpacity": 10, "pointSize": 5, "showPoints": "always"},
            },
            "overrides": [
                {"matcher": {"id": "byName", "options": "Triagers"}, "properties": [{"id": "color", "value": {"fixedColor": "blue", "mode": "fixed"}}]},
                {"matcher": {"id": "byName", "options": "Approvers"}, "properties": [{"id": "color", "value": {"fixedColor": "green", "mode": "fixed"}}]},
                {"matcher": {"id": "byName", "options": "Maintainers"}, "properties": [{"id": "color", "value": {"fixedColor": "orange", "mode": "fixed"}}]},
                {"matcher": {"id": "byName", "options": "Total with Status"}, "properties": [{"id": "color", "value": {"fixedColor": "dark-purple", "mode": "fixed"}}]},
            ],
        },
        "targets": [
            {"datasource": {"type": "prometheus", "uid": ds_uid}, "expr": f"otel_health_triagers_deduped{j}", "legendFormat": "Triagers", "refId": "A"},
            {"datasource": {"type": "prometheus", "uid": ds_uid}, "expr": f"otel_health_approvers_deduped{j}", "legendFormat": "Approvers", "refId": "B"},
            {"datasource": {"type": "prometheus", "uid": ds_uid}, "expr": f"otel_health_maintainers_deduped{j}", "legendFormat": "Maintainers", "refId": "C"},
            {"datasource": {"type": "prometheus", "uid": ds_uid}, "expr": f"otel_health_unique_users{j}", "legendFormat": "Total with Status", "refId": "D"},
        ],
    })

    # ---- User Group Membership table y=70 ----
    panels.append(_table_panel(13, "User Group Membership", user_queries, 70, ds_uid, overrides=user_overrides, sort_field="Total Groups", exclude_fields=["repo"]))

    # ---- Weekly Avg PR Cycle Time timeseries y=86 ----
    panels.append(_timeseries_panel(15, "Weekly Average PR Cycle Time by Repository", f"otel_health_repo_avg_pr_cycle_days{j}", 86, ds_uid, unit="d"))

    return {
        "id": None,
        "uid": DASHBOARD_UID,
        "title": DASHBOARD_TITLE,
        "tags": ["opentelemetry", "community", "health", "cloud"],
        "timezone": "browser",
        "schemaVersion": 38,
        "version": 1,
        "refresh": "",
        "style": "dark",
        "panels": panels,
        "time": {"from": "now-6M", "to": "now"},
        "timepicker": {},
        "templating": {"list": []},
        "weekStart": "",
    }


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


def discover_prometheus_uid(grafana_url: str, api_key: str) -> str:
    """Find the UID of the first Prometheus datasource in the Grafana instance."""
    resp = requests.get(
        f"{grafana_url}/api/datasources",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    resp.raise_for_status()
    for ds in resp.json():
        if ds["type"] == "prometheus":
            logger.info(f"Found Prometheus datasource: {ds['name']} (uid={ds['uid']})")
            return ds["uid"]
    raise RuntimeError("No Prometheus datasource found in Grafana Cloud instance")


def provision_dashboard(grafana_url: str, api_key: str, ds_uid: str) -> str:
    """Create or update the dashboard and return its URL."""
    dashboard = build_dashboard(ds_uid)
    payload = {
        "dashboard": dashboard,
        "folderUid": "",
        "overwrite": True,
    }
    resp = requests.post(
        f"{grafana_url}/api/dashboards/db",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()
    url = f"{grafana_url}{result.get('url', '')}"
    logger.info(f"Dashboard provisioned: {url}")
    return url
