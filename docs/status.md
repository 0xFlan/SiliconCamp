# Status — 2026-10-28

Host and target are the same machine: `Mac16,3` iMac (`target-type J624`), Apple M4
(`T8132`), 10 cores (6 efficiency + 4 performance), 24 GB, macOS 26.6.2 (Darwin 25.6.0),
firmware `18000.161.10`.

## Verified in this repository

| Check | Result |
|---|---|
| `uname -m` | `arm64` |
| Xcode CLT clang | 17.0.0 (Apple) |
| Homebrew LLVM | 23.1.4 |
| Homebrew LLD | 23.1.4 |
| Rust | 1.99.1, target `aarch64-unknown-none-softfloat` installed |
| QEMU | 11.1.2 |
| upstream m1n1 build | success, `third_party/m1n1/build/m1n1.bin` |
| NTASP m1n1-windows build | success, `third_party/m1n1-windows/build/m1n1.bin` |
| M4 vGIC + PSCI + PCIe/MSI patches | applied in seven cumulative patches; the series builds |
| M4 PSCI topology | T8132 tree is 2 clusters (6E+4P); ADT core count checked at init |
| m1n1 PCIe hand-off | `src/pcie_guest.c` maps ECAM + BAR windows into stage 2 and bypasses every APCIE DART, verifying each stream by read-back |
| m1n1 guest MSI | `src/guest_msi.c` programs all 256 port MSIMAP entries with read-back, unmasks AIC 1506–1761, and `hv_vgic3_guest_vintid()` maps them to INTIDs 512–767; the firmware advertises the same window as a MADT GIC MSI Frame |
| Patch integrity | the m1n1-windows series (0001-0007) and the firmware patches apply to their pinned commits, in order, in one worktree (checked 2026-10-28); applying the series to a pristine `HEAD` reproduces the submodule's working tree byte-for-byte, and that patched tree builds to the same `m1n1.macho`/`m1n1.bin` hashes as the working tree it was generated from (`a5636c6d…` / `06247d0e…`, checked 2026-10-28); the firmware platform patch reproduces every file it owns, including the MADT MSI frame. `0003-vgic-selftest.patch` is verified as a continuation of `0001` in the same cumulative worktree, because it edits files `0001` creates |
| NTASP M4 UEFI build | success, 30 MB `MAC16_3_EFI.fd`, rebuilt 2026-10-28 with the `gating.pending` and `gating.group1` probes. `artifacts/` documents release `v0.9.0`, which carries patch `0007` and both enable-gate probes |
| M4 ACPI/image checker | `./scripts/check-acpi.py` passes; run by the build script |
| Checker regression tests | 139 pass (`python3 -m unittest discover -s tests`), including 31 that mutate the vGIC series or patched sources and require `check-vgic.py` to reject each defect, 25 that mutate the self-test series or tree and require `check-vgic-selftest.py` to reject it, 5 for the GICv3 oracle, and 8 for the assistant's status, skip, submodule, walkthrough and native-bundle handling |
| Assistant walkthrough | Six-step Boot Camp-shaped AppKit/WKWebView window and browser page. The native shell starts and stops the loopback server it owns, and `--screenshot` produced a 1080×720 render; the Install control is disabled. Still not an installer |
| vGIC conformance self-test | `AppleVgicSelfTestDxe` is linked into the DXE FV and placed before `TimerDxe`; `scripts/check-vgic-selftest.py` passes in both modes (94 checks against the patch series, 93 against the patched tree) and the build script fails if the module is missing. Never executed on hardware |
| GICv3 reference oracle | `scripts/check-gicv3-oracle.py` drives QEMU 11.1.2's GICv3 model over qtest, with no guest, and passes 26 probes: the set/clear bank aliases read back the same state, a write of 0 to `GICR_ISENABLER0` is ignored, `GICD_IGROUPR` is writable under `DS=1` rather than RAZ/WI, `GICD_CTLR.DS`/`ARE` cannot be cleared, and a redistributor wake completes. Local-only; skips when QEMU is absent |
| M4 ACPI tables | disassembled and checked (below) |
| MCFG + PCI0 | MCFG describes ECAM `0x1cb0000000`, buses 0–8; PCI0 `_CRS` matches the captured windows |
| GIC MSI Frame | MADT entry 0x0D: doorbell `0xfffff000`, SPIs 512–767; cross-checked against `src/guest_msi.h` and rejected if it runs past SPI 1019 |
| ANS2 driver constants | 110 checks against m1n1's `nvme.c`/`asc.c`/`sart.c`/`rtkit.c` (`scripts/check-ans2-constants.py`) |
| ANS2 driver in the image | `AppleNANDStorageDxe` (36 KB) linked into the DXE FV; absence fails the build script |
| PCIe translation sign | `PcdPciMmio32Translation` = `0xb00000000` = host − bus == DSDT `_TRA`; checked in both directions, and a wrong sign fails the build |

The `m1n1-windows` build uses `ENABLE_VGIC_MODULE` from `config.h` and produces a
1.1 MB raw image.

## M4 platform facts used by the build

These are not guesses. The CPU topology and device addresses come from Asahi's
`t8132.dtsi`; the DRAM alias, target type, USB controllers, and PCIe ECAM come from
this Mac's `ioreg`. See `docs/hardware-values.md` for the source of each value.

| Item | Value |
|---|---|
| Target type | `J624` / `Mac16,3` / `T8132` |
| DRAM alias (`chosen/dram-base`) | `0x10000000000` (1 TB) |
| DRAM size (`chosen/dram-size`) | `0x600000000` (24 GB) |
| Firmware load address | `0x10030000000` (1 TB + 3 GB) |
| Boot args copy | `0x10220000000` (1 TB + 8.5 GB) |
| ADT copy | `0x10220004000` (1 TB + 8.5 GB + 16 KB) |
| E-core MPIDRs | `0x000`–`0x005` |
| P-core MPIDRs | `0x10100`–`0x10103` |
| UART | `0x3ad200000`, AIC IRQ 1046 |
| AICv3 | `0x381180000` (`apple,t8132-aic3`) |
| vGIC distributor / redistributors | `0x5000000000` / `0x5100000000` |
| PCIe ECAM (`apcie`) | `0x1cb0000000`, 256 MB |
| USB DWC3 controllers | 4 (`usb-drd0`–`usb-drd3`) |
| USB DARTs | 4 (`dart-usb0`–`dart-usb3`) |

## ACPI tables in the built FD

Checked by disassembling the build-tree `.aml`/`.acpi` output (the final FD stores
them compressed inside the Project Mu FV), and enforced by
`scripts/check-acpi.py`:

- **MADT**: 10 GICC entries. UIDs/GIC IDs 0–9; MPIDRs `0x0`–`0x5` for the six
  E-cores and `0x10100`–`0x10103` for the four P-cores; efficiency class 0 for
  E-cores and 1 for P-cores. GICD `0x5000000000`; GICR `0x5100000000` with length
  `0x140000` (10 × `0x20000`). One GIC MSI Frame (type `0x0D`, 24 bytes):
  doorbell `0xfffff000`, flags 0, SPIs 512–767 — the window `guest_msi.c`
  translates, so a device behind APCIE has an interrupt path without an ITS.
- **PPTT**: 13 hierarchy nodes — one package (ACPI ID 0), a 6-core E-cluster
  (ACPI ID 0), a 4-core P-cluster (ACPI ID 1), and cores with ACPI IDs 0–9. The
  parent offsets point at the package and cluster nodes; this matches the MADT
  UID order.
- **DSDT**: one `DIE0` container, `CLU0` with CPU0–CPU5 and `CLU1` with CPU6–CPU9;
  all ten `ACPI0007` processor devices have `_UID` 0–9. `COM0` is
  `APPL8900` at `0x3ad200000` with interrupt 1046. `PCI0` is `PNP0A08` with
  `_CID` `PNP0A03`, bus 0–8, the 32-bit window bus `0x80000000`–`0xbfffffff`
  with translation `0xb00000000`, the prefetch window `0xbc0000000`–
  `0xbdfffffff`, and `RES0` reserving the 256 MB ECAM at `0x1cb0000000`.
- **MCFG**: one allocation entry, ECAM `0x1cb0000000`, segment 0, buses 0–8.
- **FADT**: desktop profile, hardware-reduced ACPI, PSCI compliant, SMC conduit.
- **DBG2**: points at `_SB.COM0`, using `PcdAppleUartBase` (`0x3ad200000`).

## vGIC guest contract

The firmware is a lenient GICv3 guest. EDK2's `ArmGicV3Dxe` finds a
redistributor by walking `GICR_TYPER` until the affinity matches the boot CPU,
never writes `GICR_WAKER`, and never polls `RWP`, so it can boot through a vGIC
that an operating system cannot use. The 2026-10-27 audit compared the emulation
against ARM IHI 0069 and against upstream Linux KVM and QEMU, and found nine
defects. All are fixed in `patches/m1n1-windows/0004-vgic-guest-contract.patch`:

| Defect | Effect on a strict guest |
|---|---|
| Redistributor decoded as `addr - redist_base` with `cpu_num = ctx->cpu_id` | Every frame but the first aborts; CPU 1's `GICR_WAKER` arrives at `0x20014` |
| `GICR_WAKER` write stored verbatim | `ChildrenAsleep` never clears, so the wake poll never completes |
| `GICD_CTLR.RWP` set on group disable and `GICD_ICENABLER`, never cleared | A guest polling `RWP` spins forever |
| `GICR_CTLR` wrote bit 31 (`UWP`) instead of bit 3 (`RWP`) | `UWP` is RES0 with `VLPIS=0`; `RWP` never signalled |
| `GICR_TYPER.PLPIS`/`DirectLPI`/`DPGS` set, `GICR_SETLPIR` a no-op | Guest builds an LPI table and waits for an interrupt that cannot arrive |
| `GICD_TYPER.LPIS` set with no ITS | Guest looks for a command queue that does not exist |
| `GICD_ICENABLER` unmasked while logging "masking" | Every interrupt the guest disables stays live |
| Guest INTIDs passed to the AIC untranslated | SPI 512 touches AIC 512, not the APCIE source at 1506 |
| `IROUTER` stored `smp_get_id()` in a `u32` | `-1` becomes `0xffffffff`, and `BIT(-1)` is undefined |

Three related suspicions were checked and dismissed: the GTDT timer GSIVs 17/18
match what `hv_exc.c` injects, `ICC_SGI1R_EL1` traps without `TALL1` because
`ICH_HCR_EL2.En` is set, and `ICH_VMCR_EL2 = BIT(1)` leaves `VPMR` at its
architectural reset. `docs/next-steps.md` records the reasoning and the sources.

`scripts/check-vgic.py` is the gate. It asserts the contract against the patch
series (no submodule, no compiler) and against patched sources, and
`tests/test_check_vgic.py` puts each defect back and requires the checker to fail.

Patch 0007 adds the delivery half of the contract. `hv_vgic3_irq_enabled()`
gates every injection on `GICD_CTLR.EnableGrp1` and the INTID's own enable bit,
and a refusal keeps the interrupt queued instead of dropping it. The queue is
drained by compaction, not by popping the head, so an INTID the guest has
disabled cannot block the interrupts behind it, and enabling an INTID — in
`GICR_ISENABLER0`, a `GICD_ISENABLER` bank, or `GICD_CTLR.EnableGrp1` — drains
the queues at that write. Without the drain, an edge-triggered interrupt that
arrived while disabled would stay pending until some unrelated interrupt
happened to arrive; `gating.pending` in the self-test is the probe that measures
it. `gating.group1` and `gating.group1-pending` measure the other half of that
predicate, the distributor's Group 1 enable. The redistributor's set/clear pairs
also stopped sharing the active bank, and `GICD_IGROUPR` reports the Group 1
that delivery implements. Queuing is a
single `virq_queue_push_unique()` critical section rather than a separate
"is it queued?" test followed by a push, so two CPUs sending the same SGI to one
target cannot both enqueue it; the GIC has one pending bit per INTID.

Being correct is not the same as being observable, and the second half of this is
a boot hazard of its own. `hv_vgic.c` built with `ENABLE_VGIC_LOGGING 1`, which
put three `printf()` calls at the end of every emulated register access. Over a
115200-baud console a 60-byte line costs about 5 ms, and a guest programming
~1020 SPIs plus one redistributor frame per PE performs tens of thousands of
accesses — minutes of console output, which presents as a hang in the code the
logging was added to debug. Patch 0005 compiles the trace out and replaces it
with a 64-entry ring, one-bit-per-8-bytes touched bitmaps per address space, and
a 32-line budget for genuinely exceptional events, all in the global `vgic_diag`.
m1n1 keeps running at EL2 after the guest hangs, so that state can be read over
the proxy precisely when there is no log to read; `docs/vgic-diagnostics.md`
documents the offsets and a reader.

The one capability claim that was still wrong is `GICD_TYPER.No1N`. It was 0,
which advertises distributor-side 1-of-N SPI selection that this vGIC never
performs (and the `GICR_CTLR.DPG*` opt-out bits, which it does not model).
Patch 0006 sets it to 1, treats an `IRM` write of 1 as 0 — the behaviour ARM
IHI 0069 permits when `No1N` is 1 — and stores the routing value without bit 31,
so read-back cannot claim a mode that never runs. `No1N` is a statement about
the emulated device, not about Apple's AIC: `aic_set_affinity()` is a no-op on
AICv2/v3, so an SPI still lands on the PE the AIC picks. That residual gap, and
why it is not closed before the self-test can measure it, is in
`docs/architecture.md`.

None of this has run on hardware; a UEFI conformance self-test is the next
milestone because it is the only way to observe the contract before Windows
exists. The ring and counters say what the guest *did*, not whether it was
correct.

## Component status

| Component | Status | Notes |
|---|---|---|
| m1n1 hardware init on M4 | Partial | Upstream has T8132 CPU, NVMe, PCIe, SPTM/GXF work; Asahi still calls M4 bring-up incomplete. |
| m1n1-windows vGIC | Patched, builds | Original code lacked `T8132`; CPU count and PA window are now dynamic. Patch 0004 makes the redistributor and distributor honour the guest contract: per-frame decode, `GICR_WAKER` wake completion, `RWP` always 0, no LPI capabilities, and INTID→AIC translation on every mask/unmask/routing path. `scripts/check-vgic.py` locks it. Patch 0005 takes the per-access `printf()` off the hot path (it was a UART write per emulated register access) and replaces it with a ring buffer, touched bitmaps and a budgeted exception log in the global `vgic_diag`, readable over the proxy after a hang (`docs/vgic-diagnostics.md`). Patch 0006 sets `GICD_TYPER.No1N`: the distributor does not select a target PE, so it no longer advertises 1-of-N routing, and `GICD_IROUTER.IRM=1` is treated as 0. The affinity itself still cannot move an SPI on AICv3 (`docs/architecture.md`). Not hardware-tested. |
| PSCI | Patched, builds | `hv_psci.c` had no `T8132` case; it now has the 6E+4P power-domain tree, the T8132 CPU-start offset (`0x34000`, same as `smp.c`), ADT bounds/count checks, and Aff1:Aff0 MPIDR normalisation. Unknown SoCs are fatal instead of dereferencing an unset topology pointer. Not hardware-tested. |
| SMMU/DART abstraction | Stub | `hv_smmu.c` is 45 lines; not a real IOMMU. |
| vTPM | Stub | `hv_vtpm.c` is 16 lines; no TPM 2.0. |
| NTASP UEFI firmware | Builds | M2 reference and M4 `Mac16_3` package both build in the Lima VM. |
| User-facing assistant | Builds, not an installer | Native AppKit/WKWebView window plus a six-step walkthrough: machine, image hashes, diagnostics, safety, blockers and the Boot Camp gap. The Install control is disabled; the app reads this checkout and writes nothing. |
| M4 platform package | Builds, ACPI checked | `Silicon/Apple/T8132FamilyPkg` + `Platform/Mac16_3Pkg`; CPU/RAM/UART/PPTT match the J624 hardware. PCIe uses the captured J624 ECAM, bus window and host offset (`docs/pcie.md`); MCFG, `PCI0`/`_CRS` and the GIC MSI Frame are in the firmware. The `apciec` complexes and `_PRT`/IORT are still missing. |
| m1n1 PCIe guest hand-off | Patched, builds | Patch 0003 adds `pcie_guest.c`: ADT walk for controllers/DARTs/MSI, stage-2 mapping of ECAM and BAR windows, DART bypass with per-stream verification, and a one-line failure if any stream cannot be bypassed. Not hardware-tested. |
| PCIe MSI to a guest | Patched, builds | Patch 0003 adds `guest_msi.c`: port MSIMAP entries programmed to `0x80000000 \| i` with read-back, AIC 1506–1761 unmasked, and `hv_vgic3_guest_vintid()` translating them to SPIs 512–767. AIC interrupts with no INTID representation are masked and logged once instead of injected verbatim. Deliberately not an emulated ITS: Apple's hardware never writes `GITS_TRANSLATER`. Not hardware-tested. |
| M4 USB path | Prepared, untested | Four DWC3 controllers are described by `PcdAppleNumDwc3Controllers`; the low 1 GB MMIO window (USB and USB DARTs) is mapped. The DWC3/XHCI stack still needs hardware validation. |
| NTASP AIC DXE | Partial | AICv1/v2; AICv3 is an explicit TODO. The M4 package uses the vGIC path. |
| NTASP Windows drivers | Empty | Repository is README-only. |
| vGIC conformance self-test | In the image, untested | `AppleVgicSelfTestDxe` in the DXE FV before `TimerDxe`, gated on `AIC_BUILD == FALSE`. Walks every redistributor frame, performs the wake sequence, checks capability bits and `RWP`, drives SPI 512's banked registers, measures the SGI/PPI set/clear banks on the boot PE, and delivers a self-SGI and the virtual timer PPI through the hardware interrupt protocol. It also measures that an SGI refused while disabled is still delivered when the bit is set, without resending. 94 offline checks pin bounded waits, EOI, the modelled register set, both halves of the enable gate in both directions, the Windows HAL replay and boot order. Never executed on hardware; with `0007` applied the previously predicted gaps should be `[PASS]` lines. |
| Internal NVMe/ANS2 | Driver written, untested | `AppleNANDStorageDxe` boots the ANS coprocessor (ASC mailbox, RTKit handshake, SART windows), programs the NVMMU/NVMe queues, runs Identify and publishes `EFI_BLOCK_IO_PROTOCOL`. 36 KB DXE module, linked into the FV, 110 hardware constants checked against m1n1 on every CI run. Never executed on hardware; see `docs/ans2.md`. |
| Windows kernel boot | Not achieved | Existing project hangs at loader-to-kernel handoff even on M1/M2. |
| AGX GPU driver | None | No WDDM/D3D driver for any Apple Silicon GPU. |
| Anti-cheat/trust | None | No Secure Boot/TPM/VBS/HVCI/IOMMU certification path. |

## Immediate blockers, in order

1. **Hardware boot of m1n1 and the UEFI firmware.** The build and ACPI tables are
   coherent, but nothing has run on the M4. This is gated by `docs/safety.md`.
2. **Hardware run of the vGIC conformance self-test.** The driver is in the image
   (`AppleVgicSelfTestDxe`, patch `0003`): it walks every redistributor frame,
   wakes each frame with a bounded poll, checks both `RWP` bits and the capability
   bits, drives the banked SPI registers, and delivers a self-SGI and the timer
   PPI through the real exception path. The checker proves it cannot hang, dead
   loop or touch an unmodelled register; only the M4 can prove the device it
   describes (`docs/vgic-selftest.md`). With `0007` applied, no `[GAP ]` is
   expected from the enable gate: `gating.sgi`, `gating.pending`, `gating.rearm`,
   `gating.group1` and `gating.group1-pending` should all be `[PASS]`. The stated gaps that remain are
   `GICR_ISPENDR0` being state rather than a trigger, Group 0 delivery, and SPI
   affinity (`docs/vgic-selftest.md`).
3. **PCIe and storage.** The host bridge, page tables, MCFG and `PCI0` `_CRS`
   use the captured J624 ECAM (`0x1cb0000000`), 32-bit window (bus
   `0x80000000` → host `0xb80000000`, offset `0xb00000000`), the m1n1 side maps
   the windows and bypasses the APCIE DARTs, and MSI is now wired end to end
   (MSI frame + `guest_msi.c` + INTID translation). What remains is validation,
   not design — and code for the four `apciec` complexes and `_PRT`/IORT. The
   ANS storage driver also needs its first hardware run; until then USB storage
   is the only installer target.
4. **Windows kernel handoff.** This is the project-wide blocker; it is not
   M4-specific, but M4 cannot bypass it.
5. **Drivers.** Even a booting kernel has no internal disk, display, input, or
   network on Apple hardware.
6. **GPU.** No path to a usable desktop or games without a WDDM driver.
7. **Trust.** Anti-cheat is a platform-certification problem, not just a driver.

## What each remaining epic needs first

Every item below is blocked on the machine, not on code:

| Epic | First action | Needs |
|---|---|---|
| Windows handoff | Boot m1n1 + UEFI once and capture the serial log. `exception.c` already prints ESR/ELR/FAR and "Unhandled exception, rebooting...", which localises the fault to a trap class. | the Mac, a data-capable USB-C cable, a serial capture |
| ANS2 storage | The UEFI `BlockIo` driver is written and in the image; boot it and read the first serial log (`docs/ans2.md` has the log-to-step map). | the machine; then a Windows KMDF driver, which is a separate project |
| PCIe | Validate MSI, then describe `apciec0`–`apciec3`. MCFG, `PCI0`/`_CRS`, the stage-2 windows, DART bypass and the MSI frame/translation are in place. | the machine to validate BAR assignment, DMA and interrupt delivery (including whether Windows needs an IORT) |
| Drivers (DCP, SPI, SMC, USB, Wi-Fi) | One KMDF driver per block; each needs the hardware. | the machine plus a Windows test-signing setup |
| GPU | No public WDDM driver for AGX exists; this is research, not a port. | GPU documentation/reverse engineering plus a machine |
| Trust | Secure Boot signing + iBoot key enrollment, TPM 2.0 (vTPM is a 16-line stub), SART/DART IOMMU. VBS/HVCI additionally needs Windows' hypervisor at EL2, which m1n1 occupies. | owner consent for boot-policy changes; `ID_AA64MMFR4_EL1` to check FEAT_NV/NV2 |

## Reproduce the build

```bash
./scripts/check-host.sh
./scripts/build-m1n1.sh
./scripts/build-uefi-linux.sh Mac16_3
./tools/siliconcamp-assistant/build-app.sh
```

UEFI build output:

```
third_party/ntasp-firmware/Build/Mac16_3-AARCH64/DEBUG_CLANGPDB/FV/MAC16_3_EFI.fd
```

The build VM intentionally skips Project Mu's `cibuild` scope (NASM, uncrustify,
and the NuGet iasl) because those downloads fail in this environment and are not
needed for an AArch64 build; see `docs/build-uefi.md`. `iasl` comes from the
Ubuntu `acpica-tools` package.

The build script finishes by running `./scripts/check-acpi.py`, so a green build
means the MADT/PPTT/FADT/DBG2/GTDT contents and the FD's ARM64 Image header still
match this machine. The FDF header's `image_size` was corrected from the upstream
value (`0x100E0000`) to the declared FD size (`0x1E00000`).

To rebuild m1n1 from a clean `m1n1-windows` checkout:

```bash
git -C third_party/m1n1-windows checkout -- .
./scripts/apply-patches.sh       # applies 0001-0007 in order
./scripts/build-m1n1.sh          # also applies them, idempotently
```

That build is macOS-only in practice. A Linux build of the same tree fails at
the final link: the Rust library's `adt_get_reg` codegen references `wcslen`,
which the Mach-O link resolves from libSystem and a freestanding ELF has no
provider for. Running it in CI (`clang --target=aarch64-linux-gnu`, `ld.lld`)
also needed `llvm-objcopy` rather than the runner's x86-only GNU `objcopy`. Both
are fixable, but the fix belongs in the Rust side of the fork, so there is no
Linux CI job for the hypervisor yet.

## Honest assessment

Two audits changed the PCIe code. The 2026-10-26 audit re-decoded the `apcie`
properties with `-d 0` and found that both earlier revisions of the window were
one hex digit short (`0xb8000000` instead of `0xb80000000`). The 2026-10-27 audit
traced `TO_HOST_ADDRESS(dev, tra) = dev - tra` through `PciHostBridgeDxe` and
found that the replacement had the translation sign backwards
(`bus - host` instead of `host - bus`): every BAR would have been programmed at
an address the hardware does not decode, and the mistake passes a build, a boot
and enumeration. Both directions are now pinned by `check-acpi.py` against the
DSDT `_CRS` and the captured cells, and `docs/pcie.md` keeps the raw cells and
the three sign conventions so the arithmetic is checkable rather than memorable.

A third audit, on the interrupt path, found the oldest bug of the three: the EL2
IRQ handler forwarded the AIC's interrupt number into a GIC list register
verbatim. That is correct only for the ~1020 AIC interrupts that happen to share
their number with a valid SPI; the APCIE MSI window starts at 1506, and the UART
(1046) and root complexes (1231–1249) are in the same invalid range, so the guest
would have received INTIDs its emulated distributor does not describe and
`hv_vgic3_get_priority()` would have read past its priority banks. Interrupts are
now translated or dropped with a log line, and the MSI path is built on that
translation rather than on an ITS the hardware cannot feed. The GIC MSI Frame is
the one interrupt contract the firmware and the hypervisor both have to agree on,
so `check-acpi.py` compares them.

A fourth audit, this one on the interrupt *controller* rather than the interrupt
path, found a class of bug the first three did not: the vGIC was consistent with
itself and with the firmware, and wrong against the architecture. The firmware is
the reason nobody noticed, because EDK2's `ArmGicV3Dxe` touches one
redistributor frame, never writes `GICR_WAKER` and never polls `RWP`. Nine
defects followed from comparing the emulation against ARM IHI 0069 instead of
against the guest that happened to run. The lesson worth keeping is that "it
boots the firmware" is evidence about the firmware, not about the GIC.

This repository now has a reproducible M4 hypervisor build, the M4
vGIC/PSCI/PCIe fixes, a vGIC that honours the guest contract a real OS depends
on (and a checker that fails if it stops doing so), a working-offline MSI path,
a compiling M4 UEFI firmware whose CPU, UART, ACPI and PCIe root-bridge values
match this exact J624 machine, and a firmware driver that reads the emulated
interrupt controller back instead of trusting it. It does **not** have a Windows
boot, and none of it has run on the M4 hardware yet. The next meaningful
milestone is still "m1n1 prints its vGIC/CPU topology on the M4, and the NTASP
UEFI firmware reaches its boot menu on the M4 and enumerates the first PCIe root
complex" — but the self-test now in the image is what makes that first log
informative about the interrupt path rather than just about the boot loader
(`docs/vgic-selftest.md`). Everything after that is debugging the handoff and
writing drivers.
