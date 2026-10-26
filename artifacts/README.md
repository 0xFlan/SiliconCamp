# Build artifacts

These are the outputs of the 2026-10-25 build on the `Mac16,3` / `J624` M4 iMac.
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
5a0d258a2782bb5db602547eed9dcca5c9f2be998c7659f9719ac26815705fce  MAC16_3_EFI.fd
78459bf90fe5dbbb072fb82040ff95b3612bbe59b9075700a480cd3aaa7afcdb  m1n1-windows.bin
c518d05af33ed274ab4709daa084253ef947e1293aad37e08b38c97d202aac83  m1n1-windows.macho
```

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

The final FD stores the ACPI tables compressed inside the Project Mu FV; the
values above were checked from the build-tree `.aml`/`.acpi` files and the
disassembled DSDT, and `scripts/check-acpi.py` re-checks them on every build.
The FD's ARM64 Image header declares `image_size` `0x1E00000`, matching the FDF;
upstream's value (`0x100E0000`) was larger than the whole image.

## What is not in this image

- No PCIe host bridge configuration for the J624 APCIE ECAM (`0x1cb0000000`).
- No ANS2/SART driver for the internal SSD.
- No Windows drivers repository content; `third_party/ntasp-drivers` is still a
  skeleton.
- No Secure Boot, TPM, or anti-cheat path.

## Rebuild

```bash
./scripts/build-m1n1.sh
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
