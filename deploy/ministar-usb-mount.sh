#!/bin/sh
# Mount a labelled FAT/exFAT USB stick for the ministar console.
#
# Run by udev when a partition appears. systemd-mount creates a transient mount
# unit with --collect, so the mount disappears with the device. The uid/gid options
# make the ministar service user the owner; otherwise exFAT mounts root-owned and the
# app cannot use it.
set -eu

device="${1:?device}"
label="${2:-}"

[ -b "$device" ] || exit 0

# Never mount a partition of the disk that backs /. A USB-booted install must not
# offer its own system partition as session storage.
root_source=$(findmnt -n -o SOURCE / 2>/dev/null || true)
root_disk=$(lsblk -no PKNAME "$root_source" 2>/dev/null | head -1 || true)
device_disk=$(lsblk -no PKNAME "$device" 2>/dev/null | head -1 || true)
if [ -n "$root_disk" ] && [ "$root_disk" = "$device_disk" ]; then
    logger -t ministar-usb "refusing $device: it backs /"
    exit 0
fi

uid=$(id -u ministar 2>/dev/null || echo 1000)
gid=$(id -g ministar 2>/dev/null || echo 1000)

safe=$(printf '%s' "$label" | tr -c 'A-Za-z0-9._-' '_' | sed 's/^_*//;s/_*$//')
[ -n "$safe" ] || safe=$(basename "$device")
mountpoint="/media/$safe"
mkdir -p "$mountpoint"

logger -t ministar-usb "mounting $device at $mountpoint"
exec systemd-mount --no-block --collect \
    --options="uid=$uid,gid=$gid,umask=0022" \
    "$device" "$mountpoint"
