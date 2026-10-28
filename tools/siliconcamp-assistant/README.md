# SiliconCamp Assistant

A local, read-only readiness panel for the SiliconCamp bring-up. It is the first
user-facing piece of the project, and it is deliberately **not an installer**.

SiliconCamp can build m1n1 and the NTASP UEFI firmware, and it can check the vGIC
contract offline. Windows has never booted: the loader-to-kernel handoff is
unsolved, there is no AGX graphics driver, and the internal SSD driver has not
run on hardware. A setup assistant that offered an install button would be lying
about all three. This panel shows the state, the pinned image hashes, the safety
gates and the Boot Camp-shaped gaps instead.

## Run

```bash
python3 tools/siliconcamp-assistant/server.py
```

Open <http://127.0.0.1:8770/>. There are no dependencies beyond Python 3.9 and
the repository checkouts.

## What it shows

- **This Mac** — model, chip, macOS version, checkout tag and commit. It knows
  that `Mac16,3` / J624 is the captured target and says so when it is not.
- **Safety gate** — four acknowledgements (tested backup, a second Mac for DFU,
  a data-capable cable, owner approval). They are stored in this browser's
  `localStorage` and never sent to the server. All four do not enable the
  install button.
- **Images** — `MAC16_3_EFI.fd`, `m1n1-windows.bin` and `m1n1-windows.macho`,
  with their pinned SHA-256 from `artifacts/SHA256SUMS`, actual hashes and the
  build command for each. Missing images are a build step, not a download.
- **Diagnostics** — only the repository's checkers, through a fixed allow-list:
  the vGIC contract, the self-test gate, and patch reproducibility.
- **Boot Camp gap** — install, startup volume, partitioning, drivers, switching
  back and a one-click assistant, each with its real status.

## Security

- Binds to `127.0.0.1` only.
- Serves files from `web/` only, with path-traversal checks.
- Executes only the three checkers in `server.py:CHECKS`; there is no shell
  interpolation and request bodies cannot name a command.
- No outbound requests, no external assets, no credentials, no writes to disk.

The safety acknowledgements are browser state, not proof: the panel is a
readiness display, not an interlock.

## Files

| File | Purpose |
|---|---|
| `server.py` | stdlib HTTP server, status API, allow-listed diagnostics |
| `web/index.html` | the panel |
| `web/app.css` | light/dark styling |
| `web/app.js` | rendering, safety state, diagnostics |

## What comes next

The UI is structured so the missing pieces can arrive without a rewrite:

1. a real Windows install path (blocked on the kernel handoff);
2. a startup-disk picker once a boot entry exists;
3. serial-log capture and annotation for the first M4 boot;
4. driver inventory and installation once drivers exist.

Until then the install control stays disabled and the blockers stay on screen.
