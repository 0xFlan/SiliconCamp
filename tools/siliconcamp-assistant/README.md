# SiliconCamp Assistant

A Boot Camp-shaped setup assistant for running Windows on Apple silicon. It is a
real macOS window, and it now does two of the steps rather than describing them:

- **Windows Installer** validates a Windows 11 Arm64 ISO and writes a bootable
  FAT32 UEFI installer to an external USB disk or to a disk image, then reads
  every file back.
- **Reserve Space for Windows** measures this Mac's APFS container and plans the
  partition Boot Camp used to create.

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
   erased, formatted FAT32, populated and verified in a background job whose log
   is on screen.
5. **Reserve Space for Windows** — the live APFS container, the macOS reserve,
   the largest partition that fits, and a slider for the size. The Allocate
   button is disabled with its reason; see "What is not done" below.
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

`create` never runs `sudo` and never writes to an internal disk. Erasing a
physical USB disk needs both a per-run token from the page and a second click
that names the device.

## What is not done

The internal-disk resize is the one Boot Camp step this does not perform. Taking
tens of gigabytes out of the APFS container to hold an installation that cannot
boot would cost the user capacity for nothing, so `allocation.BOOT_PATH_READY`
is `False` and the Allocate button is disabled with that reason. The plan
itself is measured, not estimated: capacity, free space, the reserve (20 GB or
10% of free, whichever is larger) and the resulting partition come from
`diskutil apfs list` for the container that holds `/`.

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

## Safety

- Binds to `127.0.0.1` only; no outbound requests.
- Serves files from `web/` only, with path-traversal checks and a CSP that does
  not allow inline script or external origins.
- The only command execution outside the media engine is the fixed `CHECKS`
  allow-list; no request body can name a command.
- Write endpoints require `X-SiliconCamp-Token`, a per-run secret injected into
  the page, and reject a foreign `Origin`. Another web page cannot drive the
  loopback server into erasing a disk.
- One destructive job at a time.
- The panic-button property: with no Windows boot path, the Install control
  stays disabled and the internal disk is never a destination.

## Files

| File | Purpose |
|---|---|
| `server.py` | loopback HTTP server, token, job runner, status API |
| `media.py` | ISO validation, destination checks, FAT32 media, verification |
| `allocation.py` | APFS measurement and the BOOTCAMP space plan |
| `web/index.html` | the eight-step walkthrough |
| `web/app.css` | window, sidebar, panes, controls, plan boxes |
| `web/app.js` | navigation, tasks, installer job, space slider, diagnostics |
| `mac/SiliconCampAssistant.swift` | AppKit window, WKWebView, native file pickers, server lifecycle |
| `mac/Info.plist` | bundle identity and minimum macOS version |
| `mac/make-icon.swift` | draws the 1024px app icon |
| `mac/AppIcon.png` | committed icon source |
| `build-app.sh` | compiles and packages `SiliconCamp Assistant.app` |

## Tests

```bash
python3 -m unittest tests.test_siliconcamp_assistant
```

37 tests. They cover the status payload, the checker allow-list, skip handling,
dirty-submodule classification, the step order, local-only assets, the token
boundary, the media engine's validation and refusals, the APFS arithmetic, and
three end-to-end media builds against real disk images: one plain, one inspected
as a single MBR FAT32 partition, one with an oversized `install.wim` split by a
stand-in splitter, plus a failed build that must leave no image behind. The
disk-image tests skip on Linux CI; everything else runs there.

The native shell is verified by launching it with `--screenshot <path>`: the app
starts or reuses its server, renders the page at 1080×720, writes one PNG and
exits.

## Limits

This is not an installer and not Boot Camp. It cannot install Windows, cannot
make Windows boot, and cannot give you a working desktop on this hardware. What
it can do is prepare the media and measure the partition, so those are done when
the boot path lands.
