# M4Windows

**Goal:** native Windows on Apple M4 (`Mac16,3`, `T8132`), using the existing open-source Apple Silicon bring-up where it is sound, and original code for the M4-specific gaps.

This is a research and bring-up repository, not a working product. As of **2026-10-25**:

- `m1n1` upstream builds on this M4 host.
- NTASP's `m1n1-windows` fork builds with the vGIC enabled, and its tree already contains T8132 NVMe, PCIe, and SMP support.
- An original M4 vGIC topology fix is applied in `patches/m1n1-windows/0001-vgic-m4-topology.patch`.
- The NTASP Project Mu UEFI firmware now has an original M4 platform package and **builds successfully** in the Linux build VM. The generated image (`MAC16_3_EFI.fd`, 30 MB) is attached to the GitHub release with its SHA-256, not committed to git.
- The M4 values are derived from this exact `Mac16,3`/`J624` machine and Asahi's T8132 device tree: 6 E-cores + 4 P-cores, 24 GB DRAM at the 1 TB alias, UART `0x3ad200000` IRQ 1046, and four USB DWC3 controllers.
- The generated M4 ACPI tables were checked: 10 GICC entries with the correct MPIDRs, GICD at `0x5000000000`, GICR at `0x5100000000` with a `0x140000` range, a 2-cluster/6E+4P PPTT, and a matching 10-device DSDT. `scripts/check-acpi.py` enforces this on every build, so the values cannot drift silently.
- The NTASP Windows driver repository is still a README-only skeleton.
- No Windows kernel has been booted on this M4. The existing project still hangs at the Windows loader-to-kernel handoff even on M1/M2.
- There is no Windows GPU driver for Apple AGX, and no path to kernel anti-cheat yet.

## Boot chain

```
Apple iBoot (Apple-signed, unavoidable)
        |
        v
m1n1-windows  (EL2: hardware init, vGIC, PSCI, SMMU/vTPM, chainload)
        |
        v
NTASP Project Mu UEFI  (EL1: ACPI, boot services, Windows loader)
        |
        v
Windows bootmgr -> winload -> Windows kernel  (EL1 guest)
```

`m1n1` is the public chainload path; iBoot will not load an unsigned replacement. The practical project is therefore **m1n1-windows + NTASP UEFI + original M4 platform/driver work**, not a replacement for iBoot.

## Quick start

```bash
./scripts/check-host.sh
./scripts/verify-patches.sh   # patches still apply to the pinned submodules
./scripts/build-m1n1.sh
./scripts/build-uefi-linux.sh Mac16_3   # requires the Lima VM; see docs/build-uefi.md
```

`python3 -m unittest discover -s tests` runs the checker's regression tests
against the M4 tables an earlier build produced and against mutated copies of
them. It needs no hardware, no firmware build, and nothing beyond Python 3, and
CI runs it on every push.

The build outputs are:

```
third_party/m1n1-windows/build/m1n1.bin
third_party/m1n1-windows/build/m1n1.macho
third_party/ntasp-firmware/Build/Mac16_3-AARCH64/DEBUG_CLANGPDB/FV/MAC16_3_EFI.fd
```

`m1n1.bin` is the raw image used by the modern `kmutil configure-boot --raw` path. **Do not install it on the M4 yet.** Read `docs/safety.md` first.

## Layout

```
docs/                   architecture, status, bring-up plan, safety, M4 platform, hardware values
patches/m1n1-windows/   original patches against the NTASP m1n1 fork
patches/ntasp-firmware/ M4 UEFI platform and build patches
scripts/                host check, build, patch application, ACPI checker
tests/                  checker regression tests and real M4 ACPI fixtures
third_party/m1n1/            upstream Asahi m1n1 (reference)
third_party/m1n1-windows/    NTASP vGIC/PSCI Windows fork (build target)
third_party/ntasp-firmware/  Project Mu UEFI firmware
third_party/ntasp-drivers/   Windows drivers/HAL extensions (currently empty)
```

## What is already real

- `third_party/m1n1-windows` compiles with Homebrew LLVM 23, LLD 23, and Rust 1.99.1.
- The NTASP UEFI firmware compiles for both the M2 reference platform and the new M4 `Mac16_3` platform.
- The M4 ACPI tables describe one die, a 6-core E-cluster, a 4-core P-cluster, and CPU UIDs 0–9; the DSDT `COM0` is the M4 UART at `0x3ad200000`.
- The M4 virtual memory map includes the low 1 GB window used by `usb-drd0`–`usb-drd3` and `dart-usb0`–`dart-usb3`.
- The fork's `config.h` enables `ENABLE_VGIC_MODULE` and it has `hv_vgic.c`, `hv_psci.c`, `hv_smmu.c`, and `hv_vtpm.c`.
- Upstream m1n1 has T8132 (`M4 Donan`) CPU support, T8132 NVMe, T8132 PCIe, and M4 SPTM/GXF emulation work.
- The M4 patch replaces the old per-SoC vGIC switch with:
  - a 36-bit vs 42-bit MMIO window chosen from `vaddr_bits`, and
  - a redistributor count taken from the actual ADT CPU topology.
  This fixes the missing `T8132` case and the old fall-through bugs in the `T6000`/`T6020` cases.

## What is still missing

1. Hardware validation: the firmware builds and its CPU/RAM/UART/ACPI topology matches the machine, but m1n1 and the UEFI image have not run on the M4 yet.
2. PCIe and storage: the APCIE ECAM is known (`0x1cb0000000`), but the UEFI host bridge still uses the T602X PCDs; the internal ANS2 SSD has no driver. USB storage is the current install path.
3. A working Windows loader-to-kernel handoff (the central unsolved bug, even on M1/M2).
4. Windows drivers: ANS NVMe, DART, PCIe, DCP display, SPI input, SMC, USB, Wi-Fi/Bluetooth.
5. A Windows AGX GPU driver.
6. Platform trust: Secure Boot, TPM 2.0, VBS/HVCI, IOMMU, driver signing, anti-cheat certification.

See `docs/status.md` and `docs/bringup-plan.md` for the details.

## Safety

Booting m1n1 on the only M4 Mac is a hard-to-undo operation. A failed custom boot can require a **DFU restore**, which erases the machine, and DFU recovery needs a second Mac. Do not run `bputil`, `kmutil configure-boot`, or any boot-security change until the checklist in `docs/safety.md` is satisfied and the machine owner has explicitly approved it.

## Licensing and credits

This repository's original scripts and documentation are MIT. The vendored/submoduled projects keep their own licenses:

- m1n1: GPL-2.0-or-later (see `third_party/m1n1/LICENSE`).
- m1n1-windows: MIT/GPL per its files.
- NTASP firmware: BSD-2-Clause OR MIT, with GPL where noted.
- NTASP drivers: MIT/GPL per file.

Credits: Asahi Linux (m1n1, hardware bring-up), NTASP/NT-for-ASi (Windows hypervisor and UEFI), Project Mu, and the WOA Project.
