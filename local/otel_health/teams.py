#!/usr/bin/env python3
"""
Fetch all triager/approver/maintainer teams from a GitHub org and write
a human-readable teams.json mapping file.

OpenTelemetry follows the convention:
  {component}-triagers   → triage permission
  {component}-approvers  → write permission  (parent: triagers)
  {component}-maintainers → maintain permission (parent: approvers)

Each team is linked to one or more repositories via GitHub's team-repos
relationship.  This script captures that mapping together with the list of
direct members so that collector.py can derive per-repo role counts without
parsing CODEOWNERS files.
"""

import hashlib
import json
import logging
import os
import time
from argparse import ArgumentParser
from datetime import datetime, timezone
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
DEFAULT_ORG = "open-telemetry"

# Suffixes that map to a contributor role
_ROLE_SUFFIXES: list[tuple[str, str]] = [
    ("-maintainers", "maintainers"),
    ("-maintainer", "maintainers"),
    ("-approvers", "approvers"),
    ("-approver", "approvers"),
    ("-triagers", "triagers"),
    ("-triager", "triagers"),
]


# ---------------------------------------------------------------------------
# Shared HTTP / caching helpers
# ---------------------------------------------------------------------------


class RateLimiter:
    def check(self, headers: dict) -> None:
        remaining = int(headers.get("X-RateLimit-Remaining", 9999))
        reset_at = int(headers.get("X-RateLimit-Reset", 0))
        if remaining < 50:
            wait = max(0, reset_at - int(time.time())) + 5
            logger.warning(
                f"Rate limit low ({remaining} remaining). Sleeping {wait}s..."
            )
            time.sleep(wait)


class Cache:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def _path(self, key: str) -> Path:
        h = hashlib.md5(key.encode()).hexdigest()
        return self.cache_dir / f"{h}.json"

    def get(self, key: str):
        p = self._path(key)
        if p.exists():
            self.hits += 1
            return json.loads(p.read_text())
        self.misses += 1
        return None

    def set(self, key: str, value) -> None:
        self._path(key).write_text(json.dumps(value))

    def stats(self) -> str:
        total = self.hits + self.misses
        rate = self.hits / total * 100 if total > 0 else 0
        return f"Cache: {self.hits}/{total} hits ({rate:.0f}%)"


class GitHubClient:
    def __init__(self, token: str, cache: Cache):
        self.cache = cache
        self.rate_limiter = RateLimiter()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "otel-health-teams/1.0",
            }
        )

    def get(self, url: str, params: dict | None = None) -> dict | list | None:
        cache_key = f"GET:{url}:{json.dumps(params or {}, sort_keys=True)}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            response = self.session.get(url, params=params, timeout=30)
            self.rate_limiter.check(dict(response.headers))
            if response.status_code == 429 or (
                response.status_code == 403
                and int(response.headers.get("X-RateLimit-Remaining", 9999)) == 0
            ):
                reset_at = int(response.headers.get("X-RateLimit-Reset", 0))
                wait = max(0, reset_at - int(time.time()))
                logger.error(
                    f"GitHub rate limit exceeded for {url}. "
                    f"Resets in {wait}s (at {datetime.fromtimestamp(reset_at, tz=timezone.utc).isoformat()}). "
                    f"Response: {response.text[:200]}"
                )
                return None
            if response.status_code in (403, 404):
                self.cache.set(cache_key, None)
                return None
            response.raise_for_status()
            data = response.json()
            self.cache.set(cache_key, data)
            return data
        except requests.RequestException as e:
            logger.warning(f"Request failed {url}: {e}")
            return None

    def get_all_pages(self, url: str, params: dict | None = None) -> list:
        results = []
        page = 1
        while True:
            data = self.get(url, {**(params or {}), "per_page": 100, "page": page})
            if not data:
                break
            results.extend(data)
            if len(data) < 100:
                break
            page += 1
        return results


# ---------------------------------------------------------------------------
# Team processing
# ---------------------------------------------------------------------------


def determine_role(team_name: str) -> str:
    """Return 'triagers', 'approvers', 'maintainers', or 'other'."""
    name_lower = team_name.lower()
    for suffix, role in _ROLE_SUFFIXES:
        if name_lower.endswith(suffix):
            return role
    return "other"


def fetch_teams_data(client: GitHubClient, org: str) -> dict:
    """
    Fetch all teams in the org along with their direct members and linked repos.

    Returns a dict ready to be serialised as teams.json.

    Member format: list of username strings.
    """
    logger.info(f"Listing all teams for {org} ...")
    raw_teams = client.get_all_pages(f"{GITHUB_API}/orgs/{org}/teams")
    logger.info(f"Found {len(raw_teams)} teams total")

    teams: list[dict] = []
    role_counts = {"triagers": 0, "approvers": 0, "maintainers": 0, "other": 0}

    for i, raw in enumerate(raw_teams, 1):
        slug = raw["slug"]
        name = raw["name"]
        role = determine_role(name)
        role_counts[role] += 1

        logger.info(f"[{i:3d}/{len(raw_teams)}] {name}  ({role})")

        # Direct members only (not inherited through parent teams)
        members_raw = client.get_all_pages(
            f"{GITHUB_API}/orgs/{org}/teams/{slug}/members"
        )
        members = sorted(m["login"].lower() for m in (members_raw or []))

        # Repos this team has explicit access to
        repos_raw = client.get_all_pages(
            f"{GITHUB_API}/orgs/{org}/teams/{slug}/repos"
        )
        repos = sorted(r["name"] for r in (repos_raw or []) if not r.get("private"))

        teams.append(
            {
                "name": name,
                "slug": slug,
                "role": role,
                "member_count": len(members),
                "members": members,
                "repos": repos,
            }
        )

    # Sort: by first repo name, then role, then team name
    teams.sort(
        key=lambda t: (t["repos"][0] if t["repos"] else "\xff", t["role"], t["name"])
    )

    logger.info(client.cache.stats())

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "org": org,
        "stats": {
            "total_teams": len(teams),
            "triager_teams": role_counts["triagers"],
            "approver_teams": role_counts["approvers"],
            "maintainer_teams": role_counts["maintainers"],
            "other_teams": role_counts["other"],
        },
        "teams": teams,
    }


def write_teams_file(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))
    logger.info(f"Written: {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = ArgumentParser(
        description="Fetch all OTel org teams and write output/teams.json"
    )
    parser.add_argument("--org", default=DEFAULT_ORG, help="GitHub organization name")
    parser.add_argument("--output-dir", default="output", help="Output directory")
    parser.add_argument("--cache-dir", default="cache", help="Cache directory")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("ERROR: GITHUB_TOKEN environment variable is required")

    cache = Cache(Path(args.cache_dir))
    client = GitHubClient(token, cache)

    data = fetch_teams_data(client, args.org)
    out_path = Path(args.output_dir) / "teams.json"
    write_teams_file(data, out_path)

    s = data["stats"]
    print(f"\n{'=' * 52}")
    print(f"  OTel Teams  —  {args.org}")
    print(f"{'=' * 52}")
    print(f"  Total teams:      {s['total_teams']}")
    print(f"  Triager teams:    {s['triager_teams']}")
    print(f"  Approver teams:   {s['approver_teams']}")
    print(f"  Maintainer teams: {s['maintainer_teams']}")
    print(f"  Other teams:      {s['other_teams']}")
    print(f"{'=' * 52}")
    print(f"\n  Teams file written to: {out_path}\n")


if __name__ == "__main__":
    main()
