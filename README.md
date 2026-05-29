# ecm-pr-watcher

A terminal-based (TUI) dashboard for monitoring open Pull Requests across all repositories belonging to a GitHub team. Built with [Textual](https://github.com/Textualize/textual) — runs entirely in your terminal with no browser required.

## Screenshot

```
┌─────────────────────────────────────────────────────────────────────────┐
│  🔍 PR Watcher  |  Org: Equinor  |  Team: ECM WO Preparation  |  Refresh: every 60s  │
├─────────────────────────────────────────────────────────────────────────┤
│  #      Repository             Title                   Author  Review Status   Age  Labels  │
│  ────  ──────────────────────  ──────────────────────  ──────  ──────────────  ───  ──────  │
│ ► #42  ecm-wo-prep             feat: add widget        alice   ✓ Approved       2d          │
│   #41  ecm-api-backend         fix: null check         bob     ⏳ Review Needed  5d  bug    │
│   #38  ecm-react-frontend      chore: update deps      carol   ◌ Draft          1w          │
├─────────────────────────────────────────────────────────────────────────┤
│  📋 3 open PRs  │  Updated 14:23:01  │  Refresh in 45s                  │
├─────────────────────────────────────────────────────────────────────────┤
│  q quit   r refresh   Enter open in browser                             │
└─────────────────────────────────────────────────────────────────────────┘
```

> **Screenshot placeholder** — replace with an actual terminal screenshot once running.

## Prerequisites

- **Python 3.10+**
- **GitHub CLI (`gh`)** — [install here](https://cli.github.com/) and authenticated via `gh auth login`
- Access to the target GitHub organisation and team

## Installation

```bash
# Clone the repository
git clone https://github.com/Equinor/ecm-pr-watcher.git
cd ecm-pr-watcher

# Create and activate a virtual environment (recommended)
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## Usage

```bash
python main.py --org <GITHUB_ORG>
```

### CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--org ORG` | *(required)* | GitHub organisation name |
| `--team NAME` | `"ECM WO Preparation"` | GitHub team display name |
| `--team-slug SLUG` | *(derived from `--team`)* | GitHub team slug — auto-derived if omitted |
| `--interval SECONDS` | `60` | Auto-refresh interval in seconds |
| `--bell` / `--no-bell` | `--bell` (on) | Ring terminal bell when new PRs appear |

### Examples

```bash
# Watch the default ECM WO Preparation team
python main.py --org Equinor

# Use a different team with a 30-second refresh and no bell
python main.py --org Equinor --team "My Team" --interval 30 --no-bell

# Provide the slug directly (skips slug derivation)
python main.py --org Equinor --team-slug ecm-wo-preparation
```

## Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `q` | Quit the application |
| `r` | Force an immediate refresh |
| `Enter` | Open the selected PR in the default browser |
| `↑` / `↓` | Navigate the PR list |
| `Ctrl+C` | Quit the application |

## Configuration

All configuration is passed via CLI flags (see above). No config file is required.

**How the app works:**

1. Uses `gh api` to list all repositories the target team has access to.
2. For each repository, runs `gh pr list` concurrently (up to 8 parallel calls) to fetch open PRs.
3. Refreshes automatically at the configured interval.
4. Rings the terminal bell when PRs appear that weren't present in the previous fetch.

> The app relies entirely on the authenticated `gh` CLI — no personal access tokens or secrets are needed beyond a normal `gh auth login`.

## Contributing

1. Fork the repository and create a feature branch from `main`.
2. Make your changes with tests where applicable.
3. Open a Pull Request — fill in the PR template and request a review from `@Equinor/ecm-wo-preparation`.
4. Ensure all checks pass before merging.

See [PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md) for the full PR checklist.

## Licence

Internal Equinor project. See your organisation's standard licence terms.
