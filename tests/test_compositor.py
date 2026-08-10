from __future__ import annotations

from semapad.compositor import KEYS_REFRESH_SECONDS, Compositor
from semapad.config import Config
from semapad.model import LIGHT_OFF, Light

BLUE = Light(0x304FFE)
KEYS = (BLUE,) + (LIGHT_OFF,) * 5
AMBER = Light(0xFF6D00, "solid")


class Sink:
    def __init__(self, ok: bool = True) -> None:
        self.sent: list[dict] = []
        self.ok = ok

    def __call__(self, message: dict) -> bool:
        self.sent.append(message)
        return self.ok

    def methods(self) -> list[str]:
        return [message["m"] for message in self.sent]


def comp(**kwargs) -> Compositor:
    return Compositor(Config(**kwargs))


def paint(c: Compositor, sink: Sink, now: float, *, owner="claude", layer=1,
          keys=KEYS, amb=AMBER):
    return c.paint(sink, now, owner=owner, layer=layer, keys=keys, ambient=amb)


def test_first_paint_writes_both_zones_then_diff_suppresses():
    c, sink = comp(), Sink()
    causes = paint(c, sink, 0.0)
    assert sink.methods() == ["v.oai.thstatus", "v.oai.rgbcfg"]
    assert "paint_keys" in causes and "paint_ambient" in causes
    sink.sent.clear()
    paint(c, sink, 1.0)     # unchanged values, refresh not due
    assert sink.sent == []


def test_key_refresh_rewrites_unconditionally_every_five_seconds():
    c, sink = comp(), Sink()
    paint(c, sink, 0.0)
    sink.sent.clear()
    paint(c, sink, KEYS_REFRESH_SECONDS + 0.1)
    assert sink.methods() == ["v.oai.thstatus"]     # #60: drift self-heals


def test_idle_rewrite_off_suppresses_the_unconditional_refresh():
    c, sink = comp(idle_rewrite="off"), Sink()
    paint(c, sink, 0.0)
    sink.sent.clear()
    paint(c, sink, KEYS_REFRESH_SECONDS + 0.1)
    assert sink.sent == []                          # §11.4: let auto-dim sleep
    # a real change still writes immediately (P2 defence stays)
    paint(c, sink, KEYS_REFRESH_SECONDS + 0.2, keys=(LIGHT_OFF,) * 6)
    assert sink.methods() == ["v.oai.thstatus"]


def test_codex_owner_never_writes_thstatus():
    c, sink = comp(), Sink()
    paint(c, sink, 0.0, owner="codex")
    paint(c, sink, KEYS_REFRESH_SECONDS + 1.0, owner="codex")
    c.note_message({"id": 5, "method": "v.oai.thstatus"}, 20.0,
                   owner="codex", layer_one=True)   # gate: no reclaim for codex
    paint(c, sink, 21.0, owner="codex")
    assert "v.oai.thstatus" not in sink.methods()


def test_owner_none_turns_keys_off():
    c, sink = comp(), Sink()
    paint(c, sink, 0.0, owner="none")
    thstatus = [m for m in sink.sent if m["m"] == "v.oai.thstatus"]
    assert all(entry["e"] == 0 for entry in thstatus[0]["p"])


def test_vendor_ack_reclaims_only_its_zone():
    c, sink = comp(), Sink()
    paint(c, sink, 0.0)
    sink.sent.clear()
    causes = c.note_message({"id": 9, "method": "v.oai.rgbcfg"}, 1.0,
                            owner="claude", layer_one=True)
    assert causes == ["vendor_ambient"]
    causes = paint(c, sink, 1.3)          # past the 200 ms reclaim delay
    assert "reclaim_ambient" in causes
    assert sink.methods() == ["v.oai.rgbcfg"]       # keys untouched


def test_own_null_id_ack_never_schedules_reclaim():
    c = comp()
    assert c.note_message({"id": None, "method": "v.oai.thstatus"}, 1.0,
                          owner="claude", layer_one=True) == []


def test_layer_two_writes_no_zones_with_keep():
    c, sink = comp(), Sink()
    paint(c, sink, 0.0, layer=2)
    assert sink.sent == []


def test_layer_two_off_ambient_writes_once():
    c, sink = comp(layer_underglow="off"), Sink()
    paint(c, sink, 0.0, layer=2)
    assert sink.methods() == ["v.oai.rgbcfg"]
    sink.sent.clear()
    c.mark_dirty()                     # session churn must not re-spam off
    paint(c, sink, 1.0, layer=2)
    paint(c, sink, 2.0, layer=2)
    assert sink.sent == []
    # only an observed vendor ambient write reclaims
    c.note_message({"id": 4, "method": "lights.preview"}, 3.0,
                   owner="claude", layer_one=False)
    paint(c, sink, 3.5, layer=2)
    assert sink.methods() == ["v.oai.rgbcfg"]


def test_key_reclaim_never_carries_across_layers():
    c, sink = comp(), Sink()
    paint(c, sink, 0.0)
    c.note_message({"id": 2, "method": "v.oai.thstatus"}, 1.0,
                   owner="claude", layer_one=True)
    sink.sent.clear()
    paint(c, sink, 1.3, layer=2)       # reclaim due while on layer 2
    assert c.keys_reclaim_due is None
    paint(c, sink, 1.4, layer=1)
    # back on layer 1 no stale forced write beyond the ordinary diff (values
    # unchanged since last layer-1 write -> nothing to send)
    assert "v.oai.thstatus" not in sink.methods()


def test_send_failure_aborts_the_tick_before_the_second_zone():
    c, sink = comp(), Sink(ok=False)
    causes = paint(c, sink, 0.0)
    assert sink.methods() == ["v.oai.thstatus"]     # rgbcfg never attempted
    assert "paint_keys" not in causes
    # recovery: a later paint with a working sink writes both zones
    good = Sink()
    paint(c, good, 1.0)
    assert good.methods() == ["v.oai.thstatus", "v.oai.rgbcfg"]


def test_invalidate_forces_full_repaint():
    c, sink = comp(), Sink()
    paint(c, sink, 0.0)
    sink.sent.clear()
    c.invalidate()                      # epoch/layer change
    paint(c, sink, 1.0)
    assert sink.methods() == ["v.oai.thstatus", "v.oai.rgbcfg"]


def test_set_config_repaints_with_new_values():
    c, sink = comp(), Sink()
    paint(c, sink, 0.0)
    sink.sent.clear()
    c.set_config(Config(underglow_claude=0x123456))
    # both zones repaint: a config change may affect key colours too
    paint(c, sink, 1.0, amb=Light(0x123456, "solid"))
    assert sink.methods() == ["v.oai.thstatus", "v.oai.rgbcfg"]
    assert sink.sent[1]["p"]["ambient"]["c"] == 0x123456


def test_close_flags_matrix():
    flags = Compositor.close_flags
    assert flags(owner="claude", verified_layer_one=True) == (True, True)
    assert flags(owner="codex", verified_layer_one=True) == (False, True)
    assert flags(owner="none", verified_layer_one=True) == (False, True)
    assert flags(owner="claude", verified_layer_one=False) == (False, False)


def test_ble_reclaim_debounces_behind_vendor_burst_with_cap():
    c = comp()
    c.ble = True
    vendor = {"id": 1, "method": "v.oai.thstatus"}
    c.note_message(vendor, 10.0, owner="claude", layer_one=True)
    first_due = c.keys_reclaim_due
    assert first_due == 10.3                 # BLE minimum 300 ms
    c.note_message(vendor, 10.2, owner="claude", layer_one=True)
    assert c.keys_reclaim_due == 10.5        # trailing edge extends
    for t in (10.4, 10.6, 10.8, 10.95):
        c.note_message(vendor, t, owner="claude", layer_one=True)
    assert c.keys_reclaim_due == 11.0        # capped at start + 1 s


def test_usb_reclaim_keeps_first_deadline():
    c = comp()
    vendor = {"id": 1, "method": "v.oai.thstatus"}
    c.note_message(vendor, 10.0, owner="claude", layer_one=True)
    due = c.keys_reclaim_due
    c.note_message(vendor, 10.01, owner="claude", layer_one=True)
    assert c.keys_reclaim_due == due
