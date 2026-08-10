# Fresh-install rehearsal (spec: third-party installability)

Run 2026-08-10 on the development machine, simulating a user who has never
had paneglow or semapad: a brand-new venv, a clean `$HOME` with an empty
`~/.claude/settings.json` (`{"model": "opus"}` only), and every semapad path
redirected through the `SEMAPAD_*` environment overrides.

| Step | Command shape | Result |
|---|---|---|
| Install from source (non-editable, as a user would) | `pip install <repo>` | CLI entry point works |
| Hook install with **no prior hooks** | `semapad install-hooks` | `installed 11 hooks`; 11 events, one entry each; foreign settings keys preserved (`model` untouched) |
| First hook event on the fresh store | `SessionStart` piped to `semapad hook` | exit 0, state record created |
| Dashboard with **no mapping directory** (Desktop never ran) | `semapad ui --port 8653` | `/data` serves schema 1, 0 conversations, diagnostic `mapping directory missing` — degrades, never crashes |
| Second daemon while one runs | `semapad daemon` | `semapad: daemon already running`, exit 1 (singleton flock) |

Not rehearsed here: `autostart install` against a second account (it was
exercised for real on this machine the same day, including the 0755
hardening paths — see #22/#23), and real-pad behaviour on a machine that has
never paired the device (BLE pairing is done by the vendor app, out of
semapad's scope).
