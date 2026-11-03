"""Thin ctypes layer over Canon's EDSDK. FFI only -- no policy.

Everything here was verified on a real EOS 60D (brief 16). Keep it dumb: it
exposes EDSDK calls and the constants they need, and nothing about *what* the
application wants to do. Protocol logic lives in :mod:`ministar.camera.edsdk`,
so it can be tested against a fake object implementing the same handful of
methods.

Brief 08 anticipated forking ``pyedsdk``. That remains the right move if more of
the API surface is ever needed (``EdsGetPropertyDesc``, capture download,
event handling). This module covers the verified subset and nothing else.

Constants transcribed from Canon's ``EdsTypes.h`` and cross-checked against
``Canon.Eos.Framework``'s C# bindings, which agree exactly.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import threading
import time
from ctypes import (
    CFUNCTYPE,
    POINTER,
    Structure,
    byref,
    c_char,
    c_char_p,
    c_int32,
    c_uint32,
    c_uint64,
    c_void_p,
)
from dataclasses import dataclass
from pathlib import Path

EDS_MAX_NAME = 256

#: Where *our* copy lives once installed.
#:
#: Licensing, as settled by brief 08 §4 plus Canon's terms: the unmodified DLL (and
#: headers) may be distributed inside an application without extra permission. What
#: is *not* allowed is shipping the whole SDK, or shipping modified files -- so the
#: copy here is byte-for-byte Canon's, and it stays out of git simply because a
#: proprietary 1.6 MB binary does not belong in the tree (the installer places it).
_OWN = Path(os.environ.get("MINISTAR_HOME", r"C:\ministar")) / "tools" / "edsdk" / "EDSDK.dll"

#: Other applications that ship the SDK. Development only -- see resolve_dll().
_NINA = r"C:\Program Files\N.I.N.A. - Nighttime Imaging 'N' Astronomy"
BORROWED_DLLS = (
    Path(_NINA + r"\External\x64\Canon\EDSDK.dll"),
    Path(r"C:\Program Files\Adobe\Adobe Lightroom Classic\EDSDK.dll"),
    Path(r"C:\Program Files\ON1\ON1 Photo RAW 2026\EDSDK.dll"),
)

DLL_CANDIDATES = (_OWN, *BORROWED_DLLS)

# -- property IDs ------------------------------------------------------------
PROP_PRODUCT_NAME = 0x00000002
PROP_SAVE_TO = 0x0000000B
#: 0x0000000D is CurrentFolder; reading it as UInt32 returns 0x64 (brief 16 §6.1).
PROP_ISO_SPEED = 0x00000402
PROP_AV = 0x00000405
PROP_TV = 0x00000406
PROP_AVAILABLE_SHOTS = 0x0000040A
PROP_LENS_NAME = 0x0000040D
PROP_EVF_OUTPUT_DEVICE = 0x00000500
#: ⚠️ **Never write this one.** ``Evf_Mode`` is the legacy "turn live view on" switch. On the
#: EOS 60D ``EdsSetPropertyData(kEdsPropID_Evf_Mode, 1)`` has been observed to **never
#: return** -- not ``DEVICE_BUSY``, no code at all, both the pump and ``EdsCloseSession``
#: parked inside the DLL (2026-10-28, two sessions). Brief 16 §4 also records the same write
#: *succeeding* a day earlier through a different binding, so it is intermittent rather than
#: always fatal; either way it is not needed. ``Evf_OutputDevice = PC`` alone starts live
#: view on this body in 0.06 s. The constant stays as the name of a trap.
PROP_EVF_MODE = 0x00000501

# -- commands ----------------------------------------------------------------
CMD_TAKE_PICTURE = 0x00000000
CMD_EXTEND_SHUTDOWN_TIMER = 0x00000001
CMD_DRIVE_LENS_EVF = 0x00000103

# -- drive lens --------------------------------------------------------------
NEAR_1, NEAR_2, NEAR_3 = 0x00000001, 0x00000002, 0x00000003
FAR_1, FAR_2, FAR_3 = 0x00008001, 0x00008002, 0x00008003

#: Far is Near with bit 0x8000 set; that is the whole asymmetry.
NEAR_STEPS = (NEAR_1, NEAR_2, NEAR_3)
FAR_STEPS = (FAR_1, FAR_2, FAR_3)

#: ``kEdsEvfOutputDevice``. 0 means "nobody": the stream stops and a DSLR drops its mirror,
#: which is the real imaging state. PC alone is enough to start live view (16 §4).
EVF_OUTPUT_OFF = 0x00000000
EVF_OUTPUT_PC = 0x00000002

#: Codes that mean "not ready yet" rather than "failed".
#:
#: Measured on the EOS 60D: after enabling the EVF output device, the first
#: ``EdsDownloadEvfImage`` returns 0xA102 for roughly 0.85 s (17 attempts at
#: 50 ms) before a frame exists. Treating it as fatal makes healthy live view
#: look broken -- so ``EdsDownloadEvfImage`` must never be called single-shot.
#: 0x81 (DEVICE_BUSY) appears when focus pulses arrive faster than the body
#: consumes them.
RETRYABLE_EVF = frozenset({0x000000A0, 0x0000A102, 0x00000081})

# -- process-wide SDK lifecycle ----------------------------------------------
#:
#: ``EdsInitializeSDK`` / ``EdsTerminateSDK`` are **per process**, not per instance. Calling
#: initialise twice is not harmless: measured on the EOS 60D, the second call returns
#: ``0x00000002`` (EDS_ERR_INTERNAL_ERROR) and *every* later attempt in that process fails the
#: same way -- which is what the reconnect button showed as "initialization failed". So the SDK
#: is reference-counted here, and a second ``Edsdk`` in one process reuses what is already up
#: rather than initialising over it.
#:
#: The count is deliberately not reset on failure to terminate: a leaked instance that never
#: closed leaves the SDK initialised, which is harmless (it exits with the process) and far
#: better than making the process unable to talk to a camera at all.
_SDK_LOCK = threading.Lock()
_SDK_USERS = 0


def _sdk_users() -> int:
    """How many live ``Edsdk`` instances hold the process-wide initialisation."""
    return _SDK_USERS


# -- still capture -----------------------------------------------------------
#: ``kEdsSaveTo_*``. Host means the body does not keep a copy; Both keeps a card
#: copy as insurance against a host crash (02 §1.8).
SAVE_TO_CAMERA = 0x00000001
SAVE_TO_HOST = 0x00000002
SAVE_TO_BOTH = 0x00000003

#: ``kEdsFileCreateDisposition_*`` / ``kEdsAccess_*`` for ``EdsCreateFileStream``.
FILE_CREATE_ALWAYS = 0x00000001
ACCESS_READ_WRITE = 0x00000002

#: ``kEdsObjectEvent_DirItemRequestTransfer``: the body is asking us to take a file.
#: Every such item must be downloaded *or* cancelled, or the camera stops taking
#: pictures (02 §1.7) -- which is why the protocol layer always ends in one of the two.
OBJECT_EVENT_DIR_ITEM_REQUEST_TRANSFER = 0x00000208

#: ``EdsObjectEventHandler``: ``void (EdsObjectEvent, EdsBaseRef, EdsVoid* ctx)``.
ObjectEventHandler = CFUNCTYPE(None, c_uint32, c_void_p, c_void_p)


class _EdsDirectoryItemInfo(Structure):
    #: Field order from ``EdsTypes.h``; the two 32-bit fields pack into the second
    #: eight bytes, so ``szFileName`` starts at byte 16.
    _fields_ = [
        ("size", c_uint64),
        ("isFolder", c_uint32),
        ("groupID", c_uint32),
        ("szFileName", c_char * EDS_MAX_NAME),
        ("format", c_uint32),
        ("dateTime", c_uint32),
    ]


class _EdsCapacity(Structure):
    _fields_ = [
        ("numberOfFreeClusters", c_int32),
        ("bytesPerSector", c_int32),
        ("reset", c_uint32),
    ]


class EdsdkError(RuntimeError):
    """An EDSDK call returned non-zero."""

    def __init__(self, what: str, code: int) -> None:
        self.what = what
        self.code = code
        super().__init__(f"{what} failed: 0x{code:08X}")


class _EdsDeviceInfo(Structure):
    #: Field order confirmed by raw byte dump on hardware (brief 16 §2).
    _fields_ = [
        ("szPortName", c_char * EDS_MAX_NAME),
        ("szDeviceDescription", c_char * EDS_MAX_NAME),
        ("deviceSubType", c_uint32),
        ("reserved", c_uint32),
    ]


#: ``EDS_MAX_PROP_DESC``: the array inside ``EdsPropertyDesc``.
EDS_MAX_PROP_DESC = 128


class _EdsPropertyDesc(Structure):
    #: ``typedef struct { EdsUInt32 readOnly; EdsUInt32 numElements;
    #: EdsUInt32 propDesc[EDS_MAX_PROP_DESC]; } EdsPropertyDesc;``
    _fields_ = [
        ("readOnly", c_uint32),
        ("numElements", c_uint32),
        ("propDesc", c_uint32 * EDS_MAX_PROP_DESC),
    ]


@dataclass(frozen=True)
class DeviceInfo:
    port: str
    description: str
    sub_type: int


def _check(what: str, code: int) -> int:
    if code != 0:
        raise EdsdkError(what, code)
    return code


def resolve_dll(dll_path: Path | None = None) -> tuple[Path, str]:
    """Which ``EDSDK.dll`` would be used, and where it came from.

    One resolver for both the loader and ``preflight``, because they disagreed: the
    loader silently preferred N.I.N.A.'s bundled copy while preflight reported only
    Lightroom's and called the SDK "not configured". Whichever copy is in use, the
    user should be told which. Borrowing another application's copy is a normal
    development shortcut; shipping is a separate, settled question -- an unmodified
    Canon DLL may travel with our installer, the SDK tree may not (08 §4).
    """
    if dll_path is not None:
        return Path(dll_path), "explicit"

    env = os.environ.get("MINISTAR_EDSDK") or os.environ.get("EDSDK_PATH")
    if env:
        candidate = Path(env)
        if candidate.is_dir():
            candidate = candidate / "EDSDK.dll"
        if candidate.is_file():
            return candidate, "MINISTAR_EDSDK"

    if _OWN.is_file():
        return _OWN, "installed"
    for path in BORROWED_DLLS:
        if path.is_file():
            return path, "borrowed"
    raise RuntimeError("could not find EDSDK.dll (set MINISTAR_EDSDK to its path)")


def load_dll(dll_path: Path | None = None):
    """Load EDSDK.dll from an explicit path, else the known candidates."""
    path, _origin = resolve_dll(dll_path)
    try:
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(str(path.parent))
        return ctypes.WinDLL(str(path))
    except OSError as exc:  # pragma: no cover - platform dependent
        raise RuntimeError(f"could not load {path}: {exc}") from exc


class Edsdk:
    """One SDK instance: initialise, open a camera, drive it, close."""

    def __init__(self, dll_path: Path | None = None) -> None:
        self._dll = load_dll(dll_path)
        #: Which entry points this DLL actually exports, filled in by _bind().
        self.symbols: dict[str, bool] = {}
        self._bind()
        self._camera: c_void_p | None = None
        self._stream: c_void_p | None = None
        self._evf_image: c_void_p | None = None
        self._live = False
        self._initialized = False
        #: Kept alive for the SDK's benefit: a collected callback is a crash.
        self._handler: ObjectEventHandler | None = None
        #: Items the body has offered us and nobody has claimed yet.
        self._transfers: list[c_void_p] = []
        self._transfer_events = 0

    def _bind(self) -> None:
        d = self._dll

        def sig(name, restype, argtypes):
            fn = getattr(d, name)
            fn.restype = restype
            fn.argtypes = argtypes
            return fn

        self.EdsInitializeSDK = sig("EdsInitializeSDK", c_uint32, [])
        self.EdsTerminateSDK = sig("EdsTerminateSDK", c_uint32, [])
        self.EdsRelease = sig("EdsRelease", c_uint32, [c_void_p])
        self.EdsGetCameraList = sig("EdsGetCameraList", c_uint32, [POINTER(c_void_p)])
        self.EdsGetChildCount = sig("EdsGetChildCount", c_uint32, [c_void_p, POINTER(c_int32)])
        self.EdsGetChildAtIndex = sig(
            "EdsGetChildAtIndex", c_uint32, [c_void_p, c_int32, POINTER(c_void_p)]
        )
        self.EdsGetDeviceInfo = sig(
            "EdsGetDeviceInfo", c_uint32, [c_void_p, POINTER(_EdsDeviceInfo)]
        )
        self.EdsOpenSession = sig("EdsOpenSession", c_uint32, [c_void_p])
        self.EdsCloseSession = sig("EdsCloseSession", c_uint32, [c_void_p])
        self.EdsSendCommand = sig("EdsSendCommand", c_uint32, [c_void_p, c_uint32, c_uint32])
        self.EdsSendStatusCommand = sig(
            "EdsSendStatusCommand", c_uint32, [c_void_p, c_uint32, c_int32]
        )
        self.EdsSetPropertyData = sig(
            "EdsSetPropertyData", c_uint32, [c_void_p, c_uint32, c_int32, c_uint32, c_void_p]
        )
        self.EdsGetPropertySize = sig(
            "EdsGetPropertySize",
            c_uint32,
            [c_void_p, c_uint32, c_int32, POINTER(c_uint32), POINTER(c_uint32)],
        )
        self.EdsGetPropertyData = sig(
            "EdsGetPropertyData", c_uint32, [c_void_p, c_uint32, c_int32, c_uint32, c_void_p]
        )
        # NOTE: both take EdsUInt64, not EdsUInt32.
        self.EdsCreateMemoryStream = sig(
            "EdsCreateMemoryStream", c_uint32, [c_uint64, POINTER(c_void_p)]
        )
        self.EdsCreateEvfImageRef = sig(
            "EdsCreateEvfImageRef", c_uint32, [c_void_p, POINTER(c_void_p)]
        )
        self.EdsDownloadEvfImage = sig("EdsDownloadEvfImage", c_uint32, [c_void_p, c_void_p])
        self.EdsGetPointer = sig("EdsGetPointer", c_uint32, [c_void_p, POINTER(c_void_p)])
        self.EdsGetLength = sig("EdsGetLength", c_uint32, [c_void_p, POINTER(c_uint64)])

        def optional(name, restype, argtypes):
            """Bind if the DLL exports it, and record the answer either way."""
            try:
                self.symbols[name] = True
                return sig(name, restype, argtypes)
            except AttributeError:
                # Older SDK builds lack parts of the still-capture surface; failing
                # here would make live view unusable for no reason.
                self.symbols[name] = False
                return None

        # -- still capture (02 §1.7) --
        self.EdsSetCapacity = optional("EdsSetCapacity", c_uint32, [c_void_p, _EdsCapacity])
        self.EdsSetObjectEventHandler = optional(
            "EdsSetObjectEventHandler", c_uint32, [c_void_p, c_uint32, ObjectEventHandler, c_void_p]
        )
        self.EdsGetEvent = optional("EdsGetEvent", c_uint32, [])
        self.EdsGetDirectoryItemInfo = optional(
            "EdsGetDirectoryItemInfo", c_uint32, [c_void_p, POINTER(_EdsDirectoryItemInfo)]
        )
        self.EdsCreateFileStream = optional(
            "EdsCreateFileStream", c_uint32, [c_char_p, c_uint32, c_uint32, POINTER(c_void_p)]
        )
        self.EdsDownload = optional("EdsDownload", c_uint32, [c_void_p, c_uint64, c_void_p])
        self.EdsDownloadComplete = optional("EdsDownloadComplete", c_uint32, [c_void_p])
        self.EdsDownloadCancel = optional("EdsDownloadCancel", c_uint32, [c_void_p])
        # -- properties (02 §3.2) --
        self.EdsGetPropertyDesc = optional(
            "EdsGetPropertyDesc", c_uint32, [c_void_p, c_uint32, POINTER(_EdsPropertyDesc)]
        )

    # -- lifecycle ----------------------------------------------------------
    def initialize(self) -> None:
        """Initialise the SDK for this process, once.

        Reference-counted because the *SDK* is global while this class is not: two cameras in one
        process (or one camera reopened after a failed connect) must not each call
        ``EdsInitializeSDK``. See the module note by ``_SDK_USERS`` for the measurement.
        """
        global _SDK_USERS
        with _SDK_LOCK:
            if _SDK_USERS == 0:
                _check("EdsInitializeSDK", self.EdsInitializeSDK())
            _SDK_USERS += 1
        self._initialized = True

    def list_cameras(self) -> list[DeviceInfo]:
        cam_list = c_void_p()
        _check("EdsGetCameraList", self.EdsGetCameraList(byref(cam_list)))
        try:
            count = c_int32()
            _check("EdsGetChildCount", self.EdsGetChildCount(cam_list, byref(count)))
            out: list[DeviceInfo] = []
            for index in range(count.value):
                ref = c_void_p()
                _check("EdsGetChildAtIndex", self.EdsGetChildAtIndex(cam_list, index, byref(ref)))
                info = _EdsDeviceInfo()
                _check("EdsGetDeviceInfo", self.EdsGetDeviceInfo(ref, byref(info)))
                out.append(
                    DeviceInfo(
                        port=info.szPortName.decode("ascii", "replace"),
                        description=info.szDeviceDescription.decode("ascii", "replace"),
                        sub_type=info.deviceSubType,
                    )
                )
                self.EdsRelease(ref)
            return out
        finally:
            self.EdsRelease(cam_list)

    def open_camera(self, index: int = 0) -> DeviceInfo:
        cam_list = c_void_p()
        _check("EdsGetCameraList", self.EdsGetCameraList(byref(cam_list)))
        try:
            camera = c_void_p()
            _check("EdsGetChildAtIndex", self.EdsGetChildAtIndex(cam_list, index, byref(camera)))
        finally:
            self.EdsRelease(cam_list)

        info = _EdsDeviceInfo()
        _check("EdsGetDeviceInfo", self.EdsGetDeviceInfo(camera, byref(info)))
        _check("EdsOpenSession", self.EdsOpenSession(camera))
        self._camera = camera
        return DeviceInfo(
            port=info.szPortName.decode("ascii", "replace"),
            description=info.szDeviceDescription.decode("ascii", "replace"),
            sub_type=info.deviceSubType,
        )

    def close(self) -> None:
        for attr in ("_stream", "_evf_image"):
            ref = getattr(self, attr)
            if ref is not None:
                self.EdsRelease(ref)
                setattr(self, attr, None)
        if self._camera is not None:
            self.EdsCloseSession(self._camera)
            self.EdsRelease(self._camera)
            self._camera = None
        if self._initialized:
            global _SDK_USERS
            with _SDK_LOCK:
                _SDK_USERS = max(0, _SDK_USERS - 1)
                if _SDK_USERS == 0:
                    self.EdsTerminateSDK()
            self._initialized = False

    def __enter__(self):
        self.initialize()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- properties ---------------------------------------------------------
    @property
    def camera(self) -> c_void_p:
        if self._camera is None:
            raise RuntimeError("no camera open")
        return self._camera

    def string_property(self, prop_id: int) -> str:
        dtype = c_uint32()
        dsize = c_uint32()
        _check(
            "EdsGetPropertySize",
            self.EdsGetPropertySize(self.camera, prop_id, 0, byref(dtype), byref(dsize)),
        )
        if dsize.value == 0:
            return ""
        buf = ctypes.create_string_buffer(dsize.value)
        _check(
            "EdsGetPropertyData",
            self.EdsGetPropertyData(self.camera, prop_id, 0, dsize.value, buf),
        )
        return buf.value.decode("ascii", "replace")

    def uint_property(self, prop_id: int) -> int:
        """One UInt32 property (ISO, Tv, Av)."""
        value = c_uint32()
        _check(
            f"EdsGetPropertyData(0x{prop_id:08X})",
            self.EdsGetPropertyData(self.camera, prop_id, 0, ctypes.sizeof(value), byref(value)),
        )
        return int(value.value)

    def set_uint_property(self, prop_id: int, value: int) -> None:
        """Write one UInt32 property. Raises on refusal (0x81 = DEVICE_BUSY, 08 §7.2b)."""
        raw = c_uint32(value)
        _check(
            f"EdsSetPropertyData(0x{prop_id:08X})",
            self.EdsSetPropertyData(self.camera, prop_id, 0, ctypes.sizeof(raw), byref(raw)),
        )

    def property_desc(self, prop_id: int) -> tuple[int, ...]:
        """The values *this body* offers for a property, or ``()`` if it cannot say.

        ``EdsGetPropertyDesc`` is the SDK's answer to "what is legal here?" (02 §3.2), and
        it is per-body: the 60D's Tv list is not the 5D's. Empty means the symbol or the
        property is unavailable, and the caller must fall back to read-back verification.
        """
        if self.EdsGetPropertyDesc is None:
            return ()
        desc = _EdsPropertyDesc()
        try:
            _check(
                f"EdsGetPropertyDesc(0x{prop_id:08X})",
                self.EdsGetPropertyDesc(self.camera, prop_id, byref(desc)),
            )
        except EdsdkError:
            return ()
        count = min(int(desc.numElements), EDS_MAX_PROP_DESC)
        return tuple(int(v) for v in desc.propDesc[:count])

    # -- live view ----------------------------------------------------------
    def start_live_view(self) -> None:
        """Route the EVF image to the PC and allocate the stream once.

        Idempotent: the camera layer calls it lazily on the first preview request rather
        than at connect time, because on a DSLR starting live view flips the mirror and
        cycles the shutter -- which must not happen just because the app was launched.

        **Only the output route is set.** An earlier version also wrote ``Evf_Mode = 1``
        first, on the theory that "some bodies require it". On this body that call has been
        observed to never return, so the first pump iteration sat inside the SDK: the preview
        stayed blank with no error anywhere to report it, and even shutdown hung. Live view
        had in fact never been exercised through *this* binding -- the verification in brief
        16 used a separate targeted script, which is how the gap survived. Measured
        2026-10-28 on the 60D: output-route-only answers in 0.06 s, then ``0xA102`` for ~1.0 s
        and a 180 KB JPEG -- the warm-up :meth:`grab_jpeg` already retries through.
        """
        if self._live:
            return
        value = c_uint32(EVF_OUTPUT_PC)
        _check(
            "EdsSetPropertyData(Evf_OutputDevice)",
            self.EdsSetPropertyData(
                self.camera, PROP_EVF_OUTPUT_DEVICE, 0, ctypes.sizeof(value), byref(value)
            ),
        )

        stream = c_void_p()
        _check("EdsCreateMemoryStream", self.EdsCreateMemoryStream(0, byref(stream)))
        self._stream = stream
        evf = c_void_p()
        _check("EdsCreateEvfImageRef", self.EdsCreateEvfImageRef(stream, byref(evf)))
        self._evf_image = evf
        self._live = True

    def grab_jpeg(self, timeout_s: float = 6.0) -> bytes:
        """One live-view frame as JPEG bytes, retrying \"not ready\" codes."""
        if not self._live:
            raise RuntimeError("live view not started")
        deadline = time.monotonic() + timeout_s
        while True:
            code = self.EdsDownloadEvfImage(self.camera, self._evf_image)
            if code == 0:
                return self._stream_bytes()
            if code in RETRYABLE_EVF and time.monotonic() < deadline:
                time.sleep(0.05)
                continue
            raise EdsdkError("EdsDownloadEvfImage", code)

    def _stream_bytes(self) -> bytes:
        ptr = c_void_p()
        length = c_uint64()
        _check("EdsGetPointer", self.EdsGetPointer(self._stream, byref(ptr)))
        _check("EdsGetLength", self.EdsGetLength(self._stream, byref(length)))
        if not ptr.value or length.value == 0:
            return b""
        return ctypes.string_at(ptr, length.value)

    def stop_live_view(self) -> None:
        """Route the EVF away from the PC and free the stream.

        Called on every stand-down, which is now frequent -- leaving a screen that shows a
        preview, and every run. The release matters for that reason: each ``start_live_view``
        allocates a memory stream and an ``EdsEvfImageRef``, so leaking them costs an EDSDK
        object per visit. A failure here is swallowed by the caller: a body we cannot reach is
        already reported by whatever failed first.
        """
        if not self._live:
            return
        value = c_uint32(EVF_OUTPUT_OFF)
        self.EdsSetPropertyData(
            self.camera, PROP_EVF_OUTPUT_DEVICE, 0, ctypes.sizeof(value), byref(value)
        )
        for ref in (self._evf_image, self._stream):
            if ref is not None:
                with contextlib.suppress(Exception):
                    self.EdsRelease(ref)
        self._evf_image = None
        self._stream = None
        self._live = False

    # -- focus --------------------------------------------------------------
    def drive_lens(self, param: int, *, attempts: int = 5, delay: float = 0.03) -> None:
        """One DriveLensEvf pulse. Requires live view and the lens on MF."""
        last = 0
        for attempt in range(attempts):
            code = self.EdsSendCommand(self.camera, CMD_DRIVE_LENS_EVF, param)
            if code == 0:
                return
            last = code
            if code in RETRYABLE_EVF and attempt + 1 < attempts:
                time.sleep(delay)
                continue
            raise EdsdkError("EdsSendCommand(DriveLensEvf)", code)
        raise EdsdkError("EdsSendCommand(DriveLensEvf)", last)

    def keep_alive(self) -> None:
        """Push back the auto-power-off timer (08 §7.4)."""
        self.EdsSendCommand(self.camera, CMD_EXTEND_SHUTDOWN_TIMER, 0)

    # -- still capture ------------------------------------------------------
    def symbol_report(self) -> dict[str, bool]:
        """Which still-capture entry points this DLL exports.

        Useful without a camera attached: it separates "our bindings are wrong"
        from "the body did not behave", which are otherwise the same failure.
        """
        return dict(self.symbols)

    def set_save_to_host(self, *, keep_card_copy: bool = False) -> None:
        """Tell the body where photographed files should go, and how much room we have."""
        value = c_uint32(SAVE_TO_BOTH if keep_card_copy else SAVE_TO_HOST)
        _check(
            "EdsSetPropertyData(SaveTo)",
            self.EdsSetPropertyData(
                self.camera, PROP_SAVE_TO, 0, ctypes.sizeof(value), byref(value)
            ),
        )
        if self.EdsSetCapacity is None:
            return
        # The body uses this to decide whether to refuse a shot at all. Generous and
        # reset once, as Canon's samples do; the real free-space check is ours.
        capacity = _EdsCapacity(numberOfFreeClusters=0x7FFFFFFF, bytesPerSector=0x1000, reset=1)
        self.EdsSetCapacity(self.camera, capacity)

    def arm_transfer_handler(self) -> None:
        """Start collecting ``DirItemRequestTransfer`` items into a queue.

        The SDK only delivers events when the host pumps them, and a console process
        has no message loop, so :meth:`pump` has to be called in the wait loop.
        """
        if self.EdsSetObjectEventHandler is None:
            raise RuntimeError("this EDSDK.dll has no EdsSetObjectEventHandler")

        def on_event(event: int, ref: c_void_p, _context: c_void_p) -> None:
            # Runs on the SDK's callback; keep it to bookkeeping only.
            if event == OBJECT_EVENT_DIR_ITEM_REQUEST_TRANSFER:
                self._transfers.append(ref)
                self._transfer_events += 1

        self._handler = ObjectEventHandler(on_event)
        _check(
            "EdsSetObjectEventHandler",
            self.EdsSetObjectEventHandler(
                self.camera, OBJECT_EVENT_DIR_ITEM_REQUEST_TRANSFER, self._handler, None
            ),
        )
        self._transfers.clear()
        self._transfer_events = 0

    def pump(self) -> None:
        """Process pending SDK events once. Safe to call in a tight-ish loop."""
        if self.EdsGetEvent is not None:
            self.EdsGetEvent()

    def take_picture(self) -> None:
        """Release the shutter. The file arrives via the transfer handler."""
        _check(
            "EdsSendCommand(TakePicture)",
            self.EdsSendCommand(self.camera, CMD_TAKE_PICTURE, 0),
        )

    def wait_for_transfer(self, timeout_s: float = 30.0) -> c_void_p:
        """Block until the body offers a file, and return it. Caller owns the ref.

        The timeout must exceed the exposure plus write time: a 30 s sub on a slow
        card is not a hung camera. The ``13 §4`` failure mode to watch for is a body
        that never asks, which is what the timeout reports.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self.pump()
            if self._transfers:
                return self._transfers.pop(0)
            time.sleep(0.02)
        raise TimeoutError(
            f"camera did not offer a file within {timeout_s:g} s "
            f"({self._transfer_events} transfer event(s) seen)"
        )

    def download_to(self, item: c_void_p, path: Path) -> Path:
        """Write one offered item to ``path`` and complete it.

        Always downloads *or* cancels: an item left unclaimed stops the body taking
        further pictures (02 §1.7).
        """
        if self.EdsGetDirectoryItemInfo is None or self.EdsCreateFileStream is None:
            raise EdsdkError("EdsCreateFileStream", 0)
        info = _EdsDirectoryItemInfo()
        _check("EdsGetDirectoryItemInfo", self.EdsGetDirectoryItemInfo(item, byref(info)))

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        stream = c_void_p()
        _check(
            "EdsCreateFileStream",
            self.EdsCreateFileStream(
                # EDSDK takes the ANSI code page, not UTF-8. Our data dirs are ASCII.
                os.fsencode(str(target)),
                FILE_CREATE_ALWAYS,
                ACCESS_READ_WRITE,
                byref(stream),
            ),
        )
        try:
            _check("EdsDownload", self.EdsDownload(item, info.size, stream))
            _check("EdsDownloadComplete", self.EdsDownloadComplete(item))
        except Exception:
            if self.EdsDownloadCancel is not None:
                self.EdsDownloadCancel(item)
            raise
        finally:
            self.EdsRelease(stream)
            self.EdsRelease(item)
        return target

    def drop_transfers(self) -> int:
        """Cancel anything still queued, so the body is not left waiting."""
        dropped = 0
        while self._transfers:
            item = self._transfers.pop(0)
            if self.EdsDownloadCancel is not None:
                self.EdsDownloadCancel(item)
            self.EdsRelease(item)
            dropped += 1
        return dropped
