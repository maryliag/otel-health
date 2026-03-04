#!/usr/bin/env python3
"""
Fetch weekly unique contributor counts per repository over the past N weeks.

"Contributor activity" covers: commits, issue/PR creation, and issue/PR comments.

Writes output/activity.json — a list of {week, repo, unique_contributors} records
consumed by the Grafana time series panel.

Run otel_health.teams first to generate output/teams.json.
"""

import json
import logging
import os
from argparse import ArgumentParser
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from otel_health.collector import EXCLUDED_REPOS, ROLES
from otel_health.teams import Cache, GitHubClient

GITHUB_API = "https://api.github.com"
DEFAULT_ORG = "open-telemetry"
DEFAULT_WEEKS = 26  # ~6 months
DEFAULT_TOP_REPOS = 100  # repos ranked by total team member count

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def week_start(dt: datetime) -> str:
    """Return 'YYYY-MM-DD' for the Monday that begins dt's ISO week."""
    monday = dt - timedelta(days=dt.weekday())
    return monday.date().isoformat()


def build_week_list(since_dt: datetime) -> list[str]:
    """Return every Monday date from the week of since_dt through today."""
    weeks = []
    current = since_dt - timedelta(days=since_dt.weekday())
    now = datetime.now(timezone.utc)
    while current <= now:
        weeks.append(current.date().isoformat())
        current += timedelta(weeks=1)
    return weeks


def fetch_repo_activity(
    client: GitHubClient, org: str, repo: str, since: str
) -> dict[str, set[str]]:
    """
    Return {week_start_date: {username, ...}} for one repository.
    Covers commits, issues/PRs created, and issue/PR comments.
    """
    weekly: dict[str, set[str]] = defaultdict(set)

    def _add(login: str | None, date_str: str | None) -> None:
        if not login or not date_str:
            return
        login_lower = login.lower()
        if "[bot]" in login_lower or "opentelemetrybot" in login_lower:
            return
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        weekly[week_start(dt)].add(login_lower)

    # Commits
    for c in client.get_all_pages(
        f"{GITHUB_API}/repos/{org}/{repo}/commits", {"since": since}
    ) or []:
        login = (c.get("author") or {}).get("login")
        date_str = (c.get("commit", {}).get("author") or {}).get("date")
        _add(login, date_str)

    # Issues and pull requests created
    for issue in client.get_all_pages(
        f"{GITHUB_API}/repos/{org}/{repo}/issues",
        {"state": "all", "since": since},
    ) or []:
        _add((issue.get("user") or {}).get("login"), issue.get("created_at"))

    # Issue and PR comments
    for comment in client.get_all_pages(
        f"{GITHUB_API}/repos/{org}/{repo}/issues/comments",
        {"since": since},
    ) or []:
        _add((comment.get("user") or {}).get("login"), comment.get("created_at"))

    return dict(weekly)


def select_repos(teams_data: dict, top_repos: int) -> list[str]:
    """
    Return repos that appear in role teams, ranked by total direct member count.
    Repos in EXCLUDED_REPOS are skipped.
    """
    role_teams = [t for t in teams_data["teams"] if t["role"] in ROLES]

    # Accumulate member counts per repo (same person in multiple teams counts once)
    repo_members: dict[str, set[str]] = {}
    for team in role_teams:
        for repo in team["repos"]:
            if repo in EXCLUDED_REPOS:
                continue
            if repo not in repo_members:
                repo_members[repo] = set()
            for m in team["members"]:
                username = m["username"] if isinstance(m, dict) else m
                repo_members[repo].add(username)

    ranked = sorted(repo_members.keys(), key=lambda r: -len(repo_members[r]))
    return ranked[:top_repos] if top_repos > 0 else ranked


def compute_activity(
    teams_path: Path,
    client: GitHubClient,
    org: str,
    weeks: int,
    top_repos: int,
) -> tuple[list[dict], list[dict]]:
    """
    Return a tuple of:
      - sorted list of {week, repo, unique_contributors} records (for activity.json)
      - sorted list of {week, repo, contributors} records (for contributors_by_week.json)
    """
    teams_data = json.loads(teams_path.read_text())
    repo_list = select_repos(teams_data, top_repos)

    since_dt = datetime.now(timezone.utc) - timedelta(weeks=weeks)
    since = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    all_weeks = build_week_list(since_dt)

    logger.info(
        f"Fetching activity for {len(repo_list)} repos "
        f"from {since[:10]} ({weeks} weeks)"
    )

    records = []
    contributor_records = []
    for i, repo in enumerate(repo_list, 1):
        logger.info(f"[{i:3d}/{len(repo_list)}] {repo}")
        weekly = fetch_repo_activity(client, org, repo, since)
        for week in all_weeks:
            contributors = sorted(weekly.get(week, set()))
            records.append(
                {
                    "week": f"{week}T00:00:00Z",
                    "repo": repo,
                    "unique_contributors": len(contributors),
                }
            )
            if contributors:
                contributor_records.append(
                    {
                        "week": f"{week}T00:00:00Z",
                        "repo": repo,
                        "contributors": contributors,
                    }
                )

    records.sort(key=lambda r: (r["week"], r["repo"]))
    contributor_records.sort(key=lambda r: (r["week"], r["repo"]))
    return records, contributor_records


def main() -> None:
    parser = ArgumentParser(
        description="Fetch weekly unique contributor activity and write output/activity.json"
    )
    parser.add_argument("--org", default=DEFAULT_ORG, help="GitHub organization")
    parser.add_argument(
        "--teams-file",
        default="output/teams.json",
        help="Path to teams.json (generated by otel_health.teams)",
    )
    parser.add_argument("--output-dir", default="output", help="Output directory")
    parser.add_argument("--cache-dir", default="cache", help="Cache directory")
    parser.add_argument(
        "--weeks",
        type=int,
        default=DEFAULT_WEEKS,
        help=f"Number of past weeks to collect (default: {DEFAULT_WEEKS})",
    )
    parser.add_argument(
        "--top-repos",
        type=int,
        default=DEFAULT_TOP_REPOS,
        help=f"Repos to include, ranked by total team members (default: {DEFAULT_TOP_REPOS}, 0 = all)",
    )
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("ERROR: GITHUB_TOKEN environment variable is required")

    cache = Cache(Path(args.cache_dir))
    client = GitHubClient(token, cache)

    records, contributor_records = compute_activity(
        Path(args.teams_file), client, args.org, args.weeks, args.top_repos
    )

    out_path = Path(args.output_dir) / "activity.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(records, indent=2))
    logger.info(f"Written: {out_path} ({len(records)} records)")

    contributors_path = Path(args.output_dir) / "contributors_by_week.json"
    contributors_path.write_text(json.dumps(contributor_records, indent=2))
    logger.info(f"Written: {contributors_path} ({len(contributor_records)} records)")

    repos = sorted({r["repo"] for r in records})
    print(f"\n{'=' * 52}")
    print(f"  Activity data  —  {args.org}")
    print(f"{'=' * 52}")
    print(f"  Repos tracked: {len(repos)}")
    print(f"  Weeks covered: {args.weeks}")
    print(f"  Total records: {len(records)}")
    print(f"{'=' * 52}")
    print(f"\n  Written to: {out_path}")
    print(f"  Written to: {contributors_path}\n")


if __name__ == "__main__":
    main()
