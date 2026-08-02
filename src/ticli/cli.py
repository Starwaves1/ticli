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


@click.command()
@click.option("--quality", default=None, type=QualityChoice(QUALITY_NAMES, case_sensitive=False), help="Audio quality for this run (overrides the saved setting)")
@click.option("--login-flow", default=None, type=click.Choice(["device", "pkce"], case_sensitive=False), help="How to log in when there is no saved session. device (default) is quickest; pkce needs a paste but is the only flow TIDAL streams FLAC to. Settings can switch later.")
def cli(quality, login_flow):
    """Ticli - Terminal music player for TIDAL."""
    from ticli.player import HeadlessTidalPlayer
    HeadlessTidalPlayer(quality=quality, login_flow=login_flow).run()


def main():
    cli()


if __name__ == "__main__":
    main()
