# Build artifacts

These are the outputs of the 2026-10-26 build on the `Mac16,3` / `J624` M4 iMac.
They replace the v0.3.1 m1n1 images: the hypervisor now carries the M4 PSCI
topology patch (`patches/m1n1-windows/0002-psci-t8132.patch`), without which
`hv_psci_init()` had no `T8132` case and read an unset topology pointer. The
UEFI image is unchanged from v0.3.1 and still has the corrected PCIe windows
(the v0.3.0 images were one hex digit short: `0xb8000000` instead of
`0xb80000000`).
They are provided for review and for a hardware experiment that has **not** been
run yet. Read `../docs/safety.md` before installing anything on the machine.

The images themselves are attached to the GitHub release for the matching tag,
not committed to git (the FD is 30 MB and would be permanent in history).
Download them with `gh release download` or from the releases page; the
`SHA256SUMS` below identifies the exact images this repository was built and
checked against.

| File | Size | What it is |
|---|---|---|
| `MAC16_3_EFI.fd` | 30 MB | NTASP Project Mu UEFI firmware for the M4 iMac, `Mac16,3` |
| `m1n1-windows.bin` | 1.1 MB | raw m1n1-windows hypervisor image |
| `m1n1-windows.macho` | 896 KB | Mach-O form of the same m1n1-windows build |
| `SHA256SUMS` | — | SHA-256 of the three images above |

## SHA-256

```
8b966b02fb9418b449d21209625265c7dae1edec4f25c768193020ba510c7c00  MAC16_3_EFI.fd
82f5372f8aa8f1c88806be1e36b62797954d2cb5d65fe000586e2f028c3f0258  m1n1-windows.bin
cb13046385fb274188c10c00e38e800542669c526910a23f441181b39fd4ce38  m1n1-windows.macho
```

The m1n1 hashes were reproduced from a clean `m1n1-windows` checkout with
`patches/m1n1-windows/0001` and `0002` applied, so they identify
clean-checkout + patches, not a dirty tree.

## What the UEFI image contains

- `T8132FamilyPkg` with `PcdSystemMemoryBase = 0x10000000000`,
  `PcdSystemMemorySize = 0x600000000` (24 GB fallback; PrePi patches it from the
  Apple boot args), 10 cores, two clusters.
- MADT: 10 GICC entries; E-core MPIDRs `0x0`–`0x5`, P-core MPIDRs
  `0x10100`–`0x10103`; GICD `0x5000000000`; GICR `0x5100000000`, length
  `0x140000`.
- PPTT: one package, 6-core E-cluster, 4-core P-cluster, cores ACPI IDs 0–9.
- DSDT: `DIE0` with `CLU0` (CPU0–CPU5) and `CLU1` (CPU6–CPU9); `COM0` at
  `0x3ad200000`, interrupt 1046.
- FADT: desktop profile, hardware-reduced ACPI, PSCI compliant, SMC conduit.
- DBG2: `_SB.COM0` at `0x3ad200000`.
- The low 1 GB MMIO window is mapped for the four DWC3 controllers and their
  DARTs; the DSDT intentionally does not describe PCIe yet.
- PCIe: the first APCIE root complex (`apcie@B0000000`) is configured from the
  captured J624 values — ECAM `0x1cb0000000`+256 MB, bus range 0–8, host window
  `0xb80000000`+1 GB (offset `0xb00000000`), prefetch `0xbc0000000`+512 MB —
  and all three regions are mapped. `PCI0` is not in the DSDT.

The final FD stores the ACPI tables compressed inside the Project Mu FV; the
values above were checked from the build-tree `.aml`/`.acpi` files and the
disassembled DSDT, and `scripts/check-acpi.py` re-checks them on every build —
including each CPU's `_UID`, its containing cluster, and `COM0`'s `_HID`/`_CRS`.
The FD's ARM64 Image header declares `image_size` `0x1E00000`, matching the FDF;
upstream's value (`0x100E0000`) was larger than the whole image.

## What is not in this image

- No `PCI0` root bridge in the DSDT, no APCIE DART driver, and no description
  of the four `apciec0`–`apciec3` root complexes.
- No ANS2/SART driver for the internal SSD.
- No Windows drivers repository content; `third_party/ntasp-drivers` is still a
  skeleton.
- No Secure Boot, TPM, or anti-cheat path.

## Rebuild

```bash
./scripts/build-m1n1.sh   # applies patches/m1n1-windows/*.patch first
./scripts/build-uefi-linux.sh Mac16_3
```

Then copy the two outputs into this directory and regenerate `SHA256SUMS`:

```bash
cp third_party/m1n1-windows/build/m1n1.bin artifacts/m1n1-windows.bin
cp third_party/m1n1-windows/build/m1n1.macho artifacts/m1n1-windows.macho
cp third_party/ntasp-firmware/Build/Mac16_3-AARCH64/DEBUG_CLANGPDB/FV/MAC16_3_EFI.fd \
   artifacts/MAC16_3_EFI.fd
cd artifacts && shasum -a 256 MAC16_3_EFI.fd m1n1-windows.bin m1n1-windows.macho \
   > SHA256SUMS
```

## First hardware experiment

The first experiment is **m1n1 on the M4**, not Windows. It requires a verified
backup, a second Mac with Apple Configurator 2, and a data-capable USB-C cable for
DFU recovery. Do not run `bputil`, `kmutil configure-boot`, or any boot-security
change until `../docs/safety.md` is satisfied and the machine owner has approved
the risk.
