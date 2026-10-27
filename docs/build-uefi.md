# Building the NTASP UEFI firmware

**Status (2026-10-27):** both the M2 reference package and the new M4 `Mac16_3` package build successfully. The M4 image is `Build/Mac16_3-AARCH64/DEBUG_CLANGPDB/FV/MAC16_3_EFI.fd` (30 MB). Its MADT, PPTT, FADT, DSDT, MCFG, DBG2, and GTDT are checked automatically by `scripts/check-acpi.py`, which the build script runs (10 cores, 2 clusters, vGIC bases, PSCI/SMC, UART, DSDT `_UID` tree and `COM0` `_CRS`, `PCI0` bus/window/ECAM contract, FD header).

The NTASP firmware uses Project Mu's `stuart` build system. The project's own documentation says it is tested on Linux, not macOS, so this repo uses a Lima Ubuntu 24.04 VM on the M4 host.

## 1. Lima VM

```bash
brew install lima
limactl start --name=mu-build --tty=false --vm-type=vz \
  --cpus=4 --memory=8 --disk=40 --mount-writable template:ubuntu-24.04
```

Lima mounts your home directory read-only by default; the `--mount-writable`
flag above makes it writable, and the repository stays visible inside the VM at
the same absolute path. Set `REPO` to that path in the snippets below.

## 2. Guest packages

```bash
limactl shell mu-build -- bash -lc '
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  build-essential clang lld llvm uuid-dev acpica-tools \
  python3 python3-venv python3-pip git \
  gcc-aarch64-linux-gnu binutils-aarch64-linux-gnu \
  pkg-config libssl-dev
'
```

## 3. Python environment

```bash
REPO="$(git rev-parse --show-toplevel)"   # run from inside this repository
limactl shell mu-build -- bash -lc "
cd $REPO/third_party/ntasp-firmware
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r pip-requirements.txt
python -m pip install 'setuptools<81'
"
```

`setuptools<81` is required because `edk2toolext` 0.27.6 imports `pkg_resources`, which newer setuptools versions no longer provide.

## 4. Project Mu dependencies

The first `stuart_setup` initializes the Project Mu submodules:

```bash
REPO="$(git rev-parse --show-toplevel)"
limactl shell mu-build -- bash -lc "
cd $REPO/third_party/ntasp-firmware
. .venv/bin/activate
export CLANGPDB_BIN=/usr/lib/llvm-18/bin/
export CLANGPDB_AARCH64_PREFIX=aarch64-linux-gnu-
stuart_setup -c Platform/MacBookProEarly2023Pkg/PlatformBuild.py TOOL_CHAIN_TAG=CLANGPDB
"
```

### NuGet workaround

Project Mu's `cibuild` scope pulls `mu_nasm`, `edk2-acpica-iasl`, and `mu-uncrustify-release` from Azure DevOps feeds. Those fetches fail in this environment. For local firmware builds:

- `Platform/Mac16_3Pkg/PlatformBuild.py` and `Platform/MacBookProEarly2023Pkg/PlatformBuild.py` no longer include `'cibuild'` in their scopes.
- `MU_BASECORE/BaseTools/Bin/nasm_ext_dep.yaml` is scoped to `cibuild`, so it is not required for an AArch64 build.
- `iasl` comes from Ubuntu's `acpica-tools`.
- Uncrustify is a CI formatting check and is not needed for a build.

These are local build-environment changes, not changes to the firmware behavior. If you want the untouched upstream behavior, restore those files and fix the NuGet feed access instead.

## 5. Build

If you started from a clean NTASP checkout, apply the project's M4 patch first:

```bash
./scripts/apply-ntasp-patches.sh
```

The script applies `patches/ntasp-firmware/0001-m4-uefi-platform.patch` and, once
`MU_BASECORE` is initialized, `0002-nasm-scope.patch`. It is idempotent.

`./scripts/verify-patches.sh` checks the same patches against the exact commits
the submodules pin, using throwaway git worktrees, so it passes on a dirty tree
and fails loudly when a submodule moves under the patches. Applying them to a
clean checkout reproduces the build trees inside this repository exactly (checked
2026-10-26 by diffing a patched worktree against the working tree), and those
trees produced the released artifacts.

The validated reference platform is the M2 MacBook Pro package:

```bash
./scripts/build-uefi-linux.sh MacBookProEarly2023
```

The M4 package is:

```bash
./scripts/build-uefi-linux.sh Mac16_3
```

Outputs are under:

```
third_party/ntasp-firmware/Build/<PLATFORM>-AARCH64/DEBUG_CLANGPDB/FV/<PLATFORM>_EFI.fd
```

### Verification

`build-uefi-linux.sh` runs `scripts/check-acpi.py` after the build and fails if
an invariant breaks. It parses the tables the build emits and checks:

- every header (signature, length, checksum — a zero checksum is accepted
  because `AcpiPlatformDxe` fills it in when it installs the table);
- MADT: 10 GICC entries, UIDs 0–9, MPIDRs `0x0`–`0x5` then `0x10100`–`0x10103`,
  efficiency classes 0/1, GICD `0x5000000000`, GICR range
  `0x5100000000`+`0x140000`;
- PPTT: one package, two clusters, ten cores, core IDs 0–9 in MADT order;
- DSDT: every CPU/CLU/DIE device present and nested in the right container,
  each `_UID` matching the MADT order, every CPU `ACPI0007`, and `COM0` an
  `APPL8900` whose `_CRS` is the platform UART base, length and IRQ (parsed from
  the AML, not found by string search);
- DSDT `PCI0`: `PNP0A08`/`PNP0A03`, bus 0–8, the 32-bit bus window with its
  host translation, the prefetch window, and `RES0`'s 256 MB ECAM reservation;
- MCFG: one ECAM entry at `0x1cb0000000`, segment 0, buses 0–8;
- FADT: desktop PM profile, hardware-reduced, PSCI over SMC;
- DBG2: debug UART at `0x3ad200000`;
- GTDT: EL1 timer 17, virtual timer 18, no memory-mapped frames;
- the FD: ARM64 Image header, `text_offset` `0x80000`, and an `image_size` that
  covers the payload.

Run it on its own with `./scripts/check-acpi.py`; pass `--build-dir` for a build
tree somewhere else.

Expectations are per platform, in the `PLATFORMS` table at the top of the
script, and only `Mac16_3` has validated entries. Any other platform is an
error, not a pass: the M2 reference package would have failed five of the M4
assertions, so a generic checker would have been actively misleading.
`build-uefi-linux.sh` therefore passes `--unvalidated-ok` for every platform
except `Mac16_3`: the M2 reference build proves the toolchain and finishes with
a note instead of failing M4 assertions. Use that flag only for reference
builds, never for the M4. To bring
up a new SoC (an M4 Pro/Max, say), build it once, then

```bash
./scripts/check-acpi.py --print-topology --build-dir <tree>/AARCH64
```

prints a ready-to-paste `Platform` entry with the core MPIDRs, efficiency
classes, vGIC bases and UART address read from the build. Verify every value
against the machine and record its source in `docs/hardware-values.md` before
adding it to `PLATFORMS`; the checker never treats a recorded value as
authoritative on its own.

The checker has its own regression tests (`tests/test_check_acpi.py`). They run
it against the real tables from an M4 build, kept as fixtures under
`tests/fixtures/mac16_3/`, and against copies with one field mutated, so a
change that makes the checker accept bad input fails CI rather than the build:

```bash
python3 -m unittest discover -s tests -v
```

## 6. Current M4 package

The M4 package now uses values taken from this exact J624 machine:

- `Silicon/Apple/T8132FamilyPkg` — T8132 SoC PCDs, 10-core MADT, 6E+4P PPTT,
  M4 virtual memory map with the low 1 GB USB window and the J624 PCIe windows.
- `Platform/Mac16_3Pkg` — Mac16,3/J624 platform DSC/FDF, 6E+4P DSDT with
  `PCI0`, MCFG, ACPI device tables, vGIC build.

Verified values:

- 6 E-cores at MPIDR `0x0`–`0x5`, 4 P-cores at `0x10100`–`0x10103`.
- 24 GB DRAM at the 1 TB alias; FD at 1 TB + 3 GB; boot args/ADT at 1 TB + 8.5 GB.
- UART `0x3ad200000` with AIC IRQ 1046.
- Four DWC3 controllers; the low 1 GB MMIO window covers the USB blocks and DARTs.
- PCIe ECAM `0x1cb0000000` (256 MB), buses 0–8, 32-bit window bus `0x80000000`
  → host `0xb80000000`, prefetch `0xbc0000000`+512 MB. MCFG and `PCI0` `_CRS`
  carry the same values, and `check-acpi.py` fails the build if they diverge.

The ARM64 Image header in `Mac16_3.fdf` carried upstream's `image_size`
(`0x100E0000`, larger than the entire FD). It now matches the declared FD size
(`0x1E00000`). m1n1 ignores the field, but a loader that trusts it does not, and
`scripts/check-acpi.py` fails the build if the field stops covering the payload.

The following remain provisional and must be checked on hardware:

- PCIe beyond the first root complex. The `apcie@B0000000` ECAM, bus window,
  host offset and prefetch window are captured and described (`docs/pcie.md`),
  `PcdPciMmio32Translation` carries the same `host - bus` value as the DSDT
  `_TRA`, and the m1n1 hand-off maps the windows and bypasses the APCIE DARTs.
  What is untested is MSI: no interrupt path to the guest is described yet. The
  four `apciec` complexes also have their own apertures that a single
  host-bridge descriptor cannot represent.
- ANS2/SART storage for the internal SSD; USB storage is the current install path.
- DCP/display handoff beyond the iBoot framebuffer.

See `docs/hardware-values.md` for the source of every value. The firmware compiles
and its ACPI tables are statically checked, but compiling is not booting. Hardware
validation is gated by `docs/safety.md`.
