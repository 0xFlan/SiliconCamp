# The internal SSD: ANS/ANS2 from UEFI

The iMac's internal disk is not a PCIe NVMe device.  It is an **ANS/ANS2**
controller: a coprocessor running Apple firmware with an NVMe engine behind it,
address-translated by Apple's own units rather than by a PCIe function.  That is
why `MdeModulePkg/Bus/Pci/NvmExpressDxe` cannot see it — there is no PCIe
function to bind to — and why USB storage has been the only install target until
now.

`Silicon/Apple/AppleSiliconPkg/Drivers/AppleNANDStorageDxe` is a UEFI driver for
that controller, written from m1n1's protocol implementation.  It publishes the
internal SSD as an `EFI_BLOCK_IO_PROTOCOL` device, so the Windows installer sees
it as a normal disk.

> **Status: builds, links into the FD, never run on hardware.**  Everything below
> describes code and machine-checked properties, not observed behaviour.  The
> first hardware boot is what turns this into a driver.

## The four layers

| Layer | What it is | Where |
|---|---|---|
| ASC | Two 64-bit mailboxes between the AP and the coprocessor; there is no shared memory and no interrupt, so every message is polled. | `AppleAsc.c` |
| SART | A 16-entry window table that limits which DRAM the *coprocessor* may touch. Four register layouts exist; the ADT's `sart-version` selects one. | `AppleSart.c` |
| RTKit | The firmware's own protocol: HELLO/version negotiation, endpoint map, endpoint start, buffer requests, syslog, crashlog, power states. | `AppleRtkit.c` |
| NVMMU + NVMe | Apple's NVMe engine.  It does not walk a PRP list per command the way a PCIe controller does: each submission slot has an **NVMMU TCB** that carries the DMA direction, length and PRP pointers for the command in that slot. | `AppleNANDStorageDxe.c` |

The order matters and is not negotiable: the NVMe registers are dead until the
firmware has been booted through the mailbox, and the firmware will not boot
until it can hand the driver its syslog/crashlog buffers through the SART.

## Bring-up, in the order the code does it

1. **Find the device.** `dt_get("/arm-io/ans")`, then `reg[3]` for the NVMMU and
   `reg[9]` for the NVMe engine.  The `nvme-secure-bar` property selects that
   split layout; M1–M3 put both in `reg[3]` and have no such property.
2. **Allocate the queues.** 64 slots of three arrays each (commands,
   completions, TCBs), 16 KiB aligned, from `EfiBootServicesData`.
3. **Open the ASC mailbox.** `reg[0]` of the ANS node is the CPU control word;
   the mailboxes are at `reg[0] + 0x8000`.
4. **Read the SART.** `dt_get("/arm-io/sart-ans")`, `sart-version`, and record
   which entries the boot chain already owns — those are never touched.
5. **Boot the firmware.** Power the coprocessor, send `IOP_PWR_STATE(INIT)`,
   wait for `HELLO` (1 s), ack with the highest version both sides support
   (11–12), answer the endpoint map, start each endpoint the firmware advertised,
   wait for `IOP_PWR_STATE(ON)`, then send `AP_PWR_STATE(ON)` — that last message
   is what enables the firmware's logging.
6. **Wait for `NVME_BOOT_STATUS == 0xde71ce55`** at `0x1300`.
7. **Program the bridge.** `NVME_LINEAR_SQ_CTRL.EN` (TCB-per-slot mode),
   `NVME_MAX_PEND_CMDS_CTRL`, `NVMMU_NUM`, and the two TCB array bases.
8. **Enable the controller.** Clear `CC.EN`, wait for `CSTS.RDY` to clear, write
   `ASQ`/`ACQ`/`AQA`, set `CC.EN`, wait for `CSTS.RDY`.
9. **Create the IO queue pair** (admin commands 0x05 then 0x01, one CQ and one SQ
   of 64 entries, contiguous), then — on M4 only — write `NVME_IOQ_CQES` and
   `NVME_IOQ_CMDS` again.  Without those two writes the firmware crashes and its
   crashlog reads `I/O SQ: 0 / I/O CQ: 0`.
10. **Identify namespace 1** and controller 0, and publish `BlockIo`.

Every command goes through `AnsExecuteCommand`: fill slot 0's command and TCB,
fence, ring the doorbell, poll the completion queue for the phase bit, invalidate
the TCB (`NVMMU_TCB_INVAL`) and check `NVMMU_TCB_STAT`, advance the head, ring
the completion doorbell.  Between polls it drains the firmware mailbox, because
the firmware blocks when its syslog ring fills.

## What is machine-checked, and what is not

Checked on every build:

- **110 constants are compared against m1n1's sources** by
  `scripts/check-ans2-constants.py`: register offsets, opcodes, DMA direction
  flags, ASC mailbox registers, all four SART layouts (including the function-like
  register macros, evaluated at index 0) and every RTKit field position.  The
  first run of this checker found a real bug in this driver — the I2A receive
  registers were at the A2I offsets (`0x810`/`0x818` instead of `0x830`/`0x838`)
  — which is exactly the failure a compiler and a boot cannot catch.
- **The driver builds and links** as a DXE driver, and
  `scripts/build-uefi-linux.sh` fails the build if `AppleNANDStorageDxe` is not
  in the FV.  The ACPI checks cannot see inside the compressed FV, so this is
  checked against the Ffs output directly.
- **The structure sizes** (`APPLE_NVME_COMMAND` 64 B, `APPLE_NVME_COMPLETION`
  16 B, `APPLE_NVMMU_TCB` 128 B) are `STATIC_ASSERT`s, so a layout change is a
  compile error rather than a shifted field on hardware.

Not checked, because it needs the machine:

- That the ANS node on this iMac exposes `reg[9]`/`reg[3]` the way the code
  assumes, and that `/arm-io/sart-ans` exists with a supported `sart-version`.
  Both are logged (the SART dump prints all 16 entries with flags and ranges).
- That `Identify` returns usable data.  If it does not, the driver installs
  nothing and logs why, rather than inventing a geometry.
- DMA correctness at all.  The queues and the bounce buffer are ordinary
  `EfiBootServicesData` pages; the driver assumes the DXE identity map, so
  virtual and physical addresses are the same.  That is true of this firmware
  today and is a precondition for every DMA user in it.

## Deliberate limitations

- **One command per block.** A multi-block transfer is split into 4 KiB (or the
  namespace's LBA size) commands through a single, page-aligned bounce buffer.
  Apple's engine wants PRP lists for larger transfers; a single-page command is
  the shape m1n1 exercises on hardware.  This is slow — roughly one command per
  block — and is the first thing to optimise once the controller is known to
  work.  The bounce buffer also means `IoAlign` can stay 0: the caller's buffer
  never reaches the device.
- **The controller is left enabled** at `ExitBootServices`.  An OS driver that
  understands the ANS can adopt a running controller; one that starts from a
  stopped controller would have to reproduce the mailbox handshake itself.  No
  Windows ANS driver exists yet, so leaving it running is the only state a future
  driver can use.  Nothing in this driver runs after boot services end.
- **The NVMe flush command is not retried.**  It is issued from
  `FlushBlocks`, from `Reset`, and its failure is logged; the volatile write
  cache is reported as present, so an installer that trusts `WriteCaching` will
  flush.

## Reading the log on the first boot

Each step prints before it can hang, and the firmware's own syslog is printed
through the same UART as it arrives:

```
ans: compatible arm-io,ans
ans: T8132 layout, NVMe 0x... , NVMMU 0x..., die 0
sart: SARTv4 at 0x..., 2 protected entries     <- entries dumped above
rtkit(ans): booting with protocol version 12
rtkit(ans) syslog: [NVMe] ...                  <- firmware's own messages
ans: namespace 1: N blocks of 4096 bytes, LBAF 0
ans: model '...', serial '...', firmware '...'
ans: EFI_BLOCK_IO_PROTOCOL installed: N blocks x 4096 bytes
```

A stop after `rtkit(ans): no HELLO within 1s` means the ASC mailbox is not
reaching the coprocessor (wrong `reg[0]`, or the CPU control bit is not the
documented one).  A stop after `boot status is 0x..., expected 0xde71ce55` means
the firmware came up but its NVMe engine did not — the syslog lines above it are
the firmware's own explanation.  A crashlog dump (`rtkit(ans): IOP crashed`)
means the opposite: the engine was running and something the driver wrote killed
it — the M4 IO-queue writes in step 9 are the usual suspect.

## Why not just use m1n1's NVMe support?

m1n1 has an NVMe implementation, and it is what reads the kernel on the Asahi
path.  It cannot serve a UEFI boot: m1n1 is at EL2 and the firmware at EL1, the
boot services memory map and DMA rules are the firmware's, and the Windows
installer talks `EFI_BLOCK_IO_PROTOCOL`, not to a hypervisor.  Porting the
protocol into the firmware is the only way the installer can see the disk.

The two implementations now cannot drift silently: the constants are compared on
every CI run, and the driver's header cites m1n1's file for each layer.
