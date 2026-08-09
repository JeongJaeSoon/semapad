# semapad

**semapad** = semaphore + pad. A macOS status surface for Claude Desktop
conversations: a read-only web dashboard first, a 6-key RGB pad (Codex Micro)
as one of its output devices.

> The product is *reading Claude Desktop's state correctly and showing it*.
> The pad is an output device for that state.

## Status

Phase 1 — state model + read-only web dashboard.

| Phase | Scope |
|---|---|
| 1 | sources → view → web dashboard (no pad writes) |
| 2 | config editing from the dashboard |
| 3 | pad output (colours) + key input (session switching) |

## Design

- Display unit is the **conversation** (Desktop sidebar), not the process.
- Hooks are the only state signal. No hook → idle white.
- Zero dependencies (stdlib only), no TCC permissions.
- Keys compact only on lifecycle events (archive), never on activity.

## Naming

| Item | Value |
|---|---|
| Package / module / CLI | `semapad` |
| LaunchAgent label | `io.github.jeongjaesoon.semapad` |
| Runtime home | `~/.semapad/` |
| Environment variables | `SEMAPAD_HOME`, `SEMAPAD_CLAUDE_SETTINGS`, `SEMAPAD_CLAUDE_SESSIONS`, `SEMAPAD_MAPPING_DIR` |

## History

semapad is a from-scratch rewrite of
[JeongJaeSoon/paneglow](https://github.com/JeongJaeSoon/paneglow). The
hardware-proven layers (`protocol`, `pad`, `deeplink`, hook classification,
`frontmost`) are ported unmodified; everything above them is rebuilt around a
conversation-based state model. History and issues live in the old repo.

## License

MIT
