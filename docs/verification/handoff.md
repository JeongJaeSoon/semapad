# Switchover & acceptance handoff

Written 2026-08-10 at the end of the overnight build run. Everything below is
what remains between the current state (all code merged, 433 tests green) and
"semapad drives the pad". All of it needs the user present.

## Current state

- Phases 1–3 implemented and merged (PR #1–#5). The physical pad has NOT been
  touched by semapad; the **paneglow daemon still owns it** (LaunchAgent
  `io.github.jeongjaesoon.paneglow`) and paneglow's hooks are still installed.
- Dashboard: `semapad ui` → http://127.0.0.1:8642 (running). Without a daemon
  it computes for itself; once `semapad daemon` runs, the ui automatically
  draws the daemon's snapshot (`[데몬 스냅샷]` label in section A).

## One-shot switchover (do these together, in order)

```bash
# 1. stop the old daemon and remove its LaunchAgent
launchctl bootout gui/$(id -u)/io.github.jeongjaesoon.paneglow
rm ~/Library/LaunchAgents/io.github.jeongjaesoon.paneglow.plist

# 2. migrate the hooks (claims/removes all -m paneglow.cli hook entries)
/Users/dev-soon/workspace/semapad/.venv/bin/semapad install-hooks

# 3. run the semapad daemon in the foreground (first run: watch it)
/Users/dev-soon/workspace/semapad/.venv/bin/semapad daemon
```

Order matters: step 1 before step 3 keeps the pad at ≤2 writers (semapad +
vendor app). A semapad LaunchAgent is deliberately not shipped yet — run the
daemon in the foreground until §9.1 passes, then ask for the agent.

## Remaining Phase 1 items (§8) — need the user

| Item | How |
|---|---|
| Sidebar eye-check | Compare dashboard section C with the Desktop sidebar (count & titles) |
| New conversation / archive latency | Create then archive a conversation with the dashboard open; keys must compact on archive only |
| Unread toggle | Click a green conversation; its green should turn off (I verified the lock-screen negative: no focus is ever recorded while locked) |
| Pin field | Pin any conversation, then say so — the mapping-file diff takes seconds and names the field (none exists today; `pinned` is hardwired False until then) |
| P13 sleep | With the pad visible: Codex app running, hands off 3 min → dims? Then quit Codex, 3 min → dims? If our 5 s rewrite blocks sleep, set `{"timing": {"idle_rewrite": "off"}}` and repeat |
| P8 | Note border stutter conditions during the P13 runs |
| P9 | Press keys / open deeplinks and describe how the switch looks (epitaxy route should switch in place) |

## §9.1 acceptance (run twice: USB and BLE)

- [ ] 세션 2개 → A1·A2 각 상태 색
- [ ] 작업 시작 → working 파랑 / 응답 대기 → waiting 주황
- [ ] 물리 A키 → 정확히 그 세션 (싱글탭 = 백그라운드 포커스, 더블탭 = 창 전면 — 새 탭 의미)
- [ ] 7개로 늘려도 기존 6키 배치 안정 + 화면 밖 alert 테두리 blink
- [ ] 대화 하나 아카이브 → 뒤 키 당김(구멍 없음)
- [ ] Codex 전면 → A존 양보 + 테두리 Codex 파랑 / 벤더 write 후 0.2초 내 복구
- [ ] Codex 보는 동안 Claude 대화 대기 → 파란 테두리 blink
- [ ] 제3 앱 전환 → 직전 소유권 유지
- [ ] Layer 2+ → 입력·표시 포기 / Layer 1 복귀 → 전체 재도색
- [ ] USB unplug/replug → 자동 재연결 + 재도색
- [ ] Ctrl-C 종료 → Claude-owned 존만 소등, Codex-owned 보존
- [ ] no-flash: Claude 전면에서 A키 연타에도 Codex 창 노출 없음
- [ ] 지연: 상태 변화 → LED 500ms, 키 press → 세션 열림 150ms

## Open questions

1. ~~daemon.py 463 lines vs §7's 300-line target~~ → **resolved 2026-08-10**:
   supervision accepted the deviation after verifying the substance (zero
   colour/effect literals in daemon, writes decided only in the compositor).
   SoT §7 now states the substantive criteria; the 300-line proxy is retired.
2. **Single-tap delay**: vendor-style taps mean a single tap acts only after
   the 350 ms double-tap window. If that feels laggy in practice, the window
   is one constant (`input.DOUBLE_TAP_SECONDS`).
3. **paneglow repo archive** — user decides the timing (spec §10).
