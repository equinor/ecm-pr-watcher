"""Tests for pr_watcher/config.py."""
from __future__ import annotations

import pytest

from pr_watcher.config import Config, to_slug


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
