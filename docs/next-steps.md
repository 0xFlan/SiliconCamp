# Next steps

This document answers one question: given that the M4 hypervisor, firmware, ACPI
tables and PCIe/MSI path all build and are internally consistent, what is the
highest-value work that can be done *before* the machine is available, and what
must wait for it?

It is written to be argued with. Every claim is tagged with how it is known:

| Tag | Meaning |
|---|---|
| **verified** | Checked against source in this repository, in the pinned submodules, or in upstream Linux/QEMU/EDK2. Reproducible from a clean checkout. |
| **machine-checked** | Not just verified by reading: a script in `scripts/` fails if it stops being true. |
| **inferred** | Follows from verified facts plus a documented architectural rule, but the combination has not been observed on this hardware. |
| **unproven** | A guess. Listed so it can be disproved, not relied on. |

Nothing in this document has been observed on the M4. The hardware is the only
thing that can promote **inferred** to **verified**, and it is not available
(docs/safety.md).

---

## The finding

The next milestone is **vGIC guest-contract correctness**, not more ACPI.

That is a change of direction. The previous plan treated the interrupt
controller as done once the firmware's MADT described the same windows the
hypervisor mapped, and treated the remaining ACPI work — `IORT`, `_PRT`,
`_OSC`, the four `apciec` complexes — as the next offline deliverable. The
evidence below says that ordering is wrong twice over:

1. The vGIC does not currently honour the GICv3 contract that a real operating
   system relies on, so an ACPI table that describes it accurately describes
   something unusable.
2. The ACPI work that remains is either unnecessary (an `IORT` for a guest with
   no IOMMU driver) or cannot be written correctly until the interrupt path has
   produced a single working interrupt on hardware.

The direction has one more consequence worth stating plainly: the vGIC's
weaknesses were invisible to the firmware on purpose. Making them visible
requires a *conformance self-test*, which is the milestone after this one.

---

## Why the firmware never caught this

**Verified**, from the firmware tree this repository builds:
`Silicon/ARM/TIANO/ArmPkg/Drivers/ArmGicDxe/GicV3/ArmGicV3Dxe.c`.

`GicGetCpuRedistributorBase()` starts at `PcdGicRedistributorsBase`, reads
`GICR_TYPER`, compares the affinity against `ArmReadMpidr()`, and steps by
`GIC_V3_REDISTRIBUTOR_GRANULARITY` (`0x20000`) until either the affinity matches
or `GICR_TYPER.LAST` is set. It is called once, from
`ArmGicV3DxeInitialize()`, on the boot CPU.

Three things follow, and all three are properties of the code, not of luck:

- The firmware only ever *reaches* the frame that belongs to the boot CPU. The
  first frame matches, so the loop exits on its first iteration.
- The firmware never writes `GICR_WAKER`. The string does not appear in the
  driver or in `ArmPkg/Include/Library/ArmGicLib.h`.
- The firmware never polls `RWP`. It enables interrupts and returns.

So a vGIC where every redistributor frame aliases frame 0, where `GICR_WAKER`
never settles, and where `RWP` sticks at 1 is a vGIC the firmware boots through
without a single warning.

Windows is not that guest. Its HAL walks the redistributor region to build a
per-CPU map, wakes each PE through `GICR_WAKER`, and treats a register that never
changes as a hang. **Inferred** from the architectural contract in ARM IHI 0069
and from the firmware's opposite behaviour; **unproven** in the sense that no
Windows kernel has been observed doing it on this machine.

---

## Defects the audit found

All **verified** against the pinned `m1n1-windows` tree, upstream Linux
`arch/arm64/kvm/` and `include/linux/irqchip/arm-gic-v3.h`, and QEMU
`hw/intc/gicv3_internal.h`. All fixed in `patches/m1n1-windows/0004-vgic-guest-contract.patch`
and all **machine-checked** by `scripts/check-vgic.py`.

| # | Defect | What a guest sees | Rule |
|---|---|---|---|
| 1 | `relative_addr = addr - redist_base`, `cpu_num = ctx->cpu_id` | Every frame after the first decodes as an unknown register and aborts. CPU 1's `GICR_WAKER` arrives as `0x20014`. | One redistributor per PE, `0x20000` stride |
| 2 | `GICR_WAKER` write stored verbatim | `ChildrenAsleep` (RO, bit 2) stays set through a read-modify-write, so the documented "clear `ProcessorSleep`, poll `ChildrenAsleep == 0`" sequence never completes | IHI 0069, `GICR_WAKER` |
| 3 | `GICD_CTLR.RWP` set on group disable and on `GICD_ICENABLER`, never cleared | A guest polling `RWP` spins forever | `RWP` reads 1 only while a write is in flight |
| 4 | `GICR_CTLR` wrote bit 31 (`UWP`) instead of bit 3 (`RWP`) | `UWP` is RES0 when `VLPIS` is 0; `RWP` never set at all | IHI 0069, `GICR_CTLR` |
| 5 | `GICR_TYPER.PLPIS`, `DirectLPI`, `DPGS` set; `GICR_SETLPIR` a no-op | Guest allocates an LPI configuration table and waits for an interrupt that cannot arrive | Advertise only implemented features |
| 6 | `GICD_TYPER.LPIS` set with no ITS | Guest looks for a command queue that does not exist | same |
| 7 | `GICD_ICENABLER` called `aic_set_mask(irq, false)` (unmask) while logging "masking" | Every interrupt the guest disables stays live and is re-injected | Disable means mask |
| 8 | Enable/disable/`IROUTER`/EOI passed guest INTIDs to the AIC | SPI 512 touches AIC 512 instead of the APCIE source at 1506; INTIDs < 32 have no AIC line at all | Translate at the boundary |
| 9 | `IROUTER` stored `smp_get_id()` in a `u32` | `-1` becomes `0xffffffff`, and `aic_set_affinity()` expands it as `BIT(cpu)` | Signed until proven otherwise |

Defect 8 is the one that composes with the PCIe work: `0003` built the MSI
translation in the *injection* direction (`guest_msi_intid()`), and `0004` adds
the inverse (`guest_msi_aic_irq()`) for the *control* direction. A translation
that only works in one direction is worse than none, because it looks right in a
log.

---

## Ruled out

These were investigated and are **not** bugs. They are recorded so they do not
get re-litigated.

- **Timer GSIV mismatch.** The generic timer PPIs are injected as INTIDs 17 and
  18 by `hv_exc.c`, which is not the architectural 27/30. **Verified**: the
  firmware's `GTDT.aslc` assigns them deliberately ("we will assign an arbitrary
  interrupt number (17 currently)") and `scripts/check-acpi.py` checks the built
  table against 17/18. The firmware and hypervisor agree; the numbers are private
  to this platform.
- **`ICC_SGI1R_EL1` trapping.** `ICH_HCR_EL2` is set to `InEn|UIE`
  (`BIT(0)|BIT(2)`), not `TALL1`. **Verified** against upstream KVM
  (`arch/arm64/kvm/vgic/vgic-v3.c`): `TALL0`/`TALL1` are only forced for a
  GICv2-on-GICv3 model or when there is no in-kernel irqchip. With `En=1` the
  SGI register traps architecturally, so the handler in `hv_exc.c` is reachable.
- **`ICH_VMCR_EL2 = BIT(1)`.** This sets `VENG1` and leaves `VPMR` at 0.
  **Verified**: `VPMR = 0` is the architectural reset value, and guest `PMR`
  writes land in `VPMR` in hardware.
- **Redistributor stride.** `0x20000` is correct here because `VLPIS=0`. GICv4
  would make it `0x40000`. **Verified** against QEMU's `GICR_TYPER_VLPIS` case.
- **`aic_set_affinity()` on M4.** It returns immediately unless
  `aic->version == 1`. **Verified** in `src/aic.c`; AICv3 on T8132 is not
  version 1. This means `GICD_IROUTER` affinity writes currently do nothing.
  That is a real limitation, but it is a *feature gap*, not a contract violation:
  the guest is told the truth as long as `GICD_TYPER` reports one-of-N SPI
  routing. Windows distributes SPIs across CPUs as an optimisation; a fixed
  routing is slow, not broken.

---

## Options considered

### A. vGIC guest-contract correctness *(chosen)*

Fix the register interface so a strict guest can program it. Delivered as
`0004`, plus `check-vgic.py` and its tests.

- **Value:** every later milestone — Windows handoff, PCIe interrupts, storage,
  input — runs through this interface. A HAL that hangs here produces no log, no
  bugcheck and no clue.
- **Evidence strength:** high. Each defect is checked against the architecture
  and against upstream KVM/QEMU, not against intuition.
- **Cost:** one patch, no hardware.
- **Risk:** the emulation is still not a GIC. It shares Apple's physical CPU
  interface, so guest `ICC_*` register behaviour is whatever Apple implements.

### B. Boot-time vGIC conformance self-test in UEFI *(next)*

A small DXE application that exercises the contract the way a strict OS would:
walk every redistributor frame via `GICR_TYPER`, read-modify-write `GICR_WAKER`
on each and poll `ChildrenAsleep` with a timeout, confirm `GICD_CTLR.RWP` and
`GICR_CTLR.RWP` read 0, enable the GTDT timer PPI, send a self-SGI, and report
one line per check.

- **Value:** converts **inferred** to **verified** without needing Windows in the
  loop, and produces a pass/fail that is meaningful on the first hardware run.
- **Cost:** a new firmware module plus the build wiring; no hypervisor change.
- **Risk:** none to correctness; it is read-mostly and runs before any loader.
- **Dependency:** the machine, because it is the only way to execute it.

### C. Single-CPU diagnostic build

Force `num_cpus = 1` and boot.

- **Value:** turns "no output" into "the BSP got this far".
- **Cost:** trivial.
- **Risk:** high, if used early. It hides exactly the class of bug in defect 1 by
  construction, and it diverges from the SMP configuration Windows expects. Use
  it only after a hardware log exists and only to bisect.

### D. More ACPI: `IORT`, `_PRT`, `_OSC`, the `apciec` complexes

- **Value:** needed eventually. `_PRT` describes INTx, which APCIE does not
  raise; `IORT` describes an SMMU, which this guest has no driver for and whose
  DARTs are in bypass.
- **Cost:** substantial; every table is another thing that must match hardware.
- **Risk:** writing a table for behaviour no one has observed is how the PCIe
  translation sign got inverted twice. **Deferred** until interrupts work; the
  `apciec` complexes stay deferred until the first root complex enumerates.

### E. Hardware capture first

- **Value:** the only way to see the true failure mode.
- **Cost:** blocked. Needs the machine, a data-capable USB-C cable, serial
  capture and a second Mac for DFU recovery.
- **Verdict:** not an option now; it is the gate that everything else queues
  behind.

### F. Emulated ITS

- **Value:** the "proper" way to deliver MSIs on GICv3.
- **Cost:** an ITS command queue, device/interrupt translation tables, and
  `GITS_TRANSLATER` writes the guest would have to make.
- **Fatal problem:** Apple's hardware never writes `GITS_TRANSLATER`. APCIE
  raises MSIs through its own doorbell and the AIC, so an emulated ITS would
  describe a path no device in the system can use. **Rejected**, and the GIC MSI
  Frame in the MADT exists precisely because of it.

### G. VBS/HVCI groundwork (nested virtualisation)

- **Value:** on the long-term path to anti-cheat.
- **Cost:** `ICH_HCR_EL2` is already owned by this hypervisor; Windows' hypervisor
  would need EL2, and m1n1 would have to become an EL2 host for it (FEAT_NV/NV2).
- **Verdict:** a research project, gated on a booting Windows. **Not now.**

---

## Decision matrix

Scored 1–5, higher is better. "Evidence" is how much of the decision rests on
facts rather than guesses.

| Option | Unblocks | Evidence | Cost (5 = cheap) | Hardware-gated | Score |
|---|---|---|---|---|---|
| A. vGIC contract | everything downstream | 5 | 4 | no | **18** |
| B. UEFI conformance self-test | first hardware run | 4 | 3 | partly | **12** |
| C. Single-CPU build | diagnosis only | 2 | 5 | yes | 8 |
| D. More ACPI | PCIe polish | 2 | 2 | partly | 6 |
| E. Hardware capture | truth | 5 | 1 | yes | 10 |
| F. Emulated ITS | nothing | 1 | 1 | no | 2 |
| G. Nested virt | anti-cheat, far | 2 | 1 | yes | 3 |

The ordering that falls out is A → B → (E when available) → D, with C kept in
reserve as a bisection tool.

---

## Recommended sequence

### Offline, no hardware

1. **Ship `0004` and the vGIC contract checker.** *(done)*
   `scripts/check-vgic.py` asserts the contract against the patch series (no
   submodule) and against patched sources; `tests/test_check_vgic.py` puts each
   defect back and requires the checker to fail.

2. **Turn vGIC logging into an opt-in diagnostic.** *(done in this change)*
   `hv_vgic.c` defined `ENABLE_VGIC_LOGGING 1`, so `vgic_log()` was a `printf()`
   on **every emulated register access**. **Verified.** At 115200 baud a 60-byte
   line costs about 5 ms, so a Windows HAL programming ~1020 INTIDs plus a
   redistributor walk per CPU spent minutes inside the console; that is not a
   slow boot, it is a stall indistinguishable from the bug the logging existed to
   find.

   `0005-vgic-diagnostics.patch` compiles the trace out and replaces it with
   three mechanisms, chosen because they answer three different questions:
   a 64-entry ring of recent accesses (*what did the guest do just before it
   stopped?*), one-bit-per-8-bytes touched bitmaps per space (*did it ever touch
   this register?*), and a 32-line budget for genuinely exceptional events
   (*something is wrong, tell me now, but do not let a spinning guest flood the
   UART*). Everything lives in the global `vgic_diag`, so it is readable over the
   proxy **after** a hang — the case that matters, because a hung guest prints
   nothing. Field offsets and a Python reader are in `docs/vgic-diagnostics.md`.

   The severity rule is not new invention: the source already tagged messages
   `[INFO]`, `[WARN]`, `[ERROR]`. 0005 makes behaviour match the tag, and
   `scripts/check-vgic.py` fails if a later patch restores the per-access
   printing, prints instead of recording, or lets an exceptional site bypass the
   budget. **machine-checked.**

3. **Decide the `IROUTER` question honestly.** `aic_set_affinity()` is a no-op on
   AICv3. Either find the AICv3 redirection mechanism, or report
   `GICD_TYPER.No1N = 1` and accept fixed SPI routing. Right now the vGIC
   advertises one-of-N routing it does not implement, which is defect 5/6 in a
   different register.

4. **Write the UEFI conformance self-test (option B)** so it is in the image
   before the first hardware boot. It costs nothing offline and is the difference
   between "the firmware booted" and "the firmware proved the interrupt contract".

### Hardware-gated

5. Boot m1n1, then the firmware, and capture the serial log (`docs/bringup-plan.md`
   phases 1–2). The vGIC self-test runs here and its output is the first real
   data this project will have about the M4 interrupt path.

6. Only then: `_PRT`/`IORT`/`_OSC` and the `apciec` complexes, informed by what
   the log says about the first root complex.

### Explicitly deferred

- Emulated ITS (option F) — the hardware cannot feed it.
- Nested virtualisation (option G) — needs a booting Windows first.
- Single-CPU builds (option C) — a diagnostic, not a configuration.

---

## Risks

| Risk | Consequence | Mitigation |
|---|---|---|
| The contract fixes are right but incomplete | Windows hangs in the vGIC anyway, with a different bug | The self-test (step 4) enumerates the whole surface, not just the fixed registers |
| Apple's CPU interface diverges from the architecture in ways only a real guest finds | Guest `ICC_*` behaviour differs from what the emulated distributor implies | Nothing offline can detect this; it is what hardware is for |
| `ENABLE_VGIC_LOGGING` is turned back on | First boot looks like a hang at the first interrupt | Step 2 shipped `0005`; the checker fails if any patch restores it |
| The ACPI tables describe the vGIC accurately but Windows ignores the MSI frame | MSI never works | `check-acpi.py` already pins the frame against `guest_msi.h`; the self-test should read the doorbell back |
| Deterministic-build claim is mistaken | Patches and artifacts drift | The series reproduces the working tree and rebuilds to `m1n1.macho = 667cd3ce…`, `m1n1.bin = 20f98b16…` (**verified** on 2026-10-27) |
| The diagnostics are mistaken for a conformance test | A green log is read as "the vGIC is correct" | The ring records what the guest *did*, not whether it was correct; the conformance question is the UEFI self-test (step 4) |

---

## What would falsify this plan

The plan is wrong if any of these turns out to be true on hardware:

- The firmware's `ArmGicV3Dxe` *does* write `GICR_WAKER` on M4 — then defect 2
  was reachable from UEFI and the boot would have failed there, meaning something
  else explains the earlier hang.
- Windows boots past the HAL with the pre-`0004` vGIC — then the contract defects
  were not on the critical path and the effort belonged elsewhere.
- `GICD_TYPER` reporting a single redistributor region makes Windows ignore
  frames 1–9 entirely — then defect 1 was unreachable and the fix is hygiene,
  not a milestone.
- The AICv3 has no per-interrupt redirection at all and Windows refuses to run
  with fixed SPI routing — then `IROUTER` moves from hygiene to blocker.

Each of these is cheap to check once the machine is available, which is the point
of putting the self-test in the image first.

---

## References

- ARM IHI 0069, *GIC architecture version 3.0 and version 4.0*: `GICR_WAKER`,
  `GICR_CTLR`, `GICR_TYPER`, `GICD_CTLR`, `GICD_TYPER`, redistributor frame layout.
- `include/linux/irqchip/arm-gic-v3.h` and `hw/intc/gicv3_internal.h` in QEMU:
  bit positions and the `VLPIS` stride rule.
- `arch/arm64/kvm/vgic/vgic-v3.c` and `arch/arm64/kvm/sys_regs.c`: when
  `TALL0`/`TALL1` are used, and the SGI trap path.
- EDK2 `ArmGicV3Dxe.c` (vendored in `third_party/ntasp-firmware`): the firmware's
  redistributor walk, and the absence of any `GICR_WAKER` write.
- `docs/pcie.md`: the MSI contract between `guest_msi.h`, the MADT frame and the
  captured doorbell.
- `docs/hardware-values.md`: where each platform constant came from.
