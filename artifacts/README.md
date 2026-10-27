# Build artifacts

These are the outputs of the 2026-10-27 build on the `Mac16,3` / `J624` M4 iMac,
published as release **v0.7.0**.

The UEFI image changed since v0.6.0; the two hypervisor images are byte-identical
to v0.6.0 because no m1n1 source changed.

- the UEFI image now contains `AppleNANDStorageDxe`, a real driver for the
  internal ANS/ANS2 SSD: ASC mailboxes, SART windows, RTKit handshake,
  NVMMU/NVMe queues, `Identify`, and `EFI_BLOCK_IO_PROTOCOL`. It replaces a
  comment-only stub and is the first firmware in this project that attempts to
  address the internal disk. It has never run on hardware, so USB storage remains the
  install target until the first boot log says otherwise (`../docs/ans2.md`).
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
3bc227d442e2ac6258a86f3a3db6c81d5a352dd817cb54d996b04c3c21a19e02  MAC16_3_EFI.fd
ea4b4ba322d123dabfa127e9e7c70ee7b5aa1704c535e08b53c936703e641de7  m1n1-windows.bin
b2595ac056b23eed623d9a50158325714173975d1e2983efad17148f01f49ad9  m1n1-windows.macho
```

The m1n1 hashes come from a detached worktree of the pinned submodule commit
with `patches/m1n1-windows/0001`–`0003` applied. A recursive `diff -r` of that
tree against the working tree the patches were developed in reports no
difference outside `.git`, `build/` and `target/`, and both trees build the same
`m1n1.bin` byte for byte — so the hashes identify clean-checkout + patches, not
a dirty tree.

## What the UEFI image contains

- `T8132FamilyPkg` with `PcdSystemMemoryBase = 0x10000000000`,
  `PcdSystemMemorySize = 0x600000000` (24 GB fallback; PrePi patches it from the
  Apple boot args), 10 cores, two clusters.
- MADT: 10 GICC entries; E-core MPIDRs `0x0`–`0x5`, P-core MPIDRs
  `0x10100`–`0x10103`; GICD `0x5000000000`; GICR `0x5100000000`, length
  `0x140000`.
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
- `HV_QUIESCE_PCIE` in `config.h` restores upstream's `pcie_shutdown()` — it is
  **not** defined in this build.

## What is not in these images

- No MSI path for the guest. Apple's doorbell (`0xfffff000`, vector offset
  1506) works from the hardware's point of view, but nothing describes it to
  Windows, and the vGIC's ITS window handles only `GITS_CTLR`/`GITS_BASER*`.
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
