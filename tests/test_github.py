"""Tests for pr_watcher/github.py — subprocesses are fully mocked."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from pr_watcher import github


def _run_result(stdout="", stderr="", returncode=0):
    m = MagicMock()
    m.stdout = stdout
    m.stderr = stderr
    m.returncode = returncode
    return m


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
        prs = [{"number": 1, "title": "Fix bug"}, {"number": 2, "title": "Add feature"}]
        with patch("pr_watcher.github.subprocess.run", return_value=_run_result(stdout=json.dumps(prs))):
            result = github.fetch_prs_for_repo("Equinor/my-repo")
        assert all(pr["repository"] == "Equinor/my-repo" for pr in result)
        assert len(result) == 2

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


# ---------------------------------------------------------------------------
# fetch_all_team_prs
# ---------------------------------------------------------------------------

class TestFetchAllTeamPrs:
    def test_sorted_newest_first(self):
        repos = [{"full_name": "Equinor/repo-a"}]
        prs = [
            {"number": 1, "createdAt": "2024-01-01T00:00:00Z"},
            {"number": 2, "createdAt": "2024-06-01T00:00:00Z"},
        ]
        with patch("pr_watcher.github.fetch_team_repos", return_value=["Equinor/repo-a"]):
            with patch("pr_watcher.github.subprocess.run", return_value=_run_result(stdout=json.dumps(prs))):
                result = github.fetch_all_team_prs("Equinor", "my-team")
        assert result[0]["number"] == 2
        assert result[1]["number"] == 1

    def test_per_repo_errors_are_swallowed(self):
        with patch("pr_watcher.github.fetch_team_repos", return_value=["Equinor/good", "Equinor/bad"]):
            good_prs = [{"number": 1, "createdAt": "2024-01-01T00:00:00Z"}]
            def side_effect(cmd, **kwargs):
                if "Equinor/bad" in cmd:
                    return _run_result(stderr="unexpected error", returncode=1)
                return _run_result(stdout=json.dumps(good_prs))
            with patch("pr_watcher.github.subprocess.run", side_effect=side_effect):
                result = github.fetch_all_team_prs("Equinor", "my-team")
        assert len(result) == 1
        assert result[0]["number"] == 1
