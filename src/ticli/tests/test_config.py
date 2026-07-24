"""Tests for persistent settings and the settings page.

Covers utils/config.py (defaults, validation, atomic write, forward-compat),
the MODE_SETTINGS key handler, and --quality override vs configured default.
No TIDAL session or network needed.
"""

import json
import os

import pytest
from click.testing import CliRunner

from ticli import cli as cli_mod
from ticli import player as player_mod
from ticli.player import HeadlessTidalPlayer
from ticli.utils import config as config_mod
from ticli.utils.config import (
    DEFAULTS,
    SETTINGS_SPEC,
    coerce,
    cycle_value,
    get_spec,
    load_config,
    save_config,
)


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    """Point the config module at a throwaway directory."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", path)
    return path


class TestLoadConfig:
    def test_defaults_when_file_missing(self, config_file):
        cfg = load_config()
        assert cfg["quality"] == "HIGH"
        assert cfg["page_size"] == 15
        assert cfg["progress_bar_width"] == 50

    def test_defaults_when_file_corrupt(self, config_file):
        config_file.write_text("{not json at all")
        cfg = load_config()
        assert cfg["page_size"] == DEFAULTS["page_size"]

    def test_defaults_when_file_is_not_an_object(self, config_file):
        config_file.write_text('["page_size", 30]')
        cfg = load_config()
        assert cfg["page_size"] == DEFAULTS["page_size"]

    def test_partial_file_fills_in_defaults(self, config_file):
        config_file.write_text(json.dumps({"page_size": 20}))
        cfg = load_config()
        assert cfg["page_size"] == 20
        assert cfg["quality"] == DEFAULTS["quality"]

    def test_bad_values_fall_back_to_defaults(self, config_file):
        config_file.write_text(json.dumps({
            "quality": "PLATINUM",
            "page_size": "not a number",
            "progress_bar_width": None,
        }))
        cfg = load_config()
        assert cfg == {**DEFAULTS, "version": config_mod.CONFIG_VERSION}


class TestSaveLoadRoundTrip:
    def test_round_trip(self, config_file):
        save_config({"quality": "HIRES", "page_size": 25, "progress_bar_width": 80})
        cfg = load_config()
        assert cfg["quality"] == "HIRES"
        assert cfg["page_size"] == 25
        assert cfg["progress_bar_width"] == 80

    def test_missing_keys_are_saved_as_defaults(self, config_file):
        save_config({"quality": "LOW"})
        saved = json.loads(config_file.read_text())
        assert saved["quality"] == "LOW"
        assert saved["page_size"] == DEFAULTS["page_size"]
        assert saved["version"] == config_mod.CONFIG_VERSION

    def test_unknown_keys_preserved(self, config_file):
        """A setting written by a newer build must survive an older one."""
        config_file.write_text(json.dumps({"page_size": 20, "artwork": True, "cache_mb": 1800}))
        cfg = load_config()
        cfg["page_size"] = 30
        save_config(cfg)

        saved = json.loads(config_file.read_text())
        assert saved["artwork"] is True
        assert saved["cache_mb"] == 1800
        assert saved["page_size"] == 30

    def test_save_survives_unwritable_dir(self, tmp_path, monkeypatch):
        """A failed write must never crash the TUI."""
        blocked = tmp_path / "blocked"
        blocked.write_text("i am a file, not a directory")
        monkeypatch.setattr(config_mod, "CONFIG_DIR", blocked)
        monkeypatch.setattr(config_mod, "CONFIG_FILE", blocked / "config.json")
        save_config(dict(DEFAULTS))  # must not raise


class TestCoerce:
    def test_int_clamped_to_bounds(self):
        spec = get_spec("page_size")
        assert coerce(spec, 999) == spec["max"]
        assert coerce(spec, 1) == spec["min"]
        assert coerce(spec, 20) == 20

    def test_bar_width_clamped_to_bounds(self):
        spec = get_spec("progress_bar_width")
        assert coerce(spec, 5) == 20
        assert coerce(spec, 5000) == 120

    def test_int_accepts_numeric_string(self):
        assert coerce(get_spec("page_size"), "22") == 22

    def test_bool_is_not_a_valid_int_setting(self):
        assert coerce(get_spec("page_size"), True) == DEFAULTS["page_size"]

    def test_choice_is_case_insensitive(self):
        assert coerce(get_spec("quality"), "hires") == "HIRES"

    def test_unknown_choice_falls_back(self):
        assert coerce(get_spec("quality"), "MP3") == "HIGH"


class TestCycleValue:
    def test_choice_wraps_forward(self):
        spec = get_spec("quality")
        assert cycle_value(spec, "HIRES", 1) == "LOW"

    def test_choice_wraps_backward(self):
        spec = get_spec("quality")
        assert cycle_value(spec, "LOW", -1) == "HIRES"

    def test_choice_steps_in_spec_order(self):
        spec = get_spec("quality")
        assert cycle_value(spec, "LOW", 1) == "HIGH"

    def test_int_steps_and_stops_at_bounds(self):
        spec = get_spec("progress_bar_width")
        assert cycle_value(spec, 50, 1) == 52
        assert cycle_value(spec, 50, -1) == 48
        assert cycle_value(spec, 120, 1) == 120
        assert cycle_value(spec, 20, -1) == 20


class TestAtomicWrite:
    def test_no_temp_file_left_behind(self, config_file, tmp_path):
        save_config(dict(DEFAULTS))
        assert json.loads(config_file.read_text())["page_size"] == 15
        assert not (tmp_path / "config.tmp").exists()

    def test_file_is_owner_only(self, config_file):
        save_config(dict(DEFAULTS))
        assert os.stat(config_file).st_mode & 0o777 == 0o600


class TestSettingsKeyHandler:
    def _player(self):
        p = HeadlessTidalPlayer()
        p._mode = p.MODE_SETTINGS
        return p

    def test_row_navigation_is_clamped(self, config_file):
        p = self._player()
        p._handle_settings_key(player_mod.KEY_UP)
        assert p._settings_cursor == 0
        for _ in range(len(SETTINGS_SPEC) + 3):
            p._handle_settings_key(player_mod.KEY_DOWN)
        assert p._settings_cursor == len(SETTINGS_SPEC) - 1

    def test_quality_cycles_and_wraps(self, config_file):
        p = self._player()
        p._settings_cursor = 0  # quality row
        assert p.config["quality"] == "HIGH"
        p._handle_settings_key(player_mod.KEY_RIGHT)
        assert p.config["quality"] == "LOSSLESS"
        p._handle_settings_key(player_mod.KEY_RIGHT)
        assert p.config["quality"] == "HIRES"
        p._handle_settings_key(player_mod.KEY_RIGHT)
        assert p.config["quality"] == "LOW"  # wrapped
        p._handle_settings_key(player_mod.KEY_LEFT)
        assert p.config["quality"] == "HIRES"

    def test_quality_change_applies_live(self, config_file):
        import tidalapi

        p = self._player()
        p._settings_cursor = 0
        p._handle_settings_key(player_mod.KEY_LEFT)  # HIGH → LOW
        assert p._quality_name == "LOW"
        assert p.session.audio_quality == tidalapi.Quality.low_320k

    def test_page_size_change_applies_live_and_saves(self, config_file):
        p = self._player()
        p._settings_cursor = 1  # page size row
        p._handle_settings_key(player_mod.KEY_RIGHT)
        assert p._page_size == 16
        assert json.loads(config_file.read_text())["page_size"] == 16

    def test_bar_width_change_applies_live_and_saves(self, config_file):
        p = self._player()
        p._settings_cursor = 2  # progress bar width row
        p._handle_settings_key(player_mod.KEY_LEFT)
        assert p._bar_width == 48
        assert json.loads(config_file.read_text())["progress_bar_width"] == 48

    def test_clamped_edit_writes_nothing(self, config_file):
        p = self._player()
        p.config["page_size"] = 5  # already at the minimum
        p._settings_cursor = 1
        p._handle_settings_key(player_mod.KEY_LEFT)
        assert not config_file.exists()

    def test_esc_returns_to_player(self, config_file):
        p = self._player()
        p._handle_settings_key(player_mod.KEY_ESC)
        assert p._mode == p.MODE_PLAYER

    def test_c_opens_settings_from_player(self, config_file):
        p = HeadlessTidalPlayer()
        p._handle_player_key("c")
        assert p._mode == p.MODE_SETTINGS
        assert p._settings_cursor == 0

    def test_settings_page_renders(self, config_file):
        p = self._player()
        text = p._build_settings_display().plain
        assert "Settings" in text
        for spec in SETTINGS_SPEC:
            assert spec["label"] in text


class TestConfiguredValuesUsed:
    def test_player_reads_sizes_from_config(self, config_file):
        save_config({"quality": "HIGH", "page_size": 7, "progress_bar_width": 30})
        p = HeadlessTidalPlayer()
        assert p._page_size == 7
        assert p._bar_width == 30

    def test_configured_quality_used_when_flag_omitted(self, config_file):
        import tidalapi

        save_config({**DEFAULTS, "quality": "HIRES"})
        p = HeadlessTidalPlayer()
        assert p._quality_name == "HIRES"
        assert p.session.audio_quality == tidalapi.Quality.hi_res_lossless

    def test_flag_overrides_config_without_saving_it(self, config_file):
        save_config({**DEFAULTS, "quality": "LOW"})
        p = HeadlessTidalPlayer(quality="hires")
        assert p._quality_name == "HIRES"
        assert p.config["quality"] == "LOW"  # saved default untouched
        assert json.loads(config_file.read_text())["quality"] == "LOW"


class TestCLIQuality:
    def _patch_player(self, monkeypatch):
        seen = {}

        class _FakePlayer:
            def __init__(self, quality=None):
                seen["quality"] = quality

            def run(self):
                seen["ran"] = True

        monkeypatch.setattr(player_mod, "HeadlessTidalPlayer", _FakePlayer)
        return seen

    def test_explicit_quality_is_passed_through(self, monkeypatch):
        seen = self._patch_player(monkeypatch)
        result = CliRunner().invoke(cli_mod.cli, ["--quality", "HIRES"])
        assert result.exit_code == 0
        assert seen["quality"] == "HIRES"
        assert seen["ran"] is True

    def test_omitted_quality_defers_to_config(self, monkeypatch):
        seen = self._patch_player(monkeypatch)
        result = CliRunner().invoke(cli_mod.cli, [])
        assert result.exit_code == 0
        assert seen["quality"] is None  # player falls back to config.json

    def test_help_still_lists_choices(self):
        result = CliRunner().invoke(cli_mod.cli, ["--help"])
        assert result.exit_code == 0
        # click renders a case-insensitive Choice lowercased
        for choice in ("low", "high", "lossless", "hires"):
            assert choice in result.output.lower()
