# Ticli

An unofficial terminal music player for TIDAL. Search, browse, queue, download, and play music — all from your terminal. Not affiliated with TIDAL.

Ticli connects directly to TIDAL's API using your premium account. No desktop app needed. Just authenticate, search, and play — real FLAC included.

Works on **macOS** and **Linux**.

<img width="1350" height="1082" alt="image" src="https://github.com/user-attachments/assets/93aaa46d-7340-4fc2-8e07-26f7dd287ddd" />

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

Requires Python 3.10+ and [mpv](https://mpv.io). No mpv? ffplay (part of
[ffmpeg](https://ffmpeg.org)) works as a fallback — playback, pause, and
seeking all function; you lose the macOS media keys and volume above 100%.

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

## From the command line

Verbs that talk to the running player, one line of output each (`ticli --help` lists them all):

```bash
ticli status | pause | resume | next | prev          # "nothing playing" if no player is running; only resume starts one, for the saved track
ticli play                                           # bare: the same as resume ("nothing to play" if nothing was playing)
ticli start playlist edm                             # play it, then open the TUI here (--no-tui: just play)
ticli start                                          # bare: resume what was playing, then open the TUI
ticli queue "marc rebillet - reach out" --next       # add after the current track (default: the end); never starts playback
ticli playlist create "Road trip"
ticli playlist add "Road trip" "daft punk - one more time"
ticli like                                           # the playing track
ticli download https://tidal.com/browse/track/123
```

Playlist names match your own playlists case-insensitively with no TIDAL request, otherwise one TIDAL search; several matches print a numbered top 5 (`ticli start playlist 2` picks). A song is a track id, a TIDAL URL, `"artist - title"` (added only when the match is confident, otherwise you get candidates) or `current`. Dangerous verbs (deleting, clearing the cache, logout) ask y/N. Run without a terminal, a verb is treated as an AI agent and obeys the TUI's AI-control switches.

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

## Agents

`ticli agent` is the surface for callers that are programs — an AI assistant
building you a playlist, a script checking what's playing. Everything under it
speaks JSON on stdout, exactly one object per invocation, with structured
errors and a nonzero exit when something's wrong. No screen-scraping, no
importing internals.

The complete contract lives in the tool itself — `ticli agent docs` prints
every verb with its request cost and JSON shape, the rate rules, and the
workflows an agent should follow. Point your AI at that; this section is
just the trailer.

```bash
ticli agent status                                  # session, FLAC entitlement, player state — costs 0 requests
ticli agent search "four tet baby" --type track     # 1 request
ticli agent resolve --artist Folamour --title "The Journey"
ticli agent playlist create "Morning Uplift"
ticli agent playlist add <playlist-id> <track-id>...
```

Three things make it safe to point an agent at:

- **You hold the switches.** The settings page (`c`) has "Allow AI control"
  (on), "Allow dangerous commands" (off) and an optional "AI control key"
  that agents must pass as `TICLI_AI_KEY`. Only a keypress in the TUI
  changes them, and every refusal tells the agent to ask you.

- **Rate limiting is enforced, not suggested.** Every request goes through a
  cross-process throttle (2 seconds apart, however many agents are running).
  Each verb's `--help` states what it costs.
- **A block stops everything.** If TIDAL answers 429 or flags the session,
  *all* agent requests fail fast with a structured error until you — a
  human — run `ticli agent unblock`. Agents can't retry their way into
  getting your IP banned, because retrying is exactly how that happens.

`resolve` is the verb agents should reach for when they know a song and need
*the* track: it ranks candidates, refuses to let a wrong artist win however
good the title match, flags remixes and edits you didn't ask for, and says
plainly whether it's confident — so the agent (or you) decides, instead of
discovering a cover version in your playlist later.

## How it works

Ticli uses [tidalapi](https://github.com/tamland/python-tidal) to authenticate (device or PKCE OAuth) and fetch stream manifests. Audio plays through [mpv](https://mpv.io) — or ffplay, if mpv isn't installed — and on macOS mpv is also what powers the media keys and Now Playing. Hi-res DASH streams arrive as segments and are stitched into a local playlist mpv reads natively. The TUI is [Rich](https://github.com/Textualize/rich), repainting only when something actually changed — an idle player costs roughly zero CPU and zero network.

```
┌─────────┐     OAuth      ┌───────────┐    stream URL    ┌───────────┐
│  Ticli  │ ──────────────► │  TIDAL    │ ──────────────►  │    mpv    │
│  (TUI)  │ ◄────────────── │  API      │                  │           │
└─────────┘    metadata     └───────────┘                  └───────────┘
```

## Contributing

The code is the documentation. [`CLAUDE.md`](CLAUDE.md) maps the codebase and
lists the few rules that prevent real damage, [`GLOSSARY.md`](GLOSSARY.md)
defines the project's terms, and [`docs/adr/`](docs/adr/) records the
decisions the code can't explain on its own.

One constraint is worth knowing before you touch anything: **be frugal with
TIDAL's API.** Rate-limiting an account is easy to do by accident and it stops
the owner's music ([ADR-0001](docs/adr/0001-tidal-rate-limits.md)).

## Requirements

- macOS or Linux
- Python 3.10+
- TIDAL Premium subscription
- mpv (or ffmpeg's ffplay)

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
