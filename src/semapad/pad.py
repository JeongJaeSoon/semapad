"""Vendor HID channel implemented directly on top of macOS IOKit.

The Codex Micro exposes its vendor reports through the same ``IOHIDDevice``
whose primary usage is a keyboard.  Discovery therefore deliberately matches
only the measured vendor and product ids.  Product names and primary usages are
not stable identifiers, and ``IOHIDManagerOpen`` tries to open protected input
collections that this channel does not need.

An IOKit write returning success is not proof that the device understood it.
The framing differs between USB and BLE and a wrongly framed report is silently
dropped.  :meth:`Pad.status` is the delivery proof: it correlates a unique
request id with a ``device.status`` reply.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import itertools
import secrets
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from semapad import protocol


VID, PID, REPORT_ID = 0x303A, 0x8360, 6
VENDOR_ID, PRODUCT_ID = VID, PID

_OUTPUT_REPORT = 1
_UTF8 = 0x08000100
_SINT32 = 3
_INPUT_REPORT_SIZE = 128
_POLL_QUANTUM = 0.05

_VENDOR_METHODS = {"v.oai.rgbcfg", "v.oai.thstatus", "lights.preview"}


class PadError(RuntimeError):
    """Base class for vendor-channel failures."""


class PadUnavailable(PadError):
    """IOKit/CoreFoundation is unavailable on this host."""


class PadTransportError(PadError, ValueError):
    """The device reported a transport whose framing is not known."""


class PadDisconnected(PadError):
    """The device was removed or the connection has already been closed."""


class PadThreadError(PadError):
    """A run-loop operation was attempted from a different thread."""


class PadIOError(PadError):
    """An IOKit operation returned a non-zero IOReturn."""

    def __init__(self, operation: str, code: int) -> None:
        self.operation = operation
        self.code = int(code) & 0xFFFFFFFF
        super().__init__(f"{operation} failed: 0x{self.code:08x}")


def is_vendor_write(message: object) -> bool:
    """Return whether an inbound acknowledgement represents another writer.

    semapad's ``v.oai.*`` writes are notifications and their acknowledgements
    carry a null id.  An id-bearing acknowledgement for a known lighting method
    therefore came from the vendor software (or another request-style writer).
    Key events and our own ``device.status`` replies are not lighting writes.
    """
    if not isinstance(message, dict):
        return False
    request_id = message.get("id")
    method = message.get("method")
    return (type(request_id) is int
            and type(method) is str
            and method in _VENDOR_METHODS)


def _safe_firmware_version(value: object) -> str | None:
    """Keep only a small printable firmware label from untrusted device JSON."""
    if type(value) is not str or not value or len(value) > 128:
        return None
    if any(not character.isprintable() for character in value):
        return None
    return value


@dataclass(frozen=True)
class _AcquiredDevice:
    device: Any
    raw_transport: str | None


@dataclass(frozen=True)
class _Registration:
    """Objects whose lifetime must cover every scheduled run-loop callback."""

    input_buffer: Any
    input_callback: Any
    removal_callback: Any
    run_loop: Any
    mode: Any


@dataclass(frozen=True, slots=True)
class ReceivedMessage:
    """One decoded message plus fixed-shape receive metadata.

    The envelope is immutable and deliberately has no extensible metadata map.
    ``message`` remains the decoded dictionary so :meth:`Pad.poll` can preserve
    its historical ``list[dict]`` contract without copying or reshaping it.
    """

    message: dict
    received_at: float
    connection_epoch: int


@dataclass(frozen=True, slots=True)
class _ReceivedReport:
    report: bytes
    received_at: float
    connection_epoch: int


class _PadBackend(Protocol):
    def acquire(self, vid: int, pid: int) -> _AcquiredDevice | None: ...

    def register(self, device: Any,
                 on_report: Callable[[int, int, bytes], None],
                 on_remove: Callable[[int], None]) -> _Registration: ...

    def set_report(self, device: Any, report_type: int,
                   report_id: int, packet: bytes) -> int: ...

    def pump(self, registration: _Registration, seconds: float) -> None: ...

    def unregister(self, device: Any, registration: _Registration) -> None: ...

    def unschedule(self, device: Any, registration: _Registration) -> None: ...

    def close(self, device: Any) -> int: ...

    def release(self, device: Any) -> None: ...


_REPORT_CALLBACK = ctypes.CFUNCTYPE(
    None,
    ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p,
    ctypes.c_uint32, ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint8), ctypes.c_long,
)
_REMOVAL_CALLBACK = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p,
)


class _IOKitBackend:
    """Small ctypes boundary; importing :mod:`semapad.pad` stays portable."""

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise PadUnavailable("the vendor HID channel is available only on macOS")

        cf_path = ctypes.util.find_library("CoreFoundation")
        iokit_path = ctypes.util.find_library("IOKit")
        if not cf_path or not iokit_path:
            raise PadUnavailable("CoreFoundation or IOKit could not be located")

        try:
            self._cf = ctypes.CDLL(cf_path)
            self._iokit = ctypes.CDLL(iokit_path)
            self._bind()
            self._default_mode = ctypes.c_void_p.in_dll(
                self._cf, "kCFRunLoopDefaultMode")
        except (AttributeError, OSError, ValueError) as error:
            raise PadUnavailable(f"could not bind IOKit: {error}") from error

    def _bind(self) -> None:
        cf, iokit = self._cf, self._iokit

        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32,
        ]
        cf.CFNumberCreate.restype = ctypes.c_void_p
        cf.CFNumberCreate.argtypes = [
            ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p,
        ]
        cf.CFDictionarySetValue.restype = None
        cf.CFDictionarySetValue.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32,
        ]
        cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
        cf.CFRunLoopGetCurrent.argtypes = []
        cf.CFRunLoopRunInMode.restype = ctypes.c_int32
        cf.CFRunLoopRunInMode.argtypes = [
            ctypes.c_void_p, ctypes.c_double, ctypes.c_bool,
        ]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [ctypes.c_void_p]

        iokit.IOServiceMatching.restype = ctypes.c_void_p
        iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
        iokit.IOServiceGetMatchingServices.restype = ctypes.c_int32
        iokit.IOServiceGetMatchingServices.argtypes = [
            ctypes.c_uint32, ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        iokit.IOIteratorNext.restype = ctypes.c_uint32
        iokit.IOIteratorNext.argtypes = [ctypes.c_uint32]
        iokit.IOObjectRelease.restype = ctypes.c_int32
        iokit.IOObjectRelease.argtypes = [ctypes.c_uint32]
        iokit.IOHIDDeviceCreate.restype = ctypes.c_void_p
        iokit.IOHIDDeviceCreate.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32,
        ]
        iokit.IOHIDDeviceGetProperty.restype = ctypes.c_void_p
        iokit.IOHIDDeviceGetProperty.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p,
        ]
        iokit.IOHIDDeviceOpen.restype = ctypes.c_int32
        iokit.IOHIDDeviceOpen.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32,
        ]
        iokit.IOHIDDeviceClose.restype = ctypes.c_int32
        iokit.IOHIDDeviceClose.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32,
        ]
        iokit.IOHIDDeviceSetReport.restype = ctypes.c_int32
        iokit.IOHIDDeviceSetReport.argtypes = [
            ctypes.c_void_p, ctypes.c_int32, ctypes.c_long,
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_long,
        ]
        # Callback arguments are c_void_p so a null callback can explicitly
        # unregister before unscheduling and releasing the Python CFUNCTYPE.
        iokit.IOHIDDeviceRegisterInputReportCallback.restype = None
        iokit.IOHIDDeviceRegisterInputReportCallback.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_long,
            ctypes.c_void_p, ctypes.c_void_p,
        ]
        iokit.IOHIDDeviceRegisterRemovalCallback.restype = None
        iokit.IOHIDDeviceRegisterRemovalCallback.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]
        iokit.IOHIDDeviceScheduleWithRunLoop.restype = None
        iokit.IOHIDDeviceScheduleWithRunLoop.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]
        iokit.IOHIDDeviceUnscheduleFromRunLoop.restype = None
        iokit.IOHIDDeviceUnscheduleFromRunLoop.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
        ]

    def _cf_string(self, value: str) -> int:
        ref = self._cf.CFStringCreateWithCString(
            None, value.encode("utf-8"), _UTF8)
        if not ref:
            raise PadUnavailable(f"could not create CFString for {value!r}")
        return ref

    def _set_matching_int(self, matching: Any, key: str, value: int) -> None:
        key_ref = self._cf_string(key)
        number = ctypes.c_int32(value)
        value_ref = self._cf.CFNumberCreate(None, _SINT32, ctypes.byref(number))
        if not value_ref:
            self._cf.CFRelease(key_ref)
            raise PadUnavailable(f"could not create CFNumber for {key}")
        try:
            self._cf.CFDictionarySetValue(matching, key_ref, value_ref)
        finally:
            # The dictionary retained both objects.  The temporary create refs
            # belong to us and must not survive discovery.
            self._cf.CFRelease(value_ref)
            self._cf.CFRelease(key_ref)

    def _string_property(self, device: Any, key: str) -> str | None:
        key_ref = self._cf_string(key)
        try:
            value_ref = self._iokit.IOHIDDeviceGetProperty(device, key_ref)
        finally:
            self._cf.CFRelease(key_ref)
        if not value_ref:
            return None
        buffer = ctypes.create_string_buffer(512)
        if not self._cf.CFStringGetCString(
                value_ref, buffer, len(buffer), _UTF8):
            return None
        try:
            return buffer.value.decode("utf-8")
        except UnicodeDecodeError:
            return None

    seize = False          # request exclusive access on the next acquire
    seize_denied = False   # last acquire wanted exclusive but ran shared

    def acquire(self, vid: int, pid: int) -> _AcquiredDevice | None:
        """Open the first IOHIDDevice matching exactly VendorID/ProductID."""
        matching = self._iokit.IOServiceMatching(b"IOHIDDevice")
        if not matching:
            return None
        try:
            self._set_matching_int(matching, "VendorID", vid)
            self._set_matching_int(matching, "ProductID", pid)
        except Exception:
            # IOServiceGetMatchingServices has not consumed it yet.
            self._cf.CFRelease(matching)
            raise

        iterator = ctypes.c_uint32()
        # This call consumes matching regardless of the return code.
        result = self._iokit.IOServiceGetMatchingServices(
            0, matching, ctypes.byref(iterator))
        if result != 0:
            return None

        try:
            while True:
                service = self._iokit.IOIteratorNext(iterator.value)
                if not service:
                    return None
                try:
                    device = self._iokit.IOHIDDeviceCreate(None, service)
                finally:
                    self._iokit.IOObjectRelease(service)
                if not device:
                    continue

                try:
                    raw_transport = self._string_property(device, "Transport")
                    options = 1 if self.seize else 0   # kIOHIDOptionsTypeSeizeDevice
                    opened = self._iokit.IOHIDDeviceOpen(device, options)
                    self.seize_denied = False
                    if opened != 0 and options:
                        # Exclusive open refused (kIOReturnNotPrivileged without
                        # an Input Monitoring grant) -- run shared, tell the ui.
                        self.seize_denied = True
                        opened = self._iokit.IOHIDDeviceOpen(device, 0)
                except Exception:
                    self._cf.CFRelease(device)
                    raise
                if opened == 0:
                    return _AcquiredDevice(device, raw_transport)
                self._cf.CFRelease(device)
        finally:
            if iterator.value:
                self._iokit.IOObjectRelease(iterator.value)

    def register(self, device: Any,
                 on_report: Callable[[int, int, bytes], None],
                 on_remove: Callable[[int], None]) -> _Registration:
        input_buffer = (ctypes.c_uint8 * _INPUT_REPORT_SIZE)()

        @_REPORT_CALLBACK
        def input_callback(_context, result, _sender, _report_type,
                           report_id, report, length):
            try:
                if result != 0:
                    on_report(result, report_id, b"")
                elif not report or length < 0 or length > _INPUT_REPORT_SIZE:
                    on_report(-1, report_id, b"")
                else:
                    on_report(0, report_id, bytes(report[:length]))
            except BaseException:
                # Exceptions may not cross a ctypes callback boundary.  The Pad
                # callback itself records errors for the polling thread.
                on_report(-1, report_id, b"")

        @_REMOVAL_CALLBACK
        def removal_callback(_context, result, _sender):
            try:
                on_remove(result)
            except BaseException:
                # Removal already means the connection is unusable; never let a
                # Python traceback escape into CoreFoundation.
                pass

        run_loop = self._cf.CFRunLoopGetCurrent()
        if not run_loop:
            raise PadUnavailable("CFRunLoopGetCurrent returned null")
        registration = _Registration(
            input_buffer=input_buffer,
            input_callback=input_callback,
            removal_callback=removal_callback,
            run_loop=run_loop,
            mode=self._default_mode,
        )

        input_attempted = False
        removal_attempted = False
        schedule_attempted = False
        try:
            # Mark each operation before entering ctypes: a Python-side failure
            # can be raised after the native call already took effect.
            input_attempted = True
            self._iokit.IOHIDDeviceRegisterInputReportCallback(
                device, input_buffer, len(input_buffer),
                ctypes.cast(input_callback, ctypes.c_void_p), None)
            removal_attempted = True
            self._iokit.IOHIDDeviceRegisterRemovalCallback(
                device, ctypes.cast(removal_callback, ctypes.c_void_p), None)
            schedule_attempted = True
            self._iokit.IOHIDDeviceScheduleWithRunLoop(
                device, run_loop, self._default_mode)
        except BaseException:
            # Roll back every possibly completed step without replacing the
            # original registration exception.  IOHID requires the input
            # buffer to remain non-null even when only the callback is cleared.
            if input_attempted:
                try:
                    self._iokit.IOHIDDeviceRegisterInputReportCallback(
                        device, input_buffer, len(input_buffer), None, None)
                except BaseException:
                    pass
            if removal_attempted:
                try:
                    self._iokit.IOHIDDeviceRegisterRemovalCallback(
                        device, None, None)
                except BaseException:
                    pass
            if schedule_attempted:
                try:
                    self._iokit.IOHIDDeviceUnscheduleFromRunLoop(
                        device, run_loop, self._default_mode)
                except BaseException:
                    pass
            raise
        return registration

    def set_report(self, device: Any, report_type: int,
                   report_id: int, packet: bytes) -> int:
        buffer = (ctypes.c_uint8 * len(packet)).from_buffer_copy(packet)
        return int(self._iokit.IOHIDDeviceSetReport(
            device, report_type, report_id, buffer, len(buffer)))

    def pump(self, registration: _Registration, seconds: float) -> None:
        self._cf.CFRunLoopRunInMode(
            registration.mode, max(0.0, seconds), True)

    def unregister(self, device: Any, registration: _Registration) -> None:
        first_error: BaseException | None = None
        try:
            self._iokit.IOHIDDeviceRegisterInputReportCallback(
                device, registration.input_buffer,
                len(registration.input_buffer), None, None)
        except BaseException as error:
            first_error = error
        try:
            self._iokit.IOHIDDeviceRegisterRemovalCallback(device, None, None)
        except BaseException as error:
            if first_error is None:
                first_error = error
        if first_error is not None:
            raise first_error

    def unschedule(self, device: Any, registration: _Registration) -> None:
        self._iokit.IOHIDDeviceUnscheduleFromRunLoop(
            device, registration.run_loop, registration.mode)

    def close(self, device: Any) -> int:
        return int(self._iokit.IOHIDDeviceClose(device, 0))

    def release(self, device: Any) -> None:
        self._cf.CFRelease(device)


_epoch_counter = itertools.count(1)
# A stale/vendor request commonly uses id 1.  Start at a process-random base so
# it cannot accidentally prove the first status request after launch.  Stay in
# the positive signed-32-bit range as well: the firmware has only been measured
# with ordinary integer ids, so a JavaScript-safe 53-bit value is still an
# unnecessary compatibility gamble at the HID boundary.
_request_base = secrets.randbelow((1 << 30) - (1 << 20)) + (1 << 20)
_request_counter = itertools.count(_request_base)
_counter_lock = threading.Lock()


def _next(counter: Any) -> int:
    with _counter_lock:
        return next(counter)


def _next_request_id() -> int:
    request_id = _next(_request_counter)
    if type(request_id) is not int or not (0 < request_id < (1 << 31)):
        raise PadError("request id space exhausted")
    return request_id


def _wrapped_error(operation: str, error: BaseException) -> PadError:
    if isinstance(error, PadError):
        return error
    return PadError(f"{operation} failed: {error}")


class Pad:
    """One scheduled vendor-channel connection.

    Open, schedule, poll, unschedule and close all belong to the thread that
    called :meth:`open`.  ``epoch`` changes for every successful connection,
    while ``status_verified`` becomes true only after a correlated status reply.
    """

    def __init__(self, backend: _PadBackend,
                 clock: Callable[[], float]) -> None:
        self._backend = backend
        self._clock = clock
        self._owner_thread = threading.get_ident()
        self._send_lock = threading.RLock()
        self._device: Any | None = None
        self._registration: _Registration | None = None
        self._decoder = protocol.FrameDecoder()
        self._raw_reports: deque[_ReceivedReport] = deque()
        self._messages: list[ReceivedMessage] = []
        self._callback_error: PadError | None = None
        self._removal_result: int | None = None
        self._connected = False
        self._closed = True
        self._status_verified = False
        self._layer_index: int | None = None
        self._firmware_version: str | None = None
        self.transport = ""
        self.epoch = 0

        # Explicit aliases make the callback/buffer/run-loop lifetime visible
        # and ensure no temporary local is their only Python reference.
        self._input_buffer: Any | None = None
        self._input_callback: Any | None = None
        self._removal_callback: Any | None = None
        self._run_loop: Any | None = None
        self._run_loop_mode: Any | None = None

    @classmethod
    def open(cls, backend: _PadBackend | None = None,
             clock: Callable[[], float] | None = None) -> "Pad | None":
        """Find, open and schedule the exact VID/PID device.

        Absence and ordinary open failure return ``None``.  An unknown raw
        Transport is different: it raises :class:`PadTransportError` after
        closing the device, because silently guessing USB or BLE is unsafe.
        ``backend`` and ``clock`` exist for deterministic hardware-free tests.
        """
        if backend is None:
            try:
                backend = _IOKitBackend()
            except PadUnavailable:
                return None
        instance = cls(backend, clock or time.monotonic)
        try:
            if not instance._connect():
                return None
        except BaseException:
            instance._dispose_connection()
            raise
        return instance

    def set_exclusive(self, flag: bool) -> None:
        """Request (or drop) exclusive access on the next (re)connect."""
        setattr(self._backend, "seize", bool(flag))

    @property
    def exclusive_requested(self) -> bool:
        return bool(getattr(self._backend, "seize", False))

    @property
    def exclusive_denied(self) -> bool:
        return bool(getattr(self._backend, "seize_denied", False))

    @property
    def connected(self) -> bool:
        return self._connected and not self._closed

    @property
    def status_verified(self) -> bool:
        return self._status_verified and self.connected

    @property
    def layer_index(self) -> int | None:
        """The positive layer from the latest verified status response."""
        return self._layer_index if self.status_verified else None

    @property
    def firmware_version(self) -> str | None:
        """A printable firmware label from the latest verified response."""
        return self._firmware_version if self.status_verified else None

    def _invalidate_status(self) -> None:
        self._status_verified = False
        self._layer_index = None
        self._firmware_version = None

    def _assert_owner_thread(self) -> None:
        if threading.get_ident() != self._owner_thread:
            raise PadThreadError(
                "Pad I/O must use the thread/run loop that called Pad.open")

    def _connect(self) -> bool:
        self._assert_owner_thread()
        acquired = self._backend.acquire(VID, PID)
        if acquired is None:
            return False

        self._device = acquired.device
        self._closed = False
        try:
            try:
                normalized = protocol.normalize_transport(acquired.raw_transport)
            except ValueError as error:
                raise PadTransportError(str(error)) from error
            connection_epoch = _next(_epoch_counter)
            registration = self._backend.register(
                acquired.device,
                lambda result, report_id, report: self._on_report(
                    connection_epoch, result, report_id, report),
                lambda result: self._on_remove(connection_epoch, result),
            )
        except BaseException:
            self._connected = False
            self._invalidate_status()
            raise

        self.transport = normalized
        self._registration = registration
        self._input_buffer = registration.input_buffer
        self._input_callback = registration.input_callback
        self._removal_callback = registration.removal_callback
        self._run_loop = registration.run_loop
        self._run_loop_mode = registration.mode
        self._decoder = protocol.FrameDecoder()
        self._raw_reports.clear()
        self._messages.clear()
        self._callback_error = None
        self._removal_result = None
        self._connected = True
        self._closed = False
        self._invalidate_status()
        self.epoch = connection_epoch
        return True

    def _on_report(self, connection_epoch: int, result: int,
                   report_id: int, report: bytes) -> None:
        # This callback is scheduled on the owner run loop.  Still verify it:
        # an accidental backend/thread change must fail closed, not race queues.
        if threading.get_ident() != self._owner_thread:
            self._callback_error = PadThreadError(
                "input callback ran outside the owner run loop")
            self._connected = False
            self._invalidate_status()
            return
        # Registration callbacks close over their connection.  A callback that
        # somehow survives teardown must not contaminate the current decoder.
        if connection_epoch != self.epoch:
            return
        if result != 0:
            self._callback_error = PadIOError("input callback", result)
            self._connected = False
            self._invalidate_status()
            return
        if report_id != REPORT_ID:
            return
        try:
            received_at = float(self._clock())
            self._raw_reports.append(_ReceivedReport(
                report=bytes(report),
                received_at=received_at,
                connection_epoch=connection_epoch,
            ))
        except BaseException as error:
            self._callback_error = _wrapped_error("input callback", error)
            self._connected = False
            self._invalidate_status()

    def _on_remove(self, connection_epoch: int, result: int) -> None:
        # A delayed removal from a disposed registration says nothing about the
        # current device and must not invalidate its connection or status.
        if connection_epoch != self.epoch:
            return
        if threading.get_ident() != self._owner_thread:
            self._callback_error = PadThreadError(
                "removal callback ran outside the owner run loop")
        self._removal_result = int(result) & 0xFFFFFFFF
        self._connected = False
        self._invalidate_status()

    def _raise_callback_error(self) -> None:
        if self._callback_error is not None:
            raise self._callback_error

    def _ensure_writable(self) -> None:
        self._raise_callback_error()
        if not self.connected or self._device is None:
            raise PadDisconnected("Codex Micro is disconnected")

    def send(self, message: dict) -> None:
        """Send one complete JSON message using report id 6.

        The lock covers the entire multi-report frame so two callers cannot
        interleave fragments.  A zero IOReturn means only that IOKit accepted
        the report; callers still need :meth:`status` for delivery proof.
        """
        self._assert_owner_thread()
        self._ensure_writable()
        with self._send_lock:
            for packet in protocol.frame(message, self.transport):
                try:
                    result = self._backend.set_report(
                        self._device, _OUTPUT_REPORT, REPORT_ID, packet)
                except BaseException as error:
                    self._invalidate_status()
                    raise _wrapped_error("IOHIDDeviceSetReport", error) from error
                if result != 0:
                    self._invalidate_status()
                    raise PadIOError("IOHIDDeviceSetReport", result)

    def _drain_reports(self) -> None:
        while self._raw_reports:
            received_report = self._raw_reports.popleft()
            decoded = self._decoder.feed(received_report.report)
            # JSON scalars and arrays are syntactically valid, so the framing
            # decoder quite reasonably returns them.  The vendor protocol is
            # object-shaped, though, and every consumer calls ``.get``.  Drop
            # non-objects at this trust boundary instead of letting malformed
            # device traffic take the daemon down.
            self._messages.extend(
                ReceivedMessage(
                    message=message,
                    received_at=received_report.received_at,
                    connection_epoch=received_report.connection_epoch,
                )
                for message in decoded if isinstance(message, dict)
            )

    def _pump_for(self, seconds: float, *, stop_on_disconnect: bool,
                  surface_callback_error: bool,
                  stop_on_message: bool = False) -> None:
        registration = self._registration
        if registration is None:
            self._drain_reports()
            if surface_callback_error:
                self._raise_callback_error()
            return

        duration = max(0.0, float(seconds))
        deadline = self._clock() + duration
        first = True
        while first or self._clock() < deadline:
            first = False
            if stop_on_disconnect and not self.connected:
                break
            remaining = max(0.0, deadline - self._clock())
            quantum = 0.0 if duration == 0 else min(_POLL_QUANTUM, remaining)
            try:
                self._backend.pump(registration, quantum)
            except BaseException as error:
                self._connected = False
                self._invalidate_status()
                raise _wrapped_error("CFRunLoopRunInMode", error) from error
            self._drain_reports()
            if surface_callback_error:
                self._raise_callback_error()
            if stop_on_message and self._messages:
                # A waiting caller wants latency, not a full window: a key
                # press dispatches within one pump quantum of arriving.
                break
            if duration == 0:
                break

    def poll_received(self, seconds: float) -> list[ReceivedMessage]:
        """Run the CFRunLoop and return messages with callback-time metadata."""
        self._assert_owner_thread()
        if seconds < 0:
            raise ValueError("seconds must be non-negative")
        self._drain_reports()
        self._raise_callback_error()
        self._pump_for(
            seconds, stop_on_disconnect=True, surface_callback_error=True,
            stop_on_message=True)
        messages, self._messages = self._messages, []
        return messages

    def poll(self, seconds: float) -> list[dict]:
        """Run the owner's CFRunLoop and return decoded dictionaries."""
        return [received.message for received in self.poll_received(seconds)]

    def discard_hid_inputs(self) -> int:
        """Drop key events queued before a daemon status-verification boundary.

        Status requests pump the run loop, so input received while the layer is
        unknown can otherwise remain queued and replay after the gate re-arms.
        Non-input protocol messages stay queued for normal ACK handling.
        """
        self._assert_owner_thread()
        self._drain_reports()
        before = len(self._messages)
        self._messages = [
            received for received in self._messages
            if received.message.get("m") != "v.oai.hid"
        ]
        return before - len(self._messages)

    @staticmethod
    def _matches_reply(message: dict, request_id: int,
                       method: str | None) -> bool:
        reply_id = message.get("id")
        if type(reply_id) is not int or reply_id != request_id:
            return False
        if method is None:
            return True
        reply_method = message.get("method")
        return type(reply_method) is str and reply_method == method

    def _take_reply(self, request_id: int, method: str | None) -> dict | None:
        for index, received in enumerate(self._messages):
            message = received.message
            if self._matches_reply(message, request_id, method):
                self._messages.pop(index)
                return message
        return None

    def request(self, message: dict, timeout: float = 3.0) -> dict | None:
        """Send a request with a fresh id and wait only for its own reply.

        Unrelated input remains queued for the next :meth:`poll`; it is never
        mistaken for proof that this request arrived.
        """
        self._assert_owner_thread()
        if timeout < 0:
            raise ValueError("timeout must be non-negative")
        if not self.connected:
            return None

        request_id = _next_request_id()
        request = dict(message)
        request["id"] = request_id
        method = request.get("m")
        self.send(request)

        deadline = self._clock() + timeout
        self._drain_reports()
        reply = self._take_reply(request_id, method)
        first = True
        while reply is None and self.connected \
                and (first or self._clock() < deadline):
            first = False
            remaining = max(0.0, deadline - self._clock())
            quantum = 0.0 if timeout == 0 else min(_POLL_QUANTUM, remaining)
            self._pump_for(
                quantum, stop_on_disconnect=True,
                surface_callback_error=True)
            reply = self._take_reply(request_id, method)
            if timeout == 0:
                break
        return reply

    def status(self, timeout: float = 3.0) -> dict | None:
        """Round-trip ``device.status``, the proof that framing is correct."""
        self._assert_owner_thread()
        self._invalidate_status()
        if not self.connected:
            return None
        reply = self.request(protocol.status_request(), timeout=timeout)
        if (reply is None or not isinstance(reply.get("result"), dict)
                or reply.get("method") != "device.status"):
            return None
        result = reply["result"]
        layer_index = result.get("layer_index")
        if type(layer_index) is not int or layer_index < 1:
            return None
        self._layer_index = layer_index
        self._firmware_version = _safe_firmware_version(result.get("version"))
        self._status_verified = True
        return reply

    def reconnect(self, timeout: float = 3.0) -> bool:
        """Dispose the old epoch, reopen, and arm only after status succeeds."""
        self._assert_owner_thread()
        cleanup_error = self._dispose_connection()
        if cleanup_error is not None:
            raise cleanup_error
        try:
            if not self._connect():
                return False
            if self.status(timeout=timeout) is not None:
                return True
        except BaseException:
            self._dispose_connection()
            raise
        self._dispose_connection()
        return False

    def _clear_registration_references(self) -> None:
        self._registration = None
        self._input_buffer = None
        self._input_callback = None
        self._removal_callback = None
        self._run_loop = None
        self._run_loop_mode = None

    def _dispose_connection(self) -> PadError | None:
        """Unregister, unschedule, close and release; always run every step."""
        registration, device = self._registration, self._device
        first_error: PadError | None = None

        def capture(operation: str, action: Callable[[], Any]) -> Any:
            nonlocal first_error
            try:
                return action()
            except BaseException as error:
                if first_error is None:
                    first_error = _wrapped_error(operation, error)
                return None

        try:
            if registration is not None and device is not None:
                capture("callback unregister", lambda: self._backend.unregister(
                    device, registration))
                capture("run-loop unschedule", lambda: self._backend.unschedule(
                    device, registration))
            if device is not None:
                close_result = capture(
                    "IOHIDDeviceClose", lambda: self._backend.close(device))
                if close_result not in (None, 0) and first_error is None:
                    first_error = PadIOError("IOHIDDeviceClose", close_result)
                capture("CFRelease", lambda: self._backend.release(device))
        finally:
            self._connected = False
            self._closed = True
            self._invalidate_status()
            self._device = None
            self._clear_registration_references()
        return first_error

    def close(self, flush_seconds: float = 1.0, *,
              turn_off_keys: bool = True,
              turn_off_ambient: bool = True) -> None:
        """Optionally turn off owned zones, flush, then dispose exactly once.

        The daemon passes zone ownership explicitly on shutdown.  Defaults keep
        direct/context-manager use backward compatible by clearing both zones.
        """
        self._assert_owner_thread()
        if flush_seconds < 0:
            raise ValueError("flush_seconds must be non-negative")
        if self._closed and self._device is None:
            return

        first_error: PadError | None = None

        def attempt(operation: str, action: Callable[[], Any]) -> None:
            nonlocal first_error
            try:
                action()
            except BaseException as error:
                if first_error is None:
                    first_error = _wrapped_error(operation, error)

        try:
            if self.connected:
                # Keep these as separate attempts: a failed key write must not
                # prevent the ambient-off write from being submitted.
                if turn_off_keys:
                    attempt("key-off write", lambda: self.send(
                        protocol.thstatus([None] * 6)))
                if turn_off_ambient:
                    attempt("ambient-off write", lambda: self.send(
                        protocol.rgbcfg(ambient=None)))
            if self._registration is not None:
                attempt("close flush", lambda: self._pump_for(
                    flush_seconds, stop_on_disconnect=False,
                    surface_callback_error=False))
        finally:
            cleanup_error = self._dispose_connection()
            if first_error is None:
                first_error = cleanup_error
        if first_error is not None:
            raise first_error

    def __enter__(self) -> "Pad":
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        try:
            self.close()
        except PadError:
            if exc_type is None:
                raise
        return False


def open_pad() -> Pad | None:
    """Backward-compatible spelling retained for the original phase-one API."""
    return Pad.open()
