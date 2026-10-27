# Bring-up plan

This is ordered by dependency, not by excitement. Do not start the GPU or anti-cheat before the platform boots.

## Phase 0 — Reproducible host build (done)

- macOS arm64 host, Homebrew LLVM/LLD, Rust target, QEMU.
- Upstream m1n1 and NTASP m1n1-windows both build.
- M4 vGIC topology and PSCI T8132 patches applied (six E-cores, four P-cores).
- The vGIC guest contract is fixed and checked offline (patch 0004,
  `scripts/check-vgic.py`), and its diagnostics no longer print on the hot path
  (patch 0005, `docs/vgic-diagnostics.md`).

**Exit criteria:** `./scripts/build-m1n1.sh` produces `m1n1.bin` and `m1n1.macho` with no errors.

## Phase 1 — m1n1 on the M4 (hardware, gated)

This is the first hardware step and the first irreversible one. It requires the safety checklist.

- Back up the Mac (Time Machine) and verify the backup.
- Have a second Mac with Apple Configurator 2 and a USB-C cable for DFU recovery.
- Confirm the machine owner accepts the risk of a DFU restore.
- Set the boot policy to Permissive Security only for the experiment.
- Install `m1n1.bin` with the modern raw path, or boot it through the project's existing recovery flow.
- Capture the m1n1 serial/UART log.

**Exit criteria:** m1n1 reaches its console on the M4 and prints the vGIC topology line:

```
HV vGIC: 10 CPUs, 42-bit PA, dist=0x5000000000 redist=0x5100000000 its=0x5200000000
```

and the PSCI initialization agrees with it:

```
PSCI DEBUG: topology tree selected
PSCI DEBUG: Number of cores for PSCI nodes is 10
PSCI DEBUG: Number of clusters for PSCI nodes is 2
```

The exact PA width may be 42 or larger; the patch chooses the 42-bit window for
anything above 36 bits. If it prints `no CPU nodes found`, the ADT CPU parsing is
wrong for this M4. A `PSCI: topology has N cores but the ADT has M` line means
the descriptor and the machine disagree; stop and fix before proceeding.

## Phase 2 — NTASP UEFI on the M4

**Status:** the M4 firmware builds. Its CPU topology, memory alias, UART, MADT, PPTT, DSDT processor containers, MCFG and `PCI0` `_CRS` have been checked against the J624 ioreg and Asahi's `t8132.dtsi`. It has not run on hardware.

Done:

- Built the UEFI firmware in a Linux environment (Project Mu `stuart`).
- Added the `T8132` / `Mac16,3` platform package using the 1 TB DRAM alias.
- Matched the MADT GIC distributor/redistributor addresses to the vGIC window.
- Set the ACPI CPU entries to the 10-core topology (6E + 4P), with the DSDT
  containers and PPTT core IDs in the same order.
- Corrected the UART to `0x3ad200000`, AIC IRQ 1046.
- Added the low 1 GB MMIO window used by the M4 USB DWC3 controllers and DARTs.
- Set the APCIE host bridge and page tables from the captured J624 windows
  (ECAM `0x1cb0000000`, bus `0x80000000` → host `0xb80000000`, prefetch
  `0xbc0000000`); see `docs/pcie.md`.
- Added MCFG and `PCI0`/`_CRS` for the first APCIE root complex, with `RES0`
  reserving the 256 MB ECAM. `check-acpi.py` pins the bus range, both windows,
  the translation, and the ECAM reservation.
- Added a MADT GIC MSI Frame for SPIs 512-767 at Apple's APCIE doorbell
  (`0xfffff000`), and the hypervisor half in `guest_msi.c`: port MSIMAP
  programming with read-back, AIC unmask of 1506-1761, and AIC-to-INTID
  translation in the EL2 IRQ path. `check-acpi.py` compares the frame against
  `src/guest_msi.h` so the two ends cannot drift apart (`docs/pcie.md`).
- Compile out the vGIC's per-access logging and replace it with bounded,
  one-shot diagnostics (patch 0005; `docs/vgic-diagnostics.md`). Nothing on the
  first boot is readable without it.

Next:

- Add a vGIC conformance self-test to the firmware image: walk every
  redistributor frame via `GICR_TYPER`, read-modify-write `GICR_WAKER` on each
  and poll `ChildrenAsleep` with a timeout, confirm both `RWP` bits read 0,
  enable the GTDT timer PPI, and send a self-SGI. It is the only way to observe
  the contract before Windows exists (`docs/next-steps.md`).
- Chainload the UEFI firmware from m1n1 on the M4 (gated by the safety checklist).
- Capture the UEFI serial log and check the memory map, CPU count, and ACPI
  table addresses at runtime.
- Bring up the USB DWC3/XHCI path so a USB keyboard and USB installer are usable.
- Validate the MSI path: the frame, the MSIMAP programming and the translation
  are written and checked offline, but the first boot has to show that the port
  map is where `pcie.c` assumes (`0x3800`), that `msi-vector-offset` is really
  the AIC interrupt of vector 0, and that Windows accepts a MSI frame without an
  IORT. The log prints each of those numbers (`docs/pcie.md`).
- Describe the four `apciec` root complexes (ECAMs, bus ranges, apertures) so
  Thunderbolt/USB4 devices can appear; `PCI0` currently covers only
  `apcie@B0000000`.
- Add `_PRT`/IORT once the first boot says whether Windows needs them; neither is
  invented in advance.

**Exit criteria:** the UEFI boot menu appears on the M4's display or over serial, and the firmware reports the correct CPU count and memory map.

## Phase 3 — Windows loader and kernel handoff

This is the existing project's unsolved problem. Expect to spend most of the effort here.

- Boot a Windows Arm64 USB installer or WinPE.
- Capture the loader log and the point of the hang.
- Use WinDbg over serial/USB; enable Windows kernel debugging early.
- Investigate, in order:
  - EFI memory map and runtime services;
  - ACPI MADT/GICR/GTDT correctness (the vGIC *contract* is already checked
    offline; what remains is whether the guest's use of it matches — see
    `docs/next-steps.md`);
  - PSCI CPU_ON and timer state;
  - vGIC maintenance interrupt and LR injection;
  - stage-2 mappings for the loader/kernel;
  - SError/exception routing.

**Exit criteria:** Windows kernel debugger output appears, then WinPE boots, then the installer starts.

## Phase 4 — Usable desktop

Drivers, in dependency order:

1. ANS NVMe (internal disk) and DART.
2. DCP display (or GOP framebuffer first).
3. SPI/dockchannel keyboard and trackpad.
4. SMC (fans, power, thermal).
5. PCIe, USB, Ethernet/Wi-Fi/Bluetooth, audio.

**Exit criteria:** Windows installs to the internal SSD and can be used with the built-in display, keyboard, and trackpad.

## Phase 5 — GPU

- Reverse-engineer the M4 AGX firmware ABI (or reuse Asahi's work when available).
- Write a WDDM kernel-mode driver and a D3D12 user-mode driver.
- Build the shader compiler and memory-management path.
- Validate with DWM, then D3D11/12 samples, then games.

**Exit criteria:** hardware-accelerated desktop and at least one D3D11 title.

## Phase 6 — Trust and anti-cheat

- Secure Boot with a Microsoft-trusted chain.
- TPM 2.0 (fTPM or SEP-backed).
- VBS/HVCI: requires Windows' hypervisor; conflicts with m1n1's EL2 vGIC unless nested virtualization works.
- IOMMU: DART-to-SMMU abstraction.
- Production driver signing.
- Anti-cheat vendor certification, per game.

**Exit criteria:** one anti-cheat-protected game officially runs. This is a business/trust milestone, not just code.

## Realistic effort

| Phase | Team | Time |
|---|---|---|
| 0 | 1 | done |
| 1 | 1–2 + hardware/recovery | days–weeks |
| 2 | 2–4 | months |
| 3 | 2–5 | 12–24 months |
| 4 | 5–10 | 2–4 years |
| 5 | 10–20+ | 4–8+ years |
| 6 | security/business team | vendor-dependent |

These are estimates, not commitments. The existing volunteer project is years in and still pre-alpha.
