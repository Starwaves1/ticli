"""Tests for player controls that aren't tied to one screen.

Covers smart previous (back restarts the track once you're 30s in), the volume
setting reaching both audio backends, and the account UI living in settings
rather than on the player screen. No TIDAL session or real player process.
"""

import types

import pytest

from ticli import player as player_mod
from ticli.player import AudioPlayer, HeadlessTidalPlayer
from ticli.utils import config as config_mod


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    """Keep every player built here off the real ~/.config/ticli."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", path)
    return path


def _fake_track(tid, duration=200):
    return types.SimpleNamespace(id=tid, name=f"Track {tid}", duration=duration, artists=[])


class _FakeAudio:
    """Stands in for AudioPlayer: records seeks/volume, no process involved."""

    def __init__(self, seek_ok=True):
        self.seek_ok = seek_ok
        self.seeks = 0
        self.volumes = []

    def seek_to_start(self):
        self.seeks += 1
        return self.seek_ok

    def set_volume(self, value):
        self.volumes.append(value)


def _make_player(position=0.0, playing=True, seek_ok=True, index=1):
    p = HeadlessTidalPlayer()
    p._queue = [_fake_track(1), _fake_track(2), _fake_track(3)]
    p._queue_index = index
    p._current_track = p._queue[index]
    p._playing = playing
    p._play_offset = position
    p.audio = _FakeAudio(seek_ok)
    p._plays = []
    p._play_track = lambda track, seek=0: p._plays.append((track.id, seek))
    return p


class TestSmartPrevious:
    """Past the threshold, back means "start this song over"."""

    def test_late_prev_seeks_current_track_to_zero(self):
        p = _make_player(position=45.0)
        p._prev_track()
        assert p.audio.seeks == 1
        assert p._plays == []            # no respawn — mpv just seeked
        assert p._queue_index == 1       # same track
        assert p._play_offset == 0
        assert p._play_start_time is not None
        assert p._get_position() < 1

    def test_early_prev_goes_to_previous_track(self):
        p = _make_player(position=10.0)
        p._prev_track()
        assert p._queue_index == 0
        assert p._plays == [(1, 0)]
        assert p.audio.seeks == 0

    def test_threshold_is_exclusive(self):
        """Exactly 30s still counts as early — only past it do we restart."""
        p = _make_player(position=float(player_mod.PREV_RESTART_SECONDS))
        p._prev_track()
        assert p._queue_index == 0

    def test_falls_back_to_replay_when_seek_fails(self):
        """Dead or unresponsive mpv, or ffplay, which has no runtime control."""
        p = _make_player(position=45.0, seek_ok=False)
        p._prev_track()
        assert p._plays == [(2, 0)]
        assert p._queue_index == 1

    def test_paused_track_restarts_by_replaying(self):
        p = _make_player(position=45.0, playing=False)
        p._prev_track()
        assert p.audio.seeks == 0        # never seek a stopped/paused process
        assert p._plays == [(2, 0)]

    def test_restarts_first_track_instead_of_doing_nothing(self):
        p = _make_player(position=45.0, index=0)
        p._prev_track()
        assert p.audio.seeks == 1
        assert p._queue_index == 0

    def test_early_prev_on_first_track_is_a_no_op(self):
        p = _make_player(position=10.0, index=0)
        p._prev_track()
        assert p._plays == []
        assert p.audio.seeks == 0

    def test_no_current_track_is_a_no_op(self):
        p = _make_player(position=45.0)
        p._current_track = None
        p._queue = []
        p._queue_index = -1
        p._prev_track()
        assert p._plays == []
        assert p.audio.seeks == 0

    def test_media_key_prev_gets_the_same_behavior(self):
        """The macOS PREV key routes through _prev_track, so it must match."""
        p = _make_player(position=45.0)
        p._handle_media_key("prev")
        assert p.audio.seeks == 1
        assert p._queue_index == 1

        p = _make_player(position=10.0)
        p._handle_media_key("prev")
        assert p._queue_index == 0

    def test_position_uses_live_playback_clock(self):
        """A track played past the threshold restarts even with offset 0."""
        p = _make_player(position=0.0)
        p._play_start_time = player_mod.time.time() - 45
        p._prev_track()
        assert p.audio.seeks == 1


class _FakeProc:
    def __init__(self, alive=True):
        self._alive = alive

    def poll(self):
        return None if self._alive else 0


class TestAudioPlayerVolume:
    """Volume reaches both backends: live on mpv, at spawn everywhere."""

    def _recording_popen(self, monkeypatch):
        spawned = []

        def _popen(cmd, **kwargs):
            spawned.append(cmd)
            return _FakeProc()

        monkeypatch.setattr(player_mod.subprocess, "Popen", _popen)
        return spawned

    def test_mpv_spawns_with_configured_volume(self, monkeypatch):
        spawned = self._recording_popen(monkeypatch)
        AudioPlayer("mpv", volume=40).play_url("http://stream")
        assert "--volume=40" in spawned[-1]

    def test_ffplay_spawns_with_configured_volume(self, monkeypatch):
        spawned = self._recording_popen(monkeypatch)
        AudioPlayer("ffplay", volume=40).play_url("http://stream")
        ffplay_cmd = [c for c in spawned if c[0] == "ffplay"][-1]
        assert ffplay_cmd[ffplay_cmd.index("-volume") + 1] == "40"
        # ffplay wants its options before the input
        assert ffplay_cmd.index("-volume") < ffplay_cmd.index("http://stream")

    def test_ffplay_cache_resume_keeps_volume(self, monkeypatch):
        spawned = self._recording_popen(monkeypatch)
        audio = AudioPlayer("ffplay", volume=40)
        audio._cache_file = "/tmp/nope.flac"
        audio._play_from_cache(12.0)
        assert spawned[-1][spawned[-1].index("-volume") + 1] == "40"

    def test_default_volume_is_full(self, monkeypatch):
        spawned = self._recording_popen(monkeypatch)
        AudioPlayer("mpv").play_url("http://stream")
        assert "--volume=100" in spawned[-1]

    def test_set_volume_pushes_to_running_mpv(self):
        audio = AudioPlayer("mpv")
        audio._ipc_path = "/tmp/fake.sock"
        audio._process = _FakeProc()
        sent = []
        audio._mpv_request = lambda cmd, timeout=0.5: (
            sent.append(cmd), {"error": "success"})[1]

        audio.set_volume(35)
        assert audio.volume == 35
        assert sent == [{"command": ["set_property", "volume", 35]}]

    def test_set_volume_on_ffplay_only_stores_it(self):
        audio = AudioPlayer("ffplay")
        audio._process = _FakeProc()
        audio.set_volume(35)
        assert audio.volume == 35  # applied on the next spawn

    def test_set_volume_with_no_process_only_stores_it(self):
        audio = AudioPlayer("mpv")
        audio._ipc_path = "/tmp/fake.sock"
        sent = []
        audio._mpv_request = lambda cmd, timeout=0.5: sent.append(cmd)
        audio.set_volume(35)
        assert audio.volume == 35
        assert sent == []


class TestAudioPlayerSeekToStart:
    def _mpv(self, error="success", alive=True, paused=False):
        audio = AudioPlayer("mpv")
        audio._ipc_path = "/tmp/fake.sock"
        audio._process = _FakeProc(alive)
        audio._paused = paused
        audio._seek_offset = 45.0
        audio.sent = []
        audio._mpv_request = lambda cmd, timeout=0.5: (
            audio.sent.append(cmd), {"error": error})[1]
        return audio

    def test_seeks_absolute_zero(self):
        audio = self._mpv()
        assert audio.seek_to_start() is True
        assert audio.sent == [{"command": ["seek", 0, "absolute"]}]
        assert audio._seek_offset == 0

    def test_unacknowledged_seek_fails(self):
        audio = self._mpv(error="property unavailable")
        assert audio.seek_to_start() is False
        assert audio._seek_offset == 45.0  # bookkeeping untouched

    def test_dead_process_fails(self):
        audio = self._mpv(alive=False)
        assert audio.seek_to_start() is False
        assert audio.sent == []

    def test_paused_process_fails(self):
        audio = self._mpv(paused=True)
        assert audio.seek_to_start() is False

    def test_ffplay_cannot_seek(self):
        audio = AudioPlayer("ffplay")
        audio._process = _FakeProc()
        assert audio.seek_to_start() is False


class TestAccountUIMovedToSettings:
    """Logout and "logged in as" belong to the settings page now."""

    def _player(self):
        p = HeadlessTidalPlayer()
        p._user_display_name = "Ada Lovelace"
        return p

    def test_o_does_nothing_in_player_mode(self):
        p = self._player()
        p._handle_player_key("o")
        assert p._logout_pending is False

    def test_player_footer_has_no_account_lines(self):
        p = self._player()
        p._show_more = True
        text = p._build_display().renderable.plain
        assert "logout" not in text
        assert "Logged in as" not in text

    def test_settings_page_shows_account_and_hint(self):
        p = self._player()
        text = p._build_settings_display().plain
        assert "Logged in as" in text
        assert "Ada Lovelace" in text
        assert "[o]" in text

    def test_settings_page_before_login_has_a_placeholder(self):
        p = self._player()
        p._user_display_name = ""
        assert "Logged in as" in p._build_settings_display().plain

    def test_settings_footer_offers_logout(self):
        p = self._player()
        p._mode = p.MODE_SETTINGS
        assert "log out" in p._build_display().renderable.plain

    def test_o_in_settings_asks_for_confirmation(self):
        p = self._player()
        p._mode = p.MODE_SETTINGS
        p._handle_settings_key("o")
        assert p._logout_pending is True
        assert p._mode == p.MODE_SETTINGS  # stays put until answered
        assert "Log out" in p._build_display().renderable.plain

    def test_confirming_logs_out(self):
        p = self._player()
        p._mode = p.MODE_SETTINGS
        p._handle_settings_key("o")
        calls = []
        p._logout = lambda: calls.append("logout")
        p._handle_key("y")
        assert calls == ["logout"]
        assert p._logout_pending is False

    def test_any_other_key_cancels(self):
        p = self._player()
        p._mode = p.MODE_SETTINGS
        p._handle_settings_key("o")
        calls = []
        p._logout = lambda: calls.append("logout")
        p._handle_key(player_mod.KEY_ESC)
        assert calls == []
        assert p._logout_pending is False


class TestDeletedCacheFile:
    """A cached track deleted from under the player.

    Checking the file exists and then opening it are two operations, and in
    between the user can delete it. The player exits at once, which looks
    exactly like end-of-track — so the song was silently skipped. It should
    be started again from the network instead.
    """

    def _audio(self, tmp_path, persistent=True, exists=True):
        audio = AudioPlayer("mpv", cache=None)
        path = tmp_path / "12.m4a"
        if exists:
            path.write_bytes(b"whole track")
        audio._cache_file = str(path)
        audio._cache_persistent = persistent
        return audio

    def test_a_deleted_cache_file_is_noticed(self, tmp_path):
        assert self._audio(tmp_path, exists=False).source_vanished() is True

    def test_a_file_that_is_still_there_is_not(self, tmp_path):
        assert self._audio(tmp_path).source_vanished() is False

    def test_streaming_from_a_url_is_never_a_vanished_file(self, tmp_path):
        # A scratch copy isn't what's playing — the URL is
        audio = self._audio(tmp_path, persistent=False, exists=False)
        assert audio.source_vanished() is False
        assert AudioPlayer("mpv", cache=None).source_vanished() is False

    def _monitor_once(self, p):
        """Two dead polls is what the monitor requires before it acts."""
        import threading
        import time

        p.running = True
        thread = threading.Thread(target=p._monitor_playback, daemon=True)
        thread.start()
        deadline = time.time() + 3
        while time.time() < deadline and not (p._plays or p._queue_index != 1):
            time.sleep(0.02)
        p.running = False
        thread.join(timeout=2)

    def test_the_track_is_restarted_where_it_was_not_skipped(self, tmp_path):
        p = _make_player(position=45.0)
        p.audio = self._audio(tmp_path, exists=False)

        self._monitor_once(p)

        assert p._plays and p._plays[0][0] == 2, "the vanished track was skipped"
        assert p._plays[0][1] == pytest.approx(45.0, abs=1.0)
        assert p._queue_index == 1

    def test_a_track_that_simply_ended_still_advances(self, tmp_path):
        p = _make_player(position=45.0)
        p.audio = self._audio(tmp_path, exists=True)  # file is fine, track ended

        self._monitor_once(p)

        assert p._queue_index == 2


class TestTheVolumeOverlay:
    """`v` is reachable from every screen, which is the point of it — the
    volume is the one setting you reach for in the middle of a track, and
    walking to the settings page to find it was the thing being fixed. So the
    interesting question is not that it opens, but everywhere it must not."""

    MODES_THAT_OPEN = (
        HeadlessTidalPlayer.MODE_PLAYER,
        HeadlessTidalPlayer.MODE_BROWSE,
        HeadlessTidalPlayer.MODE_QUEUE,
        HeadlessTidalPlayer.MODE_PLAYLISTS,
        HeadlessTidalPlayer.MODE_ADD_TO_PLAYLIST,
        HeadlessTidalPlayer.MODE_SETTINGS,
    )

    def _player(self, mode=None):
        p = HeadlessTidalPlayer()
        p.audio = _FakeAudio()
        if mode is not None:
            p._mode = mode
        return p

    @pytest.mark.parametrize("mode", MODES_THAT_OPEN)
    def test_v_opens_it_from_every_mode_that_is_not_typing(self, mode):
        p = self._player(mode)
        p._handle_key("v")
        assert p._volume_open is True
        assert p._mode == mode, "the overlay is over the screen, not a place you go"

    def test_search_types_a_v_instead_of_opening_it(self):
        """Search mode consumes every printable key, so `v` there is the
        letter v and nothing else — an overlay stealing it would make the
        query untypeable."""
        p = self._player(HeadlessTidalPlayer.MODE_SEARCH)
        p._search_query = ""
        p._handle_key("v")
        assert p._volume_open is False
        assert p._search_query == "v"

    def test_a_settings_row_being_typed_into_keeps_its_keys(self):
        p = self._player(HeadlessTidalPlayer.MODE_SETTINGS)
        p._settings_cursor = 1          # a number row: Songs per page
        p._handle_key("2")
        assert p._settings_edit == "2", "the row is being typed into"
        p._handle_key("v")
        assert p._volume_open is False

    def test_arrows_move_it_and_apply_it_live(self):
        p = self._player()
        p._handle_key("v")
        p._handle_key(player_mod.KEY_LEFT)
        assert p.config["volume"] == 95
        assert p.audio.volumes == [95]
        p._handle_key(player_mod.KEY_RIGHT)
        assert p.config["volume"] == 100

    def test_enter_and_esc_and_v_all_close_it(self):
        for key in (player_mod.KEY_ENTER, player_mod.KEY_ENTER2,
                    player_mod.KEY_ESC, "v"):
            p = self._player()
            p._handle_key("v")
            p._handle_key(key)
            assert p._volume_open is False, key

    def test_closing_it_does_not_also_do_what_the_key_means_underneath(self):
        """Esc on the player screen asks to quit. Esc closing the overlay must
        not also arm that — the overlay takes the key, it does not pass it on."""
        p = self._player()
        p._handle_key("v")
        p._handle_key(player_mod.KEY_ESC)
        assert p._quit_pending is False

    def test_it_swallows_the_keys_that_would_have_changed_screen(self):
        p = self._player()
        p._handle_key("v")
        for key in ("q", "p", "s", "c", "t", "m"):
            p._handle_key(key)
            assert p._mode == p.MODE_PLAYER, key
            assert p._volume_open is True, key

    def test_space_still_works_because_reaching_for_the_volume_is_not_a_stop(self):
        p = self._player()
        toggles = []
        p._toggle_play_key = lambda: toggles.append(1)
        p._handle_key("v")
        p._handle_key(" ")
        assert toggles == [1]
        assert p._volume_open is True

    def test_the_backend_ceiling_still_applies(self):
        p = self._player()
        p.audio.player_cmd = "ffplay"
        p.audio.volume_ceiling = lambda: 100
        p.config["volume"] = 90
        p._handle_key("v")
        for _ in range(40):
            p._handle_key(player_mod.KEY_RIGHT)
        assert p.config["volume"] == 100
        assert max(p.audio.volumes) == 100
