"""KeyRouter -- physical key input to deep links, behind the owner gate (spec §7).

A valid press opens the conversation immediately, foreground. The vendor's
single-tap-background / double-tap-raise split was tried and dropped
(2026-08-10 acceptance): the claude:// deeplink makes Desktop raise itself, so
a "background" open does not exist for us -- the tap window added 350 ms of
lag for a distinction the platform cannot deliver. Research continues in
semapad#12; if a no-activate path is found, the window logic lives in git
history.
"""
from __future__ import annotations

import queue
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from semapad import deeplink

KEY_NAMES = {f"AG{i:02d}": i for i in range(6)}
_OPEN_TIMEOUT_SECONDS = 5.0

#: Terminal dispositions surfaced as ``last_input_result`` (spec §5.2 D).
RESULTS = frozenset({
    "opened", "open_failed", "empty_slot",
    "ignored_owner", "ignored_layer", "ignored_input",
})


class _LSOpener:
    """Prewarmed in-process LaunchServices opener (Codex recommendation #1).

    A warm LSOpenCFURLRef returns in ~0.2 ms versus ~1.4 ms just to spawn
    /usr/bin/open (which itself exits ~70 ms later). The first lookup is
    cold (~90 ms), so a worker thread binds and prewarms off the HID path
    and every press afterwards is a queue put. Any failure permanently
    falls back to the Popen path.
    """

    _ENCODING_UTF8 = 0x08000100

    def __init__(self) -> None:
        import queue
        import threading
        self._queue: "queue.SimpleQueue[str]" = queue.SimpleQueue()
        self.broken = False
        self._thread = threading.Thread(
            target=self._run, name="semapad-lsopen", daemon=True)
        self._thread.start()

    def open(self, url: str) -> None:
        self._queue.put(url)

    def _run(self) -> None:
        try:
            import ctypes
            import ctypes.util
            cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
            cs = ctypes.CDLL(
                "/System/Library/Frameworks/CoreServices.framework/CoreServices")
            cf.CFURLCreateWithBytes.restype = ctypes.c_void_p
            cf.CFURLCreateWithBytes.argtypes = [
                ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long,
                ctypes.c_uint32, ctypes.c_void_p]
            cf.CFRelease.argtypes = [ctypes.c_void_p]
            cs.LSOpenCFURLRef.restype = ctypes.c_int32
            cs.LSOpenCFURLRef.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

            def make_url(text: str):
                raw = text.encode()
                return cf.CFURLCreateWithBytes(
                    None, raw, len(raw), self._ENCODING_UTF8, None)

            # Prewarm: build and release one URL so the cold path (~90 ms
            # measured) happens here, not on the first key press.
            warm = make_url("claude://claude.ai/epitaxy/prewarm")
            if warm:
                cf.CFRelease(warm)
        except Exception:
            self.broken = True
            return
        while True:
            url = self._queue.get()
            try:
                ref = make_url(url)
                if not ref:
                    continue
                try:
                    status = cs.LSOpenCFURLRef(ref, None)
                    if status != 0:      # noErr; anything else opened nothing
                        note_open_failure(url, status)
                finally:
                    cf.CFRelease(ref)
            except Exception:
                self.broken = True
                return


_ls_opener: _LSOpener | None = None
_children: list[tuple[Any, str]] = []   # fire-and-forget opens awaiting reaping

#: Opens are fire-and-forget, so a press is dispatched long before macOS says
#: whether it worked. The verdict arrives later, on another thread, and would
#: otherwise be dropped -- leaving "opened" unfalsifiable and a dead key
#: invisible in the log. Failures land here; the daemon drains them per tick.
_open_failures: "queue.SimpleQueue[tuple[str, int]]" = queue.SimpleQueue()


def note_open_failure(url: str, status: int) -> None:
    _open_failures.put((url, int(status)))


def drain_open_failures(limit: int = 8) -> list[tuple[str, int]]:
    """Take up to *limit* pending failures. Bounded: a wedged LaunchServices
    must not turn one tick into thousands of log lines."""
    out: list[tuple[str, int]] = []
    while len(out) < limit:
        try:
            out.append(_open_failures.get_nowait())
        except queue.Empty:
            break
    return out


def _reap_children() -> None:
    """Reap finished opens, recording the ones that failed.

    ``/usr/bin/open`` exits non-zero when nothing handles the URL -- the same
    silent failure as a bad OSStatus on the LaunchServices path.
    """
    still_running = []
    for child, url in _children:
        code = child.poll()
        if code is None:
            still_running.append((child, url))
        elif code != 0:
            note_open_failure(url, code)
    _children[:] = still_running


def open_local(local_id: str,
               spawner: Callable[..., object] = subprocess.Popen) -> bool:
    """Open a conversation by its own local_id -- dead conversations included.

    Fire-and-forget: /usr/bin/open can block for hundreds of milliseconds
    waiting on LaunchServices, and a synchronous wait both delayed the switch
    and serialized rapid presses behind each other. The URL is validated
    before spawning, so a successful spawn is the success signal.
    """
    global _ls_opener
    try:
        url = deeplink.url_for(local_id)
    except ValueError:
        return False
    if spawner is subprocess.Popen:          # production path only
        if _ls_opener is None:
            _ls_opener = _LSOpener()
        if not _ls_opener.broken:
            _ls_opener.open(url)
            return True
    _reap_children()
    try:
        child = spawner(["/usr/bin/open", url],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL)
    except OSError:
        return False
    _children.append((child, url))
    return True


@dataclass(frozen=True)
class Press:
    """One validated A-key press. Anything else never reaches dispatch."""

    key_index: int


@dataclass(frozen=True)
class Outcome:
    result: str
    feedback: bool = False       # brief fault flash on the border (P6)
    press_seen: bool = False     # the pad self-paints on any press (#60)


def parse(message: object) -> Press | None | str:
    """Return a Press, ``None``, ``"release"``, or ``"invalid"``.

    A release (act 0 on a known key) is expected traffic, not noise: it must
    never overwrite the last meaningful press in the ui, so it gets its own
    disposition instead of "invalid".
    """
    if not isinstance(message, dict) or message.get("m") != "v.oai.hid":
        return None
    params = message.get("p")
    key = params.get("k") if isinstance(params, dict) else None
    action = params.get("act") if isinstance(params, dict) else None
    index = KEY_NAMES.get(key) if type(key) is str else None
    if index is None or isinstance(action, bool):
        return "invalid"
    if action == 0:
        return "release"
    if action != 1:
        return "invalid"
    return Press(key_index=index)


class KeyRouter:
    """Dispatch validated presses through the owner and layer gates."""

    def __init__(self, opener: Callable[[str], bool] | None = None) -> None:
        self._opener = opener if opener is not None else open_local

    def dispatch(self, press: Press, now: float, *, owner: str,
                 layer_one: bool, slots: Sequence[str | None]) -> Outcome:
        """Route one validated press.

        Every outcome here has ``press_seen`` -- the pad lights the whole
        A-zone by itself while a key is down, so any physical press must dirty
        the keys regardless of what the gate decided (#60).
        """
        if owner != "claude":
            return Outcome("ignored_owner", press_seen=True)
        if not layer_one:
            return Outcome("ignored_layer", press_seen=True)
        local_id = slots[press.key_index] if press.key_index < len(slots) else None
        if local_id is None:
            return Outcome("empty_slot", feedback=True, press_seen=True)
        try:
            opened = bool(self._opener(local_id))
        except Exception:
            opened = False
        if opened:
            return Outcome("opened", press_seen=True)
        return Outcome("open_failed", feedback=True, press_seen=True)
