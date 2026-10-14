"""Tests for process restart behavior in pr_watcher.__main__."""
from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

from pr_watcher.__main__ import _restart_args, main
from pr_watcher.app import RESTART_AFTER_UPDATE


def test_restart_args_preserve_module_invocation(monkeypatch):
    monkeypatch.setattr(
        sys,
        "orig_argv",
        [sys.executable, "-m", "pr_watcher", "--org", "Equinor"],
    )
    assert _restart_args() == [
        sys.executable,
        "-m",
        "pr_watcher",
        "--org",
        "Equinor",
    ]


def test_main_reexecs_after_update(monkeypatch):
    config = object()
    app = MagicMock()
    app.run.return_value = RESTART_AFTER_UPDATE
    monkeypatch.setattr(
        sys,
        "orig_argv",
        [sys.executable, "main.py", "--org", "Equinor"],
    )

    with (
        patch("pr_watcher.__main__.parse_args", return_value=config),
        patch("pr_watcher.__main__.PRWatcherApp", return_value=app),
        patch("pr_watcher.__main__.os.execv") as execv,
    ):
        main()

    execv.assert_called_once_with(
        sys.executable,
        [sys.executable, "main.py", "--org", "Equinor"],
    )


def test_main_does_not_reexec_after_normal_exit():
    app = MagicMock()
    app.run.return_value = None

    with (
        patch("pr_watcher.__main__.parse_args", return_value=object()),
        patch("pr_watcher.__main__.PRWatcherApp", return_value=app),
        patch("pr_watcher.__main__.os.execv") as execv,
    ):
        main()

    execv.assert_not_called()
