"""`ticli <verb>`: the human CLI, a client of the background player (ADR-0008).

Every verb is one command from `ticli.commands`, sent over the socket. Caller
is decided by `commands.cli_caller()` (ADR-0007): a terminal on stdin is the
human, anything else is an agent that the switches and the key gate, through
the same paced path as `ticli agent`. Output is one readable line per result.

Arguments take names and `<song>` forms (id, TIDAL URL, "artist - title",
`current`); they are resolved here, before the command is sent. Must not
import `ticli.player` at module level: `ticli --help` stays instant.
"""

import json
import re

import click

from ticli.utils import throttle

HUMAN, AGENT = "human", "agent"
TOP = 5
TRANSPORT = {"pause": "paused", "resume": "resumed", "next": "next track", "prev": "previous track"}
DONE = {**TRANSPORT, "like": "liked", "unlike": "unliked", "stop": "stopped",
        "playlist.create": "playlist created", "playlist.add": "added to the playlist",
        "playlist.remove": "removed from the playlist", "download": "downloading",
        "download.cancel": "download cancelled", "download.delete": "download deleted",
        "queue.remove": "removed from the queue", "queue.play": "playing from the queue",
        "seek": "seeked", "logout": "logged out", "cache.clear": "cache cleared"}
# Commands whose `id` argument is a name for this kind of thing.
NAMED = {"playlist.add": "playlist", "playlist.remove": "playlist", "playlist.tracks": "playlist",
         "play.playlist": "playlist", "play.album": "album", "album.tracks": "album",
         "play.artist": "artist", "artist.section": "artist"}
ID_FORM = {"playlist": re.compile(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"),
           "album": re.compile(r"\d{3,}"), "artist": re.compile(r"\d{3,}")}
PICK = re.compile(r"[1-9]\d?")
URL = re.compile(r"(?:^|/)(track|album|playlist)/([0-9A-Za-z-]+)")
CURRENT_BY_DEFAULT = ("like", "unlike", "download")
SONG_FORMS = 'a track id, a TIDAL URL, "artist - title" or current'


def caller() -> str:
    from ticli import commands
    return HUMAN if commands.cli_caller() == commands.HUMAN else AGENT


class Stop(Exception):
    def __init__(self, reply: dict):
        super().__init__(reply.get("reason"))
        self.reply = reply


def refusal(code: str, reason: str, fix: str = "", **extra) -> dict:
    return {"ok": False, "code": code, "reason": reason, **({"fix": fix} if fix else {}), **extra}


class Link:
    """One connection to the player, kept for a whole verb so it cannot leave between
    the lookup and the action. Agents go through `ticli.agent.call` instead."""

    def __init__(self, who: str):
        self.who = who
        self.conn = None

    def running(self) -> bool:
        from ticli import ipc
        if self.conn is not None:
            return True
        probe = ipc.connect()
        if probe is None:
            return False
        probe.close()
        return True

    def ask(self, cmd: str, args=None) -> dict:
        args = args or {}
        if self.who == AGENT:
            from ticli import agent
            return agent.call(cmd, args)
        from ticli import agent, ipc
        from ticli.agentq import render
        if self.conn is None:
            self.conn, status = ipc.connect_or_start()
            if self.conn is None:
                return agent._start_error(status)
        response = self.conn.request(cmd, args, caller="human", timeout=60)
        if response is None:
            self.conn = None
            return refusal("player_gone", "The player closed the connection.",
                           "Run `ticli status`; the action may still have run.")
        response.pop("id", None)
        if response.get("ok"):
            response["result"] = render(response.get("result"))
        return response

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None


# ── presenting ──


def _mmss(seconds) -> str:
    seconds = int(seconds or 0)
    return f"{seconds // 60}:{seconds % 60:02d}"


def _who(row: dict) -> str:
    artists = row.get("artists") or ([row["artist"]] if row.get("artist") else [])
    artists = [a.get("name") if isinstance(a, dict) else a for a in artists]
    return ", ".join(a for a in artists if a)


def _song(row: dict) -> str:
    row = row.get("track") or row
    text = f'{row.get("title") or row.get("name")}'
    return f"{_who(row)} - {text}" if _who(row) else text


def _named(row: dict) -> str:
    detail = f' ({row["num_tracks"]} tracks)' if row.get("num_tracks") else ""
    return f'{row.get("name") or _song(row)}{detail}'


def _lines(rows, fmt) -> str:
    return "\n".join(f'{row.get("id")}  {fmt(row)}' for row in rows) or "nothing found"


def status_line(reply: dict) -> str:
    from ticli.agentq import compact_state
    state = reply.get("state") or compact_state(reply.get("result") or {})
    track = state.get("track")
    if not track:
        return "nothing playing"
    queue = state.get("queue") or {}
    where = f' (queue {queue.get("index", 0) + 1}/{queue["len"]})' if queue.get("len") else ""
    pending = f' | {state["pending"]} agent commands pending' if state.get("pending") else ""
    return (f'{"playing" if state.get("playing") else "paused"}: {track["artist"]} - {track["title"]} '
            f'[{_mmss(track["pos"])}/{_mmss(track["dur"])}]{where}{pending}')


def _search_lines(r: dict) -> str:
    out = []
    for kind, fmt in (("tracks", _song), ("albums", lambda a: f'{a.get("title")} - {_who(a)}'),
                      ("artists", _named), ("playlists", _named)):
        if r.get(kind):
            out.append(f"{kind}:\n" + _lines(r[kind], fmt))
    return "\n".join(out) or "nothing found"


def _resolve_line(r: dict) -> str:
    best = r.get("best")
    if not best:
        return "no match"
    row = best.get("track") or best
    return f'{"match" if r.get("confident") else "closest, not confident"}: {row.get("id")}  {_song(row)}'


def _queued(reply: dict) -> str:
    result, cost = reply.get("result") or {}, reply.get("cost") or {}
    text = f'queued #{result["queued"]}, done in ~{cost.get("eta_s", 0):g} s'
    return text + (f' ({result["merged"]})' if result.get("merged") else "")


def line(cmd: str, reply: dict) -> str:
    result = reply.get("result")
    if isinstance(result, dict) and "queued" in result:
        return _queued(reply)
    if cmd == "status":
        return status_line(reply)
    r = result if isinstance(result, dict) else {}
    if cmd == "search":
        return _search_lines(r)
    if cmd == "resolve":
        return _resolve_line(r)
    if cmd == "library.playlists":
        return _lines(r.get("playlists", []), _named)
    if cmd in ("playlist.tracks", "album.tracks"):
        head = f'{_named(r["playlist"])}\n' if r.get("playlist") else ""
        return head + _lines(r.get("tracks", []), _song)
    if cmd == "queue.list":
        return "\n".join(f'{"> " if i == r.get("index") else "  "}{i}  {_song(t)}'
                         for i, t in enumerate(r.get("tracks", []))) or "the queue is empty"
    if cmd == "download.list":
        return _lines(r.get("downloads", []), lambda d: f'{d.get("artist")} - {d.get("title")}') \
            if r.get("downloads") else "no downloads"
    if cmd.startswith("play.") and "queue_length" in r:
        return f'playing: queue of {r["queue_length"]}'
    if cmd == "playlist.create" and isinstance(r.get("playlist"), dict):
        return f'created {r["playlist"].get("id")}  {r["playlist"].get("name")}'
    if cmd in DONE:
        return DONE[cmd]
    return json.dumps(result, separators=(",", ":")) if result else "done"


def fail(reply: dict) -> "SystemExit":
    click.echo(f'ticli: {reply.get("reason")} ({reply.get("code")})', err=True)
    if reply.get("fix"):
        click.echo(f'  {reply["fix"]}', err=True)
    return SystemExit(1)


def say(who: str, cmd: str, reply: dict) -> None:
    if not reply.get("ok"):
        if who == AGENT and reply.get("candidates"):
            from ticli import agent
            agent.emit(reply)
            raise SystemExit(1)
        raise fail(reply)
    click.echo(line(cmd, reply))


# ── permissions ──


def guard(cmd: str, who: str, read: bool = False) -> None:
    """A non-terminal caller is an agent: the switches and the key apply."""
    if who != AGENT:
        return
    from ticli import agent
    from ticli.commands import AGENT as AGENT_CALLER
    from ticli.commands import gate
    from ticli.utils.config import load_config
    refused = gate(cmd, AGENT_CALLER, agent._key(), load_config(), read=read)
    if refused:
        raise Stop(refused)


def confirmed(cmd: str, args: dict) -> bool:
    """Dangerous verbs ask the human, default no (ADR-0007)."""
    from types import SimpleNamespace

    from ticli.commands import COMMANDS
    from ticli.utils.config import load_config
    spec = COMMANDS[cmd]
    try:
        dangerous = spec.is_dangerous(SimpleNamespace(config=load_config()), args)
    except Exception:
        dangerous = True
    if not dangerous:
        return True
    shown = " ".join(str(v) for v in args.values())
    return click.confirm(f'"{cmd.replace(".", " ")} {shown}" is dangerous. Continue?', default=False)


# ── names ──


def _picks_file():
    return throttle.STATE_DIR / "picks.json"


def _save_picks(kind: str, rows: list) -> None:
    try:
        throttle.STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        _picks_file().write_text(json.dumps({"kind": kind, "rows": rows}))
    except OSError:
        pass


def _take_pick(kind: str, text: str):
    if not PICK.fullmatch(text):
        return None
    try:
        saved = json.loads(_picks_file().read_text())
    except (OSError, ValueError):
        return None
    rows = saved.get("rows") or []
    if saved.get("kind") != kind or not 1 <= int(text) <= len(rows):
        return None
    return rows[int(text) - 1]


def _row(kind: str, obj: dict) -> dict:
    name = obj.get("name") or f'{obj.get("title")} - {_who(obj)}'
    return {"id": str(obj.get("id")), "name": name, **(
        {"num_tracks": obj["num_tracks"]} if obj.get("num_tracks") else {})}


def _ambiguous(kind: str, text: str, rows: list, example: str) -> Stop:
    rows = rows[:TOP]
    _save_picks(kind, rows)
    who = caller()
    if who == AGENT:
        return Stop(refusal("ambiguous", f'"{text}" matches several {kind}s.',
                            "Pick one and run the command again with its id instead of the name.",
                            candidates=rows))
    shown = "\n".join(f'  {i}. {_named(r)}' for i, r in enumerate(rows, 1))
    return Stop(refusal("ambiguous", f'"{text}" matches several {kind}s:\n{shown}',
                        f"Run `{example}` to pick (the number, or the id)."))


def _local_playlists(who: str) -> list:
    guard("library.playlists", who, read=True)
    from ticli.utils.cache import MetadataCache
    return [{"id": str(p.id), "name": p.name, "num_tracks": p.num_tracks}
            for p in MetadataCache().get_playlists() or []]


def resolve_name(link: Link, kind: str, token, example: str) -> tuple:
    """(id, label). Own playlists in the local index first (0 requests); else one
    TIDAL search. Several equally good matches stop with a numbered top 5."""
    text = str(token).strip()
    picked = _take_pick(kind, text)
    if picked:
        return picked["id"], picked["name"]
    if ID_FORM[kind].fullmatch(text):
        return text, text
    wanted = text.casefold()
    if kind == "playlist":
        own = _local_playlists(link.who)
        exact = [p for p in own if p["name"].casefold() == wanted]
        loose = exact or [p for p in own if wanted in p["name"].casefold()]
        if len(loose) == 1:
            return loose[0]["id"], loose[0]["name"]
        if loose:
            raise _ambiguous(kind, text, loose, example)
    reply = link.ask("search", {"query": text, "types": [kind], "limit": TOP})
    if not reply.get("ok"):
        raise Stop(reply)
    rows = [_row(kind, o) for o in (reply["result"] or {}).get(kind + "s", [])]
    exact = [r for r in rows if r["name"].casefold() == wanted]
    if len(exact) == 1 or len(rows) == 1:
        found = (exact or rows)[0]
        return found["id"], found["name"]
    if not rows:
        raise Stop(refusal("not_found", f'No {kind} matches "{text}".',
                           f"Try `ticli search {text}` or pass an id."))
    raise _ambiguous(kind, text, exact or rows, example)


# ── songs ──


def _current(link: Link) -> str:
    if not link.running():
        raise Stop(refusal("no_track", "Nothing is playing.", "Pass a track id or another form."))
    reply = link.ask("status")
    track = ((reply.get("state") or {}).get("track")
             or ((reply.get("result") or {}).get("track")))
    if not reply.get("ok") or not track:
        raise Stop(reply if not reply.get("ok") else refusal(
            "no_track", "Nothing is playing.", "Pass a track id or another form."))
    return str(track["id"])


def _song_candidates(r: dict, token: str) -> Stop:
    rows = [{"id": str((c.get("track") or c).get("id")), "name": _song(c)}
            for c in r.get("candidates") or []][:TOP]
    if not rows:
        return Stop(refusal("not_found", f'No song matches "{token}".', "Try `ticli search`."))
    shown = "\n".join(f'  {r["id"]}  {r["name"]}' for r in rows)
    return Stop(refusal("not_confident", f'No confident match for "{token}":\n{shown}',
                        "Run the command again with the id of the one you want.", candidates=rows))


def resolve_song(link: Link, token) -> list:
    """Track ids for one `<song>` argument; an album or playlist URL is all its tracks."""
    text = str(token).strip()
    if text.isdigit():
        return [text]
    if text.lower() == "current":
        return [_current(link)]
    if "tidal.com" in text:
        found = URL.findall(text)
        if not found:
            raise Stop(refusal("bad_args", f"Not a TIDAL track, album or playlist URL: {text}",
                               f"Use {SONG_FORMS}."))
        kind, ident = found[-1]
        if kind == "track":
            return [ident]
        reply = link.ask(f"{kind}.tracks", {"id": ident})
        if not reply.get("ok"):
            raise Stop(reply)
        return [str(t["id"]) for t in reply["result"].get("tracks", [])]
    artist, sep, title = text.partition(" - ")
    if not sep or not artist.strip() or not title.strip():
        raise Stop(refusal("bad_args", f"Not a song: {text!r}.", f"Use {SONG_FORMS}."))
    reply = link.ask("resolve", {"artist": artist.strip(), "title": title.strip(), "limit": 10})
    if not reply.get("ok"):
        raise Stop(reply)
    result = reply["result"]
    best = result.get("best")
    if result.get("confident") and best:
        return [str((best.get("track") or best)["id"])]
    raise _song_candidates(result, text)


# ── running a verb ──


def _queue_guard(link: Link, args: dict) -> dict:
    """Pin the entry by its track so a queue another client moved is `stale`, not another track."""
    reply = link.ask("queue.list")
    tracks = ((reply.get("result") or {}).get("tracks") or []) if reply.get("ok") else []
    index = args.get("index")
    if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(tracks):
        return {"track_id": tracks[index].get("id")}
    return {}


def prepare(link: Link, cmd: str, args: dict) -> tuple:
    """(command, args) with names and songs turned into ids."""
    from ticli.commands import COMMANDS
    args = dict(args)
    example = f'ticli {cmd.replace(".", " ")} 2'
    kind = NAMED.get(cmd)
    if kind and "id" in args:
        args["id"], _label = resolve_name(link, kind, args["id"], example)
    if cmd in ("queue.play", "queue.remove") and "track_id" not in args:
        args = {**args, **_queue_guard(link, args)}
    params = COMMANDS[cmd].params
    for field in ("track_id", "track_ids"):
        if cmd.startswith("queue."):
            break
        if field not in params and field + "*" not in params:
            continue
        given = args.pop(field, None)
        tokens = given if isinstance(given, list) else ([] if given is None else [given])
        if not tokens and cmd in CURRENT_BY_DEFAULT:
            tokens = ["current"]
        if cmd == "play.track" and tokens:
            url = URL.findall(str(tokens[0])) if "tidal.com" in str(tokens[0]) else []
            if url and url[-1][0] != "track":
                return f"play.{url[-1][0]}", {"id": url[-1][1]}
        ids = [i for t in tokens for i in resolve_song(link, t)]
        if ids:
            args["track_id" if field == "track_id" else "track_ids"] = ids[0] if field == "track_id" else ids
    return cmd, args


def run(cmd: str, args: dict, then=None, label=None) -> None:
    who = caller()
    link = Link(who)
    try:
        try:
            if who == AGENT:
                guard(cmd, who)
            cmd, args = prepare(link, cmd, args)
            if who == HUMAN and not confirmed(cmd, args):
                click.echo("cancelled", err=True)
                raise SystemExit(1)
            reply = link.ask(cmd, args)
        except Stop as stop:
            reply = stop.reply
    finally:
        link.close()
    say(who, cmd, reply)
    if then and who == HUMAN:
        then()


def verb(dotted: str, tokens) -> None:
    from ticli import agent
    try:
        args = agent.form_args(dotted, tokens)
    except ValueError as e:
        raise fail(refusal("bad_args", str(e), f"See `ticli {dotted.replace('.', ' ')} --help`."))
    run(dotted, args)


def transport(cmd: str) -> None:
    """pause/resume/next/prev/status: never start the player just to answer."""
    from ticli.utils.config import load_config
    who = caller()
    try:
        guard(cmd, who, read=cmd == "status")
    except Stop as stop:
        raise fail(stop.reply)
    link = Link(who)
    from_disk = cmd == "status" and who == AGENT and not load_config().get("allow_ai_control", True)
    if not link.running() and not from_disk:
        click.echo("nothing playing")
        raise SystemExit(0 if cmd == "status" else 1)
    say(who, cmd, link.ask(cmd))


def search(words, types, limit) -> None:
    kinds = [f"{t}s" for t in types] if types else None
    run("search", {"query": " ".join(words), **({"types": kinds} if kinds else {}),
                   "limit": limit})


def start(kind: str, name: str, no_tui: bool) -> None:
    """Play a playlist, album or artist by name; then show the TUI unless --no-tui."""
    who = caller()
    link = Link(who)
    try:
        try:
            if who == AGENT:
                guard(f"play.{kind}", who)
            ident, label = resolve_name(link, kind, name, f"ticli start {kind} 2")
            reply = link.ask(f"play.{kind}", {"id": ident})
        except Stop as stop:
            reply = stop.reply
            label = name
    finally:
        link.close()
    if reply.get("ok") and not (reply.get("result") or {}).get("queued"):
        click.echo(f'playing {kind} "{label}": {(reply["result"] or {}).get("queue_length", "?")} tracks')
    else:
        say(who, f"play.{kind}", reply)
    if who == HUMAN and not no_tui and reply.get("ok"):
        root = click.get_current_context().find_root().params
        from ticli.player import run_tui
        run_tui(quality=root.get("quality"), login_flow=root.get("login_flow"))
