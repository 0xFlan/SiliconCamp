#!/usr/bin/env python3
"""Pin the captured J624 PCIe windows and the bus<->host arithmetic.

Two different mistakes have already been made in these numbers:

  * the values shipped with one hex digit missing (0xb8000000 instead of
    0xb80000000), and
  * PcdPciMmio32Translation carried bus - host instead of host - bus.

Neither is visible in a build, an image header or an ACPI table on its own: the
firmware compiles, PCI configuration space still enumerates, and every device BAR
then decodes nowhere. These tests compare the numbers the firmware is built with
against the values captured in docs/pcie.md, and re-run the arithmetic two edk2
files perform on them:

  * ``AppleSiliconPciHostBridgeLib`` builds the aperture translation as
    ``Base - (Base + PcdPciMmio32Translation)``, i.e. ``-PcdPciMmio32Translation``;
  * ``MdeModulePkg/Bus/Pci/PciHostBridgeDxe/PciHostBridge.h`` then converts a
    device address to a host address with ``TO_HOST_ADDRESS(dev, tra) = dev - tra``,
    so the aperture field has to be ``bus - host``.

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
        # The PCD is host - bus; the library negates it into the aperture's
        # bus - host translation.  The T602X platform's value has the same sign
        # (host 0x5c0000000, bus 0xc0000000, PCD 0x500000000).
        self.assertEqual(self.pcd("PcdPciMmio32Translation"), HOST_OFFSET)
        self.assertEqual(self.pcd("PcdPciMmio64Base"), PREFETCH_BASE)
        self.assertEqual(self.pcd("PcdPciMmio64Size"), PREFETCH_SIZE)
        self.assertEqual(self.pcd("PcdPciMmio64Translation"), 0)
        self.assertEqual(self.pcd("PcdPciExpressBaseAddress"), ECAM_BASE)
        self.assertEqual(self.pcd("PcdPciExpressBarSize"), ECAM_SIZE)
        self.assertEqual(self.pcd("PcdPciExpressBarLimit"),
                         ECAM_BASE + ECAM_SIZE - 1)

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
        # AppleSiliconPciHostBridgeLib computes the aperture field as
        # Base - (Base + PcdPciMmio32Translation), which is -Pcd.
        base = self.pcd("PcdPciMmio32Base")
        pcd = self.pcd("PcdPciMmio32Translation")
        translation = (base - ((base + pcd) & MASK64)) & MASK64

        # ...and PciHostBridgeDxe then does dev - translation for a host address
        # (TO_HOST_ADDRESS in MdeModulePkg/Bus/Pci/PciHostBridgeDxe/
        # PciHostBridge.h), so the field has to be bus - host.
        self.assertEqual(translation, (-HOST_OFFSET) & MASK64)
        self.assertEqual((base - translation) & MASK64, HOST_BASE)
        self.assertEqual((HOST_BASE + translation) & MASK64, base)

        # The same formula is what turns the captured device BARs into the host
        # addresses `ioreg` reported for lan0/wlan/bluetooth.
        for device, host in ((0x80000000, 0xB80000000),
                             (0x82000000, 0xB82000000),
                             (0x83000000, 0xB83000000)):
            self.assertEqual((device - translation) & MASK64, host)


if __name__ == "__main__":
    unittest.main()
