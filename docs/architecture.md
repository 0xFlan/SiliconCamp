# Architecture

## 1. The chain

### iBoot

Apple's first-stage bootloader is the only code that runs before a custom payload. It verifies Apple-signed images. There is no public iBoot replacement or exploit. Any workable project must use one of Apple's supported custom-boot mechanisms and then chainload.

### m1n1-windows

`m1n1` is Asahi's bootloader/experimentation payload for Apple Silicon. The NTASP fork adds:

- a **vGIC** emulation (`hv_vgic.c`) so a guest OS can see a GICv3 distributor/redistributor while the hardware uses Apple's AIC;
- **PSCI** (`hv_psci.c`) because Windows expects PSCI and Apple has no EL3 firmware; the M4 patch adds the T8132 power-domain tree (two clusters, 6E+4P) and the T8132 CPU-start offset;
- **SMMU** (`hv_smmu.c`) and **vTPM** (`hv_vtpm.c`) experiments;
- Windows-friendly traps in the exception path.

m1n1 runs at EL2. It owns the physical AIC and the physical timer, and it runs the guest at EL1. The guest's GIC MMIO accesses are trapped and emulated. The guest's PSCI calls are trapped and serviced by m1n1.

### NTASP Project Mu UEFI

The UEFI firmware is a Project Mu payload. It provides:

- UEFI boot services and runtime services;
- the ACPI tables its platform implements — this M4 package provides MADT,
  GTDT, FADT, DSDT, PPTT, MCFG and DBG2; the DSDT describes `PCI0` for the
  first APCIE root complex. IORT/SPCR only exist once a platform adds them,
  and this one does not yet;
- AIC or vGIC interrupt support;
- block I/O for the Windows installer and boot media;
- a simple framebuffer for the Windows boot UI.

It runs at EL1 as the m1n1 guest. Windows `bootmgr`/`winload` then run as UEFI applications and eventually hand off to the Windows kernel.

### Windows

Windows on Arm requires:

- a GICv3-compatible interrupt controller (the vGIC);
- PSCI CPU power management (m1n1);
- ACPI tables describing CPUs, timers, interrupts, PCIe, and the IOMMU — the
  M4 package now has MCFG, `PCI0` for the first APCIE root complex and a GIC MSI
  Frame, and the m1n1 hand-off maps its windows, bypasses the APCIE DARTs and
  translates the MSI window; but there is no IORT and no `apciec` description,
  and none of the interrupt path has run on hardware;
- a WDDM display driver (or a basic framebuffer for bring-up);
- storage and input drivers;
- for anti-cheat, VBS/HVCI and a trusted platform.

## 2. Why M4 is different

The M4 (`T8132`, "Donan") is not just a faster M1:

- **SPTM/GXF**: M4 adds a Secure Page Table Monitor / Guest eXecution Framework layer. m1n1 must emulate GXF/SPRR behavior for the hypervisor to work. Asahi's progress report describes the m1n1 hypervisor being nonfunctional until Sven Peter added this emulation; it is enough for tracing, but not yet a production hypervisor.
- **WFI state loss**: M4 cores can lose state on WFI; Apple locked the chicken bits. m1n1 has workarounds, but they are new.
- **NVMe changes**: macOS 15.x changed the NVMe firmware interface; T8132 needs extra queue setup. m1n1 has T8132 NVMe support, but it is recent.
- **PCIe**: T8132 PCIe initialization is new; the Asahi feature table still marks M4 PCIe as TBA/early.
- **AICv3**: M4 uses AICv3. NTASP's AIC DXE driver explicitly says "TODO: Add AICv3 support, seen in T8122 and T8130 SoCs."
- **M4 UEFI platform is new**: NTASP's original platform packages are M1/M2 families. This repo adds `Mac16_3Pkg` and `T8132FamilyPkg`, but they have only been build- and table-checked, not booted on hardware.
- **GPU**: M4 AGX is not supported by Asahi yet; there is no Windows AGX driver for any Apple Silicon generation.

## 3. The vGIC address windows

The NTASP vGIC maps its emulated GIC at fixed guest-physical addresses:

| Guest PA size | Distributor | Redistributors | ITS |
|---|---|---|---|
| 36-bit | `0xF00000000` | `0xF10000000` | `0xF20000000` |
| 42-bit | `0x5000000000` | `0x5100000000` | `0x5200000000` |

The UEFI MADT and the Windows ACPI expectations must match whichever window the hypervisor chooses. The original code selected the window from a per-chip `switch`; that switch had no `T8132` case and several missing `break`s. The patch in `patches/m1n1-windows/0001-vgic-m4-topology.patch` chooses the window from `vaddr_bits` and takes the redistributor count from the ADT CPU nodes.

A redistributor is not one register block: each PE owns a 64 KiB `RD_base`
frame followed by a 64 KiB `SGI_base` frame, so the stride is `0x20000` (it would
be `0x40000` if `GICR_TYPER.VLPIS` were set). On the M4 the MADT therefore
describes `0x5100000000 + 10 × 0x20000 = 0x140000` bytes. Which frame a trapped
access belongs to is decided by the address, not by the CPU that took the fault:
`0004-vgic-guest-contract.patch` computes `frame = (addr - redist_base) / 0x20000`
and `relative_addr = (addr - redist_base) % 0x20000`, bounds-checks the frame
against `num_cpus`, and indexes the per-CPU state with it. The original code used
`ctx->cpu_id` as the index and the un-normalised offset as the register, so only
the boot CPU's frame ever decoded correctly.

PSCI has a second per-SoC table of its own: `hv_psci.c` encodes the E/P core
split so it can build the power-domain tree. T8132 is 6E+4P, not the T602X 4E+4P,
and had no case at all. `0002-psci-t8132.patch` adds the tree and the PMGR
CPU-start offset, checks the descriptor against the ADT core count, and makes an
unknown SoC fatal instead of continuing with an unset topology pointer. The
guest's `CPU_ON` is still serviced through the spintable (`PSCI_POWER_ON_CPUS_ENABLE`
is off), but the affinity mapping and `CPU_OFF` path now use the right domains.

## 4. The interrupt path

```
hardware AIC IRQ/FIQ
        |
        v
m1n1 EL2 exception handler
        |
        v
hv_vgic3_guest_vintid(): AIC number -> guest INTID
        |
        v
vGIC LR injection (ICH_LR_EL2)
        |
        v
guest EL1 takes IRQ/FIQ
        |
        v
Windows GIC driver -> device driver
```

The guest's writes to the emulated GICD/GICR frames are trapped by `hv_map_hook`. The vGIC maintains distributor and redistributor state in heap memory, and injects virtual interrupts through the GIC virtual CPU interface list registers.

An AIC interrupt number is not a GIC INTID, and the two do not line up:

| AIC interrupt | What it is | Guest INTID |
|---|---|---|
| 0-1019 | peripherals whose AIC number happens to be a valid SPI | same number |
| 1506-1761 | APCIE MSI vectors (`msi-vector-offset` + index) | 512 + index |
| everything else (UART 1046, APCIE root complexes 1231-1249) | no INTID representation | dropped, source masked, logged once |

The MSI window exists because Apple's PCIe controllers do not use the GIC's ITS: a device writes an index to the controller's doorbell, the port maps it and the AIC raises `msi-vector-offset + index`. The firmware advertises SPIs 512-767 as a GIC MSI Frame at that doorbell (`docs/pcie.md`), the OS programs a device's MSI-X table with `data = INTID - 512`, and the hypervisor translates back. The two ends of that contract are checked by `scripts/check-acpi.py`.

The translation has to work in both directions, and it needs the same map in
both:

| Direction | Function | Used by |
|---|---|---|
| AIC number → guest INTID | `guest_msi_intid()` | the EL2 IRQ path, before injecting into an LR |
| guest INTID → AIC number | `guest_msi_aic_irq()` | `GICD_ISENABLER`/`ICENABLER`, `GICD_IROUTER`, and LR EOI |

`0003` added the first direction; `0004` added the second. Before `0004`, every
control-path call passed the guest INTID straight to `aic_set_mask()` or
`aic_set_affinity()`, so a guest disabling SPI 512 masked AIC 512 — an unrelated
peripheral — while the APCIE source at 1506 stayed unmasked. INTIDs below 32 are
SGIs/PPIs, which belong to the CPU interface and have no AIC line; those calls
are skipped rather than redirected.

The register interface around the translation is a contract too, and `0004`
makes it one a strict guest can drive:

- the redistributor frame is selected by address (section 3);
- `GICR_WAKER` completes the wake sequence, so `ChildrenAsleep` clears when the
  guest clears `ProcessorSleep`;
- `GICD_CTLR.RWP` and `GICR_CTLR.RWP` always read 0, because synchronous
  emulation has nothing pending;
- `GICR_TYPER`/`GICD_TYPER` advertise no LPI support, because there is no ITS
  and Apple's hardware never writes `GITS_TRANSLATER`;
- `GICD_ICENABLER` masks and `GICD_ISENABLER` unmasks.

`scripts/check-vgic.py` asserts all of it, against the patch series and against
patched sources; `docs/next-steps.md` explains why the firmware could not have
caught any of it.

The tricky parts are:

- maintenance interrupts;
- SError errata on pre-M3 SoCs;
- affinity routing when the physical AIC cannot target a specific vCPU
  (`aic_set_affinity()` is a no-op on AICv3, so `GICD_IROUTER` writes are
  currently accepted and ignored — a performance limitation, not a hang);
- SPIs/PPIs that Windows expects to be edge- or level-triggered;
- the ITS (mapped but unimplemented; a guest that discovers one through IORT would program a command queue nothing reads, so the firmware publishes no ITS and the MSI frame is used instead);
- AIC interrupts with no guest INTID, which used to be injected verbatim and are now dropped (see the table above);
- observability. The per-access console trace is compiled out
  (`ENABLE_VGIC_LOGGING 0`): it put three `printf()` calls on every emulated
  register access, which over a 115200-baud console is minutes of output for a
  guest that programs ~1020 INTIDs. What remains is a 64-entry ring of recent
  accesses, one-bit-per-8-bytes touched bitmaps per address space, and a
  32-line budget for exceptional events, all in the global `vgic_diag` and
  readable over the proxy after a guest hang (`docs/vgic-diagnostics.md`).

## 5. The memory path

m1n1 sets up stage-2 translation with 16 KB pages and a guest PA size derived from `ID_AA64MMFR0_EL1.PARange`. The vGIC's MMIO window must be inside that guest PA space. The UEFI firmware and Windows then use ACPI to discover devices, and m1n1 traps MMIO to emulate or forward it.

Apple's IOMMU is a set of per-device **DART** units, not a single ARM SMMU. Windows expects an SMMU-like IOMMU for DMA remapping and, for anti-cheat, IOMMU enforcement. NTASP's `hv_smmu.c` is a stub/experiment; a real DART-to-SMMU abstraction is required.

## 6. What "from scratch" means here

You cannot replace iBoot. The realistic from-scratch work is:

- original M4 platform code in the UEFI firmware;
- original M4 vGIC/PSCI fixes in m1n1;
- original Windows drivers for Apple hardware;
- original AGX WDDM/D3D driver work;
- original trust/attestation work.

Everything else is integration with existing open-source components.
