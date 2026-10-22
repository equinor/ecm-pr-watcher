"""Tests for pr_watcher/github.py — subprocesses are fully mocked."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from threading import Barrier, Event
from unittest.mock import MagicMock, patch

import pytest

from pr_watcher import github


def _run_result(stdout="", stderr="", returncode=0, headers=None, status=200):
    m = MagicMock()
    header_lines = [f"HTTP/2.0 {status}"]
    header_lines.extend(f"{key}: {value}" for key, value in (headers or {}).items())
    m.stdout = "\r\n".join(header_lines) + "\r\n\r\n" + stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


def _graphql_page(prs, has_next_page=False, end_cursor=None):
    return {
        "data": {
            "repository": {
                "pullRequests": {
                    "nodes": prs,
                    "pageInfo": {
                        "hasNextPage": has_next_page,
                        "endCursor": end_cursor,
                    },
                }
            }
        }
    }


# ---------------------------------------------------------------------------
# check_auth
# ---------------------------------------------------------------------------

class TestCheckAuth:
    def test_authenticated_returns_none(self):
        with patch("pr_watcher.github.subprocess.run", return_value=_run_result(returncode=0)):
            assert github.check_auth() is None

    def test_not_authenticated_returns_message(self):
        with patch("pr_watcher.github.subprocess.run", return_value=_run_result(stderr="not logged in", returncode=1)):
            result = github.check_auth()
            assert result is not None
            assert "not logged in" in result

    def test_gh_not_installed(self):
        with patch("pr_watcher.github.subprocess.run", side_effect=FileNotFoundError):
            result = github.check_auth()
            assert "not installed" in result.lower()

    def test_timeout(self):
        import subprocess
        with patch("pr_watcher.github.subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 10)):
            result = github.check_auth()
            assert "timed out" in result.lower()


# ---------------------------------------------------------------------------
# fetch_team_repos
# ---------------------------------------------------------------------------

class TestFetchTeamRepos:
    def test_returns_full_names(self):
        repos = [{"full_name": "Equinor/repo-a"}, {"full_name": "Equinor/repo-b"}]
        with patch("pr_watcher.github.subprocess.run", return_value=_run_result(stdout=json.dumps(repos))):
            result = github.fetch_team_repos("Equinor", "my-team")
        assert result == ["Equinor/repo-a", "Equinor/repo-b"]

    def test_paginates_until_fewer_than_100(self):
        page1 = [{"full_name": f"Equinor/repo-{i}"} for i in range(100)]
        page2 = [{"full_name": "Equinor/repo-last"}]
        responses = [_run_result(stdout=json.dumps(page1)), _run_result(stdout=json.dumps(page2))]
        with patch("pr_watcher.github.subprocess.run", side_effect=responses):
            result = github.fetch_team_repos("Equinor", "my-team")
        assert len(result) == 101
        assert result[-1] == "Equinor/repo-last"

    def test_raises_on_error(self):
        with patch("pr_watcher.github.subprocess.run", return_value=_run_result(stderr="forbidden", returncode=1)):
            with pytest.raises(RuntimeError, match="forbidden"):
                github.fetch_team_repos("Equinor", "my-team")


# ---------------------------------------------------------------------------
# fetch_prs_for_repo
# ---------------------------------------------------------------------------

class TestFetchPrsForRepo:
    def test_returns_prs_with_repository_field(self):
        prs = [
            {
                "number": 1,
                "title": "Fix bug",
                "labels": {"nodes": [{"name": "bug"}]},
                "totalCommentsCount": 4,
                "commits": {"nodes": [
                    {"commit": {"statusCheckRollup": {"state": "SUCCESS"}}},
                ]},
                "reviewRequests": {"nodes": [
                    {"requestedReviewer": {"login": "alice"}},
                    {"requestedReviewer": {"slug": "my-team"}},
                ]},
                "latestReviews": {"nodes": [
                    {"author": {"login": "bob"}, "state": "APPROVED"},
                ]},
            },
            {
                "number": 2,
                "title": "Add feature",
                "labels": {"nodes": []},
                "totalCommentsCount": 2,
            },
        ]
        with patch(
            "pr_watcher.github.subprocess.run",
            return_value=_run_result(stdout=json.dumps(_graphql_page(prs))),
        ) as run:
            result = github.fetch_prs_for_repo("Equinor/my-repo")
        assert all(pr["repository"] == "Equinor/my-repo" for pr in result)
        assert len(result) == 2
        assert result[0]["labels"] == [{"name": "bug"}]
        assert result[0]["totalCommentsCount"] == 4
        assert result[0]["commits"] == prs[0]["commits"]
        assert result[0]["reviewRequests"] == prs[0]["reviewRequests"]
        assert result[0]["latestReviews"] == prs[0]["latestReviews"]
        command = run.call_args.args[0]
        query_arg = next(arg for arg in command if arg.startswith("query="))
        assert "totalCommentsCount" in query_arg
        assert "updatedAt" in query_arg
        assert "commits(last: 1)" in query_arg
        assert "statusCheckRollup { state }" in query_arg
        assert "reviewRequests(first: 100)" in query_arg
        assert "... on Team { slug }" in query_arg
        assert "... on User { login }" in query_arg
        assert "... on Bot { login }" in query_arg
        assert "... on Mannequin { login }" in query_arg
        assert "latestReviews(first: 100)" in query_arg
        assert query_arg == query_arg.strip()
        assert "--include" in command
        assert "--paginate" not in command

    def test_merges_paginated_results(self):
        page1 = _graphql_page([
            {"number": 1, "labels": {"nodes": []}, "totalCommentsCount": 1}
        ], has_next_page=True, end_cursor="next-page")
        page2 = _graphql_page([
            {"number": 2, "labels": {"nodes": []}, "totalCommentsCount": 2}
        ])
        with patch(
            "pr_watcher.github.subprocess.run",
            side_effect=[
                _run_result(stdout=json.dumps(page1)),
                _run_result(stdout=json.dumps(page2)),
            ],
        ) as run:
            result = github.fetch_prs_for_repo("Equinor/my-repo")
        assert [pr["number"] for pr in result] == [1, 2]
        assert run.call_count == 2
        assert "endCursor=next-page" in run.call_args.args[0]

    def test_missing_pagination_cursor_raises(self) -> None:
        with patch(
            "pr_watcher.github.subprocess.run",
            return_value=_run_result(
                stdout=json.dumps(_graphql_page([], has_next_page=True))
            ),
        ):
            with pytest.raises(RuntimeError, match="Missing GraphQL pagination cursor"):
                github.fetch_prs_for_repo("Equinor/my-repo")

    def test_disabled_prs_returns_empty(self):
        with patch("pr_watcher.github.subprocess.run", return_value=_run_result(
            stderr="pull requests are disabled", returncode=1
        )):
            assert github.fetch_prs_for_repo("Equinor/no-prs") == []

    def test_other_error_raises(self):
        with patch("pr_watcher.github.subprocess.run", return_value=_run_result(
            stderr="API rate limit exceeded", returncode=1
        )):
            with pytest.raises(RuntimeError, match="API rate limit exceeded"):
                github.fetch_prs_for_repo("Equinor/my-repo")

    def test_graphql_error_is_not_confused_with_pr_content(self) -> None:
        page = _graphql_page([{"title": "Fix rate limit handling"}])
        page["errors"] = [{"type": "FORBIDDEN", "message": "Resource not accessible"}]
        with patch(
            "pr_watcher.github.subprocess.run",
            return_value=_run_result(stdout=json.dumps(page)),
        ):
            with pytest.raises(RuntimeError, match="Resource not accessible") as exc:
                github.fetch_prs_for_repo("Equinor/my-repo")
        assert not isinstance(exc.value, github.RateLimitError)


# ---------------------------------------------------------------------------
# fetch_all_team_prs
# ---------------------------------------------------------------------------

class TestFetchAllTeamPrs:
    def test_limits_concurrent_fetches_to_five(self):
        real_executor = github.ThreadPoolExecutor
        worker_counts = []

        class RecordingExecutor(real_executor):
            def __init__(self, max_workers):
                worker_counts.append(max_workers)
                super().__init__(max_workers=max_workers)

        with (
            patch("pr_watcher.github.fetch_team_repos", return_value=[]),
            patch("pr_watcher.github.ThreadPoolExecutor", RecordingExecutor),
        ):
            github.fetch_all_team_prs("Equinor", "my-team")

        assert worker_counts == [5]

    def test_sorted_newest_first(self):
        prs = [
            {
                "number": 1,
                "createdAt": "2024-01-01T00:00:00Z",
                "labels": {"nodes": []},
            },
            {
                "number": 2,
                "createdAt": "2024-06-01T00:00:00Z",
                "labels": {"nodes": []},
            },
        ]
        with patch("pr_watcher.github.fetch_team_repos", return_value=["Equinor/repo-a"]):
            with patch(
                "pr_watcher.github.subprocess.run",
                return_value=_run_result(stdout=json.dumps(_graphql_page(prs))),
            ):
                result = github.fetch_all_team_prs("Equinor", "my-team")
        assert result[0]["number"] == 2
        assert result[1]["number"] == 1

    def test_ignored_repos_are_not_fetched(self):
        repos = ["Equinor/keep", "Equinor/ISO-Datamodell", "Other/skip-full", "Equinor/skip-full"]
        with (
            patch("pr_watcher.github.fetch_team_repos", return_value=repos),
            patch("pr_watcher.github.fetch_prs_for_repo", return_value=[]) as fetch,
        ):
            github.fetch_all_team_prs(
                "Equinor", "my-team", ignore_repos=["iso-datamodell", "other/skip-full"]
            )
        fetched = sorted(call.args[0] for call in fetch.call_args_list)
        assert fetched == ["Equinor/keep", "Equinor/skip-full"]

    def test_per_repo_errors_are_swallowed(self):
        with patch("pr_watcher.github.fetch_team_repos", return_value=["Equinor/good", "Equinor/bad"]):
            good_prs = [
                {
                    "number": 1,
                    "createdAt": "2024-01-01T00:00:00Z",
                    "labels": {"nodes": []},
                }
            ]

            def side_effect(cmd, **kwargs):
                if "name=bad" in cmd:
                    return _run_result(stderr="unexpected error", returncode=1)
                return _run_result(
                    stdout=json.dumps(_graphql_page(good_prs))
                )

            with patch("pr_watcher.github.subprocess.run", side_effect=side_effect):
                result = github.fetch_all_team_prs("Equinor", "my-team")
        assert len(result) == 1
        assert result[0]["number"] == 1

    def test_failed_repo_reuses_cache_while_valid_responses_replace_it(self):
        failed_repos = []
        cached_prs = [
            {
                "number": 10,
                "repository": "Equinor/good",
                "createdAt": "2024-01-01T00:00:00Z",
            },
            {
                "number": 20,
                "repository": "Equinor/bad",
                "createdAt": "2024-02-01T00:00:00Z",
            },
            {
                "number": 30,
                "repository": "Equinor/no-longer-on-team",
                "createdAt": "2024-03-01T00:00:00Z",
            },
        ]
        current_pr = {
            "number": 11,
            "repository": "Equinor/good",
            "createdAt": "2024-04-01T00:00:00Z",
        }

        def fetch(repo):
            if repo == "Equinor/bad":
                raise RuntimeError("API rate limit exceeded")
            return [current_pr]

        with (
            patch(
                "pr_watcher.github.fetch_team_repos",
                return_value=["Equinor/good", "Equinor/bad"],
            ),
            patch("pr_watcher.github.fetch_prs_for_repo", side_effect=fetch),
        ):
            result = github.fetch_all_team_prs(
                "Equinor",
                "my-team",
                cached_prs,
                failed_repos,
            )

        assert failed_repos == ["Equinor/bad"]
        assert [
            (pr["repository"], pr["number"])
            for pr in result
        ] == [
            ("Equinor/good", 11),
            ("Equinor/bad", 20),
        ]

    def test_rate_limit_stops_scheduling_and_is_not_swallowed(self) -> None:
        error = github.RateLimitError("API rate limit exceeded")
        with (
            patch(
                "pr_watcher.github.fetch_team_repos",
                return_value=[f"Equinor/repo-{i}" for i in range(10)],
            ),
            patch("pr_watcher.github._MAX_CONCURRENT_FETCHES", 1),
            patch("pr_watcher.github.fetch_prs_for_repo", side_effect=error) as fetch,
        ):
            with pytest.raises(github.RateLimitError) as exc:
                github.fetch_all_team_prs("Equinor", "my-team")
        assert exc.value is error
        fetch.assert_called_once_with("Equinor/repo-0")

    def test_team_discovery_rate_limit_skips_pr_requests(self) -> None:
        with (
            patch(
                "pr_watcher.github.subprocess.run",
                return_value=_run_result(
                    stdout=json.dumps({"message": "API rate limit exceeded"}),
                    returncode=1,
                    status=403,
                    headers={
                        "X-Ratelimit-Remaining": "0",
                        "X-Ratelimit-Reset": "1893456000",
                    },
                ),
            ),
            patch("pr_watcher.github.fetch_prs_for_repo") as fetch,
        ):
            with pytest.raises(github.RateLimitError) as exc:
                github.fetch_all_team_prs("Equinor", "my-team")
        assert exc.value.retry_at == datetime(2030, 1, 1, tzinfo=timezone.utc)
        fetch.assert_not_called()

    @pytest.mark.parametrize("first_delay,later_delay,expected", [
        (60, 120, "later"),
        (120, 60, "first"),
        (None, 120, "later"),
        (60, None, "first"),
        (None, None, "first"),
        (60, 60, "first"),
    ])
    def test_collects_latest_retry_deadline_from_in_flight_requests(
        self, first_delay: int | None, later_delay: int | None, expected: str,
    ) -> None:
        start = datetime(2030, 1, 1, tzinfo=timezone.utc)
        errors = {
            name: github.RateLimitError(
                f"{name} limit",
                start + timedelta(seconds=delay) if delay is not None else None,
            )
            for name, delay in (("first", first_delay), ("later", later_delay))
        }
        started = Barrier(2)
        release_later = Event()
        first_observed = Event()
        real_wait = github.wait

        def fetch(repo: str) -> list[dict]:
            started.wait(timeout=5)
            name = repo.split("/")[1]
            if name == "later" and not release_later.wait(timeout=5):
                raise AssertionError("Later request was not drained")
            raise errors[name]

        def wait_for_completed(
            futures: dict, *, return_when: str,
        ) -> tuple[set, set]:
            if first_observed.is_set():
                release_later.set()
            result = real_wait(futures, return_when=return_when)
            first_observed.set()
            return result

        with (
            patch(
                "pr_watcher.github.fetch_team_repos",
                return_value=["acme/first", "acme/later", "acme/not-scheduled"],
            ),
            patch("pr_watcher.github._MAX_CONCURRENT_FETCHES", 2),
            patch("pr_watcher.github.wait", side_effect=wait_for_completed),
            patch("pr_watcher.github.fetch_prs_for_repo", side_effect=fetch) as request,
        ):
            with pytest.raises(github.RateLimitError) as exc:
                github.fetch_all_team_prs("acme", "platform")

        assert exc.value is errors[expected]
        assert sorted(call.args[0] for call in request.call_args_list) == [
            "acme/first", "acme/later",
        ]


class TestRateLimits:
    @pytest.mark.parametrize("returncode", [0, 1])
    def test_graphql_rate_limit_with_http_200(self, returncode: int) -> None:
        response = _run_result(
            stdout=json.dumps({
                "data": None,
                "errors": [{"type": "RATE_LIMITED", "message": "Quota exhausted"}],
            }),
            returncode=returncode,
            headers={
                "X-Ratelimit-Remaining": "0",
                "X-Ratelimit-Reset": "1893456000",
            },
        )
        with patch("pr_watcher.github.subprocess.run", return_value=response):
            with pytest.raises(github.RateLimitError) as exc:
                github.fetch_prs_for_repo("Equinor/my-repo")
        assert exc.value.retry_at == datetime(2030, 1, 1, tzinfo=timezone.utc)

    @pytest.mark.parametrize("status", [200, 403, 429])
    def test_secondary_limit_uses_retry_after_not_primary_reset(
        self, status: int,
    ) -> None:
        response = _run_result(
            stdout=json.dumps({
                "errors": [{"message": "You have exceeded a secondary rate limit"}],
            }),
            status=status,
            returncode=1,
            headers={
                "Retry-After": "120",
                "X-Ratelimit-Remaining": "4999",
                "X-Ratelimit-Reset": "1893456000",
            },
        )
        before = datetime.now(timezone.utc)
        with patch("pr_watcher.github.subprocess.run", return_value=response):
            with pytest.raises(github.RateLimitError) as exc:
                github.fetch_prs_for_repo("Equinor/my-repo")
        after = datetime.now(timezone.utc)
        assert before + timedelta(seconds=120) <= exc.value.retry_at
        assert exc.value.retry_at <= after + timedelta(seconds=120)

    def test_honors_later_of_primary_reset_and_retry_after(self) -> None:
        headers = {
            "retry-after": "120",
            "x-ratelimit-remaining": "0",
            "x-ratelimit-reset": "1893456000",
        }
        assert github._rate_limit_retry_at(headers) == datetime(
            2030, 1, 1, tzinfo=timezone.utc,
        )
        headers["x-ratelimit-reset"] = "1"
        before = datetime.now(timezone.utc)
        assert github._rate_limit_retry_at(headers) >= before + timedelta(seconds=120)

    @pytest.mark.parametrize("headers", [
        {},
        {"x-ratelimit-remaining": "0"},
        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "invalid"},
        {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "99999999999999999999"},
        {"retry-after": "invalid"},
        {"retry-after": "99999999999999999999"},
    ])
    def test_missing_or_invalid_timing_uses_app_backoff(
        self, headers: dict[str, str],
    ) -> None:
        with patch(
            "pr_watcher.github.subprocess.run",
            return_value=_run_result(
                stderr="API rate limit exceeded", returncode=1, headers=headers,
            ),
        ):
            with pytest.raises(github.RateLimitError) as exc:
                github.fetch_prs_for_repo("Equinor/my-repo")
        assert exc.value.retry_at is None

    def test_http_429_without_rate_limit_message(self) -> None:
        with patch(
            "pr_watcher.github.subprocess.run",
            return_value=_run_result(
                stdout='{"message": "Too many requests"}',
                returncode=1,
                status=429,
                headers={"Retry-After": "60"},
            ),
        ):
            with pytest.raises(github.RateLimitError):
                github.fetch_prs_for_repo("Equinor/my-repo")

    def test_success_using_last_point_is_not_discarded(self) -> None:
        with patch(
            "pr_watcher.github.subprocess.run",
            return_value=_run_result(
                stdout=json.dumps(_graphql_page([])),
                headers={
                    "X-Ratelimit-Remaining": "0",
                    "X-Ratelimit-Reset": "1893456000",
                },
            ),
        ):
            assert github.fetch_prs_for_repo("Equinor/my-repo") == []

    def test_later_page_rate_limit_does_not_return_incomplete_list(self) -> None:
        page = _graphql_page([
            {"number": 1, "labels": {"nodes": []}},
        ], has_next_page=True, end_cursor="next")
        with patch(
            "pr_watcher.github.subprocess.run",
            side_effect=[
                _run_result(stdout=json.dumps(page)),
                _run_result(stderr="API rate limit exceeded", returncode=1),
            ],
        ) as run:
            with pytest.raises(github.RateLimitError):
                github.fetch_prs_for_repo("Equinor/my-repo")
        assert run.call_count == 2
