# Building the NTASP UEFI firmware

**Status (2026-10-25):** both the M2 reference package and the new M4 `Mac16_3` package build successfully. The M4 image is `Build/Mac16_3-AARCH64/DEBUG_CLANGPDB/FV/MAC16_3_EFI.fd` (30 MB). Its MADT, PPTT, FADT, DBG2, and GTDT are now checked automatically by `scripts/check-acpi.py`, which the build script runs (10 cores, 2 clusters, vGIC bases, PSCI/SMC, UART, FD header).

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
and fails loudly when a submodule moves under the patches.

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
- FADT: hardware-reduced, PSCI over SMC;
- DBG2: debug UART at `0x3ad200000`;
- GTDT: EL1 timer 17, virtual timer 18, no memory-mapped frames;
- the FD: ARM64 Image header, `text_offset` `0x80000`, and an `image_size` that
  covers the payload.

Run it on its own with `./scripts/check-acpi.py`; pass `--build-dir` for a build
tree somewhere else.

Expectations are per platform, in the `PLATFORMS` table at the top of the
script, and only `Mac16_3` has validated entries. Any other platform is an
error, not a pass: the M2 reference package would have failed five of the M4
assertions, so a generic checker would have been actively misleading. To bring
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
  M4 virtual memory map with the low 1 GB USB window.
- `Platform/Mac16_3Pkg` — Mac16,3/J624 platform DSC/FDF, 6E+4P DSDT, ACPI
  device tables, vGIC build.

Verified values:

- 6 E-cores at MPIDR `0x0`–`0x5`, 4 P-cores at `0x10100`–`0x10103`.
- 24 GB DRAM at the 1 TB alias; FD at 1 TB + 3 GB; boot args/ADT at 1 TB + 8.5 GB.
- UART `0x3ad200000` with AIC IRQ 1046.
- Four DWC3 controllers; the low 1 GB MMIO window covers the USB blocks and DARTs.

The ARM64 Image header in `Mac16_3.fdf` carried upstream's `image_size`
(`0x100E0000`, larger than the entire FD). It now matches the declared FD size
(`0x1E00000`). m1n1 ignores the field, but a loader that trusts it does not, and
`scripts/check-acpi.py` fails the build if the field stops covering the payload.

The following remain provisional and must be checked on hardware or before PCIe is
exposed to Windows:

- PCIe ECAM/MMIO windows for each `apcie`/`apciec` root complex. The UEFI host
  bridge still uses the T602X PCDs, and PCIe is omitted from the DSDT.
- ANS2/SART storage for the internal SSD; USB storage is the current install path.
- DCP/display handoff beyond the iBoot framebuffer.

See `docs/hardware-values.md` for the source of every value. The firmware compiles
and its ACPI tables are statically checked, but compiling is not booting. Hardware
validation is gated by `docs/safety.md`.
