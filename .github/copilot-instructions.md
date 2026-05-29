# GitHub Copilot Instructions

## Project overview

`ecm-pr-watcher` is a Python terminal UI (TUI) app built with [Textual](https://github.com/Textualize/textual) that monitors open Pull Requests across all repositories belonging to a GitHub team. It shells out to the [`gh` CLI](https://cli.github.com/) for all GitHub API access — no separate token management needed.

**Entry point:** `main.py` → `pr_watcher/__main__.py`  
**Python:** 3.10+  
**Key dependency:** `textual>=0.47.0`

## Repository layout

```
pr_watcher/
  app.py       # PRWatcherApp — Textual App subclass, all UI logic
  config.py    # Config dataclass + argparse (--org, --team, --team-slug, --interval, --bell)
  github.py    # gh CLI wrapper: fetch team repos + concurrent PR fetching
main.py        # thin entry point
requirements.txt
```

## Architecture

- **`github.py`** is the only module that calls subprocesses. All GitHub data flows through `fetch_all_team_prs(org, team_slug)`. Do not add `requests`/`httpx` calls or handle tokens here.
- **`app.py`** owns all Textual widgets and state. The `PRWatcherApp` class drives the UI:
  - Background fetching via `@work(thread=True)` worker
  - Auto-refresh via `set_interval(1, self._tick)`
  - `DataTable` row selection opens PRs via `on_data_table_row_selected` (not a key binding — Textual intercepts Enter at the widget level)
  - Title column width is recalculated on every `on_resize` by mutating `column.width` and setting `_require_update_dimensions = True` + `check_idle()`
  - Repository names are truncated with `ellipsis_middle()` to preserve suffixes like `-iac`

## Code conventions

- `from __future__ import annotations` at the top of every module
- Type hints on all function signatures
- Only comment code that needs clarification — avoid noise comments
- Textual CSS lives inline in `APP_CSS` (no external `.css` files)
- Errors from individual repos during fetch are silently swallowed — full refresh errors surface in the status bar, not as crashes

## Git workflow

**All changes must go through a Pull Request. Never commit directly to `main`.**

```bash
git checkout -b <type>/<short-description>   # e.g. feat/bell-config, fix/resize-crash
# make changes
git push -u origin <branch>
gh pr create --fill
```

Branch naming: `feat/`, `fix/`, `docs/`, `chore/`, `refactor/`

## Pull requests

- Use the PR template in `.github/PULL_REQUEST_TEMPLATE.md`
- 1 approving review required; stale reviews are dismissed
- Update the README CLI Flags or Keyboard Shortcuts tables when changing flags or key bindings

## Running locally

```bash
pip install -r requirements.txt
gh auth login          # if not already authenticated
python main.py --org Equinor
python main.py --org Equinor --team "ECM WO Preparation" --interval 30 --no-bell
```
