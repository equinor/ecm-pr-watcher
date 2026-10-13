"""Configuration dataclass and CLI argument parsing."""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

SETTINGS_FILE = Path(__file__).resolve().parent.parent / "pr-watcher.json"
STATE_FILE = SETTINGS_FILE.with_name("pr-watcher-state.json")
_SETTING_TYPES: dict[str, type] = {"sort": str, "ignore_days": int}
_SETTING_CHOICES: dict[str, tuple[str, ...]] = {"sort": ("created", "priority")}
_SETTING_MINIMUMS: dict[str, int] = {"ignore_days": 1}
_SETTING_MAXIMUMS: dict[str, int] = {"ignore_days": 3650}


@dataclass
class Config:
    org: str
    team: str = "ECM WO Preparation"
    team_slug: str = ""
    refresh_interval: int = 60
    bell: bool = True
    sort: str = "created"
    ignore_days: int = 14
    state_file: Path | None = None

    def __post_init__(self) -> None:
        if not self.team_slug:
            self.team_slug = to_slug(self.team)
        if self.state_file is None:
            self.state_file = STATE_FILE


def to_slug(name: str) -> str:
    """Convert a team display name to a GitHub team slug."""
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def load_settings(path: Path = SETTINGS_FILE) -> dict:
    """Read optional local settings; a missing file means defaults."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a JSON object")
    for key, value in data.items():
        expected = _SETTING_TYPES.get(key)
        if expected is None:
            allowed = ", ".join(sorted(_SETTING_TYPES))
            raise ValueError(f"{path}: unknown setting '{key}' (allowed: {allowed})")
        # bool is a subclass of int, so `true` would otherwise pass as 1.
        if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
            raise ValueError(f"{path}: '{key}' must be {expected.__name__}")
        choices = _SETTING_CHOICES.get(key)
        if choices and value not in choices:
            raise ValueError(f"{path}: '{key}' must be one of: {', '.join(choices)}")
        minimum = _SETTING_MINIMUMS.get(key)
        if minimum is not None and value < minimum:
            raise ValueError(f"{path}: '{key}' must be at least {minimum}")
        maximum = _SETTING_MAXIMUMS.get(key)
        if maximum is not None and value > maximum:
            raise ValueError(f"{path}: '{key}' must be at most {maximum}")
    return data


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        prog="pr-watcher",
        description="TUI for monitoring GitHub team Pull Requests",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py --org my-company\n"
            '  python main.py --org my-company --team "ECM WO Preparation" --interval 30\n'
            "  python main.py --org my-company --team-slug ecm-wo-preparation\n"
        ),
    )
    parser.add_argument(
        "--org",
        required=True,
        metavar="ORG",
        help="GitHub organization name (required)",
    )
    parser.add_argument(
        "--team",
        default="ECM WO Preparation",
        metavar="NAME",
        help='GitHub team display name (default: "ECM WO Preparation")',
    )
    parser.add_argument(
        "--team-slug",
        default="",
        dest="team_slug",
        metavar="SLUG",
        help="GitHub team slug — auto-derived from --team if not provided",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        dest="refresh_interval",
        metavar="SECONDS",
        help="Auto-refresh interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--bell",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Ring terminal bell when new PRs appear (default: on). Use --no-bell to disable.",
    )

    args = parser.parse_args()
    try:
        settings = load_settings()
    except ValueError as exc:
        parser.error(str(exc))
    return Config(
        org=args.org,
        team=args.team,
        team_slug=args.team_slug,
        refresh_interval=args.refresh_interval,
        bell=args.bell,
        **settings,
    )
