#!/usr/bin/env python3
"""
SiliconCamp Assistant — a local, read-only readiness panel.

The project can build m1n1 and the NTASP UEFI firmware, and it cannot yet boot
Windows.  This serves a small UI that says exactly that: which images are
present and match their pinned hashes, what the machine is, which safety gates
are acknowledged, and which checks still pass.  It is deliberately not an
installer.

Safety properties:

  * binds only to 127.0.0.1;
  * serves files from ``web/`` only (no path traversal);
  * the only command execution is a fixed allow-list of repository checkers;
  * no request body is written to disk and no credentials are read.

Run::

    python3 tools/siliconcamp-assistant/server.py

Then open http://127.0.0.1:8770/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

TOOL_DIR = Path(__file__).resolve().parent
ROOT = TOOL_DIR.parents[1]
WEB_DIR = TOOL_DIR / "web"

#: The names in artifacts/SHA256SUMS and where a local build puts each one.
ARTIFACTS = (
    (
        "MAC16_3_EFI.fd",
        "third_party/ntasp-firmware/Build/Mac16_3-AARCH64/DEBUG_CLANGPDB/FV/MAC16_3_EFI.fd",
        "NTASP UEFI firmware for Mac16,3",
        "./scripts/build-uefi-linux.sh Mac16_3",
    ),
    (
        "m1n1-windows.bin",
        "third_party/m1n1-windows/build/m1n1.bin",
        "m1n1-windows hypervisor, raw image",
        "./scripts/build-m1n1.sh",
    ),
    (
        "m1n1-windows.macho",
        "third_party/m1n1-windows/build/m1n1.macho",
        "m1n1-windows hypervisor, Mach-O",
        "./scripts/build-m1n1.sh",
    ),
)

#: The only commands this server will run.  Each entry is (label, argv, timeout).
CHECKS = {
    "vgic-contract": (
        "vGIC guest contract (patch series)",
        [sys.executable, "scripts/check-vgic.py"],
        90,
    ),
    "vgic-selftest": (
        "vGIC conformance self-test gate",
        [sys.executable, "scripts/check-vgic-selftest.py"],
        90,
    ),
    "patch-series": (
        "Patch series reproducibility",
        ["/bin/bash", "scripts/verify-patches.sh"],
        180,
    ),
}

BLOCKERS = (
    {
        "name": "Windows loader-to-kernel handoff",
        "status": "unsolved",
        "detail": "The existing project hangs at the handoff even on M1/M2. No Windows kernel has run on this M4.",
    },
    {
        "name": "Graphics driver",
        "status": "none",
        "detail": "There is no WDDM driver for Apple AGX, so even a booting kernel has no usable desktop.",
    },
    {
        "name": "Internal SSD",
        "status": "untested",
        "detail": "AppleNANDStorageDxe is in the firmware image but has never run on hardware; USB storage is the install path.",
    },
    {
        "name": "PCIe and MSI on hardware",
        "status": "unproven",
        "detail": "The root complex, DART bypass and MSI frame are written and offline-checked; no device has raised one on this Mac.",
    },
    {
        "name": "Platform trust",
        "status": "none",
        "detail": "No Secure Boot, TPM 2.0, VBS/HVCI, IOMMU certification or anti-cheat path.",
    },
)

ROADMAP = (
    {
        "feature": "Install Windows from an ISO",
        "state": "blocked",
        "detail": "No bootable Windows path exists yet; there is nothing an installer could write.",
    },
    {
        "feature": "Choose a startup disk at boot",
        "state": "blocked",
        "detail": "iBoot only chainloads; there is no installed boot entry to select.",
    },
    {
        "feature": "Partition or resize the internal disk",
        "state": "blocked",
        "detail": "The ANS2 driver is untested and no user-space disk tooling exists.",
    },
    {
        "feature": "Drivers after install",
        "state": "missing",
        "detail": "Keyboard, trackpad, audio, Wi-Fi, Bluetooth and GPU drivers do not exist for Windows.",
    },
    {
        "feature": "Switch back to macOS",
        "state": "not tested",
        "detail": "A reboot should return to macOS because m1n1 is chainloaded, but this has not been exercised on the M4.",
    },
    {
        "feature": "One setup assistant",
        "state": "preview",
        "detail": "This panel is the first step: it makes the state and the blockers visible before any install path is offered.",
    },
)

BOOT_CHAIN = (
    {"name": "Apple iBoot", "detail": "Apple-signed and unavoidable; it loads m1n1."},
    {"name": "m1n1-windows", "detail": "EL2: hardware init, vGIC, PSCI, PCIe/DART, chainload."},
    {"name": "NTASP UEFI", "detail": "EL1: ACPI, boot services, the vGIC self-test, Windows loader."},
    {"name": "Windows", "detail": "Not reached. bootmgr -> winload -> kernel is the unsolved handoff."},
)


# ---------------------------------------------------------------------------
# Read-only facts about this checkout and machine.
# ---------------------------------------------------------------------------


def _run(argv: list[str], timeout: float = 3.0) -> str:
    try:
        result = subprocess.run(
            argv,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()


def _sysctl(name: str) -> str:
    return _run(["sysctl", "-n", name], timeout=2.0)


def _git(*args: str) -> str:
    return _run(["git", "--no-pager", *args], timeout=4.0)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def expected_hashes() -> dict[str, str]:
    """Parse artifacts/SHA256SUMS into {filename: sha256}."""
    path = ROOT / "artifacts" / "SHA256SUMS"
    hashes: dict[str, str] = {}
    if not path.exists():
        return hashes
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return hashes
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and len(parts[0]) == 64:
            hashes[parts[1]] = parts[0].lower()
    return hashes


def artifact_status() -> list[dict[str, object]]:
    pinned = expected_hashes()
    rows: list[dict[str, object]] = []
    for name, rel, description, build in ARTIFACTS:
        path = ROOT / rel
        expected = pinned.get(name, "")
        actual = ""
        if path.exists():
            try:
                actual = sha256_file(path)
            except OSError:
                actual = ""
        if not path.exists():
            state = "missing"
        elif not expected:
            state = "unpinned"
        elif actual == expected:
            state = "match"
        else:
            state = "mismatch"
        rows.append(
            {
                "name": name,
                "path": rel,
                "description": description,
                "build": build,
                "expected": expected,
                "actual": actual,
                "size": path.stat().st_size if path.exists() else 0,
                "status": state,
            }
        )
    return rows


def machine_info() -> dict[str, object]:
    model = _sysctl("hw.model")
    chip = _sysctl("machdep.cpu.brand_string")
    os_version = platform.mac_ver()[0]
    supported = model == "Mac16,3"
    return {
        "model": model or "unknown",
        "chip": chip or "unknown",
        "os": os_version or "unknown",
        "arch": platform.machine(),
        "supported": supported,
        "target": "J624 / T8132 (M4)" if supported else "not a captured target",
    }


def _submodule_paths() -> set[str]:
    """Paths declared in .gitmodules."""
    path = ROOT / ".gitmodules"
    if not path.exists():
        return set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    paths: set[str] = set()
    for line in lines:
        key, _, value = line.partition("=")
        if key.strip() == "path":
            paths.add(value.strip())
    return paths


def repo_info() -> dict[str, object]:
    porcelain = _git("status", "--porcelain")
    known_submodules = _submodule_paths()
    dirty = [
        line for line in porcelain.splitlines()
        if line.strip() and not line.startswith("??")
    ]
    submodules: list[str] = []
    other_dirty: list[str] = []
    for line in dirty:
        rel = line[3:].strip()
        #
        # `git status --porcelain` reports a submodule with modified content as
        # ` M path`, the same worktree code a modified file gets.  Classify by
        # the path, or the panel would claim the patched submodules are source
        # edits.
        #
        if rel in known_submodules:
            submodules.append(rel)
        else:
            other_dirty.append(line)
    return {
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD") or "unknown",
        "commit": _git("rev-parse", "--short", "HEAD") or "unknown",
        "tag": _git("describe", "--tags", "--abbrev=0") or "",
        "submodules_modified": submodules,
        "source_modified": other_dirty,
    }


def classify_check(exit_code: int, output: str) -> tuple[bool, bool]:
    """Return (ok, skipped) for a checker result.

    A zero exit with a ``skip:`` line is not a pass.  ``verify-patches.sh``
    behaves that way when a submodule is not initialised, and a green card for
    an unverified tree would defeat the point of the panel.
    """
    skipped = exit_code == 0 and "skip:" in output.lower()
    return (exit_code == 0 and not skipped, skipped)


def build_status() -> dict[str, object]:
    artifacts = artifact_status()
    ready = bool(artifacts) and all(row["status"] == "match" for row in artifacts)
    return {
        "product": "SiliconCamp Assistant",
        "version": "0.1",
        "preview": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "machine": machine_info(),
        "repo": repo_info(),
        "artifacts": artifacts,
        "artifacts_ready": ready,
        "blockers": list(BLOCKERS),
        "roadmap": list(ROADMAP),
        "boot_chain": list(BOOT_CHAIN),
        "checks": [
            {"id": key, "label": value[0]} for key, value in CHECKS.items()
        ],
    }


# ---------------------------------------------------------------------------
# HTTP.
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "SiliconCampAssistant/0.1"
    sys_version = ""

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("assistant: " + (fmt % args) + "\n")

    def _headers(self, status: int, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        self.end_headers()

    def _json(self, payload: object, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status=status)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        if path == "/api/status":
            self._json(build_status())
            return
        if path == "/api/checks":
            self._json(
                {
                    "checks": [
                        {"id": key, "label": value[0]}
                        for key, value in CHECKS.items()
                    ]
                }
            )
            return
        self._static(path)

    def _static(self, path: str) -> None:
        if path in ("", "/"):
            path = "/index.html"
        relative = Path(path.lstrip("/"))
        try:
            target = (WEB_DIR / relative).resolve()
            target.relative_to(WEB_DIR.resolve())
        except (OSError, ValueError):
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        if not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        try:
            body = target.read_bytes()
        except OSError:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "cannot read file")
            return
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
            "application/javascript",
            "application/json",
        ):
            content_type += "; charset=utf-8"
        self._headers(HTTPStatus.OK, content_type, len(body))
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if urlparse(self.path).path != "/api/check":
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "bad length")
            return
        if length <= 0 or length > 4096:
            self._error(HTTPStatus.BAD_REQUEST, "bad body")
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "bad json")
            return
        check_id = payload.get("id") if isinstance(payload, dict) else None
        if check_id not in CHECKS:
            self._error(HTTPStatus.BAD_REQUEST, "unknown check")
            return

        label, argv, timeout = CHECKS[check_id]
        started = time.monotonic()
        try:
            result = subprocess.run(
                argv,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            output = (result.stdout or "") + (result.stderr or "")
            exit_code = result.returncode
        except subprocess.TimeoutExpired:
            output = f"{label} timed out after {timeout}s\n"
            exit_code = 124
        except OSError as exc:
            output = f"cannot run {label}: {exc}\n"
            exit_code = 127

        #
        # verify-patches.sh exits 0 when a submodule is not initialised and
        # prints "skip:".  Calling that a pass would be exactly the kind of
        # false green this panel exists to avoid.
        #
        ok, skipped = classify_check(exit_code, output)
        self._json(
            {
                "id": check_id,
                "label": label,
                "exit": exit_code,
                "ok": ok,
                "skipped": skipped,
                "seconds": round(time.monotonic() - started, 2),
                "output": output[-16000:],
            }
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="SiliconCamp Assistant (loopback only)")
    parser.add_argument(
        "--port",
        type=int,
        default=8770,
        help="TCP port on 127.0.0.1 (default: 8770)",
    )
    args = parser.parse_args()

    if not WEB_DIR.is_dir():
        print(f"error: missing UI directory {WEB_DIR}", file=sys.stderr)
        return 1

    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as exc:
        print(f"error: cannot listen on 127.0.0.1:{args.port}: {exc}", file=sys.stderr)
        return 1

    print(f"SiliconCamp Assistant: http://127.0.0.1:{args.port}/")
    print(f"repository: {ROOT}")
    print("loopback only; press Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
