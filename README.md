# semapad

[![test](https://github.com/JeongJaeSoon/semapad/actions/workflows/test.yml/badge.svg)](https://github.com/JeongJaeSoon/semapad/actions/workflows/test.yml)

**semapad** = semaphore + pad. Claude Desktop 대화의 상태를 색으로 보여주는 macOS
도구다 — 로컬 웹 대시보드가 제품이고, 6키 RGB 패드(Codex Micro)는 그 상태의 출력
장치다. 키를 누르면 해당 대화가 Desktop에서 열린다.

> 제품은 "Claude Desktop의 상태를 정확히 읽고 보여주는 것"이다.
> 패드는 출력 장치 중 하나다.

| 색 | 의미 |
|---|---|
| ⚪ White | Idle — 살아 있음, 특별한 일 없음 |
| 🔵 Blue | Working — 에이전트 작업 중 |
| 🟠 Amber | **Requires input** — 내 입력을 기다림 |
| 🟢 Green | Done / 안 본 활동 있음 |
| 🔴 Red | Error |
| ⚫ Off | 그 키에 대화 없음 — 꺼짐은 오직 이 뜻뿐 |

## 요구사항

- **macOS** (Darwin 전용 — IOKit·NSWorkspace 사용, 별도 TCC 권한은 요구하지 않음)
- **Python 3.10+**
- **Claude Desktop** (Claude Code 세션을 이 앱으로 사용)
- **Codex Micro** (VID `0x303A` / PID `0x8360`) — USB·BLE 지원.
  BLE는 벤더 앱에서 이미 페어링된 기기에 연결한다.
- 런타임 외부 의존성 없음 (stdlib만)

## 설치

아래 절차는 fresh-install 리허설로 검증됐다
([기록](docs/verification/fresh-install.md)).

```bash
git clone https://github.com/JeongJaeSoon/semapad.git
cd semapad
python3 -m venv .venv
.venv/bin/pip install -e .
```

**1. 훅 설치 (선택이지만 권장)** — 훅 없이도 대시보드·패드는 완전히 동작하지만
색이 흰/초록으로 제한된다. 파랑(working)·주황(입력 대기)·빨강(error)을 보려면:

```bash
.venv/bin/semapad install-hooks
```

`~/.claude/settings.json`의 기존 항목은 보존하고, 구 paneglow 훅이 있으면 회수해
이중 발화를 막는다. 재실행해도 중복 설치되지 않는다.

**2. 데몬** — 로그인 자동 시작(권장):

```bash
.venv/bin/semapad autostart install
.venv/bin/semapad autostart status
```

또는 이번 로그인에서만 전면 실행: `.venv/bin/semapad daemon`
(데몬은 머신당 하나 — 두 번째 인스턴스는 스스로 종료한다)

**3. 대시보드**:

```bash
.venv/bin/semapad ui        # http://127.0.0.1:8642
```

기기 연결 상태, 실물 배치 그대로의 패드 렌더링(hover로 대화·상태·판정 사유),
전체 대화 목록, 색 legend, 설정 편집(저장 즉시 반영)을 한 화면에서 본다.

## 동작 원칙

- 표시 단위는 **대화**(Desktop 사이드바)다 — 프로세스가 아니다. 사이드바에 보이면
  키가 켜져 있고, 아카이브하면 꺼지며 뒤 키가 당겨진다. 활동으로는 재정렬하지 않는다.
- 개별 키를 시간 경과로 끄는 타이머는 없다. 패드 전체 절전은 벤더(Codex 앱)의
  auto-dim을 존중한다.
- Codex 앱이 전면이면 A존을 벤더에게 양보하고 테두리로 소유권(주황=Claude,
  파랑=Codex)만 알린다. 화면 밖 대화가 입력을 기다리면 테두리가 깜박인다.
- 설정은 `~/.semapad/config.json` — 대시보드에서 편집하는 것이 가장 쉽다.
  상태 색(`colors`), 소유권 게이트(`gate`), 테두리(`underglow`), 타이밍(`timing`).

## 알려진 제약 (Known issues)

- **Desktop의 비공개 파일 스키마에 의존한다** — 대화 목록·딥링크는 Claude Desktop이
  로컬에 쓰는 파일에서 읽는다. 앱 업데이트로 형식이 바뀌면 깨질 수 있다 (그 경우
  대시보드 진단에 표시된다).
- **Codex 앱과 동시 사용 시**: 물리 키 신호는 양쪽 모두에게 간다. Claude 전면에서
  **같은 키를 빠르게 2연타**하면 Codex의 더블탭 제스처가 발화해 Codex 창이 올라온다
  ([#19](https://github.com/JeongJaeSoon/semapad/issues/19) — 차단 방법 조사 중.
  탭은 1회로 완결되므로 2연타하지 않으면 발생하지 않는다).
- 세션이 재시작되면 입력 대기(주황) 상태가 흰색으로 씻길 수 있다
  ([#14](https://github.com/JeongJaeSoon/semapad/issues/14)).
- Codex로 양보한 직후 A존에 이전 색이 잠시 남는다
  ([#18](https://github.com/JeongJaeSoon/semapad/issues/18)).
- 새 대화·아카이브의 패드 반영은 1–2초다
  ([#16](https://github.com/JeongJaeSoon/semapad/issues/16)).

## 개발

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -m 'not integration' -q   # CI와 동일
.venv/bin/python -m pytest -m integration -q          # 실제 Codex Micro 필요
```

사양(SoT)은 개발자의 Obsidian에 있고, 결정 이력은
[paneglow#64](https://github.com/JeongJaeSoon/paneglow/issues/64)에 미러돼 있다.
semapad는 [paneglow](https://github.com/JeongJaeSoon/paneglow)(archived)의
재구현이며, 하드웨어 계층은 그 실기 검증을 그대로 승계했다.

## 라이선스

MIT
