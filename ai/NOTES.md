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
