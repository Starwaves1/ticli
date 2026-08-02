# Ticli

An unofficial terminal music player for TIDAL. Search, browse, queue, download, and play music — all from your terminal. Not affiliated with TIDAL.

Ticli connects directly to TIDAL's API using your premium account. No desktop app needed. Just authenticate, search, and play — real FLAC included.

Works on **macOS** and **Linux**.

```
╭──────────────────────── Ticli ────────────────────────╮
│                                                        │
│  ▶ ♥ Arlo Parks - Sophie                               │
│     Super Sad Generation                               │
│     1:47 ━━━━━━━━●━━━━━━━━━━━━━━━━━━━━━━━━━━━ 3:28    │
│     Queue: 3/12  16/44.1 FLAC                          │
│     Next: Cola • Arlo Parks                            │
│                                                        │
│  [space] play/pause  [←/→] prev/next  [s] search       │
│  [v] volume  [t] tiny  [m] more  [h] hide              │
│                                                        │
╰────────────────────────────────────────────────────────╯
```

## Features

- **Real lossless & hi-res** — FLAC up to 24-bit/192 kHz, with quality tiers named the way TIDAL's own app names them
- **Search** — tracks, albums, artists, playlists; `Tab` narrows the scope, and an empty search box recalls your recent searches
- **Browse** — albums, playlists, and full artist pages
- **Downloads** — keep tracks in your own music folder, tagged; whole playlists three at a time with visible progress
- **Offline-first** — downloaded and cached songs play from disk without touching the network
- **Queue & radio** — manage the queue, or generate a station from any track
- **Scrubbing** — seek through a track from the progress bar
- **Mini mode** — condensed single-line display, and `h` hides the controls while you just listen
- **Session restore** — reopens on the track you left, paused where you left it
- **Settings page** — quality, cache budget, artwork, and account in one place
- **Secure auth** — OAuth tokens in your OS keychain
- **macOS media keys** — AirPods taps, Control Center and Now Playing just work

## Install

Requires Python 3.10+ and [mpv](https://mpv.io).

```bash
# macOS
brew install mpv
pip install tidal-cli

# Ubuntu / Debian
sudo apt install mpv python3-pip
pip install tidal-cli
```

For secure token storage in your OS keychain (recommended):

```bash
pip install "tidal-cli[keyring]"
```

## Usage

```bash
ticli
```

On first run you'll get a URL to authorize with your TIDAL account. After that, your session is cached and you go straight to the player.

### Login, and where FLAC comes from

Two sign-ins exist, and they are not equal:

- **Device** (the default) — open a URL, type a code, done. Quick, but TIDAL's device flow is only entitled to AAC: ask it for lossless and it quietly serves 320k.
- **PKCE** (`ticli --login-flow pkce`) — sign in in your browser, then paste back the address it lands on. The landing page *looks* broken; that's expected — the address bar is carrying your login code. This is the only flow TIDAL streams FLAC to.

Already signed in the quick way? Press `u` on the settings page to upgrade in place — no restart, your queue keeps playing. `o` logs out.

### Quality

```bash
ticli --quality MAX     # FLAC, up to 24-bit/192 kHz
ticli --quality HIGH    # FLAC, 16-bit/44.1 kHz — the default
ticli --quality MEDIUM  # AAC 320 kbps
ticli --quality LOW     # AAC 96 kbps
```

The names follow TIDAL's own app — `MEDIUM` is the 320k option TIDAL files under Low's bitrate dropdown. The flag overrides for one run only; the saved setting lives on the settings page (`c`). The old spellings `LOSSLESS` and `HIRES` still work as aliases.

`HIGH` and `MAX` are FLAC, so they need the PKCE login. Tiers your login can't stream are shown dimmed with the reason rather than hidden — and never silently served as something else.

### Keybindings

#### Player

| Key | Action |
|-----|--------|
| `space` | Play / pause |
| `←` `→` | Previous / next track |
| `↑` | Focus the progress bar — then `←` `→` seek 10s, `↓` back |
| `s` | Search |
| `v` | Volume (`←` `→` adjust, `Esc` close) |
| `t` | Mini player |
| `h` | Hide the controls until you press something |
| `m` | More: `l` like · `r` radio · `y` add to playlist · `d` download · `q` queue · `p` playlists · `c` settings |
| `esc` | Quit |

#### Everywhere else

`↑` `↓` navigate · `Enter` / `→` open or play · `←` / `Esc` back · `space` still pauses · `v` still opens volume.

| Screen | Extra keys |
|--------|-----------|
| Search | `Tab` cycle scope (tracks / albums / artists / playlists / your playlists) |
| Album / playlist | `a` play all · `y` add to playlist · `d` / `D` download one / all · `x` remove (your own playlists) |
| Artist | `Tab` switch section · `a` play all · `d` / `D` download |
| Queue | `Enter` jump · `x` remove · `y` / `d` / `D` as above |
| Settings (`c`) | `←` `→` change · `o` log out · `u` FLAC sign-in · `R` re-fetch library at current quality · `d` downloads list · `x` clear cache |
| Downloads | `x` delete a file |

## Downloads

`d` downloads the track under the cursor — from the player, an album, an artist page, or the queue — and `D` takes the whole list, three at a time. Files land in `~/Music/Ticli/Artist/Album/`, tagged, and they're *yours*: ticli never deletes, replaces, or re-streams them behind your back. Downloaded tracks always play from disk, whatever the quality setting says; upgrading them is one explicit keypress (`R` in settings), never a side effect. A tier your login can't have steps down a rung instead of failing, and says so.

Separately from downloads, ticli keeps a bounded on-disk cache (2 GB by default, budget adjustable in settings) so repeat plays cost no data at all.

## How it works

Ticli uses [tidalapi](https://github.com/tamland/python-tidal) to authenticate (device or PKCE OAuth) and fetch stream manifests. Audio plays through [mpv](https://mpv.io) — which on macOS is also what powers the media keys and Now Playing. Hi-res DASH streams arrive as segments and are stitched into a local playlist mpv reads natively. The TUI is [Rich](https://github.com/Textualize/rich), repainting only when something actually changed — an idle player costs roughly zero CPU and zero network.

```
┌─────────┐     OAuth      ┌───────────┐    stream URL    ┌───────────┐
│  Ticli  │ ──────────────► │  TIDAL    │ ──────────────►  │    mpv    │
│  (TUI)  │ ◄────────────── │  API      │                  │           │
└─────────┘    metadata     └───────────┘                  └───────────┘
```

## Contributing, and a note for AI agents

**Start with [`ai/README.md`](ai/README.md).** Much of this project was built
with AI assistance, and `ai/` is where the reasoning lives: the constraints that
are load-bearing, what was measured, what was tried and rejected, and the
incidents that produced the rules. It is short, and reading it first will save
you from re-deriving things the hard way — several of its rules exist because
violating them broke something real.

Two constraints are worth knowing before you touch anything:

- **Be frugal with TIDAL's API.** Rate-limiting an account is easy to do by
  accident and it stops the owner's music. `ai/WORKING-RULES.md` has the
  specifics.
- **Tests assert observable reality** — bytes on disk, escape sequences on the
  terminal, request counts — not internal bookkeeping. A feature here once
  passed its whole suite for two days without writing a single byte.

If you are an AI agent working on this repository, **updating `ai/` is part of
the work, in the same commit as the code.** `ai/README.md` says what belongs
where.

## Requirements

- macOS or Linux
- Python 3.10+
- TIDAL Premium subscription
- mpv

## Credits

Created and maintained by [odonald](https://github.com/odonald).

Contributors:

- [Garrett Simko](https://github.com/Starwaves1) — lossless/hi-res playback (PKCE login, segmented
  DASH streams), downloads, album artwork, metadata and audio caching, scrubbing, scoped search,
  the artist page, and macOS media keys.

## Support

If you enjoy Ticli, consider [buying me a coffee](https://buymeacoffee.com/odonald).

## License

MIT
