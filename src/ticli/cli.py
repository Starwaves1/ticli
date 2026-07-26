"""Ticli - Terminal music player for TIDAL."""

import click


@click.command()
@click.option("--quality", default=None, type=click.Choice(["LOW", "HIGH", "LOSSLESS", "HIRES"], case_sensitive=False), help="Audio quality for this run (overrides the saved setting)")
@click.option("--login-flow", default=None, type=click.Choice(["device", "pkce"], case_sensitive=False), help="How to log in when there is no saved session. device (default) is quickest; pkce needs a paste but is the only flow TIDAL streams FLAC to. Settings can switch later.")
def cli(quality, login_flow):
    """Ticli - Terminal music player for TIDAL."""
    from ticli.player import HeadlessTidalPlayer
    HeadlessTidalPlayer(quality=quality, login_flow=login_flow).run()


def main():
    cli()


if __name__ == "__main__":
    main()
