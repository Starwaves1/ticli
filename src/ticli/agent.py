"""The agent surface: ticli for callers that are programs.

`ticli agent <verb>` is a headless, JSON-speaking sibling of the TUI, built
after two sessions proved agents will otherwise interact with ticli the worst
way — importing internals, writing throwaway scripts, and firing requests at
a rate that has already gotten the owner's IP blocked by TIDAL once. Every
verb here goes through `utils.throttle`, so the working rules' rate limits
are enforced by code rather than by an agent having read them.

Contract, held everywhere:

- **stdout is JSON, always exactly one object.** Success is `{"ok": true,
  ...}`; failure is `{"ok": false, "error": <code>, "message": ...,
  "hint": ...}` and a nonzero exit. Anything meant for a human goes to
  stderr. An agent must never have to parse prose.
- **Errors are structured and honest.** `not_logged_in`, `rate_limited`
  (the trip — includes what tripped it and that a *human* clears it),
  `auth_failed`, `api_error`, `not_found`. Never a stack trace on stdout,
  never a silent empty result for what was actually a failure.
- **Requests are counted and spaced.** Each verb documents its request cost
  in `--help`; each network call takes one `throttle.acquire()` first. A 429
  or a 401/subStatus-4006 trips the stop for every future agent call until
  `ticli agent unblock`.

This module must not import `ticli.player` — that is the whole TUI's import
chain, and both `ticli --help` and `ticli agent --help` stay instant by
keeping tidalapi and player imports inside the functions that need them.
The playlist mutations here deliberately do not touch the running player's
queue or state files; a live TUI notices new playlists the way it notices
them changing on the server, by fetching.
"""

import json
import re
import sys

from ticli.utils import throttle
from ticli.utils.credential_store import load_tokens, save_tokens


def emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")


def fail(error: str, message: str, hint: str = "", **extra) -> "SystemExit":
    payload = {"ok": False, "error": error, "message": message}
    if hint:
        payload["hint"] = hint
    payload.update(extra)
    emit(payload)
    return SystemExit(1)


# ---------------------------------------------------------------------------
# Permissions (ADR-0007): the human's switches, read from config on every call


def _permit(verb: str, offline: str = "") -> dict:
    """Refuse what the switches don't allow. With AI control off, a verb that
    has an `offline` twin is answered from disk instead (returned), never TIDAL."""
    import click

    from ticli.commands import AGENT, gate, offline_read
    from ticli.utils.config import load_config

    ctx = click.get_current_context(silent=True)
    key = ((ctx.obj if ctx else None) or {}).get("key")
    cfg = load_config()
    refused = gate(verb, AGENT, key, cfg, read=bool(offline))
    if refused:
        raise fail(refused["code"], refused["reason"], hint=refused["fix"])
    if offline and not cfg["allow_ai_control"]:
        return offline_read(offline, cfg=cfg)["result"]
    return {}


# ---------------------------------------------------------------------------
# Session


def _tripped_exit(record: dict) -> SystemExit:
    return fail(
        "rate_limited",
        "TIDAL rate-limited this machine; all agent requests are stopped.",
        hint=(
            "Do not retry — retries extend the block. Report this to the "
            "user; a human runs `ticli agent unblock` once it is safe."
        ),
        tripped=record,
    )


def _acquire() -> None:
    """One request's worth of permission, or a structured refusal."""
    try:
        throttle.acquire()
    except throttle.Tripped as t:
        raise _tripped_exit(t.record)


def _trip_from(exc) -> None:
    """Inspect a failed request; trip the stop if it is the kind that blocks.

    A 429 always trips. A 401 trips only on TIDAL's subStatus 4006 — the
    bot-detection escalation — because an ordinary 401 is a dead token, which
    is an auth problem, not a ban in progress.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status == 429:
        record = throttle.trip("http_429", detail=str(exc))
        raise _tripped_exit(record)
    if status == 401:
        try:
            sub_status = response.json().get("subStatus")
        except Exception:
            sub_status = None
        if sub_status == 4006:
            record = throttle.trip("substatus_4006", detail=str(exc))
            raise _tripped_exit(record)
        raise fail(
            "auth_failed",
            "TIDAL rejected the stored session.",
            hint="Run `ticli` interactively to log in again.",
        )


def _session():
    """The blessed bootstrap: stored tokens to a working tidalapi session.

    The `is_pkce` flag must survive into `load_oauth_session` — it selects
    which TIDAL client refreshes the token, and getting it wrong kills the
    session hours later (see credential_store's module docstring). If loading
    refreshed the token on the way in, the refreshed copy is saved back so
    the next invocation doesn't repeat the round trip.
    """
    from ticli.utils.net import tidal_session  # deferred: keep `ticli agent --help` instant

    data = load_tokens()
    if not data:
        raise fail(
            "not_logged_in",
            "No stored TIDAL session.",
            hint="Run `ticli` interactively to log in (PKCE for FLAC).",
        )
    session = tidal_session()
    try:
        session.load_oauth_session(
            data["token_type"],
            data["access_token"],
            data.get("refresh_token"),
            data.get("expiry_time"),
            is_pkce=data.get("is_pkce", False),
        )
    except Exception as e:
        raise fail(
            "auth_failed",
            f"Could not restore the stored session: {type(e).__name__}",
            hint="Run `ticli` interactively to log in again.",
        )
    if session.access_token != data.get("access_token"):
        _persist(session)
    return session


def _persist(session) -> None:
    expiry = session.expiry_time
    try:
        save_tokens({
            "token_type": session.token_type,
            "access_token": session.access_token,
            "refresh_token": session.refresh_token,
            "expiry_time": expiry.isoformat() if hasattr(expiry, "isoformat") else expiry,
            "is_pkce": bool(session.is_pkce),
        })
    except Exception:
        pass  # a failed save costs one refresh next run, not correctness


def _api_call(fn, *args, **kwargs):
    """One throttled request: acquire a slot, run it, classify the failure.

    Every failure class carries a hint — the docs promise "each carries a
    hint saying what to do", and an audit caught api_error breaking that
    promise. A 404 is its own code: "no such id" and "the API broke" send
    an agent down different paths, and folding them together made the
    common mistake (a stale or mistyped id) look like an outage.
    """
    _acquire()
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        _trip_from(e)
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status == 404:
            raise fail(
                "not_found", f"{type(e).__name__}: {e}",
                hint=("No such id. Playlist ids come from `playlist list` or "
                      "`playlist create`; track ids from `resolve` or `search`."),
            )
        raise fail(
            "api_error", f"{type(e).__name__}: {e}",
            hint=("Not a rate limit and not auth — an unclassified API "
                  "failure. Report it to the human if it persists."),
        )


# ---------------------------------------------------------------------------
# Serialization — plain dicts an agent can rely on, never tidalapi objects


def _track_json(t) -> dict:
    album = getattr(t, "album", None)
    return {
        "id": t.id,
        "title": t.name,
        "artists": [a.name for a in (t.artists or [])],
        "album": getattr(album, "name", None),
        "duration_seconds": t.duration,
        "explicit": bool(getattr(t, "explicit", False)),
    }


def _album_json(a) -> dict:
    return {
        "id": a.id,
        "title": a.name,
        "artists": [ar.name for ar in (getattr(a, "artists", None) or [])],
        "num_tracks": getattr(a, "num_tracks", None),
        "year": getattr(a, "year", None),
    }


def _artist_json(a) -> dict:
    return {"id": a.id, "name": a.name}


def _playlist_json(p) -> dict:
    return {
        "id": str(p.id),
        "name": p.name,
        "num_tracks": getattr(p, "num_tracks", None),
        "description": getattr(p, "description", "") or "",
    }


# ---------------------------------------------------------------------------
# Verbs


def status(verify: bool) -> None:
    """Zero requests by default: report what is knowable without the network.
    `--verify` spends exactly one on `check_login`."""
    from ticli.commands import switches
    from ticli.utils.config import load_config

    data = load_tokens()
    payload = {
        "ok": True,
        "ai_control": switches(load_config()),
        "session_stored": bool(data),
        "flow": ("pkce" if data.get("is_pkce") else "device") if data else None,
        # PKCE is the only flow TIDAL streams FLAC to; device gets AAC.
        "flac_capable": bool(data and data.get("is_pkce")),
        "player_running": _player_running(),
        "throttle": {
            "min_interval_seconds": throttle.MIN_INTERVAL_SECONDS,
            "tripped": throttle.tripped(),
        },
    }
    if verify:
        _permit("status.verify")
        if not data:
            raise fail("not_logged_in", "No stored TIDAL session.",
                       hint="Run `ticli` interactively to log in.")
        session = _session()
        payload["verified"] = bool(_api_call(session.check_login))
        if payload["verified"]:
            _persist(session)
    emit(payload)


def _player_running() -> bool:
    """Whether a ticli TUI holds the instance lock right now. Best-effort —
    probing takes the flock for a moment, so a *starting* TUI could race it,
    and a filesystem that can't lock reads as not-running. Informational only."""
    import fcntl
    import os

    lock_path = throttle.STATE_DIR / "instance.lock"  # player._instance_lock_path
    if not lock_path.exists():
        return False
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
    return False


_SEARCH_RENDERERS = {"tracks": _track_json, "albums": _album_json,
                     "artists": _artist_json, "playlists": _playlist_json}


def search(query: str, types: tuple, limit: int) -> None:
    """One request regardless of how many types are asked for — limit is
    per-type server-side, the same property the TUI's reservoir leans on."""
    import tidalapi

    _permit("search")
    wanted = list(types) or ["track"]
    models = {"track": tidalapi.Track, "album": tidalapi.Album,
              "artist": tidalapi.Artist, "playlist": tidalapi.Playlist}
    session = _session()
    results = _api_call(
        session.search, query,
        models=[models[kind] for kind in wanted], limit=limit,
    )
    payload = {"ok": True, "query": query}
    for kind in wanted:
        key = f"{kind}s"
        payload[key] = [_SEARCH_RENDERERS[key](item) for item in results.get(key) or []]
    emit(payload)


# Version qualifiers that make a track a different listen from the plain
# title: a caller asking for "The Journey" plain does not mean the Alex
# Martyn Remix. "feat. …" is deliberately NOT here — a featured guest is the
# same recording, and treating it as a qualifier is what buried the real
# Folamour original in this feature's motivating incident.
_QUALIFIER = re.compile(
    r"\b(remix|edit|rework|bootleg|dub|instrumental|acoustic|acapella|"
    r"live|demo|radio|extended|vip|version|mix)\b", re.I)
_FEAT = re.compile(r"\s*[(\[]\s*(?:feat|ft|featuring|with)\.?\s[^)\]]*[)\]]", re.I)
_NOISE = re.compile(r"[^a-z0-9]+")


def _normalize(title: str) -> str:
    """Case/punctuation-blind comparison form, with featured-artist credits
    stripped — 'The Journey (feat. Zeke Manyika)' answers to 'The Journey'."""
    return _NOISE.sub(" ", _FEAT.sub("", title or "").lower()).strip()


def resolve(artist: str, title: str, limit: int) -> None:
    """One search request, then local ranking with the failure modes this
    surface exists to prevent encoded as rules:

    - **Artist is a gate, not a score.** A candidate whose artists don't
      include the requested one can appear in `candidates` but can never be
      `best` while any artist-matched candidate exists, and never `confident`.
      (The motivating incident: a scorer whose remix penalty could outweigh
      its artist bonus picked "The Journey" by H.E.R. over Folamour's.)
    - **Unrequested qualifiers demote within the gate.** A remix outranks
      nothing plain, but it still resolves when it is all there is — reported
      as such, so the caller can decide instead of being silently served one.
    - **`confident` is strict**: artist-matched, normalized-title equal, no
      unrequested qualifier. Anything less is the caller's judgement call,
      and the ranked list is there for it to make one.
    """
    import tidalapi

    _permit("resolve")
    session = _session()
    results = _api_call(
        session.search, f"{artist} {title}",
        models=[tidalapi.Track], limit=limit,
    )
    want_artist = _normalize(artist)
    want_title = _normalize(title)
    asked_qualified = bool(_QUALIFIER.search(title or ""))

    candidates = []
    for t in results.get("tracks") or []:
        names = " ".join(a.name for a in (t.artists or []))
        artist_match = want_artist in _normalize(names)
        got_title = _normalize(t.name)
        title_exact = got_title == want_title
        qualifier = (not asked_qualified) and bool(_QUALIFIER.search(t.name or ""))
        # Rank *within* the artist gate; the gate itself is the sort's first key.
        score = (2 if title_exact else (1 if want_title in got_title else 0)) - (1 if qualifier else 0)
        candidates.append({
            **_track_json(t),
            "artist_match": artist_match,
            "title_exact": title_exact,
            "unrequested_qualifier": qualifier,
            "score": score,
        })
    candidates.sort(key=lambda c: (c["artist_match"], c["score"]), reverse=True)

    best = candidates[0] if candidates else None
    confident = bool(
        best and best["artist_match"] and best["title_exact"]
        and not best["unrequested_qualifier"]
    )
    emit({
        "ok": True,
        "artist": artist,
        "title": title,
        "confident": confident,
        "best": best,
        "candidates": candidates,
    })


def playlist_list() -> None:
    disk = _permit("playlist.list", offline="library.playlists")
    if disk:
        emit({"ok": True, **disk})
        return
    session = _session()
    playlists = _api_call(session.user.playlists)
    emit({"ok": True, "playlists": [_playlist_json(p) for p in playlists]})


def playlist_show(playlist_id: str) -> None:
    _permit("playlist.show")
    session = _session()
    pl = _api_call(session.playlist, playlist_id)
    tracks = _api_call(pl.tracks)
    emit({"ok": True, "playlist": _playlist_json(pl),
          "tracks": [_track_json(t) for t in tracks]})


def playlist_create(name: str, description: str) -> None:
    _permit("playlist.create")
    session = _session()
    pl = _api_call(session.user.create_playlist, name, description or "")
    emit({"ok": True, "playlist": _playlist_json(pl)})


def playlist_add(playlist_id: str, track_ids: tuple) -> None:
    """Two requests (fetch the playlist, add the tracks). The server skips
    duplicates; `added` reports what it actually took."""
    _permit("playlist.add")
    session = _session()
    pl = _api_call(session.playlist, playlist_id)
    added = _api_call(pl.add, [str(t) for t in track_ids])
    emit({"ok": True, "playlist_id": str(playlist_id),
          "requested": len(track_ids),
          "added": len(added) if added is not None else 0})


def unblock() -> None:
    """The human's lever, not the agent's: clear a tripped stop (ADR-0001)."""
    from ticli import commands

    if commands.cli_caller() != commands.HUMAN:
        raise fail(
            "human_only", "Only a human at a terminal can clear the trip.",
            hint=("Stop and ask your human to run `ticli agent unblock` in a "
                  "terminal after checking why TIDAL blocked us."))
    was = throttle.unblock()
    emit({"ok": True, "was_tripped": was})
    if not was:
        print("note: no stop was in force", file=sys.stderr)
