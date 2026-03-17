#!/usr/bin/env python3
"""
Fetch the count of issues and pull requests opened/closed in the past 30 days per
repository, and compute load ratios against role member counts.

Columns produced:
  repo                — repository name
  issues_opened       — issues opened in the past 30 days
  prs_opened          — pull requests opened in the past 30 days
  issues_closed       — issues closed in the past 30 days
  prs_closed          — pull requests closed/merged in the past 30 days
  issues_per_triager  — issues_opened / triagers  (null when triagers = 0)
  prs_per_approver    — prs_opened   / approvers  (null when approvers = 0)
  prs_per_maintainer  — prs_opened   / maintainers (null when maintainers = 0)

Reads:
  output/by_repo_details.json  (from otel_health.collector)

Writes:
  output/repo_activity_30d.json
"""

import json
import logging
import os
from argparse import ArgumentParser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from otel_health.teams import Cache, GitHubClient

GITHUB_API = "https://api.github.com"
DEFAULT_ORG = "open-telemetry"
DAYS = 30

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def fetch_opened_counts(
    client: GitHubClient, org: str, repo: str, since_dt: datetime
) -> tuple[int, int]:
    """
    Return (issues_opened, prs_opened) for items created on or after since_dt.

    Uses the issues endpoint (which returns both issues and PRs) sorted by
    created descending, stopping as soon as items fall outside the window.
    Items with a 'pull_request' key are PRs; all others are issues.
    """
    issues = 0
    prs = 0
    page = 1
    while True:
        data = client.get(
            f"{GITHUB_API}/repos/{org}/{repo}/issues",
            {
                "state": "all",
                "sort": "created",
                "direction": "desc",
                "per_page": 100,
                "page": page,
            },
        )
        if not data:
            break

        for item in data:
            created_at = datetime.fromisoformat(
                item["created_at"].replace("Z", "+00:00")
            )
            if created_at < since_dt:
                continue
            if "pull_request" in item:
                prs += 1
            else:
                issues += 1

        # Since results are sorted by created desc, stop when the last item
        # on the page was created before our window.
        last_created = datetime.fromisoformat(
            data[-1]["created_at"].replace("Z", "+00:00")
        )
        if last_created < since_dt or len(data) < 100:
            break
        page += 1

    return issues, prs


def fetch_closed_counts(
    client: GitHubClient, org: str, repo: str, since_dt: datetime
) -> tuple[int, int]:
    """
    Return (issues_closed, prs_closed) for items closed on or after since_dt.

    Uses the issues endpoint with state=closed sorted by updated descending.
    Stops when updated_at falls outside the window (safe because closed_at <= updated_at).
    Items with a 'pull_request' key are PRs; all others are issues.
    """
    issues = 0
    prs = 0
    page = 1
    while True:
        data = client.get(
            f"{GITHUB_API}/repos/{org}/{repo}/issues",
            {
                "state": "closed",
                "sort": "updated",
                "direction": "desc",
                "per_page": 100,
                "page": page,
            },
        )
        if not data:
            break

        for item in data:
            closed_at_str = item.get("closed_at") or item.get("pull_request", {}).get("merged_at")
            if not closed_at_str:
                continue
            closed_at = datetime.fromisoformat(closed_at_str.replace("Z", "+00:00"))
            if closed_at < since_dt:
                continue
            if "pull_request" in item:
                prs += 1
            else:
                issues += 1

        last_updated = datetime.fromisoformat(
            data[-1]["updated_at"].replace("Z", "+00:00")
        )
        if last_updated < since_dt or len(data) < 100:
            break
        page += 1

    return issues, prs


def compute_repo_activity_30d(
    by_repo_path: Path,
    client: GitHubClient,
    org: str,
) -> list[dict]:
    """
    Return a list of per-repo activity records sorted by prs_opened descending.
    """
    by_repo = json.loads(by_repo_path.read_text())
    since_dt = datetime.now(timezone.utc) - timedelta(days=DAYS)

    logger.info(
        f"Fetching {DAYS}-day issue/PR counts for {len(by_repo)} repos "
        f"since {since_dt.strftime('%Y-%m-%d')}"
    )

    records = []
    for i, row in enumerate(by_repo, 1):
        repo = row["repo"]
        logger.info(f"[{i:3d}/{len(by_repo)}] {repo}")

        issues_opened, prs_opened = fetch_opened_counts(client, org, repo, since_dt)
        issues_closed, prs_closed = fetch_closed_counts(client, org, repo, since_dt)

        triagers = row["triagers"]
        approvers = row["approvers"]
        maintainers = row["maintainers"]

        records.append(
            {
                "repo": repo,
                "issues_opened": issues_opened,
                "prs_opened": prs_opened,
                "issues_closed": issues_closed,
                "prs_closed": prs_closed,
                "issues_per_triager": round(issues_opened / triagers, 1) if triagers > 0 else None,
                "prs_per_approver": round(prs_opened / approvers, 1) if approvers > 0 else None,
                "prs_per_maintainer": round(prs_opened / maintainers, 1) if maintainers > 0 else None,
            }
        )

    records.sort(key=lambda r: -(r["prs_opened"] + r["issues_opened"]))
    return records


def main() -> None:
    parser = ArgumentParser(
        description="Fetch 30-day issue/PR counts and write output/repo_activity_30d.json"
    )
    parser.add_argument("--org", default=DEFAULT_ORG, help="GitHub organization")
    parser.add_argument(
        "--by-repo-file",
        default="output/by_repo_details.json",
        help="Path to by_repo_details.json (generated by otel_health.collector)",
    )
    parser.add_argument("--output-dir", default="output", help="Output directory")
    parser.add_argument("--cache-dir", default="cache", help="Cache directory")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("ERROR: GITHUB_TOKEN environment variable is required")

    by_repo_path = Path(args.by_repo_file)
    if not by_repo_path.exists():
        raise SystemExit(
            f"ERROR: {by_repo_path} not found. Run otel_health.collector first."
        )

    cache = Cache(Path(args.cache_dir))
    client = GitHubClient(token, cache)

    records = compute_repo_activity_30d(by_repo_path, client, args.org)

    out_path = Path(args.output_dir) / "repo_activity_30d.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(records, indent=2))
    logger.info(f"Written: {out_path} ({len(records)} records)")

    print(f"\n{'=' * 52}")
    print(f"  30-Day Repository Activity  —  {args.org}")
    print(f"{'=' * 52}")
    print(f"  Repos tracked: {len(records)}")
    print(f"{'=' * 52}")
    print(f"\n  Written to: {out_path}\n")


if __name__ == "__main__":
    main()
