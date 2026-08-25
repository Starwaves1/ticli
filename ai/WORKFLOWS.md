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

- **A verification campaign wants an odd number of lenses per finding, and a
  final reviewer who is allowed to overturn.** The 2026-08-25 pre-release
  review ran seven finders → one refute-by-default panel → a completeness
  critic → a single final reviewer. The last stage earned its cost twice: it
  *promoted* three critic observations to findings after verifying them
  itself, and it reclassified a refuted finding (the `.part` collision) from
  "not a bug" to "blocked by another bug, re-arms when you fix it" — a verdict
  neither the finder nor the refuter could reach alone, because each was
  looking at one of the two bugs.

- **Give the critic the refutations, not just the confirmations.** Feeding the
  reject pile in verbatim is what surfaced the wrongly-shaped refutation above.
  The 2026-08-02 lesson (read `journal.jsonl` before trusting the reject pile)
  generalises: build the re-reading into the workflow instead of doing it by
  hand afterwards.

- **Install the binaries the suite skips on.** `mpv` and `ffmpeg` are two
  `apt-get` lines and they convert eight skipped tests into eight executed
  ones, covering the segmented-HLS path that is otherwise reviewed only by
  reading. On a headless box set `ao=null` (an `mpv.conf` under `MPV_HOME`)
  and `SDL_AUDIODRIVER=dummy` first, or two tests fail for want of `/dev/snd`
  and cost an hour of investigating a bug that is not there.

- **The main thread should re-verify the verdict's load-bearing claims by
  hand.** Cheap, and it is where a review's credibility actually lives: a
  `curl` at PyPI, a build against a pinned old setuptools, `unzip -l` on the
  wheel, and four `sed -n` reads settled seven of them in one round. The cache
  rail leak was confirmed not by argument but by noticing the reviewer's own
  `~/.cache/ticli/audio.json` had appeared.
