# ai/ — project memory

Local-only (gitignored) working memory for AI-assisted development on Garrett's ticli fork.

- Fork: https://github.com/Starwaves1/ticli (origin) · upstream: https://github.com/odonald/ticli
- Baseline commit at start of feature work: 729bfde
- Dev setup: `pipx install -e src/` (editable) + injected `keyring`. Edits to `src/ticli/*.py` are live on next `ticli` launch. No src/.venv.
- Audio backend in practice: **ffplay only** (mpv not installed on this machine).

## Feature wishlist (from Garrett, 2026-07-24)

1. Add-to-playlist from the player
2. General responsiveness (caching)
3. Show-artwork toggle
4. macOS media-key / Now Playing integration
5. Search: pagination on scroll-down, filter by playlist/artist/track/album
6. Search within your playlists
7. Reopen remembers what song was playing
8. Real settings page (window size, number of songs shown)

Design docs live in `ai/design/`.

## Unreproduced test failure, 2026-08-02

One full-suite run at `10aab6e` reported `1 failed, 1440 passed` — and the
harness capturing only the summary line lost the failing test's name. It did
not reproduce: 9 full-suite runs, 6 rounds of the branch-touched files
(test_resume, test_downloads, test_cache, test_radio) and 10 rounds of the
timing-sensitive files (test_input_latency, test_buffering, test_seek,
test_download_queue, test_bulk_downloads) were all green afterwards. The
project rule stands — a flaky test is a bug until proven otherwise — so this
is recorded rather than forgotten: if a test fails once in CI or locally,
check here, get its name, and chase it. Nothing about the failure is known
except that it happened once on a loaded container.
