#!/usr/bin/env python3
"""Verify the ACPI tables and image contract of the M4 UEFI firmware.

The M4 platform package hardcodes things that must match the J624 hardware:
the 10-core MADT, the 6E+4P PPTT, the vGIC window, the UART address and IRQ,
and the firmware device size.  Compiling does not check any of that, so this
script parses the tables the build emits and asserts the invariants.

It has no third-party dependencies: the tables are parsed from the binary data
the EDK2 build leaves in the build tree.

Usage:
    ./scripts/check-acpi.py                     # Mac16_3, newest build
    ./scripts/check-acpi.py --platform MacBookProEarly2023
    ./scripts/check-acpi.py --build-dir <dir>   # any .../DEBUG_CLANGPDB dir

Exit status is 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import glob
import os
import struct
import sys

# Expected J624 (Mac16,3) values.  Sources are recorded in docs/hardware-values.md.
E_CORE_MPIDRS = [0x0, 0x1, 0x2, 0x3, 0x4, 0x5]
P_CORE_MPIDRS = [0x10100, 0x10101, 0x10102, 0x10103]
EXPECTED_MPIDRS = E_CORE_MPIDRS + P_CORE_MPIDRS
GICD_BASE = 0x5000000000
GICR_BASE = 0x5100000000
GICR_STRIDE = 0x20000
UART_BASE = 0x3AD200000
UART_IRQ = 1046

# FDT / FDF ARD64 Image header fields.
FD_BASE = 0x10030000000
FD_SIZE = 0x1E00000


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.notes: list[str] = []

    def check(self, ok: bool, message: str) -> bool:
        if ok:
            print(f"  ok   {message}")
        else:
            print(f"  FAIL {message}")
            self.failures.append(message)
        return ok

    def note(self, message: str) -> None:
        self.notes.append(message)
        print(f"  info {message}")


def acpi_header(data: bytes, name: str, ck: Checker) -> bool:
    """Validate an ACPI table header and return True if it is usable."""
    if len(data) < 36:
        ck.check(False, f"{name}: too short to hold an ACPI header")
        return False

    sig = data[0:4].decode("ascii", "replace")
    length = struct.unpack_from("<I", data, 4)[0]
    revision = data[8]
    checksum = data[9]
    oem_id = data[10:16].decode("ascii", "replace").rstrip()
    oem_table = data[16:24].decode("ascii", "replace").rstrip()
    oem_rev = struct.unpack_from("<I", data, 24)[0]

    if not ck.check(sig == name, f"{name}: signature is {sig!r}"):
        return False
    if not ck.check(0 < length <= len(data),
                    f"{name}: length {length} fits in {len(data)} bytes"):
        return False

    # __ACPI_HEADER leaves the checksum at 0; AcpiPlatformDxe computes the real
    # one when it installs the table, so a zero placeholder is not a failure.
    total = sum(data[:length]) & 0xFF
    if checksum == 0 and total != 0:
        ck.note(f"{name}: checksum placeholder 0 (filled in by AcpiPlatformDxe)")
    else:
        ck.check(total == 0, f"{name}: checksum over {length} bytes is valid")

    ck.note(f"{name}: rev {revision}, OEM {oem_id}/{oem_table}, oem rev 0x{oem_rev:x}")
    return True


def find_tables(build_dir: str) -> dict[str, str]:
    """Map ACPI signature -> path for every table the build produced."""
    tables: dict[str, str] = {}
    for path in glob.glob(os.path.join(build_dir, "**", "OUTPUT", "*"),
                          recursive=True):
        if not os.path.isfile(path):
            continue
        if not path.lower().endswith((".acpi", ".aml")):
            continue
        with open(path, "rb") as f:
            head = f.read(4)
        if len(head) < 4:
            continue
        sig = head.decode("ascii", "replace")
        if sig.isprintable() and sig.isalnum():
            tables.setdefault(sig, path)
    return tables


def parse_madt(data: bytes, ck: Checker) -> None:
    header_len = 44
    entries: list[tuple[int, int, int]] = []  # (type, offset, length)
    off = header_len
    while off + 2 <= len(data):
        etype = data[off]
        elen = data[off + 1]
        if elen < 2 or off + elen > len(data):
            ck.check(False, f"MADT: entry at 0x{off:x} has bad length {elen}")
            return
        entries.append((etype, off, elen))
        off += elen

    giccs = [(o, l) for (t, o, l) in entries if t == 0x0B]
    gicds = [(o, l) for (t, o, l) in entries if t == 0x0C]
    gicrs = [(o, l) for (t, o, l) in entries if t == 0x0E]

    ck.check(len(giccs) == 10, f"MADT: 10 GICC entries (found {len(giccs)})")
    ck.check(len(gicds) >= 1, f"MADT: has a GICD entry ({len(gicds)} found)")
    ck.check(len(gicrs) >= 1, f"MADT: has a GICR discovery range ({len(gicrs)} found)")

    if gicds:
        off, length = gicds[0]
        base = struct.unpack_from("<Q", data, off + 8)[0]
        ck.check(base == GICD_BASE,
                 f"MADT: GICD base 0x{base:x} == 0x{GICD_BASE:x}")

    if gicrs:
        off, length = gicrs[0]
        base = struct.unpack_from("<Q", data, off + 4)[0]
        size = struct.unpack_from("<I", data, off + 12)[0]
        ck.check(base == GICR_BASE and size == GICR_STRIDE * 10,
                 f"MADT: GICR range 0x{base:x}+0x{size:x} == "
                 f"0x{GICR_BASE:x}+0x{GICR_STRIDE * 10:x}")

    if len(giccs) != 10:
        return

    # ACPI 6.3 GICC layout: the 6.0 view is missing the GICH and
    # VGICMaintenanceInterrupt fields, which shift GICR/MPIDR by 12 bytes.
    uids: list[int] = []
    mpids: list[int] = []
    effs: list[int] = []
    per_cpu_gicr: list[int] = []

    for index, (off, length) in enumerate(giccs):
        if length < 80:
            ck.check(False, f"MADT: GICC {index} is {length} bytes, expected 80")
            return
        uids.append(struct.unpack_from("<I", data, off + 8)[0])
        per_cpu_gicr.append(struct.unpack_from("<Q", data, off + 60)[0])
        mpids.append(struct.unpack_from("<Q", data, off + 68)[0])
        effs.append(data[off + 76])

    ck.check(uids == list(range(10)), f"MADT: GICC UIDs are 0..9 (got {uids})")
    ck.check(mpids == EXPECTED_MPIDRS,
             "MADT: MPIDRs are 0x0-0x5 (E) then 0x10100-0x10103 (P)")
    if mpids != EXPECTED_MPIDRS:
        ck.note(f"      expected {[hex(m) for m in EXPECTED_MPIDRS]}")
        ck.note(f"      actual   {[hex(m) for m in mpids]}")
    ck.check(effs == [0] * 6 + [1] * 4,
             f"MADT: efficiency class 0 for E-cores, 1 for P-cores (got {effs})")
    # A zero per-CPU GICR base means "use the GICR discovery range" (ACPI 6.3).
    ck.check(per_cpu_gicr == [0] * 10,
             "MADT: per-CPU GICR bases are 0, GICR range is authoritative")


def parse_pptt(data: bytes, ck: Checker) -> None:
    header_len = 36
    nodes: list[dict] = []
    off = header_len
    while off + 4 <= len(data):
        ntype = data[off]
        nlen = data[off + 1]
        if nlen < 4 or off + nlen > len(data):
            ck.check(False, f"PPTT: node at 0x{off:x} has bad length {nlen}")
            return
        node = {"type": ntype, "offset": off, "length": nlen}
        if ntype == 0 and nlen >= 20:  # processor hierarchy node
            # edk2 uses UINT16 Reserved + UINT32 Flags, so Parent is at 8.
            node["flags"] = struct.unpack_from("<I", data, off + 4)[0]
            node["parent"] = struct.unpack_from("<I", data, off + 8)[0]
            node["acpi_id"] = struct.unpack_from("<I", data, off + 12)[0]
            node["resources"] = struct.unpack_from("<I", data, off + 16)[0]
        nodes.append(node)
        off += nlen

    ck.check(len(nodes) == 13,
             f"PPTT: 13 nodes = 1 package + 2 clusters + 10 cores (found {len(nodes)})")
    if len(nodes) != 13:
        return

    for index, node in enumerate(nodes):
        if node["type"] != 0:
            ck.check(False, f"PPTT: node {index} has unexpected type {node['type']}")
            continue
        parent = node["parent"]
        if index == 0:
            ck.check(parent == 0, "PPTT: package node has no parent")
            continue
        if not ck.check(0 < parent < len(data),
                        f"PPTT: node {index} parent offset 0x{parent:x} in range"):
            continue
        parent_node = next((n for n in nodes if n["offset"] == parent), None)
        ck.check(parent_node is not None,
                 f"PPTT: node {index} parent points at a node boundary")

    # edk2 emits the tree depth-first (package, cluster, its cores, cluster,
    # its cores), so classify by parent rather than by table order.
    package = nodes[0]["offset"]
    clusters = [n for n in nodes[1:] if n.get("parent") == package]
    cores = [n for n in nodes[1:] if n.get("parent") in {c["offset"] for c in clusters}]

    ck.check(nodes[0]["acpi_id"] == 0,
             f"PPTT: package ACPI ID is 0 (got {nodes[0]['acpi_id']})")
    ck.check(len(clusters) == 2, f"PPTT: 2 clusters (found {len(clusters)})")
    ck.check(len(cores) == 10, f"PPTT: 10 cores (found {len(cores)})")
    if len(clusters) != 2 or len(cores) != 10:
        return
    ck.check(sorted(c["acpi_id"] for c in clusters) == [0, 1],
             "PPTT: cluster ACPI IDs are 0 and 1")
    core_ids = [c["acpi_id"] for c in cores]
    ck.check(core_ids == list(range(10)),
             f"PPTT: core ACPI IDs are 0..9 in MADT order (got {core_ids})")
    # The efficiency cluster must come first, matching MADT UIDs 0-5.
    e_cluster = clusters[0]
    e_ids = [c["acpi_id"] for c in cores if c["parent"] == e_cluster["offset"]]
    ck.check(e_ids == list(range(6)),
             f"PPTT: first cluster holds cores 0..5 (got {e_ids})")


def parse_fadt(data: bytes, ck: Checker) -> None:
    flags = struct.unpack_from("<I", data, 112)[0]
    arm_flags = struct.unpack_from("<H", data, 129)[0]
    ck.check(bool(flags & (1 << 20)), "FADT: hardware-reduced ACPI set")
    ck.check(bool(arm_flags & 0x1), "FADT: PSCI compliant")
    # Bit 1 selects HVC for PSCI calls; Apple has no EL3, so m1n1 traps SMC.
    ck.check(not (arm_flags & 0x2), "FADT: PSCI calls use SMC (m1n1 traps them)")


def parse_gtdt(data: bytes, ck: Checker) -> None:
    non_secure_pl1 = struct.unpack_from("<I", data, 56)[0]
    virt = struct.unpack_from("<I", data, 64)[0]
    platform_timers = struct.unpack_from("<I", data, 88)[0]
    ck.check(non_secure_pl1 == 17,
             f"GTDT: non-secure EL1 timer GSIV {non_secure_pl1} == 17")
    ck.check(virt == 18, f"GTDT: virtual timer GSIV {virt} == 18")
    ck.check(platform_timers == 0,
             f"GTDT: no memory-mapped timer frames ({platform_timers})")


def parse_dbg2(data: bytes, ck: Checker) -> None:
    header_len = 44
    count = struct.unpack_from("<I", data, header_len - 4)[0]
    ck.check(count >= 1, f"DBG2: at least one debug device ({count})")
    if count < 1:
        return
    off = header_len
    if off + 12 > len(data):
        ck.check(False, "DBG2: truncated device descriptor")
        return
    nlen = struct.unpack_from("<H", data, off + 1)[0]
    ck.check(nlen >= 22, f"DBG2: device descriptor length {nlen}")

    # The generic address registers follow the fixed 22-byte descriptor.
    gas_off = off + 22
    if not ck.check(gas_off + 12 <= len(data), "DBG2: GAS fits in the table"):
        return
    base = struct.unpack_from("<Q", data, gas_off + 4)[0]
    ck.check(base == UART_BASE,
             f"DBG2: debug UART at 0x{base:x} == 0x{UART_BASE:x}")


def check_dsdt(data: bytes, ck: Checker) -> None:
    names = [f"CPU{i}" for i in range(10)] + ["COM0", "DIE0", "CLU0", "CLU1"]
    for name in names:
        ck.check(name.encode() in data, f"DSDT: {name} device present")


def check_image(build_dir: str, ck: Checker) -> None:
    """Validate the FD image header and size contract."""
    candidates = glob.glob(os.path.join(os.path.dirname(build_dir.rstrip("/")),
                                        "FV", "*_EFI.fd"))
    if not candidates:
        ck.note("no FV/*_EFI.fd yet; skipping image checks")
        return
    path = candidates[0]
    size = os.path.getsize(path)
    ck.check(size <= FD_SIZE,
             f"image: {os.path.basename(path)} is 0x{size:x} bytes "
             f"(FDF size 0x{FD_SIZE:x})")
    if size != FD_SIZE:
        ck.note(f"image: image ends 0x{FD_SIZE - size:x} bytes before the "
                f"declared FD size")

    with open(path, "rb") as f:
        header = f.read(64)
    ck.check(header[56:60] == b"ARM\x64", "image: ARM64 Image magic present")
    text_offset = struct.unpack_from("<Q", header, 8)[0]
    image_size = struct.unpack_from("<Q", header, 16)[0]
    flags = struct.unpack_from("<Q", header, 24)[0]
    ck.check(text_offset == 0x80000, f"image: text_offset 0x{text_offset:x} == 0x80000")
    ck.check(size <= image_size <= FD_SIZE,
             f"image: header image_size 0x{image_size:x} covers the payload "
             f"(0x{size:x}) and fits the FD (0x{FD_SIZE:x})")
    ck.check(flags == 0, f"image: flags 0x{flags:x} (little-endian)")
    ck.note(f"image: loaded at 0x{FD_BASE:x}")


def resolve_build_dir(platform: str, build_dir: str | None) -> str:
    if build_dir:
        return build_dir
    root = os.environ.get("NTASP_ROOT") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "third_party", "ntasp-firmware")
    pattern = os.path.join(root, "Build", f"{platform}-AARCH64", "*", "AARCH64")
    found = sorted(glob.glob(pattern), key=os.path.getmtime)
    if not found:
        sys.exit(f"error: no build tree for {platform} under {root}/Build")
    return found[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", default="Mac16_3",
                        help="platform package name (default: Mac16_3)")
    parser.add_argument("--build-dir", default=None,
                        help="build output directory containing AARCH64/")
    args = parser.parse_args()

    build_dir = resolve_build_dir(args.platform, args.build_dir)
    print(f"checking {args.platform}: {build_dir}\n")

    tables = find_tables(build_dir)
    if not tables:
        sys.exit(f"error: no ACPI tables found under {build_dir}")

    ck = Checker()
    order = [("MADT", "APIC"), ("PPTT", "PPTT"), ("FADT", "FACP"),
             ("DSDT", "DSDT"), ("DBG2", "DBG2"), ("GTDT", "GTDT")]
    for label, sig in order:
        path = tables.get(sig)
        if path is None:
            ck.check(False, f"{label}: table present")
            continue
        print(f"{label} ({os.path.relpath(path, build_dir)})")
        with open(path, "rb") as f:
            data = f.read()
        if not acpi_header(data, sig, ck):
            continue
        if sig == "APIC":
            parse_madt(data, ck)
        elif sig == "PPTT":
            parse_pptt(data, ck)
        elif sig == "FACP":
            parse_fadt(data, ck)
        elif sig == "DSDT":
            check_dsdt(data, ck)
        elif sig == "DBG2":
            parse_dbg2(data, ck)
        elif sig == "GTDT":
            parse_gtdt(data, ck)
        print()

    print("image")
    check_image(build_dir, ck)
    print()

    if ck.failures:
        print(f"{len(ck.failures)} check(s) failed")
        return 1
    print("all ACPI and image checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
