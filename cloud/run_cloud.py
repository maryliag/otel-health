#!/usr/bin/env python3
"""
OTel Community Health — Grafana Cloud push.

Collects current-week data from the OpenTelemetry GitHub org, pushes
metrics to Grafana Cloud via OTLP, and provisions the dashboard.

Environment variables (see .env.example):
    GITHUB_TOKEN
    GRAFANA_CLOUD_OTLP_ENDPOINT
    GRAFANA_CLOUD_INSTANCE_ID
    GRAFANA_CLOUD_API_KEY
    GRAFANA_CLOUD_URL
"""

import json
import logging
import os
import sys
from pathlib import Path

# Allow importing the local otel_health package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "local"))

from otel_health.teams import Cache, GitHubClient, fetch_teams_data, write_teams_file  # noqa: E402
from otel_health.collector import compute_stats, write_outputs  # noqa: E402
from otel_health.activity import compute_activity  # noqa: E402
from otel_health.pr_cycle_time import compute_pr_cycle_time  # noqa: E402
from otel_health.repo_activity_30d import compute_repo_activity_30d  # noqa: E402

from otel_cloud.metrics import push_metrics  # noqa: E402
from otel_cloud.dashboard import discover_prometheus_uid, provision_dashboard  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DEFAULT_ORG = "open-telemetry"


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"ERROR: {name} environment variable is required")
    return value


def export_dashboard() -> None:
    """Export the dashboard JSON without running the data pipeline.

    Usage: cd cloud && uv run python run_cloud.py --export-dashboard
    """
    grafana_url = _require_env("GRAFANA_CLOUD_URL").rstrip("/")
    api_key = _require_env("GRAFANA_CLOUD_API_KEY")

    try:
        ds_uid = discover_prometheus_uid(grafana_url, api_key)
    except Exception:
        ds_uid = "grafanacloud-prom"
        logger.warning(f"Could not discover datasource UID, using default: {ds_uid}")

    from otel_cloud.dashboard import build_dashboard
    dashboard_json = build_dashboard(ds_uid)
    out = Path(__file__).resolve().parent / "output" / "otel-health-cloud-dashboard.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dashboard_json, indent=2))
    print(f"Dashboard JSON written to: {out}")
    print(f"Prometheus datasource UID used: {ds_uid}")
    print("Import via Grafana Cloud → Dashboards → New → Import → paste JSON")


def main() -> None:
    if "--export-dashboard" in sys.argv:
        export_dashboard()
        return

    # -- Validate environment --
    github_token = _require_env("GITHUB_TOKEN")
    otlp_endpoint = _require_env("GRAFANA_CLOUD_OTLP_ENDPOINT")
    instance_id = _require_env("GRAFANA_CLOUD_INSTANCE_ID")
    api_key = _require_env("GRAFANA_CLOUD_API_KEY")
    grafana_url = _require_env("GRAFANA_CLOUD_URL").rstrip("/")

    org = os.environ.get("OTEL_ORG", DEFAULT_ORG)

    # -- Setup --
    cloud_dir = Path(__file__).resolve().parent
    output_dir = cloud_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)

    cache = Cache(cloud_dir / "cache")
    client = GitHubClient(github_token, cache)

    # -- Step 1: Fetch teams --
    logger.info("Step 1/5: Fetching org teams from GitHub...")
    teams_data = fetch_teams_data(client, org)
    teams_path = output_dir / "teams.json"
    write_teams_file(teams_data, teams_path)

    # -- Step 2: Compute collector stats --
    logger.info("Step 2/5: Computing health metrics...")
    stats = compute_stats(teams_data)
    write_outputs(stats, output_dir)
    by_repo_path = output_dir / "by_repo_details.json"

    # -- Step 3: Weekly contributor activity (past 1 week) --
    logger.info("Step 3/5: Fetching contributor activity (past week)...")
    try:
        activity_records, _ = compute_activity(
            teams_path, client, org, weeks=1, top_repos=500
        )
    except Exception:
        logger.exception("Activity collection failed, continuing without it.")
        activity_records = []

    # -- Step 4: PR cycle time (past 1 week) --
    logger.info("Step 4/5: Fetching PR cycle time (past week)...")
    try:
        pr_cycle_records = compute_pr_cycle_time(
            teams_path, client, org, weeks=1, top_repos=100
        )
    except Exception:
        logger.exception("PR cycle time collection failed, continuing without it.")
        pr_cycle_records = []

    # -- Step 5: 30-day repo activity --
    logger.info("Step 5/5: Fetching 30-day repo activity...")
    try:
        repo_activity_records = compute_repo_activity_30d(
            by_repo_path, client, org
        )
    except Exception:
        logger.exception("30-day repo activity failed, continuing without it.")
        repo_activity_records = []

    logger.info(cache.stats())

    # -- Push metrics to Grafana Cloud --
    logger.info("Pushing metrics to Grafana Cloud via OTLP...")
    push_metrics(
        otlp_endpoint=otlp_endpoint,
        instance_id=instance_id,
        api_key=api_key,
        stats=stats,
        activity_records=activity_records,
        pr_cycle_records=pr_cycle_records,
        repo_activity_records=repo_activity_records,
    )

    # -- Provision dashboard --
    logger.info("Provisioning dashboard on Grafana Cloud...")
    try:
        ds_uid = discover_prometheus_uid(grafana_url, api_key)
        url = provision_dashboard(grafana_url, api_key, ds_uid)
        print(f"\n  Dashboard: {url}\n")
    except Exception:
        logger.exception("Dashboard provisioning via API failed.")
        # Fall back: write the JSON so the user can import it manually.
        # Try to discover the datasource UID; fall back to a common default.
        try:
            ds_uid = discover_prometheus_uid(grafana_url, api_key)
        except Exception:
            ds_uid = "grafanacloud-prom"
            logger.warning(
                f"Could not discover Prometheus datasource UID, using default: {ds_uid}"
            )
        from otel_cloud.dashboard import build_dashboard
        dashboard_json = build_dashboard(ds_uid)
        export_path = cloud_dir / "output" / "otel-health-cloud-dashboard.json"
        export_path.write_text(json.dumps({"dashboard": dashboard_json, "overwrite": True}, indent=2))
        print(f"\n  Dashboard JSON exported to: {export_path}")
        print(f"  Import it via Grafana Cloud UI → Dashboards → Import\n")

    print("Done.")


if __name__ == "__main__":
    main()
