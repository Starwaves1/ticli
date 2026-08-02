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
        # At the end of the track, which is what "simply ended" means — the
        # clock is now what tells an ending apart from a dead stream
        p = _make_player(position=199.0)
        p.audio = self._audio(tmp_path, exists=True)  # file is fine, track ended

        self._monitor_once(p)

        assert p._queue_index == 2


class TestTruncatedStream:
    """A stream that dies mid-track must not look like a track that finished.

    Measured against a loopback HLS server returning `403 Request has expired`
    from segment 3: **both** mpv and ffplay play their buffer out and exit `0`
    with an empty stderr, 12.1 s into a 176 s track. `failure()` reads exit 0
    as end-of-track — correctly, by its own contract — so the monitor used to
    advance, and the user heard twelve seconds of a three-minute song with
    nothing said. Reachable by pausing longer than the ~1 h signed-URL life,
    or by any network blip. ai/INCIDENTS #3 by another door.
    """

    def _audio(self):
        """A backend whose process is gone, cleanly, with nothing to report —
        which is exactly what both of them do on a dead stream."""
        audio = AudioPlayer("mpv", cache=None)
        assert audio.failure() is None
        assert audio.source_vanished() is False
        return audio

    def _monitor_once(self, p, until):
        import threading
        import time

        p.running = True
        thread = threading.Thread(target=p._monitor_playback, daemon=True)
        thread.start()
        deadline = time.time() + 3
        while time.time() < deadline and not until():
            time.sleep(0.02)
        p.running = False
        thread.join(timeout=2)

    def test_the_queue_does_not_advance_and_the_user_is_told(self):
        p = _make_player(position=12.1)   # of a 200s track
        p.audio = self._audio()

        self._monitor_once(p, lambda: not p._playing)

        assert p._queue_index == 1, "a dead stream must not advance the queue"
        assert p._plays == [], "and must not silently restart it either"
        assert p._playing is False
        assert "stopped early" in (p._toast or "")
        # The honest part: where it stopped and how long the track was
        assert "0:12" in p._toast and "3:20" in p._toast

    def test_the_position_is_kept_so_space_resumes_where_it_died(self):
        p = _make_player(position=12.1)
        p.audio = self._audio()

        self._monitor_once(p, lambda: not p._playing)

        assert p._get_position() == pytest.approx(12.1, abs=1.0)

    def test_an_unknown_duration_advances_exactly_as_before(self):
        """The opposite of _track_has_time_left's rule, deliberately: here a
        wrong "yes" stops the queue on a track that really did end."""
        p = _make_player(position=12.1)
        p._current_track.duration = 0
        p.audio = self._audio()

        self._monitor_once(p, lambda: p._queue_index != 1)

        assert p._queue_index == 2

    def test_a_second_short_of_the_end_is_an_ending_not_a_failure(self):
        """TIDAL's duration is metadata and disagrees with the audio by a
        second or two; that must not stop the queue."""
        p = _make_player(position=198.0)
        p.audio = self._audio()

        self._monitor_once(p, lambda: p._queue_index != 1)

        assert p._queue_index == 2


class TestTheVolumeOverlay:
    """`v` is reachable from every screen, which is the point of it — the
    volume is the one setting you reach for in the middle of a track, and
    walking to the settings page to find it was the thing being fixed. So the
    interesting question is not that it opens, but everywhere it must not."""

    MODES_THAT_OPEN = (
        HeadlessTidalPlayer.MODE_PLAYER,
        HeadlessTidalPlayer.MODE_BROWSE,
        HeadlessTidalPlayer.MODE_ARTIST,
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


class TestTheVolumeOverlayAgainstTheOtherArrowKeys:
    """Three things want the arrow keys: prev/next, scrubbing, and the overlay.
    Only one of them may have them at a time, and handing them back has to end
    up where they came from."""

    def _player(self, focused=False):
        p = HeadlessTidalPlayer()
        p.audio = _FakeAudio()
        p.audio.player_cmd = "mpv"
        p.audio.volume_ceiling = lambda: 250   # room to go up as well as down
        p._current_track = _fake_track(1)
        p._queue = [p._current_track]
        p._playing = True
        if focused:
            p._focus_player()
            assert p._player_focus is True
        return p

    def test_v_takes_the_arrows_off_the_scrub(self):
        """Both want ←/→. A volume bar on screen while ←/→ still seeks would
        leave the same two keys meaning two things at once."""
        p = self._player(focused=True)
        p._handle_key("v")
        assert p._volume_open is True
        assert p._player_focus is False

    def test_closing_gives_the_arrows_back_to_the_scrub(self):
        """They were on loan. You were scrubbing, you turned it down, Enter
        puts you back where you were rather than making you press ↑ again."""
        p = self._player(focused=True)
        p._handle_key("v")
        p._handle_key(player_mod.KEY_ENTER)
        assert p._volume_open is False
        assert p._player_focus is True

    def test_closing_from_an_unfocused_player_leaves_prev_next_alone(self):
        p = self._player()
        p._handle_key("v")
        p._handle_key(player_mod.KEY_ESC)
        assert p._player_focus is False, "nothing was on loan, nothing comes back"

    def test_the_arrows_move_the_volume_not_the_track_while_it_is_open(self):
        p = self._player(focused=True)
        seeks = []
        p._seek_by = lambda step: seeks.append(step)
        p._handle_key("v")
        p._handle_key(player_mod.KEY_RIGHT)
        p._handle_key(player_mod.KEY_LEFT)
        assert seeks == [], "the overlay owns the arrows while it is up"
        assert p.audio.volumes == [105, 100]
        assert p.config["volume"] == 100

    def test_the_scrub_still_owns_them_when_the_overlay_is_shut(self):
        p = self._player(focused=True)
        seeks = []
        p._seek_by = lambda step: seeks.append(step)
        p._handle_key("v")
        p._handle_key(player_mod.KEY_ENTER)
        p._handle_key(player_mod.KEY_RIGHT)
        assert seeks == [player_mod.SEEK_STEP_SECONDS]

    def test_the_marker_and_the_footer_agree_about_who_has_them(self):
        """Focus is a state, so it is on screen twice on purpose: the marker on
        the progress line and the label on the footer hint."""
        p = self._player(focused=True)
        p._current_track.album = None      # _build_player_display reads it
        text = p._build_display().renderable.plain
        assert f"\u21c6 {player_mod.SEEK_STEP_SECONDS}s" in text
        assert f"seek {player_mod.SEEK_STEP_SECONDS}s" in text
        assert "prev/next" not in text
        p._handle_key("v")
        text = p._build_display().renderable.plain
        assert "\u21c6" not in text, "the arrows are the overlay's now"
        assert "Volume" in text


class TestTheVolumeOverlayAgainstTypingScreens:
    """Every screen that types a printable key is a screen where `v` is the
    letter v. There are three."""

    def _player(self):
        p = HeadlessTidalPlayer()
        p.audio = _FakeAudio()
        return p

    def test_the_new_playlist_name_prompt_types_it(self):
        p = self._player()
        p._mode = p.MODE_ADD_TO_PLAYLIST
        p._picker_new_name = ""
        p._handle_key("v")
        assert p._volume_open is False
        assert p._picker_new_name == "v"

    def test_the_picker_list_still_opens_it(self):
        """Only the prompt types; the row list underneath is a list like any
        other and keeps [v]."""
        p = self._player()
        p._mode = p.MODE_ADD_TO_PLAYLIST
        p._picker_new_name = None
        p._handle_key("v")
        assert p._volume_open is True

    def test_the_footer_offers_v_only_where_it_works(self):
        p = self._player()
        for mode, typing, wanted in (
            (p.MODE_PLAYER, None, True),
            (p.MODE_SEARCH, None, False),
            (p.MODE_ARTIST, None, True),
            (p.MODE_ADD_TO_PLAYLIST, None, True),
            (p.MODE_ADD_TO_PLAYLIST, "", False),
        ):
            p._mode = mode
            p._picker_new_name = typing
            keys = [h.key for h in p._mode_hints()]
            assert ("v" in keys) is wanted, (mode, typing, keys)
            assert p._can_open_volume() is wanted or mode != p.MODE_ADD_TO_PLAYLIST


class TestTheVolumeOverlayAndTheDownloadBox:
    """`d` opens a box and `v` opens an overlay. They are different keys
    doing different things and neither may eat the other — but only one of
    them may be on screen at a time, because both want the arrows."""

    def _player(self, mode=None):
        p = HeadlessTidalPlayer()
        p.audio = _FakeAudio()
        p._current_track = _fake_track(1)
        p._download_track = p._current_track
        if mode is not None:
            p._mode = mode
        return p

    def test_d_opens_the_download_box_and_v_does_not(self):
        p = self._player()
        p._handle_key("d")
        assert p._download_open is True
        assert p._volume_open is False
        # and it is an overlay, not a place: the screen underneath is where
        # it was, and nothing was pushed onto the history to get back from
        assert p._mode == p.MODE_PLAYER
        assert p._nav_history == []

    def test_v_does_not_open_underneath_the_download_box(self):
        """The box is modal. Two overlays wanting the same arrows at once is
        the one thing the volume overlay's own rule already forbids."""
        p = self._player()
        p._handle_key("d")
        p._handle_key("v")
        assert p._volume_open is False
        assert p._download_open is True
        assert p._can_open_volume() is False

    def test_the_box_passes_no_key_through_to_the_screen_underneath(self):
        """A key aimed at a box in the way must not act on what it covers.
        `x` cancels a download here; underneath, in the queue, it removes a
        track — and a removal you did not ask for is unrecoverable."""
        p = self._player(HeadlessTidalPlayer.MODE_QUEUE)
        p._queue = [_fake_track(1), _fake_track(2)]
        p._queue_cursor = 0
        p._handle_key("d")
        p._handle_key("q")
        p._handle_key("s")
        assert p._mode == p.MODE_QUEUE and p._download_open is True
        p._handle_key("x")
        assert len(p._queue) == 2, "x reached the queue underneath"
        p._handle_key(player_mod.KEY_ESC)
        assert p._download_open is False

    def test_a_running_job_keeps_its_progress_on_the_box(self):
        p = self._player()
        p._handle_key("d")
        p._download_job = {"state": "running", "tier": "HIGH", "track_id": 1,
                           "done": 1024, "total": 4096}
        text = "".join(row.plain for row in
                       p._build_download_overlay(player_mod.ROOMY_FIT))
        assert "25%" in text
        assert "[x] cancel" in text
        assert p._download_job["state"] == "running"

    def test_x_cancels_only_a_job_for_the_track_on_the_box(self):
        """The one job may be for something else entirely — the box is not
        allowed to report, or cancel, a download it is not about."""
        p = self._player()
        p._handle_key("d")
        p._download_job = {"state": "running", "tier": "LOW", "track_id": 99,
                           "done": 0, "total": 0}
        text = "".join(row.plain for row in
                       p._build_download_overlay(player_mod.ROOMY_FIT))
        assert "[x] cancel" not in text, text
        assert "[Enter] download" in text

    def test_the_box_carries_its_own_keys_instead_of_a_footer(self):
        p = self._player()
        p._handle_key("d")
        assert p._mode_hints() == []
        text = "".join(row.plain for row in
                       p._build_download_overlay(player_mod.ROOMY_FIT))
        assert "[Enter] download" in text and "[Esc] cancel" in text

    def test_d_is_not_a_key_in_search(self):
        """Search types every printable key, `d` included — same rule as `v`."""
        p = self._player(HeadlessTidalPlayer.MODE_SEARCH)
        p._search_query = ""
        p._handle_key("d")
        assert p._mode == p.MODE_SEARCH
        assert p._search_query == "d"
