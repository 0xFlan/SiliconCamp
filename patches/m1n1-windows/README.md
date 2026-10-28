# m1n1-windows patches

Seven patches apply to the NTASP `m1n1_windows` commit pinned by this
repository. They are cumulative (each assumes every earlier one), so the order
below is part of the contract, and `verify-patches.sh` applies the whole series,
in order, to one pristine worktree:

| Patch | Files | What it fixes |
|---|---|---|
| `0001-vgic-m4-topology.patch` | `src/hv_vgic.c`, `src/smp.c`, `src/smp.h` | Replaces the per-SoC vGIC window switch with one derived from the guest PA size, and takes the redistributor count from the ADT. Without this, `T8132` fell through to an unset window and a hard-coded 8 CPUs. |
| `0002-psci-t8132.patch` | `src/hv_psci.c`, `src/hv_psci.h`, `src/hv_exc.c` | Adds the M4 power-domain tree (two clusters: six E-cores, four P-cores), the T8132 PMGR CPU-start offset, ADT bounds/count checks, per-CPU PSCI state, and Aff1:Aff0 MPIDR normalisation. Without the first part, `hv_psci_init()` had no `T8132` case and read an unset `topology_tree` pointer. |
| `0003-pcie-guest-handoff.patch` | `src/pcie_guest.c`, `src/pcie_guest.h`, `src/guest_msi.c`, `src/guest_msi.h` (new), `src/dart.c`, `src/dart.h`, `src/hv.c`, `src/hv_exc.c`, `src/hv_vgic.c`, `src/hv_vgic.h`, `src/proxy.c`, `src/proxy.h`, `proxyclient/m1n1/proxy.py`, `Makefile`, `config.h` | Hands PCIe and MSI to a guest that has no APCIE driver: maps the controllers' ECAM and BAR windows into the guest's stage 2, puts every APCIE DART into bypass (all streams, verified by read-back), programs each port's MSI map and unmasks the AIC vectors it raises, and translates AIC interrupt numbers into the guest's INTID window. Replaces the unconditional `pcie_shutdown()` in `hv_init()`, which left a Windows guest with no root complex at all. |
| `0004-vgic-guest-contract.patch` | `src/hv_vgic.c`, `src/hv_vgic.h`, `src/hv_exc.c`, `src/guest_msi.c`, `src/guest_msi.h` | Makes the emulated distributor and redistributors behave the way a GICv3 guest is entitled to assume: decode each redistributor frame from its address instead of the faulting CPU, complete `GICR_WAKER` wake sequences, never leave `RWP` set, stop advertising LPI capabilities nothing implements, and translate every guest INTID into an AIC number before masking it, unmasking it, or routing it. |
| `0005-vgic-diagnostics.patch` | `src/hv_vgic.c` | Takes the UART write off the interrupt-controller hot path: the per-access trace becomes a 64-entry ring buffer plus counters in a global, and only genuinely exceptional events print, through a 32-line budget. Without this, a guest that programs ~1020 SPIs stalls for minutes in `printf()`, which looks exactly like a hang in the vGIC. |
| `0006-vgic-honest-spi-routing.patch` | `src/hv_vgic.c`, `src/hv_vgic.h` | Sets `GICD_TYPER.No1N` so the distributor does not claim 1-of-N SPI selection it never performs, treats a `GICD_IROUTER.IRM` write of 1 as 0 (the behaviour the architecture permits when `No1N` is set), and keeps bit 31 out of the stored routing value so read-back cannot report a mode that never runs. |
| `0007-vgic-enable-gating.patch` | `src/hv_vgic.c`, `src/hv_vgic.h`, `src/hv_exc.c` | Makes delivery obey the guest's enable state (`GICD_CTLR.EnableGrp1`, `GICR_ISENABLER0`, `GICD_ISENABLER`), keeps a refused interrupt queued instead of dropping it, and repairs the redistributor's SGI-frame bank handlers, which kept enable and pending state in the active bank. Also makes `GICD_ISPENDR` read back and reports the Group 1 that delivery implements. |

## Why 0002 is not optional on M4

`hv_psci_init()` runs unconditionally from `hv_init()` and starts with:

```c
const unsigned char *topology_tree;
switch (chip_id) {
    case T8103: ...
    case T6021: ...
}
psci_num_clusters = topology_tree[1];
```

There is no `T8132` case in the upstream fork, so on an M4 the pointer is
indeterminate and `psci_num_clusters` is whatever the stack held. Everything
after that (`malloc` size, power-domain tree population, `psci_cpu_nodes`) is
derived from it. The patch makes an unknown SoC fatal instead, and gives T8132
the correct 6E+4P tree.

The M4 tree is not the T602X tree: T602X is 4E+4P (plus a second P cluster on
the Max), while T8132 is 6E+4P. Reusing `T602X_*` here would build a 10-node tree
with the wrong cluster split and quietly mismatch the MADT/PPTT/DSDT.

`hv_psci_init()` also refuses to continue when the descriptor's core count
disagrees with the ADT. That matters because the descriptor is the only place
the E/P split is encoded; a wrong split means the guest's `CPU_ON` for a P-core
MPIDR selects an E-cluster power domain.

## Why 0003 exists

`hv_init()` in upstream m1n1 starts with `pcie_shutdown()`. That is the right
call for the guest it was written for: macOS has an APCIE driver, so it wants a
controller that has been quiesced and can bring it up itself. Our guest is
Windows, and neither Windows nor the NTASP UEFI firmware that loads it contains a
single line that touches an APCIE register. Quiescing the controllers there is
the difference between "PCIe exists" and "PCIe does not exist at all".

What a generic guest's PCIe stack does assume is two things m1n1 can do:

1. **Its MMIO is already in stage 2.** A guest access to an unmapped IPA is
   fatal: `hv_handle_dabort()` prints `HV: Unmapped IPA` and returns an unhandled
   exception. The ECAM is the one that bites first, because the guest's PCI bus
   driver polls it from the first line of enumeration, but the BAR windows matter
   just as much once a device's BAR has been assigned.
2. **Device DMA reaches memory at the address the guest programmed.** The guest
   runs with an identity stage 2 (IPA == PA), so the DART state that matches is
   bypass: device transactions pass through untranslated. The guest has no DART
   driver, so nothing else is going to arrange that.

### The MSI half

PCIe without interrupts is enumeration and DMA with no way to signal completion.
Apple's controllers have no INTx; a device writes a vector index to the
controller's doorbell (`msi-address`, `0xfffff000` on J624) and the AIC raises
`msi-vector-offset + index` (1506 + index). That is not a GIC INTID, and m1n1
used to forward the AIC number verbatim into `ICH_LR_EL2`, which is why the
guest's interrupt controller would have seen an INTID four banks past the end of
the distributor it was told about.

An emulated ITS is not an option: a real one translates writes to
`GITS_TRANSLATER`, and Apple's hardware never writes there. A GIC MSI Frame is
the same "write an index to an address" mechanism in a form the guest can
discover through ACPI, so the firmware publishes one for SPIs 512-767 at
`0xfffff000` and the hypervisor owns the translation:

- `src/guest_msi.c` walks `/arm-io` for `compatible = "apcie,*"` (the `apcie-ge`
  complexes are excluded: different layout, no guest MSI window yet), derives
  each port's register block the way `pcie.c` does, programs all 256 MSIMAP
  entries to `0x80000000 | i` and reads them back — iBoot hands over with the map
  cleared, and a silently zero map looks exactly like working MSI that no device
  ever triggers. It then unmasks AIC 1506-1761 and publishes the window.
- `hv_vgic3_guest_vintid()` (in `hv_vgic.c`, called from `hv_exc_irq()`) maps
  that window to INTIDs 512-767, passes AIC interrupts up to SPI 1019 through
  unchanged, and for anything above — the UART at 1046, the APCIE root complexes
  at 1231-1249 — masks the source and logs it once rather than injecting an INTID
  the guest cannot represent. `hv_vgic3_get_priority()` now refuses an INTID past
  the emulated distributor's last one instead of reading the bank that follows.

The constants in `src/guest_msi.h` and the firmware's MADT entry are one
contract; `scripts/check-acpi.py` compares the built table against them and
against the captured doorbell, and fails on a window past SPI 1019. Nothing here
has run on hardware; `docs/pcie.md` lists the four unknowns in the order a first
boot will expose them.

`src/pcie_guest.c` walks the ADT instead of hardcoding paths. Controllers are
found by their `compatible` prefix (`apcie`, `apcie-ge`), DARTs by `dart,` plus
`apcie` in their path, which covers the M1-era `apcie`/`dart-apcieN` names and the
`apciec0-3`/`dart-apciecN` names that t8122 and later use. Anything it does not
recognise is printed rather than guessed at, because the first hardware boot is
where that information comes from.

The BAR windows are the one thing the ADT does not describe, so they come from
the captured J624 values in `pcie_guest.c`; a SoC without an entry gets its ECAM
mapped and a warning saying which values are missing.

`dart_bypass_adt()` (patch 0003, `src/dart.c`) is deliberately not `dart_init()`:
TCR is banked per stream, so bypassing a DART means walking all of its streams --
32 for t8020/t6000, 256 for t8110 -- enabling all of them and verifying each one
by reading it back. A DART that is locked or that ignores the write is reported
instead of assumed, and a run that cannot bypass everything makes
`pcie_guest_handoff()` return an error so the log says so in one line.

To keep the controllers instead for a guest that owns them, define
`HV_QUIESCE_PCIE` in `config.h`; that restores the upstream `pcie_shutdown()`.

## Why 0004 exists

Patch 0003 taught the hypervisor to translate AIC interrupts into guest INTIDs.
0004 is about the other half of that path: the register interface the guest uses
to enable, route, wake, and acknowledge them.

The reason this was not caught earlier is that the firmware is a very forgiving
guest. EDK2's `ArmGicV3Dxe` (`Silicon/ARM/TIANO/ArmPkg/Drivers/ArmGicDxe/GicV3/ArmGicV3Dxe.c`)
locates a redistributor by walking `GICR_TYPER` from the discovery base until it
finds the executing PE's affinity, and it does that from the boot CPU. It never
writes `GICR_WAKER` at all, and it never polls `RWP`. So a vGIC can satisfy the
firmware while being unusable to an operating system:

- **Redistributor frames were all the same frame.** `handle_vgic_redist_access()`
  computed `relative_addr = addr - redist_base` and then
  `cpu_num = ctx->cpu_id`. Any access through a frame other than the first
  therefore decoded as an unknown register — CPU 1's `GICR_WAKER` arrives at
  `0x20014`, not `0x14` — and returned a data abort. A homogeneous 8-core
  emulation happened to hide it because the boot CPU's frame is frame 0. The
  fix takes the frame index from the address (`frame_offset / GICR_FRAME_SIZE`),
  bounds-checks it against `num_cpus`, and uses `frame_offset % GICR_FRAME_SIZE`
  as the register offset.
- **`GICR_WAKER` never finished waking.** The write was stored verbatim, so
  `ChildrenAsleep` (bit 2, read-only) stayed exactly as the guest wrote it. The
  documented wake sequence is: clear `ProcessorSleep`, then poll
  `ChildrenAsleep == 0`. A read-modify-write preserves the bit, so a conforming
  guest polls forever. The emulated redistributor has no separate power domain,
  so the transition is now immediate, and the reset value is the architectural
  `ProcessorSleep | ChildrenAsleep`.
- **`RWP` was set and never cleared.** `GICD_CTLR.RWP` (bit 31) was set on a
  group disable and on every `GICD_ICENABLER` write; `GICR_CTLR.RWP` was written
  as bit 31, which is `UWP` — RES0 when `VLPIS` is 0 — instead of bit 3. `RWP`
  exists so software can tell when a write has landed; with synchronous
  emulation it is always already landed, so both now read 0.
- **Capabilities advertised features with no implementation.**
  `GICR_TYPER.PLPIS`, `DirectLPI` and `DPGS` were set while `GICR_SETLPIR` was a
  no-op and `GICR_CTLR.DPG0/DPG1NS` were unimplemented, and
  `GICD_TYPER.LPIS` was set with no ITS behind it. A guest that believes those
  bits allocates an LPI configuration table and waits for an interrupt that
  cannot arrive. MSIs reach this guest through the ACPI GIC MSI Frame (0003),
  not through LPIs, so the bits are now 0 and the comments say why.
- **Mask polarity was inverted and INTIDs were not translated.**
  `GICD_ICENABLER` unmasked (`aic_set_mask(irq, false)`) while logging that it
  was masking, so every interrupt the guest disabled stayed live. Both enable and
  disable, `GICD_IROUTER`, and the LR-EOI path passed guest INTIDs straight to
  the AIC, which means SPI 512 touched AIC 512 instead of the APCIE source at
  1506. `guest_msi_aic_irq()` is the exact inverse of the existing
  `guest_msi_intid()`, and every one of those paths now goes through it. INTIDs
  below 32 are SGIs/PPIs with no AIC line and are skipped.
- **`GICD_IROUTER` could compute `BIT(-1)`.** `smp_get_id()` returns `-1` for an
  affinity that is not in the ADT, and the result was assigned to a `u32` before
  being handed to `aic_set_affinity()`, which expands it as `BIT(cpu)`. The
  variable is now signed and the call is guarded.

None of this has run on hardware. What is machine-checked is that the series
contains each fix and removes each defect: `scripts/check-vgic.py` asserts the
contract against the patches (no submodule needed) and against patched sources,
and `tests/test_check_vgic.py` mutates the series to prove each assertion fails
when the defect is put back.

## Why 0005 exists

0004 made the emulated GIC correct. 0005 makes it *observable* without making it
slow, and it is a boot hazard in its own right rather than a nicety.

`hv_vgic.c` built with `ENABLE_VGIC_LOGGING 1`, so the last three lines of every
emulated register access were three `printf()` calls. m1n1's console is a UART:
at 115200 baud a 60-byte line costs about 5 ms. A guest that programs the GIC
performs tens of thousands of accesses — Windows' HAL walks roughly 1020 SPIs at
three registers each, plus one redistributor frame per PE — so the first thing a
real operating system would do is spend minutes inside the debug console while
its watchdog concludes the interrupt controller is dead. The most likely first
boot failure was therefore going to be the logging, and it would have presented
as a hang in the code the logging was added to debug.

Deleting the trace would have made the next hang silent, so it was replaced:

| Question | Mechanism |
|---|---|
| What did the guest do just before it stopped? | a 64-entry ring of `(space, frame, offset, value, write, handled, width)` |
| Did the guest ever touch this register? | one bit per 8 bytes, per space, updated with an atomic OR |
| Something is wrong and I want to know now | `vgic_note()`, capped at 32 lines per boot |

The rings and counters live in `vgic_diag`, a global, so they are readable over
the proxy *after* a hang — which is the case that matters, because a hung guest
prints nothing at all. `docs/vgic-diagnostics.md` documents the field offsets and
walks a Python reader through them.

The severity split is not new: the source already tagged its messages `[INFO]`,
`[WARN]` and `[ERROR]`. 0005 makes the behaviour match the tag — `[INFO]` traces
go to the ring, `[WARN]`/`[ERROR]` go through the budget, because an exceptional
event must print but must not be trusted to be rare.

The budget is what makes this safe against the failure mode it exists for. A
guest that spins on an unknown register would otherwise reprint that line
forever; with the budget the first 32 appear, `vgic_diag.suppressed` counts the
rest, and the difference between "32 lines" and "thirty thousand lines" is the
difference between a readable log and a stall.

`scripts/check-vgic.py` fails if any patch sets `ENABLE_VGIC_LOGGING` back to 1,
if a handler prints instead of recording, or if an exceptional site bypasses the
budget.

## Why 0006 exists

`GICD_TYPER.No1N` was 0, which tells a guest that the distributor supports
1-of-N SPI routing: the guest may set `GICD_IROUTER.IRM` and let the distributor
pick a participating PE. This vGIC has no such selection — `hv_exc_irq()`
injects the virtual interrupt into whichever physical CPU took the AIC line —
and it does not model the `GICR_CTLR.DPG0`/`DPG1S`/`DPG1NS` bits through which a
PE opts out of that distribution (`0004` already had to clear `DPGS` in
`GICR_TYPER` for the same reason). Advertising the mode invited a guest to rely
on a selection that never happens.

The architecture gives a way out. When `No1N` is 1, 1-of-N is not supported and
a write of 1 to `GICD_IROUTER.IRM` is CONSTRAINED UNPREDICTABLE; one permitted
behaviour is that the field acts as if it were 0 for all purposes. 0006 takes
that behaviour, clears bit 31 before storing the routing value, and reports the
normalised value on read-back. The guest's original write is still what the
diagnostic ring records, because that is the useful half of the event.

`No1N` is a capability bit about the emulated device. It does not make Apple's
AIC able to move an SPI: `aic_set_affinity()` returns early unless the AIC is
version 1, and AICv2/v3 have no software-programmable per-SPI CPU target.
Linux's `aic2_chip`/`aic3_chip` has no `irq_set_affinity` at all, and the
`IRQ_CFG.TARGET` field m1n1 programs at boot comes from the ADT's
`aic-ext-intr-cfg` and selects a die. So `GICD_IROUTER` writes are decoded,
translated and stored, but on the M4 the AIC still decides which PE takes the
interrupt. That gap is documented in `docs/architecture.md`; closing it would
mean forwarding an acknowledged physical interrupt to another CPU's software
queue, which is not attempted before the conformance self-test can measure
where interrupts actually land.

`scripts/check-vgic.py` fails if a later patch drops `No1N`, accepts `IRM`
without normalising it, or stores the raw routing value.

## Why 0007 exists

The UEFI self-test (patch `0003` of the firmware series) measured three deviations
from the architecture, and all three were in the delivery path rather than in a
capability bit:

1. `gating.sgi` — a self-SGI whose `GICR_ISENABLER0` bit was clear was still
   delivered.
2. The virtual timer PPI behaved the same way: the injection path never read the
   distributor's group enables or the per-interrupt enable.
3. `GICD_IGROUPR` read 0 under `DS = 1`, describing Group 0 delivery that the
   hypervisor does not implement (every list register is written with
   `ICH_LR_GRP1`).

While implementing the first one, reading the redistributor handlers made a
larger defect visible: `GICR_ISENABLER0`, `GICR_ICENABLER0`, `GICR_ISPENDR0` and
`GICR_ICPENDR0` all read and wrote `gicr_isactiver0`/`gicr_icactiver0`. Enable
state was therefore stored in the active bank, and `GICR_ICENABLER0` never
cleared the enable bit at all. A guest that only writes cannot see this; a guest
that reads back — which Windows' HAL does — reads the wrong state immediately.
The same handlers also stored the raw write to `ISENABLER0`, so a write of 0
silently cleared lines that were already enabled. `GICD_ISPENDR` had a `;` where
it should have had `*val =`, so a read returned whatever the value register held.

None of this is guesswork about what a guest expects. Arm IHI 0069 defines both
aliases of each set/clear pair as reading the current state, and QEMU's GICv3
model agrees; `scripts/check-gicv3-oracle.py` drives that model over qtest and
asserts 26 of these read-backs. `AppleVgicSelfTestDxe` gained
`VgicCheckRedistributorBanks`, which measures the same properties on the device,
including the write-of-zero case that distinguishes write-1-to-set from a raw
store.

Group state needed a policy rather than a decode. With `GICD_CTLR.DS = 1` the
architecture leaves `GICD_IGROUPR` writable and does **not** force Group 1; a
register that read 0 would be telling the guest the line is Group 0 while the
injection path delivers it with `ICH_LR_GRP1`. Since there is no Group 0 delivery
path, the emulation now reports Group 1 for modelled SPIs and ignores a write
that tries to select Group 0. That is a limitation stated in the register, not a
capability claimed without an implementation.

Refusing an injection is only safe if the interrupt survives the refusal, and
the first version of the gate did not do that properly. A queue that stopped at
its first refused entry would let one disabled INTID block every interrupt
behind it — a disabled SGI 0 starving SGI 1, for example — and a queued
interrupt was only retried when some later, unrelated interrupt happened to
drain the queue. An edge-triggered source could therefore stay pending forever
after the guest enabled it. The drain now compacts the queue, keeping the
refused entries in order while delivering the rest, and the vGIC register paths
call it when the guest enables an INTID in `GICR_ISENABLER0`, in a
`GICD_ISENABLER` bank, or when `GICD_CTLR.EnableGrp1` turns on. The firmware
probe `gating.pending` measures exactly this: it sends an SGI while the bit is
clear, sets the bit, and waits without resending.

A refusal must also not multiply. The first version tested `virq_queue_contains()`
and then called `virq_queue_push()` as two critical sections, so two CPUs sending
the same SGI to one target could both see an empty queue and enqueue the vector
twice. A GIC has one pending bit per INTID; delivering two interrupts for it is
a behaviour the guest never asked for. `virq_queue_push_unique()` scans and
inserts under one lock, so the duplicate cannot be created in the first place.

What 0007 deliberately does not do is make `GICR_ISPENDR0` raise an interrupt.
It is modelled as pending state, which is what a guest reads back; this vGIC
raises guest interrupts from `ICC_SGI1R_EL1` and the AIC. `docs/vgic-selftest.md`
records that gap next to the probe that measures the read-back.

`scripts/check-vgic.py` fails if the gate is removed, if a bank handler writes
enable or pending state into the active bank, if `GICR_ISENABLER0` becomes a raw
store again, if the drain stops at a refused entry, if enabling an INTID stops
draining the queue, or if an enqueue site separates the duplicate test from the
insert.

## The SMC trace

`hv_handle_smc()` used to print the PSCI function ID on every guest SMC. That is
a UART write in the hot path of every `CPU_ON`, `AFFINITY_INFO`, and
`CPU_SUSPEND`, which distorts guest timing and floods the console during SMP
bring-up. It is now behind `DEBUG_PSCI_SMC` (off by default); define it in
`config.h` only while diagnosing a PSCI return value on hardware.

## Apply and verify

```bash
./scripts/apply-patches.sh          # the whole series, idempotent
./scripts/verify-patches.sh         # applies 0001-0007 in order to a clean worktree of the pin
./scripts/check-vgic.py             # asserts the vGIC contract against the series
```

`verify-patches.sh` applies the series to *one* worktree rather than checking
each patch against a pristine `HEAD`. That distinction matters: a later patch is
written on top of its predecessors, so checking it against `HEAD` either fails
for the wrong reason or, worse, passes because a hunk happened to match.

## Regenerating a patch

Every patch after 0001 is cumulative, so it has to be generated against the tree
its predecessors produce, not against `HEAD`. The reliable procedure is to build
that base as a commit in a throwaway worktree, copy the files the patch owns, and
diff against the commit:

```bash
TMP=$(mktemp -d)
git -C third_party/m1n1-windows worktree add --detach "$TMP/tree" HEAD
for p in 0001-vgic-m4-topology 0002-psci-t8132 0003-pcie-guest-handoff \
         0004-vgic-guest-contract 0005-vgic-diagnostics 0006-vgic-honest-spi-routing; do
    git -C "$TMP/tree" apply "$PWD/patches/m1n1-windows/$p.patch"
done
# make the base a commit, so the diff below contains only this patch
git -C "$TMP/tree" add -A
git -C "$TMP/tree" -c user.email=build@local -c user.name=build commit -qm base

# copy the files this patch owns out of the working tree, then:
git -C "$TMP/tree" add -A
git -C "$TMP/tree" diff --cached > patches/m1n1-windows/0007-vgic-enable-gating.patch
```

Skipping the base commit is the mistake that silently produces a 1000-line patch
containing the whole earlier series as well. Verify the result by applying the whole series to
a fresh worktree and confirming it reproduces the working tree:

```bash
diff -r --exclude=.git --exclude=build --exclude='*.o' \
     third_party/m1n1-windows "$TMP/tree"
```

Then run `./scripts/verify-patches.sh`, `./scripts/check-vgic.py --tree
third_party/m1n1-windows`, and rebuild with `./scripts/build-m1n1.sh`. As a
reproducibility check, the series as committed builds
`m1n1.macho` = `a5636c6de757…` and `m1n1.bin` = `06247d0eb94b…` (SHA-256),
the same bytes as the working tree it was generated from (checked 2026-10-28).

