"""Configuration dataclass and CLI argument parsing."""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field


@dataclass
class Config:
    org: str
    team: str = "ECM WO Preparation"
    team_slug: str = ""
    refresh_interval: int = 60
    bell: bool = True

    def __post_init__(self) -> None:
        if not self.team_slug:
            self.team_slug = to_slug(self.team)


def to_slug(name: str) -> str:
    """Convert a team display name to a GitHub team slug."""
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


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
    return Config(
        org=args.org,
        team=args.team,
        team_slug=args.team_slug,
        refresh_interval=args.refresh_interval,
        bell=args.bell,
    )
