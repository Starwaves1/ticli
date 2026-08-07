# ai/ — agent continuity for ticli

This folder is memory. Not documentation of *what the code does* — the code
covers that better — but of **why it is the way it is**: what was tried, what
was rejected, what broke, and which constraints are load-bearing rather than
incidental.

## If you are an agent: you are expected to update this folder

Reading `ai/` is half the job. **Writing back to it is the other half**, and it
is not optional — this folder only works because every agent before you left it
truer than they found it. Do it *in the same commit as the code*, never as a
retrofit later: a note written from memory two hours after the fact loses the
measurement, the rejected alternative, and the reason — which is the only part
worth keeping.

What goes where, when you finish a piece of work:

- **[HISTORY.md](HISTORY.md)** — always. A dated entry per change, in the style
  of the ones already there: what changed, *why*, what you measured, and what
  you rejected and why you rejected it.
- **[INCIDENTS.md](INCIDENTS.md)** — when something was broken in a way that
  teaches a rule. Not every bug; the ones where the *reason it survived* is
  instructive. Every rule in WORKING-RULES.md came from one of these.
- **[DECISIONS.md](DECISIONS.md)** — when the owner decides something, when a
  spec moves from proposed to built, or when you defer something. Quote him
  where the wording matters; record what is still open as clearly as what is
  settled.
- **[WORKING-RULES.md](WORKING-RULES.md)** — only when you learn a constraint
  that will bind future work. Rare, and it should hurt to add one.
- **`reference/`** — research and audits. Mark superseded material with a dated
  header rather than deleting it; a wrong answer with its date attached is more
  useful than a gap.

Two habits that matter more than the file list. **Correct what is now wrong** —
if you make a documented statement untrue, fix the statement in the same commit;
stale confidence is worse than silence. And **write down what you did not do**:
scope you dropped, a fix you judged too risky, a hypothesis you refuted. The
refutations have repeatedly been the most valuable entries here.

If you are the main thread rather than a subagent, this is your job to enforce:
say it in the brief, and check it in the diff.

## Reading order

If you are an agent picking this project up, read in this order:

| File | What it gives you |
|---|---|
| **[WORKING-RULES.md](WORKING-RULES.md)** | The constraints. Read first — several are non-obvious and violating them has already caused real damage. |
| **[HISTORY.md](HISTORY.md)** | What happened, in order, with the reasoning behind each change. |
| **[INCIDENTS.md](INCIDENTS.md)** | Seven things that went wrong and what each one taught. The most useful file here. |
| **[DECISIONS.md](DECISIONS.md)** | Product decisions locked by the owner, the roadmap, and specs for work not yet built. |
| **[WORKFLOWS.md](WORKFLOWS.md)** | If you are about to run a multi-agent workflow here: what worked, what silently failed, read before authoring one. |
| **[PR-SUMMARY.md](PR-SUMMARY.md)** | Draft material for the upstream pull request. |
| `BUGS-2026-07-24-resume-trace.md` | 11 traced findings from a resume-bug investigation. Historical; most are fixed, status noted inline. |
| `reference/download-research-2026-07-25.md` | 491 lines of live-probed TIDAL API research. Partly superseded — see the header note. |
| `reference/feature-design-research-2026-07-24.json` | Early per-feature design research. Historical. |
| `NOTES.md` | Scratch. |

The subsystems that need explaining to work on them at all — segmented DASH
playback, the artwork decoder, login flows, caching — are documented in the
module docstrings alongside the code they describe. This folder does not
duplicate them.

## The project

**ticli** — a terminal TIDAL client. Python, `tidalapi`, mpv or ffplay for
audio, Rich for the TUI. This is Garrett's fork (`Starwaves1/ticli`) of
`odonald/ticli`.

Work began 2026-07-24. Starting point: a player that worked but was, in the
owner's words, clunky. Since then: real lossless audio, sub-2ms input latency,
a UI that survives being resized, an on-disk cache with a tracker that decides
what is worth keeping, and downloads to the user's own music folder. The test
suite was 5 tests at the start and 1,480 as of 2026-08-07.

(Counts like that one go stale. If you notice a number here that no longer
matches reality, correct it — see the section above.)

## The one-paragraph version of the philosophy

Measure before fixing — two of the main thread's leading hypotheses were
wrong and were disproved by agents that were told to measure first. A refuted
hypothesis is a real result, worth reporting. Tests must assert observable
reality — bytes on disk, escape sequences on the terminal — because a feature
that never worked once passed a full test suite for two days on the strength
of tests that only checked bookkeeping. Degrade visibly rather than silently:
the worst bug in this codebase's history was not that playback broke, but
that it broke without saying so. Never show the user a number or an option
that isn't real.
