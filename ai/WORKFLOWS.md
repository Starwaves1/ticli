# Multi-agent workflows on this repo

What actually mattered across the two campaigns run so far (data-integrity
fixes 2026-08-02, dead-code sweep 2026-08-03). Everything else about
orchestration you already know.

- **Refute-by-default verification earns its cost here.** One verifier per
  finding (batched ~3 per file), told to grep string forms, read every call
  site, and check this folder for a documented reason the code exists. It
  caught the one must-fix of the first campaign and zero false removals in
  the second.

- **Join verdicts loosely.** A verifier renamed a symbol ("load_config" →
  "load_config (line 300 version pre-seed)") and an exact-match join silently
  dropped a *confirmed* verdict as refuted. Before trusting a workflow's
  reject pile, read the journal (`journal.jsonl` in the run's transcript dir)
  — the raw verdicts are all there.

- **Brief finders on the intentional dead-looking patterns first**: the
  `track_ids`+`tracks` downgrade-compat double write, `_restore_sleep` and
  other monkeypatch seams, pytest fixture imports that linters call unused,
  referenced-only-from-tests ≠ dead. With that paragraph in the prompt, the
  player.py dead-code finder returned zero findings rather than five false
  positives.

- **Seed with mechanical evidence, don't trust it.** vulture/pyflakes in a
  scratchpad venv (dev tools only — the no-new-dependencies rule is about
  ticli, not your tooling) gave finders a worklist; most hits were false
  positives and the real findings were things no linter flags (duplicated
  policy blocks, provable no-ops).

- **Agents find; the main thread edits.** player.py is 8.8k lines and every
  campaign touches it — parallel writers conflict, and the repo's
  ai/-updates-in-the-same-commit rule needs one author anyway. Worktree
  isolation is for parallel *verifiers*, not implementers.

- **Confirmed ≠ apply.** A verifier can prove a change safe under today's
  constants and it can still be wrong to make (the `art_size` guard,
  declined 2026-08-03). Record the refusal here; the refusals have been the
  most valuable entries in this folder.
