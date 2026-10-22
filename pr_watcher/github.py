"""GitHub CLI wrapper — all GitHub API calls go through `gh`."""
from __future__ import annotations

import json
import subprocess
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from itertools import islice
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


class RateLimitError(RuntimeError):
    def __init__(self, message: str, retry_at: Optional[datetime] = None) -> None:
        super().__init__(message)
        self.retry_at = retry_at


def _rate_limit_retry_at(headers: dict[str, str]) -> Optional[datetime]:
    deadlines: list[datetime] = []
    if headers.get("retry-after", "").isdigit():
        try:
            deadlines.append(
                datetime.now(timezone.utc)
                + timedelta(seconds=int(headers["retry-after"]))
            )
        except OverflowError:
            pass
    if headers.get("x-ratelimit-remaining") == "0":
        try:
            deadlines.append(
                datetime.fromtimestamp(int(headers["x-ratelimit-reset"]), timezone.utc)
            )
        except (KeyError, ValueError, OverflowError, OSError):
            pass
    return max(deadlines) if deadlines else None


def _api_json(endpoint: str, *args: str) -> dict | list:
    result = subprocess.run(
        ["gh", "api", endpoint, "--include", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout.replace("\r\n", "\n")
    headers: dict[str, str] = {}
    status = ""
    body = output
    if output.startswith("HTTP/"):
        header_block, _, body = output.partition("\n\n")
        status_line, *header_lines = header_block.splitlines()
        status = status_line.split()[1]
        for line in header_lines:
            key, separator, value = line.partition(":")
            if separator:
                headers[key.lower()] = value.strip()

    payload: dict | list = {}
    if body.strip():
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            if result.returncode == 0:
                raise
    errors = payload.get("errors") if isinstance(payload, dict) else None
    if result.returncode != 0 or errors or status in {"403", "429"}:
        api_error = json.dumps(errors) if errors else (
            payload.get("message", "") if isinstance(payload, dict) else ""
        )
        message = (
            result.stderr.strip() or api_error or body.strip()
            or f"GitHub API request failed: {endpoint}"
        )
        error_text = f"{message}\n{api_error}".lower()
        if (
            status == "429"
            or headers.get("x-ratelimit-remaining") == "0"
            or any(marker in error_text for marker in (
                "rate limit", "rate_limit", "abuse detection",
            ))
        ):
            raise RateLimitError(message, _rate_limit_retry_at(headers))
        raise RuntimeError(message)
    return payload


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
        repos = _api_json(
            f"orgs/{org}/teams/{team_slug}/repos?per_page=100&page={page}"
        )
        if not isinstance(repos, list):
            raise RuntimeError(f"Unexpected repository list for team '{team_slug}'")
        all_repos.extend(r["full_name"] for r in repos)

        if len(repos) < 100:
            break
        page += 1

    return all_repos


def fetch_prs_for_repo(repo: str) -> list[dict]:
    """Return open PRs for a single repository with rich metadata."""
    owner, name = repo.split("/", 1)
    prs: list[dict] = []
    cursor: Optional[str] = None
    while True:
        args = [
            "-f", f"owner={owner}",
            "-f", f"name={name}",
            "-f", f"query={_OPEN_PRS_QUERY}",
        ]
        if cursor is not None:
            args.extend(["-f", f"endCursor={cursor}"])
        try:
            page = _api_json("graphql", *args)
        except RateLimitError:
            raise
        except RuntimeError as exc:
            disabled_markers = [
                "pull requests are disabled",
                "issues and pull requests are disabled",
                "this repository has disabled issues",
            ]
            if any(marker in str(exc).lower() for marker in disabled_markers):
                return []
            raise RuntimeError(f"{repo}: {exc}") from exc

        if not isinstance(page, dict):
            raise RuntimeError(f"{repo}: Unexpected GraphQL response")
        connection = page["data"]["repository"]["pullRequests"]
        for pr in connection["nodes"]:
            pr["labels"] = pr["labels"]["nodes"]
            pr["repository"] = repo
            prs.append(pr)
        if not connection["pageInfo"]["hasNextPage"]:
            break
        cursor = connection["pageInfo"]["endCursor"]
        if not cursor:
            raise RuntimeError(f"{repo}: Missing GraphQL pagination cursor")
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
    Rate limits stop scheduling new requests and are surfaced to the caller.
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
    remaining_repos = iter(repos)
    with ThreadPoolExecutor(max_workers=_MAX_CONCURRENT_FETCHES) as executor:
        futures = {
            executor.submit(fetch_prs_for_repo, r): r
            for r in islice(remaining_repos, _MAX_CONCURRENT_FETCHES)
        }
        while futures:
            completed, _ = wait(futures, return_when=FIRST_COMPLETED)
            for future in completed:
                repo = futures.pop(future)
                try:
                    all_prs.extend(future.result())
                except RateLimitError:
                    for pending in futures:
                        pending.cancel()
                    raise
                except Exception:
                    # Individual repo failures are non-fatal
                    all_prs.extend(cached_by_repo.get(repo, []))
                    if failed_repos is not None:
                        failed_repos.append(repo)
            for repo in islice(
                remaining_repos, _MAX_CONCURRENT_FETCHES - len(futures)
            ):
                futures[executor.submit(fetch_prs_for_repo, repo)] = repo

    # Newest first
    all_prs.sort(key=lambda pr: pr.get("createdAt", ""), reverse=True)
    return all_prs
