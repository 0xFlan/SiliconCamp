#!/usr/bin/env python3
"""
Windows space allocation for SiliconCamp.

Boot Camp's install step shrank the macOS volume and created one FAT32
partition labelled ``BOOTCAMP`` for Windows Setup.  This module computes that
step from this Mac's own APFS container: capacity, free space, the reserve
macOS keeps, and the largest partition that fits.

It deliberately does not apply the resize.  Taking tens of gigabytes from the
APFS container to hold a Windows installation that cannot boot yet would cost
the user capacity for nothing, so the write is gated on the same missing
boot-path handoff as the Install button.  The plan itself is measured, not
estimated: it comes from ``diskutil apfs list`` and is the arithmetic the apply
step will use once the boot path exists.

CLI::

    python3 allocation.py space
    python3 allocation.py plan --gb 128
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys

GB = 1000**3

#: Windows 11 Arm64 refuses to install on less than this.
MIN_WINDOWS_GB = 64

#: Keep macOS responsive: at least 20 GiB or 10% of what is free, whichever is
#: larger.  Boot Camp made a similar promise ("at least 20 GB").
RESERVE_GB = 20
RESERVE_FRACTION = 0.10

DISKUTIL = "/usr/sbin/diskutil"

#: Flipped only with a Windows loader-to-kernel handoff that actually boots on
#: this hardware.  Until then the resize would take space for an install that
#: cannot start, so ``plan_space`` reports it as blocked rather than offering a
#: button that silently does nothing.
BOOT_PATH_READY = False

APPLY_BLOCKER = (
    "Windows has never reached the kernel on this Mac. The partition would "
    "hold an installation that cannot boot, so the resize stays disabled "
    "until the handoff works."
)


class AllocationError(RuntimeError):
    """The layout cannot be planned, or the write was refused."""


# ---------------------------------------------------------------------------
# Reading this Mac.
# ---------------------------------------------------------------------------


def _run(argv: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AllocationError(f"cannot run {argv[0]}: {exc}") from exc


def _plist(argv: list[str], *, timeout: float = 60.0) -> dict:
    result = _run(argv, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise AllocationError(f"{' '.join(argv[:3])} failed: {detail}")
    try:
        return plistlib.loads(result.stdout.encode("utf-8"))
    except (plistlib.InvalidFileException, ValueError) as exc:
        raise AllocationError(f"cannot parse {argv[1]} output: {exc}") from exc


def read_host_space() -> dict[str, object]:
    """Capacity and free space of the APFS container that holds ``/``."""
    root = _plist([DISKUTIL, "info", "-plist", "/"])
    parent = str(root.get("ParentWholeDisk") or "")
    if not parent:
        raise AllocationError("cannot identify the disk that holds /")

    capacity = 0
    free = 0
    container = ""
    try:
        listing = _plist([DISKUTIL, "apfs", "list", "-plist"])
        for entry in listing.get("Containers", []):
            if entry.get("ContainerReference") == parent:
                container = parent
                capacity = int(entry.get("CapacityCeiling") or 0)
                free = int(entry.get("CapacityFree") or 0)
                break
    except AllocationError:
        container = ""

    if not capacity:
        # An APFS container is the normal case; a plain HFS+ or a synthesised
        # test disk still has a usable whole-disk size.
        whole = _plist([DISKUTIL, "info", "-plist", f"/dev/{parent}"])
        capacity = int(whole.get("TotalSize") or whole.get("Size") or 0)
        usage = os.statvfs("/")
        free = int(usage.f_bavail) * int(usage.f_frsize)

    reserve = max(RESERVE_GB * GB, int(free * RESERVE_FRACTION))
    max_bytes = max(0, free - reserve)
    return {
        "container": container or parent,
        "disk": f"/dev/{parent}",
        "volume": str(root.get("VolumeName") or "Macintosh HD"),
        "capacity": capacity,
        "free": free,
        "reserve": reserve,
        "max_bytes": max_bytes,
        "max_gb": max_bytes // GB,
        "minimum_gb": MIN_WINDOWS_GB,
        "source": "diskutil apfs list",
    }


def plan_space(gb: int, *, stats: dict[str, object] | None = None) -> dict[str, object]:
    """Size one BOOTCAMP partition against the container's real free space."""
    host = stats if stats is not None else read_host_space()
    requested = int(gb)
    reserve = int(host["reserve"])
    free = int(host["free"])
    max_gb = int(host["max_gb"])

    blockers: list[str] = []
    warnings: list[str] = []

    if requested < MIN_WINDOWS_GB:
        blockers.append(
            f"Windows 11 Arm64 needs at least {MIN_WINDOWS_GB} GB; {requested} GB is too small."
        )
    if requested > max_gb:
        blockers.append(
            f"only {max_gb} GB can be allocated while keeping {reserve // GB} GB "
            "free for macOS."
        )

    wanted = requested * GB
    if not blockers and free - wanted < reserve:
        blockers.append("that leaves macOS below its reserve")
    if not blockers and free - wanted < reserve + 10 * GB:
        warnings.append("macOS will be close to full; consider a smaller partition.")

    layout = [
        {
            "name": "BOOTCAMP",
            "role": "Windows",
            "format": "FAT32",
            "bytes": wanted,
            "note": "Windows Setup reformats this NTFS and installs here.",
        }
    ]

    return {
        "requested_gb": requested,
        "fit": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "layout": layout,
        "host": host,
        "after": {
            "free": max(0, free - wanted),
            "reserve": reserve,
        },
        "apply": {
            "enabled": bool(BOOT_PATH_READY and not blockers),
            "reason": "" if BOOT_PATH_READY else APPLY_BLOCKER,
        },
        "steps": [
            f"shrink the APFS container by {requested} GB",
            f"create one GPT partition labelled BOOTCAMP, FAT32, {requested} GB",
            "leave the existing EFI System Partition in place for Windows Setup",
        ],
    }


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SiliconCamp Windows space allocation")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("space", help="show the APFS container and how much is allocatable")

    plan = sub.add_parser("plan", help="plan a BOOTCAMP partition")
    plan.add_argument("--gb", type=int, default=128)

    args = parser.parse_args(argv)

    try:
        if args.command == "space":
            print(json.dumps(read_host_space(), indent=2))
            return 0
        print(json.dumps(plan_space(args.gb), indent=2))
        return 0
    except AllocationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
