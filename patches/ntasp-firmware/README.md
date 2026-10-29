# NTASP firmware patches

`0001-m4-uefi-platform.patch` adds the M4 platform package. It applies to the
vendored NTASP tree at the commit recorded by the top-level repository:

- `Platform/Mac16_3Pkg` — iMac M4 (J624) DSC/FDF, DSDT, FADT, MCFG, DBG2, ACPI
  tables. The DSDT has `PCI0` (`PNP0A08`/`PNP0A03`) with the captured bus and
  host windows; MCFG describes the same ECAM. The FDF's ARM64 Image header
  declares `image_size` `0x1E00000` (the FD size) instead of upstream's
  `0x100E0000`.
- `Silicon/Apple/T8132FamilyPkg` — T8132 PCDs, MADT, PPTT, memory map, PCIe
  ECAM bar size/limit, libraries.
  The PCIe window PCDs carry the captured J624 values, and
  `PcdPciMmio32Translation` is **host - bus** (`0xb00000000`), not bus - host:
  `AppleSiliconPciHostBridgeLib` negates the PCD into the aperture's bus - host
  translation, and the T602X platform uses the same convention. The first
  revision of this patch had the sign the other way round, which builds and
  enumerates but leaves every device BAR decoding nowhere; the derivation is in
  the file's comment block and `scripts/check-acpi.py` now fails the build if the
  PCD, the DSDT `_CRS` and the captured values disagree.
- `Silicon/Apple/AppleSiliconPkg/Drivers/AppleNANDStorageDxe` — the internal
  SSD driver: ASC mailboxes, SART window table (all four register layouts),
  RTKit handshake, NVMMU/NVMe queues, `Identify` and `EFI_BLOCK_IO_PROTOCOL`.
  This replaces the 22-line comment-only stub that shipped in the tree. The
  register-level sequence is a port of m1n1's `nvme.c`, `asc.c`, `sart.c` and
  `rtkit.c`, and `scripts/check-ans2-constants.py` compares 110 constants
  against those files so the port cannot drift. `docs/ans2.md` explains the
  design and the parts that are deliberately left to the OS.
- `Silicon/Apple/AppleSiliconPkg/AppleSiliconPkg.dsc.inc` and
  `Platform/Mac16_3Pkg/Mac16_3.fdf` — build the ANS driver and place it in the
  DXE FV, replacing the commented-out `AppleANS2Dxe` TODO.
- `Platform/MacBookProEarly2023Pkg/PlatformBuild.py` — drop `cibuild` from the
  reference platform so AArch64 builds do not require the unreachable NuGet
  feeds.

`0002-nasm-scope.patch` is applied inside the `MU_BASECORE` submodule. It moves
NASM's dependency to the `cibuild` scope, so an AArch64 firmware build does not
try to download the x86-only assembler.

`0003-vgic-selftest.patch` adds `AppleVgicSelfTestDxe`, the vGIC conformance
self-test, and wires it into the image:

- `Silicon/Apple/AppleSiliconPkg/Drivers/AppleVgicSelfTestDxe` — a DXE driver that
  walks the emulated distributor and all ten redistributor frames, wakes each PE
  through `GICR_WAKER` with a bounded poll, checks the capability and banked
  registers a guest programs (including both aliases of the SGI/PPI
  enable/pending/active set-clear pairs on the boot PE, and the write-of-zero
  case that separates write-1-to-set from a raw store), then delivers a self-SGI
  and the virtual timer PPI through the real exception path. The SGI gate is
  measured in both directions, including the case that separates a vGIC which
  keeps a refused interrupt pending from one that drops it: the driver disables
  the bit, sends the SGI, re-enables the bit and waits without sending again.
  It then does the same for the distributor half of the gate: clears
  `GICD_CTLR.EnableGrp1` with the SGI's own bit set, sends, restores the register
  and waits without resending.
  Every result is one `vgic:` line and nothing halts the boot. `docs/vgic-selftest.md` lists each
  check and the limits.
- `Platform/Mac16_3Pkg/Mac16_3.dsc` and `Mac16_3.fdf` — the component, behind
  `!if $(AIC_BUILD) == FALSE`, placed before `TimerDxe` in the FV because it must
  own the virtual timer PPI before that driver registers a handler for it.

`0004-vgic-selftest-group1-bit.patch` corrects the self-test's Group 1 gate.
Under `GICD_CTLR.DS=1`, bit 1 is `EnableGrp1` and bit 0 is `EnableGrp0`. The
driver originally checked bit 0, which the vGIC also set at reset, so the wrong
check passed on an untouched device and the Windows HAL replay cleared Group 0
while claiming to clear Group 1. The patch moves the macro and every use to bit
1 and says which layout the bit comes from. It edits the file `0003` creates,
so it is applied after `0003`; the apply script deliberately does not use the
`.inf` marker for it, because that file already exists once `0003` is in place.

`0001`, `0003` and `0004` are one **cumulative series**: `0003` edits two files
that `0001` creates, and `0004` edits the driver `0003` creates. None can be
checked on its own, because `0003` does not apply to a tree without `0001` and
`0001` no longer applies cleanly once `0003` is on top.
`scripts/verify-patches.sh` and `scripts/apply-ntasp-patches.sh` both treat them
in that order, and the apply script has a marker fallback (the driver's `.inf`)
for exactly that reason.

All four patches contain a binary file (`Resources/RegulatoryLogos.png` in
`0001`), so apply them with `git apply` (not a plain `patch` command).

From a clean checkout:

```bash
git submodule update --init --depth 1 third_party/ntasp-firmware
./scripts/apply-ntasp-patches.sh
./scripts/build-uefi-linux.sh Mac16_3
```

The script is idempotent: it detects an already-applied patch and does not apply
it twice. If you reset the submodule and re-apply, the build-tree output is
regenerated by `stuart_build`.

To regenerate the patches after changing the firmware tree, **do not** regenerate
`0001` from the current worktree. `Platform/Mac16_3Pkg/` is untracked, so
`git add -N Platform Silicon` followed by `git diff` folds the whole platform,
including `0003`'s edits and driver, back into `0001`. Regenerate from a
throwaway worktree whose `HEAD + 0001` has been committed first:

```bash
TMP="$(mktemp -d)"
git -C third_party/ntasp-firmware worktree add --detach "$TMP/base" HEAD
git -C "$TMP/base" apply "$PWD/patches/ntasp-firmware/0001-m4-uefi-platform.patch"
git -C "$TMP/base" add -A
git -C "$TMP/base" -c user.name=regen -c user.email=regen@example.invalid \
    commit -q -m "base: HEAD + 0001"

# copy the files 0003 owns from the working tree into $TMP/base, then:
git -C "$TMP/base" add -A
git -C "$TMP/base" diff --cached --binary \
    > patches/ntasp-firmware/0003-vgic-selftest.patch

git -C third_party/ntasp-firmware worktree remove --force "$TMP/base"
rmdir "$TMP"
./scripts/verify-patches.sh
```

`verify-patches.sh` applies the series to `HEAD` in one worktree, in order, so it
fails if a later patch stops applying on top of its predecessors or if `0001`
stops reproducing the files it owns. `scripts/check-vgic-selftest.py` reads
`0003` directly and fails if the driver, its DSC/FDF wiring, or the safety
properties the checker pins (bounded waits, EOI in the handler, modelled register
offsets, no `%ll`) stop being true.

`scripts/check-acpi.py` reads two of the files this patch adds
(`Platform/Mac16_3Pkg/Mac16_3.dsc` and
`Silicon/Apple/T8132FamilyPkg/T8132FamilyPkg.dsc.inc`) and checks their PCIe PCDs
against the ACPI tables and the captured machine values, so regenerating the
patch is also what keeps that check meaningful. `--firmware-root` points the
checker at another tree; the test suite uses that to run the same checks against
a fixture without the firmware submodule checked out.
