"""Removable storage detection and the one safety rule that matters.

A saved ``/media/.../ministar`` path must not be recreated when the stick is absent;
that would quietly fill the SD card. The tests use a fake ``/proc/mounts`` and sysfs
tree so the USB and non-USB cases are deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ministar.storage import (
    StorageError,
    default_output_root,
    ensure_output_root,
    internal_option,
    is_removable_path,
    mounted_usb_drives,
    output_options,
)


@pytest.fixture
def media(tmp_path: Path):
    root = tmp_path / "media"
    root.mkdir()
    stick = root / "STICK"
    stick.mkdir()
    sys_block = tmp_path / "sys" / "block"
    (sys_block / "sdb1").mkdir(parents=True)
    (sys_block / "sdb1" / "removable").write_text("1\n", encoding="utf-8")
    mounts = tmp_path / "mounts"
    mounts.write_text(
        "\n".join(
            [
                "/dev/mmcblk0p2 / ext4 rw,relatime 0 0",
                f"/dev/sdb1 {stick} vfat rw,nosuid 0 0",
                "/dev/sdc1 /media/tmpfs tmpfs rw 0 0",
                "/dev/sdd1 /elsewhere ext4 rw 0 0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "root": root,
        "stick": stick,
        "internal": tmp_path / "internal",
        "sys_block": sys_block,
        "mounts": mounts,
    }


def _drives(media):
    return mounted_usb_drives(
        mounts_path=media["mounts"],
        sys_block=media["sys_block"],
        mount_roots=(media["root"],),
    )


def test_only_mounted_usb_volumes_under_media_are_found(media) -> None:
    drives = _drives(media)
    assert len(drives) == 1
    option = drives[0]
    assert option.label == "STICK"
    assert option.path == media["stick"] / "ministar"
    assert option.removable is True
    assert option.writable is True


def test_a_mountpoint_with_a_space_is_unescaped(tmp_path: Path) -> None:
    root = tmp_path / "media"
    root.mkdir()
    stick = root / "My Stick"
    stick.mkdir()
    sys_block = tmp_path / "sys" / "block"
    (sys_block / "sdb1").mkdir(parents=True)
    (sys_block / "sdb1" / "removable").write_text("1\n", encoding="utf-8")
    mounts = tmp_path / "mounts"
    mounts.write_text(f"/dev/sdb1 {root}/My\\040Stick vfat rw 0 0\n", encoding="utf-8")

    drives = mounted_usb_drives(
        mounts_path=mounts, sys_block=sys_block, mount_roots=(root,)
    )
    assert [drive.path for drive in drives] == [stick / "ministar"]


def test_internal_output_is_always_an_option_and_the_default_is_the_usb(media) -> None:
    internal = internal_option(media["internal"])
    options = output_options(
        internal.path,
        mounts_path=media["mounts"],
        sys_block=media["sys_block"],
        mount_roots=(media["root"],),
    )
    assert options[0].path == internal.path
    assert options[0].removable is False
    assert [option.removable for option in options[1:]] == [True]

    default = default_output_root(
        mounts_path=media["mounts"],
        sys_block=media["sys_block"],
        mount_roots=(media["root"],),
    )
    assert default == media["stick"] / "ministar"


def test_a_missing_selection_is_listed_as_unavailable(media) -> None:
    gone = media["root"] / "GONE" / "ministar"
    options = output_options(
        media["internal"],
        gone,
        mounts_path=media["mounts"],
        sys_block=media["sys_block"],
        mount_roots=(media["root"],),
    )
    assert options[-1].path == gone
    assert options[-1].mounted is False
    assert options[-1].writable is False
    assert "unavailable" in options[-1].display


def test_creating_an_internal_folder_is_allowed(media) -> None:
    target = media["internal"] / "session"
    ensure_output_root(target, mount_roots=(media["root"],))
    assert target.is_dir()


def test_creating_a_folder_on_a_mounted_stick_is_allowed(media) -> None:
    target = media["stick"] / "ministar" / "session"
    ensure_output_root(
        target, mounts_path=media["mounts"], mount_roots=(media["root"],)
    )
    assert target.is_dir()


def test_creating_a_folder_for_an_unmounted_stick_is_refused(media) -> None:
    target = media["root"] / "GONE" / "ministar" / "session"
    with pytest.raises(StorageError, match="not mounted"):
        ensure_output_root(
            target, mounts_path=media["mounts"], mount_roots=(media["root"],)
        )
    assert not (media["root"] / "GONE").exists(), "nothing may be created under /media"


def test_removable_paths_are_recognised(media) -> None:
    assert is_removable_path(media["stick"] / "ministar", mount_roots=(media["root"],))
    assert not is_removable_path(media["internal"], mount_roots=(media["root"],))


def test_a_partition_inherits_removable_and_usb_from_its_disk(tmp_path: Path) -> None:
    """The real sysfs shape: /sys/class/block/sda1 -> .../block/sda/sda1."""
    root = tmp_path / "media"
    root.mkdir()
    stick = root / "STICK"
    stick.mkdir()
    sys_block = tmp_path / "sys" / "block"
    (sys_block / "sdb" / "sdb1").mkdir(parents=True)
    (sys_block / "sdb" / "removable").write_text("1\n", encoding="utf-8")
    (sys_block / "sdb1").symlink_to("sdb/sdb1")
    mounts = tmp_path / "mounts"
    mounts.write_text(f"/dev/sdb1 {stick} exfat rw 0 0\n", encoding="utf-8")

    drives = mounted_usb_drives(
        mounts_path=mounts, sys_block=sys_block, mount_roots=(root,)
    )
    assert [drive.path for drive in drives] == [stick / "ministar"]
