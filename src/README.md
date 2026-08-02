# Ticli

An unofficial terminal music player for TIDAL. Search, browse, queue, and play music — all from your terminal. Not affiliated with TIDAL.

Ticli connects directly to TIDAL's API using your premium account. No desktop app needed. Just authenticate, search, and play.

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
│  [space] play/pause  [n/→] next  [←] prev             │
│  [s] search  [q] queue  [p] playlists                  │
│  [l] like  [r] radio  [t] mini  [m] more               │
│                                                        │
╰────────────────────────────────────────────────────────╯
```

## Features

- **Search** — Find tracks, albums, artists, and playlists
- **Browse** — Navigate album and playlist tracklists
- **Queue** — Manage your playback queue, reorder, remove tracks
- **Playlists** — Browse and play your saved playlists
- **Likes** — Toggle favorites on any track
- **Radio** — Generate a station from any track
- **Mini mode** — Condensed single-line display
- **Session restore** — Picks up where you left off
- **Lossless & Hi-Res** — Stream up to 24-bit/192kHz FLAC
- **Downloads** — Keep tracks in your own music folder, tagged
- **Secure auth** — OAuth tokens stored in your OS keychain

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

### Quality

```bash
ticli --quality MAX     # FLAC, up to 24-bit/192 kHz
ticli --quality HIGH    # FLAC, 16-bit/44.1 kHz — the default
ticli --quality MEDIUM  # AAC 320 kbps
ticli --quality LOW     # AAC 96 kbps
```

The names follow TIDAL's own app. FLAC (`HIGH` and `MAX`) requires the PKCE
sign-in: `ticli --login-flow pkce`, or press `u` on the settings page later.
The old spellings `LOSSLESS` and `HIRES` still work as aliases.

### Keybindings

#### Player

| Key | Action |
|-----|--------|
| `space` | Play / pause |
| `n` `→` | Next track |
| `←` | Previous track |
| `s` | Search |
| `q` | Queue |
| `p` | Playlists |
| `l` | Like / unlike track |
| `r` | Start radio from track |
| `t` | Toggle mini player |
| `m` | Show more controls |
| `esc` | Quit |

#### Search

| Key | Action |
|-----|--------|
| `↑` `↓` | Navigate results |
| `enter` `→` | Play track / open album or artist |
| `backspace` | Delete character |
| `esc` `←` | Back |

#### Queue

| Key | Action |
|-----|--------|
| `↑` `↓` | Navigate |
| `enter` | Jump to track |
| `x` | Remove track |
| `esc` `←` | Back |

## How it works

Ticli uses [tidalapi](https://github.com/tamland/python-tidal) to authenticate and fetch audio stream URLs. Audio is played through [mpv](https://mpv.io). The TUI is built with [Rich](https://github.com/Textualize/rich).

```
┌─────────┐     OAuth      ┌───────────┐    stream URL    ┌───────────┐
│  Ticli  │ ──────────────► │  TIDAL    │ ──────────────►  │    mpv    │
│  (TUI)  │ ◄────────────── │  API      │                  │           │
└─────────┘    metadata     └───────────┘                  └───────────┘
```

## Requirements

- macOS or Linux
- Python 3.10+
- TIDAL Premium subscription
- mpv

## Credits

Created and maintained by [odonald](https://github.com/odonald).

Contributors:

- [Garrett Simko](https://github.com/Starwaves1) — lossless/hi-res playback (PKCE login, segmented
  DASH streams), album artwork, metadata and audio caching, scrubbing, scoped search, the artist
  page, and macOS media keys.

## Support

If you enjoy Ticli, consider [buying me a coffee](https://buymeacoffee.com/odonald).

## License

MIT
