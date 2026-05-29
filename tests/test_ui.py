"""Textual UI regression tests for PRWatcherApp.

These tests use App.run_test() to run the app headlessly and verify that the
DataTable never produces a horizontal scrollbar after the column-width algorithm
runs.

Root cause guarded against:
    DataTable.Column.get_render_width() adds ``2 * cell_padding`` (default = 1)
    to every column's stored width.  With 7 columns that is 14 characters of
    rendering overhead.  The original code used ``SEPARATORS = 6``, which was 8
    characters too small, causing virtual_size.width > size.width and a
    persistent horizontal scrollbar.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pr_watcher.app import PRWatcherApp
from pr_watcher.config import Config


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat()


MOCK_PRS = [
    {
        "number": 42,
        "title": "feat: add widget for displaying real-time metrics dashboard",
        "repository": "Equinor/ecm-api-backend",
        "author": {"login": "alice"},
        "reviewDecision": "APPROVED",
        "isDraft": False,
        "createdAt": _iso(timedelta(days=2)),
        "labels": [{"name": "feature"}, {"name": "backend"}],
        "url": "https://github.com/Equinor/ecm-api-backend/pull/42",
    },
    {
        "number": 41,
        "title": "fix: resolve null pointer exception in authentication middleware",
        "repository": "Equinor/ecm-iso-wp-gl0560-api-iac",
        "author": {"login": "bob-long-username"},
        "reviewDecision": "REVIEW_REQUIRED",
        "isDraft": False,
        "createdAt": _iso(timedelta(days=5)),
        "labels": [],
        "url": "https://github.com/Equinor/ecm-iso-wp-gl0560-api-iac/pull/41",
    },
    {
        "number": 100,
        "title": "chore: update dependencies and bump version numbers across all packages",
        "repository": "Equinor/ecm-wo-preparation-service",
        "author": {"login": "charlie"},
        "reviewDecision": "CHANGES_REQUESTED",
        "isDraft": False,
        "createdAt": _iso(timedelta(hours=3)),
        "labels": [{"name": "chore"}, {"name": "deps"}, {"name": "semver"}, {"name": "auto"}],
        "url": "https://github.com/Equinor/ecm-wo-preparation-service/pull/100",
    },
]


async def _load_mock_data(app: PRWatcherApp, pilot) -> None:
    """Inject mock PRs directly so we never call the real gh CLI."""
    app._prs = MOCK_PRS
    app._rebuild_table()
    app._set_view("table")
    await pilot.pause()


async def test_no_horizontal_scroll():
    """After loading mock PRs, virtual width must not exceed table width."""
    config = Config(org="Equinor")
    app = PRWatcherApp(config)
    async with app.run_test(size=(220, 40)) as pilot:
        await _load_mock_data(app, pilot)
        table = app.query_one("#pr-table")
        assert table.virtual_size.width <= table.size.width, (
            f"Horizontal overflow: virtual={table.virtual_size.width}, "
            f"visible={table.size.width}, "
            f"delta={table.virtual_size.width - table.size.width}"
        )


async def test_no_horizontal_scroll_narrow_terminal():
    """Column widths adapt correctly on a narrower (120-column) terminal."""
    config = Config(org="Equinor")
    app = PRWatcherApp(config)
    async with app.run_test(size=(120, 30)) as pilot:
        await _load_mock_data(app, pilot)
        table = app.query_one("#pr-table")
        assert table.virtual_size.width <= table.size.width, (
            f"Horizontal overflow at 120 cols: virtual={table.virtual_size.width}, "
            f"visible={table.size.width}"
        )


async def test_no_horizontal_scroll_after_resize():
    """Column widths adapt correctly after terminal resize.

    We simulate resize by starting at 220, resizing to 160, and explicitly
    re-triggering column-width recomputation (mirrors what on_resize does).
    """
    config = Config(org="Equinor")
    app = PRWatcherApp(config)
    async with app.run_test(size=(220, 40)) as pilot:
        await _load_mock_data(app, pilot)

        # Resize the headless terminal to a narrower width.
        await pilot.resize_terminal(160, 40)
        # Ensure on_resize has fired and _update_dimensions has run.
        await pilot.pause()
        await pilot.pause()

        table = app.query_one("#pr-table")
        assert table.virtual_size.width <= table.size.width, (
            f"Horizontal overflow after resize to 160: virtual={table.virtual_size.width}, "
            f"visible={table.size.width}"
        )
