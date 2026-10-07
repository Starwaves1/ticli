"""Agent commands inside the player: the TIDAL queue and the agent reply (ADR-0001, ADR-0007).

Every agent command that reaches TIDAL waits its turn here, in arrival order
across all agent clients, each request spaced by `utils.throttle`. Reads block
their caller; actions are answered at once with a queue position and ETA.
Waiting adds to one playlist merge into one `Playlist.add`, and waiting likes
into one favourites POST. Must not import `ticli.player`.
"""

import itertools
import math
import threading
import time
from collections import deque
from typing import Callable, Optional

from ticli import ipc
from ticli.commands import (
    ADD_LIMIT, COMMANDS, LIST_SOURCES, _live_playlist, _local_list, queue_add_cost, tripped_error,
    unknown_tracks,
)
from ticli.utils import throttle

SPACING = throttle.MIN_INTERVAL_SECONDS
WAITS = frozenset({"playlist.create", "queue.add"})  # actions whose answer the agent needs before going on
MERGES = frozenset({"playlist.add", "like"})
# Usually local: they move the stream already playing. Ordered with the queue, but take no slot.
LOCAL_MOSTLY = frozenset({"seek", "resume", "toggle"})
DONE_KEPT = 5
NEXT_MAX = 5
FINDS_TRACKS = frozenset({"search", "album.tracks", "playlist.tracks", "artist.section",
                          "mix.tracks", "library.favorites"})

_local = threading.local()


class _Ctx:
    def __init__(self, acquire):
        self.acquire = acquire
        self.slots = 0
        self.requests = 0


def _check_trip(response) -> None:
    status = getattr(response, "status_code", None)
    if status == 429:
        raise throttle.Tripped(throttle.trip("http_429"))
    if status == 401:
        try:
            sub_status = response.json().get("subStatus")
        except Exception:
            sub_status = None
        # A plain 401 is a dead token; 4006 is TIDAL's bot-detection escalation.
        if sub_status == 4006:
            raise throttle.Tripped(throttle.trip("substatus_4006"))


def instrument(session) -> None:
    """Pace and count the requests the queue's own thread makes; trip on 429/4006.
    Other threads (the TUI's, playback's) pass straight through."""
    try:
        http = getattr(session, "request_session", None)
    except Exception:  # a stand-in session with no HTTP layer
        http = None
    if http is None or getattr(http, "_ticli_paced", False):
        return
    original = http.request

    def request(*args, **kwargs):
        ctx = getattr(_local, "ctx", None)
        if ctx is None:
            return original(*args, **kwargs)
        if ctx.requests >= ctx.slots:
            ctx.acquire()
            ctx.slots += 1
        ctx.requests += 1
        response = original(*args, **kwargs)
        _check_trip(response)
        return response

    http.request = request
    http._ticli_paced = True


def estimate(core, cmd: str, args: dict) -> int:
    """Requests a queued command is expected to make, for ETAs."""
    ids = args.get("track_ids") or [None]
    if cmd == "playlist.add":
        known = _live_playlist(core, args.get("id", "")) is not None
        return 2 * math.ceil(len(ids) / ADD_LIMIT) + (0 if known else 1)
    if cmd == "unlike":
        return len(ids)
    if cmd == "playlist.create":
        return 1 + (2 * math.ceil(len(ids) / ADD_LIMIT) if args.get("track_ids") else 0)
    if cmd == "download":
        # Unknown ids are looked up here; the stream requests are paced in the job itself.
        return unknown_tracks(core, ids) if args.get("track_ids") else 0
    if cmd == "refetch":
        return 0
    if cmd in ("playlist.delete", "playlist.rename", "playlist.describe"):
        return 1 if _live_playlist(core, args.get("id", "")) is not None else 2
    if cmd in ("download.album", "download.playlist"):
        kind = cmd.split(".")[1]
        return 0 if _local_list(core, kind, str(args.get("id", ""))) else LIST_SOURCES[kind][1]
    if cmd == "track.info":
        return unknown_tracks(core, [args.get("track_id")])
    if cmd in LOCAL_MOSTLY:
        return 0
    if cmd == "queue.add":
        return queue_add_cost(core, args)
    if cmd == "queue.remove":
        return 1 if args.get("index") == getattr(core, "_queue_index", None) else 0
    return 1 if COMMANDS[cmd].tidal else 0


class Job:
    __slots__ = ("id", "cmd", "args", "key", "est", "queued_at", "callbacks", "count", "ctx")

    def __init__(self, jid, cmd, args, key, est, queued_at):
        self.id, self.cmd, self.args, self.key = jid, cmd, dict(args), key
        self.est, self.queued_at = est, queued_at
        self.callbacks: list = []
        self.count = 1
        self.ctx: Optional[_Ctx] = None


def _merge_key(cmd, args) -> str:
    return str(args.get("id", "")) if cmd == "playlist.add" else ""


def _blocks_merge(job: Job, cmd: str, args: dict) -> bool:
    if cmd == "like":
        return job.cmd == "unlike"
    return str(job.args.get("id", "")) == str(args.get("id", ""))


class TidalQueue:
    def __init__(self, run: Callable, estimate: Callable, clock=time.time, sleep=time.sleep,
                 on_idle: Optional[Callable] = None):
        self._run = run
        self._estimate = estimate
        self.clock, self.sleep = clock, sleep
        self._on_idle = on_idle
        self._cond = threading.Condition()
        self._jobs: list = []
        self._running: Optional[Job] = None
        self._done: deque = deque(maxlen=DONE_KEPT)
        self._ids = itertools.count(1)
        self._thread: Optional[threading.Thread] = None
        self._stopped = False

    # ── intake ──

    def submit(self, cmd: str, args: dict, key=None, on_done: Optional[Callable] = None,
               waits: bool = False) -> dict:
        return self.submit_many([(cmd, args, key, on_done, waits)])[0]

    def submit_many(self, items) -> list:
        """Queue several at once, so none starts before the rest could merge into it."""
        with self._cond:
            infos = [self._submit_locked(*item) for item in items]
            for info in infos:
                info["eta_s"] = self._eta_locked(info["job"])
            self._cond.notify()
        self._ensure_worker()
        return infos

    def _submit_locked(self, cmd, args, key, on_done, waits) -> dict:
        job = None if waits else self._merge_target(cmd, args)
        if job is not None:
            ids = job.args.get("track_ids") or []
            job.args["track_ids"] = list(dict.fromkeys([*ids, *args.get("track_ids", [])]))
            job.count += 1
            job.est = self._estimate(job.cmd, job.args)
        else:
            job = Job(next(self._ids), cmd, args, key, self._estimate(cmd, args), self.clock())
            self._jobs.append(job)
        if on_done is not None:
            job.callbacks.append(on_done)
        return {"job": job, "merged": job.count > 1,
                "position": self._jobs.index(job) + 1 + (self._running is not None)}

    def _merge_target(self, cmd, args) -> Optional[Job]:
        if cmd not in MERGES or not isinstance(args.get("track_ids"), list):
            return None
        for job in reversed(self._jobs):
            if job.cmd == cmd and _merge_key(job.cmd, job.args) == _merge_key(cmd, args):
                return job
            if _blocks_merge(job, cmd, args):
                return None
        return None

    # ── what's waiting ──

    def busy(self) -> bool:
        with self._cond:
            return bool(self._jobs or self._running)

    def _eta_locked(self, job: Optional[Job]) -> float:
        t = max(0.0, throttle.next_free_at() - self.clock())
        ahead = ([self._running] if self._running else []) + self._jobs
        for j in ahead:
            if j is job:
                return round(t + max(0, j.est - 1) * SPACING, 1)
            used = j.ctx.slots if j.ctx is not None else 0
            t += max(0, j.est - used) * SPACING
        return round(max(0.0, t - SPACING), 1) if ahead else 0.0

    def eta_last(self) -> float:
        with self._cond:
            return self._eta_locked(None)

    def pending(self) -> list:
        with self._cond:
            ahead = ([self._running] if self._running else []) + self._jobs
            rows = []
            for j in ahead:
                row = {"job": j.id, "cmd": j.cmd, "eta_s": self._eta_locked(j)}
                if j.count > 1:
                    row["merged"] = j.count
                if j is self._running:
                    row["running"] = True
                rows.append(row)
            return rows

    def done(self) -> list:
        with self._cond:
            return list(self._done)

    # ── the worker ──

    def _ensure_worker(self) -> None:
        with self._cond:
            if self._thread is not None or self._stopped:
                return
            self._thread = threading.Thread(target=self._work, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._cond:
            self._stopped = True
            self._cond.notify_all()

    def _work(self) -> None:
        while True:
            with self._cond:
                while not self._jobs and not self._stopped:
                    self._cond.wait()
                if self._stopped:
                    return
                head = self._jobs[0]
                delay = throttle.next_free_at() - self.clock() if head.est else 0
                if delay <= 0:
                    self._jobs.pop(0)
                    self._running = head
            if delay > 0:
                # Still queued while it waits, so arrivals can merge into it.
                self.sleep(delay)
                continue
            response = self._execute(head)
            with self._cond:
                self._running = None
                self._done.append(_done_row(head, response))
                idle = not self._jobs
            for callback in head.callbacks:
                try:
                    callback(response)
                except Exception:
                    pass
            if idle and self._on_idle is not None:
                self._on_idle()

    def _execute(self, job: Job) -> dict:
        ctx = _Ctx(lambda: throttle.acquire(now=self.clock, sleep=self.sleep))
        job.ctx = ctx
        try:
            if job.est:
                ctx.acquire()
                ctx.slots = 1
            waited = self.clock() - job.queued_at
            _local.ctx = ctx
            try:
                response = dict(self._run(job))
            finally:
                _local.ctx = None
        except throttle.Tripped as e:
            waited = self.clock() - job.queued_at
            response = tripped_error(e.record)
        except Exception as e:
            waited = self.clock() - job.queued_at
            response = {"ok": False, "code": "failed", "reason": f"{type(e).__name__}: {e}"}
        response["cost"] = {"requests": ctx.requests, "wait_s": round(max(0.0, waited), 1)}
        return response


def _done_row(job: Job, response: dict) -> dict:
    """What every caller merged into the job reads in `status`: enough to tell its own ids apart."""
    row = {"job": job.id, "cmd": job.cmd, "ok": bool(response.get("ok"))}
    if job.count > 1:
        row["merged"] = job.count
    result = response.get("result")
    if row["ok"] and isinstance(result, dict) and "added" in result:
        row["added"] = result["added"]
    if not row["ok"]:
        row["code"] = response.get("code")
        if "failed_from" in response:
            ids = job.args.get("track_ids") or []
            row.update(added=response.get("added", 0), failed_from=response["failed_from"],
                       not_added=[str(t) for t in ids[response["failed_from"]:]])
    return row


# ── the reply an agent reads ──


def _names(objs) -> list:
    return [getattr(a, "name", None) for a in (objs or []) if getattr(a, "name", None)]


def _track_json(t) -> dict:
    album = getattr(t, "album", None)
    return {"id": t.id, "title": t.name, "artists": _names(getattr(t, "artists", None)),
            "album": getattr(album, "name", None) if album else None,
            "duration_seconds": getattr(t, "duration", None),
            "explicit": bool(getattr(t, "explicit", False))}


def _album_json(a) -> dict:
    artists = getattr(a, "artists", None) or ([a.artist] if getattr(a, "artist", None) else [])
    return {"id": a.id, "title": a.name, "artists": _names(artists),
            "num_tracks": getattr(a, "num_tracks", None), "year": getattr(a, "year", None)}


def _playlist_json(p) -> dict:
    return {"id": str(p.id), "name": p.name, "num_tracks": getattr(p, "num_tracks", None),
            "description": getattr(p, "description", "") or ""}


_RENDER = {"track": _track_json, "album": _album_json, "playlist": _playlist_json,
           "artist": lambda a: {"id": a.id, "name": a.name},
           "mix": lambda m: {"id": m.id, "name": getattr(m, "title", None) or getattr(m, "name", None)}}


def render(value):
    """Plain JSON for an agent: TIDAL objects become the documented flat shapes."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {k: render(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [render(v) for v in value]
    kind = ipc.kind_of(value)
    return _RENDER[kind](value) if kind in _RENDER else str(value)


def compact_state(status: dict, pending: int = 0) -> dict:
    track = status.get("track")
    queue = status.get("queue") or {}
    switches = status.get("switches") or {}
    state = {"track": None if not track else {
                 "id": track.get("id"), "title": track.get("title"),
                 "artist": ", ".join(track.get("artists") or []),
                 "pos": round(status.get("position") or 0), "dur": track.get("duration_seconds")},
             "playing": bool(status.get("playing")),
             "queue": {"len": queue.get("length", 0), "index": queue.get("index", -1)},
             "switches": {"ai": switches.get("allow_ai_control", True),
                          "dangerous": switches.get("allow_dangerous_commands", False)}}
    if status.get("connectivity"):
        state["connectivity"] = status["connectivity"]
    if pending:
        state["pending"] = pending
    return state


def _first_id(result: dict, kind: str):
    rows = result.get(kind) or []
    return rows[0].get("id") if rows and isinstance(rows[0], dict) else None


def next_forms(cmd: str, result, state: dict) -> list:
    """Up to five `ticli agent ...` forms that apply right now, most useful first."""
    r = result if isinstance(result, dict) else {}
    if state.get("connectivity") in ("offline", "signed_out"):
        return _offline_forms(r, state)
    forms = []
    track = _first_id(r, "tracks") if cmd in FINDS_TRACKS else None
    if cmd == "resolve" and r.get("best"):
        track = (r["best"].get("track") or r["best"]).get("id")
    if track is not None:
        forms += [f"play track {track}", f"queue add {track}", f"playlist add <playlist_id> {track}",
                  f"like {track}"]
    playlist = (r.get("playlist") or {}).get("id") if isinstance(r.get("playlist"), dict) else None
    if cmd in ("playlist.create", "playlist.tracks") and playlist:
        forms += [f"playlist add {playlist} <track_id>", f"play playlist {playlist}"]
    if cmd == "library.playlists" and _first_id(r, "playlists"):
        forms.append(f"playlist show {_first_id(r, 'playlists')}")
    if cmd == "search" and _first_id(r, "albums"):
        forms.append(f"play album {_first_id(r, 'albums')}")
    if "queued" in r or state.get("pending"):
        forms.append("status")
    if state.get("playing"):
        forms += ["pause", "next"]
    elif state.get("track"):
        forms.append("resume")
    if state.get("queue", {}).get("len"):
        forms.append("queue list")
    if not forms:
        forms.append("search <query>")
    return list(dict.fromkeys(forms))[:NEXT_MAX]


def _offline_forms(r: dict, state: dict) -> list:
    """Offline only what plays from disk applies; TIDAL reads answer from the cache."""
    forms = []
    track = _first_id(r, "tracks")
    if track is not None:
        forms.append(f"play track {track}")
    forms.append("play downloads 0")
    if state.get("playing"):
        forms += ["pause", "next"]
    elif state.get("track"):
        forms.append("resume")
    forms += ["download list", "search <query>", "queue list"]
    return list(dict.fromkeys(forms))[:NEXT_MAX]


def error_reply(response: dict) -> dict:
    reply = {"ok": False, "code": response.get("code", "failed"),
             "reason": response.get("reason", "")}
    if response.get("fix"):
        reply["fix"] = response["fix"]
    for key in ("added", "failed_from", "candidates"):
        if key in response:
            reply[key] = response[key]
    return reply


def reply(cmd: str, response: dict, state: dict, cost: dict) -> dict:
    if not response.get("ok"):
        return error_reply(response)
    result = render(response.get("result"))
    return {"ok": True, "result": result, "state": state,
            "next": next_forms(cmd, result, state), "cost": cost}
