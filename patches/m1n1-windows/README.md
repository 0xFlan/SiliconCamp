# m1n1-windows patches

Four patches apply to the NTASP `m1n1_windows` commit pinned by this
repository. They are cumulative (0002 assumes 0001, 0003 assumes both, 0004
assumes all three), so the order below is part of the contract, and
`verify-patches.sh` applies the whole series, in order, to one pristine worktree:

| Patch | Files | What it fixes |
|---|---|---|
| `0001-vgic-m4-topology.patch` | `src/hv_vgic.c`, `src/smp.c`, `src/smp.h` | Replaces the per-SoC vGIC window switch with one derived from the guest PA size, and takes the redistributor count from the ADT. Without this, `T8132` fell through to an unset window and a hard-coded 8 CPUs. |
| `0002-psci-t8132.patch` | `src/hv_psci.c`, `src/hv_psci.h`, `src/hv_exc.c` | Adds the M4 power-domain tree (two clusters: six E-cores, four P-cores), the T8132 PMGR CPU-start offset, ADT bounds/count checks, per-CPU PSCI state, and Aff1:Aff0 MPIDR normalisation. Without the first part, `hv_psci_init()` had no `T8132` case and read an unset `topology_tree` pointer. |
| `0003-pcie-guest-handoff.patch` | `src/pcie_guest.c`, `src/pcie_guest.h`, `src/guest_msi.c`, `src/guest_msi.h` (new), `src/dart.c`, `src/dart.h`, `src/hv.c`, `src/hv_exc.c`, `src/hv_vgic.c`, `src/hv_vgic.h`, `src/proxy.c`, `src/proxy.h`, `proxyclient/m1n1/proxy.py`, `Makefile`, `config.h` | Hands PCIe and MSI to a guest that has no APCIE driver: maps the controllers' ECAM and BAR windows into the guest's stage 2, puts every APCIE DART into bypass (all streams, verified by read-back), programs each port's MSI map and unmasks the AIC vectors it raises, and translates AIC interrupt numbers into the guest's INTID window. Replaces the unconditional `pcie_shutdown()` in `hv_init()`, which left a Windows guest with no root complex at all. |
| `0004-vgic-guest-contract.patch` | `src/hv_vgic.c`, `src/hv_vgic.h`, `src/hv_exc.c`, `src/guest_msi.c`, `src/guest_msi.h` | Makes the emulated distributor and redistributors behave the way a GICv3 guest is entitled to assume: decode each redistributor frame from its address instead of the faulting CPU, complete `GICR_WAKER` wake sequences, never leave `RWP` set, stop advertising LPI capabilities nothing implements, and translate every guest INTID into an AIC number before masking it, unmasking it, or routing it. |

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

## The SMC trace

`hv_handle_smc()` used to print the PSCI function ID on every guest SMC. That is
a UART write in the hot path of every `CPU_ON`, `AFFINITY_INFO`, and
`CPU_SUSPEND`, which distorts guest timing and floods the console during SMP
bring-up. It is now behind `DEBUG_PSCI_SMC` (off by default); define it in
`config.h` only while diagnosing a PSCI return value on hardware.

## Apply and verify

```bash
./scripts/apply-patches.sh          # the whole series, idempotent
./scripts/verify-patches.sh         # applies 0001-0004 in order to a clean worktree of the pin
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
for p in 0001-vgic-m4-topology 0002-psci-t8132 0003-pcie-guest-handoff; do
    git -C "$TMP/tree" apply "$PWD/patches/m1n1-windows/$p.patch"
done
# make the base a commit, so the diff below contains only this patch
git -C "$TMP/tree" add -A
git -C "$TMP/tree" -c user.email=build@local -c user.name=build commit -qm base

# copy the files this patch owns out of the working tree, then:
git -C "$TMP/tree" add -A
git -C "$TMP/tree" diff --cached > patches/m1n1-windows/0004-vgic-guest-contract.patch
```

Skipping the base commit is the mistake that silently produces a 1000-line patch
containing 0001-0003 as well. Verify the result by applying the whole series to
a fresh worktree and confirming it reproduces the working tree:

```bash
diff -r --exclude=.git --exclude=build --exclude='*.o' \
     third_party/m1n1-windows "$TMP/tree"
```

Then run `./scripts/verify-patches.sh`, `./scripts/check-vgic.py --tree
third_party/m1n1-windows`, and rebuild with `./scripts/build-m1n1.sh`. As a
reproducibility check, the series as committed builds
`m1n1.macho` = `7559eb588df7…` and `m1n1.bin` = `355a19b2b5be…` (SHA-256), the
same bytes as the working tree it was generated from.

