"""Player-level commands: one named entry point for every action, and the
permission gate in front of it (ADR-0007).

`Commands(player).execute(name, args, caller, key)` takes and returns plain
JSON-serialisable data, so a socket can carry it unchanged (ADR-0008).
TUI keypresses call it as the human; screens, cursors and menus stay in the TUI.
This module must not import `ticli.player`: `ticli agent` imports the gate.
"""

import json
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from ticli.utils import downloads, throttle
from ticli.utils.cache import (
    CachedPlaylist, CachedTrack, MetadataCache, age_label, item_from, kind_of, record_of,
)
from ticli.utils.config import (
    PROTECTED_KEYS, SETTINGS_SPEC, UNREADABLE, UNREADABLE_MESSAGE, ConfigUnreadable,
    ai_key_matches, coerce, get_spec, load_config, update_config,
)

HUMAN = "human"
AGENT = "agent"

ARTIST_SECTIONS = ("tracks", "albums", "playlists", "suggestions")

WRONG_KEY_DELAY_SECONDS = 1.0

ONLINE, OFFLINE, SIGNED_OUT = "online", "offline", "signed_out"
OFFLINE_FIX = "Reconnect, or play downloaded/cached music (`download list`, `play downloads`)."
SIGN_IN_FIX = ("Press [o] in ticli's TUI to sign in again (agents: ask your human). "
               "Downloaded and cached music still plays.")

_NEVER_EDIT = ("Only your human can change this, in ticli's TUI settings ([c]). "
               "Ask them; never edit config.json or impersonate the TUI.")


class CommandError(Exception):
    def __init__(self, code: str, reason: str, fix: str = "", **extra):
        super().__init__(reason)
        self.code, self.reason, self.fix, self.extra = code, reason, fix, extra


def _error(code: str, reason: str, fix: str = "", **extra) -> dict:
    payload = {"ok": False, "code": code, "reason": reason, **extra}
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
    params: tuple = ()  # positional CLI order; a trailing "*" collects the rest into a list

    def is_dangerous(self, player, args) -> bool:
        return bool(self.dangerous(player, args) if callable(self.dangerous) else self.dangerous)


def gate(name: str, caller: str, key, cfg: dict, *, read: bool = False,
         dangerous: bool = False, sleep=time.sleep) -> Optional[dict]:
    """A refusal for an agent call the human's switches don't allow, else None."""
    if caller == HUMAN or name == "status":
        return None
    if cfg.get(UNREADABLE):
        return _error("config_unreadable", UNREADABLE_MESSAGE,
                      "Ask your human to fix or delete ticli's config.json. " + _NEVER_EDIT)
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

    def check(self, name: str, args: dict, caller: str = AGENT, key=None) -> Optional[dict]:
        """The refusal an agent would get for this call right now, else None."""
        cmd = COMMANDS.get(name)
        if cmd is None:
            return _error("unknown_command", f"No command named {name!r}.",
                          "Run `ticli agent docs` for the command list.")
        if caller == HUMAN:
            return None
        try:
            dangerous = cmd.is_dangerous(self.player, args)
        except Exception as e:
            return _error("bad_args", f"{type(e).__name__}: {e}")
        return gate(name, caller, key, self.player.config, read=cmd.read,
                    dangerous=dangerous, sleep=self._sleep)

    def execute(self, name: str, args: Optional[dict] = None, caller: str = HUMAN,
                key=None, inline: bool = False) -> dict:
        """`inline` runs a handler's TIDAL work on this thread and lets its failure out:
        the agent queue counts and paces those requests (ADR-0001)."""
        args = dict(args or {})
        caller = HUMAN if caller == HUMAN else AGENT
        refused = self.check(name, args, caller, key)
        if refused:
            return refused
        cmd = COMMANDS[name]
        p = self.player
        was, was_caller = getattr(_inline, "on", False), getattr(_inline, "caller", HUMAN)
        _inline.on, _inline.caller = inline, caller
        try:
            if caller == AGENT and cmd.read and not p.config.get("allow_ai_control", True):
                return offline_read(name, args, p.config)
            result = cmd.handler(p, args)
        except CommandError as e:
            return _error(e.code, e.reason, e.fix, **e.extra)
        except throttle.Tripped as e:
            return {**tripped_error(e.record), **getattr(e, "partial", {})}
        except Exception as e:
            if _transport(e):
                p._went_offline()
                e = offline_error(p)
                return _error(e.code, e.reason, e.fix)
            if caller == HUMAN:
                raise
            return {**classify(e), **getattr(e, "partial", {})}
        finally:
            _inline.on, _inline.caller = was, was_caller
        if caller == AGENT and not cmd.read:
            p._note_agent_action(AGENT_TOASTS.get(name, name))
        return {"ok": True, "result": result}


def tripped_error(record: Optional[dict] = None) -> dict:
    return _error("rate_limited", "TIDAL rate-limited this machine; every agent TIDAL command is stopped.",
                  "Stop and report to your human; do not retry. Only they clear it, by running "
                  "`ticli agent unblock` in a terminal.")


def _status_of(e) -> Optional[int]:
    while e is not None:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if isinstance(status, int):
            return status
        e = e.__cause__
    return None


def classify(e) -> dict:
    status = _status_of(e)
    if status == 404 or type(e).__name__ == "ObjectNotFound":
        return _error("not_found", f"{type(e).__name__}: {e}",
                      "No such id. Playlist ids come from `playlist list` or `playlist create`; "
                      "track ids from `resolve` or `search`.")
    if status == 401 or type(e).__name__ == "AuthenticationError":
        return _error("auth_failed", "TIDAL rejected the stored session.",
                      "Ask your human to open ticli and log in again.")
    return _error("api_error", f"{type(e).__name__}: {e}",
                  "Not a rate limit and not auth. Report it to your human if it persists.")


def auth_rejected(e) -> bool:
    return _status_of(e) == 401 or type(e).__name__ == "AuthenticationError"


def _transport(e) -> bool:
    from ticli.utils.net import is_transport_failure  # deferred: keep `ticli agent --help` instant
    return is_transport_failure(e)


def offline_error(p) -> CommandError:
    if getattr(p, "_connectivity", ONLINE) == SIGNED_OUT:
        return CommandError("signed_out", "TIDAL signed this player out.", SIGN_IN_FIX)
    return CommandError("offline", "Offline: TIDAL can't be reached.", OFFLINE_FIX)


def _require_online(p) -> None:
    """Writes are refused offline, not queued for later."""
    if p._reconnect() != ONLINE:
        raise offline_error(p)


def _read(p, key: str, live) -> tuple:
    """(rows, cached_at): live when TIDAL answers, else the list as last opened (ADR-0009)."""
    if p._reconnect() == ONLINE:
        try:
            return live(), None
        except Exception as e:
            if not _transport(e):
                raise
            p._went_offline()
    entry = p._cache.entry(key)
    if entry is None:
        raise offline_error(p)
    return [item_from(r) for r in entry["data"]], entry.get("fetched") or 0


def _tagged(kind: str, obj) -> dict:
    return {"_k": kind, **record_of(kind, obj)}


def _age(cached_at) -> dict:
    if cached_at is None:
        return {}
    return {"cached_at": round(cached_at), "age": age_label(cached_at), "offline": True}


_inline = threading.local()


_in_flight = 0
_in_flight_lock = threading.Lock()
idle_hook: Optional[Callable] = None


def in_flight() -> int:
    return _in_flight


def _background(fn) -> None:
    """The player must not leave while one of these still runs (a human CLI verb
    disconnects as soon as it is accepted); `idle_hook` wakes it when the last ends."""
    if getattr(_inline, "on", False):
        fn()
        return

    def run():
        global _in_flight
        try:
            fn()
        finally:
            with _in_flight_lock:
                _in_flight -= 1
            if idle_hook is not None:
                idle_hook()

    global _in_flight
    with _in_flight_lock:
        _in_flight += 1
    try:
        threading.Thread(target=run, daemon=True).start()
    except BaseException:
        with _in_flight_lock:
            _in_flight -= 1
        raise


def _by_agent() -> bool:
    return getattr(_inline, "caller", HUMAN) == AGENT


# The agent queue serialises its own playlist writes; only the TUI's and the human CLI's hold this.
_picker_idle = threading.Condition()


def _set_picker_busy(p, busy: bool) -> None:
    with _picker_idle:
        p._picker_busy = busy
        _picker_idle.notify_all()


def wait_picker_idle(p, timeout: float = 120.0) -> None:
    with _picker_idle:
        _picker_idle.wait_for(lambda: not p._picker_busy, timeout)


def _reraise_inline() -> None:
    """Call from an except block: the TUI's thread swallows, the agent queue must see it."""
    if getattr(_inline, "on", False):
        raise


# ── lookups ──


def _sid(obj) -> str:
    return str(getattr(obj, "id", "") or "")


def _lookup(p, ids, *first) -> list:
    """Track objects for ids from lists already loaded (0 requests); None where unknown.
    Pools listed first win, so a caller's own row is used."""
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
    indexed = None
    for tid in ids:
        track = known.get(str(tid)) or p._known.get(("track", str(tid)))
        if track is None:
            # A row the TUI found locally: playback resolves it only if no local copy exists.
            if indexed is None:
                wanted = {str(t) for t in ids}
                indexed = {str(t.id): t for t in local_tracks(p._cache) if str(t.id) in wanted}
            track = indexed.get(str(tid))
        tracks.append(track)
    return tracks


def _track_id(text):
    return int(text) if str(text).isdigit() else text


def download_tracks() -> list:
    """The download library as playable rows, newest first; no request (ADR-0009)."""
    rows = []
    for row in downloads.present():
        title, artist, album = downloads.describe(row["entry"].get("path") or "")
        rows.append(CachedTrack({"id": _track_id(row["id"]), "name": title,
                                 "artists": [artist] if artist else [], "album": album,
                                 "duration": row["entry"].get("duration")}))
    return rows


def local_tracks(cache) -> list:
    """Every track known on this machine: downloads, favourites, your playlists' rows."""
    seen, found = set(), []
    sources = (download_tracks(), cache.get_items("favorites:tracks") or [],
               (CachedTrack(r) for _pid, r in cache.iter_tracks()))
    for source in sources:
        for track in source:
            if str(track.id) not in seen and kind_of(track) == "track":
                seen.add(str(track.id))
                found.append(track)
    return found


def _text(obj) -> str:
    album = getattr(obj, "album", None)
    return " ".join([obj.name, *(a.name for a in getattr(obj, "artists", None) or []),
                     getattr(album, "name", "") if album else ""]).casefold()


def local_search(cache, query: str) -> dict:
    """Search what is on disk: your playlists, favourites and downloads (0 requests)."""
    needle = query.casefold()
    return {"tracks": [t for t in local_tracks(cache) if needle in _text(t)],
            "albums": [a for a in cache.get_items("favorites:albums") or [] if needle in _text(a)],
            "artists": [a for a in cache.get_items("favorites:artists") or [] if needle in _text(a)],
            "playlists": [pl for pl in cache.get_playlists() or [] if needle in pl.name.casefold()],
            "source": "local"}


def _tracks(p, ids, *first) -> list:
    """As `_lookup`; an unknown id costs one request, so it needs TIDAL."""
    found = _lookup(p, ids, *first)
    if any(t is None for t in found):
        _require_online(p)
    return [t if t is not None else p.session.track(tid) for tid, t in zip(ids, found)]


def unknown_tracks(p, ids) -> int:
    return sum(1 for t in _lookup(p, ids, [p._download_track], p._download_tracks) if t is None)


def _known(p, kind, obj_id):
    return p._known.get((kind, str(obj_id)))


def _list(p, source):
    """The tracks of a list this player fetched or the TUI shows, else None."""
    tracks = p._lists.get(source)
    if tracks:
        return list(tracks)
    if p._browse_source == source and p._browse_tracks:
        return list(p._browse_tracks)
    return None


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
            "duration_seconds": getattr(track, "duration", None)}


def _play_list(p, tracks, index) -> dict:
    if not tracks:
        raise CommandError("empty", "Nothing to play.")
    if not 0 <= index < len(tracks):
        raise CommandError("bad_args", f"index must be 0..{len(tracks) - 1}.")
    if p._connectivity != ONLINE:
        # Offline, start at the first entry from here that plays from disk (no reconnect for that).
        owned = {row["id"] for row in downloads.present()}
        local = [i for i in range(index, len(tracks)) if p._has_local_copy(tracks[i], owned)]
        index = local[0] if local else index
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
        "connectivity": p._connectivity,
    }


def switches(cfg) -> dict:
    return {"allow_ai_control": bool(cfg.get("allow_ai_control", True)),
            "allow_dangerous_commands": bool(cfg.get("allow_dangerous_commands", False)),
            "key_required": bool(cfg.get(UNREADABLE)
                                 or coerce(get_spec("ai_control_key"), cfg.get("ai_control_key")))}


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


def _queue_entry(p, args) -> int:
    """The index asked for, checked against `track_id` when given: with several
    clients the queue may have moved under a stale index."""
    index = _index(args)
    if not 0 <= index < len(p._queue):
        raise CommandError("bad_args", "No queue entry at that index.")
    if "track_id" in args and str(args["track_id"]) != _sid(p._queue[index]):
        raise CommandError("stale", "The queue changed; that index is another track now.",
                           "Check the queue below and retry with the current index.",
                           queue={"index": p._queue_index, "length": len(p._queue),
                                  "track_ids": [getattr(t, "id", None) for t in p._queue]})
    return index


def _queue_play(p, args):
    p._play_queue_index(_queue_entry(p, args))


def _queue_remove(p, args) -> dict:
    index = _queue_entry(p, args)
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
    tracks = _list(p, ("album", album_id)) or p._cache.get_items(f"album:{album_id}")
    if not tracks:
        _require_online(p)
        album = _known(p, "album", album_id) or p.session.album(album_id)
        tracks = list(album.tracks())
    return _play_list(p, tracks, _index(args))


def _play_playlist(p, args) -> dict:
    playlist_id = str(args.get("id", ""))
    tracks = _list(p, ("playlist", playlist_id)) or p._cache.get_playlist_tracks(playlist_id)
    if not tracks:
        _require_online(p)
        tracks = list(p.session.playlist(playlist_id).tracks())
    return _play_list(p, tracks, _index(args))


def _play_mix(p, args) -> dict:
    mix_id = str(args.get("id", ""))
    tracks = _list(p, ("mix", mix_id)) or p._cache.get_items(f"mix:{mix_id}")
    if not tracks:
        tracks = _mix_tracks(p, {"id": mix_id})["tracks"]
    return _play_list(p, tracks, _index(args))


def _play_downloads(p, args) -> dict:
    """Play the download library from an entry, newest first, as the downloads screen lists it."""
    tracks = download_tracks()
    index = _index(args)
    if "track_id" in args:
        ids = [str(t.id) for t in tracks]
        if str(args["track_id"]) not in ids:
            raise CommandError("not_found", "That track is not downloaded.", "Run `download list`.")
        index = ids.index(str(args["track_id"]))
    return _play_list(p, tracks, index)


def _artist_section_arg(args) -> str:
    section = args.get("section", "tracks")
    if section not in ARTIST_SECTIONS:
        raise CommandError("bad_args", "section must be one of " + ", ".join(ARTIST_SECTIONS) + ".")
    return section


def _play_artist(p, args) -> dict:
    artist_id = str(args.get("id", ""))
    section = _artist_section_arg(args)
    if section not in ("tracks", "suggestions"):
        raise CommandError("bad_args", "section must be tracks or suggestions.")
    tracks = p._lists.get(("artist:" + section, artist_id))
    record = p._artist_sections.get((artist_id, section))
    if tracks:
        tracks = list(tracks)
    elif record and record["state"] == "ready":
        tracks = [row["obj"] for row in record["items"] if row["type"] == "track"]
    else:
        rows = _artist_section(p, {"id": artist_id, "section": section, "limit": 50})["items"]
        tracks = [row["obj"] for row in rows if row["type"] == "track"]
    return _play_list(p, tracks, _index(args))


def _radio(p, args):
    _require_online(p)
    p._start_track_radio()


def _like(on: bool):
    def handler(p, args):
        if args.get("track_ids") is not None or "track_id" in args:
            ids = _ids(args)
        else:
            current = getattr(p._current_track, "id", None)
            if current is None:
                raise CommandError("no_track", "Nothing is playing; pass track_ids.")
            ids = [current]
        _require_online(p)

        def _run():
            try:
                favorites = p.session.user.favorites
                if on:
                    # One POST for the lot: tidalapi joins a list with commas.
                    favorites.add_track([str(t) for t in ids])
                    p._liked_ids.update(ids)
                else:
                    for tid in ids:  # tidalapi's remove_track takes one id
                        favorites.remove_track(str(tid))
                        p._liked_ids.discard(tid)
            except Exception:
                _reraise_inline()

        _background(_run)
        return {"track_ids": ids} if len(ids) > 1 else {"track_id": ids[0]}
    return handler


def _editable_playlist(p, playlist_id):
    for playlist in p._editable_playlists:
        if _sid(playlist) == str(playlist_id):
            return playlist
    return None


ADD_LIMIT = 100  # tidalapi Playlist.add: ids per POST


def _live_playlist(p, playlist_id):
    for found in (_editable_playlist(p, playlist_id), _known(p, "playlist", playlist_id)):
        if hasattr(found, "add"):
            return found
    return None


def _add_chunks(playlist, ids) -> list:
    """Each add is a POST plus tidalapi's reparse GET. A failure carries how far it got."""
    added = []
    for start in range(0, len(ids), ADD_LIMIT):
        try:
            added += playlist.add(ids[start:start + ADD_LIMIT]) or []
        except Exception as e:
            e.partial = {"added": len(added), "failed_from": start}
            raise
    return added


def _claim_picker(p) -> bool:
    """Whether this call holds `_picker_busy`: the agent queue (inline) never does."""
    if getattr(_inline, "on", False):
        return False
    if p._picker_busy:
        raise CommandError("busy", "A playlist change is still in flight.")
    _set_picker_busy(p, True)
    return True


def _playlist_add(p, args) -> dict:
    _require_online(p)
    playlist_id = args.get("id", "")
    ids = list(dict.fromkeys(str(t) for t in _ids(args)))
    playlist = args.get("playlist")
    if not hasattr(playlist, "add"):
        playlist = _live_playlist(p, playlist_id)
    held = _claim_picker(p)
    outcome = {"accepted": True}

    def _run():
        target = playlist
        try:
            if target is None:
                target = p.session.playlist(playlist_id)
            added = _add_chunks(target, ids)
            outcome["added"] = len(added)
            p._remember_last_playlist(target)
            p._set_toast(f'{"Added to" if added else "Already in"} "{target.name}"')
        except Exception:
            p._set_toast("Failed to add to playlist")
            _reraise_inline()
        finally:
            if held:
                _set_picker_busy(p, False)
            p._wake()

    _background(_run)
    return outcome


def _playlist_create(p, args) -> dict:
    name = str(args.get("name") or "").strip()
    if not name:
        raise CommandError("bad_args", "Playlist name can't be empty.")
    _require_online(p)
    ids = list(dict.fromkeys(str(t) for t in args.get("track_ids") or []))
    description = str(args.get("description") or "")
    # Claimed first: a second Enter on the same tick must already see this, or the playlist is created twice.
    held = _claim_picker(p)
    outcome = {"accepted": True}

    def _release():
        if held:
            _set_picker_busy(p, False)
        p._wake()

    def _run():
        try:
            playlist = p.session.user.create_playlist(name, description)
        except Exception:
            p._set_toast(f'Failed to create "{name}"')
            _release()
            _reraise_inline()
            return
        outcome["playlist"] = playlist
        p._remember("playlist", [playlist])
        p._remember_last_playlist(playlist)
        pid = _sid(playlist)
        p._editable_playlists = [playlist] + [
            q for q in p._editable_playlists if not pid or _sid(q) != pid]
        if not ids:
            p._set_toast(f'Created "{name}"')
            _release()
            return
        try:
            outcome["added"] = len(_add_chunks(playlist, ids))
            p._set_toast(f'Created "{name}" and added track{"" if len(ids) == 1 else "s"}')
        except Exception:
            p._set_toast(f'Created "{name}", but failed to add track{"" if len(ids) == 1 else "s"}')
            _reraise_inline()
        finally:
            _release()

    _background(_run)
    return outcome


def _playlist_remove(p, args) -> dict:
    playlist_id = str(args.get("id", ""))
    index = _index(args, None)
    source = ("playlist", playlist_id)
    pl = _known(p, "playlist", playlist_id)
    if pl is None and p._browse_playlist is not None and _sid(p._browse_playlist) == playlist_id:
        pl = p._browse_playlist
    tracks = _list(p, source)
    if pl is None or not hasattr(pl, "remove_by_index") or tracks is None:
        raise CommandError("not_loaded", "Open that playlist first so its rows are known.",
                           "Run playlist.tracks with its id, then remove by index.")
    if not 0 <= index < len(tracks):
        raise CommandError("bad_args", "No track at that index.")
    track = tracks[index]
    if "track_id" in args and str(args["track_id"]) != _sid(track):
        raise CommandError("stale", "That playlist changed; reopen it before removing.")
    if p._browse_remove_busy:
        raise CommandError("busy", "A removal is still in flight.")
    _require_online(p)
    p._browse_remove_busy = True

    def _run():
        try:
            if pl.remove_by_index(index):
                p._list_changed(source, [t for i, t in enumerate(tracks) if i != index])
                p._set_toast(f'Removed "{track.name}" from {pl.name}')
            else:
                p._set_toast("Failed to remove from playlist")
        except Exception:
            p._set_toast("Failed to remove from playlist")
            _reraise_inline()
        finally:
            p._browse_remove_busy = False
            p._wake()

    _background(_run)
    return {"accepted": True}


def _download(p, args) -> dict:
    _require_online(p)
    tier = str(args.get("tier") or p._quality_name).upper()
    tracks = _tracks(p, _ids(args), [p._download_track], p._download_tracks)
    if _by_agent():
        # One slot, each track's stream request through the shared 2 s throttle (ADR-0001).
        p._start_bulk_download_job(tier, tracks=tracks, label=args.get("label"), paced=True)
    elif len(tracks) > 1:
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
    _require_online(p)
    if _by_agent():
        p._start_refetch_job(paced=True)
    else:
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
    try:
        update_config({key: value})
    except ConfigUnreadable:
        raise CommandError("config_unreadable", UNREADABLE_MESSAGE,
                           "Fix or delete ticli's config.json, then try again.")
    p.config[key] = value
    p._apply_setting(key, value)
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
    _require_online(p)
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


SEARCH_KINDS = ("tracks", "albums", "artists", "playlists")


def _search(p, args) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        raise CommandError("bad_args", "query must not be empty.")
    limit, offset = int(args.get("limit") or 50), int(args.get("offset") or 0)
    kinds = [k if k.endswith("s") else k + "s" for k in args.get("types") or SEARCH_KINDS]
    if not set(kinds) <= set(SEARCH_KINDS):
        raise CommandError("bad_args", "types are track, album, artist, playlist.")
    key = f"search:{query.casefold()}"

    def live():
        # One GET whatever the scope: `types=` carries all four and `limit` is per type.
        results = p.session.search(query, models=p._search_models(), limit=limit, offset=offset)
        found = {kind: list(results.get(kind) or []) for kind in SEARCH_KINDS}
        if offset == 0:
            p._cache.put(key, [_tagged(kind[:-1], o) for kind in SEARCH_KINDS for o in found[kind]])
        return found

    try:
        found, cached_at = _read(p, key, live)
    except CommandError as e:
        if e.code not in (OFFLINE, SIGNED_OUT):
            raise
        found = local_search(p._cache, query)
        return {**{kind: found[kind][offset:offset + limit] for kind in kinds},
                "source": "local", "offline": True}
    if cached_at is not None:
        found = {kind: [o for o in found if kind_of(o) == kind[:-1]] for kind in SEARCH_KINDS}
    for kind, objs in found.items():
        p._remember(kind[:-1], objs)
    return {**{kind: found[kind] for kind in kinds}, **_age(cached_at)}


# Version qualifiers that make a track a different listen from the plain title.
# "feat." is deliberately absent: a featured guest is the same recording.
_QUALIFIER = re.compile(
    r"\b(remix|edit|rework|bootleg|dub|instrumental|acoustic|acapella|"
    r"live|demo|radio|extended|vip|version|mix)\b", re.I)
_FEAT = re.compile(r"\s*[(\[]\s*(?:feat|ft|featuring|with)\.?\s[^)\]]*[)\]]", re.I)
_NOISE = re.compile(r"[^a-z0-9]+")


def normalize_title(title: str) -> str:
    return _NOISE.sub(" ", _FEAT.sub("", title or "").lower()).strip()


def rank_tracks(tracks, artist: str, title: str) -> dict:
    """The strict matcher: artist is a gate, never a score; an unrequested
    qualifier demotes; `confident` only for artist match + equal title + no qualifier."""
    want_artist, want_title = normalize_title(artist), normalize_title(title)
    asked_qualified = bool(_QUALIFIER.search(title or ""))
    candidates = []
    for t in tracks:
        names = " ".join(a.name for a in (getattr(t, "artists", None) or []))
        got_title = normalize_title(t.name)
        exact = got_title == want_title
        qualifier = not asked_qualified and bool(_QUALIFIER.search(t.name or ""))
        score = (2 if exact else (1 if want_title in got_title else 0)) - (1 if qualifier else 0)
        candidates.append({"track": t, "artist_match": want_artist in normalize_title(names),
                           "title_exact": exact, "unrequested_qualifier": qualifier,
                           "score": score})
    candidates.sort(key=lambda c: (c["artist_match"], c["score"]), reverse=True)
    best = candidates[0] if candidates else None
    confident = bool(best and best["artist_match"] and best["title_exact"]
                     and not best["unrequested_qualifier"])
    return {"artist": artist, "title": title, "confident": confident, "best": best,
            "candidates": candidates}


def _resolve(p, args) -> dict:
    artist, title = str(args.get("artist") or ""), str(args.get("title") or "")
    if not artist or not title:
        raise CommandError("bad_args", "resolve needs artist and title.")
    if p._reconnect() != ONLINE:
        return {**rank_tracks(local_tracks(p._cache), artist, title), "source": "local",
                "offline": True}
    results = p.session.search(f"{artist} {title}", models=p._search_models()[:1],
                               limit=int(args.get("limit") or 10))
    tracks = list(results.get("tracks") or [])
    p._remember("track", tracks)
    return rank_tracks(tracks, artist, title)


def _page(args):
    """(limit, offset) when the caller pages through a long list, else None: one default page."""
    if "offset" not in args and "limit" not in args:
        return None
    return int(args.get("limit") or 100), int(args.get("offset") or 0)


def _album_tracks(p, args) -> dict:
    album_id = str(args.get("id", ""))
    page = _page(args)
    if page is not None:
        _require_online(p)
        album = _known(p, "album", album_id) or p.session.album(album_id)
        p._remember("album", [album])
        tracks = list(album.tracks(limit=page[0], offset=page[1]) or [])
        p._remember("track", tracks)
        return {"tracks": tracks, "num_tracks": getattr(album, "num_tracks", None)}
    key = f"album:{album_id}"
    holder = {}

    def live():
        album = _known(p, "album", album_id) or p.session.album(album_id)
        p._remember("album", [album])
        holder["album"] = album
        tracks = list(album.tracks() or [])
        p._cache.put_items(key, tracks)
        return tracks

    tracks, cached_at = _read(p, key, live)
    p._remember("track", tracks)
    p._lists[("album", album_id)] = tracks
    return {"tracks": tracks, "num_tracks": getattr(holder.get("album"), "num_tracks", len(tracks)),
            **_age(cached_at)}


def _cached_playlist(p, playlist_id):
    return next((pl for pl in p._cache.get_playlists() or [] if str(pl.id) == playlist_id),
                None) or CachedPlaylist({"id": playlist_id, "name": "Playlist"})


def _playlist_tracks(p, args) -> dict:
    playlist_id = str(args.get("id", ""))
    page = _page(args)
    if page is not None:
        _require_online(p)
        found = _known(p, "playlist", playlist_id)
        if found is None or getattr(found, "cached", False):
            found = p.session.playlist(playlist_id)
            p._remember("playlist", [found])
        tracks = list(found.tracks(limit=page[0], offset=page[1]) or [])
        p._remember("track", tracks)
        return {"playlist": found, "tracks": tracks,
                "num_tracks": getattr(found, "num_tracks", None)}
    holder = {}

    def live():
        found = _known(p, "playlist", playlist_id)
        if found is None or getattr(found, "cached", False):
            found = p.session.playlist(playlist_id)
            p._remember("playlist", [found])
        holder["playlist"] = found
        tracks = list(found.tracks() or [])
        p._cache.put_playlist_tracks(playlist_id, tracks)
        return tracks

    tracks, cached_at = _read(p, f"playlist:{playlist_id}", live)
    playlist = holder.get("playlist") or _known(p, "playlist", playlist_id) \
        or _cached_playlist(p, playlist_id)
    p._remember("track", tracks)
    p._lists[("playlist", playlist_id)] = tracks
    return {"playlist": playlist, "tracks": tracks,
            "num_tracks": getattr(playlist, "num_tracks", None) or len(tracks), **_age(cached_at)}


def _artist_section(p, args) -> dict:
    artist_id = str(args.get("id", ""))
    section = _artist_section_arg(args)
    limit = int(args.get("limit") or 20)
    key = f"artist:{artist_id}:{section}"

    def live():
        artist = _known(p, "artist", artist_id) or p.session.artist(artist_id)
        rows = p._fetch_artist_section(artist, section, limit)
        p._cache.put(key, [_tagged(r["type"], r["obj"]) for r in rows])
        return rows

    rows, cached_at = _read(p, key, live)
    if cached_at is not None:
        rows = [{"type": kind_of(o) or "track", "obj": o} for o in rows]
    for row in rows:
        p._remember(row["type"], [row["obj"]])
    p._lists[("artist:" + section, artist_id)] = [r["obj"] for r in rows if r["type"] == "track"]
    return {"items": rows, **_age(cached_at)}


def _library_playlists(p, args) -> dict:
    def live():
        playlists = list(p.session.user.playlists() or [])
        p._cache.put_playlists(playlists, editable_type=p._editable_type())
        p._editable_playlists = [pl for pl in playlists if p._is_editable(pl)]
        p._editable_playlists_time = time.time()
        return playlists

    playlists, cached_at = _read(p, "playlists", live)
    if cached_at is not None:
        playlists = p._cache.get_playlists() or []
    p._remember("playlist", playlists)
    return {"playlists": playlists, **_age(cached_at)}


FAVORITE_KINDS = ("tracks", "albums", "artists")


def _library_favorites(p, args) -> dict:
    kind = str(args.get("kind") or "tracks")
    if kind not in FAVORITE_KINDS:
        raise CommandError("bad_args", "kind must be tracks, albums or artists.")
    key = f"favorites:{kind}"

    def live():
        objs = list(getattr(p.session.user.favorites, kind)() or [])
        p._cache.put_items(key, objs)
        if kind == "tracks":
            p._liked_ids = {t.id for t in objs}
        return objs

    objs, cached_at = _read(p, key, live)
    p._remember(kind[:-1], objs)
    if kind == "tracks":
        p._lists[("favorites", "tracks")] = objs
    return {kind: objs, **_age(cached_at)}


def _library_mixes(p, args) -> dict:
    def live():
        mixes = [m for m in p.session.mixes() if kind_of(m) == "mix"]
        p._cache.put_items("mixes", mixes)
        return mixes

    mixes, cached_at = _read(p, "mixes", live)
    p._remember("mix", mixes)
    return {"mixes": mixes, **_age(cached_at)}


def _mix_tracks(p, args) -> dict:
    mix_id = str(args.get("id", ""))
    key = f"mix:{mix_id}"

    def live():
        mix = _known(p, "mix", mix_id)
        if mix is None or getattr(mix, "cached", False):
            mix = p.session.mix(mix_id)
        tracks = [t for t in mix.items() if kind_of(t) == "track"]
        p._cache.put_items(key, tracks)
        return tracks

    tracks, cached_at = _read(p, key, live)
    p._remember("track", tracks)
    p._lists[("mix", mix_id)] = tracks
    return {"tracks": tracks, **_age(cached_at)}


def _stop(p, args):
    p._stop_playback()


def _login_reload(p, args):
    p._reload_login()


def _history_add(p, args):
    p._add_to_history(str(args.get("query") or ""))


def _history_forget(p, args):
    query = str(args.get("query") or "")
    p._search_history = [q for q in p._search_history if q != query]


COMMANDS = {cmd.name: cmd for cmd in (
    Command("status", _status, read=True),
    Command("toggle", _toggle, tidal=True),
    Command("pause", _pause),
    Command("resume", _resume, tidal=True),
    Command("seek", _seek, tidal=True, params=("position",)),
    Command("next", _next, tidal=True),
    Command("prev", _prev, tidal=True),
    Command("queue.list", _queue_list, read=True),
    Command("queue.play", _queue_play, tidal=True, params=("index", "track_id")),
    Command("queue.remove", _queue_remove, tidal=True, params=("index", "track_id")),
    Command("play.track", _play_track_cmd, tidal=True, params=("track_id",)),
    Command("play.album", _play_album, tidal=True, params=("id", "index")),
    Command("play.playlist", _play_playlist, tidal=True, params=("id", "index")),
    Command("play.artist", _play_artist, tidal=True, params=("id", "section", "index")),
    Command("play.mix", _play_mix, tidal=True, params=("id", "index")),
    Command("play.downloads", _play_downloads, params=("index", "track_id")),
    Command("play.radio", _radio, tidal=True),
    Command("like", _like(True), tidal=True, params=("track_ids*",)),
    Command("unlike", _like(False), tidal=True, params=("track_ids*",)),
    Command("playlist.create", _playlist_create, tidal=True, params=("name", "track_ids*")),
    Command("playlist.add", _playlist_add, tidal=True, params=("id", "track_ids*")),
    Command("playlist.remove", _playlist_remove, tidal=True, dangerous=True,
            params=("id", "index", "track_id")),
    Command("download", _download, tidal=True, params=("track_ids*",)),
    Command("download.cancel", _download_cancel),
    Command("download.list", _download_list, read=True),
    Command("download.delete", _download_delete, dangerous=True, params=("track_id",)),
    Command("refetch", _refetch, tidal=True),
    Command("refetch.cancel", _refetch_cancel),
    Command("settings.get", _settings_get, read=True),
    Command("settings.set", _settings_set, dangerous=_settings_set_dangerous,
            params=("key", "value")),
    Command("cache.clear", _cache_clear, dangerous=True),
    Command("login.pkce", _login_pkce, tidal=True, dangerous=True),
    Command("logout", _logout, dangerous=True),
    Command("stop", _stop),
    Command("login.reload", _login_reload),
    Command("history.add", _history_add, params=("query",)),
    Command("history.forget", _history_forget, params=("query",)),
    Command("search", _search, read=True, tidal=True, params=("query",)),
    Command("resolve", _resolve, read=True, tidal=True, params=("artist", "title")),
    Command("album.tracks", _album_tracks, read=True, tidal=True, params=("id", "offset", "limit")),
    Command("playlist.tracks", _playlist_tracks, read=True, tidal=True,
            params=("id", "offset", "limit")),
    Command("artist.section", _artist_section, read=True, tidal=True,
            params=("id", "section", "limit")),
    Command("library.playlists", _library_playlists, read=True, tidal=True),
    Command("library.favorites", _library_favorites, read=True, tidal=True, params=("kind",)),
    Command("library.mixes", _library_mixes, read=True, tidal=True),
    Command("mix.tracks", _mix_tracks, read=True, tidal=True, params=("id",)),
)}

AGENT_TOASTS = {
    "toggle": "play/pause", "pause": "paused", "resume": "resumed", "seek": "seeked",
    "next": "next track", "prev": "previous track", "queue.play": "played from the queue",
    "queue.remove": "removed a queue entry", "play.track": "played a track",
    "play.album": "played an album", "play.playlist": "played a playlist",
    "play.artist": "played an artist", "play.mix": "played a mix",
    "play.downloads": "played your downloads", "play.radio": "started radio", "like": "liked a track",
    "unlike": "unliked a track", "playlist.create": "created a playlist",
    "playlist.add": "added to a playlist", "playlist.remove": "removed from a playlist",
    "download": "started a download", "download.cancel": "cancelled the download",
    "download.delete": "deleted a download", "refetch": "started a re-fetch",
    "refetch.cancel": "cancelled the re-fetch", "settings.set": "changed a setting",
    "cache.clear": "cleared the cache", "login.pkce": "switched login", "logout": "logged out",
    "stop": "stopped playback", "login.reload": "reloaded the login",
    "history.add": "added to search history", "history.forget": "edited search history",
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
            "duration_seconds": record.get("duration")}


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
    query = str(args.get("query") or "").strip()
    if not query:
        raise CommandError("bad_args", "query must not be empty.")
    found = local_search(MetadataCache(), query)
    return {"playlists": [{"id": _sid(pl), "name": pl.name} for pl in found["playlists"]],
            "tracks": [_track_json(t) for t in found["tracks"]], "source": "disk"}


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
    except Exception as e:
        return _error("local_read_failed", f"{type(e).__name__}: {e}",
                      "Report this to your human; nothing was sent to TIDAL.")
