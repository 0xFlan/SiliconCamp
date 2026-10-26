# PCIe and storage on J624

The M4 platform package used to inherit the T602X PCIe PCDs, which point the
host bridge at `0x580000000` — an address that does not exist on this SoC. That
is now replaced with the J624 values that are actually known, and the memory
map covers them.

## What is wired now

| Item | Value | Source |
|---|---|---|
| APCIE ECAM | `0x1cb0000000`, 256 MB | this machine's `ioreg` |
| Bus range | 0–8 | `ioreg` (`apcie` `bus-range`) |
| 32-bit MMIO window | `0xb8000000`, 1 GB, translation 0 | contains the WLAN BAR at `0xb8200000` and the Bluetooth BAR at `0xb8301000` |
| Prefetchable window | `0xbc0000000`, 512 MB | Apple's M4-class layout; **unconfirmed on J624** |
| MMIO32/64 translation | 0 | the BARs `ioreg` reports are already CPU addresses |

`T8132FamilyVirtualMemoryMapDefines.h` maps the ECAM and both windows as device
memory, and `MAX_VIRTUAL_MEMORY_MAP_DESCRIPTORS` was raised to fit them. Without
that mapping, the first PCIe config access takes a translation fault, so a wrong
ECAM is not a "PCIe does not work" bug — it is a firmware hang.

`PcdPciExpressBarSize`/`PcdPciExpressBarLimit` (used by the DSDT `_CRS`) still
come from `AppleSiliconPkg.dsc.inc`; they need the same J624 capture before the
root bridge is described to Windows.

## What is still unknown

1. **Exact window bounds.** The 32-bit base is inferred from where the Wi-Fi and
   Bluetooth BARs landed, not from a captured `ranges` property. Capture them:

   ```bash
   ioreg -p IODeviceTree -r -n apcie -l -w0 | grep -E '"ranges"|"assigned-addresses"|"bus-range"'
   ioreg -p IODeviceTree -l -w0 | grep -E '\+-o (apcie|apciec|pci-bridge)'
   ```

   Record the result in `docs/hardware-values.md` and, if the bounds differ,
   update the two defines and the PCDs together — the checker cannot catch a
   mismatch between them yet.

2. **MSI.** The DT-shaped description of Apple PCIe uses an MSI range on the AIC
   (`AIC_IRQ 2071`, 32 vectors on M4 Max). J624's MSI base and count are not
   captured, and `AppleSiliconPciPlatformDxe` needs them for wired interrupts.

3. **DARTs.** Each `apcie` port has its own DART. They must be enabled and
   bypassed (or programmed) before devices can DMA; the UEFI tree has
   `AppleDartIoMmuDxe`, but nothing here validates it on J624.

4. **`PCI0` in the DSDT.** Deliberately still absent. Windows will not see a
   root bridge until the `_CRS` describes windows that are known to be correct.

## Storage

Two different problems, often conflated:

- **UEFI storage.** The in-box NVMe driver cannot talk to the internal SSD: it
  is an Apple ANS controller, not a PCIe NVMe device. `AppleNANDStorageDxe`
  exists in the tree but is a **22-line stub** — a readme and an empty file —
  and is not included in any DSC. So the firmware has no driver for the internal
  disk, and the first install must target USB storage.
- **Windows storage.** A Windows driver for ANS2 has to implement the ANS
  mailbox, SART, and Apple's NVMe quirks (see m1n1's `nvme.c`, which does this
  for the bootloader). That is a kernel-mode driver project, not a firmware
  change, and it needs the machine.

Until both exist, USB is the install path, and `docs/status.md` keeps saying so.
