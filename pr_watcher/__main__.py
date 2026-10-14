"""Entry point: python -m pr_watcher"""
from __future__ import annotations

import os
import sys

from .config import parse_args
from .app import PRWatcherApp, RESTART_AFTER_UPDATE


def _restart_args() -> list[str]:
    original = getattr(sys, "orig_argv", None)
    if original:
        return [sys.executable, *original[1:]]
    return [sys.executable, *sys.argv]


def main() -> None:
    config = parse_args()
    result = PRWatcherApp(config).run()
    if result is RESTART_AFTER_UPDATE:
        os.execv(sys.executable, _restart_args())


if __name__ == "__main__":
    main()
