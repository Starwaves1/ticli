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
        assert cfg["quality"] == "LOSSLESS"
        assert cfg["page_size"] == 15
        assert cfg["progress_bar_width"] == 50
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
            "progress_bar_width": None,
        }))
        cfg = load_config()
        assert cfg == {**DEFAULTS, "version": config_mod.CONFIG_VERSION}


class TestMigration:
    """v1 named every tier one step below the stream it asked for. The rename
    must keep an existing user on the exact same bytes, never downgrade them."""

    def test_v1_low_becomes_high(self, config_file):
        config_file.write_text(json.dumps({"version": 1, "quality": "LOW"}))
        assert load_config()["quality"] == "HIGH"

    def test_v1_high_becomes_lossless(self, config_file):
        config_file.write_text(json.dumps({"version": 1, "quality": "HIGH"}))
        assert load_config()["quality"] == "LOSSLESS"

    def test_v1_top_tiers_are_unchanged(self, config_file):
        """v1's LOSSLESS and HIGH asked for the same stream, and HIRES was
        already correct — neither name moves."""
        for saved in ("LOSSLESS", "HIRES"):
            config_file.write_text(json.dumps({"version": 1, "quality": saved}))
            assert load_config()["quality"] == saved

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

    def test_junk_version_never_raises(self, config_file):
        config_file.write_text(json.dumps({"version": "banana", "quality": "HIRES"}))
        assert load_config()["quality"] == "HIRES"

    def test_migration_is_idempotent(self, config_file):
        """Load, save, load again — the value must settle, not climb a tier
        per launch."""
        config_file.write_text(json.dumps({"version": 1, "quality": "LOW"}))
        save_config(load_config())
        assert load_config()["quality"] == "HIGH"


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
        assert saved["version"] == 3

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
        assert cfg["quality"] == "HIGH"  # v1 rename still applies
        assert cfg["cache_songs"] is True
        assert cfg["cache_budget_gb"] == 1


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
        assert coerce(get_spec("quality"), "hires") == "HIRES"

    def test_unknown_choice_falls_back(self):
        assert coerce(get_spec("quality"), "MP3") == DEFAULTS["quality"]


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
        spec = get_spec("progress_bar_width")
        assert cycle_value(spec, 50, 1) == 52
        assert cycle_value(spec, 50, -1) == 48
        assert cycle_value(spec, 120, 1) == 120
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
        for _ in range(len(SETTINGS_SPEC) + 3):
            p._handle_settings_key(player_mod.KEY_DOWN)
        assert p._settings_cursor == len(SETTINGS_SPEC) - 1

    def test_quality_cycles_and_wraps(self, config_file):
        p = self._player()
        p._settings_cursor = 0  # quality row
        assert p.config["quality"] == "LOSSLESS"
        p._handle_settings_key(player_mod.KEY_RIGHT)
        assert p.config["quality"] == "HIRES"
        p._handle_settings_key(player_mod.KEY_RIGHT)
        assert p.config["quality"] == "LOW"  # wrapped past the last value
        p._handle_settings_key(player_mod.KEY_RIGHT)
        assert p.config["quality"] == "HIGH"
        p._handle_settings_key(player_mod.KEY_LEFT)
        assert p.config["quality"] == "LOW"
        p._handle_settings_key(player_mod.KEY_LEFT)
        assert p.config["quality"] == "HIRES"  # wrapped past the first value

    def test_quality_change_applies_live(self, config_file):
        import tidalapi

        p = self._player()
        p._settings_cursor = 0
        p._handle_settings_key(player_mod.KEY_LEFT)  # LOSSLESS → HIGH
        assert p._quality_name == "HIGH"
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

    def test_volume_change_applies_live_and_saves(self, config_file):
        p = self._player()
        p.audio = _FakeAudio()
        p._settings_cursor = 3  # volume row
        p._handle_settings_key(player_mod.KEY_LEFT)
        assert p.config["volume"] == 95
        assert p.audio.volumes == [95]  # pushed to the running player
        assert json.loads(config_file.read_text())["volume"] == 95

    def test_volume_change_without_audio_player(self, config_file):
        """Settings can be edited before run() ever built an AudioPlayer."""
        p = self._player()
        p.audio = None
        p._settings_cursor = 3
        p._handle_settings_key(player_mod.KEY_LEFT)  # must not raise
        assert p.config["volume"] == 95

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


def _row(key):
    return [spec["key"] for spec in SETTINGS_SPEC].index(key)


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
        assert p._settings_cursor == _row("progress_bar_width")

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
        assert p.config["quality"] == "LOSSLESS"

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


class TestVolumeAboveUnity:
    def _player(self, audio=None):
        p = HeadlessTidalPlayer()
        p._mode = p.MODE_SETTINGS
        p._settings_cursor = _row("volume")
        p.audio = audio
        return p

    def test_mpv_may_be_taken_to_250(self, config_file):
        p = self._player(_FakeAudio())
        for _ in range(40):
            p._handle_settings_key(player_mod.KEY_RIGHT)
        assert p.config["volume"] == 250
        assert p.audio.volumes[-1] == 250

    def test_ffplay_is_capped_at_what_it_can_do(self, config_file):
        """ffplay's -volume is 0=min 100=max and it says so on stderr, so the
        row must stop there rather than pretend."""
        audio = _FakeAudio(ceiling=100)
        audio.player_cmd = "ffplay"
        p = self._player(audio)
        for _ in range(40):
            p._handle_settings_key(player_mod.KEY_RIGHT)
        assert p.config["volume"] == 100

    def test_typing_a_number_respects_the_backend_ceiling(self, config_file):
        audio = _FakeAudio(ceiling=100)
        audio.player_cmd = "ffplay"
        p = self._player(audio)
        for digit in "250":
            p._handle_settings_key(digit)
        p._handle_settings_key(player_mod.KEY_ENTER)
        assert p.config["volume"] == 100

    def test_the_value_reads_as_a_percentage(self, config_file):
        p = self._player(_FakeAudio())
        assert "100%" in p._build_settings_display().plain

    def test_the_caution_starts_at_105(self, config_file):
        p = self._player(_FakeAudio())
        p.config["volume"] = 100
        assert "quality suffers" not in p._build_settings_display().plain
        p.config["volume"] = 105
        assert "quality suffers" in p._build_settings_display().plain

    def test_the_caution_is_blue_not_dim(self, config_file):
        """Deliberately louder than the page's dim idiom — the owner asked for
        it to be noticed."""
        p = self._player(_FakeAudio())
        p.config["volume"] = 150
        rendered = p._build_settings_display()
        styles = {str(span.style) for span in rendered.spans
                  if "quality suffers" in rendered.plain[span.start:span.end]}
        assert styles == {"blue"}

    def test_ffplay_says_it_caps(self, config_file):
        audio = _FakeAudio(ceiling=100)
        audio.player_cmd = "ffplay"
        p = self._player(audio)
        assert "ffplay caps at 100%" in p._build_settings_display().plain

    def test_mpv_says_nothing_because_it_caps_at_nothing(self, config_file):
        p = self._player(_FakeAudio())
        assert "caps at" not in p._build_settings_display().plain

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
        monkeypatch.setattr(audio, "_start_download", lambda *a: None)
        audio.play_url("https://cdn.example/t.mp4")
        assert f"--volume-max={player_mod.VOLUME_MAX}" in spawned["cmd"]
        assert "--volume=250" in spawned["cmd"]


class TestVolumeCeilingIsDurable:
    """The volume and the backend are chosen independently — the setting is
    saved on one run and the backend is discovered on the next. These are the
    ways the two can disagree."""

    def _player(self, audio=None):
        p = HeadlessTidalPlayer()
        p._mode = p.MODE_SETTINGS
        p._settings_cursor = _row("volume")
        p.audio = audio
        return p

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
        assert "100%" in p._build_settings_display().plain

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
        assert "100%" in p._build_settings_display().plain

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
            rendered = p._build_settings_display().plain
            assert not ("quality suffers" in rendered and "caps at" in rendered), cmd
            console = Console(width=80)
            with console.capture() as cap:
                console.print(p._build_display())
            assert all(len(line) <= 80 for line in cap.get().splitlines()), cmd

    def test_the_row_never_shows_a_value_the_backend_cannot_reach(self, config_file):
        audio = _FakeAudio(ceiling=100)
        audio.player_cmd = "ffplay"
        p = self._player(audio)
        p._clamp_volume_to_backend()
        for _ in range(40):
            p._handle_settings_key(player_mod.KEY_RIGHT)
        rendered = p._build_settings_display().plain
        assert "100%" in rendered
        assert "250%" not in rendered
        assert "ffplay caps at 100%" in rendered
        assert max(audio.volumes) == 100


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
