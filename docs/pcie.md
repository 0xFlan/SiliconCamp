# PCIe and storage on J624

The M4 package inherited the T602X PCIe PCDs, which point the host bridge at
`0x580000000` — an address that does not exist on this SoC — and the memory map
had no entry for the real ECAM or device windows. Both are now set from this
machine's device tree.

## Captured from this machine (2026-10-26)

`ioreg -p IODeviceTree -r -n apcie@B0000000 -l -w0`. Cell values are 32-bit
little-endian, and 64-bit quantities are stored as (low, high) cell pairs.

| Item | Value | Where |
|---|---|---|
| APCIE ECAM | `0x1cb0000000`, 256 MB | `reg[0]` |
| Bus range | 0–8 | `bus-range` |
| 32-bit non-prefetchable | bus `0x80000000` → CPU `0xb8000000`, 1 GB | `ranges[1]` |
| Prefetchable | `0xbc0000000` → `0xbc0000000`, 512 MB | `ranges[0]` |
| MSI doorbell | `msi-address` `0xfffff000`, `msi-vector-offset` 1506 | `msi-*` |
| Root-complex IRQs | 1231, 1240, 1249 | `interrupts` |
| ANS mailbox | `0x81600000`, IRQs 1003–1006/1018 | `ans@81600000` |
| DART (`apcie0`) | `0x90000000` | `dart-apcie0@90000000` |

The important correction: the 32-bit aperture base is the **bus** address
`0x80000000`, and the host window is `0xb8000000`. `AppleSiliconPciHostBridgeLib`
computes `Translation = -PcdPciMmio32Translation`, so the PCD carries
`bus - host = -0x38000000` (`0xFFFFFFFFC8000000`). An earlier version of this
change used `0xb8000000` with zero translation, which would have handed devices
BARs that nothing decodes.

The ECAM and both CPU windows are mapped in
`T8132FamilyVirtualMemoryMapDefines.h`, and `MAX_VIRTUAL_MEMORY_MAP_DESCRIPTORS`
was raised to fit them. A wrong ECAM is not a "PCIe does not work" bug — it is a
translation fault on the first config access.

## What is still missing

1. **`PCI0` in the DSDT.** Still absent on purpose: `PcdPciExpressBarSize` and
   `PcdPciExpressBarLimit` (used by the `_CRS`) still come from
   `AppleSiliconPkg.dsc.inc` and need the same capture before Windows is told a
   root bridge exists.
2. **DART enablement.** The `apcie` DARTs have to be mapped and either bypassed
   or programmed before devices can DMA. `AppleDartIoMmuDxe` exists in the tree
   but has not run on this machine.
3. **MSI wiring.** `AppleSiliconPciPlatformDxe` needs the doorbell and vector
   base wired to the AIC before wired interrupts work for non-MSI devices.

## Storage

Two different problems, often conflated:

- **UEFI storage.** The in-box NVMe driver cannot talk to the internal SSD: it
  is an Apple ANS controller, not a PCIe NVMe device. `AppleNANDStorageDxe`
  exists in the tree but is a **22-line stub** — a readme and an empty file —
  and is not included in any DSC. The firmware therefore has no driver for the
  internal disk, and the first install must target USB storage.
- **Windows storage.** A Windows driver for ANS2 has to implement the ANS
  mailbox, SART, and Apple's NVMe quirks (m1n1's `nvme.c` does this for the
  bootloader). That is a kernel-mode driver project, not a firmware change, and
  it needs the machine.

Until both exist, USB is the install path, and `docs/status.md` keeps saying so.
