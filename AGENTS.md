# ticli — agent entry points

Two different jobs land an agent here. Pick yours; each pointer's target is
complete, so nothing else needs reading first.

**Using ticli** — searching TIDAL, resolving songs, managing playlists, or
checking the player on the user's behalf: run `ticli agent docs`. It is the
whole contract — every verb with its request cost and JSON shape, the
enforced rate rules, and the workflows. Do not import ticli's internals or
call TIDAL's API directly; the surface exists because that path got the
owner's IP blocked.

**Working on ticli's code**: read `ai/README.md` first — it is short and
routes you to the working rules (several are hard, learned expensively),
the history, and where to write your own findings back. Updating `ai/` is
part of any code change, in the same commit.

## Agent skills

### Issue tracker

Issues live in GitHub Issues on `Starwaves1/ticli` (this fork, not upstream), via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`, all created on the repo. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `GLOSSARY.md` and `docs/adr/` at the repo root, created lazily. See `docs/agents/domain.md`.
