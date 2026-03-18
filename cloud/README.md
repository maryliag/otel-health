# otel-health-cloud

Pushes OpenTelemetry community health metrics to **Grafana Cloud** via OTLP and provisions a dashboard. Designed to run weekly (manually or via GitHub Actions) so that time-series data accumulates over time.

This is the cloud counterpart of the [`local/`](../local/) project. It reuses the same data-collection modules but targets Grafana Cloud (Mimir/Prometheus) instead of a local Docker Compose stack.

## How it works

1. **Fetch teams** — queries the GitHub Teams API for all `open-telemetry` org teams and members.
2. **Compute stats** — aggregates per-repo and org-wide role counts (triagers, approvers, maintainers).
3. **Weekly contributor activity** — fetches unique contributor counts per repo for the past week.
4. **PR cycle time** — fetches average PR open-to-close time per repo for the past week.
5. **30-day repo activity** — fetches issue/PR counts opened and closed in the past 30 days per repo.
6. **Push metrics** — sends all data to Grafana Cloud as OpenTelemetry gauge metrics via OTLP/HTTP.
7. **Provision dashboard** — creates/updates the Grafana Cloud dashboard via the HTTP API (or exports JSON as fallback).

## Prerequisites

- Python 3.11+ with [uv](https://github.com/astral-sh/uv)
- A GitHub personal access token with **`read:org`** scope
- A Grafana Cloud stack with the following credentials (see [`.env.example`](.env.example))

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `GITHUB_TOKEN` | Yes | GitHub PAT with `read:org` scope |
| `GRAFANA_CLOUD_OTLP_ENDPOINT` | Yes | OTLP gateway URL (find in Grafana Cloud → OpenTelemetry → Configure) |
| `GRAFANA_CLOUD_INSTANCE_ID` | Yes | Numeric instance ID (used for OTLP auth) |
| `GRAFANA_CLOUD_API_KEY` | Yes | API key with MetricsPublisher role (+ Editor for dashboard provisioning) |
| `GRAFANA_CLOUD_URL` | Yes | Grafana instance URL, e.g. `https://your-instance.grafana.net` |
| `GRAFANA_CLOUD_PROM_UID` | No | Prometheus datasource UID — skips auto-discovery if set |

## Quick start

```bash
# 1. Install dependencies
cd cloud
uv sync

# 2. Set environment variables
cp .env.example .env
# Edit .env with your values

# 3. Run the full pipeline (collect + push + provision dashboard)
set -a && source .env && set +a
uv run python run_cloud.py
```

## Export dashboard JSON only

If dashboard provisioning via the API fails (e.g. insufficient permissions), you can export the dashboard JSON and import it manually:

```bash
set -a && source .env && set +a
cd cloud && uv run python run_cloud.py --export-dashboard
```

The JSON is written to `cloud/output/otel-health-cloud-dashboard.json`. Import it in Grafana Cloud via **Dashboards → New → Import → paste JSON**.

## Dashboard panels

| Panel | Type | Description |
|---|---|---|
| Unique Triagers | stat | Deduplicated count of triagers across all repos |
| Unique Approvers | stat | Deduplicated count of approvers across all repos |
| Unique Maintainers | stat | Deduplicated count of maintainers across all repos |
| Unique Contributors with Status | stat | Total unique people with any role |
| Total Repositories | stat | Repos that have at least one role defined |
| Avg Triagers / Repo | stat | Average number of triagers per repository |
| Avg Approvers / Repo | stat | Average number of approvers per repository |
| Avg Maintainers / Repo | stat | Average number of maintainers per repository |
| Avg Groups / User | stat | Average number of groups each community member belongs to |
| 30-Day Repository Activity | table | Issues/PRs opened and closed per repo in the past 30 days, with load ratios |
| Repository Details | table | Per-repo role counts, sorted by total |
| Weekly Unique Contributors by Repository | timeseries | Weekly unique contributors per repo (accumulates over time) |
| Unique Role Counts Over Time | timeseries | Weekly totals for triagers, approvers, maintainers, and all contributors |
| User Group Membership | table | Per-user triager, approver, and maintainer group counts |
| Weekly Average PR Cycle Time by Repository | timeseries | Average PR open-to-close time (days) per repo per week |

## Metrics

All metrics are pushed as OpenTelemetry gauges with `service.name=otel-health`. Approximately 2,200 time series total.

**Summary gauges** (no labels): `otel_health_total_repos`, `otel_health_triagers_deduped`, `otel_health_approvers_deduped`, `otel_health_maintainers_deduped`, `otel_health_unique_users`, `otel_health_avg_triagers_per_repo`, `otel_health_avg_approvers_per_repo`, `otel_health_avg_maintainers_per_repo`, `otel_health_avg_groups_per_user`

**Per-repo gauges** (`repo` label): `otel_health_repo_triagers`, `otel_health_repo_approvers`, `otel_health_repo_maintainers`, `otel_health_repo_total_roles`, `otel_health_repo_weekly_contributors`, `otel_health_repo_avg_pr_cycle_days`, `otel_health_repo_issues_opened_30d`, `otel_health_repo_issues_closed_30d`, `otel_health_repo_prs_opened_30d`, `otel_health_repo_prs_closed_30d`, `otel_health_repo_issues_per_triager`, `otel_health_repo_prs_per_approver`, `otel_health_repo_prs_per_maintainer`

**Per-user gauges** (`username` label): `otel_health_user_triager_groups`, `otel_health_user_approver_groups`, `otel_health_user_maintainer_groups`, `otel_health_user_total_groups`

## GitHub Actions

The workflow at [`.github/workflows/otel-health-cloud.yml`](../.github/workflows/otel-health-cloud.yml) runs daily at 06:00 UTC and can be triggered manually. It requires the following repository secrets:

- `GH_PAT_READ_ORG`
- `GRAFANA_CLOUD_OTLP_ENDPOINT`
- `GRAFANA_CLOUD_INSTANCE_ID`
- `GRAFANA_CLOUD_API_KEY`
- `GRAFANA_CLOUD_URL`

Add these in **GitHub → Settings → Secrets and variables → Actions → Repository secrets**.

## Caching

API responses are cached in `cloud/cache/` to speed up re-runs and avoid rate limits. The GitHub Actions workflow persists this cache between runs. Delete the directory to force a full refresh.
