"""
pr-watcher: Textual TUI application for monitoring GitHub team Pull Requests.

Layout
------
┌─────────────────────────────────────────────────────────────┐
│  🔍 PR Watcher  |  Org: acme  |  Team: ECM WO Preparation  │  ← header (1 line)
├──────────────────────────────────────────────────────────────┤
│  DataTable (fills remaining height)                         │
│   #   Repository     Title             Author  Status  Age  Comments │
│  ──  ────────────────────────────────────────────────────────────── │
│ ► #42  my-service  feat: add widget  alice   ✓ Appr.  2d      3    │  ← selected row
│   #41  api-gateway fix: null check   bob    ⏳ Review  5d      1    │
├──────────────────────────────────────────────────────────────┤
│  📋 12 PRs  │  Updated 14:23:01  │  Refresh in 45s          │  ← status bar
├──────────────────────────────────────────────────────────────┤
│  q quit   r refresh   Enter open in browser                 │  ← Footer
└──────────────────────────────────────────────────────────────┘
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.events import Click, MouseScrollDown, MouseScrollUp, Resize
from textual.message import Message
from textual.widgets import DataTable, Footer, LoadingIndicator, Static
from textual.widgets._data_table import RowKey
from textual import work
from textual.worker import Worker, WorkerState

from .config import Config
from . import github

_CONNECTIVITY_MARKERS = (
    "error connecting",
    "check your internet",
    "githubstatus",
    "network",
    "connection refused",
    "timeout",
)

# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

APP_CSS = """
Screen {
    background: $background;
    layout: vertical;
}

#app-header {
    height: 1;
    background: #1a5fb4;
    color: white;
    text-style: bold;
    padding: 0 1;
}

#loading {
    height: 1fr;
    align: center middle;
}

#error-panel {
    height: 1fr;
    border: solid $error;
    color: $error;
    padding: 2 4;
    display: none;
}

#pr-table {
    height: 1fr;
    display: none;
}

#status-bar {
    height: 1;
    background: $panel;
    color: $text-muted;
    padding: 0 1;
}
"""


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_age(created_at: str) -> str:
    """Turn an ISO-8601 timestamp into a human-readable age like '2d', '3h'."""
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        secs = int((datetime.now(dt.tzinfo) - dt).total_seconds())
        if secs < 3600:
            return f"{secs // 60}m"
        elif secs < 86_400:
            return f"{secs // 3600}h"
        elif secs < 7 * 86_400:
            return f"{secs // 86_400}d"
        elif secs < 30 * 86_400:
            return f"{secs // (7 * 86_400)}w"
        else:
            return f"{secs // (30 * 86_400)}mo"
    except Exception:
        return "?"


def format_review_status(pr: dict) -> str:
    if pr.get("isDraft"):
        return "◌ Draft"
    decision = pr.get("reviewDecision") or ""
    return {
        "APPROVED": "✓ Approved",
        "CHANGES_REQUESTED": "✗ Changes Req.",
        "REVIEW_REQUIRED": "⏳ Review Needed",
    }.get(decision, "— No Reviews")


def format_labels(labels: list[dict]) -> str:
    if not labels:
        return ""
    names = [lbl["name"] for lbl in labels[:3]]
    result = ", ".join(names)
    if len(labels) > 3:
        result += f" +{len(labels) - 3}"
    return result


def comment_count(pr: dict) -> int:
    return pr.get("totalCommentsCount", 0)


def repo_short_name(full_name: str) -> str:
    parts = full_name.split("/", 1)
    return parts[1] if len(parts) == 2 else full_name


def ellipsis_middle(s: str, width: int) -> str:
    """Truncate a string to `width` chars using a middle ellipsis.

    Preserves both the start and the end of the string so suffixes like
    '-iac' remain visible. E.g. 'ecm-iso-wp-gl0560-api-iac' → 'ecm-iso…-iac'
    """
    if len(s) <= width:
        return s
    # Reserve 1 char for the ellipsis; split remaining budget ~60/40 favouring the start
    budget = width - 1
    tail = budget // 3
    head = budget - tail
    return f"{s[:head]}…{s[len(s) - tail:]}"


def open_url(url: str) -> None:
    """Open a URL in the default system browser, cross-platform."""
    try:
        if sys.platform == "win32":
            subprocess.Popen(
                ["cmd", "/c", "start", "", url],
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url])
        else:
            subprocess.Popen(["xdg-open", url])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class PRTable(DataTable):
    """DataTable that moves the row cursor on mouse-wheel and opens PRs on middle-click."""

    class MiddleClick(Message):
        """Posted when the user middle-clicks a row."""
        def __init__(self, row_key: RowKey) -> None:
            super().__init__()
            self.row_key = row_key

    def on_mouse_scroll_down(self, event: MouseScrollDown) -> None:
        self.action_cursor_down()
        event.stop()

    def on_mouse_scroll_up(self, event: MouseScrollUp) -> None:
        self.action_cursor_up()
        event.stop()

    def on_click(self, event: Click) -> None:
        if event.button != 2:
            return
        coord = self.hover_coordinate
        if not self.is_valid_coordinate(coord):
            return
        row_key, _ = self.coordinate_to_cell_key(coord)
        self.post_message(self.MiddleClick(row_key))


class PRWatcherApp(App):
    CSS = APP_CSS

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh now"),
        Binding("enter", "open_pr", "Open in browser"),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self._prs: list[dict] = []
        self._known_prs: set[tuple[str, int]] = set()
        self._comment_counts: dict[tuple[str, int], int] = {}
        self._unread_comment_updates: set[tuple[str, int]] = set()
        self._loading: bool = False
        self._error: Optional[str] = None
        self._last_updated: Optional[datetime] = None
        self._next_refresh: Optional[datetime] = None
        self._col_keys: dict[str, object] = {}  # populated in on_mount
        self._last_opened_url: Optional[str] = None
        self._last_opened_at: Optional[datetime] = None

    @property
    def _title_col_key(self):
        return self._col_keys.get("title")

    def _compute_col_widths(self, terminal_width: int | None = None) -> dict[str, int]:
        """Measure actual content widths; title is capped at content, labels gets the excess.

        Args:
            terminal_width: Override the terminal width (used from on_resize where
                self.size.width has not yet been updated by the layout engine).
        """
        mins = {
            "number": 2,
            "repo": 10,
            "title": 10,
            "author": 6,
            "review": 8,
            "age": 3,
            "comments": 8,
            "labels": 0,
        }
        maxes = {
            "number": 7,
            "repo": 30,
            "title": 999,
            "author": 20,
            "review": 16,
            "age": 5,
            "comments": 8,
            "labels": 40,
        }

        w = dict(mins)
        for pr in self._prs:
            w["number"] = max(w["number"], len(f"#{pr['number']}"))
            w["repo"]   = max(w["repo"],   len(repo_short_name(pr.get("repository", ""))))
            w["title"]  = max(w["title"],  len(pr.get("title", "")))
            w["author"] = max(w["author"], len(pr.get("author", {}).get("login", "")))
            w["review"] = max(w["review"], len(format_review_status(pr)))
            w["age"]    = max(w["age"],    len(format_age(pr.get("createdAt", ""))))
            w["comments"] = max(w["comments"], len(str(comment_count(pr))) + 1)
            w["labels"] = max(w["labels"], len(format_labels(pr.get("labels", []))))

        # Apply caps to all columns
        for k in mins:
            w[k] = max(mins[k], min(w[k], maxes[k]))

        # Truly fixed columns: number, repo, author, review, age, comments
        # DataTable.Column.get_render_width() adds 2*cell_padding (default=1) to every
        # column's width, so with 8 columns the real rendering overhead is 8*2*1 = 16.
        NUM_COLS = 8
        CELL_PADDING = 1  # DataTable default
        SEPARATORS = NUM_COLS * 2 * CELL_PADDING  # = 16
        fixed = sum(w[k] for k in ("number", "repo", "author", "review", "age", "comments"))
        width = terminal_width if terminal_width is not None else self.size.width
        remaining = max(mins["title"] + mins["labels"], width - fixed - SEPARATORS)

        # Title takes only what its content needs; labels gets whatever is left (up to its cap)
        title_w  = max(mins["title"],  min(w["title"],  remaining - mins["labels"]))
        labels_w = max(mins["labels"], min(remaining - title_w, maxes["labels"]))
        # If labels hit its cap and space remains, let title grow back into it
        title_w  = max(title_w, remaining - labels_w)

        w["title"]  = title_w
        w["labels"] = labels_w
        return w

    def _apply_col_widths(self, widths: dict[str, int]) -> None:
        if not self._col_keys:
            return
        table = self.query_one("#pr-table", DataTable)
        for key, col_key in self._col_keys.items():
            if key in widths:
                table.columns[col_key].width = widths[key]
        table._require_update_dimensions = True
        table.check_idle()

    def _update_title_col_width(self, terminal_width: int | None = None) -> None:
        """On terminal resize: recompute all widths (keeps content-fit, adjusts title)."""
        if not self._col_keys:
            return
        self._apply_col_widths(self._compute_col_widths(terminal_width=terminal_width))

    def on_resize(self, event: Resize) -> None:
        self._update_title_col_width(terminal_width=event.size.width)

    # ------------------------------------------------------------------
    # Compose + mount
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(self._header_text(), id="app-header")
        yield LoadingIndicator(id="loading")
        yield Static("", id="error-panel")
        yield PRTable(id="pr-table", cursor_type="row", zebra_stripes=True, show_row_labels=False)
        yield Static("Starting…", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#pr-table", DataTable)
        self._col_keys["number"] = table.add_column("#",             width=7)
        self._col_keys["repo"]   = table.add_column("Repository",   width=22)
        self._col_keys["title"]  = table.add_column("Title",        width=48)
        self._col_keys["author"] = table.add_column("Author",       width=15)
        self._col_keys["review"] = table.add_column("Review Status",width=16)
        self._col_keys["age"]    = table.add_column("Age",          width=5)
        self._col_keys["comments"] = table.add_column("Comments",   width=8)
        self._col_keys["labels"] = table.add_column("Labels",       width=22)

        # Set initial title column width based on actual terminal size
        self._update_title_col_width()

        # Start in loading state
        self._set_view("loading")
        self._start_fetch()

        # 1-second tick for countdown updates and auto-refresh trigger
        self.set_interval(1, self._tick)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _header_text(self) -> str:
        return (
            f"🔍 PR Watcher  |  Org: {self.config.org}"
            f"  |  Team: {self.config.team}"
            f"  |  Refresh: every {self.config.refresh_interval}s"
        )

    def _set_view(self, state: str) -> None:
        """Switch the main content area between loading / error / table."""
        self.query_one("#loading").display = state == "loading"
        self.query_one("#error-panel").display = state == "error"
        self.query_one("#pr-table").display = state == "table"

    def _start_fetch(self) -> None:
        self._loading = True
        # Keep existing table visible during background refresh (if we have data)
        if not self._prs:
            self._set_view("loading")
        self._update_status_bar()
        self._fetch_prs()

    # ------------------------------------------------------------------
    # Worker — runs gh calls in a thread pool
    # ------------------------------------------------------------------

    @work(thread=True, exclusive=True, name="fetch-prs")
    def _fetch_prs(self) -> list[dict]:
        return github.fetch_all_team_prs(self.config.org, self.config.team_slug)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name != "fetch-prs":
            return

        if event.state == WorkerState.SUCCESS:
            new_prs: list[dict] = event.worker.result or []
            new_identities = {self._pr_identity(pr) for pr in new_prs}
            new_comment_counts = {
                self._pr_identity(pr): comment_count(pr) for pr in new_prs
            }

            # Ring bell for any PRs that weren't in the previous fetch
            if self.config.bell and self._known_prs and (new_identities - self._known_prs):
                self.bell()

            self._unread_comment_updates.intersection_update(new_comment_counts)
            self._unread_comment_updates.update(
                identity
                for identity, count in new_comment_counts.items()
                if identity in self._comment_counts
                and count > self._comment_counts[identity]
            )
            self._comment_counts = new_comment_counts
            self._known_prs = new_identities
            self._prs = new_prs
            self._error = None
            self._loading = False
            self._last_updated = datetime.now()
            self._next_refresh = datetime.now() + timedelta(
                seconds=self.config.refresh_interval
            )
            self._rebuild_table()
            self._set_view("table")
            self._update_status_bar()

        elif event.state == WorkerState.ERROR:
            self._error = str(event.worker.error)
            self._loading = False
            self._last_updated = datetime.now()
            self._next_refresh = datetime.now() + timedelta(
                seconds=self.config.refresh_interval
            )

            error_lc = self._error.lower()
            is_connectivity = any(x in error_lc for x in _CONNECTIVITY_MARKERS)
            is_auth = any(x in error_lc for x in ["auth", "401", "not logged", "logged in", "authentication"])
            not_installed = "not installed" in error_lc

            if is_connectivity:
                # Transient network error — stay in current view and auto-retry
                self._set_view("table" if self._prs else "loading")
            else:
                error_panel = self.query_one("#error-panel", Static)
                if not_installed:
                    msg = (
                        "❌  GitHub CLI (gh) not found\n\n"
                        "Install from: https://cli.github.com/\n\n"
                        "Then run: gh auth login"
                    )
                elif is_auth:
                    msg = (
                        "❌  GitHub CLI authentication required\n\n"
                        "Run:  gh auth login\n\n"
                        "Then restart pr-watcher."
                    )
                else:
                    msg = (
                        f"❌  Error fetching pull requests:\n\n{self._error}\n\n"
                        "Press 'r' to retry."
                    )

                error_panel.update(msg)

                if self._prs:
                    # Keep stale data visible; show warning in status bar
                    self._set_view("table")
                else:
                    self._set_view("error")

            self._update_status_bar()

    # ------------------------------------------------------------------
    # Table population
    # ------------------------------------------------------------------

    @staticmethod
    def _pr_identity(pr: dict) -> tuple[str, int]:
        return pr.get("repository", ""), pr["number"]

    @staticmethod
    def _row_key(pr: dict) -> str:
        repository, number = PRWatcherApp._pr_identity(pr)
        return f"{repository}#{number}"

    def _pr_for_row_key(self, row_key: RowKey) -> Optional[dict]:
        key = str(row_key.value)
        return next((pr for pr in self._prs if self._row_key(pr) == key), None)

    def _comment_display(self, pr: dict) -> str:
        suffix = "!" if self._pr_identity(pr) in self._unread_comment_updates else ""
        return f"{comment_count(pr)}{suffix}"

    def _acknowledge_comment_update(self, pr: dict) -> None:
        identity = self._pr_identity(pr)
        if identity not in self._unread_comment_updates:
            return
        self._unread_comment_updates.remove(identity)
        table = self.query_one("#pr-table", DataTable)
        table.update_cell(
            self._row_key(pr),
            self._col_keys["comments"],
            str(comment_count(pr)),
        )

    def _rebuild_table(self) -> None:
        col_widths = self._compute_col_widths()
        table = self.query_one("#pr-table", DataTable)
        table.clear()
        for pr in self._prs:
            table.add_row(
                f"#{pr['number']}",
                ellipsis_middle(repo_short_name(pr.get("repository", "")), col_widths["repo"]),
                pr.get("title", ""),
                pr.get("author", {}).get("login", ""),
                format_review_status(pr),
                format_age(pr.get("createdAt", "")),
                self._comment_display(pr),
                format_labels(pr.get("labels", [])),
                key=self._row_key(pr),
            )
        self._apply_col_widths(col_widths)

    # ------------------------------------------------------------------
    # Status bar
    # ------------------------------------------------------------------

    def _update_status_bar(self) -> None:
        parts: list[str] = []

        if self._loading:
            parts.append("⟳ Refreshing…")
        else:
            n = len(self._prs)
            icon = "📋" if n else "📭"
            parts.append(f"{icon} {n} open PR{'s' if n != 1 else ''}")

        if self._last_updated:
            parts.append(f"Updated {self._last_updated.strftime('%H:%M:%S')}")

        if not self._loading and self._next_refresh:
            secs = max(0, int((self._next_refresh - datetime.now()).total_seconds()))
            parts.append(f"Refresh in {secs}s")

        if self._error:
            error_lc = self._error.lower()
            if any(x in error_lc for x in _CONNECTIVITY_MARKERS):
                parts.append("⚠ No internet connection — retrying")
            elif self._prs:
                parts.append("⚠ Refresh error — showing stale data")

        self.query_one("#status-bar", Static).update("  │  ".join(parts))

    # ------------------------------------------------------------------
    # Timer tick — countdown + auto-refresh trigger
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        self._update_status_bar()
        if (
            not self._loading
            and self._next_refresh is not None
            and datetime.now() >= self._next_refresh
        ):
            self._next_refresh = None
            self._start_fetch()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_refresh(self) -> None:
        if not self._loading:
            self._next_refresh = None
            self._start_fetch()

    _OPEN_PR_DEBOUNCE = timedelta(seconds=1)

    def _open_pr(self, pr: dict) -> None:
        """Open a PR's URL in the browser, debouncing rapid duplicate triggers.

        A double-click (or a click quickly followed by Enter) can fire this
        twice for the same PR in quick succession, opening two browser tabs.
        Suppress repeat opens of the same URL within a short window.
        """
        url = pr.get("url", "")
        if not url:
            return
        now = datetime.now()
        if (
            url == self._last_opened_url
            and self._last_opened_at is not None
            and now - self._last_opened_at < self._OPEN_PR_DEBOUNCE
        ):
            return
        self._last_opened_url = url
        self._last_opened_at = now
        open_url(url)
        self.notify(
            f"Opened PR #{pr['number']} in browser",
            title="PR Watcher",
            timeout=2,
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Open the selected PR in the browser when Enter is pressed."""
        if not self._prs or event.row_key is None:
            return
        pr = self._pr_for_row_key(event.row_key)
        if pr:
            self._acknowledge_comment_update(pr)
            self._open_pr(pr)

    def on_prtable_middle_click(self, event: PRTable.MiddleClick) -> None:
        """Open the PR URL when the row is middle-clicked."""
        pr = self._pr_for_row_key(event.row_key)
        if pr:
            self._open_pr(pr)

    def action_open_pr(self) -> None:
        table = self.query_one("#pr-table", DataTable)
        if table.row_count == 0 or not self._prs:
            return
        row_idx = table.cursor_row
        if 0 <= row_idx < len(self._prs):
            pr = self._prs[row_idx]
            self._acknowledge_comment_update(pr)
            self._open_pr(pr)
