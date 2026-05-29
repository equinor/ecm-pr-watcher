"""Tests for pure helper functions in pr_watcher/app.py."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from pr_watcher.app import (
    ellipsis_middle,
    format_age,
    format_labels,
    format_review_status,
    repo_short_name,
)


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
        assert format_review_status({"isDraft": True}) == "◌ Draft"

    def test_approved(self):
        assert format_review_status({"isDraft": False, "reviewDecision": "APPROVED"}) == "✓ Approved"

    def test_changes_requested(self):
        assert format_review_status({"isDraft": False, "reviewDecision": "CHANGES_REQUESTED"}) == "✗ Changes Req."

    def test_review_required(self):
        assert format_review_status({"isDraft": False, "reviewDecision": "REVIEW_REQUIRED"}) == "⏳ Review Needed"

    def test_no_decision(self):
        assert format_review_status({"isDraft": False, "reviewDecision": None}) == "— No Reviews"

    def test_missing_keys(self):
        assert format_review_status({}) == "— No Reviews"

    def test_draft_takes_priority_over_decision(self):
        assert format_review_status({"isDraft": True, "reviewDecision": "APPROVED"}) == "◌ Draft"


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
