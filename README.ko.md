# semapad

[![test](https://github.com/JeongJaeSoon/semapad/actions/workflows/test.yml/badge.svg)](https://github.com/JeongJaeSoon/semapad/actions/workflows/test.yml)

병렬로 돌아가는 **Claude Desktop** 코딩 세션들을 **Codex Micro** 매크로 패드와
로컬 웹 대시보드에서 한눈에 보고, 바로 전환하는 도구입니다.

에이전트 키 6개가 각각 대화 하나의 실시간 상태를 색으로 보여주고, 키를 누르면
그 대화가 Claude Desktop에서 열립니다.

| 키 색 | 의미 |
|---|---|
| ⚪ 흰색 | 대기 중 (idle) |
| 🔵 파랑 | 에이전트 작업 중 |
| 🟠 주황 | **내 입력을 기다리는 중** |
| 🟢 초록 | 완료 / 안 본 활동 있음 |
| 🔴 빨강 | 오류 |
| ⚫ 꺼짐 | 이 키에 대화 없음 |

패드 테두리는 키의 주인(주황 = Claude, 파랑 = Codex)을 보여주고, 화면 밖 대화가
입력을 기다리면 깜박입니다.

[English →](README.md)

## 요구사항

- macOS
- [Claude Desktop](https://claude.ai/download) (Claude Code 세션 사용)
- [Codex Micro](https://worklouder.cc/) 패드 — USB 또는 블루투스
  (블루투스는 전용 앱에서 먼저 페어링)
- 별도의 macOS 권한을 요구하지 않습니다

## 설치

**Homebrew (권장):**

```bash
brew install jeongjaesoon/tap/semapad
```

**pipx:**

```bash
pipx install git+https://github.com/JeongJaeSoon/semapad.git
```

**소스에서** (Python 3.10+ 필요):

```bash
git clone https://github.com/JeongJaeSoon/semapad.git
cd semapad
python3 -m venv .venv && .venv/bin/pip install -e .
# 이후 .venv/bin/semapad 로 실행
```

## 준비

```bash
semapad install-hooks       # 1. working/입력 대기/오류 색 활성화
semapad autostart install   # 2. 로그인 시 자동 시작
semapad ui                  # 3. 대시보드 열기
```

1. **훅**은 Claude Code가 세션 상태를 알리게 합니다. 없어도 동작하지만
   색이 흰색·초록으로 제한됩니다.
2. **자동 시작**은 사용자 LaunchAgent를 설치합니다. 이번 한 번만 돌리려면
   `semapad daemon`.
3. **대시보드**(http://127.0.0.1:8642)는 패드 실물 그대로의 렌더링(키에
   마우스를 올리면 대화·상태·색의 이유), 전체 대화 목록, 기기 상태, 그리고
   즉시 반영되는 설정 편집을 제공합니다.

## 일상 사용

- 사이드바와 패드는 항상 일치합니다: 사이드바의 대화 = 켜진 키. 아카이브하면
  키가 당겨져 빈틈을 메웁니다. 키가 멋대로 재배치되는 일은 없습니다.
- 키를 누르면 그 대화가 Claude Desktop에서 열립니다.
- **주황이 복귀 신호입니다** — 그 세션이 뭔가 물어봤다는 뜻입니다.
- Codex 앱이 전면이면 키를 벤더에게 양보하고, 테두리로 소유권만 알립니다.
  그 뒤에서 Claude 세션이 기다리면 파란 테두리가 깜박입니다.

## 알아두면 좋은 것

- 색 반영은 이벤트로부터 1~2초 안입니다.
- Codex 앱을 함께 쓸 때는 **같은 키 더블탭을 피하세요** — Codex가 더블탭을
  "내 창 올리기"로 해석하고, 물리 키는 양쪽 앱 모두에게 들립니다
  ([상세](https://github.com/JeongJaeSoon/semapad/issues/19)).
- semapad는 Claude Desktop이 Mac에 저장하는 파일에서 대화 제목·상태를
  읽습니다. Desktop 업데이트로 파일 형식이 바뀌면 추측하는 대신 대시보드에
  그 사실을 표시합니다.

## 제거

```bash
semapad autostart uninstall
semapad uninstall-hooks
```

둘 다 semapad의 것이 아닌 항목은 건드리지 않습니다.

## 라이선스

MIT — [LICENSE](LICENSE) 참조.
