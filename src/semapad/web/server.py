"""Read-only web dashboard (spec §5.2).

Runs as its own process (``semapad ui``) -- never a thread inside a daemon
(#43). In Phase 1 this process alone performs sources -> view -> render and
writes nothing to the pad. Binds 127.0.0.1 only.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from semapad import config as config_mod
from semapad import frontmost, view
from semapad.sources import conversations, hooks, processes

DEFAULT_PORT = 8642
_POLL_MS = 1500


def _default_frontmost() -> str | None:
    return frontmost.bundle_id()


class Dashboard:
    """Computes one /data payload per poll; keeps slot assignment across polls."""

    def __init__(self, *, state_dir: Path, mapping_dir: Path,
                 sessions_dir: Path, config_path: Path,
                 frontmost_reader: Callable[[], str | None] = _default_frontmost,
                 clock: Callable[[], float] = time.time) -> None:
        self._state_dir = state_dir
        self._mapping_dir = mapping_dir
        self._sessions_dir = sessions_dir
        self._config_path = config_path
        self._frontmost = frontmost_reader
        self._clock = clock
        self._prev: tuple[str | None, ...] | None = None

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

    def data(self) -> dict:
        now = self._clock()
        cfg, warnings = config_mod.load(self._config_path)
        convs, conv_diags = conversations.scan(self._mapping_dir)
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
            "phase": 1,
            "connected": False,
            "note": "Phase 1: no pad writes (spec §0). Device I/O arrives in Phase 3.",
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
        return snap


_PAGE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8"><title>semapad</title>
<style>
:root { color-scheme: dark; }
body { font: 14px/1.5 -apple-system, sans-serif; background:#111; color:#ddd;
       max-width: 980px; margin: 2rem auto; padding: 0 1rem; }
h1 { font-size: 1.3rem; } h2 { font-size: 1rem; margin: 1.4rem 0 .4rem;
     color:#aaa; text-transform: uppercase; letter-spacing:.06em; }
table { border-collapse: collapse; width: 100%; }
td, th { padding: .3rem .5rem; border-bottom: 1px solid #2a2a2a; text-align: left; }
.grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: .6rem; }
.key { border: 1px solid #333; border-radius: .5rem; padding: .6rem;
       min-height: 4.4rem; position: relative; }
.key .sw { width: 1rem; height: 1rem; border-radius: 50%; display: inline-block;
           vertical-align: -2px; margin-right: .4rem; border: 1px solid #444; }
.key .t { white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
          display: block; font-weight: 600; }
.key small { color: #888; }
.badge { background:#333; border-radius:.6rem; padding:0 .5rem; font-size:.8rem; }
.warn { color: #ffb74d; } .err { color: #ff5252; } .ok { color:#69f0ae; }
#alert.alert { color: #ffb74d; font-weight: 700; }
.legend span.sw { width:.8rem; height:.8rem; display:inline-block;
                  border-radius:50%; margin-right:.3rem; vertical-align:-1px;
                  border:1px solid #444; }
.legend li { display:inline-block; margin-right:1rem; }
ul { padding-left: 1.1rem; }
</style></head><body>
<h1>semapad <small id="stale" class="warn"></small></h1>

<h2>A. 기기</h2>
<div id="device"></div>

<h2>B. 6키 그리드</h2>
<div class="grid" id="grid"></div>

<h2>C. 전체 대화</h2>
<table id="convs"><thead><tr>
<th>키</th><th>제목</th><th>상태</th><th>이유</th><th>프로세스</th>
<th>딥링크</th><th>마지막 활동</th></tr></thead><tbody></tbody></table>

<h2>D. 판정 근거</h2>
<div id="alert"></div>
<ul class="legend" id="legend"></ul>
<div id="diags"></div>

<script>
const HEX = c => c === null ? null : '#' + c.toString(16).padStart(6, '0');
const NAMES = {idle:'Idle', working:'Working', waiting:'Requires input',
               done:'Done', error:'Error'};
const REASONS = {empty:'빈 키', no_process:'프로세스 없음 → idle 흰색',
                 no_hook:'훅 신호 없음 → idle 흰색',
                 working_timeout:'working 시간 초과 → idle 강등', state:'훅 상태',
                 unread:'안 본 활동 있음 (unread 근사, 훅 불요)'};
function esc(s){ const d=document.createElement('span'); d.textContent=s??'';
                 return d.innerHTML; }
function sw(color){ return `<span class="sw" style="background:${color??'#000'}"></span>`; }
function ago(sec){ if(!sec) return '-'; const d=Date.now()/1000-sec;
  if(d<60) return Math.round(d)+'s 전'; if(d<3600) return Math.round(d/60)+'m 전';
  if(d<86400) return Math.round(d/3600)+'h 전'; return Math.round(d/86400)+'d 전'; }

function render(d){
  document.getElementById('device').innerHTML =
    `<p>${esc(d.device.note)}</p>
     <p>frontmost: <code>${esc(d.frontmost.bundle_id ?? 'n/a')}</code>` +
    (d.frontmost.owner ? ` → 소유권 <b>${esc(d.frontmost.owner)}</b>` : ' (소유권 규칙 불일치)') +
    (d.frontmost.error ? ` <span class="err">${esc(d.frontmost.error)}</span>` : '') +
    `</p><p>프로세스 ${d.processes.count}개` +
    (d.processes.authoritative ? '' : ' <span class="warn">(비권위 스캔)</span>') + `</p>`;

  document.getElementById('grid').innerHTML = d.slots.map(s => {
    if (!s.local_id) return `<div class="key" title="빈 키"><span class="t">
      <span class="sw"></span>—</span><small>${REASONS.empty} · off</small></div>`;
    const title = s.title || s.local_id.slice(0, 14) + '…';
    const tip = `${title} · ${NAMES[s.state]??s.state} · ${REASONS[s.reason]??s.reason}`;
    return `<div class="key" title="${esc(tip)}">
      <span class="t">${sw(HEX(s.color))}${esc(title)}</span>
      <small>${NAMES[s.state]??''} · ${REASONS[s.reason]??s.reason}
      · ${s.process_alive ? '<span class=ok>live</span>' : 'dead'}
      · ${s.deeplink_url ? '딥링크 OK' : '<span class=err>딥링크 불가</span>'}</small></div>`;
  }).join('');

  document.querySelector('#convs tbody').innerHTML = d.conversations.map(c =>
    `<tr><td>${c.key===null?'':'<span class=badge>A'+(c.key+1)+'</span>'}</td>
     <td>${esc(c.title || c.local_id.slice(0,20)+'…')}${c.pinned?' 📌':''}</td>
     <td>${sw(HEX(d.palette[c.state]))}${NAMES[c.state]??c.state}</td>
     <td>${REASONS[c.reason]??c.reason}</td>
     <td>${c.process_alive?'<span class=ok>live</span>':'dead'}</td>
     <td>${c.deeplink_url?'OK':'<span class=err>불가</span>'}</td>
     <td>${ago(c.last_activity_at)}</td></tr>`).join('');

  const alertBox = document.getElementById('alert');
  alertBox.className = d.alert;
  alertBox.textContent = d.alert === 'alert'
    ? '키에 오르지 못한 대화가 입력/오류 대기 중 (Phase 3: 테두리 blink)'
    : '숨은 알림 없음';

  document.getElementById('legend').innerHTML =
    Object.entries(d.palette).map(([s, c]) =>
      `<li>${sw(HEX(c))}${NAMES[s]??s} – ${esc(s)}</li>`).join('') +
    `<li><span class="sw" style="background:#000"></span>Off – 세션 없음</li>`;

  const issues = [...(d.diagnostics||[]), ...(d.config.warnings||[]),
                  ...(d.processes.diagnostics||[])];
  document.getElementById('diags').innerHTML = issues.length
    ? '<ul>' + issues.map(w => `<li class="warn">${esc(w)}</li>`).join('') + '</ul>'
    : '<p class="ok">경고 없음</p>';
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
</script></body></html>
""".replace("%POLL%", str(_POLL_MS))


class _Handler(BaseHTTPRequestHandler):
    dashboard: Dashboard   # injected by serve()

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path == "/":
            body = _PAGE.encode()
            self._reply(200, "text/html; charset=utf-8", body)
        elif self.path == "/data":
            body = json.dumps(self.dashboard.data()).encode()
            self._reply(200, "application/json", body)
        else:
            self._reply(404, "text/plain", b"not found")

    def _reply(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass  # polling every 1.5s would flood stderr


def make_server(dashboard: Dashboard, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (_Handler,), {"dashboard": dashboard})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)
