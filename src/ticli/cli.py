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


@click.group(invoke_without_command=True)
@click.option("--quality", default=None, type=QualityChoice(QUALITY_NAMES, case_sensitive=False), help="Audio quality for this run (overrides the saved setting)")
@click.option("--login-flow", default=None, type=click.Choice(["device", "pkce"], case_sensitive=False), help="How to log in when there is no saved session. device (default) is quickest; pkce needs a paste but is the only flow TIDAL streams FLAC to. Settings can switch later.")
@click.pass_context
def cli(ctx, quality, login_flow):
    """Ticli - Terminal music player for TIDAL. Plain `ticli` runs the player.

    \b
    AI or script? Run `ticli agent docs` — the complete programmatic
    contract: every verb, JSON shapes, rate rules, and workflows.
    """
    # A group with invoke_without_command rather than a command, so `ticli
    # agent` can hang off it; bare `ticli` still means the player, and the
    # player's import chain still stays out of `--help`.
    if ctx.invoked_subcommand is not None:
        return
    from ticli.player import run_tui
    run_tui(quality=quality, login_flow=login_flow)


# Registry commands the original verbs above already cover, under their old names.
COVERED = {"status", "search", "resolve", "library.playlists", "playlist.tracks",
           "playlist.create", "playlist.add"}


def _cost_line(spec) -> str:
    if not spec.tidal:
        return "Local: 0 requests, runs at once."
    if spec.read:
        return "Reads TIDAL: waits its turn in the 2 s queue, then answers with the data."
    return "TIDAL action: queued 2 s apart; answered at once with queue position and ETA."


class AgentGroup(click.Group):
    """The original verbs plus one generated verb per player command, so a new
    command in `ticli.commands` appears here with no edit. The registry is
    imported only when a verb is looked up, never for `ticli --help`."""

    def _registry(self) -> dict:
        from ticli.commands import COMMANDS
        return {n: c for n, c in COMMANDS.items() if n not in COVERED}

    def list_commands(self, ctx):
        return sorted(self.commands) + sorted(n.replace(".", " ") for n in self._registry())

    def get_command(self, ctx, name):
        if name in self.commands:
            return self.commands[name]
        dotted = name.replace(" ", ".")
        spec = self._registry().get(dotted)
        if spec is None:
            return None
        shape = " ".join(f"[{p}...]" if p.endswith("*") else f"[{p}]" for p in spec.params)

        def callback(tokens):
            from ticli import agent as impl
            impl.run_form(dotted, tokens)

        return click.Command(
            name.replace(".", " "), callback=callback,
            params=[click.Argument(["tokens"], nargs=-1, type=click.UNPROCESSED)],
            context_settings={"ignore_unknown_options": True},
            help=(f"`{dotted}`. {_cost_line(spec)}" + (" DANGEROUS: needs that switch on."
                                                     if spec.dangerous is True else "")
                  + f"\n\nArgs {shape or '(none)'}: positional, key=value, or one JSON object."),
            short_help=_cost_line(spec))

    def resolve_command(self, ctx, args):
        if len(args) >= 2 and not args[0].startswith("-"):
            dotted = f"{args[0]}.{args[1]}"
            if dotted in self._registry():
                return dotted.replace(".", " "), self.get_command(ctx, dotted), args[2:]
        return super().resolve_command(ctx, args)


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
