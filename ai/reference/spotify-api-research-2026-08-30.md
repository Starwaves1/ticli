# Spotify API research — 2026-08-30

Expert assessment of Spotify's API surface for a ticli-style port: terminal
player, OAuth login, direct stream URLs, in-process audio, downloads to disk.
Hypothesis under test: **"TIDAL's API is unusually friendly to third-party
usage; Spotify may not be."**

> **Methodology / source-access caveat.** This environment's egress proxy
> blocks every Spotify-owned domain (`developer.spotify.com`,
> `newsroom.spotify.com`, `community.spotify.com`, `spotify.com`) and
> `web.archive.org`. Spotify's own pages were therefore read via
> search-engine snapshots of the named primary URLs (multiple independent
> captures cross-checked per claim), not fetched directly. Every such claim
> cites the primary URL that owns it; quotation marks around Spotify-doc text
> mean "as captured by the search index", not "verified byte-for-byte against
> the live page". GitHub sources (librespot, terminal clients) **were**
> fetched directly and their quotes are verbatim. Claims that could not be
> pinned to a primary source are marked **[not verified]** and collected in
> the last two sections. A maintainer with normal network access can spot-check
> any quote in minutes; the URLs are all here.

---

## Verdict

**The hypothesis is confirmed, with one honest nuance.**

There is **no sanctioned path to raw audio bytes** on Spotify outside a
DRM-capable browser or the official apps — not for a terminal app, not for
anyone, and this has been deliberate policy since libspotify was sunset in
2022 with no native replacement. Everything else about the platform has been
tightening monotonically since November 2024: catalog endpoints cut, extended
access restricted to registered businesses with 250k+ MAU (May 2025),
development mode capped at 5 users with the owner required to hold Premium
(Feb–Mar 2026), refresh tokens now expiring 6 months after authorization
(June 2026). The only way third-party Spotify players actually make sound is
**librespot**, a reverse-engineered client protocol whose own README says
using it "is probably forbidden by them", which tops out at Ogg Vorbis
320 kbps with no route to Spotify's 2025 lossless tier, and which Spotify has
broken repeatedly (password auth killed July 2024; OAuth redirect rules
2025).

The nuance: TIDAL's *official* third-party API also does not hand out audio
(its Playback API issues signed manifests; audio is meant to flow through
TIDAL's Player SDK). What ticli enjoys is TIDAL's **de facto** friendliness —
tolerated well-known client IDs, DRM-free single-URL FLAC/AAC streams, no
crackdowns. Spotify's de facto posture is the opposite: DRM on every official
surface, an actively shrinking API, and a ToS that names ticli's flagship
features (stream capture, local copies, replacing the client experience) as
violations.

**Feature-by-feature for a Spotify ticli:**

| ticli feature | Spotify status |
|---|---|
| OAuth login usable by a hobby app | **(b) sanctioned but crippled** — must register own app; owner needs Premium; ≤5 allowlisted users; re-login every 6 months |
| Search / catalog metadata | **(b) sanctioned but crippled** — works, but new dev-mode apps: search limit max 10, no artist top-tracks, no browse, no recommendations/audio-features, daily quota |
| Playlist management, user library | **(a) fully sanctioned** (within dev-mode caps) |
| Remote-control playback on an official Spotify device | **(a) sanctioned** — Player API + Connect; Premium required; audio never enters ticli |
| In-process audio playback in the terminal | **(c) only via librespot, against ToS** — Ogg Vorbis ≤320 kbps, Premium required |
| Direct stream URLs / FLAC | **(d) impossible** — no official surface returns them; librespot gets encrypted Vorbis, not FLAC |
| Lossless / hi-res | **(d) impossible** — official apps + certified devices only; not on Connect third parties, not in librespot |
| Downloads to disk | **(d) impossible officially and (c) explicitly prohibited** — Developer Terms ban facilitating permanent copies; librespot could dump decrypted Vorbis but that is textbook stream-ripping |

Recommendation implied by the evidence: a Spotify backend for ticli is either
a **Connect remote controller** (legal-ish, but ticli stops being a player —
audio comes out of some other Spotify client) or a **librespot frontend**
(real audio, ToS violation, 320 kbps ceiling, breakage risk, account risk).
Neither reproduces what ticli is on TIDAL.

---

## 1. What the Web API actually offers

Catalog and account plumbing is real and decent — it is the audio that is
walled off.

- **Endpoint families** (Web API reference,
  https://developer.spotify.com/documentation/web-api): search; tracks,
  albums, artists, playlists metadata; user library (save/remove/check);
  playlist create/add/remove/reorder; user profile; player (see §2a);
  markets; categories/browse (now restricted, see §3–4).
- **OAuth flows** (https://developer.spotify.com/documentation/web-api/concepts/authorization):
  Authorization Code; **Authorization Code + PKCE** (the documented choice
  for apps that cannot keep a secret — i.e. a CLI); Client Credentials
  (no user context, so useless for a player); Implicit Grant — **removed
  27 Nov 2025** (https://developer.spotify.com/blog/2025-10-14-reminder-oauth-migration-27-nov-2025).
- **Redirect URIs** (https://developer.spotify.com/documentation/web-api/concepts/redirect_uri,
  https://developer.spotify.com/blog/2025-02-12-increasing-the-security-requirements-for-integrating-with-spotify):
  HTTPS required, except loopback **IP literals** (`http://127.0.0.1:port`,
  `http://[::1]:port`), which remain allowed; the hostname `localhost` is
  banned. So a CLI loopback-listener flow (what spotify-player does, and what
  ticli's PKCE flow resembles) is still possible.
- **Token lifetimes**: access tokens 3600 s
  (https://developer.spotify.com/documentation/web-api/concepts/access-token);
  refresh tokens — as of the 18 June 2026 announcement, **refresh tokens for
  user-authorized apps expire 6 months after the original authorization, and
  refreshing does not reset the clock**
  (https://developer.spotify.com/blog/2026-06-18-refresh-token-expiration).
  Consequence for a ticli-style stored session: the user must interactively
  re-login at least twice a year. TIDAL's stored session in
  `~/.config/ticli/session.json` has no such forced ceremony.
- **Scopes a player needs**
  (https://developer.spotify.com/documentation/web-api/concepts/scopes):
  `user-read-playback-state`, `user-modify-playback-state`,
  `user-read-currently-playing` for Connect control; `streaming` (plus
  `user-read-email`, `user-read-private`) for the Web Playback SDK;
  `playlist-read-private`, `playlist-modify-public/private`,
  `user-library-read/modify` for library/playlists.

There is no field anywhere in the Web API that is a stream URL, a manifest,
or an audio file. `preview_url` (30 s clip) was the closest thing and is
being removed (§2e).

## 2. Audio access — the core question

**Verified conclusion: no official API surface returns full audio streams or
downloadable audio to a third-party process. All official playback surfaces
are DRM-gated or delegate playback to an official Spotify client.**

### 2a. Web API Player endpoints (Connect control)

The Player API is a **remote control, not a stream source**: "tell Spotify
what audio to play and where and how to play it" — on an existing device
running an official client or licensed hardware (Spotify Engineering,
https://engineering.atspotify.com/2022/4/spotifys-player-api). `GET
/me/player` reads state (`user-read-playback-state`); `PUT /me/player`,
`/me/player/play`, `/pause`, `/next`, `/seek`, `/queue`, transfer-playback
all require `user-modify-playback-state`. Playback control returns **403
"Player command failed: Premium required"** on free accounts
(https://github.com/spotify/web-api/issues/1042; community threads, e.g.
https://community.spotify.com/t5/Spotify-for-Developers/API-403-Studen-Premium-Premium-required/td-p/5064266).
Nothing in these endpoints carries audio; if no official client is running
anywhere, there is no device and nothing plays.

### 2b. Web Playback SDK

https://developer.spotify.com/documentation/web-playback-sdk — "a client-side
only JavaScript library designed to create a local Spotify Connect device in
your browser". Requirements per the docs and getting-started page
(https://developer.spotify.com/documentation/web-playback-sdk/tutorials/getting-started):
Spotify **Premium** (full Premium; Premium Mini / mobile-only plans excluded
— https://community.spotify.com/t5/Spotify-for-Developers/Web-Playback-SDK-not-working-with-Spotify-Lite-Premium-Mini/td-p/7269497),
the `streaming` scope, and a **browser with EME/Widevine DRM enabled** — the
canonical failure mode is "Playback of protected content is not enabled"
(https://support.spotify.com/us/article/enable-the-spotify-web-player/;
https://community.spotify.com/t5/Spotify-for-Developers/DRM-might-not-be-available-from-unsecure-contexts/td-p/5972536).
Supported platforms are enumerated as desktop/mobile **browsers** (Chrome,
Firefox, Safari, Edge). The docs offer no non-browser runtime; audio is
delivered encrypted and decrypted inside the browser's CDM. Running this
"in a terminal" means embedding a Widevine-capable browser engine —
technically a headless-Chromium contraption, and the audio still never
reaches your process as bytes you may keep. **[Not verified: any Spotify doc
explicitly forbidding headless use — the docs simply define a browser-only
support matrix.]**

### 2c. iOS / Android SDKs

Both are **App Remote** SDKs: they authenticate and then control the
official Spotify app installed on the same device, which does all playback
(https://developer.spotify.com/documentation/android — "control playback in
the Spotify app"; https://developer.spotify.com/documentation/ios — App
Remote "connect[s] with the Spotify app and let[s] you control it while all
the heavy lifting of playback is offloaded to the Spotify app itself";
https://spotify.github.io/android-sdk/app-remote-lib/). The older mobile
*streaming* SDKs were deprecated and shut down alongside libspotify
(https://developer.spotify.com/blog/2022-07-15-mobile-streaming-sdks-update).
Irrelevant to a terminal app except as further proof of the pattern: official
surfaces control official clients; they do not emit audio.

### 2d. libspotify — the door that closed

libspotify was the C library that once let third-party desktop/embedded apps
stream real audio. Deprecated in **2015**; access disabled **16 May 2022**
(https://developer.spotify.com/blog/2022-04-12-libspotify-sunset). Spotify's
stated migration paths in that sunset post: Web Playback SDK (browser,
Premium), Spotify Connect certified-hardware program (eSDK, commercial
licensing), or metadata endpoints. I.e. **no equivalent replacement** for a
native third-party streaming client — a gap the community has noted ever
since (e.g. https://community.spotify.com/t5/Spotify-for-Developers/Sunset-of-libspotify-Please-reconsider/td-p/5383105).
librespot's README positions itself explicitly as "an alternative to the
official and now deprecated closed-source libspotify"
(https://github.com/librespot-org/librespot, fetched directly).

### 2e. Preview clips

The 30-second `preview_url` was the only sanctioned audio of any kind. The
27 Nov 2024 announcement removed "30-second preview URLs, in multi-get
responses" for apps without pre-existing extended access
(https://developer.spotify.com/blog/2024-11-27-changes-to-the-web-api).
Community reports show `preview_url` returning `null` for new apps in
single-track responses too
(https://community.spotify.com/t5/Spotify-for-Developers/HTTP-API-Missing-preview-url-in-single-track-GET/td-p/6656678;
https://community.spotify.com/t5/Spotify-for-Developers/Preview-URLs-Deprecated/td-p/6791368),
and an ecosystem of scrape-the-embed-player workarounds has grown up
(https://github.com/rexdotsh/spotify-preview-url-workaround). So even 30 s of
audio is now grey-market for a new app.

## 3. The November 27, 2024 deprecations

Announcement: **"Introducing some changes to our Web API"**, Spotify for
Developers blog, 27 Nov 2024 —
https://developer.spotify.com/blog/2024-11-27-changes-to-the-web-api.

Endpoints/fields cut for **new Web API use cases and apps in development
mode** (existing extended-quota apps grandfathered):

1. Related Artists
2. Recommendations
3. Audio Features
4. Audio Analysis
5. Get Featured Playlists
6. Get Category's Playlists
7. 30-second preview URLs in multi-get responses
8. Algorithmic and Spotify-owned editorial playlists

Calls from non-grandfathered apps return 403/404-class errors
(https://community.spotify.com/t5/Spotify-for-Developers/Changes-to-Web-API/td-p/6540414).
Stated reason, from the blog and a spokesperson quoted by Music Ally: "As
part of our ongoing work to address the security challenges that many
companies navigate today, we're making changes to our public APIs" — aimed at
misuse including data scraping
(https://musically.com/2024/11/28/spotify-removes-features-from-web-api-citing-security-issues/;
https://techcrunch.com/2024/11/27/spotify-cuts-developer-access-to-several-of-its-recommendation-features/).
No replacements were offered; the widely-suspected motive (denying
audio-feature/recommendation data to ML training) is commentary, not
Spotify's words. Grandfathering wording per the blog: applications with
existing extended-mode access are "unaffected by this change".

## 4. What changed after — 2025 and 2026 (it got worse)

The 2025 reports the owner heard about were true, and 2026 went further:

- **12 Feb 2025 — OAuth security tightening**
  (https://developer.spotify.com/blog/2025-02-12-increasing-the-security-requirements-for-integrating-with-spotify):
  implicit grant deprecated; HTTP redirect URIs banned except loopback IP
  literals; `localhost` banned. Enforced for new clients from 9 Apr 2025;
  hard cutoff for all on **27 Nov 2025**
  (https://developer.spotify.com/blog/2025-10-14-reminder-oauth-migration-27-nov-2025).
- **15 Apr 2025 — extended access restricted to businesses**
  (https://developer.spotify.com/blog/2025-04-15-updating-the-criteria-for-web-api-extended-access,
  effective 15 May 2025): extension requests accepted **only from registered
  business entities with a launched service and ≥250,000 MAU**; individuals
  no longer eligible. Spotify's justification includes that "over 95% of
  applications fell short of basic security, privacy, and licensing
  standards". Existing compliant extended-access holders unaffected.
  Community thread: https://community.spotify.com/t5/Spotify-for-Developers/Updating-the-Criteria-for-Web-API-Extended-Access/td-p/6920661.
- **6 Feb 2026 — "Update on Developer Access and Platform Security"**
  (https://developer.spotify.com/blog/2026-02-06-update-on-developer-access-and-platform-security):
  citing "advances in automation and AI" changing the risk profile. From
  **11 Feb 2026** for new apps, extending to existing integrations
  **9 Mar 2026**: development-mode apps require the **owner to hold Spotify
  Premium** (app stops working if it lapses); **1 Client ID per developer**;
  **5 authorized users per app** (down from 25 — TechCrunch:
  https://techcrunch.com/2026/02/06/spotify-changes-developer-mode-api-to-require-premium-accounts-limits-test-users/);
  and a reduced endpoint surface.
- **Feb 2026 dev-mode endpoint cuts** (migration guide,
  https://developer.spotify.com/documentation/web-api/tutorials/february-2026-migration-guide;
  changelog https://developer.spotify.com/documentation/web-api/references/changes/february-2026):
  removed browse endpoints, **Get Artist's Top Tracks**, other users'
  profiles and playlists, available-markets, batch multi-get endpoints;
  **search `limit` max 50→10, default 20→5**; response fields stripped
  (`popularity`, `available_markets`, `followers`; user `country`, `email`,
  `product`); playlist `/tracks` renamed `/items`; per-type library
  endpoints unified into `PUT/DELETE /me/library`. Third-party corroboration:
  https://github.com/ramsayleung/rspotify/issues/550,
  https://github.com/spotDL/spotify-downloader/issues/2617,
  https://github.com/bjarneo/cliamp/issues/54.
- **Mar 2026 — partial retreat**: after community backlash, Spotify
  **postponed the endpoint removals for *existing* dev-mode integrations**,
  but the Premium requirement, 5-user cap, and client-ID limit took effect
  9 Mar 2026 as planned; the March changelog also reverted the removal of
  `external_ids` on tracks/albums
  (https://developer.spotify.com/documentation/web-api/references/changes/march-2026;
  https://community.spotify.com/t5/Spotify-for-Developers/February-2026-Spotify-for-Developers-update-thread/td-p/7330564).
  New apps created after 11 Feb 2026 get the restricted surface regardless.
- **18 Jun 2026 — refresh-token expiration** (§1;
  https://developer.spotify.com/blog/2026-06-18-refresh-token-expiration).
- **23 Jul 2026 — quota restructure**
  (https://developer.spotify.com/blog/2026-07-23-web-api-quota-updates):
  client-ID limit raised back to 25 per developer account, but **quota is now
  counted per developer account** across all Client IDs, with structured
  `429 {"reason": "QUOTA_EXCEEDED"}` responses. Development mode now has a
  **daily/aggregate quota mechanism separate from the 30-second rate limit**;
  the actual numbers are not published. Community pain point: quota bites
  even for playback-control polling
  (https://community.spotify.com/t5/Spotify-for-Developers/Reconsider-daily-quota-limits-for-playback-endpoints/td-p/7371094).

## 5. Development mode vs extended quota mode; rate limits

https://developer.spotify.com/documentation/web-api/concepts/quota-modes:

- **Development mode** (every new app): for building and "accessing or
  managing data in a single Spotify account". **Up to 5 authenticated users**,
  each explicitly allowlisted by name + email in the dashboard's User
  Management tab (was 25 until Feb 2026). Owner must hold Premium (Feb 2026).
  Lower rate limit + account-level quota.
- **Extended quota mode**: unlimited users, higher rate limit. Since
  15 May 2025 the application review is closed to individuals — registered
  business + launched service + **250k MAU minimum** (§4). A hobbyist CLI
  app is not merely "likely rejected"; it is **categorically ineligible to
  apply**.
- **Rate limits**
  (https://developer.spotify.com/documentation/web-api/concepts/rate-limits):
  computed over a **rolling 30-second window**; exact numbers deliberately
  unpublished; development mode explicitly lower than extended; 429 responses
  normally carry `Retry-After` in seconds. Community measurements suggest
  roughly ~180 requests/min for dev mode **[not verified — unofficial
  community figure]**
  (https://community.spotify.com/t5/Spotify-for-Developers/Web-API-ratelimit/td-p/5330410).

For a personal-use ticli this is survivable (owner + ≤4 friends, throttled
requests) — but every user must be hand-allowlisted in a Spotify dashboard,
the owner's Premium lapse bricks the app for everyone, and there is no growth
path whatsoever. Contrast: ticli on TIDAL has no user cap concept at all.

## 6. Developer Policy / Terms — the clauses that hit ticli

Primary texts: Developer Terms https://developer.spotify.com/terms,
Developer Policy https://developer.spotify.com/policy, plus
https://developer.spotify.com/compliance-tips. Clauses relevant to ticli
(wording as captured; see methodology note):

- **Stream ripping / permanent copies** (Developer Terms): developers must
  not facilitate "stream ripping" or functionality that helps users "capture
  or otherwise make permanent copies" of Spotify Content, and agree to
  **cooperate with Spotify in pursuing violators**. ticli's download feature
  is this clause's exact target.
- **Caching** (Developer Terms/Policy): no local caching of Spotify Content
  except **temporary caching of metadata and cover art** for performance, and
  "Conditional Downloads" for Premium subscribers (a licensed-hardware/eSDK
  concept, not a Web API capability). ticli's audio cache tier would violate
  this; its metadata/artwork cache would not.
- **Core-experience clause** (Developer Policy §III): "Do not build products
  or services that mimic, or replicate or attempt to replace a core user
  experience of Spotify... without our prior written permission." A terminal
  client that replaces the Spotify player *is* a replacement of the core
  experience. (Community discussions of this clause:
  https://community.spotify.com/t5/Spotify-for-Developers/Does-my-idea-violate-the-prohibited-applications-part-of-the/td-p/5705226.)
- **No circumvention** of geographic or other access restrictions; no
  facilitating unauthorized access to Spotify Content (Developer Terms).
  DRM circumvention additionally implicates anti-circumvention law (DMCA
  §1201 et al.) — legal analysis out of scope here.
- **No analysis/derived data, no AI ingestion** (Developer Policy): no
  derived listenership metrics or user profiling; no using the platform to
  train ML/AI models (post-Nov-2024 wording).
- **No standalone metadata service**; metadata/cover art must link back to
  Spotify (Developer Policy).
- **Enforcement in practice**: API access revocation and app removal
  (unilateral, per the Terms); on the user side, Spotify has suspended and
  threatened termination of accounts caught downloading via unauthorized
  third-party apps (2021 reporting:
  https://www.digitalmusicnews.com/2021/07/19/spotify-suspending-users-downloading-songs/
  — secondary source). librespot-family projects run for years without mass
  bans **[not verified — absence of bans is folklore, not documented
  policy]**, but the exposure is real and acknowledged by librespot itself
  (§7).

## 7. The unofficial path — librespot

https://github.com/librespot-org/librespot (README and CHANGELOG fetched
directly; quotes verbatim).

- **What it is**: "an open source client library for Spotify... to control
  and play music via various backends, and to act as a Spotify Connect
  receiver. It is an alternative to the official and now deprecated
  closed-source libspotify." Reverse-engineered client protocol (AP TCP
  connection, Mercury/dealer messaging, CDN audio fetch + decryption —
  see `docs/` in the repo). It powers **spotifyd, raspotify, ncspot,
  librespot-java, Spot**, and (for streaming) **spotify-player** (README
  "Related Projects" + §9).
- **What it can do**: real in-process audio decode and playback, Connect
  receiver registration, many audio backends (Rodio/ALSA/PulseAudio/pipe/
  subprocess...). Bitrate selectable up to **320 kbps** (`-b 320`; Ogg
  Vorbis — the lossless discussion below confirms "Connect endpoints...
  still receive audio via the legacy Ogg Vorbis 320 kbps stream").
- **Premium required**, by project policy as well as protocol reality:
  "librespot only works with Spotify Premium. This will remain the case."
- **Its own ToS warning, verbatim**: "**Disclaimer: Using this code to
  connect to Spotify's API is probably forbidden by them. Use at your own
  risk.**"
- **Auth history — Spotify has broken it repeatedly**:
  - End of July 2024: Spotify **disabled username/password authentication**
    platform-wide; librespot logins failed with "Bad credentials"
    (https://github.com/librespot-org/librespot/issues/1308; downstream:
    https://github.com/snapcast/snapcast/issues/1274,
    https://github.com/dtcooper/raspotify/issues/675). librespot 0.5.0
    (Oct 2024) shipped OAuth-based login; sessions now bootstrap from an
    OAuth access token exchanged for reusable stored credentials
    (https://github.com/librespot-org/librespot/issues/1501; CHANGELOG
    0.7.0's `OAuthClient`/`OAuthClientBuilder`).
  - Feb 2025: Spotify's OAuth redirect-URI/implicit-grant deprecations hit
    librespot too (https://github.com/librespot-org/librespot/issues/1465 —
    "Failure to take action will cause your application to stop working as
    expected"), with the 27 Nov 2025 enforcement date.
  - librespot sessions present **Spotify's own official client IDs** (e.g.
    keymaster/Android client-ID fixes in CHANGELOG 0.7.0; spotify-player
    README: librespot credentials are "presented under Spotify's official
    client ID"). This is impersonation of an official client — the load-bearing
    trick, and the reason it can vanish on Spotify's schedule, not yours.
- **Quality ceiling / lossless**: Spotify Lossless (Sept 2025, §8) is **not
  reachable** via librespot or any Connect-credential client: the FLAC
  pipeline exists only in official apps, and Connect endpoints — including
  all third-party streamers — still get the Vorbis 320 stream
  (https://github.com/librespot-org/librespot/issues/1582,
  https://github.com/librespot-org/librespot/issues/1583,
  https://github.com/librespot-org/librespot/discussions/1578;
  https://community.spotify.com/t5/Desktop-Linux/Lossless-playback-on-third-party-Connect-devices/td-p/7196409).
  Maintainers note FLAC groundwork exists in the decoder if Spotify ever
  serves it over the same path — i.e. blocked server-side, not client-side.

## 8. Lossless, officially

Spotify Lossless launched **10 Sept 2025**: up to **24-bit/44.1 kHz FLAC**,
included with Premium, rolling out to 50+ markets through Oct 2025
(https://newsroom.spotify.com/2025-09-10/lossless-listening-arrives-on-spotify-premium-with-a-richer-more-detailed-listening-experience/).
Delivery: official apps (desktop/mobile) and compatible/certified Spotify
Connect hardware; Bluetooth excluded for bandwidth reasons; What Hi-Fi
overview: https://www.whathifi.com/advice/spotify-hifi-quality-price-release-date-free-trial-and-latest-news.
No developer surface (Web API, Web Playback SDK, App Remote) mentions
lossless access, and third-party Connect receivers demonstrably still
receive Vorbis 320 (§7). Compare TIDAL-as-ticli-uses-it: FLAC up to 24/192
over a plain signed URL, `encryptionType: "NONE"`
(`ai/reference/download-research-2026-07-25.md` §2.1–2.2, plus the PKCE/FLAC
confirmation in `ai/DECISIONS.md`).

## 9. Existing terminal Spotify clients — the two possible architectures

All four notable clients decompose into exactly two mechanisms (all READMEs
fetched directly):

| Client | Audio mechanism | Web API usage | Premium |
|---|---|---|---|
| **spotify-tui** (https://github.com/Rigellute/spotify-tui) | none — Connect controller only; "uses the Web API... which doesn't handle streaming itself", needs an official client or spotifyd running | everything | yes, for playback control |
| **spotifyd** (https://github.com/Spotifyd/spotifyd) | librespot daemon; headless Connect receiver | none | yes |
| **ncspot** (https://github.com/hrkfdn/ncspot) | librespot in-process playback | own registered client ID (extended-quota, grandfathered) | yes ("only works with a Spotify premium account") |
| **spotify-player** (https://github.com/aome510/spotify-player) | librespot in-process playback + Connect | PKCE flow; **defaults to ncspot's client ID** | yes ("A Spotify Premium account is required") |

spotify-player's README is the clearest statement of the ecosystem's
position, and worth quoting (verbatim, fetched 2026-08-30): it defaults to
ncspot's client ID because "that client ID is registered in extended quota
mode and predates Spotify's November 2024 Web API changes", and warns "**You
almost certainly should not configure your own `client_id`.** Any application
you register today starts in Spotify's restricted default quota mode...
commonly leads to 429... and 403... and missing browse/personalized data."
Meanwhile its librespot session runs "under Spotify's official client ID".
I.e. the surviving open-source Spotify players run on **borrowed grandfathered
credentials plus impersonated official ones** — both revocable by Spotify at
any time, neither available legitimately to a new app like ticli.

**Implication**: a Spotify ticli would be (1) a Connect remote (audio
elsewhere), (2) a librespot frontend (audio in-process, against ToS,
320 kbps), or the hybrid both ncspot and spotify-player chose. There is no
third architecture; nobody has one.

## 10. Comparison table — Spotify vs TIDAL as ticli actually uses it

TIDAL column from this repo's live-probed research
(`ai/reference/download-research-2026-07-25.md`, `ai/DECISIONS.md`).

| Capability | TIDAL (as ticli uses it) | Spotify |
|---|---|---|
| Login for a hobby app | Device flow or PKCE with tidalapi's well-known client IDs; no app registration, no user cap; stored session refreshes indefinitely | Register own app; owner must hold Premium; ≤5 allowlisted users; PKCE with loopback redirect OK; forced re-auth every 6 months (refresh-token expiry) |
| Search / metadata | `search()` limit up to 300 per type; full track/album/artist objects | Works but dev-mode-crippled: search limit max 10 (new apps, Feb 2026), no top-tracks/browse/recommendations/audio-features, daily quota |
| Stream access | `playbackinfopostpaywall` → signed CDN URL, `encryptionType: "NONE"`, plain HTTP GET, ranges supported | **None.** No endpoint returns audio; official playback = DRM browser SDK or remote-controlling an official client |
| Max quality (3rd party) | FLAC up to 24/192 via PKCE session | Ogg Vorbis 320 kbps, only via librespot (against ToS); lossless unreachable |
| DRM | None observed on any reachable tier | Everywhere: Widevine/EME in browser; AES-encrypted CDN files in the client protocol |
| Downloads to disk | Trivial (`requests.get`), shipped in ticli; ToS silent in-API | Officially impossible; explicitly prohibited (anti-ripping + caching clauses); librespot dump = flagrant violation |
| Rate limiting | Aggressive on stream-URL endpoint (~50 burst → 429/4006, ~60–90 s), rest of API tolerant | 30 s rolling window (numbers unpublished, dev mode lower) + per-account daily quota with `QUOTA_EXCEEDED` |
| API trajectory | Stable; tolerated third-party client IDs for years | Shrinking every 6–12 months since Nov 2024; hobbyist extension path abolished |
| Official 3rd-party audio story | Officially: Playback API issues signed manifests, audio via TIDAL Player SDK (developer.tidal.com) — ticli's direct-URL usage is tolerated-unofficial, not sanctioned | Officially: browser SDK or certified hardware only; unofficial path is reverse-engineered and disclaimed by its own maintainers |

## Refuted / corrected / uncertain claims

- **"Development mode allows 25 users"** — true until Feb 2026, now **5**
  for the cap that matters (existing >5-user apps grandfathered). The
  working question's number was out of date.
- **"Extended access is being restricted to registered businesses / large
  MAU" (2025 reports)** — **confirmed**, and stronger than reported:
  individuals are ineligible to apply at all since 15 May 2025; threshold is
  250k MAU (https://developer.spotify.com/blog/2025-04-15-updating-the-criteria-for-web-api-extended-access).
- **"librespot lossless will not be supported"** (widely-shared issue title,
  https://github.com/librespot-org/librespot/issues/1583) — the title
  overstates; the content shows lossless is **currently unreachable because
  Spotify does not serve FLAC to Connect/third-party endpoints**, while the
  maintainers consider client-side support feasible if that changes. Net
  effect for ticli today is the same: no lossless.
- **"The Feb 2026 endpoint cuts apply to all existing apps from 9 Mar
  2026"** — partially walked back: endpoint removals postponed for existing
  integrations; Premium/user-cap/client-ID rules did take effect (March 2026
  changelog + community update thread).
- **Community rate-limit figure ~180 req/min** — unofficial measurement, not
  Spotify documentation.

## What I could not verify, and why

- **Byte-exact wording of any developer.spotify.com / spotify.com / 
  newsroom.spotify.com page**: every Spotify-owned domain (and
  web.archive.org) is blocked by this environment's egress proxy. All
  Spotify-page quotes here are search-index captures of the cited primary
  URLs, cross-checked across ≥2 independent captures where possible. Risk of
  material error is low but nonzero; spot-check before quoting externally.
- **The numeric development-mode quota** (requests/day): Spotify does not
  publish it; only the mechanism (`QUOTA_EXCEEDED`, per-account counting) is
  documented.
- **Exact development-mode rate limit**: unpublished by design.
- **Whether Spotify tolerates personal-use librespot in practice** (ban
  frequency): no primary source either way; librespot's own disclaimer and
  the 2021 account-suspension reporting are the closest evidence.
- **Any Spotify statement on headless/non-browser Web Playback SDK use**:
  the docs define a browser support matrix and say nothing about headless
  embedding one way or the other.
- **spotify-tui's maintenance status** (widely believed dormant/archived):
  not confirmed from the repo page fetch; its architecture claims above are
  from its README and are verified.
- **TIDAL official developer-platform policy details** (Playback API signed
  manifests / Player SDK exclusivity): taken from developer.tidal.com
  documentation surfaced in search (https://developer.tidal.com/documentation)
  and a third-party API summary; not deeply probed here since ticli does not
  use that surface. Worth its own research pass if ticli ever considers
  moving onto TIDAL's official API.
