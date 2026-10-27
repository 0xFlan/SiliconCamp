# m1n1-windows patches

Both patches apply to the NTASP `m1n1_windows` commit pinned by this repository.
They are independent files, so the order below is the only requirement:

| Patch | Files | What it fixes |
|---|---|---|
| `0001-vgic-m4-topology.patch` | `src/hv_vgic.c`, `src/smp.c`, `src/smp.h` | Replaces the per-SoC vGIC window switch with one derived from the guest PA size, and takes the redistributor count from the ADT. Without this, `T8132` fell through to an unset window and a hard-coded 8 CPUs. |
| `0002-psci-t8132.patch` | `src/hv_psci.c`, `src/hv_psci.h`, `src/hv_exc.c` | Adds the M4 power-domain tree (two clusters: six E-cores, four P-cores), the T8132 PMGR CPU-start offset, ADT bounds/count checks, per-CPU PSCI state, and Aff1:Aff0 MPIDR normalisation. Without the first part, `hv_psci_init()` had no `T8132` case and read an unset `topology_tree` pointer. |

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
