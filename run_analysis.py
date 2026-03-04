#!/usr/bin/env python3
"""
OTel Community Health Runner

Three-step pipeline:
  Step 1 — otel_health.teams:     fetch all GitHub org teams → output/teams.json
  Step 2 — otel_health.collector: process teams.json         → output/summary.json
                                                                output/by_repo_details.json
  Step 3 — otel_health.activity:  fetch weekly contributor
                                   activity per repo          → output/activity.json
Then launches the Grafana dashboard via Docker Compose.
"""

import os
import sys
import subprocess


class Colors:
    GREEN = "\033[0;32m"
    BLUE = "\033[0;34m"
    YELLOW = "\033[1;33m"
    RED = "\033[0;31m"
    NC = "\033[0m"


def print_colored(message, color):
    print(f"{color}{message}{Colors.NC}")


def print_header():
    print_colored("=" * 60, Colors.BLUE)
    print_colored("   OTel Community Health", Colors.BLUE)
    print_colored("=" * 60, Colors.BLUE)
    print()


def check_github_token():
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print_colored("Error: GITHUB_TOKEN environment variable not set", Colors.RED)
        print()
        print("Please set your GitHub token:")
        print('  export GITHUB_TOKEN="your_github_token_here"')
        print()
        print("Create a token at: https://github.com/settings/tokens")
        print("Required scopes: read:org")
        print()
        sys.exit(1)
    return token


def check_docker():
    try:
        subprocess.run(["docker", "--version"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def run_step(label: str, module: str, extra_args: list[str]) -> bool:
    print_colored(f"Step: {label}", Colors.BLUE)
    print()
    try:
        subprocess.run(
            [sys.executable, "-m", module] + extra_args,
            check=True,
        )
        print()
        print_colored(f"{label} — done.", Colors.GREEN)
        print()
        return True
    except subprocess.CalledProcessError as e:
        print()
        print_colored(f"{label} failed (exit code {e.returncode})", Colors.RED)
        return False
    except Exception as e:
        print()
        print_colored(f"{label} failed: {e}", Colors.RED)
        return False


def start_dashboard():
    print_colored("Starting Grafana dashboard...", Colors.BLUE)
    try:
        subprocess.run(["docker", "compose", "up", "-d"], check=True)
        print()
        print_colored("=" * 60, Colors.GREEN)
        print_colored("   Dashboard Ready!", Colors.GREEN)
        print_colored("=" * 60, Colors.GREEN)
        print()
        print(f"  Dashboard URL: {Colors.BLUE}http://localhost:3000{Colors.NC}")
        print(f"  Username:      {Colors.BLUE}admin{Colors.NC}")
        print(f"  Password:      {Colors.BLUE}admin{Colors.NC}")
        print()
        print_colored(
            "Note: It may take 10-20 seconds for Grafana to fully start.",
            Colors.YELLOW,
        )
        print()
        print("To stop the dashboard:")
        print("  docker compose down")
        print()
        return True
    except subprocess.CalledProcessError as e:
        print_colored(f"Failed to start dashboard: {e}", Colors.RED)
        return False
    except FileNotFoundError:
        print_colored("Error: docker command not found", Colors.RED)
        return False


def parse_shared_args(
    argv: list[str],
) -> tuple[list[str], list[str], list[str], bool]:
    """
    Split CLI args into those relevant for each module.
    All modules share --org, --output-dir, --cache-dir.
    collector.py additionally accepts --teams-file.
    activity.py additionally accepts --teams-file, --weeks, --top-repos.
    --skip-activity suppresses step 3.
    """
    teams_args: list[str] = []
    collector_args: list[str] = []
    activity_args: list[str] = []
    skip_activity = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--org", "--output-dir", "--cache-dir"):
            val = argv[i + 1] if i + 1 < len(argv) else ""
            teams_args += [arg, val]
            collector_args += [arg, val]
            activity_args += [arg, val]
            i += 2
        elif arg == "--teams-file":
            val = argv[i + 1] if i + 1 < len(argv) else ""
            collector_args += [arg, val]
            activity_args += [arg, val]
            i += 2
        elif arg in ("--weeks", "--top-repos"):
            val = argv[i + 1] if i + 1 < len(argv) else ""
            activity_args += [arg, val]
            i += 2
        elif arg == "--skip-activity":
            skip_activity = True
            i += 1
        else:
            i += 1
    return teams_args, collector_args, activity_args, skip_activity


def main():
    print_header()
    check_github_token()

    docker_available = check_docker()
    if not docker_available:
        print_colored(
            "Warning: Docker is not installed. Dashboard will not start automatically.",
            Colors.YELLOW,
        )
        print()

    teams_args, collector_args, activity_args, skip_activity = parse_shared_args(
        sys.argv[1:]
    )

    # Step 1: fetch teams from GitHub → output/teams.json
    if not run_step(
        "Fetching org teams from GitHub", "otel_health.teams", teams_args
    ):
        sys.exit(1)

    # Step 2: process teams.json → summary.json + by_repo_details.json
    if not run_step(
        "Computing health metrics", "otel_health.collector", collector_args
    ):
        sys.exit(1)

    # Step 3: fetch weekly contributor activity → output/activity.json
    if skip_activity:
        print_colored("Skipping activity data collection (--skip-activity).", Colors.YELLOW)
        print()
    else:
        if not run_step(
            "Fetching contributor activity (past 26 weeks)", "otel_health.activity", activity_args
        ):
            print_colored(
                "Warning: activity data collection failed. Dashboard will load without it.",
                Colors.YELLOW,
            )
            print()

    if docker_available:
        start_dashboard()
    else:
        print_colored(
            "Install Docker to launch the Grafana dashboard.",
            Colors.YELLOW,
        )


if __name__ == "__main__":
    main()
