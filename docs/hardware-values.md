# M4 hardware values used by the firmware

This file records every M4-specific value that the build depends on, where it came
from, and how to re-check it. PCIe is wired into the host bridge and page tables
(`docs/pcie.md`); ANS2 storage has no driver yet.

Sources:

- **Asahi `t8132.dtsi`** — the public Linux device tree for the M4 SoC
  (`arch/arm64/boot/dts/apple/t8132.dtsi` in `AsahiLinux/linux`, `asahi` branch).
- **J624 `ioreg`** — this Mac's live device tree (`Mac16,3`, target type `J624`).
  Serial numbers and UUIDs are deliberately not reproduced here.

## Machine

| Item | Value | Source |
|---|---|---|
| Model | `Mac16,3` iMac (24-inch, M4, 2024) | `system_profiler SPHardwareDataType` |
| Target type | `J624` | `ioreg -p IODeviceTree -n device-tree` |
| SoC | `T8132` / Donan, chip ID `0x8132` | `chosen/chip-id`; DTS `compatible` |
| CPU | 10 cores: 6 E + 4 P | DTS `cpus`; ioreg |
| DRAM | 24 GB at the 1 TB boot alias | `chosen/dram-base`, `chosen/dram-size` |

`ioreg` prints `chosen/dram-base` as little-endian bytes. The value
`<0000000000010000>` is `0x1_0000_0000` (1 TB), not `0x10000`. This is the
important M4 detail: the firmware and the ADT copy live in the high DRAM alias,
not at the low `0x8_0000_0000` alias used by Linux.

## CPUs

| Item | Value |
|---|---|
| E-core MPIDRs | `0x000`–`0x005` |
| P-core MPIDRs | `0x10100`–`0x10103` |
| E-core cluster | 6 cores, ACPI IDs 0–5 |
| P-core cluster | 4 cores, ACPI IDs 6–9 |

The DTS nodes are `cpu@0`–`cpu@5` (`apple,donan-e`, `reg = <0 0>`–`<0 5>`) and
`cpu@10100`–`cpu@10103` (`apple,donan-p`). The `cpu-map` has `cluster0` with six
cores and `cluster1` with four. `MADT_Static.aslc`, `PPTT.aslc`, and `DSDT.asl`
must all stay in this order.

## Memory and firmware placement

| Item | Value |
|---|---|
| DRAM alias base | `0x10000000000` (1 TB) |
| DRAM size | `0x600000000` (24 GB) |
| Firmware FD base (FDF) | `0x10030000000` (1 TB + 3 GB) |
| Boot args copy | `0x10220000000` (1 TB + 8.5 GB) |
| ADT copy | `0x10220004000` (1 TB + 8.5 GB + 16 KiB) |
| SEC stack hint | `0x10300000000` (1 TB + 12 GB) |
| UEFI memory region | top 128 MB, computed by PrePi |

`PrePi`'s `DiscoverDramFromDt` patches `PcdSystemMemoryBase` and
`PcdSystemMemorySize` from the Apple `boot_args`; the DSC values are fallbacks.
The boot args and ADT copies are inside the 24 GB window, so they survive the
switch from m1n1's page tables to UEFI's identity map.

## Core devices

| Device | Address | Source |
|---|---|---|
| AICv3 | `0x381180000` | DTS `interrupt-controller@381180000` |
| PMGR | `0x380700000` | DTS `power-management@380700000` |
| UART (`serial0`) | `0x3ad200000`, IRQ 1046 | DTS `serial@3ad200000` |
| Watchdog | `0x3882b0000` | DTS `watchdog@3882b0000` |
| GPIO (`pinctrl_ap`) | `0x39a000000` | DTS `pinctrl@39a000000` |
| vGIC distributor | `0x5000000000` | NTASP vGIC 42-bit window |
| vGIC redistributors | `0x5100000000`, 10 × `0x20000` | NTASP vGIC; MADT GICR |

The current M4 virtual memory map also maps the low 1 GB (`0x0`–`0x40000000`)
because the M4 USB controllers and their DARTs are below 1 GB. That window is
not present in the T602X map.

## USB

| Item | Value |
|---|---|
| DWC3 controllers | `usb-drd0@0x2280000`, `usb-drd1@0xa280000`, `usb-drd2@0x12280000`, `usb-drd3@0x1a280000` |
| USB DARTs | `dart-usb0@0x2f00000`, `dart-usb1@0xaf00000`, `dart-usb2@0x12f00000`, `dart-usb3@0x1af00000` |
| Host controllers used by NTASP | DWC3 1 and 3; the driver skips 0 and 2 as DFU controllers |

`PcdAppleNumDwc3Controllers` is 4 and `PcdAppleNumDwc3Darts` is 8 in
`Platform/Mac16_3Pkg/Mac16_3.dsc`. The low 1 GB mapping in
`T8132FamilyVirtualMemoryMapDefines.h` covers both the DWC3 register blocks and
the USB DARTs. The USB stack has not been run on hardware yet.

## PCIe (host bridge wired; DSDT still off)

The J624 has several APCIE root complexes. `ioreg` reports the first `apcie`
node's ECAM as:

| Item | Value |
|---|---|
| APCIE ECAM | `0x1cb0000000`, 256 MB |
| `apcie` bus range | 0–8 |
| PCIe 32-bit window | bus `0x80000000` → host `0xb80000000`, 1 GB |
| PCIe host offset | `0xb00000000` (`PcdPciMmio32Translation` = `-0xb00000000`) |
| PCIe prefetch window | `0xbc0000000` → `0xbc0000000`, 512 MB |
| PCIe MSI doorbell | `0xfffff000`, vector offset 1506 |
| ANS mailbox | `0x81600000` |
| `dart-apcie0` / `dart-apcie2` | `0x90000000` / `0x92000000` |
| Ethernet | Broadcom `14e4:1682` (`lan0`), BARs `0xb80000000`/`0xb80010000` |
| WLAN | Broadcom `14e4:4434`, BARs `0xb82000000`/`0xb83000000` |
| Bluetooth | Broadcom `14e4:5f72`, BARs `0xb81000000`/`0xb83010000` |
| USB4/Thunderbolt root complexes | `apciec0`–`apciec3`; ECAMs from `0x1c50000000` (`apciec0`/`apciec1` decoded) |

The UEFI host bridge and memory map now use these captured values instead of the
T602X ones (`PcdPciExpressBaseAddress` was `0x580000000`). The 32-bit aperture is
the bus window; the host offset is carried in `PcdPciMmio32Translation`, and the
page tables map the host addresses (`0xb80000000`+1 GB). PCIe is still omitted
from the DSDT: no `_CRS` is exposed to Windows until the APCIE DARTs are enabled
and the ECAM/windows have been exercised on hardware. The four `apciec` root
complexes are a separate matter: the single host-bridge descriptor does not
describe them.

The internal SSD is an **Apple ANS2** controller, not a standard PCIe NVMe device.
`AppleNANDStorageDxe` is a 22-line stub and no DSC includes it, so there is no
ANS2 driver yet; USB storage is the intended installer path for the first
Windows attempt.

## Re-checking on this machine

```bash
# model, target type, DRAM alias/size
sysctl -n hw.model hw.memsize
ioreg -p IODeviceTree -r -n device-tree -l -w0 | grep -E 'target-type|model|compatible'
ioreg -p IODeviceTree -r -n chosen -l -w0 | grep -E 'dram-base|dram-size|chip-id'

# USB and PCIe topology (large output; filter it)
ioreg -p IODeviceTree -l -w0 | grep -E '\+-o (usb-drd|dart-usb|apcie|pci-bridge|pcic)'

# PCIe root complex properties. -d 0 is required: without it the same property
# names come back from child nodes and the last one wins.
ioreg -p IODeviceTree -r -n "apcie@B0000000" -d 0 -l -w0
```

The public DTS can be fetched without cloning the whole kernel:

```bash
curl -L -H 'Accept: application/vnd.github.raw' \
  'https://api.github.com/repos/AsahiLinux/linux/contents/arch/arm64/boot/dts/apple/t8132.dtsi?ref=asahi'
```

Do not copy the machine's serial number, UUID, or provisioning UDID into this
repository.
