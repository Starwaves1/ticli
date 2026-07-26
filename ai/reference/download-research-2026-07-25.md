# Download + size-estimation research — 2026-07-25

> **[Superseded in part — 2026-07-26]** This was probed on a device-flow
> (AAC-only) session. The account has since moved to PKCE and receives real
> FLAC (see `ai/HISTORY.md`, commit `48004f1`). Consequences: §1.4's ±1% size
> accuracy is calibrated on near-constant-bitrate AAC and does **not** hold for
> variable-bitrate FLAC; §2.1's "FLAC is unreachable" finding is no longer
> true; the §2.4 cache bug it identifies was fixed in `46c6c24`. Everything
> about rate limits, DRM, tagging and folder layout still stands.


All findings below were gathered against a **live TIDAL session** (the stored refresh
token in `~/.config/ticli/session.json` loads non-interactively; `check_login()` → True,
uid 206957544). Every number is from a command actually run. Where I could not verify
something it says so explicitly at the bottom.

---

## Verdict

**Feasible, and simpler than expected — but two findings reshape it.**

(1) **This account's session never receives FLAC.** Requesting `LOSSLESS` or
`HI_RES_LOSSLESS` silently returns `audioQuality: HIGH`, AAC-LC 320 kbps in an MP4
container. Everything the app streams today is a *single contiguous, unencrypted,
range-supporting `.m4a` file*. That makes "download to an ordinary playable file"
almost trivial — it is a plain HTTP GET, no remux, no concatenation, no DRM — but it
also means the app's `LOSSLESS`/`HIRES` badges are currently lying, and the honest file
extension is `.m4a`, not `.flac`.

(2) **The stream-URL endpoint is aggressively rate limited, and overrunning it revokes
streaming for ~60–90 s.** 53 sequential `playbackinfopostpaywall` calls in 2.8 s → 429,
then escalating to `401 subStatus 4006 "Session does not have streaming privileges"` —
while the rest of the API kept working. So **per-track requests to size a playlist are
the biggest risk in this feature: they can lock the user out of playback mid-listen.**

The good news is those two findings cancel out. Because the stream is near-CBR AAC,
`duration × nominal bitrate` is accurate to **≈1 %** — so playlist size previews (for
*all* tiers at once) can be computed from data already in the cache index, at **zero
network requests**. Exact sizes are only needed for the tracks you actually download,
where you're paying for the URL anyway.

Also found, incidentally: **the existing FULL audio-cache path is dead code — it has
never written a single byte.** Details in §2.4. Any download work should fix it.

---

## 1. Size estimation

### 1.1 What's on the objects already vs. what costs a request

Free — already on a `Track` you have in hand (`tidalapi/media.py:183-208`, `:312-334`):
`duration` (seconds, int), `audio_quality`, `media_metadata_tags`, `track_num`,
`volume_num`, `isrc`, `copyright`, `replay_gain`, `peak`, `artists`, `album`.
`duration` is already persisted in ticli's own cache index (`utils/cache.py:126-136`,
`track_record` stores `duration`).

Costs one request per track — `Track.get_stream()` → `GET tracks/{id}/playbackinfopostpaywall`
(`media.py:501-527`). Only this returns `bit_depth`, `sample_rate`, the manifest, and
therefore the URL. **There is no explicit file-size field anywhere in the response:**

```
{"trackId":246909121,"assetPresentation":"FULL","audioMode":"STEREO",
 "audioQuality":"HIGH","manifestMimeType":"application/vnd.tidal.bts",
 "manifestHash":"...","albumReplayGain":-8.9,"albumPeakAmplitude":1.0,
 "trackReplayGain":-8.65,"trackPeakAmplitude":1.0}
```

The BTS manifest itself is 4 keys, no size either:

```json
{"mimeType":"audio/mp4","codecs":"mp4a.40.2","encryptionType":"NONE",
 "urls":["https://amz-pr-fa.audio.tidal.com/c93d....mp4?token=1785015974~..."]}
```

### 1.2 Exact size is available, cheaply — *once you have the URL*

`HEAD` on the CDN URL returns `Content-Length` and `Accept-Ranges: bytes`; a
`Range: bytes=0-0` GET returns `Content-Range: bytes 0-0/7931562`. No auth header
needed — the token is in the query string. The CDN is **not** rate limited:
**60 HEADs in 3.5 s, 60/60 → 200.** The URL token is valid for exactly **3600 s**
(decoded `token=1785016805~…` → 2026-07-25 18:00:05, one hour out).

So the expensive half is the API call, not the HEAD.

### 1.3 Cost of sizing a whole playlist — and the hard limit

There is **no batch playbackinfo endpoint** (grep of `media.py`/`session.py`; the only
batching helper, `tidalapi/workers.py`, paginates list endpoints, not playback info).
It is strictly one request per track.

Throughput measured before the block:
- 36 tracks, `ThreadPoolExecutor(8)`: **0.30 s**
- 10 tracks sequential: **0.47 s**

Then it broke. Calibration run, sequential, no delay:

```
BLOCKED after 53 sequential OK in 2.8s -> 429
```

and the follow-up state was worse than a 429:

```
normal API (favorites): 3 tracks OK          <- rest of API unaffected
t+0:   401 {"status":401,"subStatus":4006,"userMessage":"Session does not have streaming privileges"}
t+30s: 401 (same)
t+60s: OK
```

A token refresh did **not** clear it (`token_refresh` → True, `check_login` → True,
`playbackinfopostpaywall` → still 401). Only waiting cleared it, in ~60–90 s.
No `Retry-After` / `X-RateLimit-*` headers are returned; `tidalapi` surfaces the 429 as
`tidalapi.exceptions.TooManyRequests` (`request.py:152-160`).

**Conclusion: a 100-track playlist cannot be sized via `playbackinfo`. It would trip
the limit at ~track 50 and take the user's playback down with it.**

### 1.4 The approximation is good enough — quantify

Measured `duration × nominal_bitrate / 8` against real `Content-Length`, 4 real tracks
per tier (spaced 0.3 s apart to stay under the limit):

```
LOW   nominal= 96000: err min=-2.06% max=-0.65% mean=-1.13%  n=4
HIGH  nominal=320000: err min=-1.20% max=-0.30% mean=-0.67%  n=4
```

Per-track detail at HIGH:

| track | dur | actual CL | est @320k | err |
|---|---|---|---|---|
| ♥ | 197 s | 7,931,562 | 7,880,000 | −0.7 % |
| ハルカ | 244 s | 9,789,680 | 9,760,000 | −0.3 % |
| ミユキ | 211 s | 8,482,782 | 8,440,000 | −0.5 % |
| 1,000 Light Years Away | 140 s | 5,668,226 | 5,600,000 | −1.2 % |

`ffprobe` confirms why: `bit_rate=321527` overall, `bit_rate=320037` on the stream —
AAC here is effectively CBR. The estimate is consistently a *slight under*-estimate
(container overhead), never wildly off.

**Honest labelling:** the error is small and one-signed. Round up ~2 % and show a
single figure prefixed `~` (e.g. `~1.2 GB`). A ± range would imply more uncertainty
than there is. Do **not** call it exact. (This calibration only holds for AAC — see §2.1;
if a future session ever does serve FLAC, VBR error would be far larger and the label
would need to become a range.)

### 1.5 Stretch: all four tiers at once — **free**

Since the estimate is `duration × nominal_bitrate` and `duration` is tier-independent
and already cached, showing all four tiers costs **zero extra requests**. Nominal
bitrates: LOW 96 kbps, HIGH 320 kbps, LOSSLESS ≈1000 kbps (16/44.1 FLAC, ~55 % of the
1411 kbps PCM rate), HIRES highly variable. Doing it via `playbackinfo` instead would
be 4× the requests against a limiter that already blocks at ~50 — don't.

Caveat: on this session the LOSSLESS/HIRES columns would be **advertising sizes the
account cannot actually download** (§2.1). Either omit them or mark them unavailable.

---

## 2. Downloading to an ordinary playable file

### 2.1 What each tier actually serves — and the FLAC finding

Probed all four tiers on track 246909121 (`media_metadata_tags: ['LOSSLESS']`,
`audio_quality: LOSSLESS`, i.e. TIDAL *has* a lossless master):

| requested | granted `audioQuality` | manifest type | codec | encryption | segments | ext | HEAD CL |
|---|---|---|---|---|---|---|---|
| LOW | LOW | `vnd.tidal.bts` | MP4A (`mp4a.40.5`) | `NONE` | 1 | `.m4a` | 2,387,403 |
| HIGH | HIGH | `vnd.tidal.bts` | MP4A (`mp4a.40.2`) | `NONE` | 1 | `.m4a` | 7,931,562 |
| LOSSLESS | **HIGH** | `vnd.tidal.bts` | MP4A (`mp4a.40.2`) | `NONE` | 1 | `.m4a` | 7,931,562 |
| HI_RES_LOSSLESS | **HIGH** | `vnd.tidal.bts` | MP4A (`mp4a.40.2`) | `NONE` | 1 | `.m4a` | 7,931,562 |

LOSSLESS and HI_RES_LOSSLESS return a **byte-identical manifest hash** to HIGH
(`dgU7p9Xryb35iIMw0951f78X0ExIa/eKjVfAWYPnPNU=`). Reproduced on a second album
(Daft Punk, *Random Access Memories*, `media_metadata_tags: ['LOSSLESS']` → granted HIGH).

Why:
- Subscription is `{"type":"PREMIUM", ..., "highestSoundQuality":"HI_RES", "premiumAccess":true}` —
  so TIDAL claims hi-res entitlement.
- But the session is **non-PKCE**: `s.is_pkce == False`, `client_id fX2JxdmntZWK0ixT`
  (tidalapi's default device-OAuth client, `session.py:155`). `player.py:574` calls
  plain `tidalapi.Session()` and `session.py:665` logs in via device OAuth.
- `tidalapi/session.py:443-445`, verbatim: *"Login handler for PKCE based
  authentication. **This is the only way how to get access to HiRes (Up to 24-bit,
  192 kHz) FLAC files.**"*
- Corroborating: `Track.get_url()` hard-raises `URLNotAvailable` when `is_pkce`
  (`media.py:416-421`) — the two auth paths are mutually exclusive in tidalapi 0.8.11,
  and ticli uses the one that can't do FLAC.

**Implication:** `MPEG-DASH` (`application/dash+xml`, multi-segment, `DashInfo`,
`media.py:744-877`) is **never** reached on this session — every stream is single-URL
BTS. All the concatenation complexity is moot *today*, but would come back if ticli
ever adds PKCE login.

### 2.2 DRM

**No DRM on any reachable tier.** `encryptionType: "NONE"`, `keyId` absent,
`StreamManifest.is_encrypted` → False (`media.py:731-733`) on LOW, HIGH, LOSSLESS and
HI_RES_LOSSLESS. `ffprobe` decodes the URL directly; a plain `requests.get` produced a
byte-exact 7,931,562-byte file that `ffprobe`s clean. Nothing needs decrypting.
(`media.py:659` has a `# TODO: Handle encryption key` on the MPD path — so encrypted
MPD streams may exist in TIDAL generally; none were observed here.)

### 2.3 Honest extension per tier

`.m4a` for LOW and HIGH (AAC-LC in MP4; `ffprobe`: `format_name=mov,mp4,m4a,...`,
`codec_name=aac`, `profile=LC`). `.flac` would be correct for a real LOSSLESS stream —
unreachable here. `tidalapi` already computes this for you:
`StreamManifest.file_extension` (`media.py:708-729`) returned `.m4a`. **Use that field
rather than hardcoding** — it will stay correct if PKCE/FLAC is ever added.

### 2.4 Doing it with no new dependencies — and a bug found

**No ffmpeg required.** Because BTS gives one contiguous URL with `Accept-Ranges: bytes`,
the whole download is `requests.get(url, stream=True)` → write bytes. `requests` is
already a hard `tidalapi` dependency. Measured: **7,931,562 bytes in 0.38 s**, byte-count
identical to `Content-Length`. This is *better* than remuxing — `ffmpeg -c copy` produced
7,931,543 bytes (19 bytes different, rewritten container).

So: **direct GET is the primary path and needs nothing new.** ffmpeg is needed only for
tagging (§2.5). It is present here (`/opt/homebrew/bin/ffmpeg`, version 8.1.2) alongside
`ffplay` and `mpv`. Relying on it is *not* safe in general: mpv is first in
`AUDIO_PLAYERS` (`player.py:66`), so an mpv-only user may have no ffmpeg at all.
Fallback when ffmpeg is absent: **still download the file** (untagged but fully playable),
and note that most players fall back to filename/folder. Never let a missing ffmpeg
block the download.

> **Bug found — the FULL audio cache has never worked.**
> `player.py:284-296` writes the kept file as `f"{track_id}.audio" + ".part"` and runs
> `ffmpeg -y -i url -c copy <that path>`. ffmpeg picks its muxer from the extension:
> ```
> $ ffmpeg -y -i "$URL" -c copy /tmp/12345.audio.part
> Unable to choose an output format for '/tmp/12345.audio.part'; use a standard
> extension for the filename or specify the format manually.
> ```
> Zero bytes written, every time. The non-persistent path is equally broken — it writes
> `ticli-cache-{pid}.flac` (`player.py:289`) and the stream is AAC:
> ```
> $ ffmpeg -y -i "$URL" -c copy /tmp/tc_test.flac
> [flac] Invalid audio stream. Exactly one FLAC audio stream is required.
> ```
> so ffplay's pause/resume-from-cache is also dead. And the whole cache block is inside
> the `else: # ffplay` branch (`player.py:277`), so on mpv — the default backend — FULL
> cache mode does literally nothing. Fixes: add `-f mp4` (verified working, 7,931,563
> bytes) or name by `StreamManifest.file_extension`; better, replace the ffmpeg
> subprocess with the direct `requests` GET, which is faster and dependency-free.

### 2.5 Metadata tagging

**The served file has no tags at all.** `ffprobe -show_format` on the raw CDN URL:

```
TAG:major_brand=isom
TAG:minor_version=512
TAG:compatible_brands=isomiso2mp41
TAG:encoder=Lavf61.7.100
```

No title, artist, album, track number, or cover art. Tagging is mandatory, not optional.

Everything needed is already on the `Track` (no extra requests):
`t.name`, `t.artist.name` / `t.artists`, `t.album.name`, `t.track_num`, `t.volume_num`,
`t.isrc` (`US2CT1010021`), `t.copyright` (`2010 Basement's Basement`).
Note `t.album.year` was `None` — use `album.release_date` instead.
Cover art: `t.album.image(1280)` → `https://resources.tidal.com/images/.../1280x1280.jpg`,
221 KB, one plain GET, no auth.

**Verified working ffmpeg one-liner** (tags + embedded cover, stream-copied, no re-encode):

```
ffmpeg -y -i in.m4a -i cover.jpg -map 0:a -map 1 -c copy \
  -disposition:v:0 attached_pic \
  -metadata title=... -metadata artist=... -metadata album=... \
  -metadata track="3/12" -metadata date=2023 out.m4a
```

`ffprobe` on the result: `TAG:title`, `TAG:artist`, `TAG:album`, `TAG:track=3/12`,
`TAG:date`, plus an `mjpeg` attached-pic stream. 8,152,945 bytes.

Stdlib-only alternatives:
- **FLAC** — easy. A `VORBIS_COMMENT` metadata block is trivial to synthesise with
  `struct`. Irrelevant while no FLAC is reachable.
- **MP4/M4A** — hard. Requires writing `moov/udta/meta/ilst` atoms and fixing up parent
  box sizes. Doable in ~150 lines but it is real binary surgery on the user's only copy
  of the file. **Not recommended.** Use ffmpeg when present, ship untagged when not.

---

## 3. Play-count-tiered eviction

### 3.1 Fit against the current cache

Today's eviction (`utils/cache.py:312-370`) is pure LRU by `st_atime`: it lists
`audio_dir()`, sorts `(atime, size, path)`, unlinks until under budget. Two structural
problems for the proposed design:

- **`atime` is not reliable.** macOS and most Linux mounts default to `relatime`, so
  `atime` updates at most once a day. The current LRU is already coarser than it looks.
- **Filesystem metadata can't hold a play count.** The count has to be stored by ticli.

The change is small and fits the existing shape. `MetadataCache._load()` already filters
to entries whose `data` is a `list` (`cache.py:200-203`), so a differently-shaped key is
silently dropped by the current reader — meaning a new `"plays"` entry can be added
without a `CACHE_VERSION` bump breaking old readers, but *will* be dropped by them.
Cleanest: store `{track_id: {"plays": n, "last": ts, "pinned": bool}}` under a new
top-level key in the same `metadata.json`, and bump `CACHE_VERSION` to 2.

Keeping it in the same file is right: it inherits the existing atomic
write-tmp-then-`os.replace` (`cache.py:216-224`) and the whole-dict-replacement
no-locks invariant (`cache.py:166-170`). A separate file would need its own atomicity
and its own corruption handling for no benefit.

Sort key becomes `(plays, last_played)` instead of `(atime,)` — a one-line change to
`cache.py:335`. The play increment goes where the track is known to have actually
played: the `dead_polls >= 2` auto-advance in `_monitor_playback` (`player.py:1113`),
which is already an event, not a timer. That satisfies "no new polling loops".

### 3.2 Are the IDs stable?

**Playlist IDs: yes, they are literally GUIDs.** Probed live:
`4d47b518-b5b1-47a0-91e5-43bb16607bf4` (`<3`),
`d737efe2-b05f-473f-ab5c-f4656bc333a7` (`<3 but auto`), etc.
`cache.py:141` already stores `str(playlist.id)`.

**Track IDs: integers, not GUIDs** (`media.py:183 id: int`; probed: `246909121`,
`20115558`). They are stable in normal use and are already ticli's cache filename key
(`player.py:910` passes `real.id`; `cache.py` names files `{id}.audio`). The known
durability gap: when a label re-delivers a release, TIDAL mints a *new* track ID and the
old one 404s. `Track` also carries **`isrc`** (probed: `US2CT1010021`), which is the
industry-stable identifier. Recommendation: key the file by `id` (cheap, unchanged) but
*also* store `isrc` in the play-count record, so a future "my downloads survived a
re-delivery" fix is possible without a migration.

### 3.3 Failure modes worth flagging

1. **The stated one is real.** A deliberately downloaded track with 1 play sorts below
   an auto-cached track with 2 plays and gets evicted first. This is the exact opposite
   of intent. **Downloads must be exempt from eviction entirely, not merely ranked
   higher.** The owner's framing ("saved songs folder") already implies two tiers;
   make that explicit — downloads are user-owned (like config), cache is machine-owned
   (like the current cache dir). Only the cache tier gets a budget and an evictor.
2. **`cache_budget_mb` becomes ambiguous.** If downloads live under the cache budget,
   a full download library permanently starves the cache. Give downloads no budget and
   show their size in settings as information, not as a limit.
3. **Counts grow unboundedly** — in practice harmless (an int per track, thousands of
   tracks = a few hundred KB of JSON), but the *ranking* degrades: a track played 400
   times in 2024 and never since outranks everything forever. Cheap mitigation: on each
   increment, if the record is older than N months, halve the count first (exponential
   decay, zero extra bookkeeping, computed at write time — no timer).
4. **Orphan records.** Play counts for tracks whose audio was evicted keep accumulating.
   Fine — that's a feature (a re-downloaded staple keeps its rank). But bound the map,
   e.g. drop records with `plays == 1` older than MAX_AGE during `enforce_budget`, which
   already runs after writes.

---

## 4. Durability against manual deletion

### 4.1 What happens today

Reading the actual paths:

- **Start of playback** — `AudioPlayer.play_url` (`player.py:254-259`) does
  `kept = self._cached_audio_path(cache_key)` then
  `have_kept = bool(kept) and os.path.exists(kept)`, and picks `source = kept if
  have_kept else url`. So a **deleted file is correctly detected and the network URL is
  used instead.** No crash, silent and correct recovery. This is verify-on-read and it
  already exists.
- **The race** — if the file is deleted *between* that `os.path.exists` and the player
  actually opening it, mpv exits immediately (verified: `mpv --no-video
  /tmp/definitely-missing.m4a` → exit 2). `_monitor_playback` then sees
  `not self.audio.is_playing`, counts two dead polls (`player.py:1104-1116`) and
  **auto-advances to the next track.** So the symptom is: the song is silently skipped.
  No crash, no error message, no retry from the URL. Window is milliseconds; low
  severity but it is the one real hole.
- **Resume (ffplay)** — `player.py:363` re-checks `os.path.exists(self._cache_file)` and
  falls back to `self._current_url` (`:366-376`) if gone. Correct. The signed URL is
  valid for 1 h (§1.2), so the fallback works for the length of any realistic pause.
- **`stop()`** — `player.py:505` guards the unlink with `not self._cache_persistent`, so
  it won't try to delete a file it didn't create. `os.unlink` is inside `try/except
  OSError` anyway.
- **`enforce_budget`** — `cache.py:326-344` wraps every `stat()` and `unlink()` in
  `except OSError: continue`. A file vanishing mid-sweep is handled.
- **`_dir_size`** — `cache.py:150-161`, same. Fine.

**Summary: the current code already recovers from manual deletion at every point that
matters. Nothing crashes. The only defect is the millisecond race, which manifests as a
silently skipped track.**

The *index* has no audio-file bookkeeping at all — audio is discovered by listing the
directory, never recorded in `metadata.json` — so there is nothing to go stale. That is
a genuinely good design and the play-count work should not undo it: keep the play-count
map advisory (a ranking hint), never authoritative about what exists on disk.

### 4.2 Cheapest correct approach

**Verify-on-read, which is what's already there.** No reconcile pass, no timer. Two
additions worth making:

1. In the auto-advance branch (`player.py:1113`), if the just-started source was a local
   file and it no longer exists, re-start from the URL instead of skipping. ~3 lines,
   closes the race, costs nothing when the file is present.
2. If a download index is added, treat a missing file as "not downloaded" on every read
   rather than trying to keep the index truthful. Same contract as the metadata cache:
   *the index is a first paint, never an answer.*

---

## 5. Blockers and reshapers

- **Rate limiting is the real constraint** (§1.3). Concretely: cap concurrency at 2
  in-flight `playbackinfo` calls, add ~200 ms spacing, catch
  `tidalapi.exceptions.TooManyRequests` and back off ≥90 s. A batch download of 100
  tracks should be a slow, throttled, cancellable background job on a daemon thread —
  not a fan-out. The consequence of getting it wrong is not a failed download, it is the
  user's music stopping.
- **Token expiry during a long job.** The stored access token was already past its
  `expiry_time` (2026-07-12) and one probe run died mid-way with a 401 until
  `token_refresh()` was called. A 100-track download must tolerate a mid-run refresh.
  Note `RequestSession.basic_request` (`request.py:107-121`) auto-refreshes *only* when
  the body says `"The token has expired."` — it will **not** auto-refresh a 4006.
- **Signed URLs expire in 1 h.** Fetch each track's URL immediately before downloading
  it, not all up front.
- **Terms of service:** I found **no ToS text, license header, or rate-limit
  documentation in the installed `tidalapi` package** — no `LICENSE`/`README` shipped,
  and no ToS strings in the source. The API returns no ToS-related headers. I did not
  fetch tidal.com's terms. Reporting factually: **nothing in the API or the installed
  library states a retention constraint one way or the other.** The `offlineGracePeriod:
  30` field in the subscription response and the (401-gated) `playbackmode=OFFLINE`
  parameter show TIDAL has a first-party offline concept that this client is not
  entitled to use — ticli's downloads would be using `playbackmode=STREAM` output for
  retention, which is a different thing.
- **Download folder location: no conflict, and it's the right call.** `utils/cache.py`
  is explicit that the cache dir is machine-owned and disposable ("Deleting the whole
  cache directory at any moment is always safe"). Downloads are the opposite. Put them
  somewhere user-visible and *outside* `CACHE_DIR` — `~/Music/Ticli` by default, shown
  and editable in settings. Cross-platform: `~/Music` exists on macOS and Windows;
  on Linux prefer `XDG_MUSIC_DIR` from `~/.config/user-dirs.dirs` with `~/Music` as
  fallback. `cache.py:55-65` already has the shape to copy.

---

## Recommendation — build order

1. **Fix the existing cache bug first** (§2.4). Replace the `ffmpeg -c copy` subprocess
   with a `requests` streaming GET writing `{id}{StreamManifest.file_extension}`, and
   move it out of the ffplay-only branch so mpv users get it too. This is a prerequisite
   for everything else, it removes an ffmpeg dependency rather than adding one, and it
   makes FULL cache mode work for the first time.
2. **Size estimates from `duration × nominal bitrate`, all four tiers, zero requests**
   (§1.4, §1.5). Duration is already in the cache index. Label with `~`. Ship this
   before any download UI — it is the cheapest, safest piece.
3. **Single-track download**: get URL → GET to `~/Music/Ticli/<Artist>/<Album>/NN Title.m4a`
   → if `shutil.which("ffmpeg")`, retag+cover in place; else leave untagged. Never fail
   the download because ffmpeg is missing.
4. **Batch/playlist download**: same, on one daemon thread, ≤2 concurrent, 200 ms spacing,
   `TooManyRequests` → 90 s backoff, cancellable, progress from the existing 0.5 s tick.
5. **Play counts**: increment at the auto-advance point, store in `metadata.json` under
   a new key with `CACHE_VERSION = 2`, sort eviction by `(plays, last_played)`.
   **Exempt the download folder from eviction entirely** — two tiers, one budget, and
   the budget only covers the cache.
6. **Close the deletion race** (§4.2 item 1). 3 lines.
7. **Surface the quality truth** (§2.1). Whatever else happens, the settings screen
   should not offer LOSSLESS/HIRES as if they work. Cheapest honest fix: read
   `Stream.audio_quality` (the *granted* tier) on the first track of a session and badge
   that, instead of badging what was requested.

---

## What I could not verify, and why

- **Whether PKCE login would unlock FLAC on this account.** `login_pkce` requires the
  owner to open a browser and paste a redirect URL back (`session.py:443-476`); he is not
  available and I did not attempt to obtain credentials any other way. The subscription
  says `highestSoundQuality: HI_RES` and `premiumAccess: true`, and tidalapi's own
  docstring says PKCE is the only route to FLAC — so it is **likely** that PKCE would
  work. Not proven. To verify: one interactive `Session().login_pkce()` run, then re-run
  the §2.1 tier table. **This is worth 5 minutes of the owner's time before any of this
  is built**, because a "yes" changes the file format, the extension, the size-estimate
  error bars, and reintroduces MPEG-DASH segment concatenation.
- **Whether MPEG-DASH streams from TIDAL are ever DRM-encrypted.** No MPD manifest was
  reachable on this session, so the `encryptionType` on that path is untested.
  `media.py:659-661` hardcodes `encryption_type = "NONE"` for MPD with a `# TODO: Handle
  encryption key` — so tidalapi itself may mis-report this. Only testable with a session
  that receives MPD.
- **The exact shape of the rate limiter** (window length, whether it is per-session,
  per-IP or per-account, whether concurrency or raw count is what trips it). I measured
  one hard data point — 53 sequential requests in 2.8 s → 429 → 401/4006 → recovered
  between 30 s and 60 s — and stopped, rather than repeatedly locking the owner's account
  out of playback. The safe-rate recommendation in §5 is deliberately conservative
  relative to what was measured, not tuned to it.
- **Nominal bitrate for LOSSLESS/HIRES tiers.** The ≈1000 kbps figure for 16/44.1 FLAC is
  a standard compression-ratio assumption, **not** measured here — no FLAC stream was
  obtainable. FLAC is VBR and real error will be materially worse than the ≈1 % measured
  for AAC. If PKCE unlocks FLAC, re-run the §1.4 calibration before trusting a
  lossless size estimate.
- **Windows behaviour** of any of this. The app can't start on Windows today
  (`player.py:2401-2403` imports `tty`/`termios`); pre-existing, unrelated, unchanged.
