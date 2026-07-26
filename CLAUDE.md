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

### Scrubbing

`←`/`→` already mean prev/next, and in a list they mean back/open, so seeking
gets a home rather than a key: **`↑` gives the player the arrows** — from the
top of a list, where `↑` had nowhere left to go, and from the player screen,
where it did nothing at all. Focused, `←`/`→` move `SEEK_STEP_SECONDS` inside
the track and `↓` (or Esc) hands them back; anything else — typing, `[t]`,
Enter — means you are done scrubbing, so focus drops and the key goes on to the
screen it was meant for. `_handle_focus_key` is the whole interaction, checked
once before the mode dispatch, so no screen has its own copy. Not the settings
page (`←`/`→` are the value there) and not the add-to-playlist picker.

`_seek_by` moves `_play_offset` on the keypress, before anything is asked of
the backend: everything reads position through `_get_position()`, so the bar,
the saved resume position, the prefetch and the auto-advance all follow from
that one assignment, and a seek that only appeared on the next poll felt
broken. Both ends clamp — past the start is 0:00 rather than the previous
track, and past the end stops `SEEK_END_MARGIN` short rather than advancing,
because landing on EOF makes the backend exit and reads as "the track ended".

Reaching the backend is separate and coalesced. `AudioPlayer.seek_to()` is an
IPC seek on mpv (playing or paused), a change to the number `resume()` will
start from on a paused ffplay, and on a *playing* ffplay a kill and a respawn
at the offset from the same source — deliberately not `stop()`/`play_url()`,
which would bump the download generation and make every scrub abandon the copy
being fetched for this very track. False means "could not be moved", and the
caller starts the track there instead. `_flush_seek` lets at most one seek
through per `SEEK_COALESCE_SECONDS`, on a daemon thread; a press that lands too
soon leaves its position pending and the monitor's existing 0.5s tick delivers
it, so a held-down arrow is a couple of seeks a second and never a respawn per
key repeat. The same tick skips its mpv position resync while a scrub is
pending, or the bar would snap back to where the track was before the arrow.

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
scope row under the query says which is active. **`Tab` applies the scope
as you land on it** — no second keystroke — and it is still true that one
search is one request, because a fetch buys the *query*, not the scope.

`session.search()` is asked for **all three categories** whatever scope
asked for it: `limit` is applied per type, so that is the same single
request a scoped search used to make, with the other scopes paid for. What
comes back goes into `_search_reservoir`, one per query, kept whole and
never consumed. Each scope gets a `_search_views` record drawing on it —
its own rows, cursor and `consumed` depth — so two scopes sit at two depths
in the same rows, tabbing away and back restores the rows *and* the pages
already fetched, and `_go_back` from an album needs nothing but the query
and the scope. Cycling every scope after a search costs **zero** requests,
measured in `TestScopeCache`; the artist page's tabs work the same way and
for the same reason.

Presence of a view record — loading or ready — is the whole answer to "has
this scope been applied", which is what stops a held-down `Tab` fanning
out. From cold it is `_search_fetching` that does it: the first `Tab`
starts the one request and every scope behind it is left loading until that
page lands, when `_fill_waiting_search_views` answers all of them at once.
So 40 rapid presses are one request, and no new machinery: the same
single-flight flag, `SEARCH_FETCH_MIN_INTERVAL` and `_search_gen` that
paging already used.

Under a type filter the whole page is that type; under All it stays the
50/30/20 split. `_search_pool` is now derived — what the reservoir holds
that this scope has not shown, in this scope's categories only — and
`_search_more` spends that before asking, never past TIDAL's 300-item
`SEARCH_MAX_OFFSET`, one fetch at a time and no sooner than
`SEARCH_FETCH_MIN_INTERVAL` after the last. Rows are appended, so the
cursor never moves under the user. `exhausted` is per category because they
run out at different depths: Albums being done says nothing about Tracks.

The cache is **per session and in memory only** — deliberately not the
on-disk index in `utils/cache.py`. It holds **one query at a time**: typing
or backspacing drops the reservoir and every view (`_reset_search_results`,
which also bumps `_search_gen` so a page in flight throws itself away), and
`Enter` clears them too, which is what makes it the explicit refresh. Memory
was never the reason — staleness is, and a query is retyped deliberately.
Three states read differently: `Searching…` in yellow for a scope with no
rows yet, `No results found` in green for one the query had nothing for, and
`Already loaded this session` under rows that cost nothing. The scope row
marks every scope already answered, so "Tab is free from here" is visible.

"My Playlists" is answered entirely from `cache.iter_tracks()` — TIDAL has
no server-side search of your own playlists — so it is instant, runs on the
UI thread, and makes no request at all. Case-insensitive substring over four
plain-text fields, and the *order* is the design: **track title, then artist,
then album, then the playlist's own name**. Ranking matters more the more
fields there are — a playlist name is one string standing in for every track
under it, so a query that matched only it would otherwise bury the track
actually called that under a whole loosely-named playlist; last place keeps
"type the playlist's name, get its tracks" working without letting it flood
anything. Within a rank the index's own order survives. The one indexed field
left out is the playlist's creator: on your own playlists that is your name on
every row, which matches everything or nothing. Each row says which playlist it
came from and resolves through `_resolve_track` before playback. An empty index
or `cache_metadata` turned off says so instead of looking like a query with no
hits.
### The artist page

Four tabs — Top Tracks, Albums, Playlists, Suggestions — and `Tab` /
`Shift-Tab` walk them, the same key search uses for its scope and free here
because nothing on this screen types. It could not be `←`/`→` (back and open)
and it could not be `↑` (that hands the arrows to the player for scrubbing);
the tab row under the artist's name is always on screen, so which section you
are in is never hidden state.

**Each tab is its own request and none of them runs until you land on it.**
Opening the page costs one; browsing all four costs five, because Suggestions
is two endpoints. The results are kept for the session in `_artist_sections`
keyed by `(artist id, section)`, and the *presence* of a record — loading,
ready or failed — is the whole answer to "has this tab been visited", so a
held-down `Tab` cannot fan out into requests, coming back is instant, and
`_go_back` from an album lands on the same tab with the same rows and asks
TIDAL for nothing. Cursors are remembered per tab in `_artist_cursors`.

Which sections are real is decided by what tidalapi's `Artist` actually has.
Top Tracks is `get_top_tracks()`, Albums is `get_albums()`, Suggestions is
`get_radio()` (the artist mix's tracks) followed by `get_similar()`. There is
**no `artists/{id}/playlists` endpoint and no accessor for one**, so Playlists
comes from `artist.page()` — the `pages/artist` document listen.tidal.com
itself renders — filtered for `tidalapi.Playlist` items and deduped. One
request, real rows, and an artist nobody has playlisted says so.

Loading, empty and failed are three different screens on purpose: `Loading
albums…` in yellow, `No albums for this artist` in green (information), and
`Failed to load albums` in **red** with `[Enter] retry`, because a section
that lost its one request would otherwise stay lost for the session. Losing
one half of Suggestions still shows the other half; only both failing is a
failure. Nothing is a dead end — `Tab` works while a section is loading or
failed, `←`/`Esc` always leaves, and `↑` at the top of the list still gives
the player the arrows.

Selecting a row goes through the existing navigation and nothing parallel:
album → `_open_album`, playlist → `_open_playlist`, artist → `_open_artist`
(a similar artist opens their page, with its own tabs), track → play, with
the queue set to that section's tracks only — the position is counted, not
searched for, because a mixed section holds artists too.

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

### The display loop

`Live` runs with `auto_refresh=False` — repaints are driven by `_repaint()`,
inline with input (231ms → 1.1ms) and otherwise only when the frame actually
changed — and with **`screen=True`**, the alternate screen buffer, which is
load-bearing rather than cosmetic.

Without it Rich repaints by counting: it walks the cursor up exactly as many
rows as the last frame was tall, erasing each one, then prints the new frame
over the top. Measured at 100x30 with artwork, one repaint was 22 erase-line
and 21 cursor-up sequences against a 22-row frame. That arithmetic is only
true while the terminal has not moved underneath it, and a resize breaks both
halves of it at once: terminals *reflow* on a width change, so a 22-row frame
laid out for 100 columns becomes 44 rows at 60 and Rich erases 22 of them;
and a window that got shorter has already scrolled the top of that frame into
the scrollback, where no cursor-up can reach it. Whatever is left over stays
on screen forever and the next repaint strands another one above it — the
reported artifact was eight bands of album art and three stacked `Ticli`
panels showing three different timestamps. On the alternate screen there is
no arithmetic to get wrong: every refresh homes the cursor and writes every
row of the terminal (0 cursor-ups, 30 rows, ~6% more bytes), so nothing can
be stranded by a resize, by a frame that shrank (full player to mini), or by
artwork appearing, vanishing or changing size. The player also stops
scribbling on the scrollback it was launched from: the `console.clear()`s in
`run()` and `_suspended_tui` are gone, and on exit the terminal comes back
exactly as it was.

A resize still has to be *noticed*, since nothing else would repaint until
the next key or idle tick: `SIGWINCH` sets `_resized` and `_wake()`s the loop
— no new thread, no timer — and the loop forces the next repaint. `_repaint`
also keys its skip-if-identical cache on `console.size` as well as the
segments, because most of the panel does not depend on the terminal's height:
a window that only got shorter renders byte-identical segments, and that is
the one repaint that must not be skipped.

`MIN_ROWS_AROUND_ART` is part of this. A pane that overflows is not harmless
— Rich answers it by replacing the bottom line, the controls, with a red
ellipsis — and the pane is not one height: `[m]` adds a controls row and a
toast adds another, which overflowed a 24-row terminal by exactly one. Known
and *not* fixed: the paged lists size their page from `page_size` in the
config rather than from the terminal, and the settings page does not scroll
at all, so both can still overflow a short window (queue: 43 rows for 40
tracks; settings: 32 rows at any width).

`tests/vt.py` is a small terminal model — enough of the escape sequences Rich
emits to replay a session into a fixed-size grid, *including reflow on
resize*, which is the part a merely-clipping model would let the bug through.
`tests/test_display.py` drives the real `Live` against it and asserts on what
the eye asks: one whole panel on screen, nothing above or below it, nothing
in the scrollback.

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
