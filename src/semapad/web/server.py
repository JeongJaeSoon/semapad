"""Read-only web dashboard (spec §5.2).

Runs as its own process (``semapad ui``) -- never a thread inside a daemon
(#43). In Phase 1 this process alone performs sources -> view -> render and
writes nothing to the pad. Binds 127.0.0.1 only.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from semapad import config as config_mod
from semapad import frontmost, view
from semapad.sources import conversations, hooks, processes
from semapad.web import edit

DEFAULT_PORT = 8642
_POLL_MS = 1500


def _default_frontmost() -> str | None:
    return frontmost.bundle_id()


class Dashboard:
    """Computes one /data payload per poll; keeps slot assignment across polls."""

    #: A daemon snapshot older than this is stale and the ui computes its own.
    DAEMON_SNAPSHOT_FRESH_SECONDS = 5.0

    def __init__(self, *, state_dir: Path, mapping_dir: Path,
                 sessions_dir: Path, config_path: Path,
                 daemon_snapshot_path: Path | None = None,
                 frontmost_reader: Callable[[], str | None] = _default_frontmost,
                 clock: Callable[[], float] = time.time) -> None:
        self._state_dir = state_dir
        self._mapping_dir = mapping_dir
        self._sessions_dir = sessions_dir
        self._config_path = config_path
        self._daemon_snapshot_path = daemon_snapshot_path
        self._frontmost = frontmost_reader
        self._clock = clock
        self._prev: tuple[str | None, ...] | None = None
        self._debounce = view.DepartureDebouncer()

    @property
    def config_path(self) -> Path:
        return self._config_path

    def _config_fingerprint(self) -> str:
        try:
            return hashlib.sha256(self._config_path.read_bytes()).hexdigest()[:16]
        except OSError:
            return "absent"

    def _frontmost_section(self, cfg: config_mod.Config) -> dict:
        try:
            bundle = self._frontmost()
        except Exception as error:
            return {"bundle_id": None, "owner": None, "error": str(error)}
        owner = None
        if bundle in cfg.own_when:
            owner = "claude"
        elif bundle in cfg.yield_to:
            owner = "codex"
        return {"bundle_id": bundle, "owner": owner, "error": None}

    def _daemon_snapshot(self, now: float) -> dict | None:
        """A fresh daemon snapshot wins: the ui must draw what the daemon lit
        (#57, P11). Stale or absent -> the ui computes for itself (Phase 1-2)."""
        path = self._daemon_snapshot_path
        if path is None:
            return None
        try:
            snap = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(snap, dict) or snap.get("schema") != view.SNAPSHOT_SCHEMA:
            return None
        generated_at = snap.get("generated_at")
        if not isinstance(generated_at, (int, float)) \
                or now - generated_at > self.DAEMON_SNAPSHOT_FRESH_SECONDS:
            return None
        return snap

    def data(self) -> dict:
        now = self._clock()
        from_daemon = self._daemon_snapshot(now)
        import semapad as semapad_pkg
        if from_daemon is not None:
            from_daemon["source"] = "daemon"
            from_daemon["ui_version"] = semapad_pkg.version()
            from_daemon["poll_ms"] = _POLL_MS
            # §5.1 banner: a fingerprint differing from the on-disk config
            # means the last save has not reached the daemon's tick yet.
            from_daemon["config_pending"] = (
                from_daemon.get("config_fingerprint") != self._config_fingerprint())
            return from_daemon
        cfg, warnings = config_mod.load(self._config_path)
        convs, archived_ids, conv_diags = conversations.scan(self._mapping_dir)
        convs = self._debounce.apply(convs, prev_slots=self._prev,
                                     archived=archived_ids)
        proc_snapshot = processes.scan(self._sessions_dir)
        records = hooks.read_all(self._state_dir)

        built = view.build(
            conversations=convs,
            live_cli_ids={s.session_id for s in proc_snapshot.sessions},
            records=records,
            prev_slots=self._prev,
            colors=cfg.colors,
            working_max_seconds=cfg.working_max_seconds,
            now=now,
            diagnostics=conv_diags,
        )
        self._prev = tuple(s.local_id for s in built.slots)

        snap = view.snapshot(built, config_fingerprint=self._config_fingerprint(),
                             generated_at=now)
        # Section A: Phase 1 touches no hardware -- state the fact instead of
        # pretending a device section does not exist.
        snap["device"] = {
            "connected": False,
            "note": "데몬이 실행 중이 아닙니다 — `semapad autostart install`"
                    " 또는 `semapad daemon`으로 시작하세요. (패드 표시·키 입력은"
                    " 데몬이 담당합니다)",
        }
        snap["frontmost"] = self._frontmost_section(cfg)
        snap["processes"] = {
            "count": len(proc_snapshot.sessions),
            "authoritative": proc_snapshot.authoritative,
            "diagnostics": list(proc_snapshot.diagnostics),
        }
        snap["config"] = {
            "path": str(self._config_path),
            "warnings": warnings,
        }
        snap["poll_ms"] = _POLL_MS
        snap["source"] = "ui"
        snap["version"] = semapad_pkg.version()
        snap["ui_version"] = semapad_pkg.version()
        snap["config_pending"] = False
        return snap


_PAGE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><title>semapad</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { font: 14px/1.5 -apple-system, "SF Pro Text", sans-serif; background:#fff;
       color:#1a1a1a; max-width: 900px; margin: 2.2rem auto 4rem; padding: 0 1.2rem; }
h1 { font-size: 1.45rem; font-weight: 650; margin: 0 0 1.2rem; }
h2 { font-size: 1.05rem; font-weight: 650; margin: 2rem 0 .7rem;
     display:flex; justify-content:space-between; align-items:baseline; }
h2 small { font-weight: 400; color:#8a8a86; font-size:.82rem; }
.card { background:#fff; border:1px solid #e7e6e2; border-radius:14px; }
.row { display:flex; justify-content:space-between; align-items:center;
       gap:1.2rem; padding: .85rem 1.1rem; }
.row + .row { border-top:1px solid #f0efeb; }
.row .lab { font-weight: 560; }
.row .desc { color:#8a8a86; font-size:.82rem; margin-top:.1rem; max-width: 34rem; }
.row .val { color:#1a1a1a; white-space:nowrap; }
.ok { color:#1a9c53; } .warn { color:#c77d00; } .err { color:#d0342c; }
.badge { background:#f1f0ec; border-radius:.6rem; padding:.05rem .5rem;
         font-size:.78rem; color:#5a5a56; }

/* ---- pad rendering (vendor-style) ---- */
.padwrap { display:flex; justify-content:center; padding: 2.2rem 0; }
.padbody { background:#edecE7; background:#edece7; border-radius:26px;
           padding: 14px; display:grid; grid-template-columns:repeat(4, 76px);
           grid-auto-rows:76px; gap:10px;
           box-shadow: 0 1px 3px rgba(0,0,0,.08), 0 8px 28px rgba(0,0,0,.07); }
.kc { background:#fafaf8; border-radius:16px; position:relative;
      box-shadow: inset 0 0 0 1px rgba(0,0,0,.05), 0 1px 2px rgba(0,0,0,.06);
      display:flex; align-items:center; justify-content:center; }
.kc.knob { border-radius:50%; margin:6px; background:
           radial-gradient(circle at 35% 30%, #ffffff, #eceae4); }
.kc.stick { background:#fafaf8; }
.kc.stick::after { content:""; width:52px; height:52px; border-radius:50%;
      background: radial-gradient(circle at 38% 32%, #3c3c3c, #111); }
.kc.mini { border-radius:50%; margin:14px; background:
           radial-gradient(circle at 35% 30%, #2e2e2e, #0c0c0c); }
.kc.wide { grid-column: span 2; }
.kc.cmd { color:#8f8e8a; font-size:1.05rem; }
.kc .led { width:15px; height:15px; border-radius:50%; }
.kc.agent.lit .led { border:1px solid rgba(0,0,0,.18); }
.kc.agent { cursor: default; }
.kc.agent.lit .led { box-shadow: 0 0 10px 2px var(--glow); }
.kc.agent:hover { box-shadow: inset 0 0 0 2px #e8a33d, 0 1px 2px rgba(0,0,0,.06); }
.kc.agent.pressed { box-shadow: inset 0 0 0 3px #e8a33d, 0 1px 8px rgba(232,163,61,.5); }
.ticon { width: 1rem; height: 1rem; vertical-align: -2px; margin-right: .35rem; }
.tip { display:none; position:absolute; bottom: calc(100% + 10px); left:50%;
       transform: translateX(-50%); min-width: 15rem; max-width: 19rem;
       background:#fff; border:1px solid #e7e6e2; border-radius:12px;
       box-shadow: 0 10px 30px rgba(0,0,0,.13); padding:.7rem .85rem;
       z-index: 5; text-align:left; }
.kc.agent:hover .tip { display:block; }
.tip .tt { font-weight:600; display:-webkit-box; -webkit-line-clamp:2;
           -webkit-box-orient:vertical; overflow:hidden; }
.tip .ts { color:#6a6a66; font-size:.82rem; margin-top:.25rem;
           display:flex; align-items:center; gap:.4rem; }
.tip .dot, .legend .dot { width:.62rem; height:.62rem; border-radius:50%;
           display:inline-block; border:1px solid rgba(0,0,0,.12); }

table { border-collapse: collapse; width: 100%; font-size:.88rem; }
td, th { padding: .55rem 1.1rem; border-top: 1px solid #f0efeb; text-align: left; }
th { color:#8a8a86; font-weight:560; border-top:none; }
.legend { list-style:none; margin:0; padding:.4rem 1.1rem .8rem; }
.legend li { display:inline-flex; align-items:center; gap:.35rem;
             margin: .25rem 1.1rem .25rem 0; }
#alert { padding:.85rem 1.1rem; }
#alert.alert { color:#c77d00; font-weight:650; }
#diags ul { margin:.2rem 0 .6rem; padding: 0 1.1rem 0 2.2rem; }
input, select, button { font: inherit; }
input[type=text], input[type=number], select {
  border:1px solid #d9d8d3; border-radius:9px; padding:.35rem .6rem; }
input[type=color] { width:2.6rem; height:1.9rem; border:1px solid #d9d8d3;
  border-radius:9px; padding:.1rem; background:#fff; }
button { background:#111; color:#fff; border:0; border-radius:10px;
         padding:.5rem 1.4rem; font-weight:600; cursor:pointer; }
#cfgmsg { margin-left:.8rem; }
.cfgfoot { padding: .9rem 1.1rem; border-top:1px solid #f0efeb; }
</style></head><body>
<h1>semapad <small id="ver" style="color:#8a8a86;font-weight:400"></small>
<small id="stale" class="warn"></small></h1>

<div class="card" id="device"></div>

<h2>Layout <small>hover로 대화·상태 확인 · A키 6개만 semapad 소유</small></h2>
<div class="card"><div class="padwrap"><div class="padbody" id="pad"></div></div></div>

<h2>전체 대화 <small>사이드바와 같은 집합</small></h2>
<div class="card"><table id="convs"><thead><tr>
<th>키</th><th>제목</th><th>상태</th><th>이유</th><th>프로세스</th>
<th>딥링크</th><th>마지막 활동</th></tr></thead><tbody></tbody></table></div>

<h2>판정 근거</h2>
<div class="card"><div id="alert"></div>
<ul class="legend" id="legend"></ul>
<div id="diags"></div></div>

<h2>설정 <small>저장 즉시 반영</small></h2>
<form class="card" id="cfg" onsubmit="return saveConfig(event)">
<div id="cfgrows"></div>
<div class="cfgfoot"><button type="submit">저장</button> <span id="cfgmsg"></span></div>
</form>

<script>
const TOKEN = "%TOKEN%";
let SAVE_WATCH = false;
</script>
<script>
const HEX = c => c === null || c === undefined ? null
                : '#' + c.toString(16).padStart(6, '0');
const NAMES = {idle:'Idle', working:'Working', waiting:'Requires input',
               done:'Done', error:'Error'};
const RESULTS = {opened:'열림', open_failed:'열기 실패', empty_slot:'빈 키',
                 ignored_owner:'무시 — Claude 소유가 아님',
                 ignored_layer:'무시 — Layer 1 아님', ignored_input:'무효 신호'};
const REASONS = {empty:'빈 키', no_process:'프로세스 없음 → idle 흰색',
                 no_hook:'훅 신호 없음 → idle 흰색',
                 working_timeout:'working 시간 초과 → idle 강등', state:'훅 상태',
                 unread:'안 본 활동 있음 (unread 근사, 훅 불요)'};
function esc(s){ const d=document.createElement('span'); d.textContent=s??'';
                 return d.innerHTML; }
function dot(color){ return `<span class="dot" style="background:${color??'#000'}"></span>`; }
const ICON_USB = `<svg class="ticon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 21V7"/><path d="M12 7l-3 3"/><path d="M12 7l3-3"/><circle cx="12" cy="21" r="1.6" fill="currentColor" stroke="none"/><path d="M8 12H5v-2"/><path d="M8 12l4 3"/><rect x="15" y="8" width="3" height="3" transform="rotate(0 16.5 9.5)"/><path d="M16.5 11l-4.5 4"/></svg>`;
const ICON_BT = `<svg class="ticon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7 7l10 10-5 4V3l5 4L7 17"/></svg>`;
function ago(sec){ if(!sec) return '-'; const d=Date.now()/1000-sec;
  if(d<60) return Math.round(d)+'s 전'; if(d<3600) return Math.round(d/60)+'m 전';
  if(d<86400) return Math.round(d/3600)+'h 전'; return Math.round(d/86400)+'d 전'; }

function deviceRow(lab, val, desc){
  return `<div class="row"><div><div class="lab">${lab}</div>` +
         (desc ? `<div class="desc">${desc}</div>` : '') +
         `</div><div class="val">${val}</div></div>`;
}

function renderDevice(d){
  const dev = d.device;
  let rows = '';
  if (dev.note){
    rows += deviceRow('Connection', '<span class="warn">Phase 1</span>', esc(dev.note));
  } else {
    rows += deviceRow('Connection',
      dev.connected ? '<span class="ok">Connected</span>'
                    : '<span class="err">Not connected</span>',
      dev.pad_error_code ? `오류: ${esc(dev.pad_error_code)}` : '');
    const ticon = dev.transport === 'USB' ? ICON_USB
                : dev.transport === 'BLE' ? ICON_BT : '';
    rows += deviceRow('Transport · Firmware · Layer',
      `${ticon}${esc(dev.transport ?? '?')} · ${esc(dev.firmware ?? '?')} · L${dev.layer ?? '?'}` +
      (dev.status_verified ? ' <span class="ok">verified</span>'
                           : ' <span class="warn">미검증</span>'));
    if (dev.last_input)
      rows += deviceRow('마지막 키 입력',
        `<span class="badge">A${dev.last_input.key + 1}</span> ` +
        `${RESULTS[dev.last_input.result] ?? esc(dev.last_input.result)}` +
        ` · ${ago(dev.last_input.at)}`,
        '키를 눌렀는데 이 행이 안 바뀌면 패드 신호가 데몬에 도달하지 않은 것');
  else if (dev.last_input_result)
      rows += deviceRow('마지막 키 입력',
        RESULTS[dev.last_input_result] ?? esc(dev.last_input_result));
  }
  rows += deviceRow('소유권',
    (d.frontmost.owner ? `<b>${esc(d.frontmost.owner)}</b>` : '유지(규칙 불일치)'),
    `frontmost: ${esc(d.frontmost.bundle_id ?? 'n/a')}` +
    (d.frontmost.error ? ` — <span class="err">${esc(d.frontmost.error)}</span>` : ''));
  rows += deviceRow('패드 동기화',
    d.source === 'daemon' ? '<span class="ok">화면 = 패드 ✓</span>'
                          : '<span class="warn">데몬 미실행 — 화면만 계산 중</span>',
    (d.source === 'daemon'
      ? '이 화면은 패드를 칠하는 데몬의 계산 결과를 그대로 그립니다'
      : '패드는 지금 아무도 칠하고 있지 않습니다') +
    (d.config_pending ? ' · <span class="warn">설정 저장됨 — 반영 대기 중</span>' : ''));
  const linked = d.conversations.filter(c => c.process_alive).length;
  rows += deviceRow('실행 중 세션',
    `대화와 연결 ${linked} / 전체 ${d.processes.count}` +
    (d.processes.authoritative ? '' : ' <span class="warn">(비권위 스캔)</span>'),
    '전체에는 사이드바 대화와 연결되지 않은 세션(다른 세션의 서브에이전트 등)도 포함됩니다');
  document.getElementById('device').innerHTML = rows;
}

// physical layout: knob, A1, A2, stick / A3..A6 / 4 command keys /
// mini-knob, wide mic (span 2), face key — command zone is vendor territory.
function agentKeycap(s){
  if (!s || !s.local_id)
    return `<div class="kc agent${window.__pressKey === (s && s.__idx) ? ' pressed' : ''}">
      <span class="led"></span>
      <div class="tip"><div class="tt">빈 키</div>
      <div class="ts">${dot(null)} Off — 세션 없음</div></div></div>`;
  const color = HEX(s.color);
  const title = s.title || s.local_id.slice(0, 18) + '…';
  const lit = s.color !== null && s.color !== undefined;
  return `<div class="kc agent ${lit ? 'lit' : ''}${window.__pressKey === (s.__idx ?? -1) ? ' pressed' : ''}" style="--glow:${color??'transparent'}">
    <span class="led" style="background:${color??'transparent'}"></span>
    <div class="tip"><div class="tt">${esc(title)}</div>
      <div class="ts">${dot(color)} ${NAMES[s.state]??s.state}
        · ${REASONS[s.reason]??s.reason}</div>
      <div class="ts">${s.process_alive ? '<span class=ok>프로세스 live</span>'
                                        : '프로세스 없음'}
        · ${s.deeplink_url ? '딥링크 OK' : '<span class=err>딥링크 불가</span>'}</div>
    </div></div>`;
}

function renderPad(d){
  const S = d.slots;
  const li = d.device && d.device.last_input;
  window.__pressKey = (li && (Date.now()/1000 - li.at) < 4) ? li.key : null;
  for (let i = 0; i < S.length; i++){
    if (S[i]) S[i].__idx = i; else S[i] = {__idx: i, local_id: null};
  }
  document.getElementById('pad').innerHTML =
    `<div class="kc knob"></div>` + agentKeycap(S[0]) + agentKeycap(S[1]) +
    `<div class="kc stick"></div>` +
    agentKeycap(S[2]) + agentKeycap(S[3]) + agentKeycap(S[4]) + agentKeycap(S[5]) +
    `<div class="kc cmd">✎</div><div class="kc cmd">⧉</div>` +
    `<div class="kc cmd">⇄</div><div class="kc cmd">🗑</div>` +
    `<div class="kc mini"></div><div class="kc cmd wide">🎙</div><div class="kc cmd">☺</div>`;
}

function render(d){
  const ver = document.getElementById('ver');
  if (d.version){
    ver.textContent = 'v' + d.version;
    if (d.ui_version && d.ui_version !== d.version)
      ver.innerHTML = `v${esc(d.version)} <span class="warn">(화면은 v${esc(d.ui_version)} —
        데몬과 버전이 다릅니다. semapad autostart install로 데몬을 재시작하세요)</span>`;
  }
  if (SAVE_WATCH && !d.config_pending){
    SAVE_WATCH = false;
    const msg = document.getElementById('cfgmsg');
    msg.textContent = '반영 완료 ✓'; msg.className = 'ok';
    setTimeout(() => { if (!SAVE_WATCH) msg.textContent = ''; }, 4000);
  }
  renderDevice(d);
  renderPad(d);

  document.querySelector('#convs tbody').innerHTML = d.conversations.map(c =>
    `<tr><td>${c.key===null?'':'<span class=badge>A'+(c.key+1)+'</span>'}</td>
     <td>${esc(c.title || c.local_id.slice(0,20)+'…')}${c.pinned?' 📌':''}</td>
     <td>${dot(HEX(d.palette[c.state]))} ${NAMES[c.state]??c.state}</td>
     <td>${REASONS[c.reason]??c.reason}</td>
     <td>${c.process_alive?'<span class=ok>live</span>':'dead'}</td>
     <td>${c.deeplink_url?'OK':'<span class=err>불가</span>'}</td>
     <td>${ago(c.last_activity_at)}</td></tr>`).join('');

  const alertBox = document.getElementById('alert');
  alertBox.className = d.alert;
  alertBox.textContent = d.alert === 'alert'
    ? '키에 오르지 못한 대화가 입력/오류 대기 중 (테두리 blink)'
    : '숨은 알림 없음';

  document.getElementById('legend').innerHTML =
    Object.entries(d.palette).map(([s, c]) =>
      `<li style="cursor:pointer" title="클릭: config 조각 복사"
           onclick='copySnippet("${s}", "${HEX(c)}")'>
       ${dot(HEX(c))} ${NAMES[s]??s} – ${esc(s)}</li>`).join('') +
    `<li>${dot('#000')} Off – 세션 없음</li>`;

  const issues = [...(d.diagnostics||[]), ...(d.config.warnings||[]),
                  ...(d.processes.diagnostics||[])];
  document.getElementById('diags').innerHTML = issues.length
    ? '<ul>' + issues.map(w => `<li class="warn">${esc(w)}</li>`).join('') + '</ul>'
    : '';
}

function copySnippet(state, hex){
  navigator.clipboard.writeText(
    JSON.stringify({colors: {[state]: hex}}, null, 2));
}

function fieldInput(f){
  const id = 'f_' + f.path.replaceAll('.', '_');
  if (f.kind === 'color')
    return `<input type="color" id="${id}" value="${f.value}">`;
  if (f.kind === 'enum')
    return `<select id="${id}">` + f.options.map(o =>
      `<option ${o===f.value?'selected':''}>${o}</option>`).join('') + `</select>`;
  if (f.kind === 'int')
    return `<input type="number" min="0" id="${id}" value="${f.value}">`;
  return `<input type="text" id="${id}" value="${esc(f.value.join(', '))}"
          size="34" placeholder="쉼표로 구분">`;   // strings
}

const CFG_LABELS = {
  'colors.idle':    ['Idle 색', '할 일 없는 세션의 키 색'],
  'colors.working': ['Working 색', '에이전트 작업 중인 키 색'],
  'colors.waiting': ['입력 대기 색', '내 입력을 기다리는 키 색'],
  'colors.done':    ['완료 색', '끝났거나 안 본 활동이 있는 키 색'],
  'colors.error':   ['오류 색', '오류가 난 키 색'],
  'gate.mode':      ['키 소유권 판정', 'frontmost: 전면 앱 따라감 · always: 항상 semapad · off: 비활성'],
  'gate.own_when':  ['semapad가 소유하는 앱', '이 앱이 전면이면 키가 Claude 세션을 표시'],
  'gate.yield_to':  ['양보 대상 앱', '이 앱이 전면이면 키를 벤더에게 양보'],
  'underglow.claude': ['테두리 색 — Claude 소유', 'semapad가 키를 소유할 때 semapad가 칠하는 테두리'],
  'underglow.codex':  ['테두리 색 — 양보 중', 'Codex가 키를 소유할 때 semapad가 칠하는 테두리 (Codex 앱 설정이 아님)'],
  'underglow.scope':  ['테두리 알림 범위', 'outside: 키에 없는 대화만 · all_sessions: 전부 · off: 끔'],
  'underglow.reclaim_delay_ms': ['테두리 되찾기 지연 (ms)', '벤더가 테두리를 칠한 뒤 되찾기까지 대기'],
  'underglow.effects.normal': ['테두리 효과 — 평상시', ''],
  'underglow.effects.alert':  ['테두리 효과 — 알림', '화면 밖 대화가 기다릴 때'],
  'underglow.effects.fault':  ['테두리 효과 — 실패 피드백', '빈 키·열기 실패 시 잠깐'],
};
let CFG_FIELDS = [];
async function loadConfig(){
  const r = await fetch('/config');
  CFG_FIELDS = (await r.json()).fields;
  document.getElementById('cfgrows').innerHTML =
    `<div class="row"><div class="desc">이 설정은 semapad의 표시 동작을 바꿉니다 —
     Claude/Codex 앱 자체의 설정이 아닙니다.</div></div>` +
    CFG_FIELDS.map(f => {
      const [label, desc] = CFG_LABELS[f.path] ?? [null, ''];
      return `<div class="row"><div>
       <div class="lab">${label ? esc(label) : `<code>${f.path}</code>`}</div>
       ${desc ? `<div class="desc">${esc(desc)}</div>` : ''}
       <div class="desc err" id="e_${f.path.replaceAll('.', '_')}"></div></div>
       <div class="val">${fieldInput(f)}</div></div>`;
    }).join('');
}

async function saveConfig(ev){
  ev.preventDefault();
  const edits = {};
  for (const f of CFG_FIELDS){
    const el = document.getElementById('f_' + f.path.replaceAll('.', '_'));
    let v = el.value;
    if (f.kind === 'int') v = parseInt(v, 10);
    if (f.kind === 'strings')
      v = v.split(',').map(s => s.trim()).filter(Boolean);
    edits[f.path] = v;
  }
  document.querySelectorAll('[id^="e_"]').forEach(e => e.textContent = '');
  const msg = document.getElementById('cfgmsg');
  const r = await fetch('/config', {method: 'POST',
    headers: {'X-Semapad-Token': TOKEN, 'Content-Type': 'application/json'},
    body: JSON.stringify(edits)});
  if (r.ok){
    msg.textContent = '저장됨 — 반영 대기 중…'; msg.className = 'warn';
    SAVE_WATCH = true;
  }
  else {
    const body = await r.json();
    msg.textContent = '저장 실패'; msg.className = 'err';
    for (const [path, error] of Object.entries(body.errors || {})){
      const cell = document.getElementById('e_' + path.replaceAll('.', '_'));
      if (cell) cell.textContent = error;
    }
  }
  return false;
}

async function tick(){
  try {
    const r = await fetch('/data'); render(await r.json());
    document.getElementById('stale').textContent = '';
  } catch (e) {
    document.getElementById('stale').textContent = '연결 끊김';
  }
}
tick(); setInterval(tick, %POLL%);
loadConfig();
</script></body></html>
""".replace("%POLL%", str(_POLL_MS))


class _Handler(BaseHTTPRequestHandler):
    dashboard: Dashboard   # injected by make_server()
    csrf_token: str        # injected by make_server()

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path == "/":
            body = _PAGE.replace("%TOKEN%", self.csrf_token).encode()
            self._reply(200, "text/html; charset=utf-8", body)
        elif self.path == "/data":
            body = json.dumps(self.dashboard.data()).encode()
            self._reply(200, "application/json", body)
        elif self.path == "/config":
            cfg, _warnings = config_mod.load(self.dashboard.config_path)
            body = json.dumps({"fields": edit.schema(cfg)}).encode()
            self._reply(200, "application/json", body)
        else:
            self._reply(404, "text/plain", b"not found")

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path != "/config":
            self._reply(404, "text/plain", b"not found")
            return
        # CSRF gate: the token only ever appears in the same-origin page, and
        # cross-site requests cannot attach this custom header (spec §6).
        if self.headers.get("X-Semapad-Token") != self.csrf_token:
            self._reply(403, "application/json",
                        json.dumps({"error": "bad token"}).encode())
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            edits = json.loads(self.rfile.read(min(length, 1 << 20)))
            if not isinstance(edits, dict):
                raise ValueError("body must be an object")
        except (ValueError, OSError):
            self._reply(400, "application/json",
                        json.dumps({"error": "malformed body"}).encode())
            return
        errors = edit.apply_edits(self.dashboard.config_path, edits)
        if errors:
            self._reply(400, "application/json",
                        json.dumps({"errors": errors}).encode())
        else:
            # Applies immediately (spec §11.3): every /data poll reloads the
            # config, so the next tick renders with the saved values.
            self._reply(200, "application/json", b'{"ok": true}')

    def _reply(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass  # polling every 1.5s would flood stderr


def make_server(dashboard: Dashboard, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (_Handler,), {
        "dashboard": dashboard,
        "csrf_token": secrets.token_hex(16),
    })
    return ThreadingHTTPServer(("127.0.0.1", port), handler)
