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


def run_checker(build_dir: str,
                firmware_root: str | None = None,
                env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    argv = [sys.executable, CHECKER, "--build-dir", build_dir]
    if firmware_root is not None:
        argv += ["--firmware-root", firmware_root]
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def msi_frame_offsets(madt: bytes) -> list[int]:
    """Return the byte offset of every GIC MSI Frame entry in a MADT."""
    offsets = []
    off = 44
    while off + 2 <= len(madt):
        etype, elen = madt[off], madt[off + 1]
        if elen < 2:
            break
        if etype == 0x0D:
            offsets.append(off)
        off += elen
    return offsets


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

    def test_msi_frame_is_checked(self) -> None:
        # MSI reaches Windows only through this entry: an OS with no MSI
        # controller never programs a device's MSI-X table, and an OS with the
        # wrong window programs doorbell indices m1n1 does not translate.
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("MADT: MSI doorbell 0xfffff000", result.stdout)
        self.assertIn("MADT: MSI SPIs 0x200+256", result.stdout)
        self.assertIn("window ends at SPI 767 (<= 1019)", result.stdout)

    def test_msi_range_is_checked_without_the_hypervisor_tree(self) -> None:
        # The SPI and INTID limits are ACPI/GIC facts, not m1n1 constants.  The
        # CI "checks" job does not check out the hypervisor submodule; the
        # architectural checks must not disappear with it.
        env = dict(os.environ, M1N1_ROOT=os.path.join(self.tmp, "no-such-tree"))
        result = run_checker(self.build_dir, env=env)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("hypervisor tree not checked out", result.stdout)
        self.assertIn("window ends at SPI 767 (<= 1019)", result.stdout)


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

    def edit_msi_frame(self, **fields) -> None:
        """Rewrite one field of the fixture's GIC MSI Frame entry."""
        madt = self.table("*MADT*")
        with open(madt, "rb") as f:
            data = f.read()
        off = msi_frame_offsets(data)[0]
        order = {"frame_id": ("<I", 4), "address": ("<Q", 8),
                 "flags": ("<I", 16), "count": ("<H", 20), "base": ("<H", 22)}
        for name, value in fields.items():
            fmt, delta = order[name]
            self.patch_bytes(madt, off + delta, struct.pack(fmt, value))

    def test_wrong_msi_doorbell(self) -> None:
        self.edit_msi_frame(address=0xFFFFF004)
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("MSI doorbell", result.stdout)

    def test_wrong_msi_spi_window(self) -> None:
        self.edit_msi_frame(base=1024)
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("MSI SPIs", result.stdout)

    def test_msi_window_past_the_spi_range(self) -> None:
        # 512 + 512 = 1024 would run past SPI 1019 and into the reserved range,
        # where the guest's interrupt controller has no state.  This has to be
        # rejected without the hypervisor tree: the limit is architectural.
        self.edit_msi_frame(count=512)
        env = dict(os.environ, M1N1_ROOT=os.path.join(self.tmp, "no-such-tree"))
        result = run_checker(self.build_dir, env=env)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("window ends at SPI 1023 (<= 1019)", result.stdout)

    def test_missing_msi_frame(self) -> None:
        madt = self.table("*MADT*")
        with open(madt, "rb") as f:
            data = bytearray(f.read())
        self.patch_bytes(madt, msi_frame_offsets(data)[0], b"\x0e")
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("GIC MSI frame (0 found)", result.stdout)

    def test_msi_window_disagrees_with_the_hypervisor(self) -> None:
        # The half of the contract the firmware cannot see: m1n1's own table.
        tree = os.path.join(self.tmp, "m1n1")
        os.makedirs(os.path.join(tree, "src"))
        with open(os.path.join(tree, "src", "guest_msi.h"), "w") as f:
            f.write("#define GUEST_MSI_SPI_BASE 256\n"
                    "#define GUEST_MSI_VECTORS  256\n"
                    "#define GUEST_GIC_MAX_INTID 1023\n"
                    "#define GIC_LAST_SPI 1019\n")
        env = dict(os.environ, M1N1_ROOT=tree)
        result = run_checker(self.build_dir, env=env)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("hypervisor SPI base 256 == MADT 512", result.stdout)

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


PCD_FIXTURES = os.path.join(TESTS_DIR, "fixtures", "pcd")
FW_ROOT = os.path.join(ROOT, "third_party", "ntasp-firmware")
REAL_FAMILY_INC = os.path.join(FW_ROOT, "Silicon", "Apple", "T8132FamilyPkg",
                               "T8132FamilyPkg.dsc.inc")


def load_checker_module():
    """Import scripts/check-acpi.py (its name is not a valid identifier)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_acpi", CHECKER)
    module = importlib.util.module_from_spec(spec)
    # Register before executing: from 3.14 on, @dataclass looks the defining
    # module up in sys.modules and fails on a module that was never added.
    sys.modules["check_acpi"] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules["check_acpi"]
        raise
    return module


class TestPciePcds(FixtureCase):
    """The PCIe windows are described twice, so both are checked.

    The host bridge library builds its apertures from the PCDs and Windows
    assigns BARs out of the DSDT _CRS.  A mismatch still enumerates, so it
    survives every check that looks at one side only -- and then no device BAR
    decodes.  These tests use tests/fixtures/pcd, which mirrors the firmware
    tree's layout so the checker needs nothing beyond --firmware-root.
    """

    def setUp(self) -> None:
        super().setUp()
        self.fw = os.path.join(self.tmp, "firmware")
        shutil.copytree(PCD_FIXTURES, self.fw)
        self.dsc = os.path.join(self.fw, "Silicon", "Apple", "T8132FamilyPkg",
                                "T8132FamilyPkg.dsc.inc")

    def edit_pcd(self, old: str, new: str) -> None:
        with open(self.dsc, "r", encoding="utf-8") as f:
            text = f.read()
        self.assertIn(old, text, f"fixture lost {old!r}")
        with open(self.dsc, "w", encoding="utf-8") as f:
            f.write(text.replace(old, new))

    def test_matching_pcds_pass(self) -> None:
        result = run_checker(self.build_dir, self.fw)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PCD: gEfiMdePkgTokenSpaceGuid.PcdPciMmio32Translation "
                      "= 0xb00000000 == 0xb00000000", result.stdout)

    def test_translation_sign_is_enforced(self) -> None:
        # The bug this check exists for: bus - host instead of host - bus.  It
        # compiles, it boots, PCI enumeration works, and every BAR decodes
        # nowhere, which is a very expensive way to find out.
        self.edit_pcd("PcdPciMmio32Translation|0xB00000000",
                      "PcdPciMmio32Translation|0xFFFFFFF500000000")
        result = run_checker(self.build_dir, self.fw)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("PcdPciMmio32Translation", result.stdout)

    def test_missing_pcd_fails(self) -> None:
        self.edit_pcd(
            "  gAppleSiliconPkgTokenSpaceGuid.PcdPciExpressBarLimit|0x1CBFFFFFFF\n", "")
        result = run_checker(self.build_dir, self.fw)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("PcdPciExpressBarLimit is set", result.stdout)

    def test_conflicting_sections_fail(self) -> None:
        # In a DSC the later section silently wins, so two different values for
        # the same window have to be an error rather than a coin toss.
        with open(self.dsc, "a", encoding="utf-8") as f:
            f.write("\n[PcdsFixedAtBuild.common]\n"
                    "  gArmTokenSpaceGuid.PcdPciMmio32Size|0x80000000\n")
        result = run_checker(self.build_dir, self.fw)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("different values", result.stdout)

    def test_missing_firmware_tree_is_noted(self) -> None:
        # CI's static job checks out no submodules; the check is skipped loudly
        # rather than passing in silence.
        result = run_checker(self.build_dir, os.path.join(self.tmp, "absent"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("skipping the PCIe PCD check", result.stdout)


@unittest.skipUnless(os.path.isfile(REAL_FAMILY_INC),
                     "ntasp-firmware submodule not checked out")
class TestRealPciePcds(FixtureCase):
    """The real Mac16_3 sources must satisfy the same contract."""

    def test_submodule_pcds_pass(self) -> None:
        result = run_checker(self.build_dir, FW_ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PCD: gEfiMdePkgTokenSpaceGuid.PcdPciMmio32Translation",
                      result.stdout)

    def test_fixture_matches_the_submodule(self) -> None:
        # The fixture is a copy, so every PCD the tests above mutate has to
        # still have the value the firmware really builds with.
        module = load_checker_module()
        fixture = module.read_pcds([os.path.join(
            PCD_FIXTURES, "Silicon", "Apple", "T8132FamilyPkg",
            "T8132FamilyPkg.dsc.inc")])
        real = module.read_pcds([REAL_FAMILY_INC])
        for name, values in fixture.items():
            self.assertIn(name, real, f"fixture has {name}, the submodule does not")
            self.assertEqual(sorted(set(values)), sorted(set(real[name])),
                             f"fixture and submodule disagree about {name}")


if __name__ == "__main__":
    unittest.main()
