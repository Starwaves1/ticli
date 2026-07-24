"""Ticli - Terminal music player for TIDAL."""

import click


@click.command()
@click.option("--quality", default=None, type=click.Choice(["LOW", "HIGH", "LOSSLESS", "HIRES"], case_sensitive=False), help="Audio quality for this run (overrides the saved setting)")
def cli(quality):
    """Ticli - Terminal music player for TIDAL."""
    from ticli.player import HeadlessTidalPlayer
    HeadlessTidalPlayer(quality=quality).run()


def main():
    cli()


if __name__ == "__main__":
    main()
