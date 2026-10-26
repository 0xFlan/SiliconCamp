# M4 platform package (NTASP UEFI)

NTASP originally had no M4 platform. The package is now implemented and builds;
this page records what exists, what was verified statically, and what still needs
hardware validation.

## What exists

- `Silicon/Apple/T8132FamilyPkg` — T8132 PCDs, 10-core MADT, 6E+4P PPTT,
  M4 virtual memory map, `MemoryInitPeiLib`, `MsPlatformDevicesLib`.
- `Platform/Mac16_3Pkg` — `Mac16,3`/J624 DSC/FDF, DSDT, FADT, DBG2, device ACPI
  tables, SMBIOS strings, vGIC build.

The firmware builds successfully:

```
third_party/ntasp-firmware/Build/Mac16_3-AARCH64/DEBUG_CLANGPDB/FV/MAC16_3_EFI.fd
```

The build is reproduced in the Lima VM by `./scripts/build-uefi-linux.sh Mac16_3`.

## What was verified statically

The generated tables were disassembled and checked against the J624 ioreg and
Asahi's `t8132.dtsi`. `scripts/check-acpi.py` now enforces the same values on
every build:

- **MADT** — 10 GICC entries, UIDs 0–9; six E-cores at MPIDRs `0x0`–`0x5`, four
  P-cores at `0x10100`–`0x10103`; efficiency class 0/1. GICD `0x5000000000`;
  GICR `0x5100000000`, length `0x140000` (10 × `0x20000`).
- **PPTT** — one package, a 6-core E-cluster, a 4-core P-cluster, and ten cores;
  ACPI IDs 0–9. Parent offsets point at the package and cluster nodes.
- **DSDT** — `DIE0` with `CLU0` (CPU0–CPU5) and `CLU1` (CPU6–CPU9); every processor
  device has `_UID` 0–9. `COM0` is `APPL8900` at `0x3ad200000`, interrupt 1046.
- **FADT** — desktop profile, hardware-reduced ACPI, PSCI compliant, SMC conduit.
- **DBG2** — `_SB.COM0` at `PcdAppleUartBase`.

The DRAM alias is 1 TB, the size is 24 GB, the FD is based at 1 TB + 3 GB, and the
boot args/ADT copies are at 1 TB + 8.5 GB. Those values are documented with their
sources in `docs/hardware-values.md`.

The ARM64 Image header at the start of the FD now declares `image_size`
`0x1E00000`, the FD size in the FDF, instead of upstream's `0x100E0000`; the
checker rejects a header that does not cover the payload.

## Target

- Device: `Mac16,3` (M4 iMac, `J624`, 10 cores: 4P + 6E, 24 GB).
- SoC: `T8132` ("Donan").
- Guest PA size: 42-bit or larger; the vGIC chooses the 42-bit window when
  `vaddr_bits > 36`.
- vGIC: distributor `0x5000000000`, redistributors `0x5100000000`, ITS
  `0x5200000000`.
- ACPI CPU count: 10.

## Build configuration

```
AIC_BUILD = FALSE      # the guest uses the m1n1 vGIC
SECURE_BOOT_ENABLE = FALSE
```

`T8132FamilyPkg.dsc.inc` sets:

```
PcdSystemMemoryBase = 0x10000000000
PcdSystemMemorySize = 0x600000000
PcdCoreCount        = 10
PcdClusterCount     = 2
PcdAppleUartBase    = 0x3ad200000
PcdGicDistributorBase   = 0x5000000000
PcdGicRedistributorsBase = 0x5100000000
```

`PrePi` patches the memory base/size from the Apple boot args at runtime, so the
DSC values are fallbacks, not hard limits.

## USB and storage

The M4 has four DWC3 controllers and four USB DARTs. `PcdAppleNumDwc3Controllers`
is 4; `AppleUsbTypeCBringupDxe` skips the two DFU controllers (0 and 2) and brings
up the host controllers (1 and 3). The low 1 GB MMIO window (which contains both
the DWC3 blocks and the USB DARTs) is mapped as device memory in
`BuildVirtualMemoryMap`.

The internal SSD is an Apple ANS2 device, not a standard PCIe NVMe controller.
There is no ANS2 driver in the tree, so the first Windows installer attempt must
use a USB disk. PCIe is not yet described to the guest.

## PCIe

The J624 APCIE ECAM is at `0x1cb0000000` (256 MB), with WLAN, Bluetooth, Ethernet,
and four `apciec` USB4/Thunderbolt root complexes behind it. The T602X PCDs that
pointed the host bridge at `0x580000000` are now replaced with the J624 values
(ECAM, bus range 0–8, `0xb8000000`/1 GB non-prefetchable, `0xbc0000000`/512 MB
prefetchable), and the ECAM plus both windows are mapped in
`T8132FamilyVirtualMemoryMapDefines.h`. See `docs/pcie.md` for sources and for
what is still uncaptured (exact window bounds, MSI base, DARTs).

PCIe is still omitted from the DSDT, so Windows sees no root bridge yet.
Finishing it requires:

1. capturing the J624 `apcie` `ranges`/`assigned-addresses` and correcting the
   two window defines if they differ from the inferred bounds;
2. enabling `AppleSiliconPciPlatformDxe` (or replacing the generic host bridge);
3. adding the ECAM and PCIe MMIO ranges to the virtual memory map with nGnRE;
4. validating that the DARTs and MSI path work for each root complex;
5. only then adding a `PCI0` device and `_CRS` to the DSDT.

## AICv3

`AIC_BUILD=FALSE` means UEFI never touches the physical AIC; m1n1 owns it. If the
hardware AIC path is ever used directly:

- `AppleAicDxe`/`AppleAicLib` need AICv3 support. The current source says:
  "TODO: Add AICv3 support, seen in T8122 and T8130 SoCs."
- Use the Asahi m1n1 `aic.c` and the Linux `apple-aic` driver as references.
- Validate IPI delivery, timer FIQ routing, and IRQ affinity on the M4.

Even with the vGIC path, m1n1 still needs a working physical AICv3 driver. The
current m1n1 `aic.c` supports the AIC3 register layout; hardware validation is
still required.

## Remaining unknowns

- Whether the low 1 GB mapping is sufficient for every USB/DART path on J624.
- Exact PCIe MMIO windows for each `apcie`/`apciec` root complex and the MSI
  controller.
- ANS2/SART storage protocol for the internal SSD.
- DCP/display handoff beyond the iBoot framebuffer.
- M4 SMC/SEP behavior under the m1n1 hypervisor.

Each unknown should become a small probe in m1n1 or the UEFI firmware, with the
output recorded in `docs/probes/` after redacting serial numbers and UUIDs.
