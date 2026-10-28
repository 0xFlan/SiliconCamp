#!/usr/bin/env python3
"""
Windows space allocation for SiliconCamp.

Boot Camp's install step shrank the macOS volume, then created one FAT32
partition labelled ``BOOTCAMP`` for Windows Setup.  This module measures that
step against this Mac's own APFS container, and can perform it.

The write path is implemented and exercised end to end, but it refuses the
container that holds ``/`` until Windows has a boot path.  Taking tens of
gigabytes from the internal SSD to hold an installation that cannot start would
cost the user capacity for nothing, so the policy gate (``BOOT_PATH_READY``)
lives here rather than in the UI.  ``rehearse`` runs the exact production
commands against a throwaway APFS disk image, so the code is proven without
touching the internal disk:

    shrink:  diskutil apfs resizeContainer <container> <capacity - requested>
    create:  diskutil addPartition <physical store> "MS-DOS FAT32" BOOTCAMP <size>
    restore: diskutil eraseVolume free none <partition>
             diskutil apfs resizeContainer <container> 0

Measured behaviour this module depends on:

  * ``diskutil`` sizes with a ``g`` suffix are decimal (``64g`` is
    64,000,000,000 bytes, minus cluster rounding), matching Windows' and
    Boot Camp's GB;
  * the APFS container that ``diskutil apfs list`` reports is synthesised
    (``disk3`` on this Mac), and ``addPartition`` must name its *physical
    store* (``disk0s2``), never the synthesised node;
  * ``resizeContainer`` accepts a byte count and reports the new ceiling, so
    the partition can be sized exactly rather than fit-to-fill;
  * ``addPartition`` leaves the free space it did not use unallocated, which
    is why the container is shrunk by exactly the requested size first.

CLI::

    python3 allocation.py space
    python3 allocation.py plan --gb 128
    python3 allocation.py apply --gb 128 --container disk5 --yes
    python3 allocation.py remove --container disk5 --yes
    python3 allocation.py rehearse --gb 64
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path

GB = 1000**3
MIB = 1024**2

#: Windows 11 Arm64 refuses to install on less than this.
MIN_WINDOWS_GB = 64

#: Keep macOS responsive: at least 20 GiB or 10% of what is free, whichever is
#: larger.  Boot Camp made a similar promise ("at least 20 GB").
RESERVE_GB = 20
RESERVE_FRACTION = 0.10

DISKUTIL = "/usr/sbin/diskutil"
HDIUTIL = "/usr/bin/hdiutil"

PARTITION_NAME = "BOOTCAMP"
FAT32_PERSONALITY = "MS-DOS FAT32"

#: Flipped only with a Windows loader-to-kernel handoff that actually boots on
#: this hardware.  Until then the internal resize would take space for an
#: install that cannot start, so ``apply_space`` refuses the container that
#: holds ``/`` and ``plan_space`` reports the action as unavailable.
BOOT_PATH_READY = False

APPLY_BLOCKER = (
    "Windows has never reached the kernel on this Mac. The partition would "
    "hold an installation that cannot boot, so the internal resize stays "
    "disabled until the handoff works. The engine itself is complete and can "
    "be rehearsed on a throwaway disk image."
)


class AllocationError(RuntimeError):
    """The layout cannot be planned, or the write was refused or failed."""


# ---------------------------------------------------------------------------
# Command plumbing.
# ---------------------------------------------------------------------------


def _run(argv: list[str], *, timeout: float = 60.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AllocationError(f"cannot run {argv[0]}: {exc}") from exc


def _checked(argv: list[str], *, timeout: float = 900.0) -> str:
    result = _run(argv, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        tail = " | ".join(detail[-3:]) if detail else "no output"
        raise AllocationError(f"{' '.join(argv[:3])} failed ({result.returncode}): {tail}")
    return result.stdout


def _plist(argv: list[str], *, timeout: float = 60.0) -> dict:
    result = _run(argv, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise AllocationError(f"{' '.join(argv[:3])} failed: {detail}")
    try:
        return plistlib.loads(result.stdout.encode("utf-8"))
    except (plistlib.InvalidFileException, ValueError) as exc:
        raise AllocationError(f"cannot parse {argv[1]} output: {exc}") from exc


# ---------------------------------------------------------------------------
# Reading this Mac.
# ---------------------------------------------------------------------------


def _containers() -> list[dict]:
    listing = _plist([DISKUTIL, "apfs", "list", "-plist"])
    rows = listing.get("Containers", [])
    if not isinstance(rows, list):
        raise AllocationError("diskutil apfs list returned no containers")
    return rows


def _normalise_reference(reference: str) -> str:
    return reference.strip().removeprefix("/dev/").removeprefix("r")


def host_container() -> str:
    """The APFS container reference that holds ``/`` (e.g. ``disk3``)."""
    info = _plist([DISKUTIL, "info", "-plist", "/"])
    parent = _normalise_reference(str(info.get("ParentWholeDisk") or ""))
    if not parent:
        raise AllocationError("cannot identify the container that holds /")
    return parent


def container_info(reference: str = "") -> dict:
    """Everything the planner and the writer need about one APFS container."""
    ref = _normalise_reference(reference) if reference else host_container()
    entry = next((row for row in _containers() if row.get("ContainerReference") == ref), None)
    if entry is None:
        raise AllocationError(f"no APFS container named {ref}")
    store = _normalise_reference(str(entry.get("DesignatedPhysicalStore") or ""))
    whole = ""
    if store:
        store_info = _plist([DISKUTIL, "info", "-plist", f"/dev/{store}"])
        whole = _normalise_reference(str(store_info.get("ParentWholeDisk") or ""))
    capacity = int(entry.get("CapacityCeiling") or 0)
    free = int(entry.get("CapacityFree") or 0)
    volumes = []
    for volume in entry.get("Volumes", []) or []:
        volumes.append(
            {
                "name": str(volume.get("Name") or ""),
                "device": _normalise_reference(str(volume.get("DeviceIdentifier") or "")),
                "role": str(volume.get("Role") or ""),
                "mount": str(volume.get("MountPoint") or ""),
            }
        )
    return {
        "reference": ref,
        "device": f"/dev/{ref}",
        "capacity": capacity,
        "free": free,
        "used": max(0, capacity - free),
        "store": f"/dev/{store}" if store else "",
        "whole_disk": f"/dev/{whole}" if whole else "",
        "volumes": volumes,
        "is_system": ref == host_container(),
    }


def _stats_from(info: dict) -> dict[str, object]:
    capacity = int(info["capacity"])
    free = int(info["free"])
    reserve = max(RESERVE_GB * GB, int(free * RESERVE_FRACTION))
    max_bytes = max(0, free - reserve)
    volume = ""
    for row in info.get("volumes", []):
        if row.get("mount") == "/":
            volume = str(row["name"])
    return {
        "container": info["reference"],
        "disk": info["device"],
        "volume": volume or "Macintosh HD",
        "capacity": capacity,
        "free": free,
        "reserve": reserve,
        "max_bytes": max_bytes,
        "max_gb": max_bytes // GB,
        "minimum_gb": MIN_WINDOWS_GB,
        "source": "diskutil apfs list",
    }


def read_host_space() -> dict[str, object]:
    """Capacity and free space of the APFS container that holds ``/``."""
    return _stats_from(container_info())


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
            "name": PARTITION_NAME,
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
            f"create one GPT partition labelled {PARTITION_NAME}, FAT32, {requested} GB",
            "leave the existing EFI System Partition in place for Windows Setup",
        ],
    }


# ---------------------------------------------------------------------------
# Writing.
# ---------------------------------------------------------------------------


def _partitions(whole_disk: str) -> list[dict]:
    if not whole_disk:
        return []
    listing = _plist([DISKUTIL, "list", "-plist", whole_disk])
    rows = listing.get("AllDisksAndPartitions", [])
    if not rows:
        return []
    return list(rows[0].get("Partitions", []) or [])


def find_bootcamp(whole_disk: str) -> dict:
    # `diskutil list -plist` calls the label VolumeName; `diskutil info` calls
    # the same string VolumeName too, but a partition with no filesystem only
    # has Content, so both are consulted.
    for part in _partitions(whole_disk):
        label = str(part.get("VolumeName") or part.get("Name") or "").upper()
        if label == PARTITION_NAME:
            return part
    return {}


def _partition_detail(device: str) -> dict:
    info = _plist([DISKUTIL, "info", "-plist", device])
    return {
        "device": device,
        "name": str(info.get("VolumeName") or ""),
        "filesystem": str(info.get("FilesystemType") or ""),
        "mount": str(info.get("MountPoint") or ""),
        "bytes": int(info.get("TotalSize") or info.get("Size") or 0),
        "content": str(info.get("Content") or info.get("PartitionType") or ""),
    }


def _assert_writable(info: dict) -> None:
    if info["is_system"] and not BOOT_PATH_READY:
        raise AllocationError(APPLY_BLOCKER)


def _restore_after_failure(info: dict, original: int, part: dict) -> str:
    """Undo a failed allocation: drop the new partition, grow the container.

    Returns what could not be undone, so the caller can report a real failure
    rather than claiming a clean rollback.  Used for every failure after the
    container has been shrunk, including a verification failure.
    """
    problems: list[str] = []
    # `addPartition` can fail after it has already changed the map; look again
    # rather than trusting the caller's last snapshot.
    part = part or find_bootcamp(info["whole_disk"])
    if part.get("DeviceIdentifier"):
        try:
            _checked(
                [
                    DISKUTIL,
                    "eraseVolume",
                    "free",
                    "none",
                    f"/dev/{part['DeviceIdentifier']}",
                ],
                timeout=900.0,
            )
        except AllocationError as exc:
            problems.append(f"could not delete the partition: {exc}")
    try:
        _checked(
            [DISKUTIL, "apfs", "resizeContainer", info["reference"], str(original)],
            timeout=1800.0,
        )
    except AllocationError as exc:
        problems.append(f"could not grow the container back: {exc}")
    return "; ".join(problems)


def apply_space(
    gb: int,
    *,
    container: str = "",
    confirm: bool = False,
    log=print,
) -> dict[str, object]:
    """Shrink an APFS container and create the BOOTCAMP partition in it.

    Refuses the container that holds ``/`` while ``BOOT_PATH_READY`` is false.
    A failure after the container has been shrunk restores the original
    ceiling and reports whether that rollback worked.
    """
    if not confirm:
        raise AllocationError(
            f"refusing to resize the APFS container and create {PARTITION_NAME} without confirm=True"
        )
    started = time.monotonic()
    info = container_info(container)
    _assert_writable(info)

    plan = plan_space(int(gb), stats=_stats_from(info))
    if not plan["fit"]:
        raise AllocationError("; ".join(str(item) for item in plan["blockers"]))
    if find_bootcamp(info["whole_disk"]):
        raise AllocationError(
            f"{info['whole_disk']} already has a {PARTITION_NAME} partition; "
            "remove it before allocating another"
        )

    wanted = int(gb) * GB
    original = int(info["capacity"])
    new_ceiling = original - wanted
    if new_ceiling <= int(info["used"]) + GB:
        raise AllocationError(
            f"the container holds {info['used'] // GB} GB of data; shrinking it to "
            f"{new_ceiling // GB} GB would leave no working space for macOS"
        )

    log(
        f"shrinking {info['reference']} from {original / GB:.1f} GB to "
        f"{new_ceiling / GB:.1f} GB"
    )
    _checked(
        [DISKUTIL, "apfs", "resizeContainer", info["reference"], str(new_ceiling)],
        timeout=1800.0,
    )

    part: dict = {}
    try:
        log(
            f"creating {PARTITION_NAME} ({gb} GB FAT32) on the physical store "
            f"{info['store']}"
        )
        _checked(
            [
                DISKUTIL,
                "addPartition",
                info["store"],
                FAT32_PERSONALITY,
                PARTITION_NAME,
                f"{int(gb)}g",
            ],
            timeout=1800.0,
        )
        part = find_bootcamp(info["whole_disk"])
        if not part:
            raise AllocationError(
                f"diskutil reported success but {info['whole_disk']} has no "
                f"{PARTITION_NAME} partition"
            )
        detail = _partition_detail(f"/dev/{part['DeviceIdentifier']}")
        if detail["filesystem"] != "msdos":
            raise AllocationError(
                f"{detail['device']} was created but its filesystem is "
                f"{detail['filesystem'] or 'unknown'}, not FAT32"
            )
        if detail["bytes"] < wanted * 0.995:
            raise AllocationError(
                f"{detail['device']} is {detail['bytes'] / GB:.2f} GB, smaller than the "
                f"requested {gb} GB"
            )
        after = container_info(info["reference"])
        shrank = original - int(after["capacity"])
        if shrank < wanted * 0.99:
            raise AllocationError(
                f"the container shrank by {shrank / GB:.1f} GB, not the requested "
                f"{gb} GB"
            )
    except AllocationError as exc:
        log(f"allocation failed: {exc}")
        log("restoring the original container size and partition map")
        problems = _restore_after_failure(info, original, part)
        if problems:
            raise AllocationError(
                f"{exc}; restoring the original layout also failed ({problems})"
            ) from exc
        raise AllocationError(
            f"{exc}; the container and partition map were restored"
        ) from exc

    log(
        f"created {detail['device']} ({detail['bytes'] / GB:.1f} GB FAT32) at "
        f"{detail['mount'] or 'no mount point'}"
    )
    return {
        "ok": True,
        "container": info["reference"],
        "device": detail["device"],
        "volume": detail["name"],
        "mount": detail["mount"],
        "requested_gb": int(gb),
        "bytes": detail["bytes"],
        "container_before": original,
        "container_after": int(after["capacity"]),
        "seconds": round(time.monotonic() - started, 1),
    }


def remove_space(*, container: str = "", confirm: bool = False, log=print) -> dict[str, object]:
    """Delete BOOTCAMP and give its space back to the APFS container."""
    if not confirm:
        raise AllocationError(
            f"refusing to delete {PARTITION_NAME} and grow the container without confirm=True"
        )
    started = time.monotonic()
    info = container_info(container)
    _assert_writable(info)
    part = find_bootcamp(info["whole_disk"])
    if not part:
        raise AllocationError(f"no {PARTITION_NAME} partition on {info['whole_disk']}")
    device = f"/dev/{part['DeviceIdentifier']}"
    detail = _partition_detail(device)
    log(f"erasing {device} ({detail['bytes'] / GB:.1f} GB, {detail['name'] or 'unnamed'})")
    _checked([DISKUTIL, "eraseVolume", "free", "none", device], timeout=900.0)
    log(f"growing {info['reference']} back to the free space")
    try:
        _checked(
            [DISKUTIL, "apfs", "resizeContainer", info["reference"], "0"],
            timeout=1800.0,
        )
    except AllocationError as exc:
        raise AllocationError(
            f"{PARTITION_NAME} was deleted, but growing the container back failed "
            f"({exc}); the space is unassigned rather than returned"
        ) from exc
    if find_bootcamp(info["whole_disk"]):
        raise AllocationError(f"{PARTITION_NAME} is still present after the erase")
    after = container_info(info["reference"])
    return {
        "ok": True,
        "container": info["reference"],
        "removed": device,
        "bytes": detail["bytes"],
        "container_before": int(info["capacity"]),
        "container_after": int(after["capacity"]),
        "seconds": round(time.monotonic() - started, 1),
    }


# ---------------------------------------------------------------------------
# Rehearsal: the production commands on a throwaway APFS container.
# ---------------------------------------------------------------------------


def _attach_image(image: Path) -> str:
    result = _plist([HDIUTIL, "attach", "-readwrite", "-nobrowse", "-plist", str(image)], timeout=180.0)
    for entity in result.get("system-entities", []):
        if entity.get("content-hint") == "GUID_partition_scheme":
            return str(entity.get("dev-entry"))
    raise AllocationError(f"{image.name} attached but no whole disk appeared")


def rehearsal_size(gb: int) -> int:
    """An image large enough for the partition *and* the macOS reserve.

    The rehearsal runs the production policy, including the 20 GB / 10% macOS
    reserve, so a 64 GB rehearsal on a 72 GB image would be refused for the
    right reason.  The image has to be the size of a Mac that could host this
    partition, not the partition alone.
    """
    gb = int(gb)
    need = gb + RESERVE_GB + 2
    # Above roughly 200 GB the 10% reserve overtakes the flat 20 GB.
    if gb + RESERVE_GB > 200:
        need = int(gb / (1 - RESERVE_FRACTION)) + 2
    return need * GB


def rehearse_space(gb: int, *, log=print, keep: bool = False) -> dict[str, object]:
    """Run apply + remove against a fresh APFS disk image.

    This is the only place the write path runs on this machine today, and it is
    the same code the internal disk will use: no mocks, no simulated output.
    """
    started = time.monotonic()
    gb = int(gb)
    if gb < MIN_WINDOWS_GB:
        raise AllocationError(f"Windows 11 Arm64 needs at least {MIN_WINDOWS_GB} GB")
    workdir = Path(tempfile.mkdtemp(prefix="siliconcamp-rehearsal-"))
    image = workdir / "rehearsal.sparseimage"
    whole = ""
    try:
        log(
            f"creating a throwaway APFS image ({rehearsal_size(gb) // GB} GB, sparse) "
            f"at {image}"
        )
        _checked(
            [
                HDIUTIL,
                "create",
                "-size",
                f"{rehearsal_size(gb) // GB}g",
                "-type",
                "SPARSE",
                "-fs",
                "APFS",
                "-volname",
                "SiliconCamp Rehearsal",
                str(image),
            ],
            timeout=600.0,
        )
        whole = _attach_image(image)
        store = ""
        for part in _partitions(whole):
            if part.get("Content") == "Apple_APFS":
                store = str(part.get("DeviceIdentifier") or "")
                break
        if not store:
            raise AllocationError("the rehearsal image has no APFS container")
        reference = next(
            (
                str(row.get("ContainerReference"))
                for row in _containers()
                if row.get("DesignatedPhysicalStore") == store
            ),
            "",
        )
        if not reference:
            raise AllocationError("the rehearsal image's container is not in apfs list")
        log(f"rehearsal container: {reference} (physical store {store})")

        created = apply_space(gb, container=reference, confirm=True, log=log)
        log(f"rehearsal partition verified at {created['mount'] or created['device']}")
        restored = remove_space(container=reference, confirm=True, log=log)
        log(
            f"restored the container to {restored['container_after'] / GB:.1f} GB; "
            f"rehearsal passed in {time.monotonic() - started:.1f}s"
        )
        return {
            "ok": True,
            "requested_gb": gb,
            "image": str(image),
            "container": reference,
            "created": created,
            "restored": restored,
            "seconds": round(time.monotonic() - started, 1),
        }
    finally:
        if whole:
            _run([HDIUTIL, "detach", whole], timeout=120.0)
        if keep:
            log(f"kept the rehearsal image at {image}")
        else:
            for path in (image, workdir):
                try:
                    if path.is_dir():
                        path.rmdir()
                    elif path.exists():
                        path.unlink()
                except OSError as exc:
                    log(f"could not remove {path}: {exc}")


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SiliconCamp Windows space allocation")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("space", help="show the APFS container and how much is allocatable")

    plan = sub.add_parser("plan", help="plan a BOOTCAMP partition")
    plan.add_argument("--gb", type=int, default=128)

    apply = sub.add_parser("apply", help="shrink the container and create BOOTCAMP")
    apply.add_argument("--gb", type=int, required=True)
    apply.add_argument("--container", default="")
    apply.add_argument("--yes", action="store_true", help="confirm the resize")

    remove = sub.add_parser("remove", help="delete BOOTCAMP and grow the container")
    remove.add_argument("--container", default="")
    remove.add_argument("--yes", action="store_true", help="confirm the deletion")

    rehearse = sub.add_parser("rehearse", help="run apply and remove on a throwaway image")
    rehearse.add_argument("--gb", type=int, default=MIN_WINDOWS_GB)
    rehearse.add_argument("--keep", action="store_true")

    args = parser.parse_args(argv)

    def log(message: str) -> None:
        print(message, file=sys.stderr)

    try:
        if args.command == "space":
            print(json.dumps(read_host_space(), indent=2))
            return 0
        if args.command == "plan":
            print(json.dumps(plan_space(args.gb), indent=2))
            return 0
        if args.command == "apply":
            print(json.dumps(apply_space(args.gb, container=args.container, confirm=args.yes, log=log), indent=2))
            return 0
        if args.command == "remove":
            print(json.dumps(remove_space(container=args.container, confirm=args.yes, log=log), indent=2))
            return 0
        print(json.dumps(rehearse_space(args.gb, log=log, keep=args.keep), indent=2))
        return 0
    except AllocationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
