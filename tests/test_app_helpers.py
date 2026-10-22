"""Tests for pure helper functions in pr_watcher/app.py."""
from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from rich.cells import cell_len

from pr_watcher.app import (
    ellipsis_middle,
    format_age,
    format_labels,
    format_pr_status,
    format_review_status,
    format_reviewers,
    order_prs,
    pull_updates,
    pr_priority,
    repo_short_name,
)


def _pr(
    number: int,
    decision: str | None = "REVIEW_REQUIRED",
    *,
    draft: bool = False,
    created: str = "2026-01-01T00:00:00Z",
    updated: str | None = None,
    requested: list[dict] | None = None,
    reviews: list[dict] | None = None,
) -> dict:
    return {
        "number": number,
        "reviewDecision": decision,
        "isDraft": draft,
        "createdAt": created,
        "updatedAt": updated or created,
        "reviewRequests": {"nodes": [{"requestedReviewer": r} for r in requested or []]},
        "latestReviews": {"nodes": reviews or []},
    }


def _git_result(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


class TestPullUpdates:
    def test_returns_true_when_head_changes(self, tmp_path):
        responses = [
            _git_result(stdout="old-head\n"),
            _git_result(),
            _git_result(stdout="Updating old-head..new-head\n"),
            _git_result(stdout="new-head\n"),
        ]
        with patch("pr_watcher.app.subprocess.run", side_effect=responses) as run:
            assert pull_updates(tmp_path)

        assert [call.args[0] for call in run.call_args_list] == [
            ["git", "rev-parse", "HEAD"],
            ["git", "status", "--porcelain"],
            ["git", "pull", "--ff-only"],
            ["git", "rev-parse", "HEAD"],
        ]
        assert all(call.kwargs["cwd"] == tmp_path for call in run.call_args_list)
        assert run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"

    def test_returns_false_when_already_current(self, tmp_path):
        responses = [
            _git_result(stdout="same-head\n"),
            _git_result(),
            _git_result(stdout="Already up to date.\n"),
            _git_result(stdout="same-head\n"),
        ]
        with patch("pr_watcher.app.subprocess.run", side_effect=responses):
            assert not pull_updates(tmp_path)

    def test_refuses_dirty_worktree(self, tmp_path):
        responses = [
            _git_result(stdout="head\n"),
            _git_result(stdout=" M pr_watcher/app.py\n"),
        ]
        with patch("pr_watcher.app.subprocess.run", side_effect=responses) as run:
            with pytest.raises(RuntimeError, match="uncommitted changes"):
                pull_updates(tmp_path)
        assert run.call_count == 2

    def test_surfaces_pull_error(self, tmp_path):
        responses = [
            _git_result(stdout="head\n"),
            _git_result(),
            _git_result(stderr="no tracking information", returncode=1),
        ]
        with patch("pr_watcher.app.subprocess.run", side_effect=responses):
            with pytest.raises(
                RuntimeError,
                match="git pull --ff-only failed: no tracking information",
            ):
                pull_updates(tmp_path)

    def test_reports_missing_git(self, tmp_path):
        with patch("pr_watcher.app.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError, match="Git is not installed"):
                pull_updates(tmp_path)


class TestPrPriority:
    @pytest.mark.parametrize("decision", ["REVIEW_REQUIRED", None])
    def test_needs_review_without_reviewers_is_first(self, decision):
        assert pr_priority(_pr(1, decision)) == 0

    @pytest.mark.parametrize("decision", ["REVIEW_REQUIRED", None])
    def test_needs_review_with_requested_user(self, decision):
        assert pr_priority(_pr(1, decision, requested=[{"login": "alice"}])) == 1

    def test_team_request_counts_as_reviewer(self):
        assert pr_priority(_pr(1, requested=[{"slug": "wo-prep"}])) == 1

    def test_submitted_review_counts_as_reviewer(self):
        reviews = [{"author": {"login": "bob"}, "state": "COMMENTED"}]
        assert pr_priority(_pr(1, reviews=reviews)) == 1

    def test_copilot_only_counts_as_no_reviewers(self):
        pr = _pr(
            1,
            requested=[{"login": "Copilot"}],
            reviews=[{"author": {"login": "copilot-pull-request-reviewer"}, "state": "COMMENTED"}],
        )
        assert pr_priority(pr) == 0

    def test_pending_review_is_not_a_reviewer(self):
        reviews = [{"author": {"login": "bob"}, "state": "PENDING"}]
        assert pr_priority(_pr(1, reviews=reviews)) == 0

    def test_changes_requested(self):
        assert pr_priority(_pr(1, "CHANGES_REQUESTED")) == 2

    def test_approved(self):
        assert pr_priority(_pr(1, "APPROVED")) == 3

    @pytest.mark.parametrize("decision", ["APPROVED", "REVIEW_REQUIRED", "CHANGES_REQUESTED", None])
    def test_draft_is_last_regardless_of_decision(self, decision):
        assert pr_priority(_pr(1, decision, draft=True)) == 4


class TestOrderPrs:
    PRS = [
        _pr(1, "APPROVED", created="2026-01-05T00:00:00Z"),
        _pr(2, "REVIEW_REQUIRED", created="2026-01-04T00:00:00Z", requested=[{"login": "a"}]),
        _pr(3, None, draft=True, created="2026-01-03T00:00:00Z"),
        _pr(4, "CHANGES_REQUESTED", created="2026-01-02T00:00:00Z"),
        _pr(5, "REVIEW_REQUIRED", created="2026-01-01T12:00:00Z"),
        _pr(6, None, created="2026-01-01T00:00:00Z"),
    ]

    def test_created_keeps_fetch_order(self):
        assert [pr["number"] for pr in order_prs(self.PRS, "created")] == [1, 2, 3, 4, 5, 6]

    def test_priority_orders_by_tier_then_oldest_first(self):
        assert [pr["number"] for pr in order_prs(self.PRS, "priority")] == [6, 5, 2, 4, 1, 3]

    def test_updated_orders_most_recent_change_first(self):
        prs = [
            _pr(1, updated="2026-01-05T00:00:00Z"),
            _pr(2, updated="2026-01-07T00:00:00Z"),
            _pr(3, updated="2026-01-06T00:00:00Z"),
        ]
        assert [pr["number"] for pr in order_prs(prs, "updated")] == [2, 3, 1]

    def test_does_not_mutate_input(self):
        prs = list(self.PRS)
        order_prs(prs, "priority")
        assert prs == self.PRS


# ---------------------------------------------------------------------------
# format_age
# ---------------------------------------------------------------------------

def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat()


class TestFormatAge:
    def test_minutes(self):
        assert format_age(_iso(timedelta(minutes=30))) == "30m"

    def test_hours(self):
        assert format_age(_iso(timedelta(hours=5))) == "5h"

    def test_days(self):
        assert format_age(_iso(timedelta(days=3))) == "3d"

    def test_weeks(self):
        assert format_age(_iso(timedelta(days=14))) == "2w"

    def test_months(self):
        assert format_age(_iso(timedelta(days=60))) == "2mo"

    def test_invalid_returns_question_mark(self):
        assert format_age("not-a-date") == "?"

    def test_z_suffix_handled(self):
        ts = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert format_age(ts) == "2h"


# ---------------------------------------------------------------------------
# format_review_status
# ---------------------------------------------------------------------------

class TestFormatReviewStatus:
    def test_draft(self):
        assert format_review_status({"isDraft": True}) == "📝 Draft"

    def test_approved(self):
        assert format_review_status({"isDraft": False, "reviewDecision": "APPROVED"}) == "✅ Approved"

    def test_changes_requested(self):
        assert format_review_status({"isDraft": False, "reviewDecision": "CHANGES_REQUESTED"}) == "❌ Changes Req."

    def test_review_required(self):
        assert format_review_status({"isDraft": False, "reviewDecision": "REVIEW_REQUIRED"}) == "⏳ Review Needed"

    def test_no_decision(self):
        assert format_review_status({"isDraft": False, "reviewDecision": None}) == "➖ No Reviews"

    def test_missing_keys(self):
        assert format_review_status({}) == "➖ No Reviews"

    def test_draft_takes_priority_over_decision(self):
        assert format_review_status({"isDraft": True, "reviewDecision": "APPROVED"}) == "📝 Draft"

    @pytest.mark.parametrize("pr", [
        {"reviewDecision": "APPROVED"},
        {"reviewDecision": "CHANGES_REQUESTED"},
        {"reviewDecision": "REVIEW_REQUIRED"},
        {},
        {"isDraft": True},
    ])
    def test_icons_use_two_terminal_cells(self, pr):
        icon, _ = format_review_status(pr).split(" ", 1)
        assert cell_len(icon) == 2


class TestFormatPrStatus:
    @pytest.mark.parametrize(("state", "color"), [
        ("SUCCESS", "green"),
        ("FAILURE", "red"),
        ("ERROR", "red"),
        ("PENDING", None),
        ("EXPECTED", None),
        ("UNKNOWN", None),
        (None, None),
    ])
    def test_checks_indicator_preserves_review_status(self, state, color):
        pr = {
            "reviewDecision": "APPROVED",
            "commits": {"nodes": [
                {"commit": {"statusCheckRollup": {"state": state}}},
            ]},
        }
        status = format_pr_status(pr)
        assert status.plain == ("●" if color else " ") + " ✅ Approved"
        assert [(span.start, span.end, span.style) for span in status.spans] == (
            [(0, 1, color)] if color else []
        )

    @pytest.mark.parametrize("pr", [
        {},
        {"commits": {"nodes": []}},
        {"commits": {"nodes": [{"commit": {"statusCheckRollup": None}}]}},
    ])
    def test_missing_checks_leave_space(self, pr):
        status = format_pr_status(pr)
        assert status.plain == "  ➖ No Reviews"
        assert not status.spans

    def test_draft_still_shows_check_result(self):
        pr = {
            "isDraft": True,
            "commits": {"nodes": [
                {"commit": {"statusCheckRollup": {"state": "FAILURE"}}},
            ]},
        }
        assert format_pr_status(pr).plain == "● 📝 Draft"


# ---------------------------------------------------------------------------
# format_labels
# ---------------------------------------------------------------------------

class TestFormatLabels:
    def test_empty(self):
        assert format_labels([]) == ""

    def test_single(self):
        assert format_labels([{"name": "bug"}]) == "bug"

    def test_three(self):
        result = format_labels([{"name": "bug"}, {"name": "feat"}, {"name": "docs"}])
        assert result == "bug, feat, docs"

    def test_overflow_shows_count(self):
        labels = [{"name": f"label-{i}"} for i in range(5)]
        result = format_labels(labels)
        assert result.endswith("+2")
        assert "label-0" in result

    def test_exactly_three_no_overflow(self):
        labels = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
        assert "+" not in format_labels(labels)


class TestFormatReviewers:
    def test_no_reviewers(self):
        assert format_reviewers({}) == "—"
        assert format_reviewers({
            "reviewRequests": {"nodes": []},
            "latestReviews": {"nodes": []},
        }) == "—"

    def test_requested_users_teams_and_submitted_reviewers(self):
        pr = {
            "reviewRequests": {"nodes": [
                {"requestedReviewer": {"login": "bob"}},
                {"requestedReviewer": {"slug": "my-team"}},
                {"requestedReviewer": {"login": "review-bot"}},
            ]},
            "latestReviews": {"nodes": [
                {"author": {"login": "bob"}, "state": "APPROVED"},
                {"author": {"login": "Alice"}, "state": "CHANGES_REQUESTED"},
                {"author": {"login": "bob"}, "state": "COMMENTED"},
            ]},
        }
        assert format_reviewers(pr) == "Alice, bob, review-bot, team:my-team"

    def test_deleted_reviewers_and_pending_reviews_are_ignored(self):
        pr = {
            "reviewRequests": {"nodes": [{"requestedReviewer": None}]},
            "latestReviews": {"nodes": [
                {"author": None, "state": "APPROVED"},
                {"author": {"login": "alice"}, "state": "PENDING"},
            ]},
        }
        assert format_reviewers(pr) == "—"

    @pytest.mark.parametrize("login", [
        "Copilot", "copilot[bot]", "copilot-pull-request-reviewer",
        "copilot-pull-request-reviewer[bot]", "COPILOT",
    ])
    def test_copilot_is_excluded_from_requests_and_reviews(self, login):
        pr = {
            "reviewRequests": {"nodes": [{"requestedReviewer": {"login": login}}]},
            "latestReviews": {"nodes": [
                {"author": {"login": login}, "state": "APPROVED"},
            ]},
        }
        assert format_reviewers(pr) == "—"

    def test_copilot_filter_preserves_other_reviewers_and_teams(self):
        pr = {
            "reviewRequests": {"nodes": [
                {"requestedReviewer": {"login": "Copilot"}},
                {"requestedReviewer": {"slug": "copilot"}},
                {"requestedReviewer": {"login": "copilot-tools-user"}},
            ]},
            "latestReviews": {"nodes": [
                {"author": {"login": "copilot-pull-request-reviewer"}, "state": "COMMENTED"},
                {"author": {"login": "alice"}, "state": "APPROVED"},
                {"author": {"login": "other-bot"}, "state": "COMMENTED"},
            ]},
        }
        assert format_reviewers(pr) == "alice, copilot-tools-user, other-bot, team:copilot"


# ---------------------------------------------------------------------------
# repo_short_name
# ---------------------------------------------------------------------------

class TestRepoShortName:
    def test_strips_org(self):
        assert repo_short_name("Equinor/ecm-api-backend") == "ecm-api-backend"

    def test_no_slash_unchanged(self):
        assert repo_short_name("standalone-repo") == "standalone-repo"

    def test_preserves_iac_suffix(self):
        assert repo_short_name("Equinor/ecm-wo-prep-iac") == "ecm-wo-prep-iac"


# ---------------------------------------------------------------------------
# ellipsis_middle
# ---------------------------------------------------------------------------

class TestEllipsisMiddle:
    def test_short_string_unchanged(self):
        assert ellipsis_middle("short", 10) == "short"

    def test_exact_length_unchanged(self):
        s = "a" * 10
        assert ellipsis_middle(s, 10) == s

    def test_truncated_string_fits_width(self):
        s = "ecm-iso-wp-gl0560-api-iac"
        result = ellipsis_middle(s, 15)
        assert len(result) == 15

    def test_truncated_contains_ellipsis(self):
        result = ellipsis_middle("ecm-iso-wp-gl0560-api-iac", 15)
        assert "…" in result

    def test_tail_preserved(self):
        result = ellipsis_middle("ecm-iso-wp-gl0560-api-iac", 15)
        assert result.endswith("-iac")

    def test_head_preserved(self):
        result = ellipsis_middle("ecm-iso-wp-gl0560-api-iac", 15)
        assert result.startswith("ecm-")
