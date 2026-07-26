# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Ticli: a terminal music player for TIDAL. Streams lossless/hi-res audio directly from TIDAL's API via tidalapi + ffplay.

## Repository Layout

- `src/` — Python package (`ticli`)
  - `ticli/player.py` — Main player (TUI, audio, search, queue, playlists)
  - `ticli/cli.py` — Click CLI entry point
  - `ticli/utils/credential_store.py` — Secure OAuth token storage
  - `ticli/utils/cache.py` — On-disk metadata/audio cache + budget
  - `ticli/utils/artwork.py` — Cover art: JPEG decode, pixel art, art cache
  - `ticli/tests/` — E2E tests

## Commands

```bash
# Activate the Python environment
source ./src/.venv/bin/activate

# Install the package (editable)
cd src && pip install -e ".[keyring]"

# Run tests
pytest ticli/tests/ -v

# Launch the player
ticli
ticli --quality HIRES
```

## Architecture

Ticli uses `tidalapi` (community Python client) to authenticate via OAuth and fetch audio stream URLs. Audio is played through ffplay (from ffmpeg). The TUI is built with Rich's `Live` display.

### Audio Playback

- ffplay: kills process on pause (instant silence), restarts from the downloaded local copy on resume
- mpv (if available): uses IPC socket for pause/resume
- Nothing fails silently. Neither backend is run quiet any more (`--msg-level=all=error`,
  `-loglevel error`) and stderr goes to a per-player log rather than `/dev/null`.
  `AudioPlayer.failure()` reads it back when the process has exited with a *positive*
  status — a zero is the end of the track and a negative one is a signal, which is
  `stop()`/`pause()` doing their job. `_monitor_playback` checks it on the same tick
  that already notices a dead player, toasts what the backend actually said, and stops
  rather than advancing: whatever it could not play, the next track is usually the same
  kind of thing. This is not decoration — the regression it exists for played an entire
  library as silence with a normal-looking UI.

### Segmented (MPEG-DASH) streams

A lossless/hi-res stream does not arrive as a file. It arrives as an MPEG-DASH
manifest naming an initialization segment plus N fragmented-MP4 media segments,
which is why `_stream_url` branches on `manifest.is_bts`. The segments are
rewritten as an HLS playlist (`_hls_playlist` → `_write_hls_playlist`), because
HLS is the one segmented format ffmpeg — and therefore both backends — can
demux; this ffmpeg has no DASH demuxer built in at all.

Two things make that work, and both were missing:

- **`#EXT-X-MAP`.** The fragments carry no `moov` of their own, so without the
  initialization segment declared as a map every one of them is undecodable
  ("trun track id unknown, no tfhd was found", once per segment, then silence).
  tidalapi's own `get_hls()` omits it and lists the init segment as if it were
  audio, so it is not used; the playlist is built here, at `HLS_VERSION` 7.
- **Telling the player what it is.** ffmpeg's default protocol whitelist for a
  *file* input is `file,crypto,data`, so every remote segment fails; and mpv
  would otherwise treat an `.m3u8` as a list of files to play in turn. Hence
  `_hls_flags()`: `--demuxer=lavf --demuxer-lavf-format=hls` plus a
  length-prefixed `protocol_whitelist` for mpv (its key-value lists split on
  commas), `-protocol_whitelist … -f hls` for ffplay.

Caching a segmented track is the same job with more requests: init segment
followed by every media segment, written end to end, *is* the fMP4 file, and
both backends then open the cached copy with no flags at all. `_start_download`
reads the segment list back out of the playlist it was handed (`_hls_segments`),
which keeps a stream a single string everywhere else in the player.

- macOS media keys (mpv only): mpv registers with MPRemoteCommandCenter, so keyboard
  media keys / AirPods taps / Control Center reach it. Ticli rebinds those keys over
  IPC (`keybind`) to write `user-data/ticli/media-key`, which `_monitor_playback` polls
  on its existing 0.5s tick. Gated on `IS_MACOS`; a silent no-op elsewhere and on ffplay.

### Search

Search mode types the query with every printable key, so the only key left
for a filter is one that isn't printable: `Tab` cycles the scope (All /
Tracks / Albums / Artists / My Playlists, `Shift-Tab` backwards) and the
scope row under the query says which is active. Changing scope drops the
results but never fetches — `Enter` is the one keystroke that costs a
request, the same as it is after typing.

Under a type filter the whole page is that type; under All it stays the
50/30/20 split. `session.search()` is asked for `page_size` of each category
at an offset, and whatever the page had no room for is kept in
`_search_pool`, so scrolling off the bottom usually costs nothing —
`_search_more` spends the pool first and only fetches when it runs dry. The
fetch is a daemon thread, one at a time (`_search_fetching`), no sooner than
`SEARCH_FETCH_MIN_INTERVAL` after the last, and never past TIDAL's 300-item
`SEARCH_MAX_OFFSET`; a held-down arrow therefore cannot fan out into
requests. Rows are appended, so the cursor never moves under the user, and
`_search_gen` makes a page that lands after the query changed throw itself
away.

"My Playlists" is answered entirely from `cache.iter_tracks()` — TIDAL has
no server-side search of your own playlists — so it is instant, runs on the
UI thread, and makes no request at all. Case-insensitive substring over
name, artists and album, with title matches first; each row says which
playlist it came from and resolves through `_resolve_track` before playback.
An empty index or `cache_metadata` turned off says so instead of looking
like a query with no hits.
### Album artwork

`utils/artwork.py` paints the cover above the track line as half-block pixel
art (`▀`, foreground = top pixel, background = bottom pixel, so one cell is
two pixels). On by default; `show_artwork` in `SETTINGS_SPEC` toggles it live.

No new dependency was added to get there. TIDAL serves covers as JPEG from
`resources.tidal.com` — unauthenticated, so no API call is involved, only
`album.cover` — and the stdlib cannot decode a JPEG, so this module does. It
never needs full resolution: the DC coefficient of an 8x8 block *is* that
block's mean, so decoding only the DC terms yields the image at 1/8 scale
(measured against ffmpeg: within 1/255 on real covers). The images TIDAL
sends are progressive (SOF2), whose **first scan is the DC scan**, so the
decoder reads one scan of a 320x320 file — about 2 ms — and stops. Baseline
JPEG works too (DCs kept, ACs stepped over). Anything else (arithmetic
coding, lossless, 12-bit) falls through to ffmpeg if it happens to be
installed, and to no artwork if it isn't; ffmpeg is not a dependency.

Fetch, decode and rescale all happen on a daemon thread — the paint that
wants a picture starts one and returns None, and the thread `_wake()`s the
loop when it lands, because `Live` runs with `auto_refresh=False`. It is
guarded by `_artwork_request`, a whole-tuple compare of `(cover, cols,
rows)`, so repaints don't re-fetch and a resize or a track change makes an
in-flight result stale rather than wrong. Renderings are cached on disk in
`CACHE_DIR/artwork` as `{cover}-{cols}x{rows}.art` (hex pixels, atomic
write, 0o600) — keyed on the size because the pixels *are* the render.
Deliberately outside `audio_dir()`: the song count, `total_bytes` and
eviction are about audio, and artwork keeps its own `MAX_ART_FILES` ceiling
instead. `MetadataCache.clear()` clears it; `clear_artwork()` only unlinks
`.art`/`.tmp`, never the directory.

Every failure is a missing picture, never an error: no cover, no colour
(Rich reports the terminal's `color_system`; truecolor and 256 render, 16
and none don't), a terminal too small (`art_size`), an offline fetch, an
undecodable file. Art is only drawn in full player mode — not in the mini
player, not above a list.
### Login flows and quality entitlement

Two OAuth flows, and the difference is audible. The **device flow**
(`login_oauth`, the default) is the smooth one — a code on your phone,
nothing to paste back — but its TIDAL client is only entitled to AAC:
it accepts a `LOSSLESS`/`HI_RES_LOSSLESS` request and grants `HIGH`,
silently, with a byte-identical manifest. The **PKCE flow**
(`pkce_login_url` → `pkce_get_auth_token` → `process_auth_token`, driven
step by step rather than through tidalapi's `login_pkce()`, which
`print()`s and `input()`s) uses the client tidalapi documents as "the
only way how to get access to HiRes … FLAC files". It delivers: a PKCE
session asking for hi-res is granted `LOSSLESS` and served **FLAC
16/44.1** in an MPEG-DASH manifest, where the device flow got AAC in a
BTS one. Measured, not assumed — one real `get_stream()` reported
`codecs=FLAC`, `mimeType=audio/mp4`, `bit_depth=16`,
`sample_rate=44100`, and ffmpeg then decoded `flac, 44100 Hz, stereo,
s16` from those segments. The format change is the whole reason
segmented playback matters (see above). It is opt-in: `[u]`
on the settings page, or `--login-flow pkce` on a first run. The paste
is unavoidable — the redirect URI is fixed to a tidal.com page in
tidalapi's config and re-sent in the token exchange, so no localhost
listener can stand in — and it has to work over SSH anyway, so the
prompt also accepts a bare code. The TUI stands down for the duration
(`_suspended_tui`): the one place the Live display is deliberately
paused.

Stored tokens record **which flow issued them** (`is_pkce`), because
`Session.token_refresh` picks the client id/secret from that flag alone.
A record that lost it refreshes against the wrong client and the session
dies hours later, looking like a random logout. Records predating the
flag are device-flow by construction, so migration is a defaulted read —
nothing on disk is rewritten and nobody is logged out.

Quality gating is evidence-based and costs no requests: `_stream_url`
asks for the whole stream description (one request either way, and
`get_url()` raises on a PKCE session), and `_note_granted_quality`
remembers only a *downgrade* — being granted what you asked for says
nothing about the tiers above it. Gated tiers stay listed and dimmed
with the reason rather than hidden; when nothing has been observed,
nothing is gated. A successful PKCE upgrade clears the ceiling, so the
tiers re-open without a restart. Songs already cached keep their old
quality — the upgrade toast says so and points at `[x]`.

### Caching

`utils/cache.py` holds a metadata index (your playlists, and the tracks in
each) in the OS's own cache directory — never in `~/.config/ticli`. Lists
paint from it instantly, but it never answers alone: every read is paired
with the live fetch, which replaces what was shown one round trip later.
Cached rows are `CachedTrack` / `CachedPlaylist` records, not tidalapi
objects; anything that needs the real thing resolves through the session
first. Two independent settings gate it — `cache_metadata` (the index) and
`cache_songs` (whole tracks) — and `cache_budget_gb` sizes it in whole
gigabytes; eviction runs after writes, never on a timer. Disabling and
clearing are separate: turning `cache_songs` off asks `Clear cached
songs as well?` (y clear, n keep them, Esc cancel) and touches neither
the setting nor the disk until that is answered. Clearing is also its
own action — `[x]` on the settings page, a keybinding outside
`SETTINGS_SPEC` for the same reason logout is, with its own
confirmation. A clear really clears: files in use are unlinked too. On
POSIX the playing process keeps its descriptor and plays on (verified
with mpv); if it had not opened the file yet it exits at once and
`AudioPlayer.source_vanished` + `_monitor_playback` restart the track
from the network where it left off (`_track_has_time_left` keeps that
from firing on a track that had already finished). On Windows a file
open without delete-sharing cannot be unlinked; those are counted and
reported in the toast rather than passed over silently. Deletion and
eviction only ever unlink files ticli itself wrote (`is_owned_audio` /
`owned_audio_files`: `{track_id}{ext}` and `.part`) — never the
directory — so anything else living there survives.

`cache_songs` also keeps the audio. TIDAL serves it unencrypted over plain
HTTP either way, so `AudioPlayer._start_download` is a `requests` GET on a
daemon thread while the player streams the same source — no ffmpeg, and
identical on mpv and ffplay because the download no longer rides on the
player process. A BTS stream is one file and therefore one GET; a segmented
one is the init segment plus every media segment written end to end into
the same handle (see above). The file is named `{track_id}{ext}` where the
extension comes from the CDN's `Content-Type` (AAC-in-MP4 on a device-flow
session, FLAC-in-MP4 on a PKCE one), written as `.part` and renamed only
when whole, so a lookup by stem can never serve a partial file. A `stop()`
bumps a generation counter, which is how an abandoned download knows to
delete itself. Eviction unlinks with `missing_ok`: two sweeps racing (two
downloads landing together) must not read "already gone" as "still costing
us" and go on to evict a file that fits.

### Key Files

| File | Purpose |
|------|---------|
| `player.py` | Player TUI, audio control, search, queue, playlists (~1400 LOC) |
| `cli.py` | CLI entry point |
| `utils/credential_store.py` | OAuth token storage (keychain + fallback) |
| `utils/cache.py` | Metadata cache, cached audio, budget + eviction |
| `utils/artwork.py` | JPEG decoder, cover art rendering + its own disk cache |

## Testing

Tests use Click's `CliRunner` and subprocess calls to verify CLI help text and argument parsing. No running TIDAL instance needed.
