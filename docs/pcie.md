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

`AppleSiliconPciHostBridgeLib` takes the aperture base in **bus** address space
and computes

```
Translation = PcdPciMmio32Base - (PcdPciMmio32Base + PcdPciMmio32Translation)
```

so the PCD must carry `bus - host`:

```
PcdPciMmio32Base        = 0x80000000
PcdPciMmio32Size        = 0x40000000
PcdPciMmio32Translation = 0xFFFFFFF500000000   /* -0xb00000000 */
```

That yields a host aperture of `0xb80000000`–`0xbbfffffff`. The page tables must
therefore map the **host** addresses (`0xb80000000`+1 GB), while the PCI core
does the bus↔host conversion. Mapping the bus address, or dropping a digit,
leaves every BAR access decoding nowhere.

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

## What is still missing

1. **`PCI0` in the DSDT.** Still absent on purpose. `PcdPciExpressBarSize` and
   `PcdPciExpressBarLimit` are not defined anywhere for T8132 (only T600X and
   T810X define them), so a `_CRS` has to be written from the windows captured
   above and checked against them, not copied from another package.
2. **DART enablement.** `dart-apcie0` (`0x90000000`) and `dart-apcie2`
   (`0x92000000`) have to be mapped and either bypassed or programmed before
   devices can DMA. `AppleDartIoMmuDxe` exists in the tree but has not run on
   this machine.
3. **The `apciec` root complexes.** Their ECAMs, bus ranges (0–128) and
   apertures need the same capture-then-wire treatment before Thunderbolt or
   USB4 devices can appear.
4. **MSI wiring.** In the vGIC build the guest's MSI path is the emulated GIC
   ITS at `0x5200000000`, so Apple's `0xfffff000` doorbell is not used. It only
   matters for a bare-metal `AIC_BUILD=TRUE` build, and then the doorbell range
   must be mapped and programmed into the AIC.

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
