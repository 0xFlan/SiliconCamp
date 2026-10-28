# SiliconCamp

Native Windows on Apple Silicon using existing open-source bring-up where it is sound and original code for the gaps.

This is a research and bring-up repository, not a working product. As of **2026-10-28**:

- `m1n1` upstream builds on this M4 host.
- NTASP's `m1n1-windows` fork builds with the vGIC enabled, and its tree already contains T8132 NVMe, PCIe, and SMP support.
- Original M4 fixes are applied in seven patches: `0001-vgic-m4-topology.patch` (vGIC window and redistributor count from the ADT), `0002-psci-t8132.patch` (T8132 PSCI power-domain tree, 6E+4P), `0003-pcie-guest-handoff.patch` (stage-2 mappings for the APCIE ECAM/BAR windows, DART bypass with per-stream read-back, ADT topology reporting, and a guest MSI path that programs the APCIE port MSI maps and translates AIC interrupts into SPIs 512–767), `0004-vgic-guest-contract.patch` (redistributor frame decode, `GICR_WAKER` wake completion, `RWP` semantics, honest capability bits, and one guest-INTID→AIC translation used by every mask, unmask and routing path), `0005-vgic-diagnostics.patch` (takes the per-access `printf()` off the interrupt path — it was a UART write per emulated register access, which is a multi-minute stall for an OS that programs ~1020 SPIs — and replaces it with a ring buffer, touched bitmaps and a budgeted exception log readable over the proxy after a hang), and `0006-vgic-honest-spi-routing.patch` (sets `GICD_TYPER.No1N`, so the distributor stops claiming 1-of-N SPI selection it never performs, and treats an `IRM` write of 1 as 0), and `0007-vgic-enable-gating.patch` (delivery now consults the guest's `GICD_CTLR.EnableGrp1`, `GICR_ISENABLER0` and `GICD_ISENABLER` state; a refused interrupt stays queued, the drain skips it without blocking the rest, and enabling an INTID drains the queue; and the redistributor's set/clear bank pairs keep their own state instead of sharing the active bank). See `patches/m1n1-windows/README.md`.
- The NTASP Project Mu UEFI firmware has an original M4 platform package and **builds successfully** in the Linux build VM. The 2026-10-27 image adds an MCFG table and a `PCI0` root bridge with a `_CRS` from the captured J624 windows, so Windows can find the APCIE ECAM and enumerate the first root complex, plus a MADT GIC MSI Frame that gives the guest an interrupt path to devices behind it. The generated image (`MAC16_3_EFI.fd`, 30 MB) is attached to GitHub release **v0.8.0** with its SHA-256, not committed to git.
- The M4 values are derived from this exact `Mac16,3`/`J624` machine and Asahi's T8132 device tree: 6 E-cores + 4 P-cores, 24 GB DRAM at the 1 TB alias, UART `0x3ad200000` IRQ 1046, four USB DWC3 controllers, and the APCIE host windows decoded from `ioreg` (ECAM `0x1cb0000000`, bus `0x80000000` → host `0xb80000000`).
- The generated M4 ACPI tables were checked: 10 GICC entries with the correct MPIDRs, GICD at `0x5000000000`, GICR at `0x5100000000` with a `0x140000` range, a 2-cluster/6E+4P PPTT, a matching 10-device DSDT, an MCFG entry for ECAM `0x1cb0000000` with buses 0–8, a `PCI0` `_CRS` that pins the bus and host windows plus the 256 MB ECAM reservation, and a GIC MSI Frame at the APCIE doorbell (`0xfffff000`) covering SPIs 512–767. `scripts/check-acpi.py` enforces all of this — including each CPU's `_UID`, its cluster, `COM0`'s `_CRS`, the *sign* of the PCIe translation (the PCD carries `host - bus`, the DSDT `_TRA` carries the same value, and the host bridge library negates it), and agreement between the MSI frame and the hypervisor's `src/guest_msi.h` — on every build, so the values cannot drift silently.
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
./scripts/verify-patches.sh          # the patch series still applies to the pinned submodules
./scripts/check-vgic.py              # the vGIC still honours the GICv3 guest contract
./scripts/check-vgic-selftest.py     # the self-test cannot hang, dead-loop or read an unmodelled register
./scripts/check-gicv3-oracle.py      # the GICv3 read-backs the vGIC assumes, checked against QEMU
./scripts/check-ans2-constants.py    # the storage driver's registers still match m1n1
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
patches/ntasp-firmware/ M4 UEFI platform, the ANS storage driver and build patches
scripts/                host check, build, patch application, ACPI and ANS2 constant
                        checkers, SoC coverage
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
- The internal SSD driver (`AppleNANDStorageDxe`) is no longer a stub: it boots the ANS coprocessor through its ASC mailbox and RTKit handshake, programs the SART and the NVMMU/NVMe queues, runs `Identify` and publishes the disk as `EFI_BLOCK_IO_PROTOCOL`. It links into the FD, and its 110 register offsets/bit positions are checked against m1n1's implementation on every CI run (`scripts/check-ans2-constants.py`). It has not run on hardware yet — `docs/ans2.md` maps every log line to a bring-up step.
- The fork's `config.h` enables `ENABLE_VGIC_MODULE` and it has `hv_vgic.c`, `hv_psci.c`, `hv_smmu.c`, and `hv_vtpm.c`.
- The firmware image contains `AppleVgicSelfTestDxe` (patch `0003`). It runs after `ArmGicDxe` and before `TimerDxe`, walks every redistributor frame, performs the guest wake sequence on each with a bounded poll, drives the banked SPI registers, and delivers a self-SGI and the virtual timer PPI through the real exception path, printing one line per check. `docs/vgic-selftest.md` is the per-check reference; `scripts/check-vgic-selftest.py` refuses a self-test that can hang, dead-loop, skip EOI or touch a register the vGIC does not model. It has not run on hardware.
- The vGIC now honours the parts of the GICv3 contract a real OS depends on, which the firmware does not test: it decodes each redistributor frame from the address rather than the faulting CPU, completes a `GICR_WAKER` wake sequence, leaves `RWP` at 0, stops advertising LPI support nothing implements, and translates every guest INTID into an AIC number before masking, unmasking or routing it. `scripts/check-vgic.py` asserts the contract against the patch series (no submodule, no compiler) and against patched sources, and fails if any of the nine defects audited on 2026-10-27 comes back. It also no longer prints one console line per emulated register access: that trace was a stall waiting for the first guest that programs the GIC properly. And it no longer advertises 1-of-N SPI routing it cannot perform: `GICD_TYPER.No1N` is 1, and `GICD_IROUTER.IRM` is treated as 0. Patch `0007` completes the contract on the delivery side: an interrupt the guest has disabled is no longer injected but stays pending, the queue drain delivers the entries behind a disabled one instead of stopping, enabling an INTID drains the queue immediately, the redistributor's `ISENABLER0`/`ICENABLER0`, `ISPENDR0`/`ICPENDR0` and `ISACTIVER0`/`ICACTIVER0` pairs keep their own state and read back through both aliases, and `GICD_IGROUPR` reports the Group 1 that delivery implements. Those read-backs were verified against QEMU's GICv3 model (`scripts/check-gicv3-oracle.py`, 26 probes) before the patch was written. See `docs/next-steps.md`, `docs/vgic-diagnostics.md` and `docs/architecture.md`.
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
2. A hardware run of the vGIC conformance self-test: the contract fixes above are arguments about what a strict guest does, and the firmware never exercised them. `AppleVgicSelfTestDxe` (patch `0003`) turns them into observations: it walks every redistributor frame, wakes each frame with a bounded poll, checks both `RWP` bits and the capability bits, drives the banked SPI registers, and delivers a self-SGI and the timer PPI through the real exception path. It is in the image and is machine-checked offline (`docs/vgic-selftest.md`), but nothing has executed it on the M4. The gaps it was expected to report — a disabled SGI and PPI still delivered, and `GICD_IGROUPR` reading 0 — are closed by `0007`, so the first run should report `[PASS]` for those, including `gating.pending`: an SGI sent while disabled has to arrive when the bit is set, without being sent again. The remaining stated limitations are listed in `docs/vgic-selftest.md`.
3. PCIe and storage: the host bridge, page tables, MCFG and `PCI0` `_CRS` use the captured J624 ECAM (`0x1cb0000000`), 32-bit window (bus `0x80000000` → host `0xb80000000`) and prefetch window (`0xbc0000000`); the m1n1 hand-off maps them into the guest and bypasses the APCIE DARTs; and MSI is wired from the MADT GIC MSI Frame through the APCIE port maps to the guest's INTID window. None of that has run on hardware. The four `apciec` complexes are not described, `_PRT`/IORT are absent, and the internal ANS2 SSD's UEFI driver has not run. USB storage is the current install path. See `docs/pcie.md`.
4. Validation of the MSI window on hardware: the port map offset, the meaning of `msi-vector-offset`, the AIC target CPU, and whether an APCIE device actually raises the AIC interrupt from an MSI-X write to the frame are all unproven (`docs/pcie.md` lists them in the order a first boot will expose them; OpenVMM documents that a Windows aarch64 guest drives MSI through a GICv2m frame, so the guest side is the documented one).
5. A working Windows loader-to-kernel handoff (the central unsolved bug, even on M1/M2).
6. Windows drivers: ANS NVMe (the UEFI driver is the reference now), DART, PCIe, DCP display, SPI input, SMC, USB, Wi-Fi/Bluetooth.
7. A Windows AGX GPU driver.
8. Platform trust: Secure Boot, TPM 2.0, VBS/HVCI, IOMMU, driver signing, anti-cheat certification.

See `docs/status.md`, `docs/bringup-plan.md` and `docs/next-steps.md` for the details.

## Releases

Images are published as GitHub releases. The current build is **v0.8.0**:

```bash
mkdir -p /tmp/siliconcamp-v0.8.0
gh release download v0.8.0 --dir /tmp/siliconcamp-v0.8.0
(cd /tmp/siliconcamp-v0.8.0 && shasum -a 256 -c SHA256SUMS)
```

The vGIC conformance self-test (firmware patch `0003`) is in the tree and in a
local `Mac16_3` build, but it is not in `v0.8.0`; the next published image will
carry it. `main` is ahead of `v0.8.0` by that driver and patch `0007`, and no
image has been published from it yet. `v0.8.0` adds the guest MSI path to both
halves: the UEFI image carries the MADT GIC MSI Frame, and the hypervisor
programs the APCIE port MSI maps, unmasks the AIC vectors and translates them
into the guest's SPI window. `v0.7.0` added the internal SSD driver to the UEFI
image. `v0.6.0` carries the fix for the PCIe
translation sign and the first hypervisor-side PCIe hand-off (`pcie_guest.c`,
patch 0003). The earlier `v0.5.0` / `v0.4.0` images carry the M4 PSCI topology
fix and the superseded `bus - host` translation value. Do not install any of
them until `docs/safety.md` is satisfied.

## Safety

Booting m1n1 on the only M4 Mac is a hard-to-undo operation. A failed custom boot can require a **DFU restore**, which erases the machine, and DFU recovery needs a second Mac. Do not run `bputil`, `kmutil configure-boot`, or any boot-security change until the checklist in `docs/safety.md` is satisfied and the machine owner has explicitly approved it.

## Licensing and credits

This repository's original scripts and documentation are MIT. The vendored/submoduled projects keep their own licenses:

- m1n1: GPL-2.0-or-later (see `third_party/m1n1/LICENSE`).
- m1n1-windows: MIT/GPL per its files.
- NTASP firmware: BSD-2-Clause OR MIT, with GPL where noted.
- NTASP drivers: MIT/GPL per file.

Credits: Asahi Linux (m1n1, hardware bring-up), NTASP/NT-for-ASi (Windows hypervisor and UEFI), Project Mu, and the WOA Project.
