"""Minimal, targeted EDSDK binding for bench-testing focus. NOT the product backend.

Brief 08 specifies forking ``pyedsdk`` as the real backend. This module exists to
get a focus knob on the screen today, and deliberately implements only:

    initialize / enumerate / open session
    live-view start, grab a JPEG frame
    EdsSendCommand(DriveLensEvf, Near*|Far*)
    extend the auto-shutoff timer

Constants below are transcribed from Canon's ``EdsTypes.h`` and cross-checked
against ``Canon.Eos.Framework``'s C# bindings, which agree exactly:

    kEdsCameraCommand_DriveLensEvf   0x00000103
    kEdsEvfDriveLens_Near1..3        0x0001, 0x0002, 0x0003   ("step closer")
    kEdsEvfDriveLens_Far1..3         0x8001, 0x8002, 0x8003   ("step further")

Note the asymmetry: Far is Near with bit 0x8000 set. That maps onto
``focus.driver.Direction`` as ``param = |d| if near else 0x8000 | |d|``.

Everything here is verified against a real EOS 60D. The two things that are
*not* verified until you run it: whether the body accepts ``Evf_Mode``, and
whether the lens is in MF (the drive command is a manual-focus command and will
fail or do nothing in AF).
"""

from __future__ import annotations

import ctypes
import os
import time
from ctypes import POINTER, Structure, byref, c_char, c_int32, c_uint32, c_uint64, c_void_p
from dataclasses import dataclass
from pathlib import Path

EDS_MAX_NAME = 256

#: Self-contained first: NINA ships the SDK's own dependency set beside the DLL.
_NINA = Path(r"C:\Program Files\N.I.N.A. - Nighttime Imaging 'N' Astronomy")
DLL_CANDIDATES = (
    _NINA / "External/x64/Canon/EDSDK.dll",
    Path(r"C:\Program Files\Adobe\Adobe Lightroom Classic\EDSDK.dll"),
    Path(r"C:\Program Files\ON1\ON1 Photo RAW 2026\EDSDK.dll"),
)

# -- property IDs ------------------------------------------------------------
PROP_PRODUCT_NAME = 0x00000002
PROP_SAVE_TO = 0x0000000B
PROP_AVAILABLE_SHOTS = 0x0000040A
PROP_LENS_NAME = 0x0000040D
PROP_EVF_OUTPUT_DEVICE = 0x00000500
PROP_EVF_MODE = 0x00000501

# -- commands ----------------------------------------------------------------
CMD_TAKE_PICTURE = 0x00000000
CMD_EXTEND_SHUTDOWN_TIMER = 0x00000001
CMD_DRIVE_LENS_EVF = 0x00000103

# -- drive lens --------------------------------------------------------------
NEAR_1, NEAR_2, NEAR_3 = 0x00000001, 0x00000002, 0x00000003
FAR_1, FAR_2, FAR_3 = 0x00008001, 0x00008002, 0x00008003

EVF_OUTPUT_PC = 0x00000002

#: Codes that mean "not ready yet" rather than "failed".
#: Measured on the EOS 60D: after enabling the EVF output device, the first
#: ``EdsDownloadEvfImage`` returns 0xA102 for roughly 0.85 s (17 attempts at
#: 50 ms) before a frame is available. Treating it as fatal makes live view look
#: broken when it is merely warming up.
RETRYABLE_EVF = frozenset({0x000000A0, 0x0000A102, 0x00000081})

#: Canon's three pulse magnitudes, smallest first.
NEAR_STEPS = (NEAR_1, NEAR_2, NEAR_3)
FAR_STEPS = (FAR_1, FAR_2, FAR_3)


class EdsdkError(RuntimeError):
    """An EDSDK call returned non-zero."""

    def __init__(self, what: str, code: int) -> None:
        self.what = what
        self.code = code
        super().__init__(f"{what} failed: 0x{code:08X}")


class _EdsDeviceInfo(Structure):
    _fields_ = [
        ("szPortName", c_char * EDS_MAX_NAME),
        ("szDeviceDescription", c_char * EDS_MAX_NAME),
        ("deviceSubType", c_uint32),
        ("reserved", c_uint32),
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


class Edsdk:
    """One SDK instance. Create, ``open_camera()``, use, ``close()``."""

    def __init__(self, dll_path: Path | None = None) -> None:
        self._dll = self._load(dll_path)
        self._bind()
        self._camera: c_void_p | None = None
        self._stream: c_void_p | None = None
        self._evf_image: c_void_p | None = None
        self._live = False
        self._initialized = False

    # -- loading ------------------------------------------------------------
    @staticmethod
    def _load(dll_path: Path | None):
        candidates = (dll_path,) if dll_path else DLL_CANDIDATES
        errors = []
        for path in candidates:
            if path is None or not Path(path).is_file():
                continue
            try:
                if hasattr(os, "add_dll_directory"):
                    os.add_dll_directory(str(Path(path).parent))
                return ctypes.WinDLL(str(path))
            except OSError as exc:  # pragma: no cover - hardware dependent
                errors.append(f"{path}: {exc}")
        raise RuntimeError(
            "could not load EDSDK.dll; tried " + ("; ".join(errors) if errors else "nothing")
        )

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
        # NOTE: EdsCreateMemoryStream and EdsGetLength both take EdsUInt64.
        self.EdsCreateMemoryStream = sig(
            "EdsCreateMemoryStream", c_uint32, [c_uint64, POINTER(c_void_p)]
        )
        self.EdsCreateEvfImageRef = sig(
            "EdsCreateEvfImageRef", c_uint32, [c_void_p, POINTER(c_void_p)]
        )
        self.EdsDownloadEvfImage = sig("EdsDownloadEvfImage", c_uint32, [c_void_p, c_void_p])
        self.EdsGetPointer = sig("EdsGetPointer", c_uint32, [c_void_p, POINTER(c_void_p)])
        self.EdsGetLength = sig("EdsGetLength", c_uint32, [c_void_p, POINTER(c_uint64)])

    # -- lifecycle ----------------------------------------------------------
    def initialize(self) -> None:
        _check("EdsInitializeSDK", self.EdsInitializeSDK())
        self._initialized = True

    def list_cameras(self) -> list[DeviceInfo]:
        cam_list = c_void_p()
        _check("EdsGetCameraList", self.EdsGetCameraList(byref(cam_list)))
        try:
            count = c_int32()
            _check("EdsGetChildCount", self.EdsGetChildCount(cam_list, byref(count)))
            out: list[DeviceInfo] = []
            for i in range(count.value):
                ref = c_void_p()
                _check("EdsGetChildAtIndex", self.EdsGetChildAtIndex(cam_list, i, byref(ref)))
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
        if self._stream is not None:
            self.EdsRelease(self._stream)
            self._stream = None
        if self._evf_image is not None:
            self.EdsRelease(self._evf_image)
            self._evf_image = None
        if self._camera is not None:
            self.EdsCloseSession(self._camera)
            self.EdsRelease(self._camera)
            self._camera = None
        if self._initialized:
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

    # -- live view ----------------------------------------------------------
    def start_live_view(self) -> None:
        """Route the EVF image to the PC and allocate the stream once."""
        value = c_uint32(EVF_OUTPUT_PC)
        # Some bodies require Evf_Mode first; failures here are not fatal.
        mode = c_uint32(1)
        self.EdsSetPropertyData(
            self.camera, PROP_EVF_MODE, 0, ctypes.sizeof(mode), ctypes.byref(mode)
        )
        _check(
            "EdsSetPropertyData(Evf_OutputDevice)",
            self.EdsSetPropertyData(
                self.camera, PROP_EVF_OUTPUT_DEVICE, 0, ctypes.sizeof(value), ctypes.byref(value)
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
        """One live-view frame as JPEG bytes.

        Retries while the camera reports "not ready" (see :data:`RETRYABLE_EVF`).
        The first frame after enabling live view needs ~0.85 s on a 60D, so a
        short single-attempt call would report a spurious failure.
        """
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
        if not self._live:
            return
        value = c_uint32(0)
        self.EdsSetPropertyData(
            self.camera, PROP_EVF_OUTPUT_DEVICE, 0, ctypes.sizeof(value), ctypes.byref(value)
        )
        self._live = False

    # -- focus --------------------------------------------------------------
    def drive_lens(self, param: int, *, attempts: int = 5, delay: float = 0.03) -> None:
        """Send one DriveLensEvf pulse. Requires live view and the lens on MF.

        Retries the "busy / not ready" codes. Hammering the button faster than
        the body can consume pulses makes it return DEVICE_BUSY, and treating
        that as fatal is what makes a UI look broken under fast clicking.
        """
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
        """Push back the auto-power-off timer (08 sec.7.4)."""
        self.EdsSendCommand(self.camera, CMD_EXTEND_SHUTDOWN_TIMER, 0)
