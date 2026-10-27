# SiliconCamp

Native Windows on Apple Silicon using existing open-source bring-up where it is sound and original code for the gaps.

This is a research and bring-up repository, not a working product. As of **2026-10-27**:

- `m1n1` upstream builds on this M4 host.
- NTASP's `m1n1-windows` fork builds with the vGIC enabled, and its tree already contains T8132 NVMe, PCIe, and SMP support.
- Original M4 fixes are applied in three patches: `0001-vgic-m4-topology.patch` (vGIC window and redistributor count from the ADT), `0002-psci-t8132.patch` (T8132 PSCI power-domain tree, 6E+4P), and `0003-pcie-guest-handoff.patch` (stage-2 mappings for the APCIE ECAM/BAR windows, DART bypass with per-stream read-back verification, ADT topology reporting). See `patches/m1n1-windows/README.md`.
- The NTASP Project Mu UEFI firmware has an original M4 platform package and **builds successfully** in the Linux build VM. The 2026-10-27 image adds an MCFG table and a `PCI0` root bridge with a `_CRS` from the captured J624 windows, so Windows can find the APCIE ECAM and enumerate the first root complex. The generated image (`MAC16_3_EFI.fd`, 30 MB) is attached to GitHub release **v0.6.0** with its SHA-256, not committed to git.
- The M4 values are derived from this exact `Mac16,3`/`J624` machine and Asahi's T8132 device tree: 6 E-cores + 4 P-cores, 24 GB DRAM at the 1 TB alias, UART `0x3ad200000` IRQ 1046, four USB DWC3 controllers, and the APCIE host windows decoded from `ioreg` (ECAM `0x1cb0000000`, bus `0x80000000` → host `0xb80000000`).
- The generated M4 ACPI tables were checked: 10 GICC entries with the correct MPIDRs, GICD at `0x5000000000`, GICR at `0x5100000000` with a `0x140000` range, a 2-cluster/6E+4P PPTT, a matching 10-device DSDT, an MCFG entry for ECAM `0x1cb0000000` with buses 0–8, and a `PCI0` `_CRS` that pins the bus and host windows plus the 256 MB ECAM reservation. `scripts/check-acpi.py` enforces all of this — including each CPU's `_UID`, its cluster, `COM0`'s `_CRS`, and the *sign* of the PCIe translation (the PCD carries `host - bus`, the DSDT `_TRA` carries the same value, and the host bridge library negates it) — on every build, so the values cannot drift silently.
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
./scripts/verify-patches.sh          # patches still apply to the pinned submodules
./scripts/apply-patches.sh           # m1n1-windows; idempotent, build-m1n1.sh also does this
./scripts/apply-ntasp-patches.sh     # M4 UEFI platform; needs the firmware submodule
./scripts/build-m1n1.sh
./scripts/build-uefi-linux.sh Mac16_3   # requires the Lima VM; see docs/build-uefi.md
```

The patch steps leave `third_party/m1n1-windows` and `third_party/ntasp-firmware`
dirty. That is expected: the patches, not submodule pointer commits, are the
source of truth, and `verify-patches.sh` is what proves they still apply to the
pinned commits.

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
docs/                   architecture, status, bring-up plan, safety, M4 platform, hardware values,
                        PCIe/MCFG/PCI0 windows, Apple Silicon coverage matrix, T604X (M4 Pro/Max)
                        values, probe-log conventions
patches/m1n1-windows/   original patches against the NTASP m1n1 fork
patches/ntasp-firmware/ M4 UEFI platform and build patches
scripts/                host check, build, patch application, ACPI checker, SoC coverage
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
- The M4 firmware has an MCFG table and a `PCI0` root bridge: ECAM `0x1cb0000000`+256 MB, buses 0–8, 32-bit window bus `0x80000000`→host `0xb80000000` (`PcdPciMmio32Translation` = `0xb00000000` = host − bus), prefetch `0xbc0000000`+512 MB. The PCD's sign was wrong in the previous revision — it builds, boots and enumerates, and then places every BAR at an address no device decodes — and is now pinned by the checker in both directions.
- The m1n1 hand-off (patch 0003) maps the APCIE ECAM and BAR windows into the guest's stage 2 and puts every APCIE DART into bypass with per-stream read-back verification, so a guest without an APCIE driver still gets a root complex whose devices can DMA.
- The M4 virtual memory map includes the low 1 GB window used by `usb-drd0`–`usb-drd3` and `dart-usb0`–`dart-usb3`.
- The fork's `config.h` enables `ENABLE_VGIC_MODULE` and it has `hv_vgic.c`, `hv_psci.c`, `hv_smmu.c`, and `hv_vtpm.c`.
- Upstream m1n1 has T8132 (`M4 Donan`) CPU support, T8132 NVMe, T8132 PCIe, and M4 SPTM/GXF emulation work.
- The M4 vGIC patch replaces the old per-SoC vGIC switch with:
  - a 36-bit vs 42-bit MMIO window chosen from `vaddr_bits`, and
  - a redistributor count taken from the actual ADT CPU topology.
  This fixes the missing `T8132` case and the old fall-through bugs in the `T6000`/`T6020` cases.
- The M4 PSCI patch adds the T8132 power-domain tree (two clusters, 6E+4P), the T8132 PMGR CPU-start offset, ADT bounds and core-count checks, and normalises the ADT's MPIDR encoding to Aff1:Aff0. Before it, `hv_psci_init()` had no `T8132` case and passed an unset topology pointer to `psci_num_clusters = topology_tree[1]`.
- `src/pcie_guest.c` (patch 0003) does not hard-code device paths: it walks the ADT for `apcie`/`apcie-ge` controllers and `dart,*apcie*` DARTs, prints everything it finds (ECAM, `#ports`, MSI address/vector-offset/count), and refuses to run DART bypass halfway — a stream that is locked or ignores the write makes the hand-off fail loudly instead of leaving a device that silently cannot DMA. `HV_QUIESCE_PCIE` in `config.h` restores upstream's `pcie_shutdown()` for a guest that owns the controllers.
- The per-SMC PSCI trace is compiled out by default (`DEBUG_PSCI_SMC` in `config.h` turns it on), so the guest's timing is not distorted by a UART write on every PSCI call.

## What is still missing

1. Hardware validation: the firmware builds and its CPU/RAM/UART/ACPI topology matches the machine, but m1n1 and the UEFI image have not run on the M4 yet.
2. PCIe and storage: the host bridge, page tables, MCFG and `PCI0` `_CRS` now use the captured J624 ECAM (`0x1cb0000000`), 32-bit window (bus `0x80000000` → host `0xb80000000`) and prefetch window (`0xbc0000000`), and the m1n1 hand-off maps them into the guest and bypasses the APCIE DARTs. MSI is the open PCIe problem: the guest has no way to be told about Apple's `0xfffff000` doorbell, and the vGIC's ITS window handles only `GITS_CTLR`/`GITS_BASER*`. The four `apciec` complexes are not described, and the internal ANS2 SSD has no driver. USB storage is the current install path. See `docs/pcie.md`.
3. A working Windows loader-to-kernel handoff (the central unsolved bug, even on M1/M2).
4. Windows drivers: ANS NVMe, DART, PCIe, DCP display, SPI input, SMC, USB, Wi-Fi/Bluetooth.
5. A Windows AGX GPU driver.
6. Platform trust: Secure Boot, TPM 2.0, VBS/HVCI, IOMMU, driver signing, anti-cheat certification.

See `docs/status.md` and `docs/bringup-plan.md` for the details.

## Releases

Images are published as GitHub releases. The current build is **v0.6.0**:

```bash
mkdir -p /tmp/siliconcamp-v0.6.0
gh release download v0.6.0 --dir /tmp/siliconcamp-v0.6.0
(cd /tmp/siliconcamp-v0.6.0 && shasum -a 256 -c SHA256SUMS)
```

`v0.6.0` rebuilds both images: the fix for the PCIe translation sign and the
first hypervisor-side PCIe hand-off (`pcie_guest.c`, patch 0003). The earlier
`v0.5.0` / `v0.4.0` images carry the M4 PSCI topology fix and the superseded
`bus - host` translation value. Do not install any of them until
`docs/safety.md` is satisfied.

## Safety

Booting m1n1 on the only M4 Mac is a hard-to-undo operation. A failed custom boot can require a **DFU restore**, which erases the machine, and DFU recovery needs a second Mac. Do not run `bputil`, `kmutil configure-boot`, or any boot-security change until the checklist in `docs/safety.md` is satisfied and the machine owner has explicitly approved it.

## Licensing and credits

This repository's original scripts and documentation are MIT. The vendored/submoduled projects keep their own licenses:

- m1n1: GPL-2.0-or-later (see `third_party/m1n1/LICENSE`).
- m1n1-windows: MIT/GPL per its files.
- NTASP firmware: BSD-2-Clause OR MIT, with GPL where noted.
- NTASP drivers: MIT/GPL per file.

Credits: Asahi Linux (m1n1, hardware bring-up), NTASP/NT-for-ASi (Windows hypervisor and UEFI), Project Mu, and the WOA Project.
