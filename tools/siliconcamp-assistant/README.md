# SiliconCamp Assistant

A Boot Camp-shaped setup walkthrough for the SiliconCamp bring-up. It is a real
macOS window, and it is deliberately **not an installer**.

SiliconCamp can build m1n1, the NTASP UEFI firmware and the vGIC self-test. Windows
has never booted: the loader-to-kernel handoff is unsolved, there is no AGX
graphics driver, and the internal SSD driver has not run on hardware. The
walkthrough shows those facts in the order Boot Camp Assistant used to present
its own steps, instead of hiding them behind an Install button that cannot work.

## Run the app

```bash
./tools/siliconcamp-assistant/build-app.sh
open "tools/siliconcamp-assistant/build/SiliconCamp Assistant.app"
```

The app is a small AppKit window around the existing loopback server. It starts
`server.py` on `127.0.0.1:8770` when the port is free, reuses a server that is
already running, and stops only the server it started. Its log is
`~/Library/Logs/SiliconCampAssistant.log`.

The bundle is ad-hoc signed and unsandboxed, because it reads this checkout and
runs the repository's own checkers. It does not embed Python, download anything,
or write to a disk.

## Run the page

```bash
python3 tools/siliconcamp-assistant/server.py
```

Open <http://127.0.0.1:8770/>. There are no dependencies beyond Python 3.9 and
the repository checkouts. The browser version draws a window frame around the
same walkthrough; the native app uses the real traffic lights and window.

## The walkthrough

1. **Introduction** — what the assistant does, what is ready, and the one fact
   that governs the rest: this Mac cannot install Windows yet.
2. **Select Tasks** — the Boot Camp task list, updated honestly. The
   compatibility check is available; the installer, support-software download
   and Windows install are disabled with the reason each one is unavailable.
3. **Readiness** — the machine, the three pinned firmware images and their
   SHA-256 state, and the repository's read-only checkers.
4. **Safety** — tested backup, a second Mac for DFU, a data-capable cable and
   owner approval. They are stored in `localStorage`, never sent to the server,
   and they do not enable installation.
5. **Review** — the five blockers, the four-stage boot chain, and the row-by-row
   Boot Camp gap.
6. **Install** — the disabled Install control, with the boot-path, display and
   storage reasons on screen.

The sidebar is decorative, as it was in Boot Camp; the only navigation is
Go Back and Continue. Returning to an earlier step does not reset the safety
acknowledgements.

## Design

The last Boot Camp Assistant was a small sequential wizard: a decorative
metallic sidebar, one titled pane at a time, a bordered task box, and Go Back /
Continue at the bottom. This keeps that structure. The controls are standard
macOS push buttons and checkboxes, the copy is plain 13px system text, and there
are no status badges, progress dots, or dashboard cards. The sidebar diamonds
are drawn in CSS rather than copied from Apple's artwork.

The app icon is generated from the same shape by `mac/make-icon.swift` and
packaged as `AppIcon.icns` by `build-app.sh`.

## Security

- Binds to `127.0.0.1` only.
- Serves files from `web/` only, with path-traversal checks.
- Executes only the three checkers in `server.py:CHECKS`; there is no shell
  interpolation and request bodies cannot name a command.
- No outbound requests, no external assets, no credentials, no writes to disk.
- The native shell only launches this checkout's `server.py`; it does not accept
  a command from the page.

The safety acknowledgements are browser state, not proof: this is a readiness
display, not an interlock.

## Files

| File | Purpose |
|---|---|
| `server.py` | stdlib HTTP server, status API, allow-listed diagnostics |
| `web/index.html` | the six-step walkthrough |
| `web/app.css` | Liquid Glass window, sidebar, panes and controls |
| `web/app.js` | step navigation, task state, safety gates, diagnostics |
| `mac/SiliconCampAssistant.swift` | AppKit window, WKWebView, server lifecycle |
| `mac/Info.plist` | bundle identity and minimum macOS version |
| `mac/make-icon.swift` | draws the 1024px app icon |
| `mac/AppIcon.png` | committed icon source |
| `build-app.sh` | compiles and packages `SiliconCamp Assistant.app` |

## Tests

```bash
python3 -m unittest tests.test_siliconcamp_assistant
```

Eight tests cover the status payload, the checker allow-list, skip handling,
dirty-submodule classification, the six-step order, local-only assets and the
native bundle description. The native shell itself is verified by launching it
with `--screenshot <path>`: the app starts its own server, renders the page,
writes one PNG and exits.

## What comes next

The walkthrough is structured so the missing pieces can arrive without a
rewrite:

1. a real Windows install path (blocked on the kernel handoff);
2. a startup-disk picker once a boot entry exists;
3. serial-log capture and annotation for the first M4 boot;
4. driver inventory and installation once drivers exist.

Until then the Install control stays disabled and every blocker stays on screen.
