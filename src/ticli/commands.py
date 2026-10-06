"""Player-level commands: one named entry point for every action, and the
permission gate in front of it (ADR-0007).

`Commands(player).execute(name, args, caller, key)` takes and returns plain
JSON-serialisable data, so a socket can carry it unchanged (ADR-0008).
TUI keypresses call it as the human; screens, cursors and menus stay in the TUI.
This module must not import `ticli.player`: `ticli agent` imports the gate.
"""

import json
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from ticli.utils import downloads, throttle
from ticli.utils.cache import MetadataCache
from ticli.utils.config import (
    PROTECTED_KEYS, SETTINGS_SPEC, ai_key_matches, coerce, get_spec, load_config,
    save_config,
)

HUMAN = "human"
AGENT = "agent"

WRONG_KEY_DELAY_SECONDS = 1.0

_NEVER_EDIT = ("Only your human can change this, in ticli's TUI settings ([c]). "
               "Ask them; never edit config.json or impersonate the TUI.")


class CommandError(Exception):
    def __init__(self, code: str, reason: str, fix: str = ""):
        super().__init__(reason)
        self.code, self.reason, self.fix = code, reason, fix


def _error(code: str, reason: str, fix: str = "") -> dict:
    payload = {"ok": False, "code": code, "reason": reason}
    if fix:
        payload["fix"] = fix
    return payload


@dataclass(frozen=True)
class Command:
    name: str
    handler: Callable
    read: bool = False
    tidal: bool = False
    dangerous: object = False  # bool, or (player, args) -> bool

    def is_dangerous(self, player, args) -> bool:
        return bool(self.dangerous(player, args) if callable(self.dangerous) else self.dangerous)


def gate(name: str, caller: str, key, cfg: dict, *, read: bool = False,
         dangerous: bool = False, sleep=time.sleep) -> Optional[dict]:
    """A refusal for an agent call the human's switches don't allow, else None."""
    if caller == HUMAN or name == "status":
        return None
    stored = coerce(get_spec("ai_control_key"), cfg.get("ai_control_key"))
    if stored and not key:
        return _error("key_required", "This ticli needs the AI control key.",
                      "Ask your human for the AI control key; pass it as TICLI_AI_KEY or --key. "
                      + _NEVER_EDIT)
    if stored and not ai_key_matches(stored, key):
        sleep(WRONG_KEY_DELAY_SECONDS)
        return _error("wrong_key", "That AI control key is wrong.",
                      "Ask your human for the AI control key. " + _NEVER_EDIT)
    if not cfg.get("allow_ai_control", True) and not read:
        return _error("ai_control_off", "AI control is off; only reads from disk are allowed.",
                      'Ask your human to turn on "Allow AI control". ' + _NEVER_EDIT)
    if dangerous and not cfg.get("allow_dangerous_commands", False):
        return _error("dangerous_off", f"{name} is a dangerous command and they are off.",
                      'Ask your human to turn on "Allow dangerous commands". ' + _NEVER_EDIT)
    return None


def cli_caller(stdin=None) -> str:
    """`ticli <verb>` is the human only from a real terminal (ADR-0007)."""
    stream = sys.stdin if stdin is None else stdin
    try:
        return HUMAN if stream.isatty() else AGENT
    except (AttributeError, ValueError):
        return AGENT


class Commands:
    def __init__(self, player, sleep=time.sleep):
        self.player = player
        self._sleep = sleep

    def execute(self, name: str, args: Optional[dict] = None, caller: str = HUMAN,
                key=None) -> dict:
        args = dict(args or {})
        cmd = COMMANDS.get(name)
        if cmd is None:
            return _error("unknown_command", f"No command named {name!r}.",
                          "Run `ticli agent docs` for the command list.")
        caller = HUMAN if caller == HUMAN else AGENT
        p = self.player
        if caller == AGENT:
            refused = gate(name, caller, key, p.config, read=cmd.read,
                           dangerous=cmd.is_dangerous(p, args), sleep=self._sleep)
            if refused:
                return refused
            if cmd.read and not p.config.get("allow_ai_control", True):
                return offline_read(name, args, p.config)
        try:
            result = cmd.handler(p, args)
        except CommandError as e:
            return _error(e.code, e.reason, e.fix)
        except Exception as e:
            if caller == HUMAN:
                raise
            return _error("failed", f"{type(e).__name__}: {e}")
        if caller == AGENT and not cmd.read:
            p._note_agent_action(AGENT_TOASTS.get(name, name))
        return {"ok": True, "result": result}


# ── lookups ──


def _sid(obj) -> str:
    return str(getattr(obj, "id", "") or "")


def _tracks(p, ids, *first) -> list:
    """Track objects for ids, from lists already loaded (0 requests); an unknown
    id costs one request. Pools listed first win, so a caller's own row is used."""
    pools = [*first, [p._current_track], p._queue, p._browse_tracks,
             p._artist_section_tracks(),
             [row["obj"] for row in p._search_results if row["type"] == "track"],
             p._download_tracks]
    known = {}
    for pool in pools:
        for track in pool or []:
            if track is not None:
                known.setdefault(_sid(track), track)
    tracks = []
    for tid in ids:
        track = known.get(str(tid))
        if track is None:
            track = p.session.track(tid)
        tracks.append(track)
    return tracks


def _ids(args, field="track_ids") -> list:
    ids = args.get(field)
    if ids is None and "track_id" in args:
        ids = [args["track_id"]]
    if not isinstance(ids, list) or not ids:
        raise CommandError("bad_args", f"{field} must be a non-empty list of track ids.")
    return ids


def _index(args, default=0) -> int:
    index = args.get("index", default)
    if isinstance(index, bool) or not isinstance(index, int):
        raise CommandError("bad_args", "index must be an integer.")
    return index


def _track_json(track) -> Optional[dict]:
    if track is None:
        return None
    album = getattr(track, "album", None)
    return {"id": getattr(track, "id", None), "title": getattr(track, "name", None),
            "artists": [a.name for a in (getattr(track, "artists", None) or [])],
            "album": getattr(album, "name", None) if album else None,
            "duration": getattr(track, "duration", None)}


def _play_list(p, tracks, index) -> dict:
    if not tracks:
        raise CommandError("empty", "Nothing to play.")
    if not 0 <= index < len(tracks):
        raise CommandError("bad_args", f"index must be 0..{len(tracks) - 1}.")
    p._queue = list(tracks)
    p._play_queue_index(index)
    return {"queue_length": len(tracks), "index": index}


# ── handlers ──


def _status(p, args) -> dict:
    track = p._current_track
    cfg = p.config
    return {
        "track": _track_json(track),
        "playing": bool(p._playing),
        "position": round(p._get_position(), 1) if track else 0,
        "queue": {"length": len(p._queue), "index": p._queue_index},
        "switches": switches(cfg),
    }


def switches(cfg) -> dict:
    return {"allow_ai_control": bool(cfg.get("allow_ai_control", True)),
            "allow_dangerous_commands": bool(cfg.get("allow_dangerous_commands", False)),
            "key_required": bool(coerce(get_spec("ai_control_key"), cfg.get("ai_control_key")))}


def _toggle(p, args):
    p._toggle_play()


def _pause(p, args):
    if p._playing:
        p._toggle_play()


def _resume(p, args):
    if not p._playing:
        p._toggle_play()


def _seek(p, args):
    if "delta" in args:
        p._seek_by(float(args["delta"]))
    elif "position" in args:
        p._seek_by(float(args["position"]) - p._get_position())
    else:
        raise CommandError("bad_args", "seek takes delta or position (seconds).")


def _next(p, args):
    p._next_track()


def _prev(p, args):
    p._prev_track()


def _queue_list(p, args) -> dict:
    return {"index": p._queue_index, "tracks": [_track_json(t) for t in p._queue]}


def _queue_play(p, args):
    p._play_queue_index(_index(args))


def _queue_remove(p, args) -> dict:
    index = _index(args)
    if not 0 <= index < len(p._queue):
        raise CommandError("bad_args", "No queue entry at that index.")
    removing_current = index == p._queue_index
    p._queue.pop(index)
    if index < p._queue_index:
        p._queue_index -= 1
    elif removing_current:
        if p._queue:
            p._queue_index = min(p._queue_index, len(p._queue) - 1)
            p._play_track(p._queue[p._queue_index])
        else:
            p._playing = False
            p._current_track = None
            if p.audio:
                p.audio.stop()
    return {"queue_length": len(p._queue)}


def _play_track_cmd(p, args) -> dict:
    tracks = _tracks(p, _ids(args), [r["obj"] for r in p._search_results if r["type"] == "track"])
    return _play_list(p, tracks[:1], 0)


def _play_album(p, args) -> dict:
    album_id = str(args.get("id", ""))
    if p._browse_source == ("album", album_id) and p._browse_tracks:
        tracks = list(p._browse_tracks)
    else:
        tracks = list(p.session.album(album_id).tracks())
    return _play_list(p, tracks, _index(args))


def _play_playlist(p, args) -> dict:
    playlist_id = str(args.get("id", ""))
    if p._browse_source == ("playlist", playlist_id) and p._browse_tracks:
        tracks = list(p._browse_tracks)
    else:
        tracks = (p._cache.get_playlist_tracks(playlist_id)
                  or list(p.session.playlist(playlist_id).tracks()))
    return _play_list(p, tracks, _index(args))


def _play_artist(p, args) -> dict:
    artist_id = str(args.get("id", ""))
    section = args.get("section", "tracks")
    if section not in ("tracks", "suggestions"):
        raise CommandError("bad_args", "section must be tracks or suggestions.")
    record = p._artist_sections.get((artist_id, section))
    if record and record["state"] == "ready":
        tracks = [row["obj"] for row in record["items"] if row["type"] == "track"]
    else:
        tracks = p._fetch_artist_section(p.session.artist(artist_id), section, 50)
    return _play_list(p, tracks, _index(args))


def _radio(p, args):
    p._start_track_radio()


def _like(on: bool):
    def handler(p, args):
        current = p._current_track
        tid = args.get("track_id", getattr(current, "id", None))
        if tid is None:
            raise CommandError("no_track", "Nothing is playing; pass track_id.")

        def _run():
            try:
                if on:
                    p.session.user.favorites.add_track(tid)
                    p._liked_ids.add(tid)
                else:
                    p.session.user.favorites.remove_track(tid)
                    p._liked_ids.discard(tid)
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()
        return {"track_id": tid}
    return handler


def _editable_playlist(p, playlist_id):
    for playlist in p._editable_playlists:
        if _sid(playlist) == str(playlist_id):
            return playlist
    return None


def _playlist_add(p, args) -> dict:
    if p._picker_busy:
        raise CommandError("busy", "A playlist change is still in flight.")
    playlist_id = args.get("id", "")
    ids = [str(t) for t in _ids(args)]
    playlist = _editable_playlist(p, playlist_id)
    p._picker_busy = True

    def _run():
        target = playlist
        try:
            if target is None:
                target = p.session.playlist(playlist_id)
            added = target.add(ids)
            p._remember_last_playlist(target)
            p._set_toast(f'{"Added to" if added else "Already in"} "{target.name}"')
        except Exception:
            p._set_toast("Failed to add to playlist")
        finally:
            p._picker_busy = False
        p._wake()

    threading.Thread(target=_run, daemon=True).start()
    return {"accepted": True}


def _playlist_create(p, args) -> dict:
    if p._picker_busy:
        raise CommandError("busy", "A playlist change is still in flight.")
    name = str(args.get("name") or "").strip()
    if not name:
        raise CommandError("bad_args", "Playlist name can't be empty.")
    ids = [str(t) for t in args.get("track_ids") or []]
    # Set first: a second Enter on the same tick must already see this, or the playlist is created twice.
    p._picker_busy = True

    def _run():
        try:
            playlist = p.session.user.create_playlist(name, "")
        except Exception:
            p._set_toast(f'Failed to create "{name}"')
            p._picker_busy = False
            p._wake()
            return
        p._remember_last_playlist(playlist)
        pid = _sid(playlist)
        p._editable_playlists = [playlist] + [
            q for q in p._editable_playlists if not pid or _sid(q) != pid]
        if not ids:
            p._set_toast(f'Created "{name}"')
            p._picker_busy = False
            p._wake()
            return
        try:
            playlist.add(ids)
            p._set_toast(f'Created "{name}" and added track')
        except Exception:
            p._set_toast(f'Created "{name}", but failed to add track')
        finally:
            p._picker_busy = False
        p._wake()

    threading.Thread(target=_run, daemon=True).start()
    return {"accepted": True}


def _playlist_remove(p, args) -> dict:
    playlist_id = str(args.get("id", ""))
    index = _index(args, None)
    pl = p._browse_playlist
    if pl is None or _sid(pl) != playlist_id:
        raise CommandError("not_open", "Removing needs the playlist open in the TUI.")
    if not 0 <= index < len(p._browse_tracks):
        raise CommandError("bad_args", "No track at that index.")
    if p._browse_remove_busy:
        raise CommandError("busy", "A removal is still in flight.")
    track = p._browse_tracks[index]
    p._browse_remove_busy = True

    def _run():
        try:
            if pl.remove_by_index(index):
                remaining = [t for i, t in enumerate(p._browse_tracks) if i != index]
                p._browse_tracks = remaining
                p._browse_cursor = min(p._browse_cursor, len(remaining) - 1)
                p._set_toast(f'Removed "{track.name}" from {pl.name}')
            else:
                p._set_toast("Failed to remove from playlist")
        except Exception:
            p._set_toast("Failed to remove from playlist")
        finally:
            p._browse_remove_busy = False

    threading.Thread(target=_run, daemon=True).start()
    return {"accepted": True}


def _download(p, args) -> dict:
    tier = str(args.get("tier") or p._quality_name).upper()
    tracks = _tracks(p, _ids(args), [p._download_track], p._download_tracks)
    if len(tracks) > 1:
        p._start_bulk_download_job(tier, tracks=tracks, label=args.get("label"))
    else:
        p._download_track = tracks[0]
        p._start_download_job(tier)
    return {"accepted": True, "tracks": len(tracks), "tier": tier}


def _download_cancel(p, args):
    p._cancel_download()


def _download_list(p, args) -> dict:
    return {"downloads": [{k: row[k] for k in ("id", "title", "artist", "tier", "bytes")}
                          for row in p._download_rows()]}


def _download_delete(p, args) -> dict:
    track_id = str(args.get("track_id", ""))
    title = next((row["title"] for row in p._download_rows() if row["id"] == track_id), None)
    if downloads.remove(track_id):
        p._forget_downloads()
        p._set_toast(f"Deleted {title}")
        return {"deleted": True}
    p._set_toast("Could not delete that file")
    return {"deleted": False}


def _refetch(p, args):
    p._start_refetch_job()


def _refetch_cancel(p, args):
    p._cancel_refetch()


def _settings_view(cfg) -> dict:
    return {spec["key"]: (bool(coerce(spec, cfg.get(spec["key"]))) if spec["kind"] == "secret"
                          else cfg.get(spec["key"], spec["default"]))
            for spec in SETTINGS_SPEC}


def _settings_get(p, args) -> dict:
    return _settings_view(p.config)


def _setting_spec(args) -> dict:
    key = args.get("key")
    if key in PROTECTED_KEYS:
        raise CommandError("protected_setting", f"{key} can't be set by a command.", _NEVER_EDIT)
    try:
        return get_spec(key)
    except KeyError:
        raise CommandError("unknown_setting", f"No setting named {key!r}.")


def _settings_set(p, args) -> dict:
    spec = _setting_spec(args)
    value = coerce(spec, args.get("value"))
    if spec["kind"] == "int":
        value = min(value, p._setting_ceiling(spec))
    key = spec["key"]
    if value == p.config.get(key, spec["default"]):
        return {"key": key, "value": value, "changed": False}
    p.config[key] = value
    p._apply_setting(key, value)
    save_config(p.config)
    return {"key": key, "value": value, "changed": True}


def _settings_set_dangerous(p, args) -> bool:
    key = args.get("key")
    if key not in ("cache_budget_gb", "cache_metadata"):
        return False
    spec = get_spec(key)
    value = coerce(spec, args.get("value"))
    if key == "cache_metadata":
        return value is False
    return value < coerce(spec, p.config.get(key, spec["default"]))


def _cache_clear(p, args) -> dict:
    removed, kept = p._cache.clear_audio()
    if kept:
        p._set_toast(f"Cleared {removed} songs, {kept} still in use")
    else:
        p._set_toast(f"Cleared {removed} song{'' if removed == 1 else 's'}")
    return {"removed": removed, "kept": kept}


def _login_pkce(p, args):
    p._upgrade_to_pkce()


def _logout(p, args):
    from ticli.utils.credential_store import delete_tokens
    delete_tokens()
    p.audio.stop()
    p._playing = False
    p._current_track = None
    p._queue = []
    p._queue_index = -1
    p.running = False
    p.console.print("[yellow]Logged out. Tokens cleared.[/yellow]")


def _search(p, args) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise CommandError("bad_args", "query must not be empty.")
    p._reset_search_results()
    p._search_key = query
    p._apply_search_scope()
    return {"accepted": True}


def _library_playlists(p, args) -> dict:
    p._load_playlists()
    return {"playlists": [{"id": _sid(pl), "name": pl.name} for pl in p._playlists]}


COMMANDS = {cmd.name: cmd for cmd in (
    Command("status", _status, read=True),
    Command("toggle", _toggle, tidal=True),
    Command("pause", _pause),
    Command("resume", _resume, tidal=True),
    Command("seek", _seek, tidal=True),
    Command("next", _next, tidal=True),
    Command("prev", _prev, tidal=True),
    Command("queue.list", _queue_list, read=True),
    Command("queue.play", _queue_play, tidal=True),
    Command("queue.remove", _queue_remove, tidal=True),
    Command("play.track", _play_track_cmd, tidal=True),
    Command("play.album", _play_album, tidal=True),
    Command("play.playlist", _play_playlist, tidal=True),
    Command("play.artist", _play_artist, tidal=True),
    Command("play.radio", _radio, tidal=True),
    Command("like", _like(True), tidal=True),
    Command("unlike", _like(False), tidal=True),
    Command("playlist.create", _playlist_create, tidal=True),
    Command("playlist.add", _playlist_add, tidal=True),
    Command("playlist.remove", _playlist_remove, tidal=True, dangerous=True),
    Command("download", _download, tidal=True),
    Command("download.cancel", _download_cancel),
    Command("download.list", _download_list, read=True),
    Command("download.delete", _download_delete, dangerous=True),
    Command("refetch", _refetch, tidal=True),
    Command("refetch.cancel", _refetch_cancel),
    Command("settings.get", _settings_get, read=True),
    Command("settings.set", _settings_set, dangerous=_settings_set_dangerous),
    Command("cache.clear", _cache_clear, dangerous=True),
    Command("login.pkce", _login_pkce, tidal=True, dangerous=True),
    Command("logout", _logout, dangerous=True),
    Command("search", _search, read=True, tidal=True),
    Command("library.playlists", _library_playlists, read=True, tidal=True),
)}

AGENT_TOASTS = {
    "toggle": "play/pause", "pause": "paused", "resume": "resumed", "seek": "seeked",
    "next": "next track", "prev": "previous track", "queue.play": "played from the queue",
    "queue.remove": "removed a queue entry", "play.track": "played a track",
    "play.album": "played an album", "play.playlist": "played a playlist",
    "play.artist": "played an artist", "play.radio": "started radio", "like": "liked a track",
    "unlike": "unliked a track", "playlist.create": "created a playlist",
    "playlist.add": "added to a playlist", "playlist.remove": "removed from a playlist",
    "download": "started a download", "download.cancel": "cancelled the download",
    "download.delete": "deleted a download", "refetch": "started a re-fetch",
    "refetch.cancel": "cancelled the re-fetch", "settings.set": "changed a setting",
    "cache.clear": "cleared the cache", "login.pkce": "switched login", "logout": "logged out",
}


# ── reads from disk, for agents while AI control is off: zero requests, no player ──


def _saved_state() -> dict:
    try:
        data = json.loads((throttle.STATE_DIR / "player_state.json").read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _record_json(record) -> dict:
    record = record if isinstance(record, dict) else {}
    return {"id": record.get("id"), "title": record.get("name"),
            "artists": record.get("artists") or [], "album": record.get("album"),
            "duration": record.get("duration")}


def _disk_queue(state) -> tuple:
    records = state.get("tracks")
    tracks = [_record_json(r) for r in records] if isinstance(records, list) else []
    index = state.get("queue_index", -1)
    return tracks, index if isinstance(index, int) else -1


def _disk_status(cfg, args) -> dict:
    state = _saved_state()
    tracks, index = _disk_queue(state)
    return {"track": tracks[index] if 0 <= index < len(tracks) else None,
            "playing": False, "position": state.get("position", 0),
            "queue": {"length": len(tracks), "index": index},
            "switches": switches(cfg), "source": "disk"}


def _disk_queue_list(cfg, args) -> dict:
    tracks, index = _disk_queue(_saved_state())
    return {"index": index, "tracks": tracks, "source": "disk"}


def _disk_playlists(cfg, args) -> dict:
    playlists = MetadataCache().get_playlists() or []
    return {"playlists": [{"id": _sid(pl), "name": pl.name} for pl in playlists],
            "source": "disk"}


def _disk_downloads(cfg, args) -> dict:
    rows = []
    for row in downloads.present():
        title, artist, _album = downloads.describe(row["entry"].get("path") or "")
        rows.append({"id": row["id"], "title": title, "artist": artist, "bytes": row["bytes"]})
    return {"downloads": rows, "source": "disk"}


def _disk_search(cfg, args) -> dict:
    query = str(args.get("query") or "").strip().lower()
    if not query:
        raise CommandError("bad_args", "query must not be empty.")
    cache = MetadataCache()
    playlists = [{"id": _sid(pl), "name": pl.name} for pl in cache.get_playlists() or []
                 if query in pl.name.lower()]
    seen, tracks = set(), []
    for _pid, record in cache.iter_tracks():
        found = _record_json(record)
        text = " ".join([found["title"] or "", *found["artists"]]).lower()
        if query in text and found["id"] not in seen:
            seen.add(found["id"])
            tracks.append(found)
    return {"playlists": playlists, "tracks": tracks, "source": "disk"}


_DISK_READS = {
    "status": _disk_status, "queue.list": _disk_queue_list,
    "settings.get": lambda cfg, args: {**_settings_view(cfg), "source": "disk"},
    "library.playlists": _disk_playlists, "download.list": _disk_downloads,
    "search": _disk_search,
}


def offline_read(name: str, args: Optional[dict] = None, cfg: Optional[dict] = None) -> dict:
    """A read answered from ticli's files alone: never a request, never the player."""
    reader = _DISK_READS.get(name)
    if reader is None:
        return _error("ai_control_off", "AI control is off; only reads from disk are allowed.",
                      'Ask your human to turn on "Allow AI control". ' + _NEVER_EDIT)
    try:
        return {"ok": True, "result": reader(load_config() if cfg is None else cfg, args or {})}
    except CommandError as e:
        return _error(e.code, e.reason, e.fix)
