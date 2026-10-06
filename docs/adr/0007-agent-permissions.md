# Three TUI-only switches decide what agents may do, on an honour system

"Allow AI control" (on), "Allow dangerous commands" (off) and an optional "AI control key" (salted scrypt hash in config.json) are checked by one gate inside `commands.execute` for every agent call; human calls are never gated. They change only by TUI keypress: no command, CLI verb or agent verb can write them, and each change toasts so tampering is visible. A local agent with a shell could still edit config.json, so this is an honour system: `ticli agent docs` and every refusal say only the human can change the switches, the agent must ask, and must never edit config or impersonate the TUI. Nothing about it goes in CLAUDE.md, which agents working on ticli's source read, not agents using it. `ticli <verb>` counts as the human only with a TTY on stdin; anything else is an agent. Over the player socket the caller is self-declared and `subscribe` and `reload_switches` aren't gated, so any client can claim to be the TUI: the same honour system.

## Considered options

- **Real enforcement (OS user separation, signed requests).** Rejected: out of proportion for a music player on the owner's own machine.
- **Lockout after wrong keys.** Rejected: a 1 s delay per wrong key slows guessing without letting a misbehaving agent lock the human out.
