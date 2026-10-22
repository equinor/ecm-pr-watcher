"""Textual UI regression tests for PRWatcherApp.

These tests use App.run_test() to run the app headlessly and verify that the
DataTable never produces a horizontal scrollbar after the column-width algorithm
runs.

Root cause guarded against:
    DataTable.Column.get_render_width() adds ``2 * cell_padding`` (default = 1)
    to every column's stored width. Every column needs two characters of
    rendering overhead. Under-counting that overhead causes
    virtual_size.width > size.width and a persistent horizontal scrollbar.
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from rich.cells import cell_len
from rich.color import Color
from rich.text import Text
from textual.coordinate import Coordinate
from textual.events import Click, MouseScrollDown, MouseScrollUp
from textual.widgets import Static
from textual.widgets._tooltip import Tooltip

from pr_watcher.app import PRTable, PRWatcherApp, RESTART_AFTER_UPDATE
from pr_watcher.config import Config, SETTINGS_FILE
from pr_watcher.github import RateLimitError


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
        "commits": {"nodes": [
            {"commit": {"statusCheckRollup": {"state": "SUCCESS"}}},
        ]},
        "isDraft": False,
        "createdAt": _iso(timedelta(days=2)),
        "updatedAt": _iso(timedelta(hours=2)),
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
        "commits": {"nodes": [
            {"commit": {"statusCheckRollup": {"state": "PENDING"}}},
        ]},
        "isDraft": False,
        "createdAt": _iso(timedelta(days=5)),
        "updatedAt": _iso(timedelta(days=1)),
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
        "commits": {"nodes": [
            {"commit": {"statusCheckRollup": {"state": "FAILURE"}}},
        ]},
        "isDraft": False,
        "createdAt": _iso(timedelta(hours=3)),
        "updatedAt": _iso(timedelta(minutes=30)),
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


@pytest.mark.parametrize("width", [80, 120])
async def test_no_horizontal_scroll_narrow_terminal(width):
    """Column widths adapt correctly on narrow terminals."""
    config = Config(org="Equinor")
    app = PRWatcherApp(config)
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS):
        async with app.run_test(size=(width, 30)) as pilot:
            await _run_with_mock_prs(app, pilot)
            table = app.query_one("#pr-table")
            assert table.virtual_size.width <= table.size.width, (
                f"Horizontal overflow at {width} cols: virtual={table.virtual_size.width}, "
                f"visible={table.size.width}"
            )
            assert table.cell_padding == (0 if width == 80 else 1)


async def test_no_horizontal_scroll_after_resize():
    """Column widths adapt correctly after terminal resize."""
    config = Config(org="Equinor")
    app = PRWatcherApp(config)
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)

            await pilot.resize_terminal(80, 40)
            await pilot.pause(0.2)

            table = app.query_one("#pr-table")
            assert table.virtual_size.width <= table.size.width, (
                f"Horizontal overflow after resize to 80: virtual={table.virtual_size.width}, "
                f"visible={table.size.width}"
            )
            assert table.cell_padding == 0


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


async def test_status_column_shows_checks_and_updates_on_refresh():
    prs = deepcopy(MOCK_PRS)
    app = PRWatcherApp(Config(org="Equinor"))
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=prs):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            table = app.query_one("#pr-table", PRTable)
            column = app._col_keys["review"]
            for pr, expected, color in zip(prs, [
                "● ✅ Approved", "  ⏳ Review Needed", "● ❌ Changes Req.",
            ], ["green", None, "red"]):
                status = table.get_cell(app._row_key(pr), column)
                assert isinstance(status, Text)
                assert status.plain == expected
                assert [(span.start, span.end, span.style) for span in status.spans] == (
                    [(0, 1, color)] if color else []
                )
                assert cell_len(status.plain) <= table.columns[column].width

            prs[1]["commits"]["nodes"][0]["commit"]["statusCheckRollup"]["state"] = "SUCCESS"
            app.action_refresh()
            await pilot.pause(0.5)
            status = table.get_cell(app._row_key(prs[1]), column)
            assert status.plain == "● ⏳ Review Needed"
            assert status.spans[0].style == "green"


async def test_tick_during_shutdown_does_not_update_or_refresh():
    app = PRWatcherApp(Config(org="Equinor"))
    close_all = app._close_all

    async def close_all_with_pending_tick() -> None:
        try:
            await app.query_one("#status-bar", Static).remove()
            assert not app.is_running
            app._next_refresh = datetime.now() - timedelta(seconds=1)
            app._tick()
        finally:
            await close_all()

    with (
        patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS) as fetch,
        patch.object(app, "_close_all", side_effect=close_all_with_pending_tick),
    ):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)

        fetch.assert_called_once()


async def test_review_status_labels_align():
    prs = deepcopy(MOCK_PRS)
    no_reviews = deepcopy(prs[1])
    no_reviews.update(number=101, reviewDecision=None)
    draft = deepcopy(prs[0])
    draft.update(number=102, isDraft=True)
    prs.extend([no_reviews, draft])
    labels = ["Approved", "Review Needed", "Changes Req.", "No Reviews", "Draft"]

    app = PRWatcherApp(Config(org="Equinor"))
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=prs):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            table = app.query_one("#pr-table", PRTable)
            column = app._col_keys["review"]
            label_columns = []
            for row, (pr, label) in enumerate(zip(prs, labels)):
                status = table.get_cell(app._row_key(pr), column)
                assert cell_len(status.plain) <= table.columns[column].width
                rendered = "".join(
                    segment.text
                    for segment in table.render_line(row + table.header_height)
                )
                label_columns.append(cell_len(rendered[:rendered.index(label)]))
            assert len(set(label_columns)) == 1


async def test_checks_indicator_keeps_color_on_highlighted_row():
    app = PRWatcherApp(Config(org="Equinor"))
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            table = app.query_one("#pr-table", PRTable)
            table.focus()
            for row, color in [(0, "green"), (2, "red")]:
                table.move_cursor(row=row)
                await pilot.pause()
                circle = next(
                    segment
                    for segment in table.render_line(row + table.header_height)
                    if "●" in segment.text
                )
                assert circle.style.color == Color.parse(color)
                assert circle.style.bgcolor == table.get_component_rich_style(
                    "datatable--cursor",
                ).bgcolor


async def test_last_change_column_uses_updated_timestamp():
    app = PRWatcherApp(Config(org="Equinor"))
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            table = app.query_one("#pr-table", PRTable)
            assert [str(column.label) for column in table.ordered_columns][6:9] == [
                "Age", "Last change", "Comments",
            ]
            assert table.get_cell(
                "Equinor/ecm-api-backend#42", app._col_keys["updated"],
            ) == "2h"


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


async def test_priority_sort_orders_rows_and_opens_matching_pr():
    """With sort=priority, rows follow the priority tiers and Enter opens the matching PR."""
    draft = {
        **deepcopy(MOCK_PRS[1]),
        "number": 7,
        "isDraft": True,
        "createdAt": _iso(timedelta(days=30)),
        "url": "https://github.com/Equinor/ecm-iso-wp-gl0560-api-iac/pull/7",
    }
    reviewed = {
        **deepcopy(MOCK_PRS[1]),
        "number": 8,
        "reviewRequests": {"nodes": [{"requestedReviewer": {"login": "dave"}}]},
        "createdAt": _iso(timedelta(days=10)),
        "url": "https://github.com/Equinor/ecm-iso-wp-gl0560-api-iac/pull/8",
    }
    prs = [MOCK_PRS[2], MOCK_PRS[0], MOCK_PRS[1], reviewed, draft]
    config = Config(org="Equinor", sort="priority")
    app = PRWatcherApp(config)
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=prs):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            table = app.query_one("#pr-table", PRTable)
            expected = [MOCK_PRS[1], reviewed, MOCK_PRS[2], MOCK_PRS[0], draft]
            assert [row.key.value for row in table.ordered_rows] == [
                app._row_key(pr) for pr in expected
            ]

            table.move_cursor(row=3)
            with patch("pr_watcher.app.open_url") as mock_open:
                app.action_open_pr()
            mock_open.assert_called_once_with(MOCK_PRS[0]["url"])


async def test_updated_sort_orders_most_recent_change_first():
    app = PRWatcherApp(Config(org="Equinor", sort="updated"))
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            table = app.query_one("#pr-table", PRTable)
            assert [row.key.value for row in table.ordered_rows] == [
                app._row_key(MOCK_PRS[2]),
                app._row_key(MOCK_PRS[0]),
                app._row_key(MOCK_PRS[1]),
            ]


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


async def test_failed_refresh_keeps_rows_and_recovery_does_not_ring_bell():
    cached_inputs = []

    def fetch(_org, _team_slug, cached_prs, failed_repos, _ignore_repos):
        cached_inputs.append(deepcopy(cached_prs))
        if len(cached_inputs) == 2:
            failed_repos.extend(["Equinor/repo-a", "Equinor/repo-b"])
            return list(cached_prs)
        return deepcopy(MOCK_PRS)

    app = PRWatcherApp(Config(org="Equinor"))
    with (
        patch("pr_watcher.github.fetch_all_team_prs", side_effect=fetch),
        patch.object(app, "bell") as bell,
    ):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            expected_rows = [app._row_key(pr) for pr in MOCK_PRS]

            app.action_refresh()
            await pilot.pause(0.5)
            assert _row_keys(app) == expected_rows
            assert "⚠ 2 repos stale" in _status_text(app)

            app.action_refresh()
            await pilot.pause(0.5)
            assert _row_keys(app) == expected_rows
            assert "repos stale" not in _status_text(app)

    assert cached_inputs[0] == []
    assert cached_inputs[1] == MOCK_PRS
    bell.assert_not_called()


@pytest.mark.parametrize("initial_prs", [[], MOCK_PRS])
async def test_rate_limit_pauses_refresh_and_resumes_with_cached_rows(
    initial_prs: list[dict],
) -> None:
    retry_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    error = RateLimitError("API rate limit exceeded", retry_at)
    app = PRWatcherApp(Config(org="Equinor"))
    with (
        patch(
            "pr_watcher.github.fetch_all_team_prs",
            side_effect=[deepcopy(initial_prs), error, deepcopy(MOCK_PRS)],
        ) as fetch,
        patch.object(app, "bell") as bell,
    ):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            last_updated = app._last_updated
            expected_rows = _row_keys(app)
            app.action_refresh()
            await pilot.pause(0.5)

            assert not app._loading
            assert app._rate_limit_until == (
                retry_at.astimezone().replace(tzinfo=None) + timedelta(seconds=1)
            )
            assert app._next_refresh == app._rate_limit_until
            assert app._last_updated == last_updated
            assert _row_keys(app) == expected_rows
            assert app.query_one("#pr-table").display == bool(initial_prs)
            assert "Rate limited - refresh paused until" in _status_text(app)
            assert app._rate_limit_until.strftime("%H:%M:%S") in _status_text(app)
            assert "Refresh in" not in _status_text(app)
            if initial_prs:
                assert "stale data" in _status_text(app)

            with patch("pr_watcher.app.datetime") as clock:
                clock.now.return_value = app._rate_limit_until - timedelta(seconds=90)
                app._tick()
                assert "(90s)" in _status_text(app)
                clock.now.return_value += timedelta(seconds=1)
                app._tick()
                assert "(89s)" in _status_text(app)

            await pilot.press("r")
            app._start_fetch()
            app._tick()
            assert fetch.call_count == 2
            assert app._next_refresh == app._rate_limit_until

            app._rate_limit_until = datetime.now() - timedelta(seconds=1)
            app._next_refresh = app._rate_limit_until
            app._tick()
            await pilot.pause(0.5)
            assert fetch.call_count == 3
            assert _row_keys(app) == [app._row_key(pr) for pr in MOCK_PRS]
            assert app._rate_limit_until is None
            assert app._rate_limit_attempts == 0
            assert "Rate limited" not in _status_text(app)
            assert "Refresh in" in _status_text(app)
            bell.assert_not_called()


@pytest.mark.parametrize("retry_at", [
    None, datetime(2000, 1, 1, tzinfo=timezone.utc),
])
async def test_rate_limit_on_startup_backoff_increases_until_success(
    retry_at: datetime | None,
) -> None:
    app = PRWatcherApp(Config(org="Equinor"))
    error = RateLimitError("You have exceeded a secondary rate limit", retry_at)
    with patch(
        "pr_watcher.github.fetch_all_team_prs",
        side_effect=[error, error, deepcopy(MOCK_PRS), error],
    ) as fetch:
        async with app.run_test(size=(220, 40)) as pilot:
            before = datetime.now()
            await pilot.pause(0.5)
            assert app.query_one("#error-panel").display
            assert "Rate limited" in _status_text(app)
            assert app._rate_limit_until >= before + timedelta(seconds=60)
            assert app._rate_limit_until <= datetime.now() + timedelta(seconds=61)
            assert app._last_updated is None

            app._rate_limit_until = datetime.now() - timedelta(seconds=1)
            app._next_refresh = app._rate_limit_until
            before = datetime.now()
            app._tick()
            await pilot.pause(0.5)
            assert fetch.call_count == 2
            assert app._rate_limit_until >= before + timedelta(seconds=120)
            assert app._rate_limit_attempts == 2

            app._rate_limit_until = datetime.now() - timedelta(seconds=1)
            app.action_refresh()
            await pilot.pause(0.5)
            assert fetch.call_count == 3
            assert app._rate_limit_attempts == 0

            before = datetime.now()
            app.action_refresh()
            await pilot.pause(0.5)
            assert app._rate_limit_until >= before + timedelta(seconds=60)
            assert app._rate_limit_until <= datetime.now() + timedelta(seconds=61)
            assert app._rate_limit_attempts == 1


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
    config = Config(org="Equinor")
    app = PRWatcherApp(config)
    error = RuntimeError(
        "error connecting to api.github.com\n"
        "check your internet connection or https://githubstatus.com"
    )
    with patch("pr_watcher.github.fetch_all_team_prs", side_effect=error):
        async with app.run_test(size=(220, 40)) as pilot:
            await pilot.pause(0.5)

            error_panel = app.query_one("#error-panel")
            assert not error_panel.display, "error-panel must stay hidden for connectivity errors"
            assert "No internet connection" in _status_text(app)


def _row_keys(app: PRWatcherApp) -> list[str]:
    table = app.query_one("#pr-table", PRTable)
    return [row.key.value for row in table.ordered_rows]


def _status_text(app: PRWatcherApp) -> str:
    return str(app.query_one("#status-bar", Static).content)


def _i_label(app: PRWatcherApp) -> str:
    return app.screen.active_bindings["i"].binding.description


async def _click_ignored_link(app: PRWatcherApp, pilot) -> None:
    """Click the 🙈 segment in the status bar, like a user would."""
    status = app.query_one("#status-bar", Static)
    plain = status.visual.plain
    x = status.styles.padding.left + cell_len(plain[: plain.index("🙈")]) + 3
    await pilot.click("#status-bar", offset=(x, 0))
    await pilot.pause()


async def test_ignore_hides_pr_and_persists(isolated_state_file):
    """Pressing i hides the PR, saves it to the state file and Enter maps to visible rows."""
    app = PRWatcherApp(Config(org="Equinor", ignore_days=7))
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            await pilot.press("i")
            await pilot.pause()

            assert _row_keys(app) == [app._row_key(MOCK_PRS[1]), app._row_key(MOCK_PRS[2])]
            assert "2 open PRs" in _status_text(app)
            assert "🙈 1 ignored" in _status_text(app)

            saved = json.loads(isolated_state_file.read_text(encoding="utf-8"))["ignored"]
            until = datetime.fromisoformat(saved[app._row_key(MOCK_PRS[0])])
            expected = datetime.now(timezone.utc) + timedelta(days=7)
            assert abs((until - expected).total_seconds()) < 60

            with patch("pr_watcher.app.open_url") as mock_open:
                app.action_open_pr()
            mock_open.assert_called_once_with(MOCK_PRS[1]["url"])


async def test_ignored_view_shows_only_ignored_and_restores(isolated_state_file):
    """Clicking the ignored count toggles the ignored-only view; i there restores the PR."""
    app = PRWatcherApp(Config(org="Equinor"))
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            table = app.query_one("#pr-table", PRTable)
            table.move_cursor(row=1)
            assert _i_label(app) == "Ignore"
            await pilot.press("i")
            await pilot.pause()
            await _click_ignored_link(app, pilot)

            assert app._show_ignored
            assert _i_label(app) == "Restore PR"
            assert _row_keys(app) == [app._row_key(MOCK_PRS[1])]
            assert str(table.columns[app._col_keys["age"]].label) == "Until"
            assert "🙈 1 ignored PR " in _status_text(app)
            with patch("pr_watcher.app.open_url") as mock_open:
                app.action_open_pr()
            mock_open.assert_called_once_with(MOCK_PRS[1]["url"])

            await pilot.press("i")
            await pilot.pause()
            assert _row_keys(app) == []
            assert json.loads(isolated_state_file.read_text(encoding="utf-8")) == {"ignored": {}}

            await _click_ignored_link(app, pilot)
            assert not app._show_ignored
            assert _i_label(app) == "Ignore"
            assert _row_keys(app) == [app._row_key(pr) for pr in MOCK_PRS]
            assert str(table.columns[app._col_keys["age"]].label) == "Age"


async def test_existing_ignores_load_and_expired_ones_are_pruned(isolated_state_file):
    """Active ignores from the state file hide PRs; expired ones are dropped and saved."""
    now = datetime.now(timezone.utc)
    isolated_state_file.write_text(
        json.dumps({"ignored": {
            "Equinor/ecm-iso-wp-gl0560-api-iac#41": (now + timedelta(days=3)).isoformat(),
            "Equinor/ecm-wo-preparation-service#100": (now - timedelta(minutes=1)).isoformat(),
        }}),
        encoding="utf-8",
    )
    app = PRWatcherApp(Config(org="Equinor"))
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            assert _row_keys(app) == [app._row_key(MOCK_PRS[0]), app._row_key(MOCK_PRS[2])]
            saved = json.loads(isolated_state_file.read_text(encoding="utf-8"))["ignored"]
            assert list(saved) == ["Equinor/ecm-iso-wp-gl0560-api-iac#41"], "expired entry should be saved away"

            app._ignores["Equinor/ecm-api-backend#42"] = now - timedelta(seconds=1)
            app._rebuild_table()
            saved = json.loads(isolated_state_file.read_text(encoding="utf-8"))["ignored"]
            assert list(saved) == ["Equinor/ecm-iso-wp-gl0560-api-iac#41"]


async def test_cursor_follows_pr_across_refresh_so_ignore_targets_it(isolated_state_file):
    """A refresh keeps the cursor on the same PR even if the order changes, so i hits it."""
    app = PRWatcherApp(Config(org="Equinor"))
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS) as fetch:
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            table = app.query_one("#pr-table", PRTable)
            table.move_cursor(row=0)
            target = _row_keys(app)[0]
            last = len(MOCK_PRS) - 1

            fetch.return_value = list(reversed(MOCK_PRS))
            app.action_refresh()
            await _run_with_mock_prs(app, pilot)
            assert _row_keys(app)[last] == target, "refresh should apply new order"
            assert table.cursor_row == last

            await pilot.press("i")
            await pilot.pause()
            saved = json.loads(isolated_state_file.read_text(encoding="utf-8"))["ignored"]
            assert list(saved) == [target]
            assert table.cursor_row == last - 1, "cursor should clamp to the new last row"


async def test_ignored_view_hint_stays_visible_while_refreshing(isolated_state_file):
    app = PRWatcherApp(Config(org="Equinor"))
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            await pilot.press("i")
            await pilot.pause()
            await _click_ignored_link(app, pilot)
            app._loading = True
            app._update_status_bar()
            status = _status_text(app)
            assert "🙈 1 ignored PR (click to go back)" in status
            assert "Refreshing" in status


async def test_corrupt_state_file_warns_and_starts(isolated_state_file):
    isolated_state_file.write_text("{not json", encoding="utf-8")
    app = PRWatcherApp(Config(org="Equinor"))
    with patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            assert len(_row_keys(app)) == len(MOCK_PRS)
            assert any("unreadable" in n.message for n in app._notifications)


async def test_update_shortcut_restarts_when_head_changes(isolated_state_file):
    app = PRWatcherApp(Config(org="Equinor"))
    with (
        patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS),
        patch("pr_watcher.app.pull_updates", return_value=True) as update,
    ):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            with patch.object(app, "exit") as exit_app:
                await pilot.press("u")
                await pilot.pause(0.2)

            update.assert_called_once_with(SETTINGS_FILE.parent)
            exit_app.assert_called_once_with(result=RESTART_AFTER_UPDATE)
            assert not app._checking_update


async def test_update_shortcut_reports_current_version(isolated_state_file):
    app = PRWatcherApp(Config(org="Equinor"))
    with (
        patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS),
        patch("pr_watcher.app.pull_updates", return_value=False),
    ):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            await pilot.press("u")
            await pilot.pause(0.2)

            assert any(
                notification.message == "Already up to date."
                for notification in app._notifications
            )
            assert not app._checking_update


async def test_update_shortcut_reports_failure(isolated_state_file):
    app = PRWatcherApp(Config(org="Equinor"))
    with (
        patch("pr_watcher.github.fetch_all_team_prs", return_value=MOCK_PRS),
        patch(
            "pr_watcher.app.pull_updates",
            side_effect=RuntimeError("Cannot update a dirty worktree."),
        ),
    ):
        async with app.run_test(size=(220, 40)) as pilot:
            await _run_with_mock_prs(app, pilot)
            await pilot.press("u")
            await pilot.pause(0.2)

            assert any(
                notification.message
                == "Update failed: Cannot update a dirty worktree."
                for notification in app._notifications
            )
            assert not app._checking_update
