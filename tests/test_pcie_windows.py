#!/usr/bin/env python3
"""Pin the captured J624 PCIe windows and the bus->host arithmetic.

The PCIe values shipped twice with one hex digit missing (0xb8000000 instead of
0xb80000000), which handed devices BARs that decode nowhere. Nothing in the
ACPI tables or the image header notices that: the firmware still builds and the
checker still passes. These tests compare the numbers the firmware is built
with against the values captured in docs/pcie.md, and re-run the host bridge's
translation formula.

They read the patch, not the submodule, because the patch is what CI can see and
what actually reproduces the firmware tree.
"""

from __future__ import annotations

import os
import re
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
PATCH = os.path.join(ROOT, "patches", "ntasp-firmware",
                     "0001-m4-uefi-platform.patch")

MASK64 = (1 << 64) - 1

# Captured from `apcie@B0000000` on this Mac16,3: docs/pcie.md has the raw cells.
BUS_BASE = 0x80000000
HOST_BASE = 0xB80000000
WINDOW_SIZE = 0x40000000
HOST_OFFSET = HOST_BASE - BUS_BASE
ECAM_BASE = 0x1CB0000000
ECAM_SIZE = 0x10000000
PREFETCH_BASE = 0xBC0000000
PREFETCH_SIZE = 0x20000000


class TestPcieWindows(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with open(PATCH, "r", encoding="utf-8") as f:
            cls.patch = f.read()

    def pcd(self, name: str) -> int:
        match = re.search(rf"^\+[^\n]*{re.escape(name)}\|(0x[0-9A-Fa-f]+)",
                          self.patch, re.MULTILINE)
        self.assertIsNotNone(match, f"{name} is not in the patch")
        return int(match.group(1), 16)

    def define(self, name: str) -> int:
        match = re.search(rf"^\+#define {re.escape(name)} (0x[0-9A-Fa-f]+)",
                          self.patch, re.MULTILINE)
        self.assertIsNotNone(match, f"{name} is not in the patch")
        return int(match.group(1), 16)

    def test_host_bridge_pcds_carry_the_captured_window(self) -> None:
        self.assertEqual(self.pcd("PcdPciBusMin"), 0)
        self.assertEqual(self.pcd("PcdPciBusMax"), 8)
        self.assertEqual(self.pcd("PcdPciMmio32Base"), BUS_BASE)
        self.assertEqual(self.pcd("PcdPciMmio32Size"), WINDOW_SIZE)
        self.assertEqual(self.pcd("PcdPciMmio32Translation"),
                         (-HOST_OFFSET) & MASK64)
        self.assertEqual(self.pcd("PcdPciMmio64Base"), PREFETCH_BASE)
        self.assertEqual(self.pcd("PcdPciMmio64Size"), PREFETCH_SIZE)
        self.assertEqual(self.pcd("PcdPciExpressBaseAddress"), ECAM_BASE)

    def test_page_tables_map_the_host_addresses(self) -> None:
        self.assertEqual(self.define("APPLE_PCIE_ECAM_RANGE_BASE"), ECAM_BASE)
        self.assertEqual(self.define("APPLE_PCIE_ECAM_RANGE_SIZE"), ECAM_SIZE)
        self.assertEqual(self.define("APPLE_PCIE_MMIO_RANGE_23_BASE"), HOST_BASE)
        self.assertEqual(self.define("APPLE_PCIE_MMIO_RANGE_23_SIZE"),
                         WINDOW_SIZE)
        self.assertEqual(self.define("APPLE_PCIE_MMIO_RANGE_24_BASE"),
                         PREFETCH_BASE)
        self.assertEqual(self.define("APPLE_PCIE_MMIO_RANGE_24_SIZE"),
                         PREFETCH_SIZE)
        # The prefetch window starts where the 32-bit window ends; anything
        # else means one of the two bases lost a digit.
        self.assertEqual(HOST_BASE + WINDOW_SIZE, PREFETCH_BASE)

    def test_apple_host_bridge_formula_returns_the_captured_host_base(self) -> None:
        # AppleSiliconPciHostBridgeLib computes Translation as
        # Base - (Base + PcdPciMmio32Translation); the host aperture is
        # Base + Translation.
        base = self.pcd("PcdPciMmio32Base")
        pcd = self.pcd("PcdPciMmio32Translation")
        translation = (base - ((base + pcd) & MASK64)) & MASK64
        self.assertEqual(translation, HOST_OFFSET)
        self.assertEqual((base + translation) & MASK64, HOST_BASE)


if __name__ == "__main__":
    unittest.main()
