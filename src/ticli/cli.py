"""Ticli - Terminal music player for TIDAL."""

import click

# The tier names are TIDAL's own player's, ascending. Kept in step with
# utils.config.QUALITY_CHOICES by a test rather than an import, so `ticli
# --help` stays instant — pulling in config here would be the whole player's
# import chain for a help screen.
QUALITY_NAMES = ["LOW", "MEDIUM", "HIGH", "MAX"]

# The pre-rename spellings that are unambiguous keep working. "HIGH" is not
# here because it exists in both schemes with different meanings (it used to
# be 320k AAC, it is now 16-bit FLAC) — it takes the new meaning, and the
# note printed below is what keeps that honest for old scripts.
QUALITY_ALIASES = {"LOSSLESS": "HIGH", "HIRES": "MAX"}


class QualityChoice(click.Choice):
    """The four tier names, still accepting the pre-rename spellings.

    A saved config is disambiguated by its version number; a flag has no
    version, so this is where the old vocabulary is translated. Aliases are
    corrected out loud on stderr, and `HIGH` — the one spelling whose meaning
    changed — says what it means now every time, because the failure mode of
    a script that meant 320k AAC is a silent 4x data increase.
    """

    def convert(self, value, param, ctx):
        name = str(value).upper()
        if name in QUALITY_ALIASES:
            new = QUALITY_ALIASES[name]
            click.echo(f"note: --quality {name} is now --quality {new}", err=True)
            name = new
        elif name == "HIGH":
            click.echo(
                "note: HIGH now means 16-bit/44.1 kHz FLAC; "
                "320 kbps AAC is --quality MEDIUM", err=True)
        return super().convert(name, param, ctx)


# Registry commands the original agent verbs already cover, under their old names.
COVERED = {"status", "search", "resolve", "library.playlists", "playlist.tracks",
           "playlist.create", "playlist.add"}
# Human verbs written by hand below; the registry supplies the rest.
HUMAN_COVERED = {"status", "search", "pause", "resume", "next", "prev"}
HUMAN_HIDDEN = {"login.reload", "history.add", "history.forget"}
HUMAN_ALIASES = {"playlist.list": "library.playlists", "playlist.show": "playlist.tracks"}


def _cost_line(spec) -> str:
    if not spec.tidal:
        return "Local: 0 requests, runs at once."
    if spec.read:
        return "Reads TIDAL: waits its turn in the 2 s queue, then answers with the data."
    return "TIDAL action: queued 2 s apart; answered at once with queue position and ETA."


class RegistryGroup(click.Group):
    """One generated verb per player command, so a new command in `ticli.commands`
    appears with no edit. The registry is imported when a verb is looked up or
    listed (16 ms), never the player."""

    skip: frozenset = frozenset()
    aliases: dict = {}

    def _registry(self) -> dict:
        from ticli.commands import COMMANDS
        return {n: c for n, c in COMMANDS.items() if n not in self.skip}

    def list_commands(self, ctx):
        words = [n.replace(".", " ") for n in {**self._registry(), **self.aliases}]
        return sorted(self.commands) + sorted(words)

    def help_for(self, dotted, spec) -> str:
        raise NotImplementedError

    def short_for(self, spec) -> str:
        return _cost_line(spec)

    def run(self, dotted, tokens) -> None:
        raise NotImplementedError

    def get_command(self, ctx, name):
        if name in self.commands:
            return self.commands[name]
        dotted = name.replace(" ", ".")
        target = self.aliases.get(dotted, dotted)
        spec = self._registry().get(target)
        if spec is None:
            return None

        def callback(tokens):
            self.run(target, tokens)

        return click.Command(
            name.replace(".", " "), callback=callback,
            params=[click.Argument(["tokens"], nargs=-1, type=click.UNPROCESSED)],
            context_settings={"ignore_unknown_options": True},
            help=self.help_for(target, spec), short_help=self.short_for(spec))

    def resolve_command(self, ctx, args):
        if len(args) >= 2 and not args[0].startswith("-"):
            dotted = f"{args[0]}.{args[1]}"
            if dotted in self.aliases or dotted in self._registry():
                return dotted.replace(".", " "), self.get_command(ctx, dotted), args[2:]
        return super().resolve_command(ctx, args)


def _shape(spec) -> str:
    return " ".join(f"[{p}...]" if p.endswith("*") else f"[{p}]" for p in spec.params) or "(none)"


class AgentGroup(RegistryGroup):
    skip = frozenset(COVERED)

    def help_for(self, dotted, spec) -> str:
        return (f"`{dotted}`. {_cost_line(spec)}"
                + (" DANGEROUS: needs that switch on." if spec.dangerous is True else "")
                + f"\n\nArgs {_shape(spec)}: positional, key=value, or one JSON object.")

    def run(self, dotted, tokens) -> None:
        from ticli import agent as impl
        impl.run_form(dotted, tokens)


class HumanGroup(RegistryGroup):
    skip = frozenset(HUMAN_COVERED | HUMAN_HIDDEN)
    aliases = HUMAN_ALIASES

    def short_for(self, spec) -> str:
        return ("Local." if not spec.tidal else "Reads TIDAL." if spec.read else "TIDAL action.") \
            + (" Asks y/N." if spec.dangerous else "")

    def help_for(self, dotted, spec) -> str:
        return (f"`{dotted}`. {self.short_for(spec)}"
                + f"\n\nArgs {_shape(spec)}. Playlists, albums and artists take a name or id; "
                  "songs take an id, a TIDAL URL, \"artist - title\" or current.")

    def run(self, dotted, tokens) -> None:
        from ticli import humancli
        humancli.verb(dotted, tokens)


@click.group(cls=HumanGroup, invoke_without_command=True)
@click.option("--key", envvar="TICLI_AI_KEY", default=None, help="The AI control key, when a script (not a terminal) runs a verb. Or TICLI_AI_KEY.")
@click.option("--quality", default=None, type=QualityChoice(QUALITY_NAMES, case_sensitive=False), help="Audio quality for this run (overrides the saved setting)")
@click.option("--login-flow", default=None, type=click.Choice(["device", "pkce"], case_sensitive=False), help="How to log in when there is no saved session. device (default) is quickest; pkce needs a paste but is the only flow TIDAL streams FLAC to. Settings can switch later.")
@click.pass_context
def cli(ctx, key, quality, login_flow):
    """Ticli - Terminal music player for TIDAL. Plain `ticli` runs the player.

    \b
    Verbs: `ticli pause|resume|next|prev|status`, `ticli start playlist
    NAME`, `ticli playlist add NAME SONG`, `ticli like`... A SONG is a track
    id, a TIDAL URL, "artist - title" or current.

    \b
    AI or script? Run `ticli agent docs` — the complete programmatic
    contract: every verb, JSON shapes, rate rules, and workflows.
    """
    # A group with invoke_without_command rather than a command, so `ticli
    # agent` can hang off it; bare `ticli` still means the player, and the
    # player's import chain still stays out of `--help`.
    ctx.obj = {"key": key}
    if ctx.invoked_subcommand is not None:
        return
    from ticli.player import run_tui
    run_tui(quality=quality, login_flow=login_flow)



def _transport(name, doc):
    def callback():
        from ticli import humancli
        humancli.transport(name)
    callback.__doc__ = doc
    return cli.command(name)(callback)


for _name, _doc in (("pause", "Pause playback. One line, then exit; never starts the player."),
                    ("resume", "Resume playback."), ("next", "Skip to the next track."),
                    ("prev", "Back to the previous track."),
                    ("status", "What is playing, in one line.")):
    _transport(_name, _doc)


@cli.command()
@click.argument("kind", type=click.Choice(["playlist", "album", "artist"]))
@click.argument("name", nargs=-1, required=True)
@click.option("--no-tui", is_flag=True, help="Only start playback; don't open the TUI.")
def start(kind, name, no_tui):
    """Play a playlist, album or artist by name, then open the TUI here.

    Your own playlists match case-insensitively with no TIDAL request; anything
    else is one TIDAL search. Several matches print a numbered top 5: run
    `ticli start playlist 2` to pick."""
    from ticli import humancli
    humancli.start(kind, " ".join(name), no_tui)


@cli.command("search")
@click.argument("query", nargs=-1, required=True)
@click.option("--type", "types", multiple=True, type=click.Choice(["track", "album", "artist", "playlist"]), help="Repeatable. Default: all four.")
@click.option("--limit", default=10, show_default=True)
def search_(query, types, limit):
    """Search TIDAL. One request however many --type."""
    from ticli import humancli
    humancli.search(query, types, limit)


@cli.group(cls=AgentGroup)
@click.option("--key", envvar="TICLI_AI_KEY", default=None, help="The AI control key, if your human set one (or TICLI_AI_KEY).")
@click.pass_context
def agent(ctx, key):
    """Headless verbs for programs. Start with `ticli agent docs`.

    stdout is always one JSON object (docs excepted). Every verb is
    rate-limited by a cross-process throttle; a TIDAL 429 or bot-detection
    response stops ALL agent requests until a human runs `ticli agent
    unblock`. Errors are structured: {"ok": false, "error": <code>,
    "message": ..., "hint": ...} with a nonzero exit. The human's TUI
    settings decide what agents may do; a refusal says what to ask them.
    """
    ctx.obj = {"key": key}


@agent.command()
def docs():
    """The complete contract, as markdown: every verb with its cost and JSON
    shape, the rate rules, and workflows. Read this before anything else."""
    from ticli.agent_docs import DOCS
    click.echo(DOCS, nl=False)


@agent.command()
@click.option("--verify", is_flag=True, help="Spend one request confirming the session works. Default is zero requests.")
def status(verify):
    """Session, entitlement, player and throttle state. Costs 0 requests (1 with --verify)."""
    from ticli import agent as impl
    impl.status(verify)


@agent.command()
@click.argument("query")
@click.option("--type", "types", multiple=True, type=click.Choice(["track", "album", "artist", "playlist"]), help="Repeatable. Default: track.")
@click.option("--limit", default=10, show_default=True, help="Results per type.")
def search(query, types, limit):
    """Search TIDAL. Costs 1 request regardless of how many --type."""
    from ticli import agent as impl
    impl.search(query, types, limit)


@agent.command()
@click.option("--artist", required=True)
@click.option("--title", required=True)
@click.option("--limit", default=10, show_default=True, help="Candidates to rank.")
def resolve(artist, title, limit):
    """Find THE track for an artist+title: ranked candidates, a best pick,
    and a strict `confident` flag. Costs 1 request."""
    from ticli import agent as impl
    impl.resolve(artist, title, limit)


@agent.group()
def playlist():
    """Your playlists: list, show, create, add."""


@playlist.command("list")
def playlist_list():
    """All of your playlists. Costs 1 request."""
    from ticli import agent as impl
    impl.playlist_list()


@playlist.command("show")
@click.argument("playlist_id")
def playlist_show(playlist_id):
    """A playlist and its tracks. Costs 2 requests."""
    from ticli import agent as impl
    impl.playlist_show(playlist_id)


@playlist.command("create")
@click.argument("name")
@click.option("--description", default="", help="Optional description.")
def playlist_create(name, description):
    """Create an empty playlist. Costs 1 request."""
    from ticli import agent as impl
    impl.playlist_create(name, description)


@playlist.command("add")
@click.argument("playlist_id")
@click.argument("track_ids", nargs=-1, required=True)
def playlist_add(playlist_id, track_ids):
    """Add tracks by id. One add is one request however many ids. Costs 2 requests."""
    from ticli import agent as impl
    impl.playlist_add(playlist_id, track_ids)


@agent.command("do")
@click.argument("commands", required=False)
def do_(commands):
    """Run a JSON array of commands in order, coalescing adds and likes, with one
    combined reply. Items are {"cmd": "playlist.add", "args": {...}} or verb
    strings like "playlist add <id> 1 2". Reads the array from stdin if omitted."""
    import sys

    from ticli import agent as impl
    impl.do(commands if commands is not None else sys.stdin.read())


@agent.command()
def unblock():
    """Clear a tripped rate-limit stop. For humans, after investigating —
    works only from a terminal; an agent must never run this."""
    from ticli import agent as impl
    impl.unblock()


def main():
    cli()


if __name__ == "__main__":
    main()
