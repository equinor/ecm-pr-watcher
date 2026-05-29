"""
pr-watcher: Textual TUI application for monitoring GitHub team Pull Requests.

Layout
------
┌─────────────────────────────────────────────────────────────┐
│  🔍 PR Watcher  |  Org: acme  |  Team: ECM WO Preparation  │  ← header (1 line)
├──────────────────────────────────────────────────────────────┤
│  DataTable (fills remaining height)                         │
│   #   Repository     Title             Author  Status  Age  │
│  ──  ────────────────────────────────────────────────────── │
│ ► #42  my-service  feat: add widget  alice   ✓ Appr.  2d   │  ← selected row
│   #41  api-gateway fix: null check   bob    ⏳ Review  5d   │
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
from textual.widgets import DataTable, Footer, LoadingIndicator, Static
from textual import work
from textual.worker import Worker, WorkerState

from .config import Config
from . import github


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
        self._known_pr_numbers: set[int] = set()
        self._loading: bool = False
        self._error: Optional[str] = None
        self._last_updated: Optional[datetime] = None
        self._next_refresh: Optional[datetime] = None
        self._title_col_key = None  # set in on_mount; guard on_resize from firing first

    def _update_title_col_width(self) -> None:
        """Recalculate the Title column width to fill available terminal width."""
        if self._title_col_key is None:
            return
        # Sum of all fixed columns + 1-char separator between each of the 7 columns
        # DataTable also adds 1 cell of left padding.
        FIXED_COLS = 7 + 22 + 15 + 16 + 5 + 22   # all except Title
        SEPARATORS = 7                              # 6 inter-column + 1 left pad
        MIN_TITLE = 10
        new_width = max(MIN_TITLE, self.size.width - FIXED_COLS - SEPARATORS)
        table = self.query_one("#pr-table", DataTable)
        table.columns[self._title_col_key].width = new_width
        # Signal DataTable to recalculate layout on next idle cycle
        table._require_update_dimensions = True
        table.check_idle()

    def on_resize(self) -> None:
        self._update_title_col_width()

    # ------------------------------------------------------------------
    # Compose + mount
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(self._header_text(), id="app-header")
        yield LoadingIndicator(id="loading")
        yield Static("", id="error-panel")
        yield DataTable(id="pr-table", cursor_type="row", zebra_stripes=True)
        yield Static("Starting…", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#pr-table", DataTable)
        table.add_column("#", width=7)
        table.add_column("Repository", width=22)
        self._title_col_key = table.add_column("Title", width=48)
        table.add_column("Author", width=15)
        table.add_column("Review Status", width=16)
        table.add_column("Age", width=5)
        table.add_column("Labels", width=22)

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
            new_numbers = {pr["number"] for pr in new_prs}

            # Ring bell for any PRs that weren't in the previous fetch
            if self.config.bell and self._known_pr_numbers and (new_numbers - self._known_pr_numbers):
                self.bell()

            self._known_pr_numbers = new_numbers
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

            error_panel = self.query_one("#error-panel", Static)
            is_auth = any(
                x in self._error.lower()
                for x in ["auth", "401", "not logged", "logged in", "authentication"]
            )
            not_installed = "not installed" in self._error.lower()

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

    def _rebuild_table(self) -> None:
        table = self.query_one("#pr-table", DataTable)
        table.clear()
        for pr in self._prs:
            table.add_row(
                f"#{pr['number']}",
                ellipsis_middle(repo_short_name(pr.get("repository", "")), 22),
                pr.get("title", ""),
                pr.get("author", {}).get("login", ""),
                format_review_status(pr),
                format_age(pr.get("createdAt", "")),
                format_labels(pr.get("labels", [])),
                key=str(pr["number"]),
            )

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

        if self._error and self._prs:
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

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Open the selected PR in the browser when Enter is pressed."""
        if not self._prs or event.row_key is None:
            return
        try:
            pr_number = int(str(event.row_key.value))
        except (TypeError, ValueError):
            return
        pr = next((p for p in self._prs if p["number"] == pr_number), None)
        if pr:
            url = pr.get("url", "")
            if url:
                open_url(url)
                self.notify(
                    f"Opened PR #{pr['number']} in browser",
                    title="PR Watcher",
                    timeout=2,
                )

    def action_open_pr(self) -> None:
        table = self.query_one("#pr-table", DataTable)
        if table.row_count == 0 or not self._prs:
            return
        row_idx = table.cursor_row
        if 0 <= row_idx < len(self._prs):
            pr = self._prs[row_idx]
            url = pr.get("url", "")
            if url:
                open_url(url)
                self.notify(
                    f"Opened PR #{pr['number']} in browser",
                    title="PR Watcher",
                    timeout=2,
                )
