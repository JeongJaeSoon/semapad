# semapad

[![test](https://github.com/JeongJaeSoon/semapad/actions/workflows/test.yml/badge.svg)](https://github.com/JeongJaeSoon/semapad/actions/workflows/test.yml)

Watch and switch your parallel **Claude Desktop** coding sessions from a
**Codex Micro** macro pad — and from a local web dashboard.

Each of the six agent keys lights up with the live state of one conversation.
Press a key to open that conversation in Claude Desktop.

| Key colour | Meaning |
|---|---|
| ⚪ White | Idle |
| 🔵 Blue | Agent is working |
| 🟠 Amber | **Waiting for your input** |
| 🟢 Green | Finished / has unseen activity |
| 🔴 Red | Error |
| ⚫ Off | No conversation on this key |

The pad's border shows who owns the keys (amber = Claude, blue = Codex) and
blinks when an off-screen conversation needs you.

[한국어 문서 →](README.ko.md)

## Requirements

- macOS
- [Claude Desktop](https://claude.ai/download) with Claude Code sessions
- A [Codex Micro](https://worklouder.cc/) pad, connected over USB or
  Bluetooth (pair Bluetooth with its own app first)
- No special macOS permissions are requested

## Install

**Homebrew (recommended):**

```bash
brew install jeongjaesoon/tap/semapad
```

**pipx:**

```bash
pipx install git+https://github.com/JeongJaeSoon/semapad.git
```

**From source** (needs Python 3.10+):

```bash
git clone https://github.com/JeongJaeSoon/semapad.git
cd semapad
python3 -m venv .venv && .venv/bin/pip install -e .
# then use .venv/bin/semapad
```

## Set up

```bash
semapad install-hooks       # 1. colours for working / waiting / error
semapad autostart install   # 2. start on login, keep running
semapad ui                  # 3. open the dashboard
```

1. **Hooks** teach Claude Code to report each session's state. Without them
   everything still works, but keys only show white and green.
2. **Autostart** installs a per-user LaunchAgent. To run it just once
   instead: `semapad daemon`.
3. **The dashboard** (http://127.0.0.1:8642) shows the pad as it really is —
   hover a key for its conversation, state, and why it has that colour — plus
   every conversation, device status, and settings you can edit in place.

## Everyday use

- Sidebar and pad stay in sync: a conversation on the sidebar is a lit key;
  archive it and the keys close ranks. Keys never reshuffle on their own.
- Press a key → that conversation opens in Claude Desktop.
- Amber is the signal to come back: that session asked you something.
- When the Codex app is frontmost, the keys are handed over to it; the
  border keeps showing ownership, and blinks blue if a Claude session is
  waiting behind it.

## Good to know

- Colours change within a second or two of the underlying event.
- If you also run the Codex app, avoid double-tapping a key — Codex treats a
  double tap as "raise my window" and both apps hear the keys
  ([details](https://github.com/JeongJaeSoon/semapad/issues/19)).
- semapad reads conversation titles and states from files Claude Desktop
  keeps on your Mac. A Desktop update can change those files; if that
  happens the dashboard says so instead of guessing.

## Uninstall

```bash
semapad autostart uninstall
semapad uninstall-hooks
```

Both leave everything that is not semapad's untouched.

## License

MIT — see [LICENSE](LICENSE).
