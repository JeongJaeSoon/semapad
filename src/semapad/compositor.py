"""The only pad write path (spec §7).

Everything that decides whether a desired light actually reaches the device
lives here: zone ownership, the layer gate, value diffing, vendor-write
reclaim (P13), the unconditional slow key rewrite (#60), and what may be
flushed on shutdown. Nothing else in semapad calls ``pad.send``.

State is kept in the exact protocol value space (key colour tuple / ambient
tuple-or-None) so "did it change" means "would the wire bytes change".
"""
from __future__ import annotations

from collections.abc import Callable, Sequence

from semapad import pad as pad_module
from semapad import protocol
from semapad.config import Config
from semapad.model import Light

#: We cannot read the pad's key colours back, so drift is invisible to us. A
#: slow unconditional rewrite is what makes it heal on its own (#60).
KEYS_REFRESH_SECONDS = 5.0
#: On BLE every write consumes shared connection airtime (#49): the healing
#: rewrite drops to a slow safety net and reclaims debounce behind the
#: vendor's burst instead of racing it.
BLE_KEYS_REFRESH_SECONDS = 60.0
BLE_RECLAIM_MIN_SECONDS = 0.3
BLE_RECLAIM_CAP_SECONDS = 1.0

_UNSET = object()

KEY_COUNT = 6


def _keys_value(lights: Sequence[Light]) -> tuple[int | None, ...]:
    return tuple(None if light.off else light.colour for light in lights)


def _ambient_value(light: Light) -> int | None | tuple[int, str]:
    if light.off:
        return None
    return (light.colour, light.effect)


class Compositor:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.ble = False   # set by the daemon from the pad transport
        self.keys_reclaim_due: float | None = None
        self.ambient_reclaim_due: float | None = None
        self._keys_reclaim_start: float | None = None
        self._ambient_reclaim_start: float | None = None
        self._last_keys: object = _UNSET
        self._last_ambient: object = _UNSET
        self._dirty_keys = True
        self._dirty_ambient = True
        self._next_keys_refresh_due = 0.0

    def set_config(self, cfg: Config) -> None:
        """Apply-on-save (spec §11.3): new colours must repaint next tick."""
        self.cfg = cfg
        self.mark_dirty()

    def mark_dirty(self, *, keys: bool = True, ambient: bool = True) -> None:
        self._dirty_keys = self._dirty_keys or keys
        self._dirty_ambient = self._dirty_ambient or ambient

    def invalidate(self) -> None:
        """Forget everything written: new epoch or layer needs a full repaint."""
        self._last_keys = _UNSET
        self._last_ambient = _UNSET
        self._dirty_keys = True
        self._dirty_ambient = True

    def note_message(self, message: object, now: float, *, owner: str,
                     layer_one: bool) -> list[str]:
        """Schedule zone reclaims for an observed vendor lighting write (P13)."""
        if not pad_module.is_vendor_write(message):
            return []
        causes: list[str] = []
        method = message.get("method")   # type: ignore[union-attr]
        delay = self.cfg.reclaim_delay_ms / 1000.0
        if self.ble:
            delay = max(delay, BLE_RECLAIM_MIN_SECONDS)
        if method in {"v.oai.rgbcfg", "lights.preview"}:
            if self.ambient_reclaim_due is None:
                self._ambient_reclaim_start = now
                self.ambient_reclaim_due = now + delay
                causes.append("vendor_ambient")
            elif self.ble and self._ambient_reclaim_start is not None:
                # Trailing debounce: wait out the vendor's burst, capped.
                self.ambient_reclaim_due = min(
                    now + delay,
                    self._ambient_reclaim_start + BLE_RECLAIM_CAP_SECONDS)
        elif method == "v.oai.thstatus" and owner == "claude" and layer_one:
            if self.keys_reclaim_due is None:
                self._keys_reclaim_start = now
                self.keys_reclaim_due = now + delay
                causes.append("vendor_keys")
            elif self.ble and self._keys_reclaim_start is not None:
                self.keys_reclaim_due = min(
                    now + delay,
                    self._keys_reclaim_start + BLE_RECLAIM_CAP_SECONDS)
        return causes

    def paint(self, send: Callable[[dict], bool], now: float, *, owner: str,
              layer: int, keys: Sequence[Light], ambient: Light) -> list[str]:
        """Write whatever the device should now show. ``send`` returning False
        aborts the tick -- a failed first zone must not be followed by the
        second (proven ordering from the old daemon)."""
        causes: list[str] = []

        force_keys = self.keys_reclaim_due is not None \
            and now >= self.keys_reclaim_due
        force_ambient = self.ambient_reclaim_due is not None \
            and now >= self.ambient_reclaim_due
        if force_keys:
            self.keys_reclaim_due = None
            causes.append("reclaim_keys")
        if force_ambient:
            self.ambient_reclaim_due = None
            causes.append("reclaim_ambient")

        if layer == 1:
            if now >= self._next_keys_refresh_due:
                self._next_keys_refresh_due = now + (
                    BLE_KEYS_REFRESH_SECONDS if self.ble
                    else KEYS_REFRESH_SECONDS)
                # §11.4: with idle_rewrite off, an unchanged board skips the
                # unconditional rewrite so the vendor auto-dim can reach sleep.
                if self.cfg.idle_rewrite == "on":
                    force_keys = True
                    causes.append("keys_refresh")

            desired_keys: tuple[int | None, ...] | None
            if owner == "claude":
                desired_keys = _keys_value(keys)
            elif owner == "none":
                desired_keys = (None,) * KEY_COUNT
            else:
                # Codex owns the A-zone. Even a one-shot off write destroys its
                # display, so yielding means zero thstatus writes.
                desired_keys = None

            if desired_keys is not None and (force_keys or self._dirty_keys
                                             or desired_keys != self._last_keys):
                if not send(protocol.thstatus(list(desired_keys))):
                    return causes
                self._last_keys = desired_keys
                self._dirty_keys = False
                causes.append("paint_keys")
            elif owner == "codex":
                self._dirty_keys = False
        elif force_keys:
            # Never carry a key reclaim across a layer where the A-zone is not ours.
            self.keys_reclaim_due = None

        if layer != 1:
            # "keep" retains the physical border without touching this layer.
            if self.cfg.layer_underglow == "keep":
                return causes
            # "off" writes once on entry. Owner/session changes may dirty the
            # layer-one desired border, but must not spam duplicate off writes.
            # An observed vendor ambient write is the one reason to reclaim.
            needs_off = (force_ambient or self._last_ambient is _UNSET
                         or self._last_ambient is not None)
            if needs_off:
                if not send(protocol.rgbcfg(ambient=None)):
                    return causes
                self._last_ambient = None
                causes.append("paint_ambient")
            self._dirty_ambient = False
            return causes

        desired_ambient = _ambient_value(ambient)
        if force_ambient or self._dirty_ambient \
                or desired_ambient != self._last_ambient:
            if send(protocol.rgbcfg(ambient=desired_ambient)):
                self._last_ambient = desired_ambient
                self._dirty_ambient = False
                causes.append("paint_ambient")
        return causes

    @staticmethod
    def close_flags(*, owner: str, verified_layer_one: bool) -> tuple[bool, bool]:
        """What Pad.close may flush: only Claude-owned zones (spec §9)."""
        return (verified_layer_one and owner == "claude", verified_layer_one)
