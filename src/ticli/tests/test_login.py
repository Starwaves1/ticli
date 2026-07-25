"""Tests for the two login flows, the tokens they leave behind, and the quality
tiers each one is actually served.

The failure this guards against is quiet and delayed: a stored token that has
forgotten which flow issued it refreshes against the wrong TIDAL client, and the
session dies hours later looking like a random logout. So the token-store
migration and both flows' refresh paths are covered directly, against a real
tidalapi Session with its HTTP layer stubbed — no network, no keychain, and
nothing that touches a real session file.
"""

import json
import types

import pytest
import tidalapi

from ticli import player as player_mod
from ticli.player import HeadlessTidalPlayer
from ticli.utils import config as config_mod
from ticli.utils import credential_store as store


@pytest.fixture(autouse=True)
def no_browser(monkeypatch):
    """A test must never open the real login page in someone's browser."""
    monkeypatch.setattr(HeadlessTidalPlayer, "_open_browser", lambda self, url: None)


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    """Point the config module at a throwaway directory, so building a player
    reads the defaults rather than whatever the person running the tests has
    their quality set to."""
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "config" / "config.json")


@pytest.fixture
def token_file(tmp_path, monkeypatch):
    """Point the credential store at a throwaway file, with the keychain out of
    the picture — a test must never read or write the real one."""
    path = tmp_path / "session.json"
    monkeypatch.setattr(store, "keyring", None)
    monkeypatch.setattr(store, "FALLBACK_DIR", tmp_path)
    monkeypatch.setattr(store, "FALLBACK_FILE", path)
    return path


def _stored(**overrides):
    record = {
        "token_type": "Bearer",
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "expiry_time": "2026-07-25T12:00:00",
    }
    record.update(overrides)
    return record


class TestTokenStoreMigration:
    def test_old_record_without_flag_reads_as_device_flow(self, token_file):
        # Exactly what every ticli before PKCE wrote
        token_file.write_text(json.dumps(_stored()))
        data = store.load_tokens()
        assert data["is_pkce"] is False
        assert data["access_token"] == "access-1"
        assert data["refresh_token"] == "refresh-1"

    def test_migration_does_not_rewrite_the_stored_file(self, token_file):
        raw = json.dumps(_stored())
        token_file.write_text(raw)
        store.load_tokens()
        # Reading is not a write: an older ticli must still find its session
        assert token_file.read_text() == raw

    def test_pkce_flag_survives_a_round_trip(self, token_file):
        store.save_tokens(_stored(is_pkce=True))
        data = store.load_tokens()
        assert data["is_pkce"] is True
        assert data["version"] == store.TOKEN_VERSION

    def test_saved_record_always_carries_the_flag(self, token_file):
        store.save_tokens(_stored())
        on_disk = json.loads(token_file.read_text())
        assert on_disk["is_pkce"] is False
        assert on_disk["version"] == store.TOKEN_VERSION

    def test_truthy_flag_is_normalised_to_a_bool(self, token_file):
        token_file.write_text(json.dumps(_stored(is_pkce="yes")))
        assert store.load_tokens()["is_pkce"] is True

    def test_record_without_an_access_token_is_nothing_stored(self, token_file):
        token_file.write_text(json.dumps({"token_type": "Bearer"}))
        assert store.load_tokens() is None

    def test_corrupt_record_is_nothing_stored(self, token_file):
        token_file.write_text("{ not json")
        assert store.load_tokens() is None

    def test_missing_file_is_nothing_stored(self, token_file):
        assert store.load_tokens() is None


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.ok = status_code == 200
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _CapturingPost:
    """Stands in for tidalapi's requests session, recording what it was asked to
    post so the refresh credentials can be inspected."""

    def __init__(self):
        self.calls = []

    def post(self, url, data=None, **kwargs):
        self.calls.append((url, dict(data or {})))
        return _FakeResponse({
            "access_token": "access-2",
            "expires_in": 86400,
            "token_type": "Bearer",
        })


def _session_with_capture():
    session = tidalapi.Session()
    capture = _CapturingPost()
    session.request_session = capture
    return session, capture


class TestRefreshUsesTheIssuingClient:
    """tidalapi picks the refresh client from session.is_pkce alone, so this is
    the whole of what a restored session has to get right."""

    def test_device_session_refreshes_against_the_device_client(self):
        session, capture = _session_with_capture()
        session.is_pkce = False
        assert session.token_refresh("refresh-1") is True
        _, data = capture.calls[-1]
        assert data["client_id"] == session.config.client_id
        assert data["client_secret"] == session.config.client_secret

    def test_pkce_session_refreshes_against_the_pkce_client(self):
        session, capture = _session_with_capture()
        session.is_pkce = True
        assert session.token_refresh("refresh-1") is True
        _, data = capture.calls[-1]
        assert data["client_id"] == session.config.client_id_pkce
        assert data["client_secret"] == session.config.client_secret_pkce

    def test_the_two_clients_are_actually_different(self):
        # If these ever converged the flag would be pointless — and so would
        # the whole feature, since it is the client that carries entitlement
        config = tidalapi.Session().config
        assert config.client_id != config.client_id_pkce


class _FakeSession:
    """Enough of a tidalapi Session for _login() to run against."""

    def __init__(self, logged_in=True):
        self.is_pkce = False
        self.loaded = None
        self.token_type = "Bearer"
        self.access_token = "access-1"
        self.refresh_token = "refresh-1"
        self.expiry_time = None
        self.audio_quality = None
        self._logged_in = logged_in
        self.user = types.SimpleNamespace(id=1, username="garrett")
        self.config = types.SimpleNamespace(
            pkce_uri_redirect="https://tidal.com/android/login/auth",
        )

    def load_oauth_session(self, token_type, access_token, refresh_token=None,
                           expiry_time=None, is_pkce=False):
        self.loaded = {
            "token_type": token_type,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expiry_time": expiry_time,
            "is_pkce": is_pkce,
        }
        self.is_pkce = is_pkce
        return True

    def check_login(self):
        return self._logged_in


def _player(session=None):
    player = HeadlessTidalPlayer()
    player.session = session or _FakeSession()
    return player


class TestLoginFromStoredTokens:
    def test_device_tokens_restore_a_device_session(self, token_file):
        token_file.write_text(json.dumps(_stored()))
        session = _FakeSession()
        assert _player(session)._login() is True
        assert session.loaded["is_pkce"] is False
        assert session.is_pkce is False

    def test_pkce_tokens_restore_a_pkce_session(self, token_file):
        token_file.write_text(json.dumps(_stored(is_pkce=True)))
        session = _FakeSession()
        assert _player(session)._login() is True
        assert session.loaded["is_pkce"] is True
        assert session.is_pkce is True

    def test_a_refresh_on_the_way_in_is_written_back(self, token_file):
        token_file.write_text(json.dumps(_stored()))
        session = _FakeSession()
        # What tidalapi does when the stored access token had already expired
        original_load = session.load_oauth_session

        def _load(*args, **kwargs):
            result = original_load(*args, **kwargs)
            session.access_token = "access-refreshed"
            return result

        session.load_oauth_session = _load
        assert _player(session)._login() is True
        assert json.loads(token_file.read_text())["access_token"] == "access-refreshed"

    def test_an_unchanged_token_is_not_rewritten(self, token_file):
        raw = json.dumps(_stored())
        token_file.write_text(raw)
        assert _player()._login() is True
        assert token_file.read_text() == raw


class _PkceSession(_FakeSession):
    """Records the PKCE exchange the way a real session would receive it."""

    def __init__(self, accept="good-code"):
        super().__init__()
        self._accept = accept
        self.redirects = []
        self.processed = []

    def pkce_login_url(self):
        return "https://login.tidal.com/authorize?client_id=abc&code_challenge=xyz"

    def pkce_get_auth_token(self, url_redirect):
        self.redirects.append(url_redirect)
        # Mirrors tidalapi: anything without a scheme is rejected outright
        if "https://" not in url_redirect:
            raise Exception("The provided redirect url looks wrong: " + url_redirect)
        if self._accept not in url_redirect:
            raise Exception("Wrong one-time authorization code")
        return {"access_token": "pkce-access", "refresh_token": "pkce-refresh",
                "expires_in": 86400, "token_type": "Bearer"}

    def process_auth_token(self, token, is_pkce_token=True):
        self.processed.append(token)
        self.access_token = token["access_token"]
        self.refresh_token = token["refresh_token"]
        self.is_pkce = is_pkce_token
        return True


def _typed(monkeypatch, *lines):
    """Feed the paste prompt a fixed script. Running out means EOF, which is
    what Ctrl-D at the prompt looks like."""
    pending = list(lines)

    def _input(prompt=""):
        if not pending:
            raise EOFError
        return pending.pop(0)

    monkeypatch.setattr("builtins.input", _input)


class TestPkceLogin:
    def test_pasted_redirect_url_completes_the_login(self, token_file, monkeypatch):
        session = _PkceSession()
        _typed(monkeypatch, "https://tidal.com/android/login/auth?code=good-code")
        assert _player(session)._login_pkce() is True
        assert session.is_pkce is True

    def test_a_bare_code_is_accepted_too(self, token_file, monkeypatch):
        # The SSH case: reading the address off a phone, only the code is worth
        # typing, so it gets wrapped back into a redirect URL for tidalapi
        session = _PkceSession()
        _typed(monkeypatch, "good-code")
        assert _player(session)._login_pkce() is True
        assert session.redirects[-1].startswith(session.config.pkce_uri_redirect)
        assert "code=good-code" in session.redirects[-1]

    def test_a_bad_paste_can_be_retried(self, token_file, monkeypatch):
        session = _PkceSession()
        _typed(monkeypatch, "not-the-code", "good-code")
        assert _player(session)._login_pkce() is True
        assert len(session.redirects) == 2

    def test_repeated_bad_pastes_give_up_without_raising(self, token_file, monkeypatch):
        session = _PkceSession()
        _typed(monkeypatch, *["no"] * player_mod.PKCE_PASTE_TRIES)
        assert _player(session)._login_pkce() is False
        assert session.is_pkce is False

    def test_abandoning_the_prompt_leaves_the_session_alone(self, token_file, monkeypatch):
        session = _PkceSession()
        _typed(monkeypatch)  # first read raises EOFError
        assert _player(session)._login_pkce() is False
        assert session.processed == []

    def test_success_stores_tokens_marked_pkce(self, token_file, monkeypatch):
        session = _PkceSession()
        player = _player(session)
        _typed(monkeypatch, "https://tidal.com/android/login/auth?code=good-code")
        assert player._login_pkce() is True
        assert player._finish_login() is True
        stored = json.loads(token_file.read_text())
        assert stored["is_pkce"] is True
        assert stored["access_token"] == "pkce-access"

    def test_a_fresh_login_defaults_to_the_device_flow(self, token_file):
        assert _player()._login_flow == "device"

    def test_the_flag_can_ask_for_pkce_up_front(self, token_file):
        assert HeadlessTidalPlayer(login_flow="pkce")._login_flow == "pkce"

    def test_an_unknown_flow_falls_back_to_the_default(self, token_file):
        assert HeadlessTidalPlayer(login_flow="carrier-pigeon")._login_flow == "device"

    def test_a_cancelled_pkce_login_is_not_downgraded_to_the_device_flow(
            self, token_file, monkeypatch):
        # Silently signing him in over AAC instead is the exact surprise the
        # whole feature exists to remove
        session = _PkceSession()
        player = _player(session)
        player._login_flow = "pkce"
        started = []
        monkeypatch.setattr(player, "_login_device", lambda: started.append(True))
        _typed(monkeypatch)
        assert player._login() is False
        assert started == []


class TestPkceUpgradeFromSettings:
    def _quiet_tui(self, player, monkeypatch):
        """The upgrade suspends the TUI; there is none in a test."""
        monkeypatch.setattr(player, "_restore_tty", lambda: None)
        monkeypatch.setattr(player, "_raw_tty", lambda: None)

    def test_upgrade_switches_the_session_and_the_stored_tokens(
            self, token_file, monkeypatch):
        token_file.write_text(json.dumps(_stored()))
        session = _PkceSession()
        player = _player(session)
        self._quiet_tui(player, monkeypatch)
        _typed(monkeypatch, "https://tidal.com/android/login/auth?code=good-code")
        player._upgrade_to_pkce()
        assert session.is_pkce is True
        assert json.loads(token_file.read_text())["is_pkce"] is True

    def test_a_cancelled_upgrade_keeps_the_old_session(self, token_file, monkeypatch):
        raw = json.dumps(_stored())
        token_file.write_text(raw)
        session = _PkceSession()
        player = _player(session)
        self._quiet_tui(player, monkeypatch)
        _typed(monkeypatch)  # EOF straight away
        player._upgrade_to_pkce()
        assert session.is_pkce is False
        assert token_file.read_text() == raw

    def test_upgrade_is_a_no_op_once_the_session_is_pkce(self, token_file, monkeypatch):
        session = _PkceSession()
        session.is_pkce = True
        player = _player(session)
        self._quiet_tui(player, monkeypatch)
        player._upgrade_to_pkce()
        assert session.processed == []

    def test_settings_page_offers_the_action_on_a_device_session(self, token_file):
        text = _player()._build_pkce_line().plain
        assert "[u]" in text
        assert "FLAC" in text

    def test_settings_page_confirms_a_pkce_session_instead(self, token_file):
        session = _FakeSession()
        session.is_pkce = True
        text = _player(session)._build_pkce_line().plain
        assert "PKCE login ✓" in text
        assert "Max quality available" in text
        assert "[u]" not in text

    def test_the_trailing_text_is_dimmed_the_way_the_page_already_dims(self, token_file):
        session = _FakeSession()
        session.is_pkce = True
        spans = _player(session)._build_pkce_line().spans
        assert [s for s in spans if s.style == "dim"], \
            "the note beside the check mark should use the page's dim style"

    def test_u_on_the_settings_page_starts_the_upgrade(self, token_file, monkeypatch):
        player = _player()
        called = []
        monkeypatch.setattr(player, "_upgrade_to_pkce", lambda: called.append(True))
        player._mode = player.MODE_SETTINGS
        player._handle_settings_key("u")
        assert called == [True]

    def test_the_upgrade_key_collides_with_nothing_else_on_the_page(self, token_file):
        # "u" must not also be logout, clear-cache, or a way out of the page
        player = _player()
        player._mode = player.MODE_SETTINGS
        cursor = player._settings_cursor
        player._handle_settings_key("u")
        assert player._logout_pending is False
        assert player._clear_cache_pending is False
        assert player._mode == player.MODE_SETTINGS
        assert player._settings_cursor == cursor


class _StreamSession(_FakeSession):
    def __init__(self, is_pkce=False):
        super().__init__()
        self.is_pkce = is_pkce


class _Manifest:
    def __init__(self, bts, urls, hls=""):
        self.is_bts = bts
        self._urls = urls
        self._hls = hls

    def get_urls(self):
        return self._urls

    def get_hls(self):
        return self._hls


class _StreamTrack:
    def __init__(self, manifest=None, granted="HIGH", tid=7):
        self.id = tid
        self._manifest = manifest or _Manifest(bts=True, urls=["https://cdn/track.m4a"])
        self._granted = granted
        self.url_calls = 0

    def get_url(self):
        self.url_calls += 1
        raise AssertionError("get_url() reports nothing about the tier granted")

    def get_stream(self):
        return types.SimpleNamespace(
            audio_quality=self._granted,
            get_stream_manifest=lambda: self._manifest,
        )


class TestStreamUrl:
    def test_a_single_file_stream_plays_straight_from_its_url(self, token_file):
        player = _player(_StreamSession())
        track = _StreamTrack(_Manifest(bts=True, urls=["https://cdn/track.flac"]))
        assert player._stream_url(track) == "https://cdn/track.flac"
        # get_url() is never used any more: it would raise on a PKCE session,
        # and on either one it hides which tier was actually granted
        assert track.url_calls == 0

    def test_a_segmented_stream_becomes_a_local_playlist(self, token_file, tmp_path, monkeypatch):
        monkeypatch.setattr(player_mod.tempfile, "gettempdir", lambda: str(tmp_path))
        player = _player(_StreamSession(is_pkce=True))
        hls = "#EXTM3U\n#EXTINF:4.0,\nhttps://cdn/seg0.mp4\n#EXT-X-ENDLIST\n"
        track = _StreamTrack(_Manifest(bts=False, urls=["https://cdn/seg0.mp4"], hls=hls))
        source = player._stream_url(track)
        assert source.endswith("7.m3u8")
        assert "https://cdn/seg0.mp4" in open(source).read()

    def test_old_playlists_are_pruned_on_write(self, tmp_path, monkeypatch):
        monkeypatch.setattr(player_mod.tempfile, "gettempdir", lambda: str(tmp_path))
        for i in range(player_mod.HLS_KEEP + 3):
            path = player_mod._write_hls_playlist(i, "#EXTM3U\n")
        directory = player_mod.Path(path).parent
        assert len(list(directory.glob("*.m3u8"))) == player_mod.HLS_KEEP

    def test_a_local_playlist_is_never_downloaded(self, tmp_path):
        # requests.get on a filesystem path would raise; the guard is what
        # keeps a segmented stream from looking like a broken download
        audio = player_mod.AudioPlayer.__new__(player_mod.AudioPlayer)
        assert audio._start_download(str(tmp_path / "7.m3u8"), 7, 0) is None


class TestEntitlementGating:
    """The settings page must not offer a tier TIDAL has already refused — and
    must not refuse one on a guess."""

    def _player_at(self, quality, session=None):
        player = _player(session or _StreamSession())
        player._quality_name = quality
        return player

    def test_nothing_is_gated_before_a_single_track_has_played(self, token_file):
        player = self._player_at("HIRES")
        assert player._quality_ceiling is None
        assert player._quality_unavailable("HIRES") is False
        assert player._quality_unavailable("LOSSLESS") is False

    def test_a_downgrade_gates_every_tier_above_what_was_granted(self, token_file):
        player = self._player_at("HIRES")
        player._stream_url(_StreamTrack(granted="HIGH"))
        assert player._quality_ceiling == "HIGH"
        assert player._quality_unavailable("HIRES") is True
        assert player._quality_unavailable("LOSSLESS") is True
        assert player._quality_unavailable("HIGH") is False
        assert player._quality_unavailable("LOW") is False

    def test_getting_what_was_asked_for_gates_nothing(self, token_file):
        # LOSSLESS granted says nothing at all about HIRES — asking is the only
        # way to find out, and guessing here would hide a tier he pays for
        player = self._player_at("LOSSLESS")
        player._stream_url(_StreamTrack(granted="LOSSLESS"))
        assert player._quality_ceiling is None
        assert player._quality_unavailable("HIRES") is False

    def test_an_unrecognised_answer_gates_nothing(self, token_file):
        player = self._player_at("HIRES")
        player._stream_url(_StreamTrack(granted="DOLBY_ATMOS_SOMETHING"))
        assert player._quality_ceiling is None
        assert player._quality_unavailable("HIRES") is False

    def test_a_missing_answer_gates_nothing(self, token_file):
        player = self._player_at("HIRES")
        player._note_granted_quality(None)
        assert player._quality_ceiling is None

    def test_better_news_lifts_an_earlier_ceiling(self, token_file):
        player = self._player_at("HIRES")
        player._stream_url(_StreamTrack(granted="HIGH"))
        assert player._quality_unavailable("LOSSLESS") is True
        player._stream_url(_StreamTrack(granted="HI_RES_LOSSLESS"))
        assert player._quality_ceiling is None
        assert player._quality_unavailable("HIRES") is False

    def test_a_successful_pkce_upgrade_re_opens_the_tiers_without_a_restart(
            self, token_file, monkeypatch):
        session = _PkceSession()
        player = _player(session)
        player._quality_name = "HIRES"
        player._stream_url(_StreamTrack(granted="HIGH"))
        assert player._quality_unavailable("HIRES") is True
        monkeypatch.setattr(player, "_restore_tty", lambda: None)
        monkeypatch.setattr(player, "_raw_tty", lambda: None)
        _typed(monkeypatch, "https://tidal.com/android/login/auth?code=good-code")
        player._upgrade_to_pkce()
        assert player._quality_ceiling is None
        assert player._quality_unavailable("HIRES") is False

    def test_a_cancelled_upgrade_leaves_the_gate_where_it_was(
            self, token_file, monkeypatch):
        session = _PkceSession()
        player = _player(session)
        player._quality_name = "HIRES"
        player._stream_url(_StreamTrack(granted="HIGH"))
        monkeypatch.setattr(player, "_restore_tty", lambda: None)
        monkeypatch.setattr(player, "_raw_tty", lambda: None)
        _typed(monkeypatch)
        player._upgrade_to_pkce()
        assert player._quality_ceiling == "HIGH"


class TestGatingOnThePage:
    def _settings_page(self, granted=None, quality="HIRES"):
        player = _player(_StreamSession())
        player._quality_name = quality
        player._settings_cursor = 0  # the quality row, where the ladder shows
        if granted:
            player._stream_url(_StreamTrack(granted=granted))
        return player

    def test_an_ungated_page_says_nothing_about_availability(self, token_file):
        page = self._settings_page()._build_settings_display().plain
        assert "isn't served" not in page

    def test_a_gated_tier_is_named_with_its_reason_not_hidden(self, token_file):
        page = self._settings_page(granted="HIGH")._build_settings_display().plain
        # Still listed — a hidden option looks like a missing feature
        assert "LOSSLESS" in page and "HIRES" in page
        assert "isn't served" in page
        assert "TIDAL sends HIGH instead" in page

    def test_the_page_points_at_the_fix_when_the_chosen_tier_is_gated(self, token_file):
        page = self._settings_page(granted="HIGH")._build_settings_display().plain
        assert "[u] fixes it" in page

    def test_a_pkce_session_is_never_gated_on_stale_evidence(self, token_file):
        player = self._settings_page(granted="HIGH")
        player.session.is_pkce = True
        player._quality_ceiling = None  # what the upgrade clears
        assert "isn't served" not in player._build_settings_display().plain

    def test_the_gate_note_is_dimmed_rather_than_shouted(self, token_file):
        player = self._settings_page(granted="HIGH")
        styles = {str(s.style) for s in player._build_quality_gate_note().spans}
        assert all("dim" in style for style in styles), styles
