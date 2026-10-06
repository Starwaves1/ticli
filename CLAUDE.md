# CLAUDE.md

Ticli: a terminal music player for TIDAL. tidalapi for the API, mpv (preferred) or ffplay for audio, Rich for the TUI. The code is the source of truth; read it. This file is a map, `GLOSSARY.md` holds the domain terms, and `docs/adr/` holds the decisions code can't explain.

## Commands

```bash
cd src && pip install -e ".[keyring]" pytest   # editable install
python -m pytest ticli/tests/ -q                # tests (from src/, as CI runs them)
ticli                                           # launch
```

## Map

Package in `src/ticli`; entry point `ticli = ticli.cli:main`.

- `cli.py`: Click entry, `--quality` / `--login-flow`, the `ticli agent` command group.
- `player.py`: everything interactive. `AudioPlayer` drives the mpv/ffplay process, seeking and the cache download; `HeadlessTidalPlayer` is the TUI (despite the name): login, `_stream_url`, `_monitor_playback` (the 0.5 s tick), `_handle_key` → per-screen `_handle_*_key`, `_build_display`, `run`.
- `agent.py` + `agent_docs.py`: the agent surface and the contract `ticli agent docs` prints.
- `utils/throttle.py`: cross-process request pacing and the trip.
- `utils/config.py`: settings and config migrations; `SETTINGS_SPEC` drives the settings page.
- `utils/credential_store.py`: tokens (keyring, or a `~/.config/ticli` fallback).
- `utils/cache.py`: metadata index, cached audio, budget and eviction.
- `utils/downloads.py` + `utils/tags.py`: user-owned downloads and stdlib tagging.
- `utils/artwork.py`: stdlib JPEG decode to half-block pixel art.
- `tests/`: `conftest.py` (suite-wide path redirects), `fakes.py` (network stand-ins), `vt.py` (terminal model for display tests).

## Damage gates

Rules the code doesn't fully enforce, where a slip is hard to undo:

- **Never hit TIDAL's live API from scripts, tests or probes.** Build against `tests/fakes.py`. If a live request is unavoidable: at most one per 15 s, and on any 429, 401/4006 or bot-detection page stop and report, never retry. It has blocked the owner's IP before ([ADR-0001](docs/adr/0001-tidal-rate-limits.md)).
- **For runtime TIDAL data use `ticli agent <verb>`**, never a script over the internals; only the agent surface is throttled.
- **Never delete or rewrite the owner's TIDAL playlists programmatically.** Destructive library actions are human-only, in the TUI.
- **Never delete or rewrite files in `~/Music/Ticli`** outside the user's own download actions.
- **Never bump `downloads.INDEX_VERSION` or `cache.TRACKER_VERSION`** for a rename or display change: a mismatched file is silently discarded, forgetting the download library or every play count. Translate old values at read time; a real schema change needs a migration.
- **Config and token schema changes ship a value-preserving migration** (precedents in `utils/config.py`), or existing users are silently downgraded or logged out.
- **Tests never touch the real network, `~/Music`, `~/.config/ticli`, the OS cache directory or the keyring.** Only some paths are redirected suite-wide in `tests/conftest.py`; anything new that writes at startup needs a redirect there first.

## Agent skills

### Issue tracker

Issues live in GitHub Issues on `odonald/ticli`, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` and `docs/adr/` at the repo root. See `docs/agents/domain.md`.

