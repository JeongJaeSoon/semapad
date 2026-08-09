# Phase 1 verification record (spec §8)

Run 2026-08-10, unattended overnight (screen locked the whole time — this
matters below). Spec: Obsidian SoT v2.3.

## Checklist status

| §8 item | Status | Evidence |
|---|---|---|
| Dashboard list = sidebar list | ✅ automated / 👤 eye-check pending | 3 non-archived mapping files = 3 dashboard rows, titles match. Visual sidebar comparison needs an unlocked session. |
| New conversation → latency / archive → removal | 👤 pending | Requires Desktop interaction. Procedure below. |
| Process death → dead but listed | ✅ | One dead conversation renders `dead` and stays listed with a working deeplink. |
| **Fully functional without hooks** | ✅ | Hooks not installed for semapad; list/titles/live-dead/deeplinks/unread all work; zero warnings. White/green only, as specified. |
| **Unread approximation** | ✅ partial / 👤 focus-toggle pending | All 3 real conversations had `lastFocusedAt < lastActivityAt` on disk and rendered unread green. Focus-toggle (green turns off after viewing) needs an unlocked session. |
| (After hook install) hook → colour latency | ✅ path verified / 🔒 live install blocked | Synthetic events through `semapad hook`: hook process **38 ms**, event → `/data` **81 ms** (browser poll adds ≤1.5 s). `permission_prompt` → waiting amber, `Stop` → done, joined the correct conversation. Live `install-hooks` on `~/.claude/settings.json` was denied by the session's permission classifier — run it manually (one command, see below). |
| Deeplink mapping for all conversations | ✅ | 132/132 mapping filenames are canonical `local_` UUIDs; all displayed conversations produce a URL. |
| P13 sleep experiment | 🔒 hardware | Needs eyes on the pad + Codex app toggling. Note: the paneglow daemon (5 s rewrite) is still running — observing whether the pad ever auto-dims *now* answers "does periodic rewrite block sleep" before semapad even ships Phase 3. |
| P8 reproduction conditions | 🔒 hardware | With P13 experiment. |
| P9 reproduction | ◐ file-level only | See findings. Visual smoothness needs an unlocked session. |

## Measurements

- Mapping scan (132 files): **45 ms** — comfortable inside the 1.5 s poll.
- Hook event → `/data` reflection: **81 ms** end-to-end (fired by a real
  `semapad hook` subprocess; state store write is atomic + rev-guarded).

## Findings

1. **No pin field exists.** Field union across all 132 mapping files contains
   nothing pin-like (`pinned`, `isPinned`, …). `Conversation.pinned` is
   hardwired `False` until the pin/unpin diff names the field (spec §11.7).
   → Procedure: pin any conversation in Desktop, then diff its mapping file;
   wire the revealed field into `conversations.scan`.
2. **Locked screen: deeplink open does not update `lastFocusedAt`.**
   `open claude://claude.ai/epitaxy/<local_id>` at the loginwindow touched the
   mapping file (mtime moved) but changed no fields, spawned no process, and
   recorded no focus in a 20 s watch. Good news for the unread model: focus is
   only recorded on *actual visible focus*, so background/locked opens cannot
   fake "read". Re-run the P9 observation unlocked.
3. **Startup ordering behaves per §11.9**: after a ui restart the keys resorted
   once by `lastActivityAt` (supervision conversation had overtaken this one);
   within a running ui the assignment is sticky across polls.

## Manual steps for the user

```bash
# 1. Hook migration (removes paneglow hooks -> pad colours freeze until Phase 3):
/Users/dev-soon/workspace/semapad/.venv/bin/semapad install-hooks

# 2. Dashboard:
/Users/dev-soon/workspace/semapad/.venv/bin/semapad ui   # http://127.0.0.1:8642
```

Then, with the dashboard open: compare section C against the sidebar; create
and archive a conversation; click a green conversation and watch the green
turn off; pin one conversation and say so (the file diff takes seconds).
P13/P8: with the pad visible, leave Codex app running 3 min untouched, then
quit it and wait 3 min again — note whether the pad dims in either case.
