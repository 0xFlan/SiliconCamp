# Build artifacts

These are the outputs of the 2026-10-28 build on the `Mac16,3` / `J624` M4 iMac.
Both images were rebuilt after the Group 1 audit: the hypervisor carries
patches `0001`-`0009`, and the firmware carries `0001`-`0004`. Release
**v0.9.0** is older: its hypervisor predates `0008`/`0009` and its self-test
still checks bit 0 as Group 1. No release tag carries the corrected pair, so use
these hashes, not v0.9.0, for the first hardware session.

All three images changed between v0.8.0 and v0.9.0.

- the UEFI image now carries **`AppleVgicSelfTestDxe`** (firmware patch `0003`).
  It runs after `ArmGicDxe` and before `TimerDxe`, walks every redistributor
  frame, performs the guest wake sequence with a bounded poll, drives the banked
  SPI registers, and delivers a self-SGI and the virtual timer PPI through the
  real exception path. It measures both halves of the hypervisor's enable gate,
  including `gating.pending` (an SGI sent while disabled has to arrive when the
  bit is set, without a resend) and `gating.group1` / `gating.group1-pending`
  (the distributor's Group 1 enable). It has never run on hardware;
  `../docs/vgic-selftest.md` is the per-check reference.
- the hypervisor now carries patch `0007-vgic-enable-gating.patch`: injection
  consults `GICD_CTLR.EnableGrp1` and the INTID's own enable bit, a refused
  interrupt stays queued, the drain compacts instead of stopping at it, enabling
  an INTID drains the queues, and queueing is one atomic test-and-insert so two
  CPUs cannot enqueue the same SGI.
- the v0.8.0 changes remain: the UEFI MADT carries a **GIC MSI Frame** (doorbell
  `0xfffff000`, SPIs 512–767), and `guest_msi.c` programs each APCIE port's
  256-entry MSI map (read back, because iBoot hands over with it cleared),
  unmasks AIC 1506–1761, and `hv_vgic3_guest_vintid()` translates those AIC
  interrupts into the SPIs the firmware advertises. An AIC interrupt with no
  guest INTID is masked and logged instead of being injected verbatim.
- the v0.7.0 changes remain: `AppleNANDStorageDxe`, a real driver for the
  internal ANS/ANS2 SSD, is in the UEFI image. It has never run on hardware, so
  USB storage remains the install target until the first boot log says otherwise
  (`../docs/ans2.md`).
- the v0.6.0 changes remain: the hypervisor's `0003-pcie-guest-handoff.patch`
  maps the APCIE ECAM and BAR windows into the guest's stage 2 and bypasses every
  APCIE DART with per-stream read-back, and the UEFI image carries the corrected
  `PcdPciMmio32Translation` sign (`0xb00000000` = host − bus).

They are provided for review and for a hardware experiment that has **not** been
run yet. Read `../docs/safety.md` before installing anything on the machine.

The images themselves are attached to the GitHub release for the matching tag,
not committed to git (the FD is 30 MB and would be permanent in history).
Download them with `gh release download` or from the releases page; the
`SHA256SUMS` below identifies the exact images this repository was built and
checked against.

| File | Size | What it is |
|---|---|---|
| `MAC16_3_EFI.fd` | 30 MB | NTASP Project Mu UEFI firmware for the M4 iMac, `Mac16,3` |
| `m1n1-windows.bin` | 1.1 MB | raw m1n1-windows hypervisor image |
| `m1n1-windows.macho` | 896 KB | Mach-O form of the same m1n1-windows build |
| `SHA256SUMS` | — | SHA-256 of the three images above |

## SHA-256

```
1c2d9384fc495cc74b4d557ea2760420aa00bcfccd24c8839550b9629c1c28d1  MAC16_3_EFI.fd
e168cdd47fa769607735a13bef140039aaf9159e5377d3c9ca34e48e3d40733f  m1n1-windows.bin
e380f969b4cc669313e161291036bc2ffeb369e946431ac551a683f9eca989ec  m1n1-windows.macho
```

The m1n1 hashes come from a detached worktree of the pinned submodule commit
with `patches/m1n1-windows/0001`–`0009` applied. A recursive `diff -r` of that
tree against the working tree the patches were developed in reports no
difference outside `.git`, `build/` and `target/`, and both trees build the same
`m1n1.bin` and `m1n1.macho` byte for byte — so the hashes identify
clean-checkout + patches, not a dirty tree. The UEFI image was built from the
firmware tree that `patches/ntasp-firmware/0001` reproduces from its pinned
commit with `0003` and `0004` applied on top; a `diff -r` ignoring `__pycache__`
reports no difference.

## What the UEFI image contains

- `T8132FamilyPkg` with `PcdSystemMemoryBase = 0x10000000000`,
  `PcdSystemMemorySize = 0x600000000` (24 GB fallback; PrePi patches it from the
  Apple boot args), 10 cores, two clusters.
- MADT: 10 GICC entries; E-core MPIDRs `0x0`–`0x5`, P-core MPIDRs
  `0x10100`–`0x10103`; GICD `0x5000000000`; GICR `0x5100000000`, length
  `0x140000`; one GIC MSI Frame (type `0x0D`): doorbell `0xfffff000`, flags 0,
  SPIs 512–767.
- PPTT: one package, 6-core E-cluster, 4-core P-cluster, cores ACPI IDs 0–9.
- DSDT: `DIE0` with `CLU0` (CPU0–CPU5) and `CLU1` (CPU6–CPU9); `COM0` at
  `0x3ad200000`, interrupt 1046.
- FADT: desktop profile, hardware-reduced ACPI, PSCI compliant, SMC conduit.
- DBG2: `_SB.COM0` at `0x3ad200000`.
- The low 1 GB MMIO window is mapped for the four DWC3 controllers and their
  DARTs.
- MCFG: one ECAM entry at `0x1cb0000000`, segment 0, buses 0–8.
- PCIe: the first APCIE root complex (`apcie@B0000000`) is configured from the
  captured J624 values — ECAM `0x1cb0000000`+256 MB, bus range 0–8, host window
  `0xb80000000`+1 GB (offset `0xb00000000`), prefetch `0xbc0000000`+512 MB —
  and all three regions are mapped. `PCI0` is in the DSDT with `_HID`
  `PNP0A08`, `_CID` `PNP0A03`, an `_CRS` matching those windows, and `RES0`
  reserving the 256 MB ECAM aperture.
- The internal SSD driver, `AppleNANDStorageDxe` (36 KB in the FV): it boots the
  ANS coprocessor, programs the SART and NVMMU/NVMe queues and publishes the
  disk as `EFI_BLOCK_IO_PROTOCOL`. It has not run on hardware; `docs/ans2.md`
  maps the serial log to its bring-up steps.
- The vGIC conformance self-test, `AppleVgicSelfTestDxe` (a DXE module before
  `TimerDxe`, behind `!if $(AIC_BUILD) == FALSE`): it prints one `vgic:` line per
  check and never halts the boot. It has not run on hardware; `docs/vgic-selftest.md`
  lists every check and its stated limits.

The final FD stores the ACPI tables compressed inside the Project Mu FV; the
values above were checked from the build-tree `.aml`/`.acpi` files and the
disassembled DSDT, and `scripts/check-acpi.py` re-checks them on every build —
including each CPU's `_UID`, its containing cluster, `COM0`'s `_HID`/`_CRS`, and
the sign of the PCIe translation. The FD's ARM64 Image header declares
`image_size` `0x1E00000`, matching the FDF; upstream's value (`0x100E0000`) was
larger than the whole image.

## What the hypervisor image contains

- M4 vGIC topology (patch 0001): the MMIO window comes from the guest PA size
  and the redistributor count from the ADT, instead of a per-SoC switch that had
  no `T8132` case.
- M4 PSCI topology (patch 0002): two power domains (6 E-cores, 4 P-cores), the
  T8132 PMGR CPU-start offset, ADT bounds and core-count checks.
- PCIe hand-off (patch 0003): the ADT is walked for `apcie`/`apcie-ge`
  controllers and `dart,*apcie*` DARTs; every controller is printed (path, ECAM,
  `#ports`, MSI address/offset/vector count); the ECAM and the captured BAR
  windows are mapped into the guest's stage 2; every DART stream is put into
  bypass (`BYPASS_DART | BYPASS_DAPF`), the TLB is flushed, and every stream is
  read back. A stream that is locked or ignores the write fails the hand-off.
- guest MSI (patch 0003): 256 MSIMAP entries per APCIE port programmed to
  `0x80000000 | i` and read back, AIC 1506–1761 unmasked, and AIC interrupts
  translated to SPIs 512–767 (`guest_msi.c`, `hv_vgic3_guest_vintid()`). AIC
  interrupts above SPI 1019 with no INTID representation — the UART at 1046, the
  root complexes at 1231–1249 — are masked and logged once.
- distributor active bits (patch 0008): `GICD_ISACTIVER` sets and
  `GICD_ICACTIVER` clears the shared active bitmap. Without it, the firmware's
  `dist.active` probe fails on the first boot. The probe was already in the
  v0.9.0 image; this hypervisor build is the one that makes it pass.
- Group 1 gate (patch 0009, firmware 0004): the vGIC and the self-test both
  check `GICD_CTLR` bit 1, which is `EnableGrp1` under DS=1. Before the fix both
  checked bit 0 (`EnableGrp0`), so a Windows HAL enabling Group 1 would have
  left every virtual interrupt gated while the firmware reported a pass.
- `HV_QUIESCE_PCIE` in `config.h` restores upstream's `pcie_shutdown()` — it is
  **not** defined in this build.

## What is not in these images

- No *validated* MSI path. The frame, the port map programming and the
  translation are written, but the port map offset on T8132, the meaning of
  `msi-vector-offset`, the AIC target CPU, and whether an APCIE device actually
  raises the AIC interrupt from an MSI-X write to the frame are unproven on
  hardware (`../docs/pcie.md`).
- No description of the four `apciec0`–`apciec3` root complexes; `PCI0`
  describes only the first `apcie` node. Their DARTs are bypassed by the m1n1
  hand-off, and the hand-off prints their ECAMs at boot for the next capture.
- No *proven* ANS2 path: the driver is in the image, but the first hardware boot
  decides whether the controller actually comes up.
- No Windows drivers repository content; `third_party/ntasp-drivers` is still a
  skeleton.
- No Secure Boot, TPM, or anti-cheat path.

## Rebuild

```bash
./scripts/build-m1n1.sh   # applies patches/m1n1-windows/*.patch first
./scripts/build-uefi-linux.sh Mac16_3
```

Then copy the three outputs into this directory and regenerate `SHA256SUMS`:

```bash
cp third_party/m1n1-windows/build/m1n1.bin artifacts/m1n1-windows.bin
cp third_party/m1n1-windows/build/m1n1.macho artifacts/m1n1-windows.macho
cp third_party/ntasp-firmware/Build/Mac16_3-AARCH64/DEBUG_CLANGPDB/FV/MAC16_3_EFI.fd \
   artifacts/MAC16_3_EFI.fd
cd artifacts && shasum -a 256 MAC16_3_EFI.fd m1n1-windows.bin m1n1-windows.macho \
   > SHA256SUMS
```

`build-uefi-linux.sh` finishes by running `scripts/check-acpi.py`, so an FD that
reaches this directory has already passed the ACPI/image checks.

## First hardware experiment

The first experiment is **m1n1 on the M4**, not Windows. It requires a verified
backup, a second Mac with Apple Configurator 2, and a data-capable USB-C cable for
DFU recovery. Do not run `bputil`, `kmutil configure-boot`, or any boot-security
change until `../docs/safety.md` is satisfied and the machine owner has approved
the risk.
