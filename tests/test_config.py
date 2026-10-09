"""Tests for pr_watcher/config.py."""
from __future__ import annotations

import json

import pytest

from pr_watcher import config as config_module
from pr_watcher.config import Config, load_settings, parse_args, to_slug


class TestParseArgs:
    def test_loaded_settings_reach_config(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["pr-watcher", "--org", "Equinor"])
        monkeypatch.setattr(config_module, "load_settings", lambda: {"sort": "priority"})
        assert parse_args().sort == "priority"

    def test_invalid_settings_stop_startup(self, monkeypatch, capsys):
        def broken() -> dict:
            raise ValueError("pr-watcher.json: unknown setting 'x'")

        monkeypatch.setattr("sys.argv", ["pr-watcher", "--org", "Equinor"])
        monkeypatch.setattr(config_module, "load_settings", broken)
        with pytest.raises(SystemExit) as exc:
            parse_args()
        assert exc.value.code == 2
        assert "unknown setting 'x'" in capsys.readouterr().err


class TestToSlug:
    def test_spaces_become_hyphens(self):
        assert to_slug("ECM WO Preparation") == "ecm-wo-preparation"

    def test_already_lowercase(self):
        assert to_slug("my-team") == "my-team"

    def test_special_chars_become_hyphens(self):
        assert to_slug("Team (Alpha)") == "team-alpha"

    def test_leading_trailing_hyphens_stripped(self):
        assert to_slug(" team ") == "team"

    def test_multiple_spaces_single_hyphen(self):
        assert to_slug("ECM  WO   Prep") == "ecm-wo-prep"


class TestConfig:
    def test_slug_auto_derived_from_team(self):
        cfg = Config(org="Equinor", team="ECM WO Preparation")
        assert cfg.team_slug == "ecm-wo-preparation"

    def test_explicit_slug_not_overridden(self):
        cfg = Config(org="Equinor", team="ECM WO Preparation", team_slug="custom-slug")
        assert cfg.team_slug == "custom-slug"

    def test_defaults(self):
        cfg = Config(org="Equinor")
        assert cfg.team == "ECM WO Preparation"
        assert cfg.refresh_interval == 60
        assert cfg.bell is True
        assert cfg.sort == "created"


class TestLoadSettings:
    def test_missing_file_returns_defaults(self, tmp_path):
        assert load_settings(tmp_path / "pr-watcher.json") == {}

    @pytest.mark.parametrize("sort", ["created", "priority"])
    def test_reads_sort(self, tmp_path, sort):
        path = tmp_path / "pr-watcher.json"
        path.write_text(json.dumps({"sort": sort}), encoding="utf-8")
        assert load_settings(path) == {"sort": sort}

    def test_accepts_utf8_bom(self, tmp_path):
        path = tmp_path / "pr-watcher.json"
        path.write_text('{"sort": "priority"}', encoding="utf-8-sig")
        assert load_settings(path) == {"sort": "priority"}

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            ("{not json", "invalid JSON"),
            ("[]", "expected a JSON object"),
            ('{"approved_last": true}', "unknown setting"),
            ('{"sort": 1}', "must be str"),
            ('{"sort": "oldest"}', "must be one of: created, priority"),
        ],
    )
    def test_invalid_content_raises(self, tmp_path, content, message):
        path = tmp_path / "pr-watcher.json"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            load_settings(path)

    def test_example_file_is_valid(self):
        from pr_watcher.config import SETTINGS_FILE

        example = SETTINGS_FILE.with_name("pr-watcher.example.json")
        assert load_settings(example) == {"sort": "priority"}
