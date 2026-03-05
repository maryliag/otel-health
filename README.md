# otel-health

Collects triager, approver, and maintainer counts across all public repositories in the [open-telemetry](https://github.com/open-telemetry) GitHub organization and visualizes them in a Grafana dashboard.

## How it works

The pipeline runs in three steps:

1. **`otel_health.teams`** — queries the [GitHub Teams API](https://docs.github.com/en/rest/teams) for every team in the org.
   OpenTelemetry follows the convention `{component}-triagers`, `{component}-approvers`, `{component}-maintainers`.
   For each team it fetches the direct member list and the linked repositories,
   and writes everything to **`output/teams.json`** — a human-readable mapping of all groups and their repos.

2. **`otel_health.collector`** — reads `output/teams.json`, aggregates counts per repo and org-wide (deduplicated), and writes the Grafana data files.
   Role deduplication is applied: a maintainer is not also counted as an approver or triager; an approver is not also counted as a triager.

3. **`otel_health.activity`** — fetches weekly unique contributor counts per repository for the past 52 weeks (1 Year) and writes **`output/activity.json`**.
   Activity includes: commits authored, issues/PRs opened, and issue/PR comments.
   Only the top repositories by team member count are included (default: 20, configurable with `--top-repos`).

4. A Docker Compose stack (file-server + Grafana) serves the output files and renders the dashboard.

## Prerequisites

- Python 3.11+ with [uv](https://github.com/astral-sh/uv)
- Docker with Compose plugin
- A GitHub personal access token with **`read:org`** scope (required to list team members)

## Quick start

```bash
# 1. Install dependencies
uv sync

# 2. Set your GitHub token
export GITHUB_TOKEN="ghp_..."

# 3. Run collection + launch dashboard
uv run python run_analysis.py
```

Open **http://localhost:3000** (login: `admin` / `admin`).

To stop:
```bash
docker compose down
```

## Dashboard panels

| Panel | Description |
|---|---|
| Unique Triagers | Deduplicated count of triagers across all repos |
| Unique Approvers | Deduplicated count of approvers across all repos |
| Unique Maintainers | Deduplicated count of maintainers across all repos |
| Total Repositories | Repos that have at least one role defined |
| Avg Triagers / Repo | Average number of triagers per repository |
| Avg Approvers / Repo | Average number of approvers per repository |
| Avg Maintainers / Repo | Average number of maintainers per repository |
| Repository Details | Table of all repos sorted alphabetically, with role counts, member usernames, and clickable GitHub links |
| Weekly Unique Contributors by Repository | Line chart of weekly unique contributors per repo over the past year |

## Output files

| File | Contents |
|---|---|
| `output/teams.json` | All org teams with members and linked repos (human-readable reference) |
| `output/summary.json` | Aggregate counts and averages (for Grafana stat panels) |
| `output/by_repo_details.json` | Per-repo counts and member usernames (for Grafana table panel) |
| `output/by_repo.csv` | Per-repo counts (for external use) |
| `output/otel-health.json` | Full dataset including member lists and team names per role |
| `output/activity.json` | Weekly unique contributor counts per repo for the past year |
| `output/contributors_by_week.json` | Weekly contributor usernames per repo (only weeks with activity) |

## Options

```
uv run python run_analysis.py --org open-telemetry --output-dir output --cache-dir cache
```

Pass `--skip-activity` to skip step 3 (faster, but the weekly contributors chart will not be populated):

```bash
uv run python run_analysis.py --skip-activity
```

Activity options (passed through to step 3):

| Flag | Default | Description |
|---|---|---|
| `--weeks N` | 52 | Number of past weeks to collect |
| `--top-repos N` | 20 | Repos to include, ranked by team member count (0 = all) |

You can also run each step independently:

```bash
# Step 1 only — fetch teams and write output/teams.json
uv run python -m otel_health.teams

# Step 2 only — reprocess an existing teams.json (no GitHub API call)
uv run python -m otel_health.collector --teams-file output/teams.json

# Step 3 only — fetch contributor activity (reads teams.json, calls GitHub API)
uv run python -m otel_health.activity --weeks 52 --top-repos 100
```

The cache directory stores GitHub API responses to speed up re-runs and avoid hitting rate limits.
To force a full refresh, delete the `cache/` directory.
