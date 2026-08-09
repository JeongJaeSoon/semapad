"""Read the frontmost macOS application's bundle identifier without TCC.

``NSWorkspace`` exposes this metadata without Accessibility or Screen Recording
permission.  semapad intentionally binds the tiny Objective-C surface through
``ctypes`` so the foreground daemon keeps its standard-library-only footprint.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import sys
import threading
from collections.abc import Callable
from typing import Protocol


class FrontmostUnavailable(RuntimeError):
    """The native workspace API could not be bound or queried safely."""


class _Runtime(Protocol):
    def bundle_id(self) -> str | None: ...


class _ObjCRuntime:
    """Minimal, zero-argument Objective-C message bridge for NSWorkspace."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise FrontmostUnavailable("frontmost application lookup requires macOS")

        appkit_path = ctypes.util.find_library("AppKit")
        objc_path = ctypes.util.find_library("objc")
        if not appkit_path or not objc_path:
            raise FrontmostUnavailable("AppKit or Objective-C runtime was not found")

        try:
            # Loading AppKit registers NSWorkspace and NSRunningApplication.
            self._appkit = ctypes.CDLL(appkit_path)
            self._objc = ctypes.CDLL(objc_path)
            self._objc.objc_getClass.restype = ctypes.c_void_p
            self._objc.objc_getClass.argtypes = [ctypes.c_char_p]
            self._objc.sel_registerName.restype = ctypes.c_void_p
            self._objc.sel_registerName.argtypes = [ctypes.c_char_p]
            self._objc.objc_autoreleasePoolPush.restype = ctypes.c_void_p
            self._objc.objc_autoreleasePoolPush.argtypes = []
            self._objc.objc_autoreleasePoolPop.restype = None
            self._objc.objc_autoreleasePoolPop.argtypes = [ctypes.c_void_p]
            self._message = self._objc.objc_msgSend
            self._message.restype = ctypes.c_void_p
            self._message.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        except (AttributeError, OSError, TypeError, ValueError) as error:
            raise FrontmostUnavailable("could not bind NSWorkspace") from error

        self._workspace_class = self._class(b"NSWorkspace")
        self._shared_workspace = self._selector(b"sharedWorkspace")
        self._frontmost_application = self._selector(b"frontmostApplication")
        self._bundle_identifier = self._selector(b"bundleIdentifier")
        self._utf8_string = self._selector(b"UTF8String")

    def _class(self, name: bytes) -> int:
        value = self._objc.objc_getClass(name)
        if not value:
            raise FrontmostUnavailable("NSWorkspace class is unavailable")
        return int(value)

    def _selector(self, name: bytes) -> int:
        value = self._objc.sel_registerName(name)
        if not value:
            raise FrontmostUnavailable("an NSWorkspace selector is unavailable")
        return int(value)

    def _send(self, receiver: int | None, selector: int) -> int | None:
        if not receiver:
            return None
        value = self._message(
            ctypes.c_void_p(receiver), ctypes.c_void_p(selector))
        return int(value) if value else None

    def bundle_id(self) -> str | None:
        # A long-running Python process does not otherwise establish a Cocoa
        # autorelease pool.  Push one per lookup so NSRunningApplication and
        # NSString temporaries cannot accumulate for the lifetime of the daemon.
        pool = self._objc.objc_autoreleasePoolPush()
        try:
            workspace = self._send(self._workspace_class, self._shared_workspace)
            if workspace is None:
                raise FrontmostUnavailable("NSWorkspace.sharedWorkspace returned nil")
            application = self._send(workspace, self._frontmost_application)
            if application is None:
                # This is possible in a non-Aqua/headless login session.  It is
                # not a guessed owner: the daemon retains its previous owner.
                return None
            identifier = self._send(application, self._bundle_identifier)
            if identifier is None:
                return None
            pointer = self._send(identifier, self._utf8_string)
            if pointer is None:
                return None
            raw = ctypes.cast(pointer, ctypes.c_char_p).value
            value = raw.decode("utf-8") if raw is not None else ""
        except (UnicodeDecodeError, ValueError) as error:
            raise FrontmostUnavailable("bundle identifier was not UTF-8") from error
        finally:
            self._objc.objc_autoreleasePoolPop(pool)
        return value or None


_runtime: _Runtime | None = None
_runtime_lock = threading.Lock()


def bundle_id(
    runtime_factory: Callable[[], _Runtime] | None = None,
) -> str | None:
    """Return the active app bundle ID, lazily binding AppKit on first use.

    ``runtime_factory`` is an injection seam for deterministic tests.  Injected
    runtimes are intentionally not cached into the process-wide native runtime.
    """
    if runtime_factory is not None:
        return runtime_factory().bundle_id()

    global _runtime
    with _runtime_lock:
        if _runtime is None:
            _runtime = _ObjCRuntime()
        runtime = _runtime
    return runtime.bundle_id()
