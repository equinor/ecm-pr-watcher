"""
pr-watcher: Textual TUI application for monitoring GitHub team Pull Requests.

Layout
------
┌─────────────────────────────────────────────────────────────┐
│  🔍 PR Watcher  |  Org: acme  |  Team: ECM WO Preparation  │  ← header (1 line)
├──────────────────────────────────────────────────────────────┤
│  DataTable (fills remaining height)                         │
│   #   Repository     Title             Author  Reviewers  Status  Age  Last change  Comments │
│  ──  ────────────────────────────────────────────────────────────── │
│ ► #42  my-service  feat: add widget  alice   ✓ Appr.  2d      3    │  ← selected row
│   #41  api-gateway fix: null check   bob    ⏳ Review  5d      1    │
├──────────────────────────────────────────────────────────────┤
│  📋 12 PRs  │  🙈 2 ignored  │  Updated 14:23:01  │  Refresh in 45s │  ← status bar
├──────────────────────────────────────────────────────────────┤
│  q quit   r refresh   u update   Enter open   i ignore        │  ← Footer
└──────────────────────────────────────────────────────────────┘

Clicking "🙈 N ignored" in the status bar toggles the ignored-only view, where
`i` restores the PR under the cursor instead of ignoring it.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Optional

from rich.cells import cell_len
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.coordinate import Coordinate
from textual.events import Click, MouseMove, MouseScrollDown, MouseScrollUp, Resize
from textual.message import Message
from textual.widgets import DataTable, Footer, LoadingIndicator, Static
from textual.widgets._data_table import RowKey
from textual import work
from textual.worker import Worker, WorkerState

from .config import Config, SETTINGS_FILE
from . import github
from .ignores import load_ignores, prune_expired, save_ignores

_CONNECTIVITY_MARKERS = (
    "error connecting",
    "check your internet",
    "githubstatus",
    "network",
    "connection refused",
    "timeout",
)

_COPILOT_REVIEWER_LOGINS = {
    "copilot",
    "copilot[bot]",
    "copilot-pull-request-reviewer",
    "copilot-pull-request-reviewer[bot]",
}

_UPDATE_TIMEOUT_SECONDS = 120
RESTART_AFTER_UPDATE = object()

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
        return "📝 Draft"
    decision = pr.get("reviewDecision") or ""
    return {
        "APPROVED": "✅ Approved",
        "CHANGES_REQUESTED": "❌ Changes Req.",
        "REVIEW_REQUIRED": "⏳ Review Needed",
    }.get(decision, "➖ No Reviews")


def format_pr_status(pr: dict) -> Text:
    commits = pr.get("commits", {}).get("nodes", [])
    rollup = commits[-1]["commit"].get("statusCheckRollup") if commits else None
    color = {
        "SUCCESS": "green",
        "FAILURE": "red",
        "ERROR": "red",
    }.get((rollup or {}).get("state"), "")
    return Text.assemble(
        ("●" if color else " ", color),
        f" {format_review_status(pr)}",
    )


def pr_priority(pr: dict) -> int:
    """Rank for the priority sort; lower values are shown first."""
    if pr.get("isDraft"):
        return 4
    decision = pr.get("reviewDecision")
    if decision == "APPROVED":
        return 3
    if decision == "CHANGES_REQUESTED":
        return 2
    return 1 if reviewer_names(pr) else 0


def order_prs(prs: list[dict], sort: str) -> list[dict]:
    """Return PRs in the configured display order."""
    if sort == "priority":
        return sorted(prs, key=lambda pr: (pr_priority(pr), pr.get("createdAt", "")))
    if sort == "updated":
        return sorted(prs, key=lambda pr: pr.get("updatedAt", ""), reverse=True)
    return list(prs)


def format_labels(labels: list[dict]) -> str:
    if not labels:
        return ""
    names = [lbl["name"] for lbl in labels[:3]]
    result = ", ".join(names)
    if len(labels) > 3:
        result += f" +{len(labels) - 3}"
    return result


def reviewer_names(pr: dict) -> set[str]:
    """Requested users/teams and submitted reviewers, excluding Copilot."""
    names: set[str] = set()
    for request in pr.get("reviewRequests", {}).get("nodes", []):
        reviewer = request.get("requestedReviewer") or {}
        if reviewer.get("login"):
            names.add(reviewer["login"])
        elif reviewer.get("slug"):
            names.add(f"team:{reviewer['slug']}")
    for review in pr.get("latestReviews", {}).get("nodes", []):
        author = review.get("author") or {}
        if review.get("state") != "PENDING" and author.get("login"):
            names.add(author["login"])
    return {name for name in names if name.casefold() not in _COPILOT_REVIEWER_LOGINS}


def format_reviewers(pr: dict) -> str:
    """Combine requested users/teams and submitted reviewers without duplicates."""
    return ", ".join(sorted(reviewer_names(pr), key=str.casefold)) or "—"


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


def _run_git(repo_root: Path, *args: str) -> str:
    command = ["git", *args]
    environment = dict(os.environ)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    try:
        result = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=_UPDATE_TIMEOUT_SECONDS,
            env=environment,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Git is not installed or not available on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Timed out while checking for updates.") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(f"{' '.join(command)} failed: {detail}")
    return result.stdout.strip()


def pull_updates(repo_root: Path) -> bool:
    """Fast-forward the current branch and report whether HEAD changed."""
    before = _run_git(repo_root, "rev-parse", "HEAD")
    if _run_git(repo_root, "status", "--porcelain"):
        raise RuntimeError(
            "Cannot update while the repository has uncommitted changes."
        )
    _run_git(repo_root, "pull", "--ff-only")
    after = _run_git(repo_root, "rev-parse", "HEAD")
    return before != after


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

    class CellHovered(Message):
        def __init__(self, coordinate: Coordinate | None) -> None:
            super().__init__()
            self.coordinate = coordinate

    def on_mouse_move(self, event: MouseMove) -> None:
        meta = event.style.meta
        coordinate = Coordinate(meta.get("row", -1), meta.get("column", -1))
        if meta.get("out_of_bounds") or not self.is_valid_coordinate(coordinate):
            self.post_message(self.CellHovered(None))
        else:
            self.post_message(self.CellHovered(coordinate))

    def on_leave(self) -> None:
        self.post_message(self.CellHovered(None))

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
        Binding("u", "check_update", "Check for updates"),
        Binding("enter", "open_pr", "Open in browser"),
        Binding("i", "ignore", "Ignore"),
        Binding("i", "restore", "Restore PR"),
        Binding("ctrl+c", "quit", "Quit", show=False),
    ]

    def __init__(self, config: Config) -> None:
        super().__init__()
        self.config = config
        self._prs: list[dict] = []
        self._visible_prs: list[dict] = []
        self._show_ignored: bool = False
        # Expired entries are pruned (and saved) by the first _rebuild_table().
        self._ignores, self._ignore_warning = load_ignores(config.state_file)
        self._known_prs: set[tuple[str, int]] = set()
        self._comment_counts: dict[tuple[str, int], int] = {}
        self._unread_comment_updates: set[tuple[str, int]] = set()
        self._loading: bool = False
        self._error: Optional[str] = None
        self._failed_repo_count: int = 0
        self._last_updated: Optional[datetime] = None
        self._next_refresh: Optional[datetime] = None
        self._rate_limit_until: Optional[datetime] = None
        self._rate_limit_attempts: int = 0
        self._col_keys: dict[str, object] = {}  # populated in on_mount
        self._last_opened_url: Optional[str] = None
        self._last_opened_at: Optional[float] = None
        self._checking_update: bool = False

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
            "reviewers": 9,
            "review": 8,
            "age": 3,
            "updated": 11,
            "comments": 8,
            "labels": 0,
        }
        maxes = {
            "number": 7,
            "repo": 30,
            "title": 999,
            "author": 20,
            "reviewers": 30,
            "review": 18,
            "age": 6,
            "updated": 11,
            "comments": 8,
            "labels": 40,
        }

        w = dict(mins)
        for pr in self._visible_prs:
            w["number"] = max(w["number"], len(f"#{pr['number']}"))
            w["repo"]   = max(w["repo"],   len(repo_short_name(pr.get("repository", ""))))
            w["title"]  = max(w["title"],  len(pr.get("title", "")))
            w["author"] = max(w["author"], len(pr.get("author", {}).get("login", "")))
            w["reviewers"] = max(w["reviewers"], len(format_reviewers(pr)))
            w["review"] = max(w["review"], cell_len(format_pr_status(pr).plain))
            w["age"]    = max(w["age"],    len(self._age_display(pr)))
            w["updated"] = max(w["updated"], len(format_age(pr.get("updatedAt", ""))))
            w["comments"] = max(w["comments"], len(str(comment_count(pr))) + 1)
            w["labels"] = max(w["labels"], len(format_labels(pr.get("labels", []))))

        # Apply caps to all columns
        for k in mins:
            w[k] = max(mins[k], min(w[k], maxes[k]))

        # DataTable.Column.get_render_width() adds 2*cell_padding (default=1) to every
        # column's width; derive the overhead from the number of columns.
        NUM_COLS = len(mins)
        DEFAULT_CELL_PADDING = 1
        flexible_keys = ("title", "labels", "reviewers")
        shrinkable_fixed_keys = ("repo", "author", "review")
        fixed_keys = ("number", "repo", "author", "review", "age", "updated", "comments")
        fixed = sum(w[k] for k in fixed_keys)
        width = terminal_width if terminal_width is not None else self.size.width
        flexible_min = sum(mins[k] for k in flexible_keys)
        minimum_fixed = sum(
            mins[k] if k in shrinkable_fixed_keys else w[k] for k in fixed_keys
        )
        minimum_content = minimum_fixed + flexible_min
        default_separators = NUM_COLS * 2 * DEFAULT_CELL_PADDING
        cell_padding = (
            0 if width < minimum_content + default_separators else DEFAULT_CELL_PADDING
        )
        separators = NUM_COLS * 2 * cell_padding
        content_width = max(minimum_content, width - separators)

        # Repository, author, and review cells can be clipped with tooltip support.
        overflow = max(0, fixed + flexible_min - content_width)
        for key in shrinkable_fixed_keys:
            reduction = min(overflow, w[key] - mins[key])
            w[key] -= reduction
            overflow -= reduction
            if overflow == 0:
                break

        fixed = sum(w[k] for k in fixed_keys)
        remaining = max(flexible_min, content_width - fixed)
        w["reviewers"] = min(w["reviewers"], remaining - mins["title"] - mins["labels"])
        remaining -= w["reviewers"]

        # Title takes only what its content needs; labels gets whatever is left (up to its cap)
        title_w  = max(mins["title"],  min(w["title"],  remaining - mins["labels"]))
        labels_w = max(mins["labels"], min(remaining - title_w, maxes["labels"]))
        # If labels hit its cap and space remains, let title grow back into it
        title_w  = max(title_w, remaining - labels_w)

        w["title"]  = title_w
        w["labels"] = labels_w
        return w

    def _apply_col_widths(
        self,
        widths: dict[str, int],
        terminal_width: int | None = None,
    ) -> None:
        if not self._col_keys:
            return
        table = self.query_one("#pr-table", DataTable)
        width = terminal_width if terminal_width is not None else self.size.width
        default_render_width = sum(widths.values()) + len(widths) * 2
        table.cell_padding = 0 if default_render_width > width else 1
        for key, col_key in self._col_keys.items():
            if key in widths:
                table.columns[col_key].width = widths[key]
        table._require_update_dimensions = True
        table.check_idle()

    def _update_title_col_width(self, terminal_width: int | None = None) -> None:
        """On terminal resize: recompute all widths (keeps content-fit, adjusts title)."""
        if not self._col_keys:
            return
        self._apply_col_widths(
            self._compute_col_widths(terminal_width=terminal_width),
            terminal_width=terminal_width,
        )

    def on_resize(self, event: Resize) -> None:
        self._update_title_col_width(terminal_width=event.size.width)

    # ------------------------------------------------------------------
    # Compose + mount
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Static(self._header_text(), id="app-header")
        yield LoadingIndicator(id="loading")
        yield Static("", id="error-panel")
        yield PRTable(
            id="pr-table",
            cursor_type="row",
            cursor_foreground_priority="renderable",
            zebra_stripes=True,
            show_row_labels=False,
        )
        yield Static("Starting…", id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#pr-table", DataTable)
        self._col_keys["number"] = table.add_column("#",             width=7)
        self._col_keys["repo"]   = table.add_column("Repository",   width=22)
        self._col_keys["title"]  = table.add_column("Title",        width=48)
        self._col_keys["author"] = table.add_column("Author",       width=15)
        self._col_keys["reviewers"] = table.add_column("Reviewers", width=30)
        self._col_keys["review"] = table.add_column("Review Status",width=18)
        self._col_keys["age"]    = table.add_column("Age",          width=5)
        self._col_keys["updated"] = table.add_column("Last change", width=11)
        self._col_keys["comments"] = table.add_column("Comments",   width=8)
        self._col_keys["labels"] = table.add_column("Labels",       width=22)

        # Set initial title column width based on actual terminal size
        self._update_title_col_width()

        # Start in loading state
        self._set_view("loading")
        self._start_fetch()

        # 1-second tick for countdown updates and auto-refresh trigger
        self.set_interval(1, self._tick)

        if self._ignore_warning:
            self.notify(self._ignore_warning, title="PR Watcher", severity="warning")

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
        if self._loading:
            return
        if self._rate_limit_until and datetime.now(timezone.utc) < self._rate_limit_until:
            self._update_status_bar()
            return
        self._rate_limit_until = None
        self._next_refresh = None
        self._loading = True
        # Keep existing table visible during background refresh (if we have data)
        if not self._prs:
            self._set_view("loading")
        self._update_status_bar()
        self._fetch_prs()

    # ------------------------------------------------------------------
    # Worker — runs gh calls in a thread pool
    # ------------------------------------------------------------------

    @work(thread=True, exclusive=True, exit_on_error=False, name="fetch-prs")
    def _fetch_prs(self) -> tuple[list[dict], int]:
        failed_repos: list[str] = []
        prs = github.fetch_all_team_prs(
            self.config.org,
            self.config.team_slug,
            list(self._prs),
            failed_repos,
            self.config.ignore_repos,
        )
        return prs, len(failed_repos)

    @work(
        thread=True,
        group="updates",
        exclusive=True,
        exit_on_error=False,
        name="check-update",
    )
    def _pull_updates(self) -> bool:
        return pull_updates(SETTINGS_FILE.parent)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.worker.name == "check-update":
            if event.state == WorkerState.SUCCESS:
                self._checking_update = False
                if event.worker.result:
                    self.notify(
                        "Update installed. Restarting…",
                        title="PR Watcher",
                        timeout=2,
                    )
                    self.exit(result=RESTART_AFTER_UPDATE)
                else:
                    self.notify(
                        "Already up to date.",
                        title="PR Watcher",
                        timeout=2,
                    )
            elif event.state == WorkerState.ERROR:
                self._checking_update = False
                self.notify(
                    f"Update failed: {event.worker.error}",
                    title="PR Watcher",
                    severity="error",
                    timeout=8,
                )
            elif event.state == WorkerState.CANCELLED:
                self._checking_update = False
            return

        if event.worker.name != "fetch-prs":
            return

        if event.state == WorkerState.SUCCESS:
            new_prs, failed_repo_count = event.worker.result
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
            self._prs = order_prs(new_prs, self.config.sort)
            self._error = None
            self._rate_limit_attempts = 0
            self._failed_repo_count = failed_repo_count
            self._loading = False
            self._last_updated = datetime.now()
            self._next_refresh = datetime.now(timezone.utc) + timedelta(
                seconds=self.config.refresh_interval
            )
            self._rebuild_table()
            self._set_view("table")
            self._update_status_bar()

        elif event.state == WorkerState.ERROR:
            self._error = str(event.worker.error)
            self._loading = False
            if isinstance(event.worker.error, github.RateLimitError):
                self._rate_limit_attempts += 1
                retry_at = event.worker.error.retry_at
                now = datetime.now(timezone.utc)
                if retry_at is not None:
                    retry_at = retry_at.astimezone(timezone.utc)
                if retry_at is None or retry_at <= now:
                    delay = min(3600, 60 * 2 ** min(self._rate_limit_attempts - 1, 6))
                    retry_at = now + timedelta(seconds=delay)
                self._rate_limit_until = retry_at + timedelta(seconds=1)
                self._next_refresh = self._rate_limit_until
                if self._prs:
                    self._set_view("table")
                else:
                    self.query_one("#error-panel", Static).update(
                        "GitHub API rate limited.\n\n"
                        "Refresh is paused and will resume automatically."
                    )
                    self._set_view("error")
                self._update_status_bar()
                return
            self._next_refresh = datetime.now(timezone.utc) + timedelta(
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

    def _is_ignored(self, pr: dict) -> bool:
        return self._row_key(pr) in self._ignores

    def _age_display(self, pr: dict) -> str:
        if self._show_ignored and self._is_ignored(pr):
            return self._ignores[self._row_key(pr)].astimezone().strftime("%d %b")
        return format_age(pr.get("createdAt", ""))

    def _prune_ignores(self) -> None:
        pruned = prune_expired(self._ignores, datetime.now(timezone.utc))
        if len(pruned) != len(self._ignores):
            self._ignores = pruned
            self._save_ignores()

    def _save_ignores(self) -> None:
        try:
            save_ignores(self.config.state_file, self._ignores)
        except OSError as exc:
            self.notify(
                f"Could not save ignored PRs: {exc}",
                title="PR Watcher",
                severity="error",
            )

    def _rebuild_table(self, keep_cursor: bool = True) -> None:
        """Re-render visible rows, keeping the cursor on the same PR when possible.

        DataTable.clear() resets the cursor to row 0; without restoring it, a
        refresh would make `i`/Enter act on whichever PR happens to be first.
        If the PR under the cursor is gone (e.g. just ignored), the cursor stays
        at the same index so it lands on the next row.
        """
        table = self.query_one("#pr-table", DataTable)
        old_row = table.cursor_row
        old_key = (
            self._row_key(self._visible_prs[old_row])
            if keep_cursor and 0 <= old_row < len(self._visible_prs)
            else None
        )
        self._prune_ignores()
        self._visible_prs = [
            pr for pr in self._prs if self._is_ignored(pr) == self._show_ignored
        ]
        col_widths = self._compute_col_widths()
        table.tooltip = None
        table.clear()
        table.columns[self._col_keys["age"]].label = Text(
            "Until" if self._show_ignored else "Age"
        )
        for pr in self._visible_prs:
            table.add_row(
                f"#{pr['number']}",
                ellipsis_middle(repo_short_name(pr.get("repository", "")), col_widths["repo"]),
                pr.get("title", ""),
                pr.get("author", {}).get("login", ""),
                format_reviewers(pr),
                format_pr_status(pr),
                self._age_display(pr),
                format_age(pr.get("updatedAt", "")),
                self._comment_display(pr),
                format_labels(pr.get("labels", [])),
                key=self._row_key(pr),
            )
        self._apply_col_widths(col_widths)
        if keep_cursor and table.row_count:
            new_row = next(
                (i for i, pr in enumerate(self._visible_prs) if self._row_key(pr) == old_key),
                min(max(old_row, 0), table.row_count - 1),
            )
            table.move_cursor(row=new_row)

    # ------------------------------------------------------------------
    # Status bar
    # ------------------------------------------------------------------

    def _update_status_bar(self) -> None:
        parts: list[str] = []

        if self._rate_limit_until:
            secs = max(
                0, int((self._rate_limit_until - datetime.now(timezone.utc)).total_seconds())
            )
            parts.append(
                f"Rate limited - refresh paused until "
                f"{self._rate_limit_until.astimezone():%H:%M:%S} ({secs}s)"
            )

        if self._show_ignored:
            n = len(self._visible_prs)
            parts.append(
                f"[@click=app.toggle_ignored_view]🙈 {n} ignored PR{'s' if n != 1 else ''}"
                " (click to go back)[/]"
            )
        elif not self._loading:
            n = len(self._visible_prs)
            icon = "📋" if n else "📭"
            parts.append(f"{icon} {n} open PR{'s' if n != 1 else ''}")
            ignored = sum(1 for pr in self._prs if self._is_ignored(pr))
            if ignored:
                parts.append(f"[@click=app.toggle_ignored_view]🙈 {ignored} ignored[/]")

        if self._loading:
            parts.append("⟳ Refreshing…")

        if self._last_updated:
            parts.append(f"Updated {self._last_updated.strftime('%H:%M:%S')}")

        if not self._loading and self._next_refresh and not self._rate_limit_until:
            secs = max(
                0, int((self._next_refresh - datetime.now(timezone.utc)).total_seconds())
            )
            parts.append(f"Refresh in {secs}s")

        if self._rate_limit_until:
            if self._prs:
                parts.append("Showing stale data")
        elif self._error:
            error_lc = self._error.lower()
            if any(x in error_lc for x in _CONNECTIVITY_MARKERS):
                parts.append("⚠ No internet connection — retrying")
            elif self._prs:
                parts.append("⚠ Refresh error — showing stale data")
        elif self._failed_repo_count:
            suffix = "repo stale" if self._failed_repo_count == 1 else "repos stale"
            parts.append(f"⚠ {self._failed_repo_count} {suffix}")

        self.query_one("#status-bar", Static).update("  │  ".join(parts))

    # ------------------------------------------------------------------
    # Timer tick — countdown + auto-refresh trigger
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        # Textual unmounts widgets before stopping app timers.
        if not self.is_running:
            return
        self._update_status_bar()
        if (
            not self._loading
            and self._next_refresh is not None
            and datetime.now(timezone.utc) >= self._next_refresh
        ):
            self._next_refresh = None
            self._start_fetch()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def on_prtable_cell_hovered(self, event: PRTable.CellHovered) -> None:
        table = self.query_one("#pr-table", PRTable)
        coordinate = event.coordinate
        if coordinate is None or not table.is_valid_coordinate(coordinate):
            table.tooltip = None
            return
        row_key, column_key = table.coordinate_to_cell_key(coordinate)
        pr = self._pr_for_row_key(row_key)
        if pr is None:
            table.tooltip = None
            return
        displayed = str(table.get_cell_at(coordinate))
        full_text = displayed
        if column_key == self._col_keys["repo"]:
            full_text = repo_short_name(pr.get("repository", ""))
        elif column_key == self._col_keys["labels"]:
            full_text = ", ".join(label["name"] for label in pr.get("labels", []))
        clipped = full_text != displayed or cell_len(full_text) > table.columns[column_key].width
        table.tooltip = Text(full_text) if clipped else None

    def action_refresh(self) -> None:
        if not self._loading:
            self._start_fetch()

    def action_check_update(self) -> None:
        if self._checking_update:
            return
        self._checking_update = True
        self.notify(
            "Checking for updates…",
            title="PR Watcher",
            timeout=2,
        )
        self._pull_updates()

    _OPEN_PR_DEBOUNCE_SECONDS = 1.0

    def _open_pr(self, pr: dict) -> None:
        """Open a PR's URL in the browser, debouncing rapid duplicate triggers.

        A double-click (or a click quickly followed by Enter) can fire this
        twice for the same PR in quick succession, opening two browser tabs.
        Suppress repeat opens of the same URL within a short window.
        """
        url = pr.get("url", "")
        if not url:
            return
        now = monotonic()
        if (
            url == self._last_opened_url
            and self._last_opened_at is not None
            and now - self._last_opened_at < self._OPEN_PR_DEBOUNCE_SECONDS
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
        if table.row_count == 0 or not self._visible_prs:
            return
        row_idx = table.cursor_row
        if 0 <= row_idx < len(self._visible_prs):
            pr = self._visible_prs[row_idx]
            self._acknowledge_comment_update(pr)
            self._open_pr(pr)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Both actions share the `i` key; only the one matching the view is active,
        # which also makes the Footer show "Ignore" or "Restore PR".
        if action == "ignore":
            return not self._show_ignored
        if action == "restore":
            return self._show_ignored
        return True

    def action_ignore(self) -> None:
        self._toggle_ignore()

    def action_restore(self) -> None:
        self._toggle_ignore()

    def _toggle_ignore(self) -> None:
        table = self.query_one("#pr-table", DataTable)
        row_idx = table.cursor_row
        if table.row_count == 0 or not 0 <= row_idx < len(self._visible_prs):
            return
        pr = self._visible_prs[row_idx]
        key = self._row_key(pr)
        if key in self._ignores:
            del self._ignores[key]
            message = f"Restored PR #{pr['number']}"
        else:
            until = datetime.now(timezone.utc) + timedelta(days=self.config.ignore_days)
            self._ignores[key] = until
            message = (
                f"Ignored PR #{pr['number']} until "
                f"{until.astimezone().strftime('%d %b')}"
            )
        self._save_ignores()
        self._rebuild_table()
        self._update_status_bar()
        self.notify(message, title="PR Watcher", timeout=2)

    def action_toggle_ignored_view(self) -> None:
        self._show_ignored = not self._show_ignored
        self._rebuild_table(keep_cursor=False)
        self._update_status_bar()
        self.refresh_bindings()
