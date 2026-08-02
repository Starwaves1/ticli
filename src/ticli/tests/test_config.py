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
    SETTINGS_ROWS,
    SETTINGS_SPEC,
    coerce,
    cycle_value,
    get_spec,
    load_config,
    save_config,
)


class _FakeAudio:
    """Records volume changes the way a live AudioPlayer would receive them."""

    player_cmd = "mpv"

    def __init__(self, ceiling=None):
        self.volumes = []
        self._ceiling = ceiling if ceiling is not None else player_mod.VOLUME_MAX

    def set_volume(self, value):
        self.volumes.append(value)

    def volume_ceiling(self):
        return self._ceiling


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """The settings page counts cached songs, so keep it off the real cache."""
    from ticli.utils import cache as cache_mod
    monkeypatch.setattr(cache_mod, "CACHE_DIR", tmp_path / "cache")


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
        assert cfg["progress_bar_max"] == 50
        assert cfg["volume"] == 100

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
            "progress_bar_max": None,
        }))
        cfg = load_config()
        assert cfg == {**DEFAULTS, "version": config_mod.CONFIG_VERSION}


class TestMigration:
    """v1 named every tier one step below the stream it asked for. The rename
    must keep an existing user on the exact same bytes, never downgrade them."""

    def test_v1_low_lands_on_medium(self, config_file):
        """v1's "LOW" asked for the 320k stream; the v1 rename called that
        HIGH and the v5 rename calls it MEDIUM. Two hops, same bytes."""
        config_file.write_text(json.dumps({"version": 1, "quality": "LOW"}))
        assert load_config()["quality"] == "MEDIUM"

    def test_v1_high_lands_on_high(self, config_file):
        """v1's "HIGH" asked for the lossless stream — which the v5 scheme
        happens to call HIGH again. The name comes full circle; the stream
        never moved."""
        config_file.write_text(json.dumps({"version": 1, "quality": "HIGH"}))
        assert load_config()["quality"] == "HIGH"

    def test_v1_top_tiers_take_the_new_names(self, config_file):
        """v1's LOSSLESS and HIRES pass the v1 block untouched and are then
        renamed by the v5 block like any other old spelling."""
        for saved, now in (("LOSSLESS", "HIGH"), ("HIRES", "MAX")):
            config_file.write_text(json.dumps({"version": 1, "quality": saved}))
            assert load_config()["quality"] == now

    def test_v1_stream_is_preserved_end_to_end(self, config_file):
        """The point of the migration: same tidalapi Quality before and after."""
        import tidalapi

        config_file.write_text(json.dumps({"version": 1, "quality": "LOW"}))
        p = HeadlessTidalPlayer()
        assert p.session.audio_quality == tidalapi.Quality.low_320k  # v1's "LOW"

    def test_v2_passes_through_untouched(self, config_file):
        config_file.write_text(json.dumps({"version": 2, "quality": "LOW", "page_size": 22}))
        cfg = load_config()
        assert cfg["quality"] == "LOW"
        assert cfg["page_size"] == 22

    def test_versionless_file_is_treated_as_current(self, config_file):
        """No version key means "written by this build" — don't rename."""
        config_file.write_text(json.dumps({"quality": "LOW"}))
        assert load_config()["quality"] == "LOW"

    def test_migration_stamps_the_new_version(self, config_file):
        config_file.write_text(json.dumps({"version": 1, "quality": "HIGH"}))
        assert load_config()["version"] == config_mod.CONFIG_VERSION


class TestMigrationV5:
    """v4 named the tiers after tidalapi's wire values; v5 uses TIDAL's own
    player's names. Same rule as v1: a rename must keep an existing user on
    the exact same bytes — the name moves, the stream never does."""

    def test_every_v4_name_lands_on_its_stream(self, config_file):
        import tidalapi

        expected = {
            "LOW": ("LOW", tidalapi.Quality.low_96k),
            "HIGH": ("MEDIUM", tidalapi.Quality.low_320k),
            "LOSSLESS": ("HIGH", tidalapi.Quality.high_lossless),
            "HIRES": ("MAX", tidalapi.Quality.hi_res_lossless),
        }
        for saved, (name, stream) in expected.items():
            config_file.write_text(json.dumps({"version": 4, "quality": saved}))
            assert load_config()["quality"] == name
            assert HeadlessTidalPlayer.QUALITY_MAP[name] == stream

    def test_high_is_disambiguated_by_the_version_number(self, config_file):
        """"HIGH" is the one spelling both schemes use for different streams:
        320k AAC in v4, 16-bit FLAC in v5. The version number is the only
        thing that can tell the two apart, and it must."""
        config_file.write_text(json.dumps({"version": 4, "quality": "HIGH"}))
        assert load_config()["quality"] == "MEDIUM"
        config_file.write_text(json.dumps({"version": 5, "quality": "HIGH"}))
        assert load_config()["quality"] == "HIGH"

    def test_a_v1_file_chains_through_both_renames(self, config_file):
        """The full gauntlet: v1 "LOW" asked for 320k, which v2 called HIGH
        and v5 calls MEDIUM — and the player must end up streaming exactly
        what that user has heard since v1."""
        import tidalapi

        config_file.write_text(json.dumps({"version": 1, "quality": "LOW"}))
        p = HeadlessTidalPlayer()
        assert p.config["quality"] == "MEDIUM"
        assert p.session.audio_quality == tidalapi.Quality.low_320k

    def test_junk_version_never_raises(self, config_file):
        config_file.write_text(json.dumps({"version": "banana", "quality": "MAX"}))
        assert load_config()["quality"] == "MAX"

    def test_migration_is_idempotent(self, config_file):
        """Load, save, load again — the value must settle, not climb a tier
        per launch."""
        config_file.write_text(json.dumps({"version": 1, "quality": "LOW"}))
        save_config(load_config())
        assert load_config()["quality"] == "MEDIUM"


class TestCacheMigrationV2:
    """v2's single cache choice became two booleans, and its MB budget became
    whole GB. Everything here goes through a written v2 file, because the bug
    the v1 migration shipped with was invisible to a unit test of _migrate:
    the value was computed and then thrown away by the loop that followed."""

    def _v2(self, config_file, **extra):
        config_file.write_text(json.dumps({"version": 2, **extra}))
        return load_config()

    def test_full_becomes_both_switches_on(self, config_file):
        cfg = self._v2(config_file, cache_mode="FULL")
        assert cfg["cache_metadata"] is True
        assert cfg["cache_songs"] is True

    def test_metadata_becomes_lists_only(self, config_file):
        cfg = self._v2(config_file, cache_mode="METADATA")
        assert cfg["cache_metadata"] is True
        assert cfg["cache_songs"] is False

    def test_off_becomes_both_switches_off(self, config_file):
        cfg = self._v2(config_file, cache_mode="OFF")
        assert cfg["cache_metadata"] is False
        assert cfg["cache_songs"] is False

    def test_the_old_key_is_gone_after_a_save(self, config_file):
        self._v2(config_file, cache_mode="METADATA")
        save_config(load_config())
        saved = json.loads(config_file.read_text())
        assert "cache_mode" not in saved and "cache_budget_mb" not in saved
        assert saved["version"] == config_mod.CONFIG_VERSION

    def test_the_budget_becomes_whole_gigabytes(self, config_file):
        assert self._v2(config_file, cache_budget_mb=1536)["cache_budget_gb"] == 2
        assert self._v2(config_file, cache_budget_mb=4096)["cache_budget_gb"] == 4

    def test_a_small_budget_rounds_up_rather_than_to_nothing(self, config_file):
        """Truncating 200 MB to 0 GB would silently turn the cache off."""
        assert self._v2(config_file, cache_budget_mb=200)["cache_budget_gb"] == 1

    def test_a_zero_budget_stays_zero(self, config_file):
        assert self._v2(config_file, cache_budget_mb=0)["cache_budget_gb"] == 0

    def test_a_junk_budget_falls_back_to_the_default(self, config_file):
        cfg = self._v2(config_file, cache_budget_mb="lots")
        assert cfg["cache_budget_gb"] == DEFAULTS["cache_budget_gb"]

    def test_migration_is_idempotent(self, config_file):
        self._v2(config_file, cache_mode="METADATA", cache_budget_mb=1536)
        save_config(load_config())
        cfg = load_config()
        assert (cfg["cache_metadata"], cfg["cache_songs"], cfg["cache_budget_gb"]) == (
            True, False, 2)

    def test_a_v1_file_gets_both_migrations(self, config_file):
        config_file.write_text(json.dumps(
            {"version": 1, "quality": "LOW", "cache_mode": "FULL", "cache_budget_mb": 512}))
        cfg = load_config()
        assert cfg["quality"] == "MEDIUM"  # the v1 and v5 renames still apply
        assert cfg["cache_songs"] is True
        assert cfg["cache_budget_gb"] == 1


class TestBarWidthMigrationV3:
    """v3's `progress_bar_width` was the width the bar was laid out at. It is
    `progress_bar_max` now, and only a ceiling — the bar itself comes from the
    window. Same number, so nobody's bar changes on a wide terminal.

    Every test here goes through a written file and, where it is about what
    survives, a save and a re-load: the v1 migration shipped a bug that was
    invisible to a unit test of _migrate, because the value it computed was
    thrown away by the loop that ran after it.
    """

    def _v3(self, config_file, **extra):
        config_file.write_text(json.dumps({"version": 3, **extra}))
        return load_config()

    def test_the_width_becomes_the_ceiling(self, config_file):
        assert self._v3(config_file, progress_bar_width=80)["progress_bar_max"] == 80

    def test_it_survives_a_save_and_a_reload(self, config_file):
        self._v3(config_file, progress_bar_width=80)
        save_config(load_config())
        assert load_config()["progress_bar_max"] == 80

    def test_the_old_key_is_gone_after_a_save(self, config_file):
        self._v3(config_file, progress_bar_width=80)
        save_config(load_config())
        saved = json.loads(config_file.read_text())
        assert "progress_bar_width" not in saved
        assert saved["progress_bar_max"] == 80
        assert saved["version"] == config_mod.CONFIG_VERSION

    def test_a_v3_file_without_it_gets_the_default(self, config_file):
        assert self._v3(config_file)["progress_bar_max"] == DEFAULTS["progress_bar_max"]

    def test_junk_never_raises_and_falls_back(self, config_file):
        for junk in (None, "wide", True, [50]):
            cfg = self._v3(config_file, progress_bar_width=junk)
            assert cfg["progress_bar_max"] == DEFAULTS["progress_bar_max"], junk

    def test_migration_is_idempotent(self, config_file):
        self._v3(config_file, progress_bar_width=80)
        for _ in range(3):
            save_config(load_config())
        assert load_config()["progress_bar_max"] == 80

    def test_the_player_starts_with_the_migrated_ceiling(self, config_file):
        """End to end, which is the point: the number in the v3 file is the
        one the running player caps its bar at."""
        self._v3(config_file, progress_bar_width=80)
        assert HeadlessTidalPlayer()._bar_max == 80

    def test_a_v1_file_gets_this_migration_too(self, config_file):
        config_file.write_text(json.dumps(
            {"version": 1, "quality": "LOW", "progress_bar_width": 96}))
        cfg = load_config()
        assert cfg["quality"] == "MEDIUM"  # the v1+v5 rename chain rides along
        assert cfg["progress_bar_max"] == 96


class TestVolumeSurvivesLeavingThePage:
    """The volume row moved off the settings page and onto the [v] overlay.
    That is a schema change even though no key changed name — a `hidden` spec
    that stopped being defaulted, coerced or written would silently reset
    everyone's volume on the first save after the upgrade."""

    def test_an_existing_value_survives_the_upgrade(self, config_file):
        config_file.write_text(json.dumps({"version": 3, "volume": 140}))
        assert load_config()["volume"] == 140
        save_config(load_config())
        assert json.loads(config_file.read_text())["volume"] == 140
        assert load_config()["volume"] == 140

    def test_it_is_still_defaulted_coerced_and_clamped(self, config_file):
        config_file.write_text(json.dumps({"version": 3, "volume": 9000}))
        assert load_config()["volume"] == get_spec("volume")["max"]
        assert "volume" in DEFAULTS

    def test_a_file_written_before_the_move_still_loads(self, config_file):
        """The whole v3 record, exactly as the last release wrote it."""
        config_file.write_text(json.dumps({
            "version": 3, "quality": "MAX", "page_size": 20,
            "progress_bar_width": 64, "volume": 120, "show_artwork": False,
            "cache_metadata": True, "cache_songs": True, "cache_budget_gb": 4,
        }))
        cfg = load_config()
        assert cfg["volume"] == 120
        assert cfg["progress_bar_max"] == 64
        assert cfg["page_size"] == 20
        assert cfg["quality"] == "MAX"
        assert cfg["cache_budget_gb"] == 4

    def test_the_page_no_longer_lists_it_but_the_spec_still_holds_it(self):
        assert "volume" in [s["key"] for s in SETTINGS_SPEC]
        assert "volume" not in [s["key"] for s in SETTINGS_ROWS]


class TestBooleanSettings:
    def test_a_bool_row_takes_only_real_booleans(self):
        spec = get_spec("cache_songs")
        assert coerce(spec, False) is False
        assert coerce(spec, "true") is True
        assert coerce(spec, 0) is False
        assert coerce(spec, "maybe") is spec["default"]

    def test_either_direction_toggles(self):
        spec = get_spec("cache_metadata")
        assert cycle_value(spec, True, 1) is False
        assert cycle_value(spec, True, -1) is False
        assert cycle_value(spec, False, 1) is True

    def test_a_bool_reads_as_a_word(self):
        assert config_mod.display_value(get_spec("cache_songs"), True) == "On"
        assert config_mod.display_value(get_spec("cache_songs"), False) == "Off"

    def test_sizes_carry_their_unit(self):
        assert config_mod.display_value(get_spec("cache_budget_gb"), 2) == "2 GB"
        assert config_mod.display_value(get_spec("volume"), 150) == "150%"


class TestQualityTiers:
    """The player's map is the only place setting names meet tidalapi."""

    def test_every_choice_maps_to_a_distinct_tidalapi_tier(self):
        values = [HeadlessTidalPlayer.QUALITY_MAP[c] for c in config_mod.QUALITY_CHOICES]
        assert len(set(values)) == len(values)

    def test_map_covers_the_spec_choices_exactly(self):
        assert set(HeadlessTidalPlayer.QUALITY_MAP) == set(config_mod.QUALITY_CHOICES)
        assert set(HeadlessTidalPlayer.QUALITY_LABELS) == set(config_mod.QUALITY_CHOICES)
        assert set(config_mod.QUALITY_MEANINGS) == set(config_mod.QUALITY_CHOICES)

    def test_tiers_ascend(self):
        import tidalapi

        assert [HeadlessTidalPlayer.QUALITY_MAP[c] for c in config_mod.QUALITY_CHOICES] == [
            tidalapi.Quality.low_96k,
            tidalapi.Quality.low_320k,
            tidalapi.Quality.high_lossless,
            tidalapi.Quality.hi_res_lossless,
        ]


class TestSaveLoadRoundTrip:
    def test_round_trip(self, config_file):
        save_config({"quality": "MAX", "page_size": 25, "progress_bar_max": 80})
        cfg = load_config()
        assert cfg["quality"] == "MAX"
        assert cfg["page_size"] == 25
        assert cfg["progress_bar_max"] == 80

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
        spec = get_spec("progress_bar_max")
        assert coerce(spec, 5) == 20
        assert coerce(spec, 5000) == 200

    def test_volume_clamped_to_bounds(self):
        spec = get_spec("volume")
        assert coerce(spec, -20) == 0
        assert coerce(spec, 300) == 250  # amplification tops out at 250%
        assert coerce(spec, 45) == 45

    def test_int_accepts_numeric_string(self):
        assert coerce(get_spec("page_size"), "22") == 22

    def test_bool_is_not_a_valid_int_setting(self):
        assert coerce(get_spec("page_size"), True) == DEFAULTS["page_size"]

    def test_choice_is_case_insensitive(self):
        assert coerce(get_spec("quality"), "max") == "MAX"

    def test_unknown_choice_falls_back(self):
        assert coerce(get_spec("quality"), "MP3") == DEFAULTS["quality"]


class TestCycleValue:
    def test_choice_wraps_forward(self):
        spec = get_spec("quality")
        assert cycle_value(spec, "MAX", 1) == "LOW"

    def test_choice_wraps_backward(self):
        spec = get_spec("quality")
        assert cycle_value(spec, "LOW", -1) == "MAX"

    def test_choice_steps_in_spec_order(self):
        spec = get_spec("quality")
        assert cycle_value(spec, "LOW", 1) == "MEDIUM"

    def test_every_choice_setting_wraps_at_both_ends(self):
        """Choices are a ring: → off the last lands on the first, and back."""
        for spec in SETTINGS_SPEC:
            if spec["kind"] != "choice":
                continue
            first, last = spec["choices"][0], spec["choices"][-1]
            assert cycle_value(spec, last, 1) == first
            assert cycle_value(spec, first, -1) == last

    def test_full_forward_lap_returns_to_start(self):
        spec = get_spec("quality")
        value = spec["choices"][0]
        for _ in range(len(spec["choices"])):
            value = cycle_value(spec, value, 1)
        assert value == spec["choices"][0]

    def test_int_steps_and_stops_at_bounds(self):
        spec = get_spec("progress_bar_max")
        assert cycle_value(spec, 50, 1) == 52
        assert cycle_value(spec, 50, -1) == 48
        assert cycle_value(spec, 200, 1) == 200
        assert cycle_value(spec, 20, -1) == 20

    def test_volume_steps_by_five_and_stops_at_bounds(self):
        spec = get_spec("volume")
        assert cycle_value(spec, 100, -1) == 95
        assert cycle_value(spec, 95, 1) == 100
        assert cycle_value(spec, 100, 1) == 105  # above unity is allowed now
        assert cycle_value(spec, 250, 1) == 250
        assert cycle_value(spec, 0, -1) == 0


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
        for _ in range(len(SETTINGS_ROWS) + 3):
            p._handle_settings_key(player_mod.KEY_DOWN)
        assert p._settings_cursor == len(SETTINGS_ROWS) - 1

    def test_quality_cycles_and_wraps(self, config_file):
        p = self._player()
        p._settings_cursor = 0  # quality row
        assert p.config["quality"] == "HIGH"
        p._handle_settings_key(player_mod.KEY_RIGHT)
        assert p.config["quality"] == "MAX"
        p._handle_settings_key(player_mod.KEY_RIGHT)
        assert p.config["quality"] == "LOW"  # wrapped past the last value
        p._handle_settings_key(player_mod.KEY_RIGHT)
        assert p.config["quality"] == "MEDIUM"
        p._handle_settings_key(player_mod.KEY_LEFT)
        assert p.config["quality"] == "LOW"
        p._handle_settings_key(player_mod.KEY_LEFT)
        assert p.config["quality"] == "MAX"  # wrapped past the first value

    def test_quality_change_applies_live(self, config_file):
        import tidalapi

        p = self._player()
        p._settings_cursor = 0
        p._handle_settings_key(player_mod.KEY_LEFT)  # HIGH → MEDIUM
        assert p._quality_name == "MEDIUM"
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
        assert p._bar_max == 48
        assert json.loads(config_file.read_text())["progress_bar_max"] == 48

    def test_no_row_on_this_page_edits_the_volume(self, config_file):
        """Volume moved to the [v] overlay, and the page it left has to be
        clean about it: no row of it, and nothing here can change it."""
        p = self._player()
        p.audio = _FakeAudio()
        for i in range(len(SETTINGS_ROWS)):
            p._settings_cursor = i
            p._handle_settings_key(player_mod.KEY_LEFT)
            p._handle_settings_key(player_mod.KEY_RIGHT)
        assert p.config["volume"] == 100
        assert p.audio.volumes == []

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
        for spec in SETTINGS_ROWS:
            assert spec["label"] in text
        assert "Volume" not in text, "the volume row lives behind [v] now"


def _row(key):
    """Where a setting sits on the page. Deliberately SETTINGS_ROWS and not
    SETTINGS_SPEC: a hidden setting has no row, and asking for one raises
    rather than silently pointing the cursor at its neighbour."""
    return [spec["key"] for spec in SETTINGS_ROWS].index(key)


class TestNumericEntry:
    """Typing a number into a row. Leaving the box is what saves it — there
    is no separate confirm step, so every way out means the same thing."""

    def _player(self, key="page_size"):
        p = HeadlessTidalPlayer()
        p._mode = p.MODE_SETTINGS
        p._settings_cursor = _row(key)
        return p

    def _type(self, p, keys):
        for key in keys:
            p._handle_settings_key(key)

    def test_a_digit_starts_typing_instead_of_navigating(self, config_file):
        p = self._player()
        self._type(p, "2")
        assert p._settings_edit == "2"
        assert p.config["page_size"] == 15, "nothing is written until you leave"

    def test_digits_append_and_enter_saves(self, config_file):
        p = self._player()
        self._type(p, "22")
        p._handle_settings_key(player_mod.KEY_ENTER)
        assert p._settings_edit is None
        assert p.config["page_size"] == 22
        assert json.loads(config_file.read_text())["page_size"] == 22

    def test_backspace_deletes(self, config_file):
        p = self._player()
        self._type(p, "39")
        p._handle_settings_key(player_mod.KEY_BACKSPACE)
        self._type(p, "0")
        p._handle_settings_key(player_mod.KEY_ENTER)
        assert p.config["page_size"] == 30

    def test_esc_saves_and_stays_on_the_page(self, config_file):
        """The spec says leaving the textbox saves. Esc is leaving the box,
        not the page — a second Esc is what goes back."""
        p = self._player()
        self._type(p, "20")
        p._handle_settings_key(player_mod.KEY_ESC)
        assert p.config["page_size"] == 20
        assert p._mode == p.MODE_SETTINGS

        p._handle_settings_key(player_mod.KEY_ESC)
        assert p._mode == p.MODE_PLAYER

    def test_arrowing_away_saves_and_moves(self, config_file):
        p = self._player()
        self._type(p, "20")
        p._handle_settings_key(player_mod.KEY_DOWN)
        assert p.config["page_size"] == 20
        assert p._settings_cursor == _row("progress_bar_max")

    def test_out_of_range_clamps_to_the_bound(self, config_file):
        p = self._player()
        self._type(p, "999")
        p._handle_settings_key(player_mod.KEY_ENTER)
        assert p.config["page_size"] == get_spec("page_size")["max"]

    def test_below_range_clamps_to_the_bound(self, config_file):
        p = self._player()
        self._type(p, "1")
        p._handle_settings_key(player_mod.KEY_ENTER)
        assert p.config["page_size"] == get_spec("page_size")["min"]

    def test_an_empty_box_reverts_rather_than_writing_zero(self, config_file):
        p = self._player()
        self._type(p, "2")
        p._handle_settings_key(player_mod.KEY_BACKSPACE)
        assert p._settings_edit == ""
        p._handle_settings_key(player_mod.KEY_ENTER)
        assert p.config["page_size"] == 15
        assert not config_file.exists(), "an empty entry must not write anything"

    def test_a_digit_on_a_choice_row_is_ignored(self, config_file):
        p = self._player("quality")
        self._type(p, "3")
        assert p._settings_edit is None
        assert p.config["quality"] == "HIGH"

    def test_arrows_still_step_when_not_typing(self, config_file):
        p = self._player()
        p._handle_settings_key(player_mod.KEY_RIGHT)
        assert p.config["page_size"] == 16
        p._handle_settings_key(player_mod.KEY_LEFT)
        assert p.config["page_size"] == 15

    def test_the_screen_shows_that_you_are_typing(self, config_file):
        p = self._player()
        self._type(p, "2")
        text = p._build_settings_display().plain
        assert "‹ 2▏›" in text
        assert "Typing a number" in text

    def test_the_budget_is_typed_in_gigabytes(self, config_file):
        p = self._player("cache_budget_gb")
        self._type(p, "5")
        p._handle_settings_key(player_mod.KEY_ENTER)
        assert p.config["cache_budget_gb"] == 5
        assert p._cache.budget_gb == 5
        assert p._cache.budget_bytes == 5 * 1024 ** 3


def _overlay_player(audio=None):
    """A player with the [v] overlay up, which is where the volume is edited
    now. The mode underneath is the player screen, because the overlay is over
    whatever is on screen rather than a place you navigate to."""
    p = HeadlessTidalPlayer()
    p.audio = audio
    p._handle_key("v")
    assert p._volume_open is True
    return p


def _overlay_text(p, fit=None):
    """What the overlay actually puts on screen, as plain text."""
    lines = p._build_volume_overlay(fit or player_mod.ROOMY_FIT)
    return "\n".join(line.plain for line in lines)


class TestVolumeAboveUnity:
    def _player(self, audio=None):
        return _overlay_player(audio)

    def _up(self, p, times=1):
        for _ in range(times):
            p._handle_key(player_mod.KEY_RIGHT)

    def test_mpv_may_be_taken_to_250(self, config_file):
        p = self._player(_FakeAudio())
        self._up(p, 40)
        assert p.config["volume"] == 250
        assert p.audio.volumes[-1] == 250

    def test_ffplay_is_capped_at_what_it_can_do(self, config_file):
        """ffplay's -volume is 0=min 100=max and it says so on stderr, so the
        overlay must stop there rather than pretend."""
        audio = _FakeAudio(ceiling=100)
        audio.player_cmd = "ffplay"
        p = self._player(audio)
        self._up(p, 40)
        assert p.config["volume"] == 100

    def test_the_value_reads_as_a_percentage(self, config_file):
        p = self._player(_FakeAudio())
        assert "100%" in _overlay_text(p)

    def test_the_caution_starts_at_105(self, config_file):
        p = self._player(_FakeAudio())
        p.config["volume"] = 100
        assert "quality suffers" not in _overlay_text(p)
        p.config["volume"] = 105
        assert "quality suffers" in _overlay_text(p)

    def test_the_caution_is_blue_not_dim(self, config_file):
        """Deliberately louder than the page's dim idiom — the owner asked for
        it to be noticed."""
        p = self._player(_FakeAudio())
        p.config["volume"] = 150
        note = next(line for line in p._build_volume_overlay(player_mod.ROOMY_FIT)
                    if "quality suffers" in line.plain)
        styles = {str(note.style)} | {str(span.style) for span in note.spans}
        assert styles == {"blue"}

    def test_ffplay_says_it_caps(self, config_file):
        audio = _FakeAudio(ceiling=100)
        audio.player_cmd = "ffplay"
        p = self._player(audio)
        assert "ffplay caps at 100%" in _overlay_text(p)

    def test_mpv_says_nothing_because_it_caps_at_nothing(self, config_file):
        p = self._player(_FakeAudio())
        assert "caps at" not in _overlay_text(p)

    def test_ffplay_is_never_handed_more_than_it_takes(self):
        from ticli.player import AudioPlayer
        assert AudioPlayer("ffplay", volume=250)._ffplay_volume() == 100
        assert AudioPlayer("ffplay", volume=80)._ffplay_volume() == 80
        assert AudioPlayer("mpv", volume=250).volume_ceiling() == 250
        assert AudioPlayer("ffplay", volume=250).volume_ceiling() == 100

    def test_mpv_is_spawned_with_a_ceiling_it_can_reach(self, monkeypatch):
        """mpv refuses a volume above --volume-max, whose default (130) is
        below what the setting allows."""
        from ticli.player import AudioPlayer

        spawned = {}

        class _Proc:
            def __init__(self, cmd, **kw):
                spawned["cmd"] = cmd

            def poll(self):
                return None

        monkeypatch.setattr(player_mod.subprocess, "Popen", _Proc)
        audio = AudioPlayer("mpv", volume=250)
        monkeypatch.setattr(audio, "_start_download", lambda *a, **kw: None)
        audio.play_url("https://cdn.example/t.mp4")
        assert f"--volume-max={player_mod.VOLUME_MAX}" in spawned["cmd"]
        assert "--volume=250" in spawned["cmd"]


class TestVolumeCeilingIsDurable:
    """The volume and the backend are chosen independently — the setting is
    saved on one run and the backend is discovered on the next. These are the
    ways the two can disagree."""

    def _player(self, audio=None):
        return _overlay_player(audio)

    def test_the_ceiling_comes_from_the_running_backend_not_the_platform(self, monkeypatch):
        """Same mpv, same answer, whichever OS it is running on — nothing here
        predicts a platform, so Linux and macOS need no separate branch."""
        from ticli.player import AudioPlayer
        for platform in ("darwin", "linux", "win32"):
            monkeypatch.setattr(player_mod.sys, "platform", platform)
            assert AudioPlayer("mpv", volume=100).volume_ceiling() == 250, platform
            assert AudioPlayer("ffplay", volume=100).volume_ceiling() == 100, platform

    def test_an_unknown_backend_gets_unity_not_the_higher_number(self):
        """A backend added later, or a mangled player_cmd: guessing high would
        apply a volume the backend then quietly reinterprets."""
        from ticli.player import AudioPlayer
        assert AudioPlayer("vlc", volume=250).volume_ceiling() == 100
        assert AudioPlayer("", volume=250).volume_ceiling() == 100

    @pytest.mark.parametrize("answer", [
        RuntimeError("no idea"),   # raised
        None,                      # not a number
        "loud",                    # not a number either
    ])
    def test_a_backend_that_cannot_answer_gets_unity(self, answer, config_file):
        """Durability over cleverness — a ceiling that can't be established is
        not a licence to amplify, and a settings row is never worth a crash."""
        class _Broken:
            player_cmd = "mpv"

            def volume_ceiling(self):
                if isinstance(answer, Exception):
                    raise answer
                return answer

            def set_volume(self, value):
                pass

        p = self._player(_Broken())
        assert p._setting_ceiling(get_spec("volume")) == 100
        assert "100%" in _overlay_text(p)

    def test_a_backend_with_no_ceiling_method_at_all_gets_unity(self, config_file):
        p = self._player(type("_Old", (), {"player_cmd": "mpv"})())
        assert p._setting_ceiling(get_spec("volume")) == 100

    def test_no_backend_yet_means_unity(self, config_file):
        p = self._player(None)
        assert p._setting_ceiling(get_spec("volume")) == 100

    def test_a_saved_250_is_clamped_when_only_ffplay_is_there(self, config_file):
        """The config was written next to mpv. mpv is gone. Neither fail nor
        send 250 — come back at 100, on screen and in the file."""
        save_config({**DEFAULTS, "volume": 250})
        audio = _FakeAudio(ceiling=100)
        audio.player_cmd = "ffplay"
        p = self._player(audio)
        assert p.config["volume"] == 250, "it is still what the file says"

        p._clamp_volume_to_backend()

        assert p.config["volume"] == 100
        assert audio.volumes == [100], "the backend is told the clamped value"
        assert json.loads(config_file.read_text())["volume"] == 100
        assert "100%" in _overlay_text(p)

    def test_the_clamp_leaves_a_reachable_value_alone(self, config_file):
        save_config({**DEFAULTS, "volume": 250})
        p = self._player(_FakeAudio())  # mpv

        p._clamp_volume_to_backend()

        assert p.config["volume"] == 250
        assert p.audio.volumes == [250]

    def test_the_clamp_repairs_a_hand_edited_out_of_range_value(self, config_file):
        config_file.write_text(json.dumps({"version": 3, "volume": 9000}))
        p = self._player(_FakeAudio())
        assert p.config["volume"] == 250, "load_config clamps to the spec max"

        p._clamp_volume_to_backend()

        assert p.config["volume"] == 250
        assert p.audio.volumes == [250]

    def test_run_clamps_before_anything_plays(self, config_file, monkeypatch):
        """The whole point of doing it in run(): the backend is only known
        there, and nothing must have been played at the stale volume yet."""
        save_config({**DEFAULTS, "volume": 250})
        monkeypatch.setattr(player_mod, "_find_audio_player", lambda: "ffplay")
        p = HeadlessTidalPlayer()
        monkeypatch.setattr(p, "_login", lambda: False)  # stop run() right after

        p.run()

        assert p.config["volume"] == 100
        assert p.audio.volume == 100, "never handed the out-of-range value"
        assert json.loads(config_file.read_text())["volume"] == 100

    def test_switching_back_to_mpv_does_not_resurrect_the_old_value(self, config_file):
        """Clamping is destructive on purpose: the file holds one number, and
        it is the one that was really applied."""
        save_config({**DEFAULTS, "volume": 250})
        ffplay = _FakeAudio(ceiling=100)
        ffplay.player_cmd = "ffplay"
        p = self._player(ffplay)
        p._clamp_volume_to_backend()

        later = self._player(_FakeAudio())  # mpv is back next run
        later._clamp_volume_to_backend()

        assert later.config["volume"] == 100
        assert later.audio.volumes == [100]

    def test_the_two_blue_notes_can_never_appear_together(self, config_file):
        """The caution needs >=105 and the cap note needs a backend that stops
        at 100 — the clamp makes those mutually exclusive, which is what keeps
        the row inside 80 columns."""
        from rich.console import Console

        for cmd, ceiling, volume in (("mpv", 250, 250), ("ffplay", 100, 250)):
            audio = _FakeAudio(ceiling=ceiling)
            audio.player_cmd = cmd
            p = self._player(audio)
            p.config["volume"] = volume
            p._clamp_volume_to_backend()
            rendered = _overlay_text(p)
            assert not ("quality suffers" in rendered and "caps at" in rendered), cmd
            console = Console(width=80)
            with console.capture() as cap:
                console.print(p._build_display())
            assert all(len(line) <= 80 for line in cap.get().splitlines()), cmd

    def test_the_overlay_never_shows_a_value_the_backend_cannot_reach(self, config_file):
        audio = _FakeAudio(ceiling=100)
        audio.player_cmd = "ffplay"
        p = self._player(audio)
        p._clamp_volume_to_backend()
        for _ in range(40):
            p._handle_key(player_mod.KEY_RIGHT)
        rendered = _overlay_text(p)
        assert "100%" in rendered
        assert "250%" not in rendered
        assert "ffplay caps at 100%" in rendered
        assert max(audio.volumes) == 100


class TestConfiguredValuesUsed:
    def test_player_reads_sizes_from_config(self, config_file):
        save_config({"quality": "HIGH", "page_size": 7, "progress_bar_max": 30})
        p = HeadlessTidalPlayer()
        assert p._page_size == 7
        assert p._bar_max == 30

    def test_configured_quality_used_when_flag_omitted(self, config_file):
        import tidalapi

        save_config({**DEFAULTS, "quality": "MAX"})
        p = HeadlessTidalPlayer()
        assert p._quality_name == "MAX"
        assert p.session.audio_quality == tidalapi.Quality.hi_res_lossless

    def test_flag_overrides_config_without_saving_it(self, config_file):
        save_config({**DEFAULTS, "quality": "LOW"})
        p = HeadlessTidalPlayer(quality="max")
        assert p._quality_name == "MAX"
        assert p.config["quality"] == "LOW"  # saved default untouched
        assert json.loads(config_file.read_text())["quality"] == "LOW"


class TestCLIQuality:
    def _patch_player(self, monkeypatch):
        seen = {}

        class _FakePlayer:
            def __init__(self, quality=None, login_flow=None):
                seen["quality"] = quality
                seen["login_flow"] = login_flow

            def run(self):
                seen["ran"] = True

        monkeypatch.setattr(player_mod, "HeadlessTidalPlayer", _FakePlayer)
        return seen

    def test_explicit_quality_is_passed_through(self, monkeypatch):
        seen = self._patch_player(monkeypatch)
        result = CliRunner().invoke(cli_mod.cli, ["--quality", "MAX"])
        assert result.exit_code == 0
        assert seen["quality"] == "MAX"
        assert seen["ran"] is True

    def test_omitted_quality_defers_to_config(self, monkeypatch):
        seen = self._patch_player(monkeypatch)
        result = CliRunner().invoke(cli_mod.cli, [])
        assert result.exit_code == 0
        assert seen["quality"] is None  # player falls back to config.json

    def test_the_old_spellings_still_work(self, monkeypatch):
        """The pre-rename names keep doing what they always did — corrected
        to the new name out loud, never rejected."""
        for legacy, now in (("LOSSLESS", "HIGH"), ("hires", "MAX")):
            seen = self._patch_player(monkeypatch)
            result = CliRunner().invoke(cli_mod.cli, ["--quality", legacy])
            assert result.exit_code == 0
            assert seen["quality"] == now
            assert f"--quality {now}" in result.output

    def test_high_says_what_it_means_now(self, monkeypatch):
        """"HIGH" changed meaning (320k AAC → 16-bit FLAC). It is accepted
        with the new meaning, and says so — an old script that meant AAC
        must be told it is now getting FLAC, not silently upgraded."""
        seen = self._patch_player(monkeypatch)
        result = CliRunner().invoke(cli_mod.cli, ["--quality", "HIGH"])
        assert result.exit_code == 0
        assert seen["quality"] == "HIGH"
        assert "MEDIUM" in result.output

    def test_help_still_lists_choices(self):
        result = CliRunner().invoke(cli_mod.cli, ["--help"])
        assert result.exit_code == 0
        # click renders a case-insensitive Choice lowercased
        for choice in ("low", "medium", "high", "max"):
            assert choice in result.output.lower()
