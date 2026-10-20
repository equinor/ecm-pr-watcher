"""GitHub CLI wrapper — all GitHub API calls go through `gh`."""
from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional


_MAX_CONCURRENT_FETCHES = 5

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
        reviewRequests(first: 100) {
          nodes {
            requestedReviewer {
              ... on User { login }
              ... on Bot { login }
              ... on Mannequin { login }
              ... on Team { slug }
            }
          }
        }
        latestReviews(first: 100) {
          nodes {
            author { login }
            state
          }
        }
        labels(first: 100) { nodes { name } }
        reviewDecision
        createdAt
        updatedAt
        url
        isDraft
        commits(last: 1) {
          nodes {
            commit {
              statusCheckRollup { state }
            }
          }
        }
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


def is_repo_ignored(repo: str, ignore_repos: list[str]) -> bool:
    """Match `owner/name` against entries given as `name` or `owner/name`, case-insensitively."""
    full = repo.lower()
    name = full.split("/", 1)[-1]
    for entry in ignore_repos:
        entry = entry.strip().lower()
        if entry == full or ("/" not in entry and entry == name):
            return True
    return False


def fetch_all_team_prs(
    org: str,
    team_slug: str,
    cached_prs: Optional[list[dict]] = None,
    failed_repos: Optional[list[str]] = None,
    ignore_repos: Optional[list[str]] = None,
) -> list[dict]:
    """
    Fetch all open PRs across every repo the team has access to.
    Uses up to 5 concurrent GitHub API calls.
    Per-repo errors reuse cached PRs and are added to `failed_repos` when provided.
    Repos matching `ignore_repos` are skipped entirely.
    """
    repos = [
        r for r in fetch_team_repos(org, team_slug)
        if not is_repo_ignored(r, ignore_repos or [])
    ]
    cached_by_repo: dict[str, list[dict]] = {}
    for pr in cached_prs or []:
        repo = pr.get("repository")
        if repo:
            cached_by_repo.setdefault(repo, []).append(pr)

    all_prs: list[dict] = []
    with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_FETCHES) as executor:
        futures = {executor.submit(fetch_prs_for_repo, r): r for r in repos}
        for future in as_completed(futures):
            repo = futures[future]
            try:
                all_prs.extend(future.result())
            except Exception:
                # Individual repo failures are non-fatal
                all_prs.extend(cached_by_repo.get(repo, []))
                if failed_repos is not None:
                    failed_repos.append(repo)

    # Newest first
    all_prs.sort(key=lambda pr: pr.get("createdAt", ""), reverse=True)
    return all_prs
