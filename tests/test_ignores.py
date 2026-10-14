"""Tests for pr_watcher/ignores.py."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from pr_watcher.ignores import load_ignores, prune_expired, save_ignores

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


class TestLoadIgnores:
    def test_missing_file_is_empty_without_warning(self, tmp_path):
        assert load_ignores(tmp_path / "state.json") == ({}, None)

    def test_round_trip(self, tmp_path):
        path = tmp_path / "state.json"
        ignores = {
            "Equinor/b#2": NOW + timedelta(days=14),
            "Equinor/a#1": NOW + timedelta(days=1),
        }
        save_ignores(path, ignores)
        assert load_ignores(path) == (ignores, None)

    def test_saved_file_is_sorted_utf8_without_bom(self, tmp_path):
        path = tmp_path / "state.json"
        save_ignores(path, {"Equinor/b#2": NOW, "Equinor/a#1": NOW})
        raw = path.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")
        data = json.loads(raw)
        assert list(data["ignored"]) == ["Equinor/a#1", "Equinor/b#2"]
        assert data["ignored"]["Equinor/a#1"] == "2026-10-09T12:00:00+00:00"

    def test_save_leaves_no_temp_files(self, tmp_path):
        path = tmp_path / "state.json"
        save_ignores(path, {"Equinor/a#1": NOW})
        assert [p.name for p in tmp_path.iterdir()] == ["state.json"]

    def test_naive_timestamp_is_treated_as_utc(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text('{"ignored": {"Equinor/a#1": "2026-10-09T12:00:00"}}', encoding="utf-8")
        assert load_ignores(path) == ({"Equinor/a#1": NOW}, None)

    def test_z_suffix_is_accepted(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text('{"ignored": {"Equinor/a#1": "2026-10-09T12:00:00Z"}}', encoding="utf-8")
        assert load_ignores(path) == ({"Equinor/a#1": NOW}, None)

    def test_invalid_entries_are_skipped_individually(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(
            json.dumps({"ignored": {
                "Equinor/good#1": "2026-10-09T12:00:00+00:00",
                "Equinor/number#2": 5,
                "Equinor/text#3": "not a date",
            }}),
            encoding="utf-8",
        )
        ignores, warning = load_ignores(path)
        assert ignores == {"Equinor/good#1": NOW}
        assert warning and "Equinor/number#2" in warning and "Equinor/text#3" in warning

    @pytest.mark.parametrize(
        "content",
        [
            "{not json",
            "[]",
            "{}",
            '{"ignored": []}',
        ],
    )
    def test_corrupt_file_is_empty_with_warning(self, tmp_path, content):
        path = tmp_path / "state.json"
        path.write_text(content, encoding="utf-8")
        ignores, warning = load_ignores(path)
        assert ignores == {}
        assert warning and "state.json" in warning


class TestPruneExpired:
    def test_drops_expired_and_keeps_future(self):
        ignores = {
            "expired": NOW - timedelta(seconds=1),
            "boundary": NOW,
            "active": NOW + timedelta(seconds=1),
        }
        assert prune_expired(ignores, NOW) == {"active": NOW + timedelta(seconds=1)}
