from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_state_file(tmp_path, monkeypatch):
    """Keep tests from reading or writing the developer's real ignore state."""
    path = tmp_path / "pr-watcher-state.json"
    monkeypatch.setattr("pr_watcher.config.STATE_FILE", path)
    return path
