# m1n1-windows patches

Both patches apply to the NTASP `m1n1_windows` commit pinned by this repository.
They are independent files, so the order below is the only requirement:

| Patch | Files | What it fixes |
|---|---|---|
| `0001-vgic-m4-topology.patch` | `src/hv_vgic.c`, `src/smp.c`, `src/smp.h` | Replaces the per-SoC vGIC window switch with one derived from the guest PA size, and takes the redistributor count from the ADT. Without this, `T8132` fell through to an unset window and a hard-coded 8 CPUs. |
| `0002-psci-t8132.patch` | `src/hv_psci.c`, `src/hv_psci.h`, `src/hv_exc.c` | Adds the M4 power-domain tree (two clusters: six E-cores, four P-cores), the T8132 PMGR CPU-start offset, ADT bounds/count checks, per-CPU PSCI state, and Aff1:Aff0 MPIDR normalisation. Without the first part, `hv_psci_init()` had no `T8132` case and read an unset `topology_tree` pointer. |
| `0003-pcie-guest-handoff.patch` | `src/pcie_guest.c`, `src/pcie_guest.h` (new), `src/dart.c`, `src/dart.h`, `src/hv.c`, `src/proxy.c`, `src/proxy.h`, `proxyclient/m1n1/proxy.py`, `Makefile`, `config.h` | Hands PCIe to a guest that has no APCIE driver: maps the controllers' ECAM and BAR windows into the guest's stage 2, puts every APCIE DART into bypass (all streams, verified by read-back), and prints the ADT's controller/DART/MSI topology. Replaces the unconditional `pcie_shutdown()` in `hv_init()`, which left a Windows guest with no root complex at all. |

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

## The SMC trace

`hv_handle_smc()` used to print the PSCI function ID on every guest SMC. That is
a UART write in the hot path of every `CPU_ON`, `AFFINITY_INFO`, and
`CPU_SUSPEND`, which distorts guest timing and floods the console during SMP
bring-up. It is now behind `DEBUG_PSCI_SMC` (off by default); define it in
`config.h` only while diagnosing a PSCI return value on hardware.

## Apply and verify

```bash
./scripts/apply-patches.sh          # both patches, idempotent
./scripts/verify-patches.sh         # applies each to a clean worktree of the pin
```

To regenerate a patch after changing the tree:

```bash
git -C third_party/m1n1-windows diff src/hv_psci.c src/hv_psci.h src/hv_exc.c \
    > patches/m1n1-windows/0002-psci-t8132.patch
```

Then run `verify-patches.sh` and rebuild with `./scripts/build-m1n1.sh`.
