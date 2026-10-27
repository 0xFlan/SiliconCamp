#!/usr/bin/env python3
"""Regression tests for scripts/check-acpi.py.

The fixtures under tests/fixtures/mac16_3 are the real tables the M4 build
emitted, so these tests fail if the checker stops understanding the tables that
Project Mu actually produces. Each negative test mutates one field and asserts
that the checker rejects it, which is the property that makes the checker
useful as a build gate.

Run with: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import glob
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
CHECKER = os.path.join(ROOT, "scripts", "check-acpi.py")
FIXTURES = os.path.join(TESTS_DIR, "fixtures", "mac16_3")

# First 64 bytes of the MAC16_3_EFI.fd produced by the 2026-10-26 M4 build.
# Only the header is a fixture: the 30 MB image is a release asset, so the
# tests pad this header to a few pages instead of committing the payload.
ARM64_HEADER = bytes.fromhex(
    "01000010ff1f0014"        # code0/code1
    "0000080000000000"        # text_offset = 0x80000
    "0000e00100000000"        # image_size = 0x1e00000
    "0000000000000000"        # flags
    "0000000000000000"        # reserved[3]
    "0000000000000000"
    "0000000000000000"
    "41524d64"                # "ARM\x64"
    "00000000"                # reserved
)


def run_checker(build_dir: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, CHECKER, "--build-dir", build_dir],
        capture_output=True,
        text=True,
        check=False,
    )


def gicc_offsets(madt: bytes) -> list[int]:
    """Return the byte offset of every GICC entry in a MADT."""
    offsets = []
    off = 44
    while off + 2 <= len(madt):
        etype, elen = madt[off], madt[off + 1]
        if elen < 2:
            break
        if etype == 0x0B:
            offsets.append(off)
        off += elen
    return offsets


class FixtureCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="acpi-test-")
        self.build_dir = os.path.join(self.tmp, "AARCH64")
        shutil.copytree(os.path.join(FIXTURES, "AARCH64"), self.build_dir)
        # check_image() reads the FD from the FV/ directory next to AARCH64/,
        # exactly as a real build tree lays it out.
        self.image = os.path.join(self.tmp, "FV", "MAC16_3_EFI.fd")
        os.makedirs(os.path.dirname(self.image))
        with open(self.image, "wb") as f:
            f.write(ARM64_HEADER + b"\0" * (0x8000 - len(ARM64_HEADER)))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def table(self, pattern: str) -> str:
        found = glob.glob(os.path.join(self.build_dir, "OUTPUT", pattern))
        self.assertTrue(found, f"fixture {pattern} missing")
        return found[0]

    def patch_bytes(self, path: str, offset: int, payload: bytes) -> None:
        with open(path, "r+b") as f:
            f.seek(offset)
            f.write(payload)


class TestCheckerPasses(FixtureCase):
    def test_real_tables_pass(self) -> None:
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("all ACPI and image checks passed", result.stdout)

    def test_reports_which_tables_it_read(self) -> None:
        result = run_checker(self.build_dir)
        for label in ("MADT", "PPTT", "FADT", "DSDT", "MCFG", "DBG2", "GTDT"):
            self.assertIn(label, result.stdout)

    def test_image_checks_run(self) -> None:
        # The FD header is part of the platform contract; the checker must not
        # silently skip it when the build tree has no image.
        result = run_checker(self.build_dir)
        self.assertNotIn("skipping image checks", result.stdout)
        self.assertIn("image: ARM64 Image magic present", result.stdout)


class TestTopologyDump(FixtureCase):
    def test_dump_describes_the_build(self) -> None:
        result = subprocess.run(
            [sys.executable, CHECKER, "--print-topology", "--build-dir",
             self.build_dir],
            capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for mpidr in ("0x0", "0x5", "0x10100", "0x10103"):
            self.assertIn(mpidr, result.stdout)
        self.assertIn("efficiency=(0, 0, 0, 0, 0, 0, 1, 1, 1, 1)", result.stdout)
        self.assertIn("gicr_base=0x5100000000", result.stdout)
        self.assertIn("uart_base=0x3ad200000", result.stdout)


class TestCheckerRejects(FixtureCase):
    def test_unvalidated_platform_is_an_error(self) -> None:
        # A checker that guesses would bless a wrong package; it must refuse.
        result = subprocess.run(
            [sys.executable, CHECKER, "--platform", "NotARealPlatform",
             "--build-dir", self.build_dir],
            capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no validated expectations", result.stderr)

    def test_unvalidated_platform_can_be_skipped_explicitly(self) -> None:
        # build-uefi-linux.sh uses this for the M2 reference package, whose
        # tables the M4 expectations do not describe.
        result = subprocess.run(
            [sys.executable, CHECKER, "--platform", "MacBookProEarly2023",
             "--unvalidated-ok", "--build-dir", self.build_dir],
            capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("the build was not checked", result.stdout)

    def test_wrong_mpidr(self) -> None:
        madt = self.table("*MADT*")
        with open(madt, "rb") as f:
            data = f.read()
        first = gicc_offsets(data)[0]
        self.patch_bytes(madt, first + 68, struct.pack("<Q", 0x10103))
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("MPIDRs", result.stdout)

    def test_wrong_efficiency_class(self) -> None:
        madt = self.table("*MADT*")
        with open(madt, "rb") as f:
            data = f.read()
        first = gicc_offsets(data)[0]
        self.patch_bytes(madt, first + 76, b"\x01")
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("efficiency class", result.stdout)

    def test_wrong_gicr_range(self) -> None:
        madt = self.table("*MADT*")
        with open(madt, "rb") as f:
            data = f.read()
        off = 44
        while off + 2 <= len(data):
            etype, elen = data[off], data[off + 1]
            if etype == 0x0E:
                break
            off += elen
        self.patch_bytes(madt, off + 12, struct.pack("<I", 0x20000))
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("GICR range", result.stdout)

    def test_missing_table(self) -> None:
        os.remove(self.table("GTDT*"))
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("GTDT: table present", result.stdout)

    def test_missing_mcfg(self) -> None:
        # PCI0 without an MCFG entry still boots, but Windows has no way to
        # find configuration space; the build must not quietly omit it.
        os.remove(self.table("MCFG*"))
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("MCFG: table present", result.stdout)

    def test_wrong_mcfg_bus_range(self) -> None:
        # Header is 44 bytes; EndBusNumber is the last byte of the entry.
        self.patch_bytes(self.table("MCFG*"), 55, b"\x07")
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("MCFG: buses", result.stdout)

    def test_wrong_pci0_window(self) -> None:
        # The DSDT 32-bit window carries host - bus = 0xb00000000.  A wrong
        # translation is the exact failure the project already hit once, and
        # it leaves every device BAR decoding nowhere.
        dsdt = self.table("DSDT.aml")
        with open(dsdt, "rb") as f:
            data = f.read()
        needle = struct.pack("<Q", 0xB00000000)
        offset = data.find(needle)
        self.assertGreater(offset, 0, "fixture lost PCI0's translation")
        self.patch_bytes(dsdt, offset, struct.pack("<Q", 0xB00001000))
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("PCI0 _CRS windows", result.stdout)

    def test_wrong_pci0_cba(self) -> None:
        # _CBA is what Windows uses to map ECAM; MCFG and RES0 can be right
        # while this method returns a different window.
        dsdt = self.table("DSDT.aml")
        with open(dsdt, "rb") as f:
            data = f.read()
        cba = data.find(b"_CBA")
        self.assertGreater(cba, 0, "fixture lost PCI0's _CBA")
        offset = data.find(struct.pack("<Q", 0x1CB0000000), cba)
        self.assertGreater(offset, 0, "fixture lost _CBA's return value")
        self.patch_bytes(dsdt, offset, struct.pack("<Q", 0x1CB1000000))
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("PCI0 _CBA", result.stdout)

    def test_wrong_res0_ecam(self) -> None:
        # RES0 has to reserve the whole 256 MB ECAM aperture, not just the
        # first window the compiler happened to see.
        dsdt = self.table("DSDT.aml")
        with open(dsdt, "rb") as f:
            data = f.read()
        res0 = data.find(b"RES0")
        self.assertGreater(res0, 0, "fixture lost RES0")
        offset = data.find(struct.pack("<Q", 0x1CB0000000), res0)
        self.assertGreater(offset, 0, "fixture lost RES0's ECAM base")
        self.patch_bytes(dsdt, offset, struct.pack("<Q", 0x1CB1000000))
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("RES0 _CRS ECAM", result.stdout)

    def test_truncated_table(self) -> None:
        dsdt = self.table("DSDT.aml")
        with open(dsdt, "r+b") as f:
            f.truncate(16)
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("too short", result.stdout)

    def test_wrong_cpu_uid(self) -> None:
        # The device name and the MADT stay valid; only the _UID is wrong.
        # Windows matches CPUs by _UID, so this must fail the build.
        dsdt = self.table("DSDT.aml")
        with open(dsdt, "rb") as f:
            data = f.read()
        uid = data.find(b"\x08_UID", data.find(b"CPU3") + 4)
        self.assertGreater(uid, 0, "fixture lost CPU3's _UID")
        self.patch_bytes(dsdt, uid + 5, b"\x07")
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("CPU3 _UID", result.stdout)

    def test_wrong_uart_address(self) -> None:
        dsdt = self.table("DSDT.aml")
        with open(dsdt, "rb") as f:
            data = f.read()
        address = data.find(struct.pack("<Q", 0x3AD200000))
        self.assertGreater(address, 0, "fixture lost COM0's MMIO base")
        self.patch_bytes(dsdt, address, struct.pack("<Q", 0x3AD201000))
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("COM0 _CRS", result.stdout)

    def test_wrong_uart_irq(self) -> None:
        dsdt = self.table("DSDT.aml")
        with open(dsdt, "rb") as f:
            data = f.read()
        irq = data.find(struct.pack("<I", 1046))
        self.assertGreater(irq, 0, "fixture lost COM0's interrupt")
        self.patch_bytes(dsdt, irq, struct.pack("<I", 1045))
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("COM0 _CRS interrupt", result.stdout)

    def test_fadt_profile_is_not_desktop(self) -> None:
        self.patch_bytes(self.table("FADT*"), 45, b"\x02")
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("preferred PM profile", result.stdout)

    def test_fadt_not_hardware_reduced(self) -> None:
        fadt = self.table("FADT*")
        with open(fadt, "rb") as f:
            flags = struct.unpack_from("<I", f.read(116), 112)[0]
        self.patch_bytes(fadt, 112, struct.pack("<I", flags & ~(1 << 20)))
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("hardware-reduced", result.stdout)

    def test_fadt_psci_over_hvc(self) -> None:
        self.patch_bytes(self.table("FADT*"), 129, struct.pack("<H", 0x3))
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("PSCI calls use SMC", result.stdout)

    def test_short_fadt_fails_without_crashing(self) -> None:
        fadt = self.table("FADT*")
        with open(fadt, "r+b") as f:
            f.truncate(112)
            f.seek(4)
            f.write(struct.pack("<I", 112))
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FADT: 112 bytes cover", result.stdout)

    def test_wrong_gtdt_el1_timer(self) -> None:
        self.patch_bytes(self.table("GTDT*"), 56, struct.pack("<I", 16))
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("non-secure EL1 timer GSIV", result.stdout)

    def test_wrong_gtdt_virtual_timer(self) -> None:
        self.patch_bytes(self.table("GTDT*"), 64, struct.pack("<I", 19))
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("virtual timer GSIV", result.stdout)

    def test_short_gtdt_fails_without_crashing(self) -> None:
        gtdt = self.table("GTDT*")
        with open(gtdt, "r+b") as f:
            f.truncate(60)
            f.seek(4)
            f.write(struct.pack("<I", 60))
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("GTDT: 60 bytes cover", result.stdout)

    def test_wrong_dbg2_uart(self) -> None:
        # DBG2's generic address register starts 22 bytes into the descriptor.
        self.patch_bytes(self.table("DBG2*"), 70, struct.pack("<Q", 0x3AD201000))
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("debug UART", result.stdout)

    def test_missing_image(self) -> None:
        os.remove(self.image)
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("image: FV/*_EFI.fd present", result.stdout)

    def test_image_bad_magic(self) -> None:
        self.patch_bytes(self.image, 56, b"ARM\x65")
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("ARM64 Image magic", result.stdout)

    def test_image_bad_text_offset(self) -> None:
        self.patch_bytes(self.image, 8, struct.pack("<Q", 0x40000))
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("text_offset", result.stdout)

    def test_image_size_too_small(self) -> None:
        # 0x7000 does not cover the 0x8000-byte file, so the header lies about
        # the payload and a loader that trusts it would cut the image short.
        self.patch_bytes(self.image, 16, struct.pack("<Q", 0x7000))
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("header image_size", result.stdout)


if __name__ == "__main__":
    unittest.main()
