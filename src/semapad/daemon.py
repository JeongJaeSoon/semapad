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
from semapad.model import OWNERS
from semapad.sources import conversations, hooks, processes
from semapad.surfaces import agent_keys, ambient

_STATUS_TIMEOUT_SECONDS = 1.0
_RETRY_MAX_SECONDS = 30.0
_FEEDBACK_SECONDS = 0.3
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
        self.pad_error_code: str | None = None
        self.last_status_at: float | None = None
        self.causes: tuple[str, ...] = ()
        self.generation = 0

        self._prev_slots: tuple[str | None, ...] | None = None
        self._feedback_until: float | None = None
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

    @property
    def verified_layer(self) -> int | None:
        return self._verified_layer

    def _cause(self, value: str) -> None:
        if value not in self._tick_causes:
            self._tick_causes.append(value)

    def _set_pad_error(self, code: str | None, *, clear_status: bool = False) -> None:
        changed = code != self.pad_error_code
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
        convs, conv_diags = conversations.scan(self.mapping_dir)
        snapshot = processes.scan(self.sessions_dir)
        live_ids = {session.session_id for session in snapshot.sessions}
        try:
            hooks.prune(self.state_dir,
                        live_ids if snapshot.authoritative else None,
                        self.cfg.ttl_minutes * 60.0, now)
        except Exception:
            pass
        records = hooks.read_all(self.state_dir)

        built = view_module.build(
            conversations=convs, live_cli_ids=live_ids, records=records,
            prev_slots=self._prev_slots, colors=self.cfg.colors,
            working_max_seconds=self.cfg.working_max_seconds, now=now,
            diagnostics=conv_diags + snapshot.diagnostics,
        )
        self._prev_slots = tuple(slot.local_id for slot in built.slots)
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
            self._set_pad_error(error_code, clear_status=clear_status)
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
        try:
            current.discard_hid_inputs()
        except Exception:
            return False
        self._verified_epoch, self._verified_layer = epoch, layer
        self.last_status_at = now
        self._set_pad_error(None)
        self._cause("status")
        self._next_status_due = now + max(0.001, self.cfg.status_poll_ms / 1000.0)
        self._needs_reconnect = False
        self._retry_seconds = 1.0
        self._next_retry_due = now
        if epoch != old_epoch or layer != old_layer or old_layer is None:
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
            self._next_status_due = now + max(0.001,
                                              self.cfg.status_poll_ms / 1000.0)

    def _ensure_pad(self, now: float) -> None:
        if self.pad is None:
            if now < self._next_retry_due:
                return
            try:
                self.pad = self._pad_factory()
            except Exception:
                self.pad = None
            if self.pad is None:
                self._set_pad_error("unavailable", clear_status=True)
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
                    self._set_pad_error("disconnected", clear_status=True)
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

    # --- input & messages ---------------------------------------------------

    def _finish_input(self, outcome: input_module.Outcome, now: float) -> None:
        if outcome.press_seen:
            # The pad lights the whole A-zone itself while a key is down (#60).
            self.compositor.mark_dirty(keys=True, ambient=False)
        if outcome.feedback:
            self._feedback_until = now + _FEEDBACK_SECONDS
            self.compositor.mark_dirty(keys=False, ambient=True)
            self._cause("input_feedback")
        if outcome.result != "pending":
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
            parsed = input_module.parse(message)
            if parsed is None:
                continue
            if parsed == "invalid":
                self._finish_input(input_module.Outcome("ignored_input"), now)
                continue
            # Re-read ownership at dispatch time; an app switch must close the
            # gate before a press can open a conversation.
            self._refresh_owner()
            outcome = self.router.dispatch(
                parsed, now, owner=self.owner, layer_one=self._gate_layer_one(),
                slots=self._prev_slots or (None,) * view_module.KEY_COUNT)
            self._finish_input(outcome, now)

    # --- snapshot (spec §5.1: consumers draw finished values) ---------------

    def _write_snapshot(self, built: view_module.View, now: float) -> None:
        current = self.pad
        snap = view_module.snapshot(
            built, config_fingerprint=self._config_fingerprint, generated_at=now)
        snap["device"] = {
            "phase": 3,
            "connected": bool(getattr(current, "connected", False)),
            "transport": getattr(current, "transport", ""),
            "firmware": getattr(current, "firmware_version", None),
            "layer": self._verified_layer,
            "status_verified": self._verified_epoch is not None,
            "pad_error_code": self.pad_error_code,
            "last_input_result": self.last_input_result,
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
        self._tick_causes = []
        self._reload_config()
        built = self._build_view(now)
        self._ensure_pad(now)

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

        flushed = self.router.flush(now)
        if flushed is not None:
            self._finish_input(flushed, now)

        if self._feedback_until is not None and now >= self._feedback_until:
            self._feedback_until = None
            self.compositor.mark_dirty(keys=False, ambient=True)
            self._cause("input_feedback_restore")

        if self.pad is not None and self._verified_epoch is not None \
                and self._verified_layer is not None:
            feedback = self._feedback_until is not None
            for cause in self.compositor.paint(
                    lambda message: self._send(message, now), now,
                    owner=self.owner, layer=self._verified_layer,
                    keys=agent_keys.lights(built),
                    ambient=ambient.light(built, self.owner, self.cfg,
                                          feedback=feedback)):
                self._cause(cause)

        self.causes = tuple(self._tick_causes)
        if self.causes:
            self.generation += 1
        if self.causes or now >= self._next_snapshot_due:
            self._next_snapshot_due = now + _SNAPSHOT_INTERVAL_SECONDS
            self._write_snapshot(built, now)

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
