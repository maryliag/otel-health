#!/usr/bin/env python3
"""
Process output/teams.json and produce the Grafana data files:
  output/summary.json   — aggregate counts for stat panels
  output/by_repo.csv    — per-repo counts for bar chart and table
  output/otel-health.json — full dataset including member lists

Run otel_health.teams first to generate output/teams.json.
"""

import csv
import json
import logging
from argparse import ArgumentParser
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

ROLES = ("triagers", "approvers", "maintainers")

# Teams whose name contains any of these substrings are excluded from the given repo's metrics.
EXCLUDED_TEAM_PATTERNS: dict[str, list[str]] = {
    "opentelemetry-js": ["browser"],
    "opentelemetry-js-contrib": ["browser"],
}

# Repositories to exclude from metrics (meta/org-management repos with no code)
EXCLUDED_REPOS: frozenset[str] = frozenset(
    [
        ".github",
        ".project",
        ".roadmap",
        "admin",
        "build-tools",
        "changelog.opentelemetry.io",
        "community",
        "opentelemetry-collector-ghsa-cfmr-cj23-f997",
        "opentelemetry-collector-ghsa-wwgm-p9qq-7gh7",
        "govanityurls",
        "opentelemetry.io-ghsa-m79v-h4fx-3fw6",
        "opentelemetry-ebpf-profiler-ghsa-7jcw-7r7m-wp3p",
        "opentelemetry-php-ghsa-fpr9-v4h2-8rwf",
    ]
)


def load_teams(path: Path) -> dict:
    """Load and return the teams.json file."""
    if not path.exists():
        raise SystemExit(
            f"ERROR: {path} not found.\n"
            "Run 'python -m otel_health.teams' first to fetch team data."
        )
    logger.info(f"Loading teams from {path}")
    return json.loads(path.read_text())


def compute_stats(teams_data: dict) -> dict:
    """
    Aggregate per-repo role counts and org-wide unique counts from teams.json.

    Only teams with role 'triagers', 'approvers', or 'maintainers' are used.
    A team can be linked to multiple repos; members are counted once per
    (repo, role) combination.
    """
    role_teams = [t for t in teams_data["teams"] if t["role"] in ROLES]
    logger.info(
        f"Processing {len(role_teams)} role teams "
        f"(of {teams_data['stats']['total_teams']} total)"
    )

    # Org-wide unique people per role (across all repos)
    all_by_role: dict[str, set[str]] = {r: set() for r in ROLES}

    # Per-repo accumulator: repo -> role -> set of usernames
    repo_members: dict[str, dict[str, set[str]]] = {}
    # Per-repo team names: repo -> role -> set of team names
    repo_teams: dict[str, dict[str, set[str]]] = {}
    # Per-user team membership (org-wide): username -> role -> set of team names
    user_teams: dict[str, dict[str, set[str]]] = {}

    for team in role_teams:
        role = team["role"]
        repos = team["repos"]
        team_name = team["name"]

        for raw_m in team["members"]:
            username = raw_m["username"] if isinstance(raw_m, dict) else raw_m

            all_by_role[role].add(username)

            if username not in user_teams:
                user_teams[username] = {r: set() for r in ROLES}
            user_teams[username][role].add(team_name)

            for repo in repos:
                if repo in EXCLUDED_REPOS:
                    continue
                if any(p in team_name for p in EXCLUDED_TEAM_PATTERNS.get(repo, [])):
                    continue
                if repo not in repo_members:
                    repo_members[repo] = {r: set() for r in ROLES}
                    repo_teams[repo] = {r: set() for r in ROLES}
                repo_members[repo][role].add(username)

        for repo in repos:
            if repo in EXCLUDED_REPOS:
                continue
            if any(p in team_name for p in EXCLUDED_TEAM_PATTERNS.get(repo, [])):
                continue
            if repo not in repo_teams:
                repo_teams[repo] = {r: set() for r in ROLES}
                repo_members.setdefault(repo, {r: set() for r in ROLES})
            repo_teams[repo][role].add(team_name)

    # Apply the same hierarchy to org-wide counts
    all_by_role["approvers"] -= all_by_role["maintainers"]
    all_by_role["triagers"] -= all_by_role["approvers"] | all_by_role["maintainers"]

    # Build per-repo list
    by_repo: list[dict] = []
    for repo, role_data in repo_members.items():
        maintainers = sorted(role_data["maintainers"])
        # Approvers must not already be maintainers
        approvers = sorted(role_data["approvers"] - role_data["maintainers"])
        # Triagers must not already be approvers or maintainers
        triagers = sorted(
            role_data["triagers"] - role_data["approvers"] - role_data["maintainers"]
        )
        total = len(triagers) + len(approvers) + len(maintainers)

        teams_for_repo = repo_teams.get(repo, {r: set() for r in ROLES})
        by_repo.append(
            {
                "repo": repo,
                "triagers": len(triagers),
                "approvers": len(approvers),
                "maintainers": len(maintainers),
                "total": total,
                "triager_list": triagers,
                "approver_list": approvers,
                "maintainer_list": maintainers,
                "triager_teams": sorted(teams_for_repo["triagers"]),
                "approver_teams": sorted(teams_for_repo["approvers"]),
                "maintainer_teams": sorted(teams_for_repo["maintainers"]),
            }
        )

    # Sort descending by total so charts show the most active repos first
    by_repo.sort(key=lambda r: r["total"], reverse=True)

    # Build per-user group membership list
    by_user: list[dict] = []
    for username, role_data in user_teams.items():
        maintainer_groups = sorted(role_data["maintainers"])
        # Components already covered by a maintainer team (strip "-maintainers" suffix)
        maintainer_components = {g[: -len("-maintainers")] for g in maintainer_groups if g.endswith("-maintainers")}
        # Exclude approver teams whose component is already covered by a maintainer team
        approver_groups = sorted(
            g for g in role_data["approvers"]
            if not (g.endswith("-approvers") and g[: -len("-approvers")] in maintainer_components)
        )
        # Components covered by maintainer or (remaining) approver teams
        approver_components = {g[: -len("-approvers")] for g in approver_groups if g.endswith("-approvers")}
        covered_components = maintainer_components | approver_components
        # Exclude triager teams whose component is already covered
        triager_groups = sorted(
            g for g in role_data["triagers"]
            if not (g.endswith("-triagers") and g[: -len("-triagers")] in covered_components)
        )
        total = len(triager_groups) + len(approver_groups) + len(maintainer_groups)
        by_user.append(
            {
                "username": username,
                "triager_group_count": len(triager_groups),
                "triager_group_names": ", ".join(triager_groups),
                "approver_group_count": len(approver_groups),
                "approver_group_names": ", ".join(approver_groups),
                "maintainer_group_count": len(maintainer_groups),
                "maintainer_group_names": ", ".join(maintainer_groups),
                "total_groups": total,
            }
        )
    by_user.sort(key=lambda u: (-u["total_groups"], u["username"]))

    avg_groups_per_user = round(sum(u["total_groups"] for u in by_user) / len(by_user), 1) if by_user else 0

    return {
        "generated_at": teams_data["generated_at"],
        "org": teams_data["org"],
        "summary": {
            "total_repos": len(by_repo),
            "repos_with_data": len(by_repo),
            "total_triagers_deduped": len(all_by_role["triagers"]),
            "total_approvers_deduped": len(all_by_role["approvers"]),
            "total_maintainers_deduped": len(all_by_role["maintainers"]),
            "total_triagers_all": sum(r["triagers"] for r in by_repo),
            "total_approvers_all": sum(r["approvers"] for r in by_repo),
            "total_maintainers_all": sum(r["maintainers"] for r in by_repo),
            "avg_triagers_per_repo": round(sum(r["triagers"] for r in by_repo) / len(by_repo), 1) if by_repo else 0,
            "avg_approvers_per_repo": round(sum(r["approvers"] for r in by_repo) / len(by_repo), 1) if by_repo else 0,
            "avg_maintainers_per_repo": round(sum(r["maintainers"] for r in by_repo) / len(by_repo), 1) if by_repo else 0,
            "avg_groups_per_user": avg_groups_per_user,
            "total_unique_users": len(by_user),
        },
        "by_repo": by_repo,
        "by_user": by_user,
    }


def write_outputs(data: dict, output_dir: Path) -> None:
    """Write JSON and CSV files consumed by the Grafana Infinity datasource."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Full dataset (includes member lists)
    full_path = output_dir / "otel-health.json"
    full_path.write_text(json.dumps(data, indent=2))
    logger.info(f"Written: {full_path}")

    # Summary only — used by Grafana stat panels
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "generated_at": data["generated_at"],
                "org": data["org"],
                "summary": data["summary"],
            },
            indent=2,
        )
    )
    logger.info(f"Written: {summary_path}")

    # Per-repo details JSON — used by Grafana table panel (names)
    details_path = output_dir / "by_repo_details.json"
    details = [
        {
            "repo": row["repo"],
            "triagers": row["triagers"],
            "approvers": row["approvers"],
            "maintainers": row["maintainers"],
            "total": row["total"],
            "triager_names": ", ".join(row["triager_list"]),
            "approver_names": ", ".join(row["approver_list"]),
            "maintainer_names": ", ".join(row["maintainer_list"]),
        }
        for row in data["by_repo"]
    ]
    details_path.write_text(json.dumps(details, indent=2))
    logger.info(f"Written: {details_path}")

    # Per-repo CSV — used by Grafana bar chart panel
    csv_path = output_dir / "by_repo.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["repo", "triagers", "approvers", "maintainers", "total"]
        )
        writer.writeheader()
        for row in data["by_repo"]:
            writer.writerow(
                {
                    "repo": row["repo"],
                    "triagers": row["triagers"],
                    "approvers": row["approvers"],
                    "maintainers": row["maintainers"],
                    "total": row["total"],
                }
            )
    logger.info(f"Written: {csv_path}")

    # Per-user group membership JSON — used by Grafana table panel
    user_path = output_dir / "by_user_groups.json"
    user_path.write_text(json.dumps(data["by_user"], indent=2))
    logger.info(f"Written: {user_path}")


def main() -> None:
    parser = ArgumentParser(
        description="Process teams.json and generate Grafana data files"
    )
    parser.add_argument(
        "--teams-file",
        default="output/teams.json",
        help="Path to teams.json (generated by otel_health.teams)",
    )
    parser.add_argument("--output-dir", default="output", help="Output directory")
    args = parser.parse_args()

    teams_data = load_teams(Path(args.teams_file))
    data = compute_stats(teams_data)
    write_outputs(data, Path(args.output_dir))

    s = data["summary"]
    print(f"\n{'=' * 52}")
    print(f"  OTel Community Health  —  {data['org']}")
    print(f"{'=' * 52}")
    print(f"  Repos with data: {s['total_repos']}")
    print(
        f"  Triagers:    {s['total_triagers_deduped']:4d} unique  "
        f"({s['total_triagers_all']} total assignments)"
    )
    print(
        f"  Approvers:   {s['total_approvers_deduped']:4d} unique  "
        f"({s['total_approvers_all']} total assignments)"
    )
    print(
        f"  Maintainers: {s['total_maintainers_deduped']:4d} unique  "
        f"({s['total_maintainers_all']} total assignments)"
    )
    print(f"{'=' * 52}")
    print(f"\n  Output written to: {args.output_dir}/")
    print("  Run: docker compose up -d")
    print("  Dashboard: http://localhost:3000\n")


if __name__ == "__main__":
    main()
