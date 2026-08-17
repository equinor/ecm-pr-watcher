"""GitHub CLI wrapper — all GitHub API calls go through `gh`."""
from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional


_OPEN_PRS_QUERY = """
query($owner: String!, $name: String!, $endCursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(
      states: OPEN
      first: 100
      after: $endCursor
      orderBy: {field: CREATED_AT, direction: DESC}
    ) {
      nodes {
        number
        title
        author { login }
        labels(first: 100) { nodes { name } }
        reviewDecision
        createdAt
        updatedAt
        url
        isDraft
        headRefName
        totalCommentsCount
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
""".strip()


def check_auth() -> Optional[str]:
    """Return None if `gh` is installed and authenticated, else an error string."""
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return result.stderr.strip() or "Not authenticated. Run: gh auth login"
        return None
    except FileNotFoundError:
        return "GitHub CLI (gh) is not installed.\nInstall from: https://cli.github.com/"
    except subprocess.TimeoutExpired:
        return "Timed out checking gh authentication status."


def fetch_team_repos(org: str, team_slug: str) -> list[str]:
    """
    Return a list of full repo names (owner/repo) for the given GitHub team.
    Paginates automatically.
    """
    all_repos: list[str] = []
    page = 1

    while True:
        result = subprocess.run(
            [
                "gh",
                "api",
                f"orgs/{org}/teams/{team_slug}/repos?per_page=100&page={page}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip()
                or f"Failed to fetch repos for team '{team_slug}' in org '{org}'"
            )

        repos: list[dict] = json.loads(result.stdout)
        all_repos.extend(r["full_name"] for r in repos)

        if len(repos) < 100:
            break
        page += 1

    return all_repos


def fetch_prs_for_repo(repo: str) -> list[dict]:
    """Return open PRs for a single repository with rich metadata."""
    owner, name = repo.split("/", 1)
    result = subprocess.run(
        [
            "gh",
            "api",
            "graphql",
            "--paginate",
            "--slurp",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-f",
            f"query={_OPEN_PRS_QUERY}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    if result.returncode != 0:
        stderr = result.stderr.strip().lower()
        disabled_markers = [
            "pull requests are disabled",
            "issues and pull requests are disabled",
            "this repository has disabled issues",
        ]
        if any(m in stderr for m in disabled_markers):
            return []
        raise RuntimeError(f"{repo}: {result.stderr.strip()}")

    pages: list[dict] = json.loads(result.stdout)
    prs: list[dict] = []
    for page in pages:
        nodes = page["data"]["repository"]["pullRequests"]["nodes"]
        for pr in nodes:
            pr["labels"] = pr["labels"]["nodes"]
            pr["repository"] = repo
            prs.append(pr)
    return prs


def fetch_all_team_prs(org: str, team_slug: str) -> list[dict]:
    """
    Fetch all open PRs across every repo the team has access to.
    Uses up to 8 concurrent `gh pr list` calls for speed.
    Per-repo errors are silently skipped to avoid blocking the full refresh.
    """
    repos = fetch_team_repos(org, team_slug)

    all_prs: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(fetch_prs_for_repo, r): r for r in repos}
        for future in as_completed(futures):
            try:
                all_prs.extend(future.result())
            except Exception:
                # Individual repo failures are non-fatal
                pass

    # Newest first
    all_prs.sort(key=lambda pr: pr.get("createdAt", ""), reverse=True)
    return all_prs
