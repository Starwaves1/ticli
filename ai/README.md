# ai/ — agent continuity for ticli

This folder is memory. Not documentation of *what the code does* — the code
covers that better — but of **why it is the way it is**: what was tried, what
was rejected, what broke, and which constraints are load-bearing rather than
incidental.

If you are an agent picking this project up, read in this order:

| File | What it gives you |
|---|---|
| **[WORKING-RULES.md](WORKING-RULES.md)** | The constraints. Read first — several are non-obvious and violating them has already caused real damage. |
| **[HISTORY.md](HISTORY.md)** | What happened, in order, with the reasoning behind each change. |
| **[INCIDENTS.md](INCIDENTS.md)** | Five things that went wrong and what each one taught. The most useful file here. |
| **[DECISIONS.md](DECISIONS.md)** | Product decisions locked by the owner, the roadmap, and specs for work not yet built. |
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

Work ran 2026-07-24 to 2026-07-26. Starting point: a player that worked but
was, in the owner's words, clunky. Twenty commits later: 605 tests, real
lossless audio, sub-2ms input latency, and a UI that survives being resized.

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
