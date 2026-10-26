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
        for label in ("MADT", "PPTT", "FADT", "DSDT", "DBG2", "GTDT"):
            self.assertIn(label, result.stdout)


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

    def test_truncated_table(self) -> None:
        dsdt = self.table("DSDT.aml")
        with open(dsdt, "r+b") as f:
            f.truncate(16)
        result = run_checker(self.build_dir)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("too short", result.stdout)


if __name__ == "__main__":
    unittest.main()
