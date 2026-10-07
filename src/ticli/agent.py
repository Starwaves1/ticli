"""The agent surface: ticli for callers that are programs.

`ticli agent <verb>` is a JSON-speaking client of the background player
(ADR-0008), built after agents imported internals and fired requests at a
rate that once got the owner's IP blocked by TIDAL. Verbs travel over the
socket as caller=agent; the player queues and paces every TIDAL request
(`ticli.agentq`), so the brake is code, not an agent having read the rules.

- **stdout is exactly one JSON object.** Errors are `code`, `reason`, `fix`
  and a nonzero exit; the original verbs keep `error`/`message`/`hint` too.
- **AI control off never starts the player**: reads come from disk, the rest
  is refused (ADR-0007).
- `status` and `unblock` stay local; `status --verify` is the one request
  made from this process, through `utils.throttle`.

Must not import `ticli.player`: `ticli agent --help` stays instant.
"""

import json
import sys

from ticli.utils import throttle
from ticli.utils.credential_store import load_tokens, save_tokens


def emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, separators=(",", ":"))
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
        read = offline_read(offline, cfg=cfg)
        if not read["ok"]:
            raise fail(read["code"], read["reason"], hint=read["fix"])
        return read["result"]
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
    if payload["player_running"]:
        payload.update(_live_status())
    emit(payload)


def _live_status() -> dict:
    """What the running player is doing and what it has queued; never starts one."""
    from ticli import ipc

    conn = ipc.connect()
    if conn is None:
        return {}
    try:
        reply = conn.request("status", caller="agent", key=_key(), timeout=5) or {}
    finally:
        conn.close()
    if not reply.get("ok"):
        return {}
    result = reply.get("result") or {}
    return {"state": reply.get("state"), "pending": result.get("pending", []),
            "done": result.get("done", []), "next": reply.get("next", [])}


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


# ---------------------------------------------------------------------------
# Verbs over the player socket (ADR-0008): caller=agent, the player queues and paces


def _key():
    import click

    ctx = click.get_current_context(silent=True)
    return ((ctx.obj if ctx else None) or {}).get("key")


_ZERO_COST = {"requests": 0, "wait_s": 0, "eta_s": 0}


def _disk_reply(name: str, args: dict, cfg: dict) -> dict:
    from ticli.agentq import compact_state, error_reply
    from ticli.commands import offline_read

    read = offline_read(name, args, cfg)
    if not read["ok"]:
        return error_reply(read)
    state = compact_state(offline_read("status", {}, cfg).get("result") or {})
    return {"ok": True, "result": read["result"], "state": state, "next": [],
            "cost": _ZERO_COST}


def _start_error(status: str) -> dict:
    from ticli import ipc

    if status == "login":
        return {"ok": False, "code": "not_logged_in", "reason": "No usable stored TIDAL session.",
                "fix": "Ask your human to run `ticli` in a terminal and log in."}
    return {"ok": False, "code": "player_unavailable", "reason": status,
            "fix": f"Tell your human; the player's log is {ipc.log_path()}."}


def call(cmd: str, args: dict) -> dict:
    """One agent command: refused, answered from disk, or sent to the player
    (started if needed). Never starts the player while AI control is off."""
    from ticli import ipc
    from ticli.agentq import error_reply
    from ticli.commands import AGENT, COMMANDS, gate
    from ticli.utils.config import load_config

    cfg, key = load_config(), _key()
    items = args.get("commands") if cmd == "agent.do" else None
    spec = COMMANDS.get(cmd)
    if spec is None and items is None:
        return {"ok": False, "code": "unknown_command", "reason": f"No command named {cmd!r}.",
                "fix": "Run `ticli agent docs` for the command list."}
    refused = gate(cmd, AGENT, key, cfg, read=bool(spec and spec.read) or items is not None)
    if refused:
        return error_reply(refused)
    if not cfg.get("allow_ai_control", True):
        return _disk_batch(items, cfg) if items is not None else _disk_reply(cmd, args, cfg)
    conn, status = ipc.connect_current()
    if conn is None:
        return _start_error(status)
    try:
        response = conn.request(cmd, args, caller="agent", key=key)
    finally:
        conn.close()
    if response is None:
        return {"ok": False, "code": "player_gone", "reason": "The player closed the connection.",
                "fix": "Run `ticli agent status`; queued actions may still have run."}
    response.pop("id", None)
    return ipc.stale_reply(response, "ticli agent restart") if conn.stale else response


def _disk_batch(items, cfg) -> dict:
    from ticli.commands import COMMANDS

    records, ok = [], True
    for item in items or []:
        cmd = item.get("cmd") if isinstance(item, dict) else None
        spec = COMMANDS.get(cmd)
        if not ok:
            records.append({"cmd": cmd, "ok": False, "code": "skipped"})
            continue
        if spec is None or not spec.read:
            reply = {"ok": False, "code": "ai_control_off",
                     "reason": "AI control is off; only reads from disk are allowed.",
                     "fix": 'Ask your human to turn on "Allow AI control". Only your human can '
                            "change this, in ticli's TUI settings; never edit config.json."}
        else:
            reply = _disk_reply(cmd, item.get("args") or {}, cfg)
        ok = ok and reply["ok"]
        records.append({"cmd": cmd, **{k: reply[k] for k in reply if k not in
                                          ("state", "next", "cost")}})
    return {"ok": ok, "result": records, "next": [], "cost": _ZERO_COST}


def finish(payload: dict) -> None:
    emit(payload)
    if not payload.get("ok"):
        raise SystemExit(1)


def parse_value(token: str):
    try:
        return json.loads(token)
    except ValueError:
        return token


def form_args(name: str, tokens) -> dict:
    """Positional tokens fill the command's params in order (a trailing `x*` takes the
    rest as a list); `key=value` sets one by name, `--flag` sets it true; a lone JSON
    object is the args."""
    from ticli.commands import COMMANDS

    tokens = list(tokens)
    if len(tokens) == 1 and tokens[0].lstrip().startswith("{"):
        args = json.loads(tokens[0])
        if not isinstance(args, dict):
            raise ValueError("args must be a JSON object")
        return args
    params = list(COMMANDS[name].params)
    args: dict = {}
    for token in tokens:
        if token.startswith("--") and token[2:].isidentifier():
            args[token[2:]] = True
            continue
        field, sep, value = token.partition("=")
        if sep and field.isidentifier():
            args[field] = parse_value(value)
            continue
        if not params:
            raise ValueError(f"{name} takes no more positional arguments: {token!r}")
        if params[0].endswith("*"):
            args.setdefault(params[0][:-1], []).append(parse_value(token))
        else:
            args[params.pop(0)] = parse_value(token)
    return args


def split_form(words) -> tuple:
    """`["playlist", "add", "X", "1"]` -> ("playlist.add", ["X", "1"])."""
    from ticli.commands import COMMANDS

    words = list(words)
    if not words:
        raise ValueError("empty command")
    if len(words) >= 2 and f"{words[0]}.{words[1]}" in COMMANDS:
        return f"{words[0]}.{words[1]}", words[2:]
    head = words[0].replace(" ", ".")
    if head in COMMANDS:
        return head, words[1:]
    raise ValueError(f"no command {' '.join(words[:2])!r}")


def _needs_names(cmd: str, args: dict) -> bool:
    from ticli import humancli

    kind = humancli.NAMED.get(cmd)
    if kind and "id" in args and not humancli.ID_FORM[kind].fullmatch(str(args["id"]).strip()):
        return True
    if cmd == "queue.add" and any(k in args and not humancli.ID_FORM[k].fullmatch(str(args[k]).strip())
                                  for k in ("album", "playlist")):
        return True
    if cmd in humancli.QUEUE_ENTRY:
        return False
    given = args.get("track_ids", args.get("track_id"))
    tokens = given if isinstance(given, list) else ([] if given is None else [given])
    return any(not str(t).strip().isdigit() for t in tokens)


def with_ids(cmd: str, args: dict) -> tuple:
    """(cmd, args, None) with names and songs turned into ids as `ticli <verb>` does,
    or (cmd, args, refusal): ambiguous names come back as `candidates` with ids."""
    if not _needs_names(cmd, args):
        return cmd, args, None
    from ticli import humancli

    link = humancli.Link(humancli.AGENT, picks=False)
    try:
        cmd, args = humancli.prepare(link, cmd, args)
    except humancli.Stop as stop:
        return cmd, args, stop.reply
    finally:
        link.close()
    return cmd, args, None


def _call_named(cmd: str, args: dict) -> dict:
    cmd, args, refused = with_ids(cmd, args)
    return refused if refused is not None else call(cmd, args)


def run_form(name: str, tokens) -> None:
    try:
        args = form_args(name, tokens)
    except ValueError as e:
        raise fail_reply("bad_args", str(e), f"See `ticli agent {name.replace('.', ' ')} --help`.")
    finish(_call_named(name, args))


def fail_reply(code: str, reason: str, fix: str) -> SystemExit:
    emit({"ok": False, "code": code, "reason": reason, "fix": fix})
    return SystemExit(1)


def do(text: str) -> None:
    """A JSON array of {"cmd", "args"} objects or "verb args..." strings, in order."""
    import shlex

    try:
        items = json.loads(text)
        if not isinstance(items, list) or not items:
            raise ValueError("do takes a non-empty JSON array")
        commands = []
        for item in items:
            if isinstance(item, str):
                name, tokens = split_form(shlex.split(item))
                commands.append({"cmd": name, "args": form_args(name, tokens)})
            elif isinstance(item, dict) and isinstance(item.get("cmd"), str):
                name = item["cmd"].replace(" ", ".")
                commands.append({"cmd": name, "args": item.get("args") or {}})
            else:
                raise ValueError(f"not a command: {item!r}")
    except ValueError as e:
        raise fail_reply("bad_args", str(e),
                         'Pass a JSON array like ["pause", {"cmd": "playlist.add", '
                         '"args": {"id": "...", "track_ids": [1]}}].')
    finish(call("agent.do", {"commands": commands}))


# ---------------------------------------------------------------------------
# The original verbs: same top-level keys as before the player existed


def _legacy(payload: dict, top) -> None:
    if not payload.get("ok"):
        payload = {**payload, "error": payload.get("code"), "message": payload.get("reason"),
                   "hint": payload.get("fix") or "Report this to your human if it persists."}
        finish(payload)
        return
    out = {"ok": True, **top(payload.get("result") or {})}
    out.update({k: payload[k] for k in ("state", "next", "cost") if k in payload})
    finish(out)


def search(query: str, types: tuple, limit: int, offset: int = 0) -> None:
    kinds = [f"{t}s" for t in (types or ("track",))]
    _legacy(call("search", {"query": query, "types": kinds, "limit": limit, "offset": offset}),
            lambda r: {"query": query, "offset": offset, **{k: r.get(k, []) for k in kinds},
                       **({"source": r["source"]} if "source" in r else {})})


def _flat(candidate):
    if not candidate:
        return candidate
    rest = {k: v for k, v in candidate.items() if k != "track"}
    return {**(candidate.get("track") or {}), **rest}


def resolve(artist: str, title: str, limit: int) -> None:
    _legacy(call("resolve", {"artist": artist, "title": title, "limit": limit}),
            lambda r: {"artist": artist, "title": title, "confident": r.get("confident", False),
                       "best": _flat(r.get("best")),
                       "candidates": [_flat(c) for c in r.get("candidates") or []]})


def playlist_list() -> None:
    _legacy(call("library.playlists", {}), lambda r: r)


def playlist_show(playlist_id: str) -> None:
    _legacy(_call_named("playlist.tracks", {"id": playlist_id}),
            lambda r: {"playlist": r.get("playlist"), "tracks": r.get("tracks", [])})


def playlist_create(name: str, description: str, track_ids: tuple = ()) -> None:
    args = {"name": name, "description": description}
    if track_ids:
        args["track_ids"] = [str(t) for t in track_ids]
    cmd, args, refused = with_ids("playlist.create", args)
    _legacy(refused if refused is not None else call(cmd, args),
            lambda r: {"playlist": r.get("playlist"),
                       **({"added": r["added"]} if "added" in r else {})})


def playlist_add(playlist_id: str, track_ids: tuple) -> None:
    """Queued: answered at once; `added` is gone because it is not known yet."""
    cmd, args, refused = with_ids("playlist.add", {"id": playlist_id,
                                                   "track_ids": [str(t) for t in track_ids]})
    _legacy(refused if refused is not None else call(cmd, args),
            lambda r: {"playlist_id": str(args["id"]), "requested": len(args["track_ids"]), **r})


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


def restart() -> None:
    """A fresh player on the current code, resuming where the old one was. Refused
    while a download, re-fetch or queued agent work runs."""
    from ticli import ipc
    from ticli.agentq import error_reply
    from ticli.commands import AGENT, gate
    from ticli.utils.config import load_config

    refused = gate("restart", AGENT, _key(), load_config())
    if refused:
        finish(error_reply(refused))
    reply = ipc.restart()
    finish(reply if reply["ok"] else error_reply(reply))
