# SiliconCamp Assistant

A Boot Camp-shaped setup assistant for running Windows on Apple silicon. It is a
real macOS window, and three of the steps now do work rather than describe it:

- **Windows Installer** validates a Windows 11 Arm64 ISO (including the PE
  machine type of `BOOTAA64.EFI`), writes a bootable FAT32 UEFI installer to an
  external USB disk or a disk image, adds the Windows Setup answer file, and
  reads every file back.
- **Reserve Space for Windows** measures this Mac's APFS container and contains
  the real `diskutil` resize / FAT32 partition / restore engine. The engine runs
  end to end on a throwaway APFS image; the internal disk is gated until Windows
  can boot.
- **The walkthrough** reports the machine, the pinned image hashes, the boot
  chain, the blockers and the read-only repository checks.

The remaining gap is unchanged and on screen: Windows has never reached the
kernel on this hardware, so there is no boot path, no AGX graphics driver, and
the internal SSD driver has not run. The assistant does not partition the
internal disk or pretend to install Windows.

## Run the app

```bash
./tools/siliconcamp-assistant/build-app.sh
open "tools/siliconcamp-assistant/build/SiliconCamp Assistant.app"
```

The app is a small AppKit window around the loopback server. It starts
`server.py` on `127.0.0.1:8770` when the port is free, reuses a server that is
already running, and stops only the server it started. Its log is
`~/Library/Logs/SiliconCampAssistant.log`. The `Choose…` buttons are native
`NSOpenPanel`/`NSSavePanel` sheets: a WKWebView cannot give a page a real file
path, and the server needs a path it can open.

The bundle is ad-hoc signed and unsandboxed, because it reads this checkout and
writes installer media to a destination the user chooses. It does not embed
Python or download anything.

## Run the page

```bash
python3 tools/siliconcamp-assistant/server.py
```

Open <http://127.0.0.1:8770/>. Nothing beyond Python 3.9 and the repository
checkouts is required. Without the native shell the path fields are typed
instead of picked.

## The walkthrough

1. **Introduction** — what the assistant does, and the one fact that governs the
   rest: this Mac cannot boot Windows yet.
2. **Select Tasks** — the Boot Camp task list. Compatibility check, installer
   media and the space plan are selectable; drivers and Install are disabled
   with the reason each one is unavailable. Unselected tasks are skipped.
3. **Readiness** — the machine, the three pinned firmware images and their
   SHA-256 state, and the repository's read-only checkers.
4. **Windows Installer** — choose an ISO and a destination. The ISO is mounted
   read-only and inspected before anything is written; the destination is
   erased, formatted FAT32, populated, given `Autounattend.xml`, and verified in
   a background job whose log is on screen.
5. **Reserve Space for Windows** — the live APFS container, the macOS reserve,
   the largest partition that fits, and a slider for the size. **Allocate Space**
   is disabled with its reason. **Rehearse on a Disk Image** runs the actual
   shrink, FAT32 partition, mount check and restore against a throwaway APFS
   image and reports each command as it runs; the internal disk is untouched.
6. **Before You Continue** — tested backup, a second Mac for DFU, a data-capable
   cable and owner approval. Stored in `localStorage`, never sent to the server.
7. **Review** — the five blockers, the four-stage boot chain and the row-by-row
   Boot Camp gap.
8. **Install** — the disabled Install control and the boot-path, display and
   storage reasons.

## Installer media

`media.py` is the engine; `server.py` exposes it and runs one write job at a
time.

What it enforces before the first write:

- `EFI/BOOT/BOOTAA64.EFI`, `sources/boot.wim` and an install image
  (`.wim`, `.esd` or `.swm`) must be present, matched case-insensitively;
- `BOOTAA64.EFI` must be a PE image whose machine type is `0xAA64`; the file
  name is not treated as proof that the ISO is Arm64;
- two paths that differ only by case are rejected, because FAT32 cannot hold
  both;
- `sources/install.wim` over FAT32's 4 GiB file limit is split with
  `wimlib-imagex split` into `install.swm` parts, and the whole operation is
  refused when wimlib is absent rather than producing media Setup cannot use;
- the destination must be free of the payload, and a physical destination is
  re-checked as a whole, external, USB, non-virtual disk that is not the disk
  backing `/`, immediately before `eraseDisk`;
- the media is one MBR FAT32 partition, the layout Microsoft documents for a
  flash drive. `diskutil`'s GPT layout silently inserts an empty 209.7 MB EFI
  System Partition and puts the payload on a second partition, which a strict
  UEFI boot manager would not scan for `\EFI\BOOT\BOOTAA64.EFI`;
- the copy excludes the source `install.wim` when splitting, and macOS's
  AppleDouble `._*` files and `.fseventsd` are removed from the media before
  the read-back.

Verification reads every copied file back: size for all of them and SHA-256 for
files up to 32 MiB, plus the presence of the removable-media boot path. A failed
build removes the disk image it created, so a retry is not blocked by a partial
file.

### Autounattend.xml

The media root carries an answer file, generated by `render_unattend()` and
validated before and after it is written. It:

- accepts the EULA for this unattended run;
- sets the setup language and input locale to `en-US`;
- runs `reg add HKLM\SYSTEM\Setup\LabConfig` for `BypassTPMCheck`,
  `BypassSecureBootCheck`, `BypassRAMCheck` and `BypassCPUCheck`, because Apple
  silicon has no TPM 2.0 and the firmware does not implement Microsoft's Secure
  Boot database.

It deliberately contains no `DiskConfiguration` or `ImageInstall` element. A
fixed disk number on a machine with several disks could format the wrong volume,
so partition selection stays in Setup's own UI. `validate_unattend()` rejects a
file that gains either element, a missing gate, a missing EULA or a non-arm64
component.

`create` never runs `sudo` and never writes to an internal disk. Erasing a
physical USB disk needs both a per-run token from the page and a second click
that names the device.

## Space allocation

`allocation.py` measures and writes. The write is three `diskutil` operations:

```
diskutil apfs resizeContainer <container> <capacity - requested>
diskutil addPartition <physical store> "MS-DOS FAT32" BOOTCAMP <requested>g
diskutil eraseVolume free none <partition>          # restore only
diskutil apfs resizeContainer <container> 0         # restore only
```

Measured details it encodes:

- `diskutil`'s `g` suffix is decimal, matching Windows' and Boot Camp's GB;
- `addPartition` must name the container's **physical store** (`disk0s2`), not
  the synthesised APFS node (`disk3`) — `diskutil` refuses the latter with "the
  disk must contain a partitioning scheme";
- the container is shrunk by exactly the requested size first, then the
  partition is created, so leftover free space is not silently swallowed;
- a failed add, format or verification erases any partition it created and
  grows the container back, and reports if the rollback itself failed.

`BOOT_PATH_READY = False` makes `apply_space()` and `remove_space()` refuse the
container that holds `/` before the first write. This is the policy gate, not a
missing implementation: `rehearse_space()` runs the same functions against a
fresh APFS disk image, verifies the mounted FAT32 `BOOTCAMP` volume, deletes it,
restores the container and removes the image. It takes about 10–12 seconds and
needs no hardware.

## Serial capture

The hardware session is judged by the console log, so `serial.py` captures it
without a watch window:

```bash
python3 tools/siliconcamp-assistant/serial.py ports
python3 tools/siliconcamp-assistant/serial.py capture \
  --device /dev/cu.usbmodem1101 --out probes/m4-first-boot.log
```

It opens the call-out device, sets raw 115200 8N1 (no echo, no flow control),
and timestamps each line with a monotonic gap in mind. Ctrl-C ends the capture
and still writes the footer with the byte and line count, so a truncated log is
obvious. `--seconds` bounds a run, `--echo` also prints to the terminal, and
`ports` lists every `/dev/cu.*` device with likely USB-UART adapters first. It
is tested against a pseudo-terminal, which exercises the same termios and read
path as a real adapter.

## What is not done

The internal-disk resize remains unavailable by policy. Taking tens of gigabytes
out of the APFS container to hold an installation that cannot boot would cost
the user capacity for nothing. `apply_space()` is complete and rehearsed; when
the loader-to-kernel handoff works, `BOOT_PATH_READY` becomes `True` and the
Allocate control follows `plan.apply.enabled` without further changes.

The assistant still cannot install Windows, make Windows boot, or give you a
desktop on this hardware. The answer file gets Setup past Microsoft's hardware
checks; it cannot manufacture a Windows kernel that reaches the display.

## API

| Endpoint | Purpose |
|---|---|
| `GET /api/status` | machine, repo, artifacts, blockers, roadmap, diagnostics |
| `POST /api/check` | run one allow-listed repository checker |
| `GET /api/installer` | external USB disks, wimlib path, boot disk |
| `POST /api/installer/inspect` | mount, validate and detach an ISO |
| `POST /api/installer/plan` | show what would be written |
| `POST /api/installer/create` | start the write job (token required) |
| `GET /api/jobs/<id>` | job state, transcript and result |
| `GET /api/allocation` | live APFS container and the default plan |
| `POST /api/allocation/plan` | plan a partition for a requested size |
| `POST /api/allocation/rehearse` | run the resize and restore on a throwaway image (token required) |

## Safety

- Binds to `127.0.0.1` only; no outbound requests.
- Serves files from `web/` only, with path-traversal checks and a CSP that does
  not allow inline script or external origins.
- The only command execution outside the media and allocation engines is the
  fixed `CHECKS` allow-list; no request body can name a command.
- Write endpoints require `X-SiliconCamp-Token`, a per-run secret injected into
  the page, and reject a foreign `Origin`. Another web page cannot drive the
  loopback server into erasing a disk or resizing a container.
- One destructive job at a time.
- The panic-button property: with no Windows boot path, the Install control and
  the internal Allocate control stay disabled, and the internal disk is never a
  media destination.

## Files

| File | Purpose |
|---|---|
| `server.py` | loopback HTTP server, token, job runner, status API |
| `media.py` | ISO and PE validation, destination checks, FAT32 media, answer file, verification |
| `allocation.py` | APFS measurement, resize/partition/restore, disk-image rehearsal |
| `serial.py` | raw 115200 8N1 console capture with timestamps and a counted footer |
| `web/index.html` | the eight-step walkthrough |
| `web/app.css` | window, sidebar, panes, controls, plan boxes |
| `web/app.js` | navigation, tasks, installer job, space slider, rehearsal job, diagnostics |
| `mac/SiliconCampAssistant.swift` | AppKit window, WKWebView, native file pickers, server lifecycle |
| `mac/Info.plist` | bundle identity and minimum macOS version |
| `mac/make-icon.swift` | draws the 1024px app icon |
| `mac/AppIcon.png` | committed icon source |
| `build-app.sh` | compiles and packages `SiliconCamp Assistant.app` |

## Tests

```bash
python3 -m unittest tests.test_siliconcamp_assistant
```

51 tests. They cover the status payload, the checker allow-list, skip handling,
dirty-submodule classification, the step order, local-only assets, the token
boundary, the media engine's validation (including an x64 `BOOTAA64.EFI` and a
non-PE file), the answer file's contents and its refusal of disk configuration,
the APFS arithmetic, the write gate on the system container, and the engine
itself: three end-to-end media builds against real disk images (plain, single
MBR FAT32 shape, oversized `install.wim` split), a failed build that must leave
no image behind, and a full resize + restore rehearsal on a throwaway APFS
image. The disk-image tests skip on Linux CI; everything else runs there.

`python3 -m unittest tests.test_siliconcamp_serial` adds four tests for the
capture tool, including a pseudo-terminal run that checks the header, per-line
timestamps, the unterminated last line and the footer counts.

The native shell is verified by launching it with `--screenshot <path>`: the app
starts or reuses its server, renders the page at 1080×720, writes one PNG and
exits.

## Limits

This is not an installer and not Boot Camp. It cannot install Windows, cannot
make Windows boot, and cannot give you a working desktop on this hardware. What
it can do is prepare the media, write the answer file, and prove the partition
engine, so those are done when the boot path lands.
