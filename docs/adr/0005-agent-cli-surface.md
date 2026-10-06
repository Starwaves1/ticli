# Programs use ticli through `ticli agent`, a JSON CLI

Agents were importing ticli's internals and writing throwaway scripts, which bypassed every brake (ADR-0001). The answer is `ticli agent <verb>`: one JSON object on stdout, structured errors, each verb's request cost in its `--help`, and `ticli agent docs` as the single contract. Any agent with a shell can use it and it needs no framework dependency.

Revised 2026-10-06: agents should have everything a human has, so a control channel into the running player is now wanted. It arrives as the background player's socket (ADR-0008), reached through the same `ticli agent` CLI, with every action one named command in `commands.py` behind the human's switches (ADR-0007). Destructive verbs become possible, but only behind "Allow dangerous commands". Still a CLI, still no MCP.

## Considered options

- **An MCP server.** Still deferred: a second surface to maintain, worth building only once the verb shapes have settled, and it would have to be hand-rolled to keep ADR-0002.
- **A Python API.** Rejected: it is the import-the-internals path this replaced.
- **No destructive verbs ever.** The original stance; replaced by the dangerous-commands switch, which leaves the decision with the human per install.
