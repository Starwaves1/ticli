# Programs use ticli through `ticli agent`, a JSON CLI

Agents were importing ticli's internals and writing throwaway scripts, which bypassed every brake (ADR-0001). The answer is `ticli agent <verb>`: one JSON object on stdout, structured errors, each verb's request cost in its `--help`, and `ticli agent docs` as the single contract. Any agent with a shell can use it and it needs no framework dependency. It has no destructive verbs; deleting or rewriting a user's TIDAL playlists stays a human action in the TUI.

## Considered options

- **An MCP server.** Deferred: a second surface to maintain, worth building only once the verb shapes have settled, and it would have to be hand-rolled to keep ADR-0002.
- **A control socket into the running TUI.** Deferred: a deliberate second way into the player, not a loosening of the single-instance lock.
- **A Python API.** Rejected: it is the import-the-internals path this replaced.
