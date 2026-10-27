#!/usr/bin/env python3
"""Verify the ACPI tables and image contract of a Mac firmware build.

A platform package hardcodes things that must match real hardware: the core
count and MPIDRs, the efficiency classes, the vGIC window, the UART address and
IRQ, and the firmware device size.  Compiling does not check any of that, so this
script parses the tables the build emits and asserts the platform's contract.

Expectations live in the PLATFORMS table below, one entry per platform package
that has been validated.  There is deliberately no "close enough" path: an
unvalidated platform is an error, not a pass, because a checker that guesses is
worse than no checker.  `--print-topology` turns a known-good build into a
ready-to-paste entry when a new SoC is brought up.

It has no third-party dependencies: the tables are parsed straight from the
binary data the EDK2 build leaves in the build tree.

Usage:
    ./scripts/check-acpi.py                     # Mac16_3, newest build
    ./scripts/check-acpi.py --platform Mac16_3
    ./scripts/check-acpi.py --build-dir <dir>   # <Build>/<plat>-AARCH64/<tag>/AARCH64
    ./scripts/check-acpi.py --print-topology    # dump a Platform entry
    ./scripts/check-acpi.py --platform X --unvalidated-ok   # reference build

Exit status is 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import glob
import os
import struct
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class Platform:
    """Everything the checker expects of one platform's firmware output."""

    name: str
    #: MPIDR of every core in ACPI UID order, as the MADT lists them.
    cores: tuple[int, ...]
    #: Efficiency class per core, in the same order.
    efficiency: tuple[int, ...]
    gicd_base: int
    gicr_base: int
    gicr_stride: int
    uart_base: int
    uart_irq: int
    fd_size: int
    #: PCIe root complex contract.  All zero when the platform has no MCFG or
    #: PCI0; otherwise MCFG and the DSDT _CRS are checked against these.
    pcie_ecam: int = 0
    pcie_ecam_size: int = 0
    pcie_bus_min: int = 0
    pcie_bus_max: int = 0
    pcie_mmio32_bus: int = 0
    pcie_mmio32_host: int = 0
    pcie_mmio32_size: int = 0
    pcie_prefetch: int = 0
    pcie_prefetch_size: int = 0


# Sources for every value are recorded in docs/hardware-values.md.
PLATFORMS: dict[str, Platform] = {
    "Mac16_3": Platform(
        name="Mac16,3 J624 (T8132, M4 iMac)",
        cores=(
            0x0, 0x1, 0x2, 0x3, 0x4, 0x5,
            0x10100, 0x10101, 0x10102, 0x10103,
        ),
        efficiency=(0, 0, 0, 0, 0, 0, 1, 1, 1, 1),
        gicd_base=0x5000000000,
        gicr_base=0x5100000000,
        gicr_stride=0x20000,
        uart_base=0x3AD200000,
        uart_irq=1046,
        fd_size=0x1E00000,
        # Captured from apcie@B0000000; see docs/pcie.md for the raw cells.
        pcie_ecam=0x1CB0000000,
        pcie_ecam_size=0x10000000,
        pcie_bus_min=0,
        pcie_bus_max=8,
        pcie_mmio32_bus=0x80000000,
        pcie_mmio32_host=0xB80000000,
        pcie_mmio32_size=0x40000000,
        pcie_prefetch=0xBC0000000,
        pcie_prefetch_size=0x20000000,
    ),
}


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def check(self, ok: bool, message: str) -> bool:
        if ok:
            print(f"  ok   {message}")
        else:
            print(f"  FAIL {message}")
            self.failures.append(message)
        return ok

    @staticmethod
    def note(message: str) -> None:
        print(f"  info {message}")


# --------------------------------------------------------------------------
# Helpers shared by the assertion pass and the topology dump.
# --------------------------------------------------------------------------

def cluster_id(mpidr: int) -> int:
    """Aff1:Aff0-style cluster identity of an MPIDR, ignoring the core bits."""
    return (mpidr >> 8) & 0xFFFF


def cluster_runs(cores: tuple[int, ...] | list[int]) -> list[list[int]]:
    """Split the core list into contiguous runs that share a cluster.

    Apple numbers E-core MPIDRs 0..5 (cluster 0) and P-core MPIDRs 0x10100..
    (cluster 0x101), and the MADT lists E cores first, so a cluster is always a
    contiguous run in ACPI UID order.
    """
    runs: list[list[int]] = []
    for index, mpidr in enumerate(cores):
        cid = cluster_id(mpidr)
        if runs and cluster_id(cores[runs[-1][0]]) == cid:
            runs[-1].append(index)
        else:
            runs.append([index])
    return runs


def madt_entries(data: bytes, ck: Checker | None = None):
    """Yield (entry_type, offset, length) for every MADT entry."""
    entries = []
    off = 44
    while off + 2 <= len(data):
        etype, elen = data[off], data[off + 1]
        if elen < 2 or off + elen > len(data):
            if ck is not None:
                ck.check(False, f"MADT: entry at 0x{off:x} has bad length {elen}")
            return entries
        entries.append((etype, off, elen))
        off += elen
    return entries


def read_madt(data: bytes, ck: Checker | None = None) -> dict:
    """Extract the fields the checks need from a MADT, without asserting."""
    entries = madt_entries(data, ck)
    info: dict = {
        "gicds": [off for (t, off, _) in entries if t == 0x0C],
        "gicrs": [(off, length) for (t, off, length) in entries if t == 0x0E],
        "uids": [],
        "mpidrs": [],
        "efficiency": [],
        "per_cpu_gicr": [],
        "gicc_lengths": [],
    }
    for etype, off, length in entries:
        if etype != 0x0B:
            continue
        info["gicc_lengths"].append(length)
        if length < 80:
            continue
        info["uids"].append(struct.unpack_from("<I", data, off + 8)[0])
        info["per_cpu_gicr"].append(struct.unpack_from("<Q", data, off + 60)[0])
        info["mpidrs"].append(struct.unpack_from("<Q", data, off + 68)[0])
        info["efficiency"].append(data[off + 76])
    if info["gicds"]:
        info["gicd_base"] = struct.unpack_from("<Q", data, info["gicds"][0] + 8)[0]
    if info["gicrs"]:
        off = info["gicrs"][0][0]
        info["gicr_base"] = struct.unpack_from("<Q", data, off + 4)[0]
        info["gicr_size"] = struct.unpack_from("<I", data, off + 12)[0]
    return info


def read_pptt(data: bytes, ck: Checker | None = None) -> list[dict]:
    """Extract processor-hierarchy nodes from a PPTT."""
    nodes: list[dict] = []
    off = 36
    while off + 4 <= len(data):
        ntype, nlen = data[off], data[off + 1]
        if nlen < 4 or off + nlen > len(data):
            if ck is not None:
                ck.check(False, f"PPTT: node at 0x{off:x} has bad length {nlen}")
            return nodes
        node = {"type": ntype, "offset": off, "length": nlen}
        if ntype == 0 and nlen >= 20:
            # edk2 uses UINT16 Reserved + UINT32 Flags, so Parent is at 8.
            node["flags"] = struct.unpack_from("<I", data, off + 4)[0]
            node["parent"] = struct.unpack_from("<I", data, off + 8)[0]
            node["acpi_id"] = struct.unpack_from("<I", data, off + 12)[0]
            node["resources"] = struct.unpack_from("<I", data, off + 16)[0]
        nodes.append(node)
        off += nlen
    return nodes


# --------------------------------------------------------------------------
# Minimal AML parsing for the DSDT checks.
#
# The DSDT is the one table whose contents matter to Windows in a way that a
# string search cannot prove: a CPU attached to the wrong cluster, or a UART
# _CRS with the wrong address, still contains the right device names.  These
# helpers read exactly the AML objects the platform contract uses.
# --------------------------------------------------------------------------

def aml_pkg_length(data: bytes, off: int) -> tuple[int, int]:
    """Decode an AML PkgLength at *off*; return (length, bytes consumed).

    The length counts the PkgLength bytes themselves, so a package starting at
    ``off`` ends at ``off + length``.
    """
    first = data[off]
    extra = first >> 6
    if extra == 0:
        return first & 0x3F, 1
    value = first & 0x0F
    for index in range(extra):
        value |= data[off + 1 + index] << (4 + 8 * index)
    return value, 1 + extra


def aml_name(data: bytes, off: int) -> tuple[str, int]:
    """Decode a four-character AML NameString; return (name, bytes used)."""
    prefix = 0
    if data[off] == 0x5C:                       # root prefix
        prefix = 1
    elif data[off] == 0x5E:                     # parent prefix, unused here
        raise ValueError("parent-prefixed name")
    raw = data[off + prefix:off + prefix + 4]
    # AML name segments are a lead character (A-Z or _) plus three name
    # characters, which may also be digits: CPU0, CLU1, DIE0.
    if len(raw) != 4 or not (0x41 <= raw[0] <= 0x5A or raw[0] == 0x5F):
        raise ValueError("not a four-character AML name")
    if not all(0x41 <= b <= 0x5A or 0x30 <= b <= 0x39 or b == 0x5F
               for b in raw[1:]):
        raise ValueError("not a four-character AML name")
    return raw.decode("ascii"), prefix + 4


def aml_devices(data: bytes) -> list[dict]:
    """Find every DeviceOp in an AML table, nested ones included.

    Each entry carries its byte span, so a caller can prove that CPU nodes sit
    inside the intended cluster without implementing the whole AML object model.
    """
    devices: list[dict] = []
    pos = 36
    while True:
        pos = data.find(b"\x5B\x82", pos)
        if pos < 0:
            return devices
        try:
            length, length_bytes = aml_pkg_length(data, pos + 2)
            end = pos + 2 + length
            name, name_bytes = aml_name(data, pos + 2 + length_bytes)
        except (IndexError, ValueError):
            pos += 2
            continue
        if end <= pos or end > len(data):
            pos += 2
            continue
        devices.append({
            "name": name,
            "start": pos,
            "end": end,
            "name_end": pos + 2 + length_bytes + name_bytes,
        })
        pos += 2


def aml_integer(data: bytes, off: int) -> tuple[int, int]:
    """Decode an AML integer/data object; return (value, bytes consumed)."""
    op = data[off]
    if op == 0x00:
        return 0, 1
    if op == 0x01:
        return 1, 1
    if op == 0xFF:
        return 0xFFFFFFFFFFFFFFFF, 1
    if op == 0x0A:
        return data[off + 1], 2
    if op == 0x0B:
        return struct.unpack_from("<H", data, off + 1)[0], 3
    if op == 0x0C:
        return struct.unpack_from("<I", data, off + 1)[0], 5
    if op == 0x0E:
        return struct.unpack_from("<Q", data, off + 1)[0], 9
    raise ValueError(f"not an AML integer: 0x{op:02x}")


def aml_property(data: bytes, device: dict, name: str) -> int | None:
    """Return the value of ``Name(name, <integer>)`` inside a device."""
    marker = b"\x08" + name.encode("ascii")
    off = data.find(marker, device["name_end"], device["end"])
    if off < 0:
        return None
    try:
        return aml_integer(data, off + len(marker))[0]
    except (IndexError, ValueError):
        return None


def aml_string(data: bytes, device: dict, name: str) -> str | None:
    """Return the value of ``Name(name, "string")`` inside a device."""
    marker = b"\x08" + name.encode("ascii")
    off = data.find(marker, device["name_end"], device["end"])
    if off < 0 or data[off + len(marker):off + len(marker) + 1] != b"\x0D":
        return None
    start = off + len(marker) + 1
    end = data.find(b"\x00", start, device["end"])
    if end < 0:
        return None
    return data[start:end].decode("ascii", "replace")


def aml_method_return(data: bytes, device: dict, name: str) -> int | None:
    """Return the integer from ``Method(name) { Return (<integer>) }``."""
    off = device["name_end"]
    while True:
        off = data.find(b"\x14", off, device["end"])
        if off < 0:
            return None
        try:
            length, length_bytes = aml_pkg_length(data, off + 1)
            method_name, name_bytes = aml_name(data, off + 1 + length_bytes)
        except (IndexError, ValueError):
            off += 1
            continue
        if method_name != name:
            off += 1
            continue
        body_start = off + 1 + length_bytes + name_bytes + 1  # skip flags
        end = min(off + 1 + length, device["end"])
        returned = data.find(b"\xA4", body_start, end)      # ReturnOp
        if returned < 0:
            return None
        try:
            return aml_integer(data, returned + 1)[0]
        except (IndexError, ValueError):
            return None


def aml_resources(data: bytes, device: dict, name: str):
    """Parse ``Name(name, ResourceTemplate)`` into its address descriptors.

    Returns (memories, gsivs, buses).  A memory descriptor is
    (minimum, maximum, translation, length); a bus descriptor is the same
    shape.  The translation matters for PCI0: the J624 32-bit window is a
    bus address plus a host offset, and a checker that only looks at the
    minimum cannot tell the two address spaces apart.
    """
    marker = b"\x08" + name.encode("ascii")
    off = data.find(marker, device["name_end"], device["end"])
    if off < 0:
        return [], [], []
    off += len(marker)
    if data[off:off + 1] != b"\x11":            # BufferOp
        return [], [], []
    try:
        length, length_bytes = aml_pkg_length(data, off + 1)
        end = off + 1 + length
        size, size_bytes = aml_integer(data, off + 1 + length_bytes)
    except (IndexError, ValueError):
        return [], [], []
    start = off + 1 + length_bytes + size_bytes
    buf = data[start:min(end, start + size)]

    memories: list[tuple[int, int, int, int]] = []
    gsivs: list[int] = []
    buses: list[tuple[int, int, int, int]] = []
    pos = 0
    while pos + 3 <= len(buf):
        tag = buf[pos]
        if tag == 0x79:                          # EndTag
            break
        count = buf[pos + 1] | (buf[pos + 2] << 8)
        body = buf[pos + 3:pos + 3 + count]
        if tag == 0x8A and len(body) >= 43:      # QWordMemory
            _, minimum, maximum, translation, length64 = \
                struct.unpack_from("<QQQQQ", body, 3)
            memories.append((minimum, maximum, translation, length64))
        elif tag == 0x87 and len(body) >= 23:    # DWordMemory
            _, minimum, maximum, translation, length32 = \
                struct.unpack_from("<IIIII", body, 3)
            memories.append((minimum, maximum, translation, length32))
        elif tag == 0x88 and len(body) >= 13 and body[0] == 0x02:
            # WordBusNumber: address space descriptor, resource type 2.
            minimum, maximum, translation, length16 = \
                struct.unpack_from("<HHHH", body, 5)
            buses.append((minimum, maximum, translation, length16))
        elif tag == 0x89 and len(body) >= 2:     # extended interrupt
            for index in range(body[1]):
                gsivs.append(struct.unpack_from("<I", body, 2 + 4 * index)[0])
        pos += 3 + count
    return memories, gsivs, buses


def acpi_header(data: bytes, name: str, ck: Checker) -> bool:
    """Validate an ACPI table header and return True if it is usable."""
    if len(data) < 36:
        ck.check(False, f"{name}: too short to hold an ACPI header")
        return False

    sig = data[0:4].decode("ascii", "replace")
    length = struct.unpack_from("<I", data, 4)[0]
    revision = data[8]
    checksum = data[9]
    oem_id = data[10:16].decode("ascii", "replace").rstrip("\0")
    oem_table = data[16:24].decode("ascii", "replace").rstrip("\0")
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

    ck.note(f"{name}: rev {revision}, OEM {oem_id}/{oem_table}, "
            f"oem rev 0x{oem_rev:x}")
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


# --------------------------------------------------------------------------
# Per-table checks.
# --------------------------------------------------------------------------

def check_madt(data: bytes, ck: Checker, plat: Platform) -> None:
    info = read_madt(data, ck)
    cores = plat.cores
    count = len(cores)
    giccs = len(info["gicc_lengths"])

    ck.check(giccs == count,
             f"MADT: {count} GICC entries (found {len(info['gicc_lengths'])})")
    ck.check(len(info["gicds"]) >= 1,
             f"MADT: has a GICD entry ({len(info['gicds'])} found)")
    ck.check(len(info["gicrs"]) >= 1,
             f"MADT: has a GICR discovery range ({len(info['gicrs'])} found)")

    if info["gicds"]:
        base = info.get("gicd_base", 0)
        ck.check(base == plat.gicd_base,
                 f"MADT: GICD base 0x{base:x} == 0x{plat.gicd_base:x}")

    if info["gicrs"]:
        base = info.get("gicr_base", 0)
        size = info.get("gicr_size", 0)
        want = plat.gicr_stride * count
        ck.check(base == plat.gicr_base and size == want,
                 f"MADT: GICR range 0x{base:x}+0x{size:x} == "
                 f"0x{plat.gicr_base:x}+0x{want:x}")

    if giccs != count:
        ck.note(f"      skipping per-core checks: expected {count} GICCs")
        return

    # ACPI 6.3 GICC layout: the 6.0 view is missing the GICH and
    # VGICMaintenanceInterrupt fields, which shift GICR/MPIDR by 12 bytes.
    if not ck.check(all(length >= 80 for length in info["gicc_lengths"]),
                    "MADT: every GICC is at least 80 bytes (ACPI 6.3)"):
        return

    ck.check(info["uids"] == list(range(count)),
             f"MADT: GICC UIDs are 0..{count - 1} (got {info['uids']})")
    if not ck.check(info["mpidrs"] == list(cores),
                    "MADT: MPIDRs match the platform core list"):
        ck.note(f"      expected {[hex(m) for m in cores]}")
        ck.note(f"      actual   {[hex(m) for m in info['mpidrs']]}")
    ck.check(info["efficiency"] == list(plat.efficiency),
             f"MADT: efficiency class {list(plat.efficiency)} "
             f"(got {info['efficiency']})")
    # A zero per-CPU GICR base means "use the GICR discovery range" (ACPI 6.3).
    ck.check(info["per_cpu_gicr"] == [0] * count,
             "MADT: per-CPU GICR bases are 0, GICR range is authoritative")


def check_pptt(data: bytes, ck: Checker, plat: Platform) -> None:
    nodes = read_pptt(data, ck)
    cores = plat.cores
    runs = cluster_runs(cores)
    expected_nodes = 1 + len(runs) + len(cores)

    ck.check(len(nodes) == expected_nodes,
             f"PPTT: {expected_nodes} nodes = 1 package + {len(runs)} clusters "
             f"+ {len(cores)} cores (found {len(nodes)})")
    if len(nodes) != expected_nodes:
        return

    for index, node in enumerate(nodes):
        if not ck.check(node["type"] == 0,
                        f"PPTT: node {index} is a processor hierarchy node"):
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
    cluster_offsets = {c["offset"] for c in clusters}
    core_nodes = [n for n in nodes[1:] if n.get("parent") in cluster_offsets]

    ck.check(nodes[0]["acpi_id"] == 0,
             f"PPTT: package ACPI ID is 0 (got {nodes[0]['acpi_id']})")
    ck.check(len(clusters) == len(runs),
             f"PPTT: {len(runs)} clusters (found {len(clusters)})")
    ck.check(len(core_nodes) == len(cores),
             f"PPTT: {len(cores)} cores (found {len(core_nodes)})")
    if len(clusters) != len(runs) or len(core_nodes) != len(cores):
        return

    ck.check(sorted(c["acpi_id"] for c in clusters) == list(range(len(runs))),
             f"PPTT: cluster ACPI IDs are 0..{len(runs) - 1}")

    for run_index, run in enumerate(runs):
        cluster = clusters[run_index]
        members = [c["acpi_id"] for c in core_nodes
                   if c["parent"] == cluster["offset"]]
        ck.check(members == run,
                 f"PPTT: cluster {run_index} holds cores {run[0]}..{run[-1]} "
                 f"(got {members})")

    core_ids = [c["acpi_id"] for c in core_nodes]
    ck.check(core_ids == list(range(len(cores))),
             f"PPTT: core ACPI IDs are 0..{len(cores) - 1} in MADT order "
             f"(got {core_ids})")


def check_fadt(data: bytes, ck: Checker) -> None:
    # Preferred_PM_Profile is byte 45 and the ARM boot flags arrive in ACPI 5.1
    # at 129.  A table shorter than that cannot be checked -- and reading past
    # its end would crash the checker instead of failing the build.
    if not ck.check(len(data) >= 131,
                    f"FADT: {len(data)} bytes cover the ACPI 5.1 fields"):
        return
    profile = data[45]
    flags = struct.unpack_from("<I", data, 112)[0]
    arm_flags = struct.unpack_from("<H", data, 129)[0]
    ck.check(profile == 1,
             f"FADT: preferred PM profile is desktop (got {profile})")
    ck.check(bool(flags & (1 << 20)), "FADT: hardware-reduced ACPI set")
    ck.check(bool(arm_flags & 0x1), "FADT: PSCI compliant")
    # Bit 1 selects HVC for PSCI calls; Apple has no EL3, so m1n1 traps SMC.
    ck.check(not (arm_flags & 0x2), "FADT: PSCI calls use SMC (m1n1 traps them)")


def check_gtdt(data: bytes, ck: Checker) -> None:
    if not ck.check(len(data) >= 92,
                    f"GTDT: {len(data)} bytes cover the platform timer fields"):
        return
    non_secure_pl1 = struct.unpack_from("<I", data, 56)[0]
    virt = struct.unpack_from("<I", data, 64)[0]
    platform_timers = struct.unpack_from("<I", data, 88)[0]
    ck.check(non_secure_pl1 == 17,
             f"GTDT: non-secure EL1 timer GSIV {non_secure_pl1} == 17")
    ck.check(virt == 18, f"GTDT: virtual timer GSIV {virt} == 18")
    ck.check(platform_timers == 0,
             f"GTDT: no memory-mapped timer frames ({platform_timers})")


def check_dbg2(data: bytes, ck: Checker, plat: Platform) -> None:
    header_len = 44
    count = struct.unpack_from("<I", data, header_len - 4)[0]
    ck.check(count >= 1, f"DBG2: at least one debug device ({count})")
    if count < 1:
        return
    off = header_len
    if not ck.check(off + 12 <= len(data), "DBG2: device descriptor present"):
        return
    nlen = struct.unpack_from("<H", data, off + 1)[0]
    ck.check(nlen >= 22, f"DBG2: device descriptor length {nlen}")

    # The generic address registers follow the fixed 22-byte descriptor.
    gas_off = off + 22
    if not ck.check(gas_off + 12 <= len(data), "DBG2: GAS fits in the table"):
        return
    base = struct.unpack_from("<Q", data, gas_off + 4)[0]
    ck.check(base == plat.uart_base,
             f"DBG2: debug UART at 0x{base:x} == 0x{plat.uart_base:x}")


def check_mcfg(data: bytes, ck: Checker, plat: Platform) -> None:
    """Check the ECAM base and bus range Windows will use for PCIe."""
    if not ck.check(plat.pcie_ecam != 0,
                    "MCFG: platform has a PCIe contract"):
        return
    if not ck.check(len(data) >= 60,
                    f"MCFG: {len(data)} bytes hold the header and one entry"):
        return
    reserved = struct.unpack_from("<Q", data, 36)[0]
    ck.check(reserved == 0, "MCFG: reserved header field is zero")
    count = (len(data) - 44) // 16
    if not ck.check(count == 1,
                    f"MCFG: one allocation entry (found {count})"):
        return
    base, segment, start, end, _ = struct.unpack_from("<QHBBI", data, 44)
    ck.check(base == plat.pcie_ecam,
             f"MCFG: ECAM base 0x{base:x} == 0x{plat.pcie_ecam:x}")
    ck.check(segment == 0, f"MCFG: segment 0 (got {segment})")
    ck.check((start, end) == (plat.pcie_bus_min, plat.pcie_bus_max),
             f"MCFG: buses {start}-{end} == "
             f"{plat.pcie_bus_min}-{plat.pcie_bus_max}")


def check_dsdt(data: bytes, ck: Checker, plat: Platform) -> None:
    count = len(plat.cores)
    runs = cluster_runs(plat.cores)
    body = data[:struct.unpack_from("<I", data, 4)[0]]
    found = {
        node["name"]: node for node in aml_devices(body)
    }

    names = [f"CPU{i}" for i in range(count)]
    names += [f"CLU{i}" for i in range(len(runs))]
    names += ["DIE0", "COM0", "PCI0", "RES0"]
    present = [ck.check(name in found, f"DSDT: {name} device present")
               for name in names]
    if not all(present):
        return

    def within(child: str, parent: str) -> bool:
        inner, outer = found[child], found[parent]
        return outer["start"] < inner["start"] and inner["end"] <= outer["end"]

    for run_index, run in enumerate(runs):
        cluster = f"CLU{run_index}"
        ck.check(within(cluster, "DIE0"), f"DSDT: {cluster} sits inside DIE0")
        for index in run:
            ck.check(within(f"CPU{index}", cluster),
                     f"DSDT: CPU{index} sits inside {cluster}")

    # The _UID values are what Windows matches against the MADT GICC UIDs; a
    # device in the right place with the wrong _UID is still a broken topology.
    expected_uids = {f"CPU{i}": i for i in range(count)}
    expected_uids.update({f"CLU{i}": i + 1 for i in range(len(runs))})
    expected_uids["DIE0"] = 0
    for name, want in expected_uids.items():
        got = aml_property(body, found[name], "_UID")
        ck.check(got == want, f"DSDT: {name} _UID {got} == {want}")

    hids = [aml_string(body, found[f"CPU{i}"], "_HID") for i in range(count)]
    ck.check(all(hid == "ACPI0007" for hid in hids),
             "DSDT: every CPU device is ACPI0007")

    hid = aml_string(body, found["COM0"], "_HID")
    ck.check(hid == "APPL8900", f"DSDT: COM0 _HID {hid!r} == 'APPL8900'")
    memories, gsivs, _ = aml_resources(body, found["COM0"], "_CRS")
    want_memory = (plat.uart_base, plat.uart_base + 0xFFF, 0, 0x1000)
    ck.check(memories == [want_memory],
             f"DSDT: COM0 _CRS is 0x{plat.uart_base:x}+0x1000 "
             f"(got {[(hex(a), hex(b), hex(c), hex(d)) for a, b, c, d in memories]})")
    ck.check(gsivs == [plat.uart_irq],
             f"DSDT: COM0 _CRS interrupt {gsivs} == [{plat.uart_irq}]")

    #
    # PCI0 is the root bridge for the first APCIE node.  MCFG and the _CRS
    # have to agree with each other and with the captured bus/host windows;
    # a device BAR assigned on the wrong side of the translation decodes
    # nowhere, which is exactly the failure mode this project already hit.
    #
    pci = found["PCI0"]
    ck.check(aml_string(body, pci, "_HID") == "PNP0A08",
             f"DSDT: PCI0 _HID {aml_string(body, pci, '_HID')!r} == 'PNP0A08'")
    ck.check(aml_string(body, pci, "_CID") == "PNP0A03",
             f"DSDT: PCI0 _CID {aml_string(body, pci, '_CID')!r} == 'PNP0A03'")
    ck.check(aml_string(body, pci, "_UID") == "PCI0",
             f"DSDT: PCI0 _UID {aml_string(body, pci, '_UID')!r} == 'PCI0'")
    ck.check(aml_property(body, pci, "_SEG") == 0,
             f"DSDT: PCI0 _SEG {aml_property(body, pci, '_SEG')} == 0")
    ck.check(aml_property(body, pci, "_BBN") == 0,
             f"DSDT: PCI0 _BBN {aml_property(body, pci, '_BBN')} == 0")
    ck.check(aml_property(body, pci, "_CCA") == 1,
             f"DSDT: PCI0 _CCA is DMA coherent")
    cba = aml_method_return(body, pci, "_CBA")
    ck.check(cba == plat.pcie_ecam,
             f"DSDT: PCI0 _CBA 0x{cba:x} == 0x{plat.pcie_ecam:x}" if cba is not None
             else "DSDT: PCI0 _CBA present")

    pci_memories, _, pci_buses = aml_resources(body, pci, "_CRS")
    want_bus = (plat.pcie_bus_min, plat.pcie_bus_max, 0,
                plat.pcie_bus_max - plat.pcie_bus_min + 1)
    ck.check(pci_buses == [want_bus],
             f"DSDT: PCI0 _CRS bus range {pci_buses} == "
             f"{[want_bus]}")
    want_32 = (
        plat.pcie_mmio32_bus,
        plat.pcie_mmio32_bus + plat.pcie_mmio32_size - 1,
        plat.pcie_mmio32_host - plat.pcie_mmio32_bus,
        plat.pcie_mmio32_size,
    )
    want_prefetch = (
        plat.pcie_prefetch,
        plat.pcie_prefetch + plat.pcie_prefetch_size - 1,
        0,
        plat.pcie_prefetch_size,
    )
    ck.check(pci_memories == [want_32, want_prefetch],
             f"DSDT: PCI0 _CRS windows {[(hex(a), hex(b), hex(c), hex(d)) for a, b, c, d in pci_memories]} "
             f"== {[(hex(a), hex(b), hex(c), hex(d)) for a, b, c, d in [want_32, want_prefetch]]}")

    res = found["RES0"]
    ck.check(aml_string(body, res, "_HID") == "PNP0C02",
             f"DSDT: RES0 _HID {aml_string(body, res, '_HID')!r} == 'PNP0C02'")
    res_memories, _, _ = aml_resources(body, res, "_CRS")
    want_ecam = (
        plat.pcie_ecam,
        plat.pcie_ecam + plat.pcie_ecam_size - 1,
        0,
        plat.pcie_ecam_size,
    )
    ck.check(res_memories == [want_ecam],
             f"DSDT: RES0 _CRS ECAM "
             f"{[(hex(a), hex(b), hex(c), hex(d)) for a, b, c, d in res_memories]} "
             f"== {[(hex(a), hex(b), hex(c), hex(d)) for a, b, c, d in [want_ecam]]}")


def check_image(build_dir: str, ck: Checker, plat: Platform) -> None:
    """Validate the FD image header and size contract."""
    candidates = glob.glob(os.path.join(os.path.dirname(build_dir.rstrip("/")),
                                        "FV", "*_EFI.fd"))
    if not candidates:
        # A completed build always leaves an FD; if it is missing, the image
        # contract was not checked and the build should not be certified.
        ck.check(False, "image: FV/*_EFI.fd present")
        return
    path = candidates[0]
    size = os.path.getsize(path)
    ck.check(size <= plat.fd_size,
             f"image: {os.path.basename(path)} is 0x{size:x} bytes "
             f"(FDF size 0x{plat.fd_size:x})")
    if size != plat.fd_size:
        ck.note(f"image: image ends 0x{plat.fd_size - size:x} bytes before the "
                f"declared FD size")

    with open(path, "rb") as f:
        header = f.read(64)
    ck.check(header[56:60] == b"ARM\x64", "image: ARM64 Image magic present")
    text_offset = struct.unpack_from("<Q", header, 8)[0]
    image_size = struct.unpack_from("<Q", header, 16)[0]
    flags = struct.unpack_from("<Q", header, 24)[0]
    ck.check(text_offset == 0x80000,
             f"image: text_offset 0x{text_offset:x} == 0x80000")
    ck.check(size <= image_size <= plat.fd_size,
             f"image: header image_size 0x{image_size:x} covers the payload "
             f"(0x{size:x}) and fits the FD (0x{plat.fd_size:x})")
    ck.check(flags == 0, f"image: flags 0x{flags:x} (little-endian)")


# --------------------------------------------------------------------------
# Modes.
# --------------------------------------------------------------------------

def print_topology(build_dir: str, tables: dict[str, str]) -> int:
    """Print a Platform entry describing the tables in a build tree."""
    for needed in ("APIC", "DBG2"):
        if needed not in tables:
            sys.exit(f"error: no {needed} table in this build tree")

    with open(tables["APIC"], "rb") as f:
        madt = read_madt(f.read())
    with open(tables["DBG2"], "rb") as f:
        dbg2 = f.read()
    uart = struct.unpack_from("<Q", dbg2, 44 + 22 + 4)[0]

    mpidrs = madt["mpidrs"]
    cores = ",\n            ".join(
        ", ".join(f"0x{m:x}" for m in mpidrs[i:i + 6])
        for i in range(0, len(mpidrs), 6)
    )
    print("Copy this into PLATFORMS, then verify every value against the machine")
    print("and record the source in docs/hardware-values.md:\n")
    print('    "<platform>": Platform(')
    print('        name="<model> (<chip>)",')
    print(f"        cores=(\n            {cores},\n        ),")
    print(f"        efficiency={tuple(madt['efficiency'])!r},")
    print(f"        gicd_base=0x{madt.get('gicd_base', 0):x},")
    print(f"        gicr_base=0x{madt.get('gicr_base', 0):x},")
    print(f"        gicr_stride=0x{madt.get('gicr_size', 0) // max(len(mpidrs), 1):x},")
    print(f"        uart_base=0x{uart:x},")
    print("        uart_irq=<AIC IRQ>,")
    images = glob.glob(os.path.join(os.path.dirname(build_dir.rstrip("/")),
                                    "FV", "*_EFI.fd"))
    if images:
        print(f"        fd_size=0x{os.path.getsize(images[0]):x},  "
              f"# actual image size")
    print("    ),")
    return 0


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
                        help="the AARCH64 build output directory, e.g. "
                             "Build/<platform>-AARCH64/<tag>/AARCH64")
    parser.add_argument("--print-topology", action="store_true",
                        help="print a PLATFORMS entry for the build tree")
    parser.add_argument("--unvalidated-ok", action="store_true",
                        help="exit 0 for a platform with no PLATFORMS entry; "
                             "for reference builds the checker does not certify")
    args = parser.parse_args()

    build_dir = resolve_build_dir(args.platform, args.build_dir)
    tables = find_tables(build_dir)
    if not tables:
        sys.exit(f"error: no ACPI tables found under {build_dir}")

    if args.print_topology:
        print(f"topology of {build_dir}\n")
        return print_topology(build_dir, tables)

    plat = PLATFORMS.get(args.platform)
    if plat is None:
        message = (
            f"no validated expectations for platform '{args.platform}'.\n"
            f"  validated: {', '.join(sorted(PLATFORMS))}\n"
            f"  run with --print-topology to generate an entry, then check "
            f"every value against the machine before adding it."
        )
        if not args.unvalidated_ok:
            sys.exit(f"error: {message}")
        print(f"note: {message}\n"
              f"      --unvalidated-ok: the build was not checked.")
        return 0

    print(f"checking {plat.name}: {build_dir}\n")

    ck = Checker()
    order = [("MADT", "APIC"), ("PPTT", "PPTT"), ("FADT", "FACP"),
             ("DSDT", "DSDT"), ("MCFG", "MCFG"), ("DBG2", "DBG2"),
             ("GTDT", "GTDT")]
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
            check_madt(data, ck, plat)
        elif sig == "PPTT":
            check_pptt(data, ck, plat)
        elif sig == "FACP":
            check_fadt(data, ck)
        elif sig == "DSDT":
            check_dsdt(data, ck, plat)
        elif sig == "MCFG":
            check_mcfg(data, ck, plat)
        elif sig == "DBG2":
            check_dbg2(data, ck, plat)
        elif sig == "GTDT":
            check_gtdt(data, ck)
        print()

    print("image")
    check_image(build_dir, ck, plat)
    print()

    if ck.failures:
        print(f"{len(ck.failures)} check(s) failed")
        return 1
    print("all ACPI and image checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
