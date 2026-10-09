"""Tests for pr_watcher/config.py."""
from __future__ import annotations

import json

import pytest

from pr_watcher import config as config_module
from pr_watcher.config import Config, load_settings, parse_args, to_slug


class TestParseArgs:
    @pytest.fixture(autouse=True)
    def default_args_and_settings(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["pr-watcher"])
        monkeypatch.setattr(config_module, "load_settings", lambda: {})

    def test_cli_only_uses_existing_defaults(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["pr-watcher", "--org", "Equinor"])
        assert parse_args() == Config(org="Equinor")

    def test_file_only_configures_all_options(self, monkeypatch):
        monkeypatch.setattr(
            config_module,
            "load_settings",
            lambda: {
                "org": "Equinor",
                "team": "My Team",
                "team_slug": "custom-slug",
                "interval": 30,
                "bell": False,
                "sort": "priority",
            },
        )
        assert parse_args() == Config(
            org="Equinor",
            team="My Team",
            team_slug="custom-slug",
            refresh_interval=30,
            bell=False,
            sort="priority",
        )

    def test_org_only_in_file_uses_existing_defaults(self, monkeypatch):
        monkeypatch.setattr(config_module, "load_settings", lambda: {"org": "Equinor"})
        assert parse_args() == Config(org="Equinor")

    def test_cli_options_override_file_settings(self, monkeypatch):
        monkeypatch.setattr(
            config_module,
            "load_settings",
            lambda: {
                "org": "configured-org",
                "team": "Configured Team",
                "team_slug": "configured-slug",
                "interval": 120,
                "bell": True,
                "sort": "priority",
            },
        )
        monkeypatch.setattr(
            "sys.argv",
            [
                "pr-watcher", "--org", "Equinor", "--team", "My Team",
                "--team-slug", "my-team", "--interval", "30", "--no-bell",
            ],
        )
        assert parse_args() == Config(
            org="Equinor",
            team="My Team",
            team_slug="my-team",
            refresh_interval=30,
            bell=False,
            sort="priority",
        )

    @pytest.mark.parametrize(
        ("configured_bell", "flag", "expected"),
        [(False, "--bell", True), (True, "--no-bell", False)],
    )
    def test_cli_bell_overrides_file(self, monkeypatch, configured_bell, flag, expected):
        monkeypatch.setattr(
            config_module, "load_settings",
            lambda: {"org": "Equinor", "bell": configured_bell},
        )
        monkeypatch.setattr("sys.argv", ["pr-watcher", flag])
        assert parse_args().bell is expected

    def test_cli_team_overrides_file_and_derives_slug(self, monkeypatch):
        monkeypatch.setattr(
            config_module, "load_settings",
            lambda: {"org": "Equinor", "team": "Configured Team"},
        )
        monkeypatch.setattr("sys.argv", ["pr-watcher", "--team", "CLI Team"])
        assert parse_args().team_slug == "cli-team"

    def test_explicit_file_slug_is_preserved_when_cli_overrides_team(self, monkeypatch):
        monkeypatch.setattr(
            config_module, "load_settings",
            lambda: {"org": "Equinor", "team_slug": "custom-slug"},
        )
        monkeypatch.setattr("sys.argv", ["pr-watcher", "--team", "CLI Team"])
        assert parse_args().team_slug == "custom-slug"

    def test_missing_org_stops_startup(self, capsys):
        with pytest.raises(SystemExit) as exc:
            parse_args()
        assert exc.value.code == 2
        assert "--org" in capsys.readouterr().err

    def test_help_does_not_require_org_or_load_settings(self, monkeypatch, capsys):
        def broken() -> dict:
            raise ValueError("invalid settings")

        monkeypatch.setattr("sys.argv", ["pr-watcher", "--help"])
        monkeypatch.setattr(config_module, "load_settings", broken)
        with pytest.raises(SystemExit) as exc:
            parse_args()
        assert exc.value.code == 0
        assert "--org" in capsys.readouterr().out

    def test_example_file_matches_cli_example(self, monkeypatch):
        example = config_module.SETTINGS_FILE.with_name("pr-watcher.example.json")
        monkeypatch.setattr(config_module, "load_settings", lambda: load_settings(example))
        from_file = parse_args()
        monkeypatch.setattr(config_module, "load_settings", lambda: {"sort": "priority"})
        monkeypatch.setattr(
            "sys.argv",
            [
                "pr-watcher", "--org", "Equinor", "--team", "My Team",
                "--interval", "30", "--no-bell",
            ],
        )
        assert from_file == parse_args()

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
        ("key", "value"),
        [
            ("org", "Equinor"),
            ("team", "My Team"),
            ("team_slug", "my-team"),
            ("interval", 30),
            ("bell", True),
            ("bell", False),
        ],
    )
    def test_reads_cli_settings(self, tmp_path, key, value):
        path = tmp_path / "pr-watcher.json"
        settings = {key: value}
        path.write_text(json.dumps(settings), encoding="utf-8")
        assert load_settings(path) == settings

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            ("{not json", "invalid JSON"),
            ("[]", "expected a JSON object"),
            ('{"approved_last": true}', "unknown setting"),
            ('{"sort": 1}', "must be str"),
            ('{"sort": "oldest"}', "must be one of: created, priority"),
            ('{"org": 1}', "must be str"),
            ('{"team": false}', "must be str"),
            ('{"team_slug": []}', "must be str"),
            ('{"interval": "30"}', "must be int"),
            ('{"interval": 30.5}', "must be int"),
            ('{"interval": true}', "must be int"),
            ('{"bell": "false"}', "must be bool"),
            ('{"bell": 0}', "must be bool"),
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
        assert load_settings(example) == {
            "org": "Equinor",
            "team": "My Team",
            "team_slug": "my-team",
            "interval": 30,
            "bell": False,
            "sort": "priority",
        }
