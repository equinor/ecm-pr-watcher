"""Textual UI regression tests for PRWatcherApp.

These tests use App.run_test() to run the app headlessly and verify that the
DataTable never produces a horizontal scrollbar after the column-width algorithm
runs.

Root cause guarded against:
    DataTable.Column.get_render_width() adds ``2 * cell_padding`` (default = 1)
    to every column's stored width.  The original 8 columns needed 16 characters
    of rendering overhead.  The original code used ``SEPARATORS = 6``, which was 10
    characters too small, causing virtual_size.width > size.width and a
    persistent horizontal scrollbar.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from rich.text import Text
from textual.coordinate import Coordinate
from textual.events import Click, MouseScrollDown, MouseScrollUp
from textual.widgets import Static
from textual.widgets._tooltip import Tooltip

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
        "reviewRequests": {"nodes": [
            {"requestedReviewer": {"login": "bob"}},
            {"requestedReviewer": {"slug": "ecm-wo-preparation"}},
        ]},
        "latestReviews": {"nodes": [
            {"author": {"login": "bob"}, "state": "APPROVED"},
            {"author": {"login": "charlie"}, "state": "COMMENTED"},
        ]},
        "reviewDecision": "APPROVED",
        "isDraft": False,
        "createdAt": _iso(timedelta(days=2)),
        "totalCommentsCount": 1,
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
        "totalCommentsCount": 0,
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
        "totalCommentsCount": 2,
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


async def test_reviewers_column_next_to_author():
    app = PRWatcherApp(Config(org="Equinor"))
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            table = app.query_one("#pr-table", PRTable)
            assert [str(column.label) for column in table.ordered_columns][3:6] == [
                "Author", "Reviewers", "Review Status",
            ]
            assert table.get_cell(
                "Equinor/ecm-api-backend#42", app._col_keys["reviewers"],
            ) == "bob, charlie, team:ecm-wo-preparation"
            assert table.get_cell(
                "Equinor/ecm-iso-wp-gl0560-api-iac#41", app._col_keys["reviewers"],
            ) == "—"
            assert table.columns[app._col_keys["reviewers"]].width == 30

            await pilot.resize_terminal(120, 30)
            await pilot.pause(0.2)
            assert table.columns[app._col_keys["reviewers"]].width < 30
            assert table.virtual_size.width <= table.size.width


@pytest.mark.parametrize("column", ["repo", "title", "author", "reviewers", "labels"])
async def test_hover_shows_full_clipped_cell_text(column):
    prs = deepcopy(MOCK_PRS)
    prs[0]["repository"] = "Equinor/ecm-service-with-a-very-long-repository-name-iac"
    prs[0]["title"] = "feat: show [literal] text " + "long title " * 15
    prs[0]["author"] = {"login": "author-with-a-very-long-github-login"}
    prs[0]["reviewRequests"]["nodes"].append(
        {"requestedReviewer": {"login": "Copilot"}}
    )
    if column == "labels":
        for pr in prs:
            pr["title"] = "Short title"
        prs[0]["labels"] = [
            {"name": "feature-with-a-very-long-label-that-overflows-the-column"},
            {"name": "backend"},
        ]
    expected = {
        "repo": prs[0]["repository"].split("/", 1)[1],
        "title": prs[0]["title"],
        "author": prs[0]["author"]["login"],
        "reviewers": "bob, charlie, team:ecm-wo-preparation",
        "labels": ", ".join(label["name"] for label in prs[0]["labels"]),
    }
    app = PRWatcherApp(Config(org="Equinor"))
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=prs):
        async with app.run_test(size=(160, 40), tooltips=True) as pilot:
            await _run_with_mock_prs(app, pilot)
            table = app.query_one("#pr-table", PRTable)
            keys = list(app._col_keys)
            x = sum(
                table.columns[app._col_keys[key]].width + 2 * table.cell_padding
                for key in keys[:keys.index(column)]
            ) + table.cell_padding
            await pilot.hover(table, offset=(x, 1))
            await pilot.pause(0.6)
            assert isinstance(table.tooltip, Text)
            assert table.tooltip.plain == expected[column]
            tooltip = app.screen.query_one(Tooltip)
            assert tooltip.display
            assert tooltip.content.plain == expected[column]

            await pilot.hover(table, offset=(1, 1))
            await pilot.pause(0.1)
            assert table.tooltip is None
            assert not tooltip.display

            await pilot.hover(table, offset=(x, 1))
            await pilot.pause(0.1)
            await pilot.hover(table, offset=(x, 0))
            await pilot.pause(0.1)
            assert table.tooltip is None

            await pilot.hover(table, offset=(x, 1))
            await pilot.pause(0.1)
            await pilot.hover("#app-header")
            await pilot.pause(0.1)
            assert table.tooltip is None


async def test_hover_expands_summarized_labels_even_when_summary_fits():
    prs = deepcopy(MOCK_PRS[:1])
    prs[0]["labels"] = [{"name": name} for name in ("a", "b", "c", "d")]
    app = PRWatcherApp(Config(org="Equinor"))
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=prs):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            table = app.query_one("#pr-table", PRTable)
            x = sum(
                column.width + 2 * table.cell_padding
                for column in table.ordered_columns[:-1]
            ) + table.cell_padding
            await pilot.hover(table, offset=(x, 1))
            await pilot.pause(0.1)
            assert table.tooltip.plain == "a, b, c, d"


@pytest.mark.parametrize("change", ["title", "reorder", "empty"])
async def test_refresh_clears_visible_tooltip_with_stationary_pointer(change):
    initial_prs = deepcopy(MOCK_PRS)
    initial_prs[0]["title"] = "Original title " * 20
    updated_prs = deepcopy(initial_prs)
    if change == "title":
        updated_prs[0]["title"] = "Updated title " * 20
    elif change == "reorder":
        updated_prs.reverse()
    else:
        updated_prs = []

    app = PRWatcherApp(Config(org="Equinor"))
    with patch(
        "pr_watcher.github.fetch_all_team_prs",
        side_effect=[initial_prs, updated_prs],
    ) as fetch:
        async with app.run_test(size=(160, 40), tooltips=True) as pilot:
            await _run_with_mock_prs(app, pilot)
            table = app.query_one("#pr-table", PRTable)
            x = sum(
                table.columns[app._col_keys[key]].width + 2 * table.cell_padding
                for key in ("number", "repo")
            ) + table.cell_padding
            await pilot.hover(table, offset=(x, 1))
            await pilot.pause(0.6)
            tooltip = app.screen.query_one(Tooltip)
            assert tooltip.display
            assert table.tooltip.plain == initial_prs[0]["title"]

            app._next_refresh = datetime.now() - timedelta(seconds=1)
            app._tick()
            await pilot.pause(0.5)

            assert fetch.call_count == 2
            assert table.row_count == len(updated_prs)
            if updated_prs:
                assert table.get_cell_at(Coordinate(0, 2)) == updated_prs[0]["title"]
            assert table.tooltip is None
            assert not tooltip.display

            if change == "title":
                await pilot.hover(table, offset=(x + 1, 1))
                await pilot.pause(0.6)
                assert tooltip.display
                assert table.tooltip.plain == updated_prs[0]["title"]


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


async def test_rapid_duplicate_opens_are_debounced():
    """Rapid open events for the same PR open only one browser tab."""
    config = Config(org="Equinor")
    app = PRWatcherApp(config)
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            table = app.query_one("#pr-table", PRTable)
            row_key = table.ordered_rows[0].key

            with patch("pr_watcher.app.open_url") as mock_open:
                table.post_message(PRTable.MiddleClick(row_key))
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


async def test_comment_increase_marked_until_row_selected():
    """A comment increase gets a persistent marker that Enter acknowledges."""
    initial_prs = deepcopy(MOCK_PRS)
    updated_prs = deepcopy(MOCK_PRS)
    updated_prs[0]["totalCommentsCount"] += 1

    config = Config(org="Equinor")
    app = PRWatcherApp(config)
    with patch(
        "pr_watcher.github.fetch_all_team_prs",
        side_effect=[initial_prs, updated_prs, deepcopy(updated_prs)],
    ):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            table = app.query_one("#pr-table", PRTable)
            row_key = "Equinor/ecm-api-backend#42"
            assert table.get_cell(row_key, app._col_keys["comments"]) == "1"

            app.action_refresh()
            await pilot.pause(0.5)
            assert table.get_cell(row_key, app._col_keys["comments"]) == "2!"

            app.action_refresh()
            await pilot.pause(0.5)
            assert table.get_cell(row_key, app._col_keys["comments"]) == "2!"

            with patch("pr_watcher.app.open_url"):
                await pilot.press("enter")
                await pilot.pause(0.1)

            assert table.get_cell(row_key, app._col_keys["comments"]) == "2"


async def test_duplicate_pr_numbers_use_repository_identity():
    """PRs with the same number remain distinct across repositories."""
    initial_pr = deepcopy(MOCK_PRS[0])
    duplicate_pr = deepcopy(MOCK_PRS[0])
    duplicate_pr["repository"] = "Equinor/ecm-other-service"
    duplicate_pr["url"] = "https://github.com/Equinor/ecm-other-service/pull/42"

    config = Config(org="Equinor")
    app = PRWatcherApp(config)
    with patch(
        "pr_watcher.github.fetch_all_team_prs",
        side_effect=[[initial_pr], [initial_pr, duplicate_pr]],
    ), patch.object(app, "bell") as mock_bell:
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)

            app.action_refresh()
            await pilot.pause(0.5)

            table = app.query_one("#pr-table", PRTable)
            assert table.row_count == 2
            assert table.get_row("Equinor/ecm-api-backend#42")
            assert table.get_row("Equinor/ecm-other-service#42")
            mock_bell.assert_called_once()

            with patch("pr_watcher.app.open_url") as mock_open:
                table.post_message(
                    PRTable.MiddleClick(table.ordered_rows[1].key)
                )
                await pilot.pause(0.1)

            mock_open.assert_called_once_with(duplicate_pr["url"])


async def test_connectivity_error_hides_error_panel():
    """Connectivity errors keep the error panel hidden and show a status bar warning."""
    from textual.worker import WorkerFailed

    config = Config(org="Equinor")
    app = PRWatcherApp(config)
    error = RuntimeError(
        "error connecting to api.github.com\n"
        "check your internet connection or https://githubstatus.com"
    )
    with patch("pr_watcher.github.fetch_all_team_prs", side_effect=error):
        try:
            async with app.run_test(size=(220, 40)) as pilot:
                await pilot.pause(0.5)

                error_panel = app.query_one("#error-panel")
                assert not error_panel.display, "error-panel must stay hidden for connectivity errors"

                status_text = str(app.query_one("#status-bar", Static).renderable)
                assert "No internet connection" in status_text, (
                    f"status bar should contain 'No internet connection', got: {status_text!r}"
                )
        except WorkerFailed:
            pass  # Textual re-raises worker exceptions on teardown; assertions already ran
