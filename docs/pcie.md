# PCIe and storage on J624

The M4 package inherited the T602X PCIe PCDs, which pointed the host bridge at
`0x580000000` — an address that does not exist on this SoC — and the memory map
had no entry for the real ECAM or device windows. Both are now set from this
machine's device tree.

An earlier revision of this file (and of the two commits before this one) used
`0xb8000000` for the 32-bit window: the right structure, but one hex digit
short. The window is `0xb80000000`, and the host offset is `0xb00000000`, not
`0x38000000`. The raw cells below are here so the next person can check the
arithmetic instead of trusting a decimal-free summary.

## How the values were read

```bash
ioreg -p IODeviceTree -r -n "apcie@B0000000" -d 0 -l -w0
```

`-d 0` matters. Without it, `ioreg` also prints every child node, and a naive
"last property with this name wins" parse returns `lan0`'s `ranges`/`reg`
instead of the root complex's — which is how the first decode went wrong.
Property data is a byte string; each 32-bit cell is little-endian, and a 64-bit
quantity is a (low, high) cell pair.

`ranges` is a list of 7-cell entries:

| Cell | Meaning |
|---|---|
| 0 | PCI address flags (`0x43000000` = 64-bit prefetchable, `0x02000000` = 64-bit non-prefetchable) |
| 1, 2 | child (bus) address, low then high |
| 3, 4 | parent (host) address, low then high |
| 5, 6 | size, low then high |

## Captured from this machine (2026-10-26)

| Item | Value | Where |
|---|---|---|
| APCIE ECAM | `0x1cb0000000`, 256 MB | `reg[0]` |
| Bus range | 0–8 | `bus-range` |
| 32-bit non-prefetchable | bus `0x80000000` → host `0xb80000000`, 1 GB | `ranges[1]` |
| Prefetchable | `0xbc0000000` → `0xbc0000000`, 512 MB | `ranges[0]` |
| MSI doorbell | `0xfffff000`, vector offset 1506 | `msi-address`, `msi-vector-offset` |
| Root-complex IRQs | 1231, 1240, 1249 | `interrupts` |
| Ethernet BARs | `0xb80000000`, `0xb80010000` (64 KB each) | `lan0` `IODeviceMemory` |
| WLAN BARs | `0xb82000000` (16 MB), `0xb83000000` (64 KB) | `wlan` `IODeviceMemory` |
| Bluetooth BARs | `0xb81000000` (16 MB), `0xb83010000` (32 KB) | `bluetooth-pcie` `IODeviceMemory` |
| APCIE0 DART | `0x90000000` | `dart-apcie0@90000000` |
| APCIE2 DART | `0x92000000` | `dart-apcie2@92000000` |
| ANS mailbox | `0x81600000`, IRQs 1004/1003/1006/1005/1018 | `ans@81600000` |

The device BARs are the proof that the host address is right: every one of them
falls inside `0xb80000000`+1 GB and inside the bus window after subtracting
`0xb00000000`. With the old `0xb8000000` value none of them did.

## The raw cells

`apcie@B0000000` `ranges` (14 cells, two 7-cell entries):

```
43000000 c0000000 0000000b c0000000 0000000b 20000000 00000000
02000000 80000000 00000000 80000000 0000000b 40000000 00000000
```

Decoded:

- entry 0 — flags `0x43000000`, child `0x0b_c0000000` = `0xbc0000000`,
  parent `0xbc0000000` (no translation), size `0x20000000` (512 MB);
- entry 1 — flags `0x02000000`, child `0x80000000` (bus), parent
  `0x0b_80000000` = `0xb80000000` (host), size `0x40000000` (1 GB).

`reg[0]` is `1cb0000000` + 256 MB. `msi-address` is `fffff000`,
`msi-vector-offset` is `000005e2` (1506), and `interrupts` is
`000004cf 000004d8 000004e1`.

## What this means for the firmware

The 32-bit window exists in two coordinate systems at once, and three separate
sign conventions meet in the middle:

1. `AppleSiliconPciHostBridgeLib` fills in the root bridge descriptor from the
   PCDs and computes the aperture field as
   `PcdPciMmio32Base - (PcdPciMmio32Base + PcdPciMmio32Translation)`, i.e.
   `-PcdPciMmio32Translation`.
2. `MdeModulePkg/Bus/Pci/PciHostBridgeDxe/PciHostBridge.h` defines that field as
   `Translation = Device Address - Host Address` and converts with
   `TO_HOST_ADDRESS(dev, tra) = dev - tra`.
3. ACPI's `_TRA` in the DSDT `_CRS` is the other way round:
   `DynamicTablesPkg`'s `SsdtPcieGenerator` emits `_TRA = CpuAddress -
   PciAddress`, which is `host - bus` — the value this file's `_CRS` uses.

So the PCD carries **host - bus**:

```
PcdPciMmio32Base        = 0x80000000          /* bus address */
PcdPciMmio32Size        = 0x40000000
PcdPciMmio32Translation = 0xB00000000         /* host - bus */
```

which the library turns into an aperture translation of `-0xb00000000`, and
`TO_HOST_ADDRESS(0x80000000, -0xb00000000)` then returns `0xb80000000`. The
T602X platform uses the same convention (`bus 0xc0000000`, `host 0x5c0000000`,
`PCD 0x500000000`).

The page tables must map the **host** addresses (`0xb80000000`+1 GB), while the
PCI core does the bus↔host conversion.

This sign was wrong in the first revision of the platform: the PCD carried
`bus - host` (`0xFFFFFFF500000000`), which builds, boots, and enumerates, and
then places every BAR at a host address that no device decodes. The fix is in
`T8132FamilyPkg.dsc.inc`, and `scripts/check-acpi.py` now fails the build if the
PCD, the DSDT `_CRS` and the captured values disagree — including the sign.

`T8132FamilyVirtualMemoryMapDefines.h` maps three regions for this root complex:
ECAM `0x1cb0000000`+256 MB, the 32-bit window `0xb80000000`+1 GB, and the
prefetchable window `0xbc0000000`+512 MB. `MAX_VIRTUAL_MEMORY_MAP_DESCRIPTORS`
is 48 and the table currently uses 45 entries including the terminator. Most of
those entries are inherited from the T602X package and cover no J624 device;
they are left in place until a pruned map can be validated on hardware.

Only the first root complex (`apcie@B0000000`) is described by the host bridge.
The iMac has four more: `apciec0`–`apciec3` at `0x50000000`, `0x60000000`,
`0x70000000` and `0x80000000`. Each has its own ECAM (`0x1c50000000` and
`0x1c60000000` were decoded from `apciec0`/`apciec1`) and its own apertures; a
single `PCI_ROOT_BRIDGE` cannot represent them.

## MCFG and PCI0 (2026-10-27)

`DeviceAcpiTables.inf` now emits `MCFG.aslc`, and `DSDT.asl` describes `PCI0`.
Both are pinned by `scripts/check-acpi.py`:

- **MCFG**: ECAM `0x1cb0000000`, segment 0, buses 0–8.
- **PCI0**: `_HID` `PNP0A08`, `_CID` `PNP0A03`, `_SEG`/`_BBN` 0, `_CCA` 1,
  and `_CBA` returning `0x1cb0000000`.
- **`_CRS`**: bus 0–8; the 32-bit window is bus `0x80000000`–`0xbfffffff`
  with translation `0xb00000000` (host `0xb80000000`–`0xbbfffffff`); the
  prefetchable window is `0xbc0000000`–`0xbdfffffff`.
- **`RES0`**: reserves the 256 MB ECAM, `0x1cb0000000`–`0x1cbfffffff`.

The `_CRS` translation and `PcdPciMmio32Translation` are **both host - bus**
(`+0xb00000000`) — the ACPI descriptor and the PCD use the same value, and the
host bridge library negates it on the way into the root-bridge descriptor. The
checker pins the arithmetic in both directions so a future edit cannot silently
flip it.

## Guest hand-off: what m1n1 now does (2026-10-27, patch 0003)

Between the tables and a working device there is one entity: the guest's stage 2.
An EL1 guest started by m1n1 can only touch IPAs that m1n1 has mapped, and a
fault on an unmapped one is fatal — `hv_handle_dabort()` prints `HV: Unmapped
IPA` and returns an unhandled exception. Before patch 0003 nothing mapped the
controllers, so the first PCIe config read would have killed the guest; and
nothing had touched the DARTs, so the first DMA would have faulted in the DART.

`src/pcie_guest.c` (patch 0003) walks the ADT at `hv_init()` time and does three
things:

1. reports every APCIE controller it finds — path, ECAM base and size, `#ports`,
   and the MSI parameters (`msi-address`, `msi-vector-offset`, MSI vector count)
   — so the first hardware boot records the topology instead of requiring another
   capture session;
2. maps the ECAM and the two BAR windows into the guest's stage 2 (skipping
   anything already mapped, and refusing addresses outside the guest's PA size);
3. puts each APCIE DART into bypass: every stream enabled, every stream's TCR
   written with `BYPASS_DART | BYPASS_DAPF`, the TLB flushed, and every stream
   read back to prove it.

Bypass is the right state because the guest runs with an identity stage 2
(IPA == PA): a device DMA address is a physical address, and bypass makes the
DART pass it through unchanged. The guest has no DART driver, so nothing else
would arrange that. A DART that is locked or that ignores the write is reported
and makes the hand-off return an error, rather than leaving a device that
silently cannot DMA.

The one thing the ADT does not describe is the host side of the BAR windows, so
those are the captured values above, keyed by SoC; a SoC without an entry gets
its ECAM mapped and a warning naming the missing values.

## What is still missing

1. **MSI.** Devices behind `PCI0` can enumerate and DMA, but their MSI/MSI-X
   messages have nowhere useful to land yet. Two mechanisms exist and neither is
   wired to the guest:

   - Apple's own doorbell: a device writes `msi-vector-offset + vector` to
     `msi-address` (`0xfffff000` on this machine, offset 1506/`0x5e2`), the
     controller turns that into an AIC interrupt, and m1n1's vGIC forwards AIC
     interrupts to the guest one-for-one. This works from the hardware's point
     of view today — it is only Windows that has no way to be told about it.
   - A guest-visible GIC ITS. m1n1's vGIC maps an ITS window
     (`0x5200000000` on a 40-bit guest) and `hv_vgic.c` handles `GITS_CTLR` and
     `GITS_BASER*`, but the command queue is not decoded, so an OS that
     discovers an ITS through IORT would program it into a black hole.

   The shortest path to MSI for Windows is the first mechanism plus a small
   kernel driver (or an ACPI-described mailbox, which no ACPI binding covers
   yet); the second is a vITS project that has to trap the command queue by
   mapping it read-only in stage 2.
2. **The `apciec` root complexes.** Their ECAMs, bus ranges (0–128) and
   apertures need the same capture-then-wire treatment before Thunderbolt or
   USB4 devices can appear. `PCI0` describes only `apcie@B0000000`; the m1n1
   hand-off reports all of them, so the next capture session can fill in the
   rest. Their DARTs are already bypassed (`dart-apciec0`-`dart-apciec3`), so
   DMA is not the blocker there — the tables are.

## Storage

Two different problems, often conflated:

- **UEFI storage.** The in-box NVMe driver cannot talk to the internal SSD: it
  is an Apple ANS controller, not a PCIe NVMe device. `AppleNANDStorageDxe`
  exists in the tree but is a **22-line stub** with no DSC entry, so the
  firmware has no driver for the internal disk and the first install must
  target USB storage.
- **Windows storage.** A Windows driver for ANS2 has to implement the ANS
  mailbox, SART, and Apple's NVMe quirks (m1n1's `nvme.c` does this for the
  bootloader). That is a kernel-mode driver project, not a firmware change, and
  it needs the machine.

Until both exist, USB is the install path, and `docs/status.md` keeps saying so.
