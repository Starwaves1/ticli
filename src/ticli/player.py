"""Ticli - Terminal music player for TIDAL.

Uses tidalapi for TIDAL API access and ffplay/mpv for audio playback.
OAuth login via browser, session persisted to disk.
"""

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import tidalapi
except ImportError:
    print("This feature requires 'tidalapi'. Install it with: pip install tidalapi")
    sys.exit(1)

try:
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
except ImportError:
    print("This feature requires 'rich'. Install it with: pip install rich")
    sys.exit(1)


def format_time(seconds):
    if seconds is None or seconds != seconds:
        return "--:--"
    seconds = int(seconds)
    if seconds < 0:
        return "0:00"
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"


# Key constants
KEY_UP = "\x1b[A"
KEY_DOWN = "\x1b[B"
KEY_RIGHT = "\x1b[C"
KEY_LEFT = "\x1b[D"
KEY_ESC = "\x1b"
KEY_ENTER = "\r"
KEY_ENTER2 = "\n"
KEY_BACKSPACE = "\x7f"
KEY_BACKSPACE2 = "\x08"

from ticli.utils.credential_store import save_tokens, load_tokens

PAGE_SIZE = 15

STATE_DIR = Path.home() / ".config" / "ticli"
STATE_FILE = STATE_DIR / "player_state.json"

AUDIO_PLAYERS = ["mpv", "ffplay"]

IS_MACOS = sys.platform == "darwin"

# macOS media keys (keyboard, AirPods taps, Control Center): mpv registers with
# MPRemoteCommandCenter and turns remote commands into these key names. We rebind
# them over IPC so they set a property ticli polls, instead of mpv acting on them
# itself (NEXT would otherwise end the playlist, STOP would quit mpv).
MEDIA_KEY_ACTIONS = {
    "PLAY": "toggle",       # togglePlayPause
    "PLAYPAUSE": "toggle",
    "PLAYONLY": "play",     # play
    "PAUSEONLY": "pause",   # pause
    "STOP": "pause",        # stop — pause rather than let mpv quit
    "NEXT": "next",         # nextTrack
    "PREV": "prev",         # previousTrack
}
MEDIA_KEY_PROP = "user-data/ticli/media-key"


def _find_audio_player():
    """Find an available audio player binary."""
    for player in AUDIO_PLAYERS:
        for path_dir in os.environ.get("PATH", "").split(os.pathsep):
            full = os.path.join(path_dir, player)
            if os.path.isfile(full) and os.access(full, os.X_OK):
                return player
    return None


class AudioPlayer:
    """Manages audio playback via external player (mpv or ffplay).

    Supports pause/resume:
    - mpv: uses IPC socket to send pause property commands
    - ffplay: kills process on pause, restarts from cached local file on resume
    """

    def __init__(self, player_cmd: str):
        self.player_cmd = player_cmd
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._paused = False
        self._ipc_path: Optional[str] = None
        # For ffplay pause/resume: track position and local cache
        self._current_url: Optional[str] = None
        self._cache_file: Optional[str] = None
        self._cache_process: Optional[subprocess.Popen] = None
        self._play_start: Optional[float] = None
        self._seek_offset: float = 0
        # macOS media keys: rebound per mpv process, title shown in Now Playing
        self._media_keys_bound = False
        self._media_title: Optional[str] = None

    def play_url(self, url: str, seek: float = 0, title: Optional[str] = None):
        """Play an audio URL, stopping any current playback."""
        self.stop()
        with self._lock:
            self._paused = False
            self._current_url = url
            self._media_title = title
            self._media_keys_bound = False
            self._seek_offset = seek
            self._play_start = time.time()
            if self.player_cmd == "mpv":
                self._ipc_path = f"/tmp/ticli-mpv-{os.getpid()}.sock"
                try:
                    os.unlink(self._ipc_path)
                except OSError:
                    pass
                cmd = [
                    "mpv", "--no-video", "--really-quiet",
                    f"--input-ipc-server={self._ipc_path}",
                    url,
                ]
                if seek > 0:
                    cmd.insert(-1, f"--start={seek}")
            else:  # ffplay
                self._ipc_path = None
                # Download to temp file in background for instant resume
                self._cache_file = f"/tmp/ticli-cache-{os.getpid()}.flac"
                self._cache_process = subprocess.Popen(
                    ["ffmpeg", "-y", "-loglevel", "quiet", "-i", url,
                     "-c", "copy", self._cache_file],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                # Always play from the URL here — the cache has only just
                # started downloading, so it can't satisfy a seek yet
                cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet"]
                if seek > 0:
                    cmd += ["-ss", str(seek)]
                cmd.append(url)
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def _play_from_cache(self, seek: float):
        """Resume ffplay from local cached file at given position."""
        cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet",
               "-ss", str(seek), self._cache_file]
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._play_start = time.time()
        self._paused = False

    def pause(self):
        """Pause playback."""
        with self._lock:
            if not self._process or self._process.poll() is not None or self._paused:
                return
            if self.player_cmd == "mpv" and self._ipc_path:
                # The IPC socket takes ~100ms to come up after spawn — retry
                # briefly so an immediate pause isn't silently lost, and only
                # mark paused once mpv actually acknowledged the command
                for _ in range(5):
                    if self._mpv_command({"command": ["set_property", "pause", True]}):
                        self._paused = True
                        return
                    if not self._process or self._process.poll() is not None:
                        return
                    time.sleep(0.1)
            else:
                # ffplay: record position, kill process (instant silence)
                elapsed = time.time() - self._play_start if self._play_start else 0
                self._seek_offset += elapsed
                self._play_start = None
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                self._process = None
                self._paused = True

    def resume(self) -> bool:
        """Resume paused playback. False if it failed (e.g. player died)."""
        with self._lock:
            if not self._paused:
                return False
            if self.player_cmd == "mpv" and self._ipc_path:
                if self._mpv_command({"command": ["set_property", "pause", False]}):
                    self._paused = False
                    return True
                return False
            else:
                # ffplay: restart from cached local file (instant, no network)
                if self._cache_file and os.path.exists(self._cache_file):
                    self._play_from_cache(self._seek_offset)
                    return True
                elif self._current_url:
                    # Cache not ready — fall back to URL
                    self._paused = False
                    url = self._current_url
                    seek = self._seek_offset
                    self._lock.release()
                    try:
                        self.play_url(url, seek=seek)
                    finally:
                        self._lock.acquire()
                    return True
            return False

    def _mpv_request(self, cmd: dict, timeout: float = 0.5) -> Optional[dict]:
        """Send a JSON IPC command to mpv and return its reply, or None."""
        if not self._ipc_path:
            return None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            try:
                sock.connect(self._ipc_path)
                payload = dict(cmd)
                payload["request_id"] = 1
                sock.sendall((json.dumps(payload) + "\n").encode())
                # mpv also broadcasts events on this socket — scan lines for
                # the reply carrying our request_id
                buf = b""
                deadline = time.time() + timeout
                while time.time() < deadline:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                    for line in buf.split(b"\n"):
                        if not line.strip():
                            continue
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if msg.get("request_id") == 1:
                            return msg
            finally:
                sock.close()
        except OSError:
            pass
        return None

    def _mpv_command(self, cmd: dict) -> bool:
        """Send an mpv IPC command; True only if mpv acknowledged it."""
        reply = self._mpv_request(cmd)
        return reply is not None and reply.get("error") == "success"

    def get_time_pos(self) -> Optional[float]:
        """Ask mpv for its actual playback position. None if unavailable."""
        if self.player_cmd != "mpv":
            return None
        with self._lock:
            if self._paused or not self._process or self._process.poll() is not None:
                return None
            reply = self._mpv_request({"command": ["get_property", "time-pos"]}, timeout=0.2)
        if reply and reply.get("error") == "success" and isinstance(reply.get("data"), (int, float)):
            return float(reply["data"])
        return None

    def _bind_media_keys(self) -> bool:
        """Point mpv's media keys at MEDIA_KEY_PROP so ticli handles them."""
        for key, action in MEDIA_KEY_ACTIONS.items():
            if not self._mpv_command(
                {"command": ["keybind", key, f"set {MEDIA_KEY_PROP} {action}"]}
            ):
                return False
        if self._media_title:
            # Shown in Control Center / lock screen instead of the raw stream URL
            self._mpv_command(
                {"command": ["set_property", "force-media-title", self._media_title]}
            )
        return True

    def poll_media_key(self) -> Optional[str]:
        """Return a pending media-key action, or None. macOS + mpv only."""
        if not IS_MACOS or self.player_cmd != "mpv" or not self._ipc_path:
            return None
        if not self._media_keys_bound:
            self._media_keys_bound = self._bind_media_keys()
            if not self._media_keys_bound:
                return None
        reply = self._mpv_request({"command": ["get_property", MEDIA_KEY_PROP]})
        # The property only exists once a key was actually pressed
        if not reply or reply.get("error") != "success" or not reply.get("data"):
            return None
        self._mpv_command({"command": ["set_property", MEDIA_KEY_PROP, ""]})
        return reply["data"]

    def stop(self):
        """Stop current playback."""
        with self._lock:
            if self._process and self._process.poll() is None:
                self._process.terminate()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._process.kill()
                self._process = None
            # Stop cache download
            if self._cache_process and self._cache_process.poll() is None:
                self._cache_process.terminate()
                self._cache_process = None
            # Clean up cache file
            if self._cache_file:
                try:
                    os.unlink(self._cache_file)
                except OSError:
                    pass
                self._cache_file = None
            self._paused = False
            self._play_start = None
            self._seek_offset = 0
            self._media_keys_bound = False
            # Clean up mpv socket
            if self._ipc_path:
                try:
                    os.unlink(self._ipc_path)
                except OSError:
                    pass
                self._ipc_path = None

    @property
    def is_playing(self) -> bool:
        with self._lock:
            if self._paused:
                return True  # Paused but track is active
            return self._process is not None and self._process.poll() is None

    @property
    def is_paused(self) -> bool:
        with self._lock:
            if not self._paused:
                return False
            if self.player_cmd == "mpv":
                # A paused mpv must still be alive; a dead one can't resume.
                # (ffplay pause kills the process by design, so no check there.)
                return self._process is not None and self._process.poll() is None
            return True


class HeadlessTidalPlayer:
    """Headless TIDAL player - no desktop app required."""

    MODE_PLAYER = "player"
    MODE_SEARCH = "search"
    MODE_BROWSE = "browse"
    MODE_QUEUE = "queue"
    MODE_PLAYLISTS = "playlists"
    MODE_ADD_TO_PLAYLIST = "add_to_playlist"

    def __init__(self, quality: str = "HIGH"):
        self.console = Console()
        self.session = tidalapi.Session()
        self.audio = None  # set after finding player
        self.running = True
        self._mode = self.MODE_PLAYER
        # Playback state
        self._current_track: Optional[tidalapi.Track] = None
        self._queue: list = []
        self._queue_index: int = -1
        self._playing = False
        self._play_start_time: Optional[float] = None
        self._play_offset: float = 0
        self._liked_ids: set = set()
        # Search state
        self._search_query = ""
        self._search_results = []
        self._search_cursor = 0
        self._search_loading = False
        self._search_message = ""
        self._search_history: list = []  # recent searches, newest first
        # Browse state
        self._browse_title = ""
        self._browse_tracks = []
        self._browse_cursor = 0
        self._browse_loading = False
        self._browse_message = ""
        # Set when browse is showing one of the user's own (editable) playlists
        self._browse_playlist = None
        self._browse_remove_busy = False
        # Queue view state
        self._queue_cursor = 0
        # Playlists state
        self._playlists: list = []
        self._playlists_cursor = 0
        self._playlists_loading = False
        self._playlists_message = ""
        # Add-to-playlist picker state
        self._editable_playlists: list = []
        self._editable_playlists_time: float = 0.0
        self._picker_track = None
        self._picker_cursor = 0
        self._picker_loading = False
        self._picker_busy = False
        # Transient toast message
        self._toast = ""
        self._toast_until = 0.0
        # Quit confirmation
        self._quit_pending = False
        # Logout confirmation
        self._logout_pending = False
        # Mini player mode
        self._mini_player = False
        # Show more controls
        self._show_more = False
        # Space-held guard: prevents toggle-looping from key repeat
        self._space_held = False
        # User display name (set after login)
        self._user_display_name = ""
        # True while the restore-state thread is still fetching tracks
        self._restore_pending = False
        # True while _play_track is between killing the old player process
        # and spawning the new one (guards the monitor's end-of-track check)
        self._track_changing = False
        # Navigation
        self._nav_history = []
        # Quality
        quality_map = {
            "LOW": tidalapi.Quality.low_320k,
            "HIGH": tidalapi.Quality.high_lossless,
            "LOSSLESS": tidalapi.Quality.high_lossless,
            "HIRES": tidalapi.Quality.hi_res_lossless,
        }
        self.session.audio_quality = quality_map.get(quality.upper(), tidalapi.Quality.high_lossless)

    def _get_user_display_name(self) -> str:
        """Get a display name for the logged-in user."""
        u = self.session.user
        if not u:
            return "Unknown"
        # LoggedInUser has username; FetchedUser has first_name/last_name
        first = getattr(u, "first_name", None)
        last = getattr(u, "last_name", None)
        if first and last:
            return f"{first} {last}"
        if first:
            return first
        username = getattr(u, "username", None)
        if username:
            return username
        email = getattr(u, "email", None)
        if email:
            return email
        return f"User {u.id}"

    def _login(self) -> bool:
        """Login to TIDAL via OAuth device flow."""
        # Try loading existing session from secure storage
        data = load_tokens()
        if data:
            try:
                self.session.load_oauth_session(
                    data["token_type"],
                    data["access_token"],
                    data.get("refresh_token"),
                    data.get("expiry_time"),
                )
                if self.session.check_login():
                    self._user_display_name = self._get_user_display_name()
                    return True
            except Exception as e:
                logger.debug("Failed to load saved session: %s", e)

        # Fresh login
        self.console.print("[cyan]Starting TIDAL login...[/cyan]")
        login, future = self.session.login_oauth()
        self.console.print(f"\n[bold yellow]Open this URL to login:[/bold yellow]")
        self.console.print(f"[bold white]https://{login.verification_uri_complete}[/bold white]\n")
        self.console.print(f"[dim]Or go to [bold]{login.verification_uri}[/bold] and enter code: [bold]{login.user_code}[/bold][/dim]\n")
        self.console.print("[dim]Waiting for authorization...[/dim]")

        future.result()

        if self.session.check_login():
            # Save session to secure storage (keychain or chmod-600 file)
            try:
                data = {
                    "token_type": self.session.token_type,
                    "access_token": self.session.access_token,
                    "refresh_token": self.session.refresh_token,
                    "expiry_time": self.session.expiry_time.isoformat() if self.session.expiry_time else None,
                }
                save_tokens(data)
            except Exception as e:
                logger.warning("Failed to save session: %s", e)
            self._user_display_name = self._get_user_display_name()
            return True

        self.console.print("[red]Login failed.[/red]")
        return False

    def _logout(self):
        """Log out and clear saved tokens."""
        from ticli.utils.credential_store import delete_tokens
        delete_tokens()
        self.audio.stop()
        self._playing = False
        self._current_track = None
        self._queue = []
        self._queue_index = -1
        self.running = False
        self.console.print("[yellow]Logged out. Tokens cleared.[/yellow]")

    def _load_favorites(self):
        """Load liked track IDs in background."""
        def _run():
            try:
                favs = self.session.user.favorites.tracks(limit=999)
                self._liked_ids = {t.id for t in favs}
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _write_state_file(self, state: dict):
        """Atomically write the state file (temp + rename, never torn)."""
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.chmod(tmp, 0o600)
        os.replace(tmp, STATE_FILE)

    def _merge_position_into_saved_state(self):
        """Refresh only the position in the saved file, if the track matches."""
        if self._current_track is None or not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text())
            ids = data.get("track_ids", [])
            idx = data.get("queue_index", 0)
            if ids and 0 <= idx < len(ids) and ids[idx] == self._current_track.id:
                data["position"] = self._get_position()
                self._write_state_file(data)
        except Exception as e:
            logger.debug("Failed to merge position into saved state: %s", e)

    def _save_state(self):
        """Save queue and playback state to disk for next session."""
        # If the restore never finished attaching the queue, our in-memory
        # state is incomplete — a full save would shrink the good file. Still
        # refresh the position in case the user played the restored track.
        if self._restore_pending and not self._queue:
            self._merge_position_into_saved_state()
            return
        try:
            track_ids = [t.id for t in self._queue]
            queue_index = self._queue_index
            if not track_ids and self._current_track is not None:
                track_ids = [self._current_track.id]
                queue_index = 0
            state = {
                "track_ids": track_ids,
                "queue_index": queue_index,
                "position": self._get_position(),
                "search_history": self._search_history[:20],
            }
            self._write_state_file(state)
        except Exception as e:
            logger.debug("Failed to save player state: %s", e)

    def _restore_state(self):
        """Restore queue and search history from previous session."""
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return
        self._search_history = data.get("search_history", [])[:20]
        track_ids = data.get("track_ids", [])
        queue_index = data.get("queue_index", 0)
        position = data.get("position", 0) or 0
        if not track_ids:
            return

        self._restore_pending = True

        def _run():
            try:
                # Fetch the current track first so it appears immediately,
                # paused at its saved position, before the rest of the queue loads
                idx = min(max(queue_index, 0), len(track_ids) - 1)
                current = None
                try:
                    current = self.session.track(track_ids[idx])
                except Exception:
                    pass
                if current is not None and not self._playing and self._current_track is None:
                    duration = getattr(current, "duration", 0) or 0
                    if 1 <= position < duration - 2:
                        self._play_offset = position
                    self._current_track = current

                tracks = []
                for i, tid in enumerate(track_ids):
                    if i == idx and current is not None:
                        tracks.append(current)
                        continue
                    try:
                        t = self.session.track(tid)
                        if t:
                            tracks.append(t)
                    except Exception:
                        pass
                # Don't clobber anything the user started while we were loading
                # (playing the restored track itself is fine — attach its queue)
                if tracks and self._current_track in (None, current):
                    self._queue = tracks
                    try:
                        self._queue_index = tracks.index(current) if current is not None else min(idx, len(tracks) - 1)
                    except ValueError:
                        self._queue_index = min(idx, len(tracks) - 1)
                    if self._current_track is None:
                        self._current_track = tracks[self._queue_index]
                    # Latch: only a SUCCESSFUL attach re-enables full saves.
                    # If restore failed, empty-queue saves stay suppressed so
                    # they can't shrink the good file to a single track.
                    self._restore_pending = False
            except Exception as e:
                logger.debug("Failed to restore player state: %s", e)

        threading.Thread(target=_run, daemon=True).start()

    def _play_track(self, track: tidalapi.Track, seek: float = 0):
        """Play a track via the audio player, optionally starting at an offset."""
        self._track_changing = True
        try:
            url = track.get_url()
            artist = ", ".join(a.name for a in track.artists) if track.artists else ""
            title = f"{track.name} — {artist}" if artist else track.name
            self.audio.play_url(url, seek=seek, title=title)
            self._current_track = track
            self._playing = True
            self._play_start_time = time.time()
            self._play_offset = seek
        except Exception:
            self._playing = False
        finally:
            self._track_changing = False

    def _play_queue_index(self, index: int):
        """Play track at queue index."""
        if 0 <= index < len(self._queue):
            self._queue_index = index
            self._play_track(self._queue[index])

    def _next_track(self):
        """Skip to next track in queue."""
        if self._queue and self._queue_index < len(self._queue) - 1:
            self._play_queue_index(self._queue_index + 1)

    def _prev_track(self):
        """Go to previous track in queue."""
        if self._queue and self._queue_index > 0:
            self._play_queue_index(self._queue_index - 1)

    def _toggle_play(self):
        """Toggle play/pause — pauses in place, resumes from same position."""
        if self._playing:
            self.audio.pause()
            self._playing = False
            if self._play_start_time:
                self._play_offset += time.time() - self._play_start_time
                self._play_start_time = None
        else:
            if self._current_track and self.audio and self.audio.is_paused:
                # Resume from paused position
                if self.audio.resume():
                    self._playing = True
                    self._play_start_time = time.time()
                else:
                    # Player died while paused — restart from where we were
                    self._start_current_from_position()
            elif self._current_track:
                # No paused process — start fresh from the last known position
                self._start_current_from_position()

    def _start_current_from_position(self):
        """(Re)start the current track from the last known position."""
        seek = self._get_position()
        duration = getattr(self._current_track, "duration", 0) or 0
        # Clamp unconditionally: an unknown duration must not let a stale
        # offset seek past EOF (mpv exits instantly on that)
        if seek < 1 or seek >= duration - 2:
            seek = 0
        self._play_track(self._current_track, seek=seek)

    def _toggle_like(self):
        """Toggle like on current track."""
        if not self._current_track:
            return
        tid = self._current_track.id
        def _run():
            try:
                if tid in self._liked_ids:
                    self.session.user.favorites.remove_track(tid)
                    self._liked_ids.discard(tid)
                else:
                    self.session.user.favorites.add_track(tid)
                    self._liked_ids.add(tid)
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _start_track_radio(self):
        """Start radio based on current track."""
        if not self._current_track:
            return
        track_id = self._current_track.id
        def _run():
            try:
                radio_tracks = self._current_track.get_track_radio(limit=25)
                if radio_tracks:
                    self._queue = radio_tracks
                    self._queue_index = 0
                    self._play_track(self._queue[0])
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True).start()

    def _handle_media_key(self, action):
        """Apply a media-key action coming from mpv (macOS only)."""
        if action == "next":
            self._next_track()
        elif action == "prev":
            self._prev_track()
        elif action == "toggle":
            self._toggle_play()
        elif action == "play" and not self._playing:
            self._toggle_play()
        elif action == "pause" and self._playing:
            self._toggle_play()

    def _monitor_playback(self):
        """Background thread: auto-advance, position resync, periodic save."""
        last_save = time.time()
        dead_polls = 0
        while self.running:
            if (self._playing and not self._track_changing and self.audio
                    and not self.audio.is_paused and not self.audio.is_playing):
                # Require two consecutive dead polls before advancing: a track
                # change kills the old process before spawning the new one,
                # and a single poll can land inside that window
                dead_polls += 1
            else:
                dead_polls = 0
            if dead_polls >= 2:
                dead_polls = 0
                if self._queue and self._queue_index < len(self._queue) - 1:
                    self._play_queue_index(self._queue_index + 1)
                else:
                    self._playing = False
                    self._play_start_time = None
                    self._play_offset = 0
            elif self._playing and self.audio:
                # Resync wall-clock position with mpv's real position so
                # startup latency and drift never accumulate
                pos = self.audio.get_time_pos()
                if pos is not None:
                    self._play_offset = pos
                    self._play_start_time = time.time()
            # Save state periodically so a crash doesn't lose the position
            if time.time() - last_save > 10:
                self._save_state()
                last_save = time.time()
            if self.audio:
                self._handle_media_key(self.audio.poll_media_key())
            time.sleep(0.5)

    # ── Display builders ──

    def _get_position(self) -> float:
        if self._play_start_time and self._playing:
            return self._play_offset + (time.time() - self._play_start_time)
        return self._play_offset

    def _build_player_display(self) -> Text:
        s = self._current_track
        title = s.name if s else "No track"
        artist = ", ".join(a.name for a in s.artists) if s and s.artists else ""
        album = s.album.name if s and s.album else ""
        duration = s.duration if s else 0
        position = self._get_position() if s else 0
        liked = (s.id in self._liked_ids) if s else None

        state_icon = "\u25b6" if self._playing else "\u23f8"

        # Mini player: single compact line
        if self._mini_player:
            content = Text()
            content.append(f" {state_icon} ", style="bold cyan")
            if liked is True:
                content.append("\u2665 ", style="bold red")
            content.append(title, style="bold white")
            if artist:
                content.append(f" \u2022 {artist}", style="dim white")
            pos_str = format_time(position)
            dur_str = format_time(duration) if duration > 0 else "--:--"
            content.append(f"  {pos_str}/{dur_str}", style="cyan")
            if self._queue:
                content.append(f"  [{self._queue_index + 1}/{len(self._queue)}]", style="dim")
            return content

        # Full player display
        track_line = Text()
        track_line.append(f" {state_icon} ", style="bold cyan")
        if liked is True:
            track_line.append("\u2665 ", style="bold red")
        elif liked is False:
            track_line.append("\u2661 ", style="dim")
        track_line.append(title, style="bold white")
        if artist:
            track_line.append(f"  {artist}", style="dim white")

        album_line = Text()
        if album:
            album_line.append(f"   {album}", style="dim")

        progress_pct = (position / duration * 100) if duration > 0 else 0
        pos_str = format_time(position)
        dur_str = format_time(duration) if duration > 0 else "--:--"

        bar_width = 50
        filled = int(bar_width * min(progress_pct, 100) / 100)
        if duration > 0:
            bar = "\u2501" * filled + "\u2578" + "\u2500" * max(0, bar_width - filled - 1)
        else:
            bar = "\u2500" * bar_width
        progress_line = Text()
        progress_line.append(f"   {pos_str} ", style="cyan")
        progress_line.append(bar, style="bold cyan" if self._playing else "dim")
        progress_line.append(f" {dur_str}", style="cyan")

        # Queue info
        status_line = Text()
        if self._queue:
            status_line.append(f"   Queue: {self._queue_index + 1}/{len(self._queue)}", style="dim")
        quality_label = {
            tidalapi.Quality.low_320k: "HIGH 320k",
            tidalapi.Quality.high_lossless: "LOSSLESS",
            tidalapi.Quality.hi_res_lossless: "HI-RES",
        }.get(self.session.audio_quality, "")
        if quality_label:
            status_line.append(f"   {quality_label}", style="dim cyan")

        # Next track preview (only in player mode)
        up_next = Text()
        if self._mode == self.MODE_PLAYER and self._queue and self._queue_index < len(self._queue) - 1:
            t = self._queue[self._queue_index + 1]
            t_name = t.name if hasattr(t, "name") else "?"
            t_artist = t.artists[0].name if hasattr(t, "artists") and t.artists else ""
            up_next.append("\n   Next: ", style="dim")
            up_next.append(t_name, style="dim white")
            if t_artist:
                up_next.append(f" \u2022 {t_artist}", style="dim")

        content = Text()
        content.append_text(track_line)
        content.append("\n")
        content.append_text(album_line)
        content.append("\n")
        content.append_text(progress_line)
        content.append("\n")
        content.append_text(status_line)
        content.append_text(up_next)
        return content

    def _build_search_display(self) -> Text:
        content = Text()
        content.append("   Search: ", style="bold yellow")
        content.append(self._search_query, style="white")
        content.append("\u2588", style="bold white")

        if self._search_loading:
            content.append("\n\n   Searching...", style="dim yellow")
        elif self._search_message:
            content.append(f"\n\n   {self._search_message}", style="dim green")
        elif self._search_results:
            total = len(self._search_results)
            page_start = (self._search_cursor // PAGE_SIZE) * PAGE_SIZE
            page_end = min(page_start + PAGE_SIZE, total)
            content.append("\n", style="")
            for i in range(page_start, page_end):
                item = self._search_results[i]
                content.append("\n")
                if i == self._search_cursor:
                    content.append("  \u25b8 ", style="bold cyan")
                else:
                    content.append("    ", style="")
                type_styles = {"track": "bold green", "album": "bold magenta", "artist": "bold yellow"}
                badge = item["type"].upper()
                content.append(f"[{badge}]", style=type_styles.get(item["type"], "dim"))
                content.append(f" {item['name']}", style="bold white" if i == self._search_cursor else "white")
                if item.get("artist"):
                    content.append(f"  {item['artist']}", style="dim")
            if total > PAGE_SIZE:
                page_num = (self._search_cursor // PAGE_SIZE) + 1
                total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
                content.append(f"\n\n   Page {page_num}/{total_pages}", style="dim")
                content.append(f"  ({total} results)", style="dim")
        elif self._search_query:
            content.append("\n\n   Press Enter to search", style="dim")

        return content

    def _build_browse_display(self) -> Text:
        content = Text()
        content.append(f"   {self._browse_title}", style="bold magenta")

        if self._browse_loading:
            content.append("\n\n   Loading...", style="dim yellow")
        elif self._browse_message:
            content.append(f"\n\n   {self._browse_message}", style="dim green")
        elif self._browse_tracks:
            total = len(self._browse_tracks)
            # browse_cursor -1 = "Play All" row, 0..N-1 = tracks
            page_start = max(0, ((self._browse_cursor - 1) // PAGE_SIZE) * PAGE_SIZE) if self._browse_cursor > 0 else 0
            page_end = min(page_start + PAGE_SIZE, total)
            content.append(f"  ({total} tracks)", style="dim")
            content.append("\n", style="")

            # "Play All" row (always visible when on first page)
            if self._browse_cursor <= 0 or page_start == 0:
                content.append("\n")
                if self._browse_cursor == -1:
                    content.append("  \u25b8 ", style="bold cyan")
                    content.append("\u25b6 Play All", style="bold cyan")
                else:
                    content.append("    ", style="")
                    content.append("\u25b6 Play All", style="dim green")

            for i in range(page_start, page_end):
                track = self._browse_tracks[i]
                content.append("\n")
                if i == self._browse_cursor:
                    content.append("  \u25b8 ", style="bold cyan")
                else:
                    content.append("    ", style="")
                content.append(f"{i+1:>2}. ", style="dim")
                content.append(track.name, style="bold white" if i == self._browse_cursor else "white")
                if track.artists:
                    content.append(f"  {track.artists[0].name}", style="dim")
                content.append(f"  {format_time(track.duration)}", style="dim cyan")
            if total > PAGE_SIZE:
                page_num = (max(0, self._browse_cursor - 1) // PAGE_SIZE) + 1 if self._browse_cursor > 0 else 1
                total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
                content.append(f"\n\n   Page {page_num}/{total_pages}", style="dim")

        return content

    def _build_queue_display(self) -> Text:
        content = Text()
        content.append("   Queue", style="bold yellow")
        if not self._queue:
            content.append("\n\n   Queue is empty", style="dim")
        else:
            total = len(self._queue)
            page_start = (self._queue_cursor // PAGE_SIZE) * PAGE_SIZE
            page_end = min(page_start + PAGE_SIZE, total)
            content.append(f"  ({total} tracks)", style="dim")
            content.append("\n", style="")
            for i in range(page_start, page_end):
                track = self._queue[i]
                content.append("\n")
                is_current = (i == self._queue_index)
                is_cursor = (i == self._queue_cursor)
                if is_cursor:
                    content.append("  \u25b8 ", style="bold cyan")
                elif is_current:
                    content.append("  \u266b ", style="bold cyan")
                else:
                    content.append("    ", style="")
                t_name = track.name if hasattr(track, "name") else "?"
                t_artist = track.artists[0].name if hasattr(track, "artists") and track.artists else ""
                t_dur = format_time(track.duration) if hasattr(track, "duration") else ""
                name_style = "bold cyan" if is_current else ("bold white" if is_cursor else "white")
                content.append(f"{i + 1:>2}. ", style="dim")
                content.append(t_name, style=name_style)
                if is_current:
                    content.append("  \u25b6" if self._playing else "  \u23f8", style="bold cyan")
                if t_artist:
                    content.append(f"  {t_artist}", style="dim")
                if t_dur:
                    content.append(f"  {t_dur}", style="dim cyan")
            if total > PAGE_SIZE:
                page_num = (self._queue_cursor // PAGE_SIZE) + 1
                total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
                content.append(f"\n\n   Page {page_num}/{total_pages}", style="dim")
        return content

    def _build_playlists_display(self) -> Text:
        content = Text()
        content.append("   Your Playlists", style="bold magenta")

        if self._playlists_loading:
            content.append("\n\n   Loading playlists...", style="dim yellow")
        elif self._playlists_message:
            content.append(f"\n\n   {self._playlists_message}", style="dim green")
        elif self._playlists:
            total = len(self._playlists)
            page_start = (self._playlists_cursor // PAGE_SIZE) * PAGE_SIZE
            page_end = min(page_start + PAGE_SIZE, total)
            content.append(f"  ({total})", style="dim")
            content.append("\n", style="")
            for i in range(page_start, page_end):
                pl = self._playlists[i]
                content.append("\n")
                if i == self._playlists_cursor:
                    content.append("  \u25b8 ", style="bold cyan")
                else:
                    content.append("    ", style="")
                pl_name = pl.name if hasattr(pl, "name") else "?"
                num_tracks = pl.num_tracks if hasattr(pl, "num_tracks") else ""
                creator = ""
                if hasattr(pl, "creator") and pl.creator:
                    creator = pl.creator.name if hasattr(pl.creator, "name") else ""
                content.append(pl_name, style="bold white" if i == self._playlists_cursor else "white")
                if num_tracks:
                    content.append(f"  {num_tracks} tracks", style="dim cyan")
                if creator:
                    content.append(f"  by {creator}", style="dim")
            if total > PAGE_SIZE:
                page_num = (self._playlists_cursor // PAGE_SIZE) + 1
                total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
                content.append(f"\n\n   Page {page_num}/{total_pages}", style="dim")
        else:
            content.append("\n\n   No playlists found", style="dim")

        return content

    def _build_add_to_playlist_display(self) -> Text:
        content = Text()
        track_name = self._picker_track.name if self._picker_track is not None else "?"
        content.append("   Add to playlist: ", style="bold magenta")
        content.append(track_name, style="bold white")

        if self._picker_loading:
            content.append("\n\n   Loading playlists...", style="dim yellow")
        elif self._editable_playlists:
            total = len(self._editable_playlists)
            page_start = (self._picker_cursor // PAGE_SIZE) * PAGE_SIZE
            page_end = min(page_start + PAGE_SIZE, total)
            content.append("\n", style="")
            for i in range(page_start, page_end):
                pl = self._editable_playlists[i]
                content.append("\n")
                if i == self._picker_cursor:
                    content.append("  ▸ ", style="bold cyan")
                else:
                    content.append("    ", style="")
                pl_name = pl.name if hasattr(pl, "name") else "?"
                num_tracks = pl.num_tracks if hasattr(pl, "num_tracks") else ""
                content.append(pl_name, style="bold white" if i == self._picker_cursor else "white")
                if num_tracks != "":
                    content.append(f"  {num_tracks} tracks", style="dim cyan")
            if total > PAGE_SIZE:
                page_num = (self._picker_cursor // PAGE_SIZE) + 1
                total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
                content.append(f"\n\n   Page {page_num}/{total_pages}", style="dim")
        else:
            content.append("\n\n   No playlists you can edit", style="dim")

        return content

    def _build_quit_confirm(self) -> Text:
        content = Text()
        content.append("\n   Quit player? ", style="bold yellow")
        content.append("Press ", style="dim")
        content.append("Esc", style="bold")
        content.append(" again to confirm, any other key to cancel", style="dim")
        return content

    def _build_logout_confirm(self) -> Text:
        content = Text()
        content.append("\n   Log out and clear saved tokens? ", style="bold yellow")
        content.append("Press ", style="dim")
        content.append("y", style="bold")
        content.append(" to confirm, any other key to cancel", style="dim")
        return content

    def _build_display(self) -> Panel:
        player = self._build_player_display()

        controls = Text()
        if self._mode == self.MODE_PLAYER:
            controls.append("   [space]", style="bold")
            controls.append(" play/pause  ", style="dim")
            controls.append("[\u2190/\u2192]", style="bold")
            controls.append(" prev/next  ", style="dim")
            controls.append("[s]", style="bold")
            controls.append(" search  ", style="dim")
            controls.append("[t]", style="bold")
            controls.append(" tiny  ", style="dim")
            controls.append("[m]", style="bold")
            controls.append(" more", style="dim")
            if self._show_more:
                controls.append("\n   [l]", style="bold")
                controls.append(" like  ", style="dim")
                controls.append("[r]", style="bold")
                controls.append(" radio  ", style="dim")
                controls.append("[y]", style="bold")
                controls.append(" add to playlist  ", style="dim")
                controls.append("[q]", style="bold")
                controls.append(" queue  ", style="dim")
                controls.append("[p]", style="bold")
                controls.append(" playlists  ", style="dim")
                controls.append("[o]", style="bold")
                controls.append(" logout  ", style="dim")
                controls.append("[Esc]", style="bold")
                controls.append(" quit", style="dim")
                if self._user_display_name:
                    controls.append(f"\n   Logged in as ", style="dim")
                    controls.append(self._user_display_name, style="bold")
        elif self._mode == self.MODE_SEARCH:
            controls.append("   [Enter/\u2192]", style="bold")
            controls.append(" search/open  ", style="dim")
            controls.append("[\u2191/\u2193]", style="bold")
            controls.append(" navigate  ", style="dim")
            if self._search_results:
                controls.append("[Space]", style="bold")
                controls.append(" pause/play  ", style="dim")
            controls.append("[\u2190/Esc]", style="bold")
            controls.append(" back  ", style="dim")
            controls.append("[Bksp]", style="bold")
            controls.append(" delete", style="dim")
        elif self._mode == self.MODE_BROWSE:
            controls.append("   [Enter/\u2192]", style="bold")
            controls.append(" play track  ", style="dim")
            controls.append("[\u2191/\u2193]", style="bold")
            controls.append(" navigate  ", style="dim")
            controls.append("[Space]", style="bold")
            controls.append(" pause/play  ", style="dim")
            controls.append("[a]", style="bold")
            controls.append(" play all  ", style="dim")
            controls.append("[y]", style="bold")
            controls.append(" add to playlist  ", style="dim")
            if self._browse_playlist is not None:
                controls.append("[x]", style="bold")
                controls.append(" remove  ", style="dim")
            controls.append("[\u2190/Esc]", style="bold")
            controls.append(" back", style="dim")
        elif self._mode == self.MODE_QUEUE:
            controls.append("   [Enter]", style="bold")
            controls.append(" play  ", style="dim")
            controls.append("[\u2191/\u2193]", style="bold")
            controls.append(" navigate  ", style="dim")
            controls.append("[Space]", style="bold")
            controls.append(" pause/play  ", style="dim")
            controls.append("[x]", style="bold")
            controls.append(" remove  ", style="dim")
            controls.append("[y]", style="bold")
            controls.append(" add to playlist  ", style="dim")
            controls.append("[\u2190/Esc]", style="bold")
            controls.append(" back", style="dim")
        elif self._mode == self.MODE_PLAYLISTS:
            controls.append("   [Enter/\u2192]", style="bold")
            controls.append(" open  ", style="dim")
            controls.append("[\u2191/\u2193]", style="bold")
            controls.append(" navigate  ", style="dim")
            controls.append("[Space]", style="bold")
            controls.append(" pause/play  ", style="dim")
            controls.append("[\u2190/Esc]", style="bold")
            controls.append(" back", style="dim")
        elif self._mode == self.MODE_ADD_TO_PLAYLIST:
            controls.append("   [Enter]", style="bold")
            controls.append(" add  ", style="dim")
            controls.append("[\u2191/\u2193]", style="bold")
            controls.append(" navigate  ", style="dim")
            controls.append("[\u2190/Esc]", style="bold")
            controls.append(" cancel", style="dim")

        content = Text()
        content.append_text(player)
        if time.time() < self._toast_until:
            content.append(f"\n   {self._toast}", style="bold green")

        if self._mini_player:
            # Tiny mode: just the player line, no controls
            if self._quit_pending:
                content.append_text(self._build_quit_confirm())
            return Panel(
                content,
                title="[bold cyan]Ticli[/bold cyan]",
                border_style="cyan",
                padding=(0, 1),
            )

        if self._mode != self.MODE_PLAYER:
            content.append("\n\n")
            content.append("  " + "\u2500" * 56, style="dim")
            content.append("\n\n")
            if self._mode == self.MODE_SEARCH:
                content.append_text(self._build_search_display())
            elif self._mode == self.MODE_BROWSE:
                content.append_text(self._build_browse_display())
            elif self._mode == self.MODE_QUEUE:
                content.append_text(self._build_queue_display())
            elif self._mode == self.MODE_PLAYLISTS:
                content.append_text(self._build_playlists_display())
            elif self._mode == self.MODE_ADD_TO_PLAYLIST:
                content.append_text(self._build_add_to_playlist_display())

        if self._quit_pending:
            content.append_text(self._build_quit_confirm())
        elif self._logout_pending:
            content.append_text(self._build_logout_confirm())

        content.append("\n\n")
        content.append_text(controls)

        return Panel(
            content,
            title="[bold cyan]Ticli[/bold cyan]",
            border_style="cyan",
            padding=(1, 2),
        )

    # ── Actions ──

    def _push_nav(self):
        if self._mode == self.MODE_SEARCH:
            self._nav_history.append({
                "mode": self.MODE_SEARCH,
                "query": self._search_query,
                "results": list(self._search_results),
                "cursor": self._search_cursor,
            })
        elif self._mode == self.MODE_BROWSE:
            self._nav_history.append({
                "mode": self.MODE_BROWSE,
                "title": self._browse_title,
                "tracks": list(self._browse_tracks),
                "cursor": self._browse_cursor,
            })
        elif self._mode == self.MODE_QUEUE:
            self._nav_history.append({
                "mode": self.MODE_QUEUE,
                "cursor": self._queue_cursor,
            })
        elif self._mode == self.MODE_PLAYLISTS:
            self._nav_history.append({
                "mode": self.MODE_PLAYLISTS,
                "cursor": self._playlists_cursor,
            })
        else:
            self._nav_history.append({"mode": self.MODE_PLAYER})

    def _go_back(self):
        if not self._nav_history:
            self._mode = self.MODE_PLAYER
            return
        state = self._nav_history.pop()
        mode = state["mode"]
        if mode == self.MODE_SEARCH:
            self._mode = self.MODE_SEARCH
            self._search_query = state.get("query", "")
            self._search_results = state.get("results", [])
            self._search_cursor = state.get("cursor", 0)
            self._search_loading = False
            self._search_message = ""
        elif mode == self.MODE_BROWSE:
            self._mode = self.MODE_BROWSE
            self._browse_title = state.get("title", "")
            self._browse_tracks = state.get("tracks", [])
            self._browse_cursor = state.get("cursor", 0)
            self._browse_loading = False
            self._browse_message = ""
        elif mode == self.MODE_QUEUE:
            self._mode = self.MODE_QUEUE
            self._queue_cursor = state.get("cursor", 0)
        elif mode == self.MODE_PLAYLISTS:
            self._mode = self.MODE_PLAYLISTS
            self._playlists_cursor = state.get("cursor", 0)
        else:
            self._mode = self.MODE_PLAYER

    def _add_to_history(self, query: str):
        """Add a search query to history (deduped, newest first)."""
        q = query.strip()
        if not q:
            return
        # Remove if already present, then prepend
        self._search_history = [h for h in self._search_history if h.lower() != q.lower()]
        self._search_history.insert(0, q)
        self._search_history = self._search_history[:20]

    def _do_search(self):
        query = self._search_query.strip()
        if not query:
            return
        self._add_to_history(query)
        self._search_loading = True
        self._search_results = []
        self._search_cursor = 0
        self._search_message = ""

        def _run():
            try:
                results = self.session.search(query, models=[tidalapi.Track, tidalapi.Album, tidalapi.Artist], limit=8)
                items = []
                for track in (results.get("tracks") or [])[:5]:
                    artist = track.artists[0].name if track.artists else ""
                    items.append({"type": "track", "name": track.name, "artist": artist, "obj": track})
                for album in (results.get("albums") or [])[:3]:
                    artist = album.artist.name if album.artist else ""
                    items.append({"type": "album", "name": album.name, "artist": artist, "obj": album})
                for artist in (results.get("artists") or [])[:2]:
                    items.append({"type": "artist", "name": artist.name, "artist": "", "obj": artist})
                self._search_results = items
                if not items:
                    self._search_message = "No results found"
            except Exception as e:
                self._search_message = f"Search failed: {e}"
            finally:
                self._search_loading = False

        threading.Thread(target=_run, daemon=True).start()

    def _select_search_result(self):
        if not self._search_results:
            return
        item = self._search_results[self._search_cursor]
        obj = item["obj"]

        if item["type"] == "track":
            self._queue = [obj]
            self._queue_index = 0
            self._play_track(obj)
            self._mode = self.MODE_PLAYER
            self._nav_history.clear()
        elif item["type"] == "album":
            self._open_album(obj)
        elif item["type"] == "artist":
            self._open_artist(obj)

    def _open_album(self, album):
        self._push_nav()
        self._mode = self.MODE_BROWSE
        self._browse_playlist = None
        self._browse_title = album.name
        self._browse_tracks = []
        self._browse_cursor = -1
        self._browse_loading = True
        self._browse_message = ""

        def _run():
            try:
                tracks = album.tracks()
                self._browse_tracks = list(tracks)
                if not self._browse_tracks:
                    self._browse_message = "No tracks found"
            except Exception:
                self._browse_message = "Failed to load album"
            finally:
                self._browse_loading = False

        threading.Thread(target=_run, daemon=True).start()

    def _open_artist(self, artist):
        self._push_nav()
        self._mode = self.MODE_BROWSE
        self._browse_playlist = None
        self._browse_title = f"{artist.name} - Top Tracks"
        self._browse_tracks = []
        self._browse_cursor = -1
        self._browse_loading = True
        self._browse_message = ""

        def _run():
            try:
                tracks = artist.get_top_tracks(limit=20)
                self._browse_tracks = list(tracks)
                if not self._browse_tracks:
                    self._browse_message = "No tracks found"
            except Exception:
                self._browse_message = "Failed to load artist"
            finally:
                self._browse_loading = False

        threading.Thread(target=_run, daemon=True).start()

    def _play_browse_track(self):
        if not self._browse_tracks:
            return
        track = self._browse_tracks[self._browse_cursor]
        self._queue = list(self._browse_tracks)
        self._queue_index = self._browse_cursor
        self._play_track(track)

    def _play_all_browse(self):
        if not self._browse_tracks:
            return
        self._queue = list(self._browse_tracks)
        self._play_queue_index(0)

    def _load_playlists(self):
        """Load user playlists in background."""
        self._playlists_loading = True
        self._playlists = []
        self._playlists_cursor = 0
        self._playlists_message = ""

        def _run():
            try:
                playlists = self.session.user.playlists()
                self._playlists = list(playlists) if playlists else []
                if not self._playlists:
                    self._playlists_message = "No playlists found"
            except Exception:
                self._playlists_message = "Failed to load playlists"
            finally:
                self._playlists_loading = False

        threading.Thread(target=_run, daemon=True).start()

    def _open_playlist(self, playlist):
        """Open a playlist and show its tracks in browse mode."""
        self._push_nav()
        self._mode = self.MODE_BROWSE
        # Removal is only offered on the user's own playlists
        self._browse_playlist = playlist if isinstance(playlist, tidalapi.UserPlaylist) else None
        self._browse_title = playlist.name if hasattr(playlist, "name") else "Playlist"
        self._browse_tracks = []
        self._browse_cursor = -1
        self._browse_loading = True
        self._browse_message = ""

        def _run():
            try:
                tracks = playlist.tracks()
                self._browse_tracks = list(tracks) if tracks else []
                if not self._browse_tracks:
                    self._browse_message = "Playlist is empty"
            except Exception:
                self._browse_message = "Failed to load playlist"
            finally:
                self._browse_loading = False

        threading.Thread(target=_run, daemon=True).start()

    def _remove_from_queue(self):
        """Remove the selected track from the queue."""
        if not self._queue or self._queue_cursor >= len(self._queue):
            return
        removing_current = (self._queue_cursor == self._queue_index)
        removing_before_current = (self._queue_cursor < self._queue_index)
        self._queue.pop(self._queue_cursor)
        if removing_before_current:
            self._queue_index -= 1
        elif removing_current:
            # If we removed the playing track, play the next one or stop
            if self._queue and self._queue_index < len(self._queue):
                self._play_track(self._queue[self._queue_index])
            elif self._queue and self._queue_index > 0:
                self._queue_index = len(self._queue) - 1
                self._play_track(self._queue[self._queue_index])
            else:
                self._playing = False
                self._current_track = None
                if self.audio:
                    self.audio.stop()
        # Adjust cursor
        if self._queue_cursor >= len(self._queue) and self._queue:
            self._queue_cursor = len(self._queue) - 1

    def _remove_from_browse_playlist(self):
        """Remove the track under the cursor from the open (own) playlist."""
        pl = self._browse_playlist
        if pl is None or not self._browse_tracks or self._browse_cursor < 0:
            return
        if self._browse_remove_busy:
            return  # previous removal still in flight — indices would shift
        index = self._browse_cursor
        track = self._browse_tracks[index]
        self._browse_remove_busy = True

        def _run():
            try:
                if pl.remove_by_index(index):
                    remaining = [t for i, t in enumerate(self._browse_tracks) if i != index]
                    self._browse_tracks = remaining
                    if self._browse_cursor >= len(remaining):
                        self._browse_cursor = len(remaining) - 1
                    self._set_toast(f'Removed "{track.name}" from {pl.name}')
                else:
                    self._set_toast("Failed to remove from playlist")
            except Exception:
                self._set_toast("Failed to remove from playlist")
            finally:
                self._browse_remove_busy = False

        threading.Thread(target=_run, daemon=True).start()

    def _set_toast(self, msg: str, seconds: float = 2.5):
        """Show a transient message; the 4fps render loop expires it."""
        self._toast = msg
        self._toast_until = time.time() + seconds

    def _target_track_for_picker(self):
        """Resolve which track 'add to playlist' refers to in the current mode."""
        if self._mode == self.MODE_QUEUE and self._queue and self._queue_cursor < len(self._queue):
            return self._queue[self._queue_cursor]
        if self._mode == self.MODE_BROWSE and self._browse_tracks and self._browse_cursor >= 0:
            return self._browse_tracks[self._browse_cursor]
        return self._current_track

    def _open_playlist_picker(self):
        """Open the add-to-playlist picker for the targeted track."""
        track = self._target_track_for_picker()
        if track is None:
            self._set_toast("No track selected")
            return
        self._push_nav()
        self._mode = self.MODE_ADD_TO_PLAYLIST
        self._picker_track = track
        self._picker_cursor = 0
        # Reuse the cached list for a minute — listing re-fetches every playlist
        if not self._editable_playlists or time.time() - self._editable_playlists_time > 60:
            self._picker_loading = True

            def _run():
                try:
                    playlists = self.session.user.playlists()
                    self._editable_playlists = [
                        p for p in (playlists or []) if isinstance(p, tidalapi.UserPlaylist)
                    ]
                    self._editable_playlists_time = time.time()
                except Exception:
                    pass
                finally:
                    self._picker_loading = False

            threading.Thread(target=_run, daemon=True).start()

    def _picker_add_to(self, playlist):
        """Add the picked track to a playlist (network in background)."""
        if self._picker_busy:
            return
        track = self._picker_track
        self._picker_busy = True
        self._go_back()

        def _run():
            try:
                added = playlist.add([str(track.id)])  # server skips duplicates
                if added:
                    self._set_toast(f'Added to "{playlist.name}"')
                else:
                    self._set_toast(f'Already in "{playlist.name}"')
            except Exception:
                self._set_toast("Failed to add to playlist")
            finally:
                self._picker_busy = False

        threading.Thread(target=_run, daemon=True).start()

    # ── Key handlers ──

    def _handle_key(self, key: str):
        # Handle quit confirmation first
        if self._quit_pending:
            if key == KEY_ESC:
                self.running = False
            else:
                self._quit_pending = False
            return

        # Handle logout confirmation
        if self._logout_pending:
            if key == "y" or key == "Y":
                self._logout()
            self._logout_pending = False
            return

        if self._mode == self.MODE_SEARCH:
            self._handle_search_key(key)
        elif self._mode == self.MODE_BROWSE:
            self._handle_browse_key(key)
        elif self._mode == self.MODE_QUEUE:
            self._handle_queue_key(key)
        elif self._mode == self.MODE_PLAYLISTS:
            self._handle_playlists_key(key)
        elif self._mode == self.MODE_ADD_TO_PLAYLIST:
            self._handle_add_to_playlist_key(key)
        else:
            self._handle_player_key(key)

    def _handle_player_key(self, key: str):
        if key in (" ", "k"):
            if self._space_held:
                return  # Key is still held down — ignore repeats
            self._toggle_play()
            self._space_held = True
        elif key == "n" or key == KEY_RIGHT:
            self._next_track()
        elif key == KEY_LEFT:
            self._prev_track()
        elif key == "s":
            self._mini_player = False
            self._mode = self.MODE_SEARCH
            self._search_query = ""
            self._search_results = []
            self._search_cursor = 0
            self._search_message = ""
            self._nav_history.clear()
        elif key == "t":
            self._mini_player = not self._mini_player
        elif key == "m":
            self._show_more = not self._show_more
        # Commands below are in the "more" menu but still work even when hidden
        elif key == "l":
            self._toggle_like()
        elif key == "r":
            self._start_track_radio()
        elif key == "y":
            self._open_playlist_picker()
        elif key == "q":
            self._mini_player = False
            self._mode = self.MODE_QUEUE
            self._queue_cursor = self._queue_index if self._queue else 0
            self._nav_history.clear()
        elif key == "p":
            self._mini_player = False
            self._mode = self.MODE_PLAYLISTS
            self._nav_history.clear()
            if not self._playlists and not self._playlists_loading:
                self._load_playlists()
        elif key == "o":
            self._logout_pending = True
        elif key == KEY_ESC:
            self._quit_pending = True

    def _handle_search_key(self, key: str):
        if key == KEY_ESC or key == KEY_LEFT:
            self._go_back()
            return
        if key == " " and self._search_results:
            # Space toggles play/pause when browsing search results
            if self._space_held:
                return
            self._toggle_play()
            self._space_held = True
            return
        if key == KEY_UP:
            if self._search_results:
                self._search_cursor = max(0, self._search_cursor - 1)
        elif key == KEY_DOWN:
            if self._search_results:
                self._search_cursor = min(len(self._search_results) - 1, self._search_cursor + 1)
        elif key in (KEY_ENTER, KEY_ENTER2, KEY_RIGHT):
            if self._search_results:
                self._select_search_result()
            elif key != KEY_RIGHT:
                self._do_search()
        elif key in (KEY_BACKSPACE, KEY_BACKSPACE2):
            self._search_query = self._search_query[:-1]
            self._search_results = []
            self._search_cursor = 0
            self._search_message = ""
        elif len(key) == 1 and key.isprintable():
            self._search_query += key
            self._search_results = []
            self._search_cursor = 0
            self._search_message = ""

    def _handle_browse_key(self, key: str):
        if key == KEY_ESC or key == KEY_LEFT:
            self._go_back()
            return
        if key == " ":
            if self._space_held:
                return
            self._toggle_play()
            self._space_held = True
            return
        if key == KEY_UP:
            if self._browse_tracks:
                self._browse_cursor = max(-1, self._browse_cursor - 1)
        elif key == KEY_DOWN:
            if self._browse_tracks:
                self._browse_cursor = min(len(self._browse_tracks) - 1, self._browse_cursor + 1)
        elif key in (KEY_ENTER, KEY_ENTER2, KEY_RIGHT):
            if self._browse_cursor == -1:
                self._play_all_browse()
            else:
                self._play_browse_track()
        elif key == "a":
            self._play_all_browse()
        elif key == "y":
            self._open_playlist_picker()
        elif key == "x":
            self._remove_from_browse_playlist()

    def _handle_queue_key(self, key: str):
        if key == KEY_ESC or key == KEY_LEFT:
            self._mode = self.MODE_PLAYER
            return
        if key == " ":
            if self._space_held:
                return
            self._toggle_play()
            self._space_held = True
            return
        if key == KEY_UP:
            if self._queue:
                self._queue_cursor = max(0, self._queue_cursor - 1)
        elif key == KEY_DOWN:
            if self._queue:
                self._queue_cursor = min(len(self._queue) - 1, self._queue_cursor + 1)
        elif key in (KEY_ENTER, KEY_ENTER2):
            if self._queue:
                self._play_queue_index(self._queue_cursor)
        elif key == "x":
            self._remove_from_queue()
        elif key == "y":
            self._open_playlist_picker()

    def _handle_playlists_key(self, key: str):
        if key == KEY_ESC or key == KEY_LEFT:
            self._mode = self.MODE_PLAYER
            return
        if key == " ":
            if self._space_held:
                return
            self._toggle_play()
            self._space_held = True
            return
        if key == KEY_UP:
            if self._playlists:
                self._playlists_cursor = max(0, self._playlists_cursor - 1)
        elif key == KEY_DOWN:
            if self._playlists:
                self._playlists_cursor = min(len(self._playlists) - 1, self._playlists_cursor + 1)
        elif key in (KEY_ENTER, KEY_ENTER2, KEY_RIGHT):
            if self._playlists:
                self._open_playlist(self._playlists[self._playlists_cursor])

    def _handle_add_to_playlist_key(self, key: str):
        if key == KEY_ESC or key == KEY_LEFT:
            self._go_back()
            return
        if key == " ":
            if self._space_held:
                return
            self._toggle_play()
            self._space_held = True
            return
        if key == KEY_UP:
            if self._editable_playlists:
                self._picker_cursor = max(0, self._picker_cursor - 1)
        elif key == KEY_DOWN:
            if self._editable_playlists:
                self._picker_cursor = min(len(self._editable_playlists) - 1, self._picker_cursor + 1)
        elif key in (KEY_ENTER, KEY_ENTER2, KEY_RIGHT):
            if self._editable_playlists and self._picker_cursor < len(self._editable_playlists):
                self._picker_add_to(self._editable_playlists[self._picker_cursor])

    # ── Main loop ──

    def _drain_stdin(self, select_mod=None):
        """Discard all pending stdin input to prevent buffered key repeats."""
        import select as _sel
        # Wait briefly for in-flight key-repeat bytes, then drain everything
        time.sleep(0.05)
        while _sel.select([sys.stdin], [], [], 0)[0]:
            os.read(sys.stdin.fileno(), 4096)

    def _read_key(self, select_mod):
        if not select_mod.select([sys.stdin], [], [], 0.25)[0]:
            return None
        ch = os.read(sys.stdin.fileno(), 1)
        if not ch:
            return None
        # If escape byte, try to read the rest of the sequence (arrow keys etc.)
        if ch == b"\x1b":
            if select_mod.select([sys.stdin], [], [], 0.05)[0]:
                ch += os.read(sys.stdin.fileno(), 7)
        return ch.decode("utf-8", errors="ignore")

    def run(self):
        """Start the headless player."""
        # Find audio player
        player_cmd = _find_audio_player()
        if not player_cmd:
            self.console.print("[red]No audio player found. Install mpv or ffplay.[/red]")
            return
        self.audio = AudioPlayer(player_cmd)

        # Login
        if not self._login():
            return

        # Load favorites in background
        self._load_favorites()

        # Restore previous session state (queue, current track)
        self._restore_state()

        # Start playback monitor
        monitor = threading.Thread(target=self._monitor_playback, daemon=True)
        monitor.start()

        # A closed terminal (SIGHUP) or kill (SIGTERM) should still exit the
        # main loop cleanly so the finally block saves state and stops audio
        def _on_signal(signum, frame):
            self.running = False
        for _sig in (signal.SIGTERM, signal.SIGHUP):
            try:
                signal.signal(_sig, _on_signal)
            except (ValueError, OSError):
                pass

        import tty
        import termios
        import select

        if not sys.stdin.isatty():
            self.console.print("[red]Player requires an interactive terminal.[/red]")
            return

        old_settings = termios.tcgetattr(sys.stdin)
        try:
            tty.setcbreak(sys.stdin.fileno())
            self.console.clear()

            with Live(
                self._build_display(),
                console=self.console,
                refresh_per_second=4,
                screen=False,
            ) as live:
                while self.running:
                    live.update(self._build_display())
                    key = self._read_key(select)
                    if key is not None:
                        self._handle_key(key)
                    elif self._space_held:
                        # No key this cycle — user released space
                        self._space_held = False
        finally:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
            # Save state before cleanup
            self._save_state()
            if self.audio:
                self.audio.stop()

        self.console.print("[dim]Player closed.[/dim]")


def main():
    import click

    @click.command()
    @click.option("--quality", default="HIGH", type=click.Choice(["LOW", "HIGH", "LOSSLESS", "HIRES"], case_sensitive=False), help="Audio quality")
    def headless(quality):
        """Launch Ticli terminal player."""
        HeadlessTidalPlayer(quality=quality).run()

    headless()


if __name__ == "__main__":
    main()
