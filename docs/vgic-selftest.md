# vGIC conformance self-test

The firmware image contains a driver that reads the emulated interrupt controller
back, the way a strict operating system would, and prints one line per property it
checked. It exists because the two halves of this project were each internally
consistent and still wrong together: the hypervisor emulated a GIC, the ACPI
tables described a GIC, and nothing ever asked whether the device behaved like
one. UEFI is a lenient guest, so a build that boots is evidence about UEFI, not
about the vGIC.

**Status:** builds with zero warnings, is present in the DXE FV, and is
machine-checked offline by `scripts/check-vgic-selftest.py` (94 checks against the
patch series, 93 against the tree). `scripts/check-gicv3-oracle.py` additionally
re-checks the register read-backs it assumes against QEMU's independent GICv3
model. **It has never run on the M4.** Everything it asserts about the device is
an expectation derived from ARM IHI 0069 and from the hypervisor's source; the run
is what turns that into a measurement.

---

## Where it runs, and why there

`AppleVgicSelfTestDxe`, `Silicon/Apple/AppleSiliconPkg/Drivers/AppleVgicSelfTestDxe`,
DXE driver, FILE_GUID `7b1f0a52-4c3e-4a8d-9f60-2d5c1e8b7a31`.

```
[Depex]
  gEfiCpuArchProtocolGuid AND gHardwareInterruptProtocolGuid
```

It is placed in the FV immediately **before** `TimerDxe`, inside
`!if $(AIC_BUILD) == FALSE`.

| Constraint | Reason |
|---|---|
| After `ArmGicDxe` | It needs `gHardwareInterruptProtocolGuid`, and the IRQ exception vector that driver installs. Without the vector, an SGI would be asserted and never taken. |
| After `CpuArchProtocol` | It maps the redistributor window it walks; `SetMemoryAttributes` needs the CPU arch protocol. |
| Before `TimerDxe` | The delivery tests register a handler for the virtual timer PPI. The interrupt protocol refuses a second registration for the same source, so `TimerDxe` must not own it yet. The test leaves `CNTV_CTL_EL0` disabled, which is the state `TimerDxe` expects. |
| GIC build only | The `AIC_BUILD` image has no vGIC at all; the driver would read Apple's AIC window as if it were a distributor. |

It reads no ACPI table. The tables are not installed at dispatch time, so the
addresses come from the same PCDs the tables are generated from
(`PcdGicDistributorBase`, `PcdGicRedistributorsBase`, `PcdCoreCount`,
`PcdArmArchTimerVirtIntrNum`), which is also why the GTDT and this test cannot
disagree.

---

## Log format

Every result is one line, `DEBUG_INFO` over `AppleUartSerialPortLib`:

```
vgic: [PASS] dist.intids: GICD_TYPER describes 1024 INTIDs
vgic: [PASS] dist.igroupr: SPI 512 reads as Group 1 under DS = 1
vgic: [PASS] redist.enable-w0: a write of 0 to GICR_ISENABLER0 leaves SGI 13 enabled
vgic: totals: pass=60 fail=0 gap=0 skip=1
vgic: RESULT: PASS
```

| Kind | Meaning |
|---|---|
| `[PASS]` | The device behaved as the architecture requires. |
| `[FAIL]` | It did not, and the difference is a defect a guest would hit. |
| `[GAP ]` | It did not, and the difference is a known limitation of the hypervisor. Reported, counted, not fixed in the change that added the test. |
| `[SKIP]` | The check could not run (no second PE, no timer frequency, no protocol). Not a pass. |

`RESULT: FAIL` if any `[FAIL]`, else `PASS-WITH-GAPS` if any `[GAP ]`, else
`PASS`. No check halts the boot and no check hangs: every wait is a `for` loop
bounded by an `Elapsed < TimeoutUs` header, and the checker refuses a `while` in
this file at all. A conformance test that can stop the boot is worse than none,
because its symptom — a boot that ends two lines into the log — is exactly the
symptom it was written to diagnose.

`[GAP ]` has a trailing space so the four kinds are the same width and a captured
log can be column-diffed between boots.

---

## What it checks

### Distributor capability registers (`VgicCheckDistributorCaps`)

| Check | Asserts |
|---|---|
| `dist.intids` | `GICD_TYPER.ITLinesNumber` describes 1024 INTIDs. The MADT GIC MSI Frame numbers its vectors up to SPI 767; a distributor with fewer lines would not represent them. |
| `dist.idbits` | `IDbits = 0xF`, i.e. 16 bits, matching the CPU interface. |
| `dist.no1n` | `No1N = 1`. This is the runtime half of patch 0006: the vGIC does not select a target PE, so it must not advertise 1-of-N routing. |
| `dist.a3v` | `A3V = 0`. Every MPIDR in the ADT has Aff3 = 0, so affinity level 3 is not part of the topology. |
| `dist.no-secure` | `SecurityExtn = 0`, which is what `GICD_CTLR.DS = 1` implies. A guest that sees SecurityExtn = 1 will try to program Group 0 / Group 1 Secure. |
| `dist.no-lpis` | `LPIS = 0` and `MBIS = 0`. There is no ITS, so a guest that believes in LPIs will program a command queue that nothing services. |
| `dist.cpunumber` | `CPUNumber = 0` with affinity routing enabled (it is RES0 then). |
| `dist.typer2` | `GICD_TYPER2` exists only from GICv4.1 and reads 0 here. |
| `dist.msi-window` | SPIs 512–767 fit inside the INTID count. This is the seam between the ACPI claim and the device: `check-acpi.py` pins the table against `src/guest_msi.h`, this pins the table's window against the distributor. |

### Distributor control (`VgicCheckDistributorControl`)

| Check | Asserts |
|---|---|
| `dist.ds` | `GICD_CTLR.DS = 1`: one security state, all interrupts Group 1. |
| `dist.are` | `ARE = 1`: affinity routing is enabled, and legacy routing is not modelled. |
| `dist.grp1` | `EnableGrp1NS = 1` at dispatch time. |
| `dist.ds-are-forced` | A write of 0 leaves DS and ARE set. A guest is allowed to try to clear them; silently clearing them would break every later write. |
| `dist.rwp` | `RWP` reads 0 once the write has been applied. **A guest that polls RWP would spin forever if this were not true**, and the spin would look like a hang in the guest. |
| `dist.restore` | `GICD_CTLR` reads back the value it was found with, so the test is not a configuration change. |

### One SPI's banks (`VgicCheckSpiRegisters`, SPI 512)

| Check | Asserts |
|---|---|
| `dist.igroupr` | The SPI reads as Group 1. Under `DS = 1`, `GICD_IGROUPR` is RAZ/WI and must read all ones. The emulation does not model it, so this is a **measured gap**, not a failure. |
| `dist.enable` / `dist.disable` | `GICD_ISENABLER` sets and `GICD_ICENABLER` clears the bit. |
| `dist.pending` / `dist.unpending` | `ISPENDR` / `ICPENDR` round-trip. |
| `dist.active` / `dist.unactive` | `ISACTIVER` / `ICACTIVER` round-trip. |
| `dist.priority` | A priority written as `0xa5` reads back as `0xa5`. The probe uses a 32-bit read-modify-write of `GICD_IPRIORITYR`, because the emulator decodes accesses at the width the instruction used, and a byte access would test the hook's width handling instead of the register. |

Every write is undone before the next check.

### Routing (`VgicCheckIrouter`, SPI 512)

| Check | Asserts |
|---|---|
| `dist.irouter` | A write of `Aff0:Aff1:Aff2` reads back unchanged (with IRM masked out). |
| `dist.iroute-normalised` | A write with `IRM = 1` reads back with `IRM = 0`. `0006` implements this as the behaviour IHI 0069 permits when `No1N = 1`; a read-back that kept IRM = 1 would tell the guest distributor-side selection is in use. |
| `dist.irouter-affinity` | A cross-PE affinity round-trips, and the line says plainly that it is bookkeeping only: on AICv2/v3 no software path can move an SPI. Reported as `[GAP ]` when a second PE exists, `[SKIP]` when it does not. |

### The Windows HAL's distributor teardown (`VgicCheckWindowsHalSequence`)

This group is **reverse-engineered, not vendor-documented**. Connor McGarr's
public analysis of the Windows ARM64 HAL ([*Pardon The Interruption*](https://connormcgarr.github.io/windows-arm64-interrupts/))
reports that `nt!HalpGic3InitializeIoUnit`, before it enables anything, clears
`GICD_CTLR.EnableGrp1NS`, writes `0xFFFFFFFF` to every `GICD_ICENABLER<n>` bank
and zeroes every `GICD_IROUTER<n>`. It is one report on one GICv3 machine, not a
Microsoft contract, and the article itself contains at least one register-name
error (it calls `ICC_SRE_EL1` bit 1 the SRE bit; `SRE` is bit 0). The group is
worth running anyway: these are the bulk writes a real HAL issues, and this vGIC
had never been asked to absorb them. A guest that stops in the middle of this
sequence prints nothing.

| Check | Asserts |
|---|---|
| `windows.grp1` | `EnableGrp1NS` can be cleared and restored. Since patch `0007` the injection path consults this bit, so the guest's temporary disable now stops delivery instead of being advisory. |
| `windows.icenabler` | `0xFFFFFFFF` to all eight `GICD_ICENABLER` banks of the MSI window disables all 256 SPIs, read back through `GICD_ISENABLER`. Every bit has to translate to an AIC vector before it can be masked, and again before it can be unmasked: this is patch 0004's inverse translation at full width. |
| `windows.irouter-zero` | A zeroed `GICD_IROUTER` reads back as zero. Zero names the PE with affinity 0, so it is not a neutral value, but the HAL writes it before it has described any line. |
| `windows.restore` | The MSI window's enables, the two probed routes and `GICD_CTLR` all return to the values they had before the replay. |

Only the MSI window is replayed. Writing `0xFFFFFFFF` to every bank across the
whole distributor would also disable lines the firmware's USB and PCIe stack may
be using, and the interesting half is the window where an INTID has to map back
to an AIC number.

### Redistributors (`VgicWalkRedistributors`, `VgicCheckRedistributorFrames`)

Walking every frame is the part `ArmGicV3Dxe` never does: it stops at the first
frame whose affinity matches the boot PE, so on this platform nothing has ever
read frames 1–9.

| Check | Asserts |
|---|---|
| `redist.last` | `GICR_TYPER.LAST` is set exactly once, and the walk stops there. A walk that never sees LAST either runs off the mapped frames or reads somebody else's memory. |
| `redist.frames` | The number of frames found equals `PcdCoreCount` and the MADT's GICC count. |
| `redist.affinity` | One frame reports this PE's MPIDR. The frame index is printed, because it is not necessarily 0: an SGI enable bit must be written to *this PE's* frame. |
| `redist.affinity-unique` | No two frames report the same affinity, which would make a guest bind two redistributors to one PE. |
| `redist.procnum-unique` | `GICR_TYPER.Processor_Number` is unique. IMPLEMENTATION DEFINED, and nothing without an ITS depends on it, so a duplicate is a gap rather than a failure. |
| `redist.caps` | No frame advertises `VLPIS`, `PLPIS`, `DirectLPI` or `DPGS`. |
| `redist.ctlr-razwi` | Writing `EnableLPIs`, `DPG0`, `DPG1NS`, `DPG1S` and the RES0 bits to `GICR_CTLR` leaves them reading 0. `EnableLPIs` with no LPI space is the one that matters: accepting it tells a guest LPI delivery exists. |
| `redist.waker-wake` | The guest's wake sequence completes: read `GICR_WAKER`, clear `ProcessorSleep`, write, poll `ChildrenAsleep` until 0. This is the exact sequence patch 0004 fixed, and it is polled because `ChildrenAsleep` is read-only and a read-modify-write that preserved a stale value would never settle. |
| `redist.waker-sleep` | Setting `ProcessorSleep` again brings `ChildrenAsleep` back with it, so the field is not stuck at zero. Reported as a gap if the model leaves the PE awake. |
| `redist.ctlr-waker` | Summary line: every frame accepted a wake inside the timeout with `GICR_CTLR` reserved bits clear. |

Frames are left awake, which is the state a guest expects to find them in.
`ArmGicV3Dxe` never writes `GICR_WAKER`, so before this driver ran, all ten frames
were still at their reset value.

### Redistributor SGI/PPI banks (`VgicCheckRedistributorBanks`)

This is the per-PE half of the distributor's bank checks, on the boot PE's own
frame, using SGI 13 so the delivery tests' SGIs are untouched. Both halves of each
set/clear pair are read back, because a guest sees them as one state and an
emulation that keeps them in separate places is wrong the moment the guest
reads the alias it did not write.

| Check | Asserts |
|---|---|
| `redist.enable` | Writing `GICR_ISENABLER0` enables the bit. |
| `redist.enable-w0` | A write of **0** to `GICR_ISENABLER0` leaves it enabled. This is the check that separates write-1-to-set from a raw store; before patch `0007` the emulation stored the raw value, so a second write could silently disable a line. |
| `redist.disable` / `redist.disable-alias` | `GICR_ICENABLER0` clears the bit, and both enable aliases read the cleared state. |
| `redist.pending` / `redist.pending-alias` | `GICR_ISPENDR0` sets pending, and `GICR_ICPENDR0` reads the same bitmap. |
| `redist.unpending` | `GICR_ICPENDR0` clears pending. |
| `redist.active` / `redist.active-alias` | `GICR_ISACTIVER0` sets active, and `GICR_ICACTIVER0` reads the same bitmap. |
| `redist.deactivate` | `GICR_ICACTIVER0` clears active. |
| `redist.igroup` | `GICR_IGROUPR0` reports Group 1 for the bit, the group every virtual interrupt is injected as. |
| `redist.igroup-forced` | A write that tries to select Group 0 is ignored. Delivery has no Group 0 path, so a register that accepted the write would describe an interrupt routing that never happens. |

Before `0007` the enable and pending handlers in this region read and wrote the
**active** bank, and `GICR_ICENABLER0` never cleared the enable bit at all. A
write-only guest could not see any of that. The probe exists because a real guest
reads back.

### CPU interface (`VgicCheckCpuInterface`)

| Check | Asserts |
|---|---|
| `cpu.eoimode` | `ICC_CTLR_EL1.EOImode = 0`, i.e. `ICC_EOIR1_EL1` both drops priority and deactivates. The hypervisor assumes that. |
| `cpu.igrpen1` | `ICC_IGRPEN1_EL1.EnableGrp1 = 1`. If Group 1 were disabled, no guest interrupt could be taken at all. |
| `cpu.pmr` | `ICC_PMR_EL1` is writable and reads back. Restored afterwards. |
| `cpu.idle` | `ICC_HPPIR1_EL1` reports 1023 (spurious) before any test interrupt. `HPPIR1` is read-only, so this probe cannot consume an interrupt the way a read of `IAR1` would. |

### Delivery (`VgicCheckSgiDelivery`, `VgicCheckSgiGating`, `VgicCheckPpiDelivery`)

These are the only checks that go through the real exception path, and they are
why the driver has to run where it does. `ICC_SGI1R_EL1` traps to EL2, m1n1
queues the SGI and sends itself an Apple IPI, the IPI handler injects a list
register, and the architected virtual CPU interface takes over: the UEFI IRQ
vector reads `ICC_IAR1_EL1`, calls the registered handler, and the handler writes
`ICC_EOIR1_EL1`. If that chain works, the chain a guest OS depends on works.

| Check | Asserts |
|---|---|
| `delivery.sgi` | A self-SGI (INTID 9) arrives, twice, so a list register was free the second time. |
| `delivery.sgi-eoi` | After EOI, `HPPIR1` returns to spurious, i.e. the list register was reclaimed. A handler that forgets EOI would pass the first delivery and fail the second. |
| `gating.sgi` | A **disabled** SGI (INTID 10) is not delivered. The protocol enables the source when a handler is registered, so the test clears `GICR_ISENABLER0` in the boot PE's own frame behind the protocol's back and sends the SGI. Delivery that ignores the enable bit is reported as a gap. |
| `gating.pending` | The SGI refused above is not lost: the test sets `GICR_ISENABLER0` and waits **without sending again**. A vGIC that dropped the interrupt instead of queueing it leaves this silent, which for an edge-triggered source can be forever. This is a `[FAIL]`, not a gap: the pending state is part of the contract, and the check is the only one that distinguishes "queued" from "dropped". |
| `gating.rearm` | The same SGI is enabled again and sent again, and has to arrive. A gate that refuses forever would be worse than no gate: this is the failure mode of testing the enable bit before the guest has programmed it, and it is the reason the check is a `[FAIL]`, not a gap. |
| `gating.group1` | The **other half** of the gate: with the SGI's own enable bit set, the test clears `GICD_CTLR.EnableGrp1` and sends it. Every list register is injected as Group 1, so delivery while that bit is clear is reported as a gap. The register is written back exactly; DS and ARE are preserved. |
| `gating.group1-pending` | Restoring `GICD_CTLR.EnableGrp1` delivers the SGI sent while the group was disabled, **without a resend**. A vGIC that discarded it leaves the guest waiting; the check is a `[FAIL]`. |
| `delivery.ppi` | The virtual timer PPI (from `PcdArmArchTimerVirtIntrNum`, 18 on this platform) is programmed 5 ms out and delivered. It is then stopped again for `TimerDxe`. |

The handler calls `EndOfInterrupt` itself. That is not optional: EDK2's
`GicV3IrqInterruptHandler` writes `ICC_EOIR1_EL1` only when **no** handler is
registered for the acknowledged INTID, so a registered handler owns the EOI. A
handler that does not EOI leaves the interrupt active and the next one is never
taken.

Interrupts are enabled only for the delivery section, and `PSTATE.I` is restored
to whatever it was on entry.

---

## What it deliberately does not do

These are limits, not omissions discovered later. Each one is written down so a
green log is not read as more than it is.

1. **No SPI or doorbell delivery.** The obvious way to test the MSI window is to
   write the GIC MSI Frame doorbell at `0xfffff000`. It is not mapped into the
   guest, and `hv_handle_dabort()` treats an unmapped IPA as fatal, so the test
   would convert a conformance check into a boot stop. The MSI window is therefore
   checked as a *description* (`dist.msi-window`) and the delivery half stays a
   hardware question for the first PCIe driver that raises a real MSI.
2. **No measurement of where an SPI lands.** `dist.irouter-affinity` records that
   a cross-PE affinity round-trips; it cannot make an SPI arrive on that PE,
   because AICv2/v3 expose no software-programmable per-SPI CPU target
   (`docs/architecture.md`). Measuring the delivering PE needs a real SPI source,
   which is PCIe.
3. **No SMP affinity measurement.** The test runs on the boot PE only. It reads
   every frame, but it never wakes a *remote* PE and never sends it an SGI.
4. **No unmodelled redistributor probe.** The distributor hook tolerates an
   unknown offset; the redistributor hook returns false, and a false from a
   stage-2 hook is a fatal guest abort. The test therefore never touches an offset
   it does not know is modelled — including `GICR_TYPER2`, which does not exist
   (offset `0xC` is `GICR_TYPER[63:32]`). That asymmetry is a defect in the
   hypervisor's error handling, and it is the reason this test enumerates its
   register offsets instead of exploring.
5. **No assertion that Windows accepts the MSI frame.** The platform advertises
   SPIs 512–767 as a MADT GIC MSI Frame and the hypervisor translates them, but
   public evidence that Windows on ARM64 binds PCI MSI/MSI-X to a type-`0x0D`
   frame *without* an IORT or an ITS is not available. Microsoft documents GIC
   and generic PCI MSI support; a public Windows ARM64 analysis shows a frame in
   a real MADT; neither shows the provider binding. The replay above measures
   that the device survives the HAL's writes, not that the OS will use the
   window. `docs/pcie.md` records this as an open risk, and the first PCIe
   driver that raises a real MSI is the experiment that settles it.
5. **No LPI or ITS behaviour**, because the platform has neither and deliberately
   says so.

## Gaps: what `0007` closed, and what remains

Patch `0007-vgic-enable-gating.patch` closed the three gaps the previous revision
of this document predicted. They are not expected to appear in a log any more:

| Was | Check | What changed |
|---|---|---|
| `GICD_IGROUPR` read 0 under `DS = 1` | `dist.igroupr` | The distributor initialises every modelled SPI's group bit to Group 1, and ignores a write that tries to select Group 0, because delivery has no Group 0 path. |
| SPI/PPI delivery ignored `GICD_ISENABLER` / group enables | PPI path exercised by `delivery.ppi` | `hv_vgic3_inject_irq()` now consults `GICD_CTLR.EnableGrp1` and the SPI's `GICD_ISENABLER` bank. A refused interrupt stays queued instead of being dropped. |
| SGI/PPI delivery ignored `GICR_ISENABLER0` | `gating.sgi` | The same predicate reads the taking PE's own redistributor frame for INTIDs below 32. |

Closing the gate exposed its other half. Refusing to inject is only correct if
the interrupt stays pending, and the original queues stopped at their first
refusal: one disabled INTID at the head would have blocked every interrupt
behind it. The drain now compacts instead of popping at the first failure, and
enabling an INTID (`GICR_ISENABLER0`, a `GICD_ISENABLER` bank, or
`GICD_CTLR.EnableGrp1`) drains the queues immediately rather than waiting for the
next unrelated interrupt. `gating.pending` is the probe that measures it, and
`gating.group1` / `gating.group1-pending` measure the group half of the same
predicate in both directions.

What remains is stated rather than hidden:

- **`GICR_ISPENDR0` is state, not a trigger.** A guest write sets the pending bit
  and both aliases read it back, but this vGIC raises guest interrupts from
  `ICC_SGI1R_EL1` and the AIC, not from that bank. A guest that raises an SGI only
  through `ISPENDR0` would set a bit and wait. No known guest does; the probe
  checks the read-back contract, not delivery from the write.
- **Group 0 is not implemented.** The group registers force Group 1 because every
  injection sets `ICH_LR_GRP1`. A guest that wants an FIQ-backed Group 0 SPI is
  told, by the read-back, that the line is Group 1.
- **SPI affinity is still bookkeeping.** AICv2/v3 have no software-programmable
  per-SPI target, so `GICD_IROUTER` decodes and stores an affinity the hardware
  cannot act on. `dist.irouter-affinity` reports that as a gap when a second PE
  exists.

When a gap is fixed, its check becomes a `[PASS]` and the checker's expectations
change in the same commit, which is the point of pinning behaviour rather than
prose.

---

## Reading a run

Expected shape on the M4:

```
vgic: ===== vGIC conformance self-test =====
vgic: dist = 0x5000000000, redist = 0x5100000000, 10 frames declared, boot MPIDR 0x...
vgic: dist.TYPER = 0x..., GICD_IIDR = 0x...
vgic: [PASS] dist.intids: GICD_TYPER describes 1024 INTIDs
...
vgic: [PASS] gating.sgi: a disabled SGI 10 was correctly not delivered
vgic: [PASS] gating.pending: an SGI 10 sent while disabled was delivered when its bit was set
vgic: [PASS] gating.group1: an SGI 10 sent while Group 1 was disabled was not delivered
vgic: [PASS] gating.group1-pending: an SGI 10 sent while Group 1 was disabled was delivered when it was re-enabled
vgic: cpu virt-timer PPI 18
...
vgic: -----
vgic: totals: pass=.. fail=.. gap=.. skip=..
vgic: RESULT: ...
vgic: ===== end self-test =====
```

How to read it:

- **No `vgic:` lines at all.** The driver is not in the FV, or its depex never
  became true, or `PcdDebugPrintErrorLevel` filters `DEBUG_INFO` (it is
  `0xFFFFFF4F`, so it does not). Check the build script's `FV contains
  …AppleVgicSelfTestDxe` line first.
- **A run that stops mid-way** is the one shape this design cannot produce: every
  wait is bounded and no check halts. A stop is therefore a guest abort or a
  triple fault, not this driver waiting. The last printed line says which register
  was being touched.
- **`dist.*` failures** point at the emulated distributor (`src/hv_vgic.c`).
- **`redist.*` failures** point at the frame decode or the wake state machine.
- **`cpu.*` failures** point at the CPU interface, which is Apple's hardware, not
  the emulation. A failure there is not something m1n1 can patch; it changes what
  the guest can be told.
- **`delivery.*` failures** point at the trap/queue/IPI path (`hv_exc.c`).
- **`setup: cannot map …`** means the platform's memory map does not cover the
  interrupt controller at the address the PCDs give, which would be an ACPI/memory
  map defect, not an interrupt one.

The redistributor walk's frame count is the single most useful number in the log:
it is the first direct evidence that the ten MADT CPUs and the ten emulated
frames agree.

---

## Reproducing the checks offline

```bash
./scripts/check-vgic-selftest.py                         # the shipped patch series
./scripts/check-vgic-selftest.py --tree third_party/ntasp-firmware
./scripts/check-gicv3-oracle.py                          # QEMU's GICv3, 26 probes
python3 -m unittest tests.test_check_vgic_selftest -v     # the checker's own tests
```

The oracle is the independent half of the argument. This driver and `hv_vgic.c`
are two descriptions of one device; QEMU's GICv3 is a third, written by someone
else against the same architecture. `scripts/check-gicv3-oracle.py` drives it
over qtest, without booting a guest, and asserts the read-backs this document
relies on: that `ICENABLER0` and the pending and active clear aliases read their
state, that a write of 0 to `ISENABLER0` is ignored, that `GICD_CTLR.DS`
and `ARE` cannot be cleared, and that `GICD_IGROUPR` is writable under `DS = 1`
rather than RAZ/WI. If QEMU is not installed the script skips; `--require` turns
that into a failure.

The checker exists because this driver is firmware that runs before anything can
be diagnosed, so its failure modes are unusually expensive. It refuses, in the
source or in the patch that adds it:

- a `while`, a `for (;;)`, or a `MicroSecondDelay` outside a loop with an
  `Elapsed <` bound — a self-test that can hang;
- `ASSERT`, `ASSERT_*`, `CpuDeadLoop` or `gBS->Stall` — anything that stops the
  boot;
- a registered handler that does not call `EndOfInterrupt`;
- a redistributor register outside the modelled set, or the doorbell address;
- a `GICR_WAKER` write that is not a read-modify-write from a fresh read;
- `%ll`, which EDK2's `PrintLib` does not implement;
- a PCD the code reads but the INF does not declare;
- a DSC/FDF wiring that drops the `AIC_BUILD` guard or puts the driver after
  `TimerDxe`.

`tests/test_check_vgic_selftest.py` puts each of those defects back into a copy of
the series or the tree and requires the checker to reject it, so the checker
cannot rot into a script that always passes.
