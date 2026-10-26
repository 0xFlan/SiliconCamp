# Status — 2026-10-26

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
| M4 vGIC topology patch | applied and builds |
| Patch integrity | each patch reproduces the submodule's whole working-tree diff (2026-10-26: the trees the artifacts came from) |
| NTASP M4 UEFI build | success, `artifacts/MAC16_3_EFI.fd` (30 MB) |
| M4 ACPI/image checker | `./scripts/check-acpi.py` passes; run by the build script |
| Checker regression tests | 35 pass (`python3 -m unittest discover -s tests`) |
| M4 ACPI tables | disassembled and checked (below) |

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
  `0x140000` (10 × `0x20000`).
- **PPTT**: 13 hierarchy nodes — one package (ACPI ID 0), a 6-core E-cluster
  (ACPI ID 0), a 4-core P-cluster (ACPI ID 1), and cores with ACPI IDs 0–9. The
  parent offsets point at the package and cluster nodes; this matches the MADT
  UID order.
- **DSDT**: one `DIE0` container, `CLU0` with CPU0–CPU5 and `CLU1` with CPU6–CPU9;
  all ten `ACPI0007` processor devices have `_UID` 0–9. `COM0` is
  `APPL8900` at `0x3ad200000` with interrupt 1046.
- **FADT**: desktop profile, hardware-reduced ACPI, PSCI compliant, SMC conduit.
- **DBG2**: points at `_SB.COM0`, using `PcdAppleUartBase` (`0x3ad200000`).

## Component status

| Component | Status | Notes |
|---|---|---|
| m1n1 hardware init on M4 | Partial | Upstream has T8132 CPU, NVMe, PCIe, SPTM/GXF work; Asahi still calls M4 bring-up incomplete. |
| m1n1-windows vGIC | Patched, builds | Original code lacked `T8132`; CPU count and PA window are now dynamic. Not hardware-tested. |
| PSCI | Present, builds | `hv_psci.c`; needs hardware validation on M4. |
| SMMU/DART abstraction | Stub | `hv_smmu.c` is 45 lines; not a real IOMMU. |
| vTPM | Stub | `hv_vtpm.c` is 16 lines; no TPM 2.0. |
| NTASP UEFI firmware | Builds | M2 reference and M4 `Mac16_3` package both build in the Lima VM. |
| M4 platform package | Builds, ACPI checked | `Silicon/Apple/T8132FamilyPkg` + `Platform/Mac16_3Pkg`; CPU/RAM/UART/PPTT match the J624 hardware. PCIe uses the captured J624 ECAM, bus window and host offset (`docs/pcie.md`); the DSDT root bridge, APCIE DARTs and ANS2 driver are still missing. |
| M4 USB path | Prepared, untested | Four DWC3 controllers are described by `PcdAppleNumDwc3Controllers`; the low 1 GB MMIO window (USB and USB DARTs) is mapped. The DWC3/XHCI stack still needs hardware validation. |
| NTASP AIC DXE | Partial | AICv1/v2; AICv3 is an explicit TODO. The M4 package uses the vGIC path. |
| NTASP Windows drivers | Empty | Repository is README-only. |
| Internal NVMe/ANS2 | Missing | The internal SSD is an Apple ANS2 device, not a standard PCIe NVMe device. `AppleNANDStorageDxe` is a 22-line stub with no DSC entry; a USB disk is the current install path. |
| Windows kernel boot | Not achieved | Existing project hangs at loader-to-kernel handoff even on M1/M2. |
| AGX GPU driver | None | No WDDM/D3D driver for any Apple Silicon GPU. |
| Anti-cheat/trust | None | No Secure Boot/TPM/VBS/HVCI/IOMMU certification path. |

## Immediate blockers, in order

1. **Hardware boot of m1n1 and the UEFI firmware.** The build and ACPI tables are
   coherent, but nothing has run on the M4. This is gated by `docs/safety.md`.
2. **PCIe and storage.** The host bridge and page tables now use the captured
   J624 ECAM (`0x1cb0000000`), 32-bit window (bus `0x80000000` → host
   `0xb80000000`, offset `0xb00000000`) and prefetch window (`0xbc0000000`).
   What remains is code, not capture: `PCI0`/`_CRS` in the DSDT, the APCIE
   DARTs, the four `apciec` complexes, and an ANS2 driver
   (`AppleNANDStorageDxe` is a stub). Until the driver exists, USB storage is
   the only installer target.
3. **Windows kernel handoff.** This is the project-wide blocker; it is not
   M4-specific, but M4 cannot bypass it.
4. **Drivers.** Even a booting kernel has no internal disk, display, input, or
   network on Apple hardware.
5. **GPU.** No path to a usable desktop or games without a WDDM driver.
6. **Trust.** Anti-cheat is a platform-certification problem, not just a driver.

## What each remaining epic needs first

Every item below is blocked on the machine, not on code:

| Epic | First action | Needs |
|---|---|---|
| Windows handoff | Boot m1n1 + UEFI once and capture the serial log. `exception.c` already prints ESR/ELR/FAR and "Unhandled exception, rebooting...", which localises the fault to a trap class. | the Mac, a data-capable USB-C cable, a serial capture |
| ANS2 storage | Port m1n1's `nvme.c` (581 lines: ANS mailbox, SART, Apple NVMe quirks) to a UEFI `BlockIo` driver, then to a Windows KMDF driver. | the machine to test against; the two drivers are separate projects |
| PCIe | Add `PCI0`/`_CRS` from the captured windows, enable the APCIE DARTs, then describe `apciec0`–`apciec3`. | the machine to validate BAR assignment and DMA; MSI goes through the vGIC ITS, not Apple's doorbell |
| Drivers (DCP, SPI, SMC, USB, Wi-Fi) | One KMDF driver per block; each needs the hardware. | the machine plus a Windows test-signing setup |
| GPU | No public WDDM driver for AGX exists; this is research, not a port. | GPU documentation/reverse engineering plus a machine |
| Trust | Secure Boot signing + iBoot key enrollment, TPM 2.0 (vTPM is a 16-line stub), SART/DART IOMMU. VBS/HVCI additionally needs Windows' hypervisor at EL2, which m1n1 occupies. | owner consent for boot-policy changes; `ID_AA64MMFR4_EL1` to check FEAT_NV/NV2 |

## Reproduce the build

```bash
./scripts/check-host.sh
./scripts/build-m1n1.sh
./scripts/build-uefi-linux.sh Mac16_3
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
git -C third_party/m1n1-windows apply ../../patches/m1n1-windows/0001-vgic-m4-topology.patch
./scripts/build-m1n1.sh
```

## Honest assessment

The 2026-10-26 audit re-decoded the `apcie` properties with `-d 0` and found that
both earlier revisions of the window were one hex digit short (`0xb8000000`
instead of `0xb80000000`). The PCDs, page tables, artifact and docs are now
consistent, and `docs/pcie.md` keeps the raw cells so the arithmetic is
checkable rather than memorable.

This repository now has a reproducible M4 hypervisor build, one concrete M4 vGIC
fix, and a compiling M4 UEFI firmware whose CPU, UART, MADT, and PPTT values match
this exact J624 machine. It does **not** have a Windows boot, and none of it has run
on the M4 hardware yet. The next meaningful milestone is not "Windows installer";
it is "m1n1 prints its vGIC/CPU topology on the M4, and the NTASP UEFI firmware
reaches its boot menu on the M4." Everything after that is debugging the handoff
and writing drivers.
