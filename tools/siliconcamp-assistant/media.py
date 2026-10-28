#!/usr/bin/env python3
"""
Installer media engine for SiliconCamp.

Turns a Windows 11 Arm64 ISO into a bootable UEFI installer on a GPT disk or on
a disk image.  It is the part of the old Boot Camp flow that does not depend on
a Windows kernel existing: validating the media, choosing a destination,
partitioning it FAT32, copying the setup files and reading them back.

What the media must contain (Microsoft, "Install Windows from a USB flash
drive"):

    \\EFI\\BOOT\\BOOTAA64.EFI    the removable-media boot application
    \\sources\\boot.wim          Windows PE / Setup
    \\sources\\install.wim       the image, or install.swm parts of it

FAT32 cannot hold a single file larger than 4 GiB.  When ``sources/install.wim``
is bigger than that, the parts have to be written as ``install.swm`` with
``wimlib-imagex split``; this module refuses to erase a destination when no
splitter is available rather than produce media Windows Setup cannot use.

The media is one MBR partition, FAT32, the shape Microsoft documents for a
flash drive.  Two measured reasons: ``diskutil``'s GPT layout silently inserts
an empty 209.7 MB EFI System Partition and puts the payload on a second
"Microsoft Basic Data" partition, which a strict UEFI boot manager will not
scan for ``\\EFI\\BOOT\\BOOTAA64.EFI``; and an MBR FAT32 volume uses the whole
destination instead of costing 200 MB.  UEFI firmware reads the FAT filesystem
through the removable-media path regardless of the partition-table type.

Safety:

  * an image target is a file the user names, never a device;
  * a device target must be a whole, physical, external, USB disk that is not
    the disk backing ``/``, re-checked immediately before it is erased;
  * ``create`` never runs ``sudo`` and never writes to an internal disk;
  * the caller must pass ``confirm=True`` for a physical device.

CLI::

    python3 media.py inspect --iso Win11_ARM64.iso
    python3 media.py targets
    python3 media.py plan --iso Win11_ARM64.iso --image ~/Desktop/installer.sparseimage
    python3 media.py create --iso Win11_ARM64.iso --image ~/Desktop/installer.sparseimage
    python3 media.py create --iso Win11_ARM64.iso --device /dev/disk5 --yes
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

MIB = 1024 * 1024
GIB = 1024 * MIB

#: FAT32's largest file is 4 GiB - 1 byte.  Leave a cluster of slack.
FAT32_FILE_LIMIT = 4 * GIB - 4096

#: Media needs room for the payload plus FAT tables and directory entries.
IMAGE_SLACK = 512 * MIB

#: hdiutil refuses to make a useful FAT32 volume below this, so the plan must
#: quote what will actually be created rather than the payload size.
MIN_IMAGE_BYTES = 4 * GIB

#: Where a Homebrew wimlib lives when the app is launched from Finder, whose
#: PATH is /usr/bin:/bin:/usr/sbin:/sbin.
WIMLIB_CANDIDATES = (
    "/opt/homebrew/bin/wimlib-imagex",
    "/usr/local/bin/wimlib-imagex",
)

HDIUTIL = "/usr/bin/hdiutil"
DISKUTIL = "/usr/sbin/diskutil"

REQUIRED_BOOT = "efi/boot/bootaa64.efi"
REQUIRED_BOOT_WIM = "sources/boot.wim"
INSTALL_CANDIDATES = (
    "sources/install.wim",
    "sources/install.esd",
    "sources/install.swm",
)

IMAGE_SUFFIXES = (".sparseimage", ".sparsebundle", ".dmg")


class MediaError(RuntimeError):
    """A media operation failed, or was refused before it could destroy data."""


# ---------------------------------------------------------------------------
# Payload: what the ISO actually contains.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PayloadFile:
    relative: str
    size: int


@dataclass(frozen=True)
class Payload:
    root: str
    boot_efi: str
    boot_wim: str
    install_image: str
    install_kind: str
    install_bytes: int
    needs_split: bool
    oversized: tuple[str, ...]
    total_bytes: int
    file_count: int
    files: tuple[PayloadFile, ...]

    def summary(self) -> dict[str, object]:
        return {
            "root": self.root,
            "boot_efi": self.boot_efi,
            "boot_wim": self.boot_wim,
            "install_image": self.install_image,
            "install_kind": self.install_kind,
            "install_bytes": self.install_bytes,
            "needs_split": self.needs_split,
            "oversized": list(self.oversized),
            "total_bytes": self.total_bytes,
            "file_count": self.file_count,
        }


def scan_payload(root: str | Path) -> Payload:
    """Read a mounted Windows media tree and prove it is one.

    Nothing is copied here: the required paths, the install image, the FAT32
    limit and case collisions are all decided before a destination is touched.
    """
    base = Path(root)
    if not base.is_dir():
        raise MediaError(f"not a directory: {base}")

    files: list[PayloadFile] = []
    seen: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames.sort()
        for name in sorted(filenames):
            full = Path(dirpath) / name
            try:
                size = full.stat().st_size
            except OSError as exc:
                raise MediaError(f"cannot read {full}: {exc}") from exc
            relative = full.relative_to(base).as_posix()
            key = relative.lower()
            if key in seen:
                # FAT32 is case-insensitive; two entries that differ only by
                # case cannot both exist on the destination.
                raise MediaError(
                    f"two files differ only by case: {seen[key]!r} and {relative!r}"
                )
            seen[key] = relative
            files.append(PayloadFile(relative, size))

    if not files:
        raise MediaError(f"no files in {base}")

    boot_efi = seen.get(REQUIRED_BOOT)
    if not boot_efi:
        raise MediaError(
            "not a Windows Arm64 installer: EFI/BOOT/BOOTAA64.EFI is missing"
        )
    boot_wim = seen.get(REQUIRED_BOOT_WIM)
    if not boot_wim:
        raise MediaError("not a Windows installer: sources/boot.wim is missing")

    install_kind = ""
    install_image = ""
    install_bytes = 0
    swm_total = 0
    for candidate in INSTALL_CANDIDATES:
        match = seen.get(candidate)
        if match:
            install_kind = candidate.rsplit(".", 1)[1]
            install_image = match
            if install_kind == "swm":
                swm_total = sum(
                    entry.size
                    for entry in files
                    if entry.relative.lower().startswith("sources/install")
                    and entry.relative.lower().endswith(".swm")
                )
                install_bytes = swm_total
            else:
                install_bytes = next(
                    entry.size for entry in files if entry.relative == match
                )
            break
    if not install_image:
        raise MediaError(
            "not a Windows installer: sources/install.wim, .esd or .swm is missing"
        )

    needs_split = install_kind == "wim" and install_bytes > FAT32_FILE_LIMIT

    # An oversized boot.wim or a big driver file cannot be split the way the
    # install image can, so it is a blocker rather than something to retry.
    oversized = tuple(
        entry.relative
        for entry in files
        if entry.size > FAT32_FILE_LIMIT and entry.relative != install_image
    )

    total = sum(entry.size for entry in files)
    return Payload(
        root=str(base),
        boot_efi=boot_efi,
        boot_wim=boot_wim,
        install_image=install_image,
        install_kind=install_kind,
        install_bytes=install_bytes,
        needs_split=needs_split,
        oversized=oversized,
        total_bytes=total,
        file_count=len(files),
        files=tuple(files),
    )


# ---------------------------------------------------------------------------
# Command plumbing.
# ---------------------------------------------------------------------------


def _run(argv: list[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError(f"cannot run {argv[0]}: {exc}") from exc


def _checked(argv: list[str], *, timeout: float = 120.0) -> str:
    result = _run(argv, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        tail = " | ".join(detail[-3:]) if detail else "no output"
        raise MediaError(f"{' '.join(argv[:3])} failed ({result.returncode}): {tail}")
    return result.stdout


def _plist(argv: list[str], *, timeout: float = 30.0) -> dict:
    result = _run(argv, timeout=timeout)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise MediaError(f"{' '.join(argv[:3])} failed: {detail}")
    try:
        return plistlib.loads(result.stdout.encode("utf-8"))
    except (plistlib.InvalidFileException, ValueError) as exc:
        raise MediaError(f"cannot parse {argv[1]} output: {exc}") from exc


def find_splitter() -> str:
    """Locate wimlib-imagex, including where Homebrew puts it off PATH."""
    found = shutil.which("wimlib-imagex")
    if found:
        return found
    for candidate in WIMLIB_CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate
    return ""


# ---------------------------------------------------------------------------
# ISO mounting.
# ---------------------------------------------------------------------------


def mount_iso(iso: str | Path) -> tuple[str, str]:
    """Attach a Windows ISO read-only and return (mount point, device)."""
    path = Path(iso).expanduser()
    if not path.is_file():
        raise MediaError(f"ISO not found: {path}")
    if not os.access(path, os.R_OK):
        raise MediaError(f"ISO is not readable: {path}")

    info = _plist([HDIUTIL, "attach", "-readonly", "-nobrowse", "-plist", str(path)], timeout=180.0)
    entities = info.get("system-entities", [])
    for entity in entities:
        mount = entity.get("mount-point")
        device = entity.get("dev-entry")
        if mount and device:
            return str(mount), str(device)
    for entity in entities:
        device = entity.get("dev-entry")
        if device:
            _run([HDIUTIL, "detach", str(device)], timeout=60.0)
    raise MediaError(f"{path.name} mounted but no volume appeared")


def detach(device: str) -> None:
    _run([HDIUTIL, "detach", device], timeout=120.0)


def inspect_iso(iso: str | Path) -> Payload:
    """Mount an ISO, read its tree, and always detach it again."""
    mount, device = mount_iso(iso)
    try:
        return scan_payload(mount)
    finally:
        detach(device)


# ---------------------------------------------------------------------------
# Destinations.
# ---------------------------------------------------------------------------


def disk_info(device: str) -> dict:
    return _plist([DISKUTIL, "info", "-plist", device])


def boot_disk() -> str:
    """The whole disk that backs /, so it can never be a target."""
    try:
        info = disk_info("/")
    except MediaError:
        return ""
    parent = info.get("ParentWholeDisk")
    return f"/dev/{parent}" if parent else ""


def list_targets() -> dict[str, object]:
    """Return (external USB disks, splitter path, boot disk).

    A candidate must be a whole, physical, external USB disk.  ``Removable`` is
    reported but never used as the test: USB bridges often report fixed media,
    and attached disk images report themselves removable.
    """
    targets: list[dict[str, object]] = []
    listing = _plist([DISKUTIL, "list", "-plist", "external", "physical"])
    for name in listing.get("WholeDisks", []):
        device = f"/dev/{name}"
        try:
            info = disk_info(device)
        except MediaError:
            continue
        if info.get("Internal") is True:
            continue
        if info.get("VirtualOrPhysical") == "Virtual":
            continue
        if info.get("WholeDisk") is False:
            continue
        protocol = str(info.get("BusProtocol") or info.get("Protocol") or "")
        if protocol.upper() != "USB":
            continue
        targets.append(
            {
                "device": device,
                "name": str(info.get("MediaName") or info.get("VolumeName") or name),
                "size": int(info.get("TotalSize") or info.get("Size") or 0),
                "protocol": protocol,
                "removable": bool(info.get("RemovableMedia", False)),
                "ejectable": bool(info.get("Ejectable", False)),
            }
        )

    reserve = boot_disk()
    targets = [row for row in targets if row["device"] != reserve]
    targets.sort(key=lambda row: str(row["device"]))
    return {
        "targets": targets,
        "wimlib": find_splitter(),
        "boot_disk": reserve,
    }


def image_size_for(payload: Payload) -> int:
    return max(payload.total_bytes + IMAGE_SLACK, MIN_IMAGE_BYTES)


def normalise_image(path: str | Path) -> Path:
    image = Path(path).expanduser()
    if not image.is_absolute():
        raise MediaError("the image path must be absolute")
    if image.suffix not in IMAGE_SUFFIXES:
        raise MediaError(
            f"the image must end in one of {', '.join(IMAGE_SUFFIXES)}: {image}"
        )
    if image.exists():
        raise MediaError(f"refusing to overwrite an existing file: {image}")
    if not image.parent.is_dir():
        raise MediaError(f"no such folder: {image.parent}")
    return image


def plan_media(
    payload: Payload,
    *,
    target: dict[str, object],
    splitter: str = "",
) -> dict[str, object]:
    """Decide whether a destination can hold this payload, before touching it.

    Raises :class:`MediaError` for anything that makes the media unusable.
    Returns a plan with ``blockers`` for conditions the user can fix (a missing
    splitter, a destination that is too small).
    """
    splitter = splitter or find_splitter()
    blockers: list[str] = []
    warnings: list[str] = []
    steps: list[str] = []
    capacity = 0

    kind = str(target.get("kind", ""))
    if kind not in ("image", "disk"):
        raise MediaError(f"unknown target kind: {kind!r}")

    if payload.needs_split and not splitter:
        blockers.append(
            "sources/install.wim is larger than FAT32's 4 GiB file limit and "
            "wimlib-imagex is not installed. Install it with `brew install wimlib`."
        )
    if payload.oversized:
        blockers.append(
            "these files are larger than FAT32 allows and cannot be split: "
            + ", ".join(payload.oversized)
        )

    if kind == "image":
        image = normalise_image(str(target.get("path", "")))
        capacity = image_size_for(payload)
        if target.get("size"):
            capacity = int(target["size"])
            if capacity < payload.total_bytes + 32 * MIB:
                blockers.append("the image is smaller than the Windows media")
        steps.append(f"hdiutil create -size {capacity // MIB}m -type SPARSE {image}")
        steps.append("diskutil eraseDisk FAT32 <label> MBRFormat <image device>")
        label = str(target.get("label") or "WINDOWS")
    elif kind == "disk":
        device = str(target.get("device", ""))
        if not device.startswith("/dev/disk"):
            raise MediaError(f"not a disk device: {device!r}")
        allowed = {
            str(row["device"]): row
            for row in list_targets()["targets"]
        }
        if device not in allowed:
            raise MediaError(
                f"{device} is not an external USB disk; refusing to erase it"
            )
        info = disk_info(device)
        if info.get("Internal") is True or info.get("VirtualOrPhysical") == "Virtual":
            raise MediaError(f"{device} is internal or virtual; refusing to erase it")
        capacity = int(info.get("TotalSize") or info.get("Size") or 0)
        if capacity < payload.total_bytes + 32 * MIB:
            blockers.append(
                f"{device} is {capacity // GIB} GiB, too small for the media"
            )
        steps.append(f"diskutil eraseDisk FAT32 <label> MBRFormat {device}")
        label = str(target.get("label") or "WINDOWS")
    else:  # pragma: no cover - guarded above
        raise MediaError("unreachable")

    steps.append(
        f"copy {payload.file_count - (1 if payload.needs_split else 0)} files "
        f"({payload.total_bytes} bytes)"
    )
    if payload.needs_split:
        steps.append(
            f"wimlib-imagex split sources/install.wim sources/install.swm 3800"
        )
    steps.append("read every copied file back and compare it with the source")

    if capacity and not blockers:
        slack = capacity - payload.total_bytes
        if slack < 128 * MIB:
            warnings.append(
                f"only {slack // MIB} MiB of slack on the destination; FAT "
                "overhead may not fit"
            )

    return {
        "target": {"kind": kind, "label": label, **{k: v for k, v in target.items() if k not in ("label",)}},
        "capacity": capacity,
        "required_bytes": payload.total_bytes,
        "split": payload.needs_split,
        "splitter": splitter,
        "blockers": blockers,
        "warnings": warnings,
        "steps": steps,
        "payload": payload.summary(),
    }


# ---------------------------------------------------------------------------
# Writing.
# ---------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _volume_for(device: str, *, timeout: float = 30.0) -> str:
    """Wait for the formatted partition to mount and return its mount point."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            listing = _plist([DISKUTIL, "list", "-plist", device])
        except MediaError:
            listing = {}
        for row in listing.get("AllDisksAndPartitions", []):
            for part in row.get("Partitions", []):
                mount = part.get("MountPoint")
                if mount:
                    return str(mount)
        time.sleep(0.25)
    raise MediaError(f"{device} did not mount after formatting")


def _copy_payload(
    volume: Path,
    payload: Payload,
    *,
    splitter: str,
    log,
) -> dict[str, object]:
    skipped = payload.install_image if payload.needs_split else ""
    copied_bytes = 0
    copied_files = 0
    split_parts: list[str] = []

    if payload.needs_split:
        sources = volume / Path(payload.install_image).parent
        sources.mkdir(parents=True, exist_ok=True)
        prefix = Path(payload.install_image).name.split(".")[0]  # install
        log(f"splitting {payload.install_image} into {prefix}.swm parts (3800 MiB)")
        target_swm = sources / f"{prefix}.swm"
        _checked(
            [splitter, "split", str(Path(payload.root) / payload.install_image), str(target_swm), "3800"],
            timeout=3600.0,
        )
        split_parts = sorted(
            str(path.relative_to(volume))
            for path in sources.glob(f"{prefix}*.swm")
        )
        if not split_parts:
            raise MediaError("wimlib-imagex reported success but wrote no .swm parts")
        for part in split_parts:
            copied_bytes += (volume / part).stat().st_size
        copied_files += len(split_parts)
        log(f"wrote {len(split_parts)} split part(s): {', '.join(split_parts)}")

    for index, entry in enumerate(payload.files, start=1):
        if entry.relative == skipped:
            continue
        source = Path(payload.root) / entry.relative
        destination = volume / entry.relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and not destination.is_dir():
            raise MediaError(f"destination already has {entry.relative}")
        shutil.copyfile(source, destination)
        copied_bytes += entry.size
        copied_files += 1
        if entry.size >= 32 * MIB or index % 250 == 0:
            log(f"copied {entry.relative} ({entry.size // MIB} MiB)")

    return {
        "files": copied_files,
        "bytes": copied_bytes,
        "split": split_parts,
    }


def clean_metadata(volume: str | Path, *, log=print) -> int:
    """Remove the macOS metadata the FAT driver writes into the payload.

    Every file created on a FAT volume by macOS gets an AppleDouble ``._name``
    companion for its extended attributes, and the mounted volume gets
    ``.fseventsd``.  Windows Setup reads the media as-is, so they are deleted
    after the copy and before the read-back check.
    """
    base = Path(volume)
    removed = 0
    for name in (".fseventsd", ".Spotlight-V100", ".Trashes", ".TemporaryItems"):
        target = base / name
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            removed += 1
    for dirpath, dirnames, filenames in os.walk(base):
        for name in list(dirnames):
            if name in (".fseventsd", ".Spotlight-V100", ".Trashes"):
                shutil.rmtree(Path(dirpath) / name, ignore_errors=True)
                dirnames.remove(name)
                removed += 1
        for name in filenames:
            if name.startswith("._") or name in (".DS_Store", ".hidden"):
                try:
                    (Path(dirpath) / name).unlink()
                    removed += 1
                except OSError:
                    pass
    if removed:
        log(f"removed {removed} macOS metadata item(s) from the media")
    return removed


def verify_media(
    volume: str | Path,
    payload: Payload,
    *,
    hashed_limit: int = 32 * MIB,
    log=print,
) -> dict[str, object]:
    """Read the media back: presence, sizes, and hashes for small files.

    A whole-ISO hash of a multi-gigabyte install image would double the write
    time for no extra safety; files up to ``hashed_limit`` are byte-compared and
    the rest are checked by size, which is reported honestly.
    """
    base = Path(volume)
    missing: list[str] = []
    mismatched: list[str] = []
    hashed = 0
    size_only = 0
    checked = 0

    expected: list[tuple[str, int]] = []
    for entry in payload.files:
        if payload.needs_split and entry.relative == payload.install_image:
            continue
        expected.append((entry.relative, entry.size))

    for relative, size in expected:
        target = base / relative
        if not target.is_file():
            missing.append(relative)
            continue
        checked += 1
        if target.stat().st_size != size:
            mismatched.append(relative)
            continue
        if size <= hashed_limit:
            source = Path(payload.root) / relative
            if source.is_file() and _sha256(source) != _sha256(target):
                mismatched.append(relative)
                continue
            hashed += 1
        else:
            size_only += 1

    if payload.needs_split:
        # The parts come from wimlib, not from the ISO, so there is no source
        # byte count to compare against.  Prove the set Setup needs is there:
        # install.swm first, every part non-empty, and no source install.wim
        # left behind to shadow it.
        parts = sorted((base / "sources").glob("install*.swm")) if (base / "sources").is_dir() else []
        if not parts:
            missing.append("sources/install.swm")
        else:
            for part in parts:
                checked += 1
                size_only += 1
                if part.stat().st_size == 0:
                    mismatched.append(str(part.relative_to(base)))
            if parts[0].name != "install.swm":
                missing.append("sources/install.swm")
        if (base / payload.install_image).exists():
            mismatched.append(payload.install_image + " (should have been split, not copied)")

    for required in (REQUIRED_BOOT, REQUIRED_BOOT_WIM):
        if not (base / required).is_file() and required not in missing:
            missing.append(required)

    ok = not missing and not mismatched
    log(
        f"verified {checked} file(s): {hashed} hashed, {size_only} by size; "
        f"{len(missing)} missing, {len(mismatched)} mismatched"
    )
    return {
        "ok": ok,
        "checked": checked,
        "hashed": hashed,
        "size_only": size_only,
        "missing": missing,
        "mismatched": mismatched,
    }


def _create_image(path: Path, size: int) -> str:
    _checked(
        [
            HDIUTIL,
            "create",
            "-size",
            f"{max(size, 4 * GIB) // MIB}m",
            "-type",
            "SPARSE",
            "-layout",
            "NONE",
            str(path),
        ],
        timeout=600.0,
    )
    info = _plist([HDIUTIL, "attach", "-readwrite", "-nomount", "-plist", str(path)], timeout=120.0)
    for entity in info.get("system-entities", []):
        device = entity.get("dev-entry")
        if device:
            return str(device)
    raise MediaError(f"{path.name} attached but no device appeared")


def plan_iso(iso: str | Path, *, target: dict[str, object]) -> dict[str, object]:
    """Inspect an ISO and plan against a destination, without writing."""
    mount, device = mount_iso(iso)
    try:
        payload = scan_payload(mount)
        plan = plan_media(payload, target=target)
        plan["iso"] = str(Path(iso).expanduser())
        return plan
    finally:
        detach(device)


def create_media(
    iso: str | Path,
    *,
    target: dict[str, object],
    confirm: bool = False,
    splitter: str = "",
    log=print,
) -> dict[str, object]:
    """Mount the ISO, partition, format, copy and verify.

    The ISO stays mounted for the whole operation: scanning it in one call and
    copying from it in another let the mount disappear in between.
    """
    mount, iso_device = mount_iso(iso)
    try:
        payload = scan_payload(mount)
        return _write_media(
            payload, target=target, confirm=confirm, splitter=splitter, log=log
        )
    finally:
        detach(iso_device)


def _write_media(
    payload: Payload,
    *,
    target: dict[str, object],
    confirm: bool = False,
    splitter: str = "",
    log=print,
) -> dict[str, object]:
    """Partition, format, copy and verify.  Refuses before the first write."""
    started = time.monotonic()
    plan = plan_media(payload, target=target, splitter=splitter)
    if plan["blockers"]:
        raise MediaError("; ".join(str(item) for item in plan["blockers"]))

    kind = str(target.get("kind"))
    label = str(plan["target"].get("label") or "WINDOWS")[:11]
    splitter = str(plan["splitter"])
    device = ""
    image: Path | None = None
    owns_image = False
    finished = False

    if kind == "image":
        image = normalise_image(str(target.get("path", "")))
    elif kind == "disk":
        device = str(target.get("device", ""))
        if not confirm:
            raise MediaError(
                f"refusing to erase {device} without confirm=True"
            )
        log(f"erasing {device} as one FAT32 partition ({label})")
    else:  # pragma: no cover
        raise MediaError("unreachable")

    try:
        if image is not None:
            size = max(int(plan["capacity"]), MIN_IMAGE_BYTES)
            log(f"creating {image} ({size // MIB} MiB)")
            device = _create_image(image, int(plan["capacity"]))
            owns_image = True
        if not device:
            raise MediaError("no destination device")

        _checked(
            [DISKUTIL, "eraseDisk", "FAT32", label, "MBRFormat", device],
            timeout=900.0,
        )
        volume = _volume_for(device)
        log(f"formatted {device} as FAT32, mounted at {volume}")

        usage = shutil.disk_usage(volume)
        if usage.free < payload.total_bytes + 8 * MIB:
            raise MediaError(
                f"only {usage.free // MIB} MiB free on the formatted volume"
            )

        copied = _copy_payload(Path(volume), payload, splitter=splitter, log=log)
        removed = clean_metadata(volume, log=log)
        verified = verify_media(volume, payload, log=log)
        if not verified["ok"]:
            raise MediaError(
                "verification failed: "
                + ", ".join(verified["missing"] + verified["mismatched"])[:400]
            )
        finished = True
        return {
            "ok": True,
            "kind": kind,
            "device": device,
            "volume": volume,
            "image": str(image) if image else "",
            "label": label,
            "seconds": round(time.monotonic() - started, 1),
            "copied": copied,
            "metadata_removed": removed,
            "verified": verified,
        }
    finally:
        if owns_image and device:
            detach(device)
        if image is not None and not finished:
            #
            # A partial image is ours and only this run made it.  Leaving it
            # behind would also block the retry, because create never
            # overwrites an existing file.
            #
            try:
                if image.exists():
                    image.unlink()
                    log(f"removed the incomplete image {image}")
            except OSError as exc:
                log(f"could not remove the incomplete image: {exc}")


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def _target_from_args(args: argparse.Namespace) -> dict[str, object]:
    if getattr(args, "device", ""):
        return {"kind": "disk", "device": args.device, "label": args.label}
    return {"kind": "image", "path": args.image, "label": args.label}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SiliconCamp installer media")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("targets", help="list external USB disks")

    inspect = sub.add_parser("inspect", help="validate a Windows Arm64 ISO")
    inspect.add_argument("--iso", required=True)

    plan = sub.add_parser("plan", help="show what would be written")
    plan.add_argument("--iso", required=True)
    plan.add_argument("--image", default="")
    plan.add_argument("--device", default="")
    plan.add_argument("--label", default="WINDOWS")

    create = sub.add_parser("create", help="write the installer")
    create.add_argument("--iso", required=True)
    create.add_argument("--image", default="")
    create.add_argument("--device", default="")
    create.add_argument("--label", default="WINDOWS")
    create.add_argument("--yes", action="store_true", help="confirm a device erase")

    args = parser.parse_args(argv)

    try:
        if args.command == "targets":
            print(json.dumps(list_targets(), indent=2))
            return 0
        if args.command == "inspect":
            payload = inspect_iso(args.iso)
            print(json.dumps(payload.summary(), indent=2))
            return 0
        if not args.image and not args.device:
            parser.error("choose --image or --device")

        target = _target_from_args(args)
        if args.command == "plan":
            print(json.dumps(plan_iso(args.iso, target=target), indent=2))
            return 0

        def log(message: str) -> None:
            print(message, flush=True)

        report = create_media(
            args.iso,
            target=target,
            confirm=bool(args.yes),
            log=log,
        )
        print(json.dumps(report, indent=2))
        return 0
    except MediaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
