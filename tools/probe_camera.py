"""Throwaway EDSDK enumeration probe. NOT the product binding.

Brief 08 specifies forking ``pyedsdk`` for the real backend. This file exists
only to answer three questions against real hardware before any of that is
built:

  1. Does a ctypes ``WinDLL`` load of ``EDSDK.dll`` work at all?
  2. Does the SDK see the camera?
  3. What does ``EdsDeviceInfo`` actually contain (field order unverified)?

It deliberately guesses nothing about struct layout: the raw bytes are dumped
with printable runs highlighted, so a wrong field order is still readable
instead of silently garbage.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import POINTER, Structure, byref, c_char, c_int32, c_uint32, c_void_p

EDS_MAX_NAME = 256

#: Candidate DLL locations, most self-contained first.
CANDIDATES = (
    r"C:\Program Files\N.I.N.A. - Nighttime Imaging 'N' Astronomy\External\x64\Canon\EDSDK.dll",
    r"C:\Program Files\Adobe\Adobe Lightroom Classic\EDSDK.dll",
    r"C:\Program Files\ON1\ON1 Photo RAW 2026\EDSDK.dll",
)

kEdsPropID_ProductName = 0x00000002
kEdsPropID_DeviceModel = 0x0000000C
kEdsPropID_AvailableShots = 0x0000000D

Names = {
    kEdsPropID_ProductName: "ProductName",
    kEdsPropID_DeviceModel: "DeviceModel",
    kEdsPropID_AvailableShots: "AvailableShots",
}


class EdsDeviceInfo(Structure):
    """Layout unverified on purpose -- see module docstring."""

    _fields_ = [
        ("szPortName", c_char * EDS_MAX_NAME),
        ("szDeviceDescription", c_char * EDS_MAX_NAME),
        ("deviceSubType", c_uint32),
        ("reserved", c_uint32),
    ]


def printable_runs(raw: bytes, minimum: int = 3) -> list[tuple[int, str]]:
    runs: list[tuple[int, str]] = []
    start = 0
    buf = []
    for i, byte in enumerate(raw):
        if 32 <= byte < 127:
            if not buf:
                start = i
            buf.append(chr(byte))
        else:
            if len(buf) >= minimum:
                runs.append((start, "".join(buf)))
            buf = []
    if len(buf) >= minimum:
        runs.append((start, "".join(buf)))
    return runs


def load_dll():
    for path in CANDIDATES:
        if not os.path.isfile(path):
            print(f"  missing: {path}")
            continue
        dll_dir = os.path.dirname(path)
        print(f"  trying : {path}")
        try:
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(dll_dir)
            dll = ctypes.WinDLL(path)
            print(f"  loaded : {path}")
            return dll
        except OSError as exc:
            print(f"  FAILED : {exc}")
    return None


def bind(dll) -> None:
    """Set restypes/argtypes for the handful of calls needed."""
    for name in (
        "EdsInitializeSDK",
        "EdsTerminateSDK",
        "EdsOpenSession",
        "EdsCloseSession",
        "EdsRelease",
    ):
        fn = getattr(dll, name)
        fn.restype = c_uint32
        takes_ref = name in ("EdsOpenSession", "EdsCloseSession", "EdsRelease")
        fn.argtypes = [c_void_p] if takes_ref else []

    dll.EdsGetCameraList.restype = c_uint32
    dll.EdsGetCameraList.argtypes = [POINTER(c_void_p)]

    dll.EdsGetChildCount.restype = c_uint32
    dll.EdsGetChildCount.argtypes = [c_void_p, POINTER(c_int32)]

    dll.EdsGetChildAtIndex.restype = c_uint32
    dll.EdsGetChildAtIndex.argtypes = [c_void_p, c_int32, POINTER(c_void_p)]

    dll.EdsGetDeviceInfo.restype = c_uint32
    dll.EdsGetDeviceInfo.argtypes = [c_void_p, POINTER(EdsDeviceInfo)]

    dll.EdsGetPropertySize.restype = c_uint32
    dll.EdsGetPropertySize.argtypes = [
        c_void_p,
        c_uint32,
        c_int32,
        POINTER(c_uint32),
        POINTER(c_uint32),
    ]

    dll.EdsGetPropertyData.restype = c_uint32
    dll.EdsGetPropertyData.argtypes = [
        c_void_p,
        c_uint32,
        c_int32,
        c_uint32,
        c_void_p,
    ]


def read_string_prop(dll, camera, prop_id: int) -> str:
    dtype = c_uint32()
    dsize = c_uint32()
    err = dll.EdsGetPropertySize(camera, prop_id, 0, byref(dtype), byref(dsize))
    if err != 0 or dsize.value == 0:
        return f"<EdsGetPropertySize err=0x{err:08X} size={dsize.value}>"
    buf = ctypes.create_string_buffer(dsize.value)
    err = dll.EdsGetPropertyData(camera, prop_id, 0, dsize.value, buf)
    if err != 0:
        return f"<EdsGetPropertyData err=0x{err:08X}>"
    return buf.value.decode("ascii", "replace")


def read_uint_prop(dll, camera, prop_id: int) -> str:
    val = c_uint32()
    err = dll.EdsGetPropertyData(camera, prop_id, 0, ctypes.sizeof(val), byref(val))
    if err != 0:
        return f"<err=0x{err:08X}>"
    return str(val.value)


def main() -> int:
    print("EDSDK probe")
    print("=" * 60)
    print("loading DLL...")
    dll = load_dll()
    if dll is None:
        print("could not load EDSDK.dll")
        return 2

    bind(dll)

    err = dll.EdsInitializeSDK()
    print(f"EdsInitializeSDK -> 0x{err:08X} ({'OK' if err == 0 else 'FAILED'})")
    if err != 0:
        return 3

    try:
        cam_list = c_void_p()
        err = dll.EdsGetCameraList(byref(cam_list))
        print(f"EdsGetCameraList -> 0x{err:08X} handle={cam_list.value}")
        if err != 0:
            return 4

        count = c_int32()
        err = dll.EdsGetChildCount(cam_list, byref(count))
        print(f"EdsGetChildCount -> 0x{err:08X} cameras={count.value}")

        if count.value == 0:
            print()
            print("No camera visible to EDSDK.")
            print("Likely causes: camera off, in sleep, USB cable in a charging-only")
            print("port, or another app (Lightroom/ON1/NINA) holding the session.")
            return 5

        for index in range(count.value):
            camera = c_void_p()
            err = dll.EdsGetChildAtIndex(cam_list, index, byref(camera))
            print(f"\n-- camera {index}: EdsGetChildAtIndex -> 0x{err:08X}")
            if err != 0:
                continue

            # Dump raw bytes as well as the parsed struct: the field order is
            # unverified, and a raw dump degrades to readable text rather than
            # to silent garbage.
            raw = (c_char * ctypes.sizeof(EdsDeviceInfo))()
            info = ctypes.cast(raw, POINTER(EdsDeviceInfo)).contents
            err = dll.EdsGetDeviceInfo(camera, byref(info))
            print(f"   EdsGetDeviceInfo -> 0x{err:08X}")
            print(f"   raw printable runs: {printable_runs(bytes(raw), 4)}")
            print(f"   parsed szPortName        : {info.szPortName.decode('ascii', 'replace')!r}")
            print(
                f"   parsed szDeviceDescription: "
                f"{info.szDeviceDescription.decode('ascii', 'replace')!r}"
            )
            print(f"   parsed deviceSubType     : 0x{info.deviceSubType:08X}")

            err = dll.EdsOpenSession(camera)
            print(f"   EdsOpenSession -> 0x{err:08X} ({'OK' if err == 0 else 'FAILED'})")
            if err != 0:
                dll.EdsRelease(camera)
                continue

            for prop_id, label in Names.items():
                if label == "AvailableShots":
                    value = read_uint_prop(dll, camera, prop_id)
                else:
                    value = read_string_prop(dll, camera, prop_id)
                print(f"   {label:16s} = {value}")

            dll.EdsCloseSession(camera)
            dll.EdsRelease(camera)

        dll.EdsRelease(cam_list)
    finally:
        err = dll.EdsTerminateSDK()
        print(f"\nEdsTerminateSDK -> 0x{err:08X}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
