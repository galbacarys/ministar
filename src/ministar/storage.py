"""Removable storage for session output.

The appliance should prefer a USB stick over the SD card: cards wear out, and a
night's CR2s are tens of gigabytes. This module answers two questions for the UI:

* which mounted filesystems are actually removable USB volumes;
* is a chosen output path safe to create right now, or would ``mkdir`` create a
  directory on the SD card where an unmounted drive used to be?

The second is the one that matters. A saved ``/media/astroberry/STICK/ministar``
path must never be recreated if ``STICK`` is not mounted; that would silently fill
the card. Detection reads ``/proc/mounts`` and sysfs rather than guessing from a
directory name.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

#: Debian/udisks mounts under /media; Fedora and some udev rules use /run/media.
MOUNT_ROOTS: tuple[Path, ...] = (Path("/media"), Path("/run/media"))

#: Filesystems that cannot be a session output even if they appear under /media.
_NOT_STORAGE = frozenset({"squashfs", "tmpfs", "devtmpfs", "overlay", "proc", "sysfs"})

_UNESCAPE = re.compile(r"\\([0-7]{3})")


class StorageError(RuntimeError):
    """The selected output root cannot be used safely."""


@dataclass(frozen=True)
class StorageOption:
    """One choice in the output-folder dropdown."""

    label: str
    path: Path
    free_bytes: int | None
    removable: bool = False
    writable: bool = True
    mounted: bool = True

    @property
    def free_text(self) -> str:
        if self.free_bytes is None:
            return "size unknown"
        return f"{self.free_bytes / 1e9:.1f} GB free"

    @property
    def display(self) -> str:
        kind = "USB" if self.removable else "internal"
        bits = [f"{kind}: {self.label}", self.free_text]
        if not self.mounted:
            bits.append("not mounted")
        elif not self.writable:
            bits.append("read-only")
        return " -- ".join(bits)


def _unescape(field: str) -> str:
    return _UNESCAPE.sub(lambda match: chr(int(match.group(1), 8)), field)


def _read_mounts(mounts_path: Path = Path("/proc/mounts")) -> list[tuple[str, Path, str]]:
    """``(source, mountpoint, fstype)`` for every mount, in file order."""
    entries: list[tuple[str, Path, str]] = []
    try:
        text = Path(mounts_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return entries
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        source, mountpoint, fstype = fields[0], _unescape(fields[1]), fields[2]
        entries.append((source, Path(mountpoint), fstype))
    return entries


def _is_usb(device: str, sys_block: Path) -> bool:
    node = sys_block / device
    try:
        resolved = node.resolve()
    except OSError:
        return False

    # ``sda1`` is a partition: ``removable`` lives on ``sda``, one level up from
    # the resolved partition node, not on the /sys/class/block symlink itself.
    removable = False
    for candidate in (resolved, resolved.parent):
        with contextlib.suppress(OSError):
            if (candidate / "removable").read_text(encoding="utf-8").strip() == "1":
                removable = True
                break

    # USB SSDs often report removable=0; the early path component is ``usb1``, not
    # a part equal to ``usb``, so a prefix test is the second signal.
    usb_path = any(part.startswith("usb") for part in resolved.parts)
    return removable or usb_path


def _under(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def mounted_usb_drives(
    *,
    mounts_path: Path = Path("/proc/mounts"),
    sys_block: Path = Path("/sys/class/block"),
    mount_roots: tuple[Path, ...] = MOUNT_ROOTS,
) -> list[StorageOption]:
    """Mounted USB volumes under the platform's media roots.

    A volume is writable from this process's point of view only if the mount point
    allows it; read-only mounts stay in the list so the UI can explain why they
    cannot be selected.
    """
    found: dict[Path, StorageOption] = {}
    for source, mountpoint, fstype in _read_mounts(mounts_path):
        if fstype in _NOT_STORAGE:
            continue
        if not (source.startswith("/dev/") or source.startswith("/dev/mapper/")):
            continue
        if not _under(mountpoint, mount_roots) or mountpoint in mount_roots:
            continue
        if not mountpoint.is_dir():
            continue
        device = Path(source).name
        if not _is_usb(device, sys_block):
            continue
        try:
            free = shutil.disk_usage(mountpoint).free
        except OSError:
            free = None
        try:
            writable = os.access(mountpoint, os.W_OK | os.X_OK)
        except OSError:
            writable = False
        found[mountpoint] = StorageOption(
            label=mountpoint.name,
            path=mountpoint / "ministar",
            free_bytes=free,
            removable=True,
            writable=writable,
        )

    return sorted(
        found.values(),
        key=lambda option: (not option.writable, -(option.free_bytes or 0), option.label.lower()),
    )


def internal_option(output: Path) -> StorageOption:
    """The SD-card fallback: always present, never the preferred destination."""
    output = Path(output)
    probe = output if output.exists() else output.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        free = None
    return StorageOption(
        label="internal storage",
        path=output,
        free_bytes=free,
        removable=False,
        writable=os.access(probe, os.W_OK | os.X_OK),
    )


def is_removable_path(path: Path | str, *, mount_roots: tuple[Path, ...] = MOUNT_ROOTS) -> bool:
    return _under(Path(path).expanduser(), mount_roots)


def output_options(
    output: Path,
    selected: str | Path | None = None,
    *,
    mounts_path: Path = Path("/proc/mounts"),
    sys_block: Path = Path("/sys/class/block"),
    mount_roots: tuple[Path, ...] = MOUNT_ROOTS,
) -> list[StorageOption]:
    """Internal storage, every USB drive, and the current selection if it is missing."""
    options = [internal_option(output)]
    options.extend(
        mounted_usb_drives(mounts_path=mounts_path, sys_block=sys_block, mount_roots=mount_roots)
    )
    if selected is not None:
        chosen = Path(selected).expanduser()
        if not any(option.path == chosen for option in options):
            options.append(
                StorageOption(
                    label=f"unavailable: {chosen}",
                    path=chosen,
                    free_bytes=None,
                    removable=is_removable_path(chosen, mount_roots=mount_roots),
                    writable=False,
                    mounted=False,
                )
            )
    return options


def default_output_root(
    *,
    mounts_path: Path = Path("/proc/mounts"),
    sys_block: Path = Path("/sys/class/block"),
    mount_roots: tuple[Path, ...] = MOUNT_ROOTS,
) -> Path | None:
    """The first writable USB volume's ``ministar`` folder, or None for internal."""
    for option in mounted_usb_drives(
        mounts_path=mounts_path, sys_block=sys_block, mount_roots=mount_roots
    ):
        if option.writable:
            return option.path
    return None


def ensure_output_root(
    path: Path | str,
    *,
    mounts_path: Path = Path("/proc/mounts"),
    mount_roots: tuple[Path, ...] = MOUNT_ROOTS,
) -> Path:
    """Create ``path`` only when its removable parent is really mounted.

    Raises :class:`StorageError` rather than letting ``mkdir(parents=True)`` create a
    shadow directory on the SD card under /media.
    """
    root = Path(path).expanduser()
    if is_removable_path(root, mount_roots=mount_roots):
        mounted = [
            mountpoint
            for _source, mountpoint, _fstype in _read_mounts(mounts_path)
            if (root == mountpoint or mountpoint in root.parents)
            and _under(mountpoint, mount_roots)
        ]
        if not mounted:
            raise StorageError(
                "the removable drive is not mounted; plug it in or choose another output folder"
            )
        mountpoint = max(mounted, key=lambda item: len(item.parts))
        if not os.access(mountpoint, os.W_OK | os.X_OK):
            raise StorageError(f"{mountpoint} is not writable by this process")
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageError(f"could not create {root}: {exc}") from exc
    if not os.access(root, os.W_OK | os.X_OK):
        raise StorageError(f"{root} is not writable by this process")
    return root


__all__ = [
    "MOUNT_ROOTS",
    "StorageError",
    "StorageOption",
    "default_output_root",
    "ensure_output_root",
    "internal_option",
    "is_removable_path",
    "mounted_usb_drives",
    "output_options",
]
