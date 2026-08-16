"""Thin foreground loop: sources -> view -> surfaces -> compositor (spec §7).

No colour, effect or slot policy lives here. This module owns exactly two
things: the pad connection lifecycle (retry, reconnect, status verification --
behaviour proven in the old daemon) and the per-tick orchestration order.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from pathlib import Path

from semapad import frontmost as frontmost_module
from semapad import input as input_module
from semapad import pad as pad_module
from semapad import view as view_module
from semapad.compositor import Compositor
from semapad.config import Config, load as load_config
from semapad.model import OWNERS, Light
from semapad.sources import conversations, hooks, processes
from semapad.surfaces import agent_keys, ambient

_STATUS_TIMEOUT_SECONDS = 1.0
_RETRY_MAX_SECONDS = 30.0
_FEEDBACK_SECONDS = 0.3
_PRESS_ECHO_SECONDS = 0.6   # long enough to survive the vendor's press-time overwrite
_SNAPSHOT_INTERVAL_SECONDS = 1.0


def owner_for(bundle_id: str | None, previous: str, cfg: Config) -> str:
    """The exact three-state owner transition for the frontmost app."""
    prior = previous if previous in OWNERS else "none"
    if cfg.gate_mode == "off":
        return "none"
    if cfg.gate_mode == "always":
        return "claude"
    if bundle_id in cfg.own_when:
        return "claude"
    if bundle_id in cfg.yield_to:
        return "codex"
    return prior


class Daemon:
    """One deterministic tick machine; every side effect is injectable."""

    def __init__(self, cfg: Config, *, state_dir: Path, mapping_dir: Path,
                 sessions_dir: Path, config_path: Path, snapshot_path: Path,
                 pad: pad_module.Pad | None = None,
                 pad_factory: Callable[[], object | None] = pad_module.Pad.open,
                 opener: Callable[..., bool] = input_module.open_local,
                 frontmost: Callable[[], str | None] = frontmost_module.bundle_id,
                 ) -> None:
        self.cfg = cfg
        self.state_dir = state_dir
        self.mapping_dir = mapping_dir
        self.sessions_dir = sessions_dir
        self.config_path = config_path
        self.snapshot_path = snapshot_path
        self.pad = pad
        self._pad_factory = pad_factory
        self._frontmost = frontmost
        self.compositor = Compositor(cfg)
        self.router = input_module.KeyRouter(opener)

        self.owner: str = "none"
        self.frontmost_ok = False
        self.frontmost_id: str | None = None
        self.view: view_module.View | None = None
        self.last_input_result: str | None = None
        self.last_input: dict | None = None   # {key, result, at} for the ui
        self.async_scan = False
        self._SCAN_INTERVAL_SECONDS = 0.5
        import threading as _threading
        self._scan_lock = _threading.Lock()
        self._scan_result = None
        self._scan_started = None
        self._scan_running = False
        self._events_path = (state_dir.parent / "logs" / "events.jsonl"
                             if state_dir is not None else None)
        self.pad_error_code: str | None = None
        self.last_status_at: float | None = None
        self.causes: tuple[str, ...] = ()
        self.generation = 0

        self._prev_slots: tuple[str | None, ...] | None = None
        self._debounce = view_module.DepartureDebouncer()
        self._feedback_until: float | None = None
        self._flash_light: Light | None = None
        self._slot_colors: tuple[int | None, ...] = ()
        self._verified_epoch: int | None = None
        self._verified_layer: int | None = None
        self._next_status_due = 0.0
        self._needs_reconnect = False
        self._next_retry_due = 0.0
        self._retry_seconds = 1.0
        self._closed = False
        self._tick_causes: list[str] = []
        self._config_stat: tuple[int, int] | None = None
        self._config_fingerprint = "absent"
        self._processes_info: dict = {"count": 0, "authoritative": False,
                                      "diagnostics": []}
        self._next_snapshot_due = 0.0
        self._view_fingerprint: object = None
        self._outage_started_at: float | None = None
        self._outage_code: str | None = None

    @property
    def verified_layer(self) -> int | None:
        return self._verified_layer

    def _cause(self, value: str) -> None:
        if value not in self._tick_causes:
            self._tick_causes.append(value)

    def _set_pad_error(self, code: str | None, *, clear_status: bool = False,
                       now: float | None = None) -> None:
        changed = code != self.pad_error_code
        if code != self.pad_error_code:
            self._log_event("pad_error", code=code,
                            transport=getattr(self.pad, "transport", None))
            # A press during an outage never reaches the pad's queue, so it
            # leaves no trace anywhere -- the window length is the only
            # evidence it could have happened. Timed here because this is the
            # one choke point every entry and exit flows through.
            if self.pad_error_code is None:
                self._outage_started_at = now
                self._outage_code = code
            elif code is None and self._outage_started_at is not None:
                self._log_event(
                    "outage", code=self._outage_code,
                    seconds=(round(now - self._outage_started_at, 1)
                             if now is not None else None))
                self._outage_started_at = None
        self.pad_error_code = code
        if clear_status and self.last_status_at is not None:
            self.last_status_at = None
            changed = True
        if changed:
            self._cause("pad")

    # --- config (apply on save, spec §11.3) ---------------------------------

    def _reload_config(self) -> None:
        try:
            stat = self.config_path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = None
        if signature == self._config_stat:
            return
        self._config_stat = signature
        cfg, _warnings = load_config(self.config_path)
        try:
            raw = self.config_path.read_bytes()
            self._config_fingerprint = hashlib.sha256(raw).hexdigest()[:16]
        except OSError:
            self._config_fingerprint = "absent"
        if cfg != self.cfg:
            self.cfg = cfg
            self.compositor.set_config(cfg)
            self._cause("config")

    # --- sources -> view ----------------------------------------------------

    def _build_view(self, now: float) -> view_module.View:
        convs, archived_ids, conv_diags = self._scan_conversations()
        snapshot = processes.scan(self.sessions_dir)
        live_ids = {session.session_id for session in snapshot.sessions}
        try:
            hooks.prune(self.state_dir,
                        live_ids if snapshot.authoritative else None,
                        self.cfg.ttl_minutes * 60.0, now)
        except Exception:
            pass
        records = hooks.read_all(self.state_dir)

        convs = self._debounce.apply(convs, prev_slots=self._prev_slots,
                                     archived=archived_ids)
        built = view_module.build(
            conversations=convs, live_cli_ids=live_ids, records=records,
            prev_slots=self._prev_slots, colors=self.cfg.colors,
            working_max_seconds=self.cfg.working_max_seconds, now=now,
            diagnostics=conv_diags + snapshot.diagnostics,
        )
        self._prev_slots = tuple(slot.local_id for slot in built.slots)
        self._slot_colors = tuple(slot.color for slot in built.slots)
        self._processes_info = {"count": len(snapshot.sessions),
                                "authoritative": snapshot.authoritative,
                                "diagnostics": list(snapshot.diagnostics)}
        fingerprint = tuple(
            (slot.local_id, slot.state, slot.color) for slot in built.slots
        ) + (built.alert,)
        if fingerprint != self._view_fingerprint:
            self._view_fingerprint = fingerprint
            self.compositor.mark_dirty()
            self._cause("view")
        self.view = built
        return built

    # --- owner gate ---------------------------------------------------------

    def _refresh_owner(self) -> None:
        try:
            bundle_id = self._frontmost()
            if bundle_id is not None and type(bundle_id) is not str:
                raise ValueError("frontmost bundle id must be a string or None")
        except Exception:
            self.frontmost_ok, self.frontmost_id = False, None
            # Frontmost mode fails closed; policies that do not depend on
            # NSWorkspace keep meaning exactly what they say.
            new_owner = owner_for(None, "none", self.cfg)
        else:
            self.frontmost_ok, self.frontmost_id = True, bundle_id
            new_owner = owner_for(bundle_id, self.owner, self.cfg)
        if new_owner != self.owner:
            self._log_event("owner", from_=self.owner, to=new_owner)
            self.owner = new_owner
            self.compositor.mark_dirty()
            self._cause("owner")

    # --- pad lifecycle (ported behaviour) -----------------------------------

    def _invalidate_pad(self, *, reconnect: bool, now: float,
                        error_code: str | None = None,
                        clear_status: bool = True) -> None:
        self._verified_epoch = self._verified_layer = None
        self.compositor.invalidate()
        if error_code is not None:
            self._set_pad_error(error_code, clear_status=clear_status, now=now)
        if reconnect:
            self._needs_reconnect = True
            self._next_retry_due = max(self._next_retry_due,
                                       now + self._retry_seconds)
            self._retry_seconds = min(_RETRY_MAX_SECONDS, self._retry_seconds * 2)

    def _accept_pad_status(self, old_epoch: int | None, old_layer: int | None,
                           now: float) -> bool:
        current = self.pad
        if current is None or not getattr(current, "connected", False) \
                or not getattr(current, "status_verified", False):
            return False
        epoch = getattr(current, "epoch", None)
        layer = getattr(current, "layer_index", None)
        if type(epoch) is not int or epoch < 1 \
                or type(layer) is not int or layer < 1:
            return False
        # Only a real boundary -- a new connection epoch, a layer change, or a
        # first verification -- leaves queued input that was made under a layer
        # we cannot vouch for. The routine 1 s poll is not one of those: the
        # press it finds was made under the layer already verified, and
        # dropping it silently ate ~2.4% of presses on a healthy pad (#60 lesson
        # applied too widely, measured 2026-08-17).
        boundary = (epoch != old_epoch or layer != old_layer
                    or old_layer is None)
        if boundary:
            try:
                dropped = current.discard_hid_inputs()
            except Exception:
                return False
            if dropped:
                # The one input loss we can actually count.
                self._log_event("input_dropped", count=dropped,
                                transport=getattr(current, "transport", None))
        self._verified_epoch, self._verified_layer = epoch, layer
        self.last_status_at = now
        self._set_pad_error(None, now=now)
        self._cause("status")
        self._next_status_due = now + self._status_poll_seconds()
        self._needs_reconnect = False
        self._retry_seconds = 1.0
        self._next_retry_due = now
        if boundary:
            self.compositor.invalidate()
            if epoch != old_epoch:
                self._cause("epoch")
            if layer != old_layer:
                self._cause("layer")
        return True

    def _verify_status(self, now: float) -> None:
        current = self.pad
        if current is None:
            return
        old_epoch, old_layer = self._verified_epoch, self._verified_layer
        self._verified_epoch = self._verified_layer = None
        try:
            reply = current.status(timeout=_STATUS_TIMEOUT_SECONDS)
        except Exception:
            self._invalidate_pad(reconnect=True, now=now,
                                 error_code="status_unverified")
            return
        result = reply.get("result") if isinstance(reply, dict) else None
        layer = result.get("layer_index") if isinstance(result, dict) else None
        if type(layer) is not int or layer < 1 \
                or not self._accept_pad_status(old_epoch, old_layer, now):
            self._invalidate_pad(reconnect=False, now=now,
                                 error_code="status_unverified",
                                 clear_status=False)
            self._next_status_due = now + self._status_poll_seconds()

    def _status_poll_seconds(self) -> float:
        base = max(0.001, self.cfg.status_poll_ms / 1000.0)
        if getattr(self.pad, "transport", "") == "BLE":
            return base * 5   # each poll is airtime the input shares (#49)
        return base

    def _apply_exclusive(self, now: float) -> None:
        """Reopen the device seized while Claude owns it (spec: #41/#19).

        ponytail: a full reconnect per ownership flip -- cheap enough (one
        status roundtrip); revisit only if flip latency is ever felt.
        """
        current = self.pad
        if current is None or not hasattr(current, "set_exclusive"):
            return
        desired = self.cfg.exclusive and self.owner == "claude"
        if desired == current.exclusive_requested:
            return
        current.set_exclusive(desired)
        self._log_event("exclusive", requested=desired, owner=self.owner)
        self._invalidate_pad(reconnect=True, now=now)
        self._next_retry_due = now          # flip immediately, no backoff
        self._retry_seconds = 1.0

    def _ensure_pad(self, now: float) -> None:
        if self.pad is None:
            if now < self._next_retry_due:
                return
            try:
                self.pad = self._pad_factory()
            except Exception:
                self.pad = None
            if self.pad is None:
                self._set_pad_error("unavailable", clear_status=True, now=now)
                self._next_retry_due = now + self._retry_seconds
                self._retry_seconds = min(_RETRY_MAX_SECONDS,
                                          self._retry_seconds * 2)
                return
            self._needs_reconnect = False
            self._next_status_due = now

        current = self.pad
        if self._needs_reconnect or not getattr(current, "connected", False):
            if now < self._next_retry_due:
                if not self._needs_reconnect:
                    self._set_pad_error("disconnected", clear_status=True, now=now)
                return
            old_epoch, old_layer = self._verified_epoch, self._verified_layer
            try:
                reconnected = current.reconnect(timeout=_STATUS_TIMEOUT_SECONDS)
            except Exception:
                reconnected = False
            if reconnected and self._accept_pad_status(old_epoch, old_layer, now):
                return
            self._invalidate_pad(reconnect=True, now=now,
                                 error_code="reconnect_failed")
            return
        if now >= self._next_status_due:
            self._verify_status(now)

    def _gate_layer_one(self) -> bool:
        current = self.pad
        return (current is not None
                and getattr(current, "connected", False)
                and getattr(current, "status_verified", False)
                and self._verified_epoch == getattr(current, "epoch", None)
                and self._verified_layer == 1)

    def _scan_conversations(self):
        """Cached, off-thread mapping scan (Codex recommendation #3).

        The scan reads ~130 files and measured 58 ms median on real
        hardware -- synchronously inside the HID owner thread it was the
        largest controllable tail on press latency. With async_scan the
        owner thread only swaps in the latest completed result; the scan
        itself runs on a worker at most once per interval. Tests construct
        the daemon without async_scan and keep fully synchronous behaviour.
        """
        if not self.async_scan:
            return conversations.scan(self.mapping_dir)
        import threading
        import time as time_mod
        now = time_mod.monotonic()
        with self._scan_lock:
            result = self._scan_result
            due = (self._scan_started is None
                   or (now - self._scan_started) >= self._SCAN_INTERVAL_SECONDS)
            if due and not self._scan_running:
                self._scan_running = True
                self._scan_started = now
                threading.Thread(target=self._scan_worker,
                                 name="semapad-scan", daemon=True).start()
        if result is None:
            # First tick: nothing cached yet -- scan synchronously once.
            result = conversations.scan(self.mapping_dir)
            with self._scan_lock:
                if self._scan_result is None:
                    self._scan_result = result
        return result

    def _scan_worker(self) -> None:
        try:
            result = conversations.scan(self.mapping_dir)
            with self._scan_lock:
                self._scan_result = result
        finally:
            with self._scan_lock:
                self._scan_running = False

    def _log_event(self, kind: str, **fields: object) -> None:
        """Append one analysis line; logging must never break the daemon.

        Local-only operational log (0600 dir). Carries ids, never titles.
        """
        if self._events_path is None:
            return
        try:
            import json as json_mod
            import time as time_mod
            self._events_path.parent.mkdir(parents=True, exist_ok=True)
            record = {"ts": round(time_mod.time(), 3), "event": kind, **fields}
            with open(self._events_path, "a") as handle:
                handle.write(json_mod.dumps(record) + "\n")
        except Exception:
            pass

    # --- input & messages ---------------------------------------------------

    def _finish_input(self, outcome: input_module.Outcome, now: float,
                      key_colour: int | None = None) -> None:
        if outcome.press_seen:
            # The pad lights the whole A-zone itself while a key is down (#60).
            self.compositor.mark_dirty(keys=True, ambient=False)
        flash = None
        duration = _FEEDBACK_SECONDS
        if outcome.feedback:
            flash = Light(self.cfg.underglow_claude, self.cfg.effect_fault)
        elif outcome.result == "opened" and key_colour is not None:
            # Press echo: the border briefly takes the pressed key's colour.
            flash = Light(key_colour)
            duration = _PRESS_ECHO_SECONDS
        if flash is not None:
            self._flash_light = flash
            self._feedback_until = now + duration
            self.compositor.mark_dirty(keys=False, ambient=True)
            self._cause("input_feedback")
        if True:
            self.last_input_result = outcome.result
            self._cause("input")

    def _handle_messages(self, messages: list, now: float) -> None:
        for received in messages:
            if not isinstance(received, pad_module.ReceivedMessage):
                continue
            if received.connection_epoch != self._verified_epoch:
                continue
            message = received.message
            for cause in self.compositor.note_message(
                    message, now, owner=self.owner,
                    layer_one=self._gate_layer_one()):
                self._cause(cause)
            if pad_module.is_vendor_write(message):
                # Raw capture: does the device ACK echo the vendor's payload?
                # Decides whether Codex's session states are observable (#41).
                self._log_event("vendor_write", owner=self.owner,
                                message=message)
            parsed = input_module.parse(message)
            if parsed is None or parsed == "release":
                continue
            if parsed == "invalid":
                self._finish_input(input_module.Outcome("ignored_input"), now)
                self._log_event("input", key=None, result="ignored_input")
                continue
            # Re-read ownership at dispatch time; an app switch must close the
            # gate before a press can open a conversation.
            self._refresh_owner()
            outcome = self.router.dispatch(
                parsed, now, owner=self.owner, layer_one=self._gate_layer_one(),
                slots=self._prev_slots or (None,) * view_module.KEY_COUNT)
            colours = self._slot_colors
            self._finish_input(
                outcome, now,
                key_colour=(colours[parsed.key_index]
                            if parsed.key_index < len(colours) else None))
            self.last_input = {"key": parsed.key_index,
                               "result": outcome.result, "at": now}
            self._had_input = True
            import time as time_mod
            lat_ms = round((time_mod.monotonic() - received.received_at) * 1000, 1)
            slots = self._prev_slots or ()
            self._log_event(
                "input", key=parsed.key_index, result=outcome.result,
                owner=self.owner, lat_ms=lat_ms,
                transport=getattr(self.pad, "transport", None),
                local_id=(slots[parsed.key_index]
                          if parsed.key_index < len(slots) else None))

    # --- snapshot (spec §5.1: consumers draw finished values) ---------------

    def _write_snapshot(self, built: view_module.View, now: float) -> None:
        current = self.pad
        snap = view_module.snapshot(
            built, config_fingerprint=self._config_fingerprint, generated_at=now)
        import semapad as semapad_pkg
        snap["version"] = semapad_pkg.version()
        snap["device"] = {
            "phase": 3,
            "connected": bool(getattr(current, "connected", False)),
            "transport": getattr(current, "transport", ""),
            "firmware": getattr(current, "firmware_version", None),
            "layer": self._verified_layer,
            "status_verified": self._verified_epoch is not None,
            "pad_error_code": self.pad_error_code,
            "exclusive": (
                "denied" if getattr(current, "exclusive_requested", False)
                and getattr(current, "exclusive_denied", False)
                else "on" if getattr(current, "exclusive_requested", False)
                else "off"),
            "last_input_result": self.last_input_result,
            "last_input": self.last_input,
            "note": None,
        }
        snap["frontmost"] = {"bundle_id": self.frontmost_id,
                             "owner": self.owner, "error": None}
        snap["processes"] = dict(self._processes_info)
        snap["config"] = {"path": str(self.config_path), "warnings": []}
        snap["causes"] = list(self.causes)
        try:
            self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.snapshot_path.parent,
                                       suffix=".tmp")
            with os.fdopen(fd, "w") as handle:
                json.dump(snap, handle)
            os.replace(tmp, self.snapshot_path)
        except OSError:
            pass

    # --- the tick -----------------------------------------------------------

    def tick(self, now: float) -> None:
        if self._closed:
            return
        import time as time_mod
        self._tick_causes = []
        self._stage_ms: dict[str, float] = {}
        self._had_input = False
        t0 = time_mod.perf_counter()
        self._reload_config()
        built = self._build_view(now)
        self._stage_ms["view"] = round((time_mod.perf_counter() - t0) * 1000, 1)
        self._apply_exclusive(now)
        self._ensure_pad(now)
        self.compositor.ble = \
            getattr(self.pad, "transport", "") == "BLE"

        if self.pad is not None and self._verified_epoch is not None:
            try:
                messages = self.pad.poll_received(self.cfg.poll_ms / 1000.0)
            except Exception:
                self._invalidate_pad(reconnect=True, now=now,
                                     error_code="poll_failed")
            else:
                # Ownership is sampled after the blocking poll: an app switch
                # during it must close the gate before dispatch or painting.
                self._refresh_owner()
                self._handle_messages(messages, now)
                if messages:
                    self._refresh_owner()
        else:
            self._refresh_owner()


        if self._feedback_until is not None and now >= self._feedback_until:
            self._feedback_until = None
            self._flash_light = None
            self.compositor.mark_dirty(keys=False, ambient=True)
            self._cause("input_feedback_restore")
        _paint_t0 = __import__("time").perf_counter()

        if self.pad is not None and self._verified_epoch is not None \
                and self._verified_layer is not None:
            flash = (self._flash_light
                     if self._feedback_until is not None else None)
            for cause in self.compositor.paint(
                    lambda message: self._send(message, now), now,
                    owner=self.owner, layer=self._verified_layer,
                    keys=agent_keys.lights(built),
                    ambient=ambient.light(built, self.owner, self.cfg,
                                          flash=flash)):
                self._cause(cause)

        self._stage_ms["paint"] = round(
            (__import__("time").perf_counter() - _paint_t0) * 1000, 1)
        self.causes = tuple(self._tick_causes)
        if self.causes:
            self.generation += 1
        if self.causes or now >= self._next_snapshot_due:
            self._next_snapshot_due = now + _SNAPSHOT_INTERVAL_SECONDS
            _snap_t0 = __import__("time").perf_counter()
            self._write_snapshot(built, now)
            self._stage_ms["snapshot"] = round(
                (__import__("time").perf_counter() - _snap_t0) * 1000, 1)
        # An open that failed after dispatch has no press left to attach to,
        # so it gets its own line rather than silently contradicting "opened".
        for url, status in input_module.drain_open_failures():
            self._log_event("open_failed_async", status=status,
                            local_id=url.rsplit("/", 1)[-1])

        if self._had_input:
            self._log_event("tick_stages", causes=list(self.causes),
                            transport=getattr(self.pad, "transport", None),
                            **self._stage_ms)

    def _send(self, message: dict, now: float) -> bool:
        current = self.pad
        if current is None:
            return False
        try:
            current.send(message)
            return True
        except Exception:
            self._invalidate_pad(reconnect=True, now=now,
                                 error_code="send_failed")
            return False

    def close(self) -> None:
        """Flush only Claude-owned zones, exactly once (spec §9)."""
        if self._closed:
            return
        self._tick_causes = ["shutdown"]
        # SIGTERM may arrive after an app switch but before the next tick.
        self._refresh_owner()
        self._closed = True
        current, self.pad = self.pad, None
        if current is not None:
            verified_layer_one = (
                getattr(current, "connected", False)
                and getattr(current, "status_verified", False)
                and self._verified_epoch == getattr(current, "epoch", None)
                and self._verified_layer == 1
            )
            turn_off_keys, turn_off_ambient = Compositor.close_flags(
                owner=self.owner, verified_layer_one=verified_layer_one)
            try:
                current.close(turn_off_keys=turn_off_keys,
                              turn_off_ambient=turn_off_ambient)
            except Exception:
                self._set_pad_error("close_failed", clear_status=True)
            else:
                self._set_pad_error(None, clear_status=True)
        self._verified_epoch = self._verified_layer = None
        self.causes = tuple(self._tick_causes)
        self.generation += 1
