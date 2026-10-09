"""Configuration dataclass and CLI argument parsing."""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

SETTINGS_FILE = Path(__file__).resolve().parent.parent / "pr-watcher.json"
_SETTING_TYPES: dict[str, type] = {
    "org": str,
    "team": str,
    "team_slug": str,
    "interval": int,
    "bell": bool,
    "sort": str,
}
_SETTING_CHOICES: dict[str, tuple[str, ...]] = {"sort": ("created", "priority")}


@dataclass
class Config:
    org: str
    team: str = "ECM WO Preparation"
    team_slug: str = ""
    refresh_interval: int = 60
    bell: bool = True
    sort: str = "created"

    def __post_init__(self) -> None:
        if not self.team_slug:
            self.team_slug = to_slug(self.team)


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
        if type(value) is not expected:
            raise ValueError(f"{path}: '{key}' must be {expected.__name__}")
        choices = _SETTING_CHOICES.get(key)
        if choices and value not in choices:
            raise ValueError(f"{path}: '{key}' must be one of: {', '.join(choices)}")
    return data


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        prog="pr-watcher",
        description="TUI for monitoring GitHub team Pull Requests",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        argument_default=argparse.SUPPRESS,
        epilog=(
            "Examples:\n"
            "  python main.py --org my-company\n"
            '  python main.py --org my-company --team "ECM WO Preparation" --interval 30\n'
            "  python main.py --org my-company --team-slug ecm-wo-preparation\n"
        ),
    )
    parser.add_argument(
        "--org",
        metavar="ORG",
        help="GitHub organization name (required unless set in pr-watcher.json)",
    )
    parser.add_argument(
        "--team",
        metavar="NAME",
        help='GitHub team display name (default: "ECM WO Preparation")',
    )
    parser.add_argument(
        "--team-slug",
        dest="team_slug",
        metavar="SLUG",
        help="GitHub team slug — auto-derived from --team if not provided",
    )
    parser.add_argument(
        "--interval",
        type=int,
        dest="refresh_interval",
        metavar="SECONDS",
        help="Auto-refresh interval in seconds (default: 60)",
    )
    parser.add_argument(
        "--bell",
        action=argparse.BooleanOptionalAction,
        help="Ring terminal bell when new PRs appear (default: on). Use --no-bell to disable.",
    )

    args = parser.parse_args()
    try:
        settings = load_settings()
    except ValueError as exc:
        parser.error(str(exc))
    if "interval" in settings:
        settings["refresh_interval"] = settings.pop("interval")
    settings.update(vars(args))
    if "org" not in settings:
        parser.error("the following arguments are required: --org (or 'org' in pr-watcher.json)")
    return Config(**settings)
