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
from unittest.mock import patch

import pytest

from textual.coordinate import Coordinate
from textual.events import Click, MouseScrollDown, MouseScrollUp

from pr_watcher.app import PRTable, PRWatcherApp
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


async def _run_with_mock_prs(app: PRWatcherApp, pilot) -> None:
    """Wait for the worker (which returns MOCK_PRS) to populate the table."""
    # Give the background thread time to complete and the UI to update.
    await pilot.pause(0.5)


async def test_no_horizontal_scroll():
    """After loading mock PRs, virtual width must not exceed table width."""
    config = Config(org="Equinor")
    app = PRWatcherApp(config)
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
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
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS):
        async with app.run_test(size=(120, 30)) as pilot:
            await _run_with_mock_prs(app, pilot)
            table = app.query_one("#pr-table")
            assert table.virtual_size.width <= table.size.width, (
                f"Horizontal overflow at 120 cols: virtual={table.virtual_size.width}, "
                f"visible={table.size.width}"
            )


async def test_no_horizontal_scroll_after_resize():
    """Column widths adapt correctly after terminal resize."""
    config = Config(org="Equinor")
    app = PRWatcherApp(config)
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)

            await pilot.resize_terminal(160, 40)
            await pilot.pause(0.2)

            table = app.query_one("#pr-table")
            assert table.virtual_size.width <= table.size.width, (
                f"Horizontal overflow after resize to 160: virtual={table.virtual_size.width}, "
                f"visible={table.size.width}"
            )


async def test_mouse_scroll_moves_row_cursor():
    """MouseScrollDown/Up move the row cursor instead of scrolling the viewport."""
    config = Config(org="Equinor")
    app = PRWatcherApp(config)
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            table = app.query_one("#pr-table", PRTable)
            assert table.cursor_row == 0

            scroll_kwargs = dict(x=0, y=0, delta_x=0, delta_y=1, button=0, shift=False, meta=False, ctrl=False)
            table.post_message(MouseScrollDown(table, **scroll_kwargs))
            await pilot.pause(0.1)
            assert table.cursor_row == 1, "scroll down should advance cursor to row 1"

            table.post_message(MouseScrollUp(table, **scroll_kwargs))
            await pilot.pause(0.1)
            assert table.cursor_row == 0, "scroll up should return cursor to row 0"


async def test_middle_click_opens_url():
    """Middle-click message on a row opens the PR URL."""
    config = Config(org="Equinor")
    app = PRWatcherApp(config)
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            table = app.query_one("#pr-table", PRTable)
            row_key = table.ordered_rows[0].key

            with patch("pr_watcher.app.open_url") as mock_open:
                # Post via table so the message bubbles up to the app handler
                table.post_message(PRTable.MiddleClick(row_key))
                await pilot.pause(0.1)

            mock_open.assert_called_once_with(MOCK_PRS[0]["url"])


async def test_middle_click_dispatched_on_button2():
    """Clicking with button=2 on PRTable dispatches PRTable.MiddleClick."""
    config = Config(org="Equinor")
    app = PRWatcherApp(config)
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            table = app.query_one("#pr-table", PRTable)

            with patch("pr_watcher.app.open_url") as mock_open:
                click_kwargs = dict(x=0, y=0, delta_x=0, delta_y=0, button=2, shift=False, meta=False, ctrl=False)
                table.post_message(Click(table, **click_kwargs))
                await pilot.pause(0.1)

            # hover_coordinate defaults to (0,0); a middle-click should open the first PR
            mock_open.assert_called_once_with(MOCK_PRS[0]["url"])

