#!/usr/bin/env python3
"""
SiliconCamp Assistant — a local Boot Camp-shaped setup assistant.

It serves the walkthrough, the repository's read-only checks, the installer
media engine, the Windows space plan and both space operations.  Windows still
cannot boot on this hardware; the assistant says so in the same pane as the
controls it cannot offer yet.

What can write:

  * installer media, and only to a disk image the user names or to an external
    USB disk that is re-validated immediately before it is erased;
  * the BOOTCAMP partition.  Allocating resizes the internal APFS container and
    is refused until Windows has a boot path; removing a partition returns its
    space and is always available.  Both need the write token and the disk
    identifier typed into the page.

Safety properties:

  * binds only to 127.0.0.1;
  * serves files from ``web/`` only (no path traversal);
  * diagnostics run a fixed allow-list of repository checkers;
  * write endpoints require a per-run token embedded in the page, so another
    web page cannot post to the loopback server;
  * one destructive job at a time, and the media engine re-checks the target
    type before it erases anything.

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
import secrets
import subprocess
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

TOOL_DIR = Path(__file__).resolve().parent
ROOT = TOOL_DIR.parents[1]
WEB_DIR = TOOL_DIR / "web"

if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))

import allocation  # noqa: E402 - local module, path set above
import media  # noqa: E402 - local module, path set above

#: Embedded in index.html and required by every write endpoint.  A page on
#: another origin cannot read it, so it cannot drive the local server.
TOKEN = secrets.token_urlsafe(24)
TOKEN_PLACEHOLDER = "__SILICONCAMP_TOKEN__"

#: One version string for the page payload and the HTTP `Server` header; they
#: drifted once, and the window identified itself as 0.3 while serving 0.4.
VERSION = "0.4"

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
        "feature": "Create a bootable Windows installer",
        "state": "available",
        "detail": "Validates a Windows Arm64 ISO, checks the boot application's PE machine type, writes FAT32 UEFI media to a USB disk or a disk image, and reads every file back.",
    },
    {
        "feature": "Windows Setup answer file",
        "state": "available",
        "detail": "Autounattend.xml on the media accepts the license, sets the locale and bypasses the TPM, Secure Boot and RAM checks. It never selects or formats a disk.",
    },
    {
        "feature": "Reserve space for Windows",
        "state": "engine ready",
        "detail": "The resize and restore commands are implemented and rehearse end to end on a throwaway APFS image. Allocation stays disabled until Windows can boot; removing an existing partition returns its space now.",
    },
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
        "detail": "This walkthrough is the first step: it makes the state and the blockers visible before any install path is offered.",
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


def _run(argv: list[str], timeout: float = 3.0, strip: bool = True) -> str:
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
    return result.stdout.strip() if strip else result.stdout


def _sysctl(name: str) -> str:
    return _run(["sysctl", "-n", name], timeout=2.0)


def _git(*args: str, strip: bool = True) -> str:
    return _run(["git", "--no-pager", *args], timeout=4.0, strip=strip)


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
    #
    # Do not strip the whole output: the first porcelain line starts with a
    # space when the change is in the worktree, and stripping it shifts the
    # path by one character.  That made m1n1-windows look like a source edit.
    #
    porcelain = _git("status", "--porcelain", strip=False)
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
        "version": VERSION,
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
# Background jobs: media creation can take minutes and must not block the UI.
# ---------------------------------------------------------------------------


class Job:
    """One long-running operation with a bounded transcript."""

    def __init__(self, job_id: str, kind: str) -> None:
        self.id = job_id
        self.kind = kind
        self.state = "running"
        self.result: object = None
        self.error = ""
        self.started = time.time()
        self.finished: float | None = None
        self.notes: list[str] = []
        self._lock = threading.Lock()

    def log(self, message: str) -> None:
        text = str(message).rstrip()
        if not text:
            return
        with self._lock:
            self.notes.append(text)
            if len(self.notes) > 500:
                del self.notes[: len(self.notes) - 500]

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            notes = list(self.notes)
        return {
            "id": self.id,
            "kind": self.kind,
            "state": self.state,
            "notes": notes,
            "result": self.result,
            "error": self.error,
            "seconds": round((self.finished or time.time()) - self.started, 1),
        }


class Jobs:
    """At most one writer at a time, and a short history for the UI."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._active: str = ""
        self._lock = threading.Lock()

    def start(self, kind: str, work) -> str:
        with self._lock:
            if self._active:
                raise RuntimeError("another write is already running")
            job_id = uuid.uuid4().hex[:12]
            job = Job(job_id, kind)
            self._jobs[job_id] = job
            self._order.append(job_id)
            self._active = job_id
            while len(self._order) > 20:
                self._jobs.pop(self._order.pop(0), None)
        thread = threading.Thread(target=self._run, args=(job, work), daemon=True)
        thread.start()
        return job_id

    def _run(self, job: Job, work) -> None:
        try:
            job.result = work(job.log)
            job.state = "done"
        except (media.MediaError, allocation.AllocationError, OSError) as exc:
            job.error = str(exc)
            job.state = "failed"
            job.log(f"error: {exc}")
        except Exception as exc:  # noqa: BLE001 - the transcript is the report
            job.error = f"unexpected failure: {exc}"
            job.state = "failed"
            job.log(traceback.format_exc())
        finally:
            job.finished = time.time()
            with self._lock:
                if self._active == job.id:
                    self._active = ""

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)


JOBS = Jobs()


# ---------------------------------------------------------------------------
# HTTP.
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = f"SiliconCampAssistant/{VERSION}"
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

    def _host_ok(self) -> bool:
        """Reject a request addressed to a name that is not this loopback server.

        Binding to 127.0.0.1 stops a remote connection, not a browser that has
        been pointed at an attacker-controlled DNS name resolving to 127.0.0.1.
        In that case the request arrives with ``Host: evil.example`` and the page
        is same-origin from the browser's point of view, so it can read the page
        (and the write token) without any CORS error.  The port is part of the
        check because loopback names on another port are a different server.
        """
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.server.server_address[1]
        if host in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return True
        self._error(HTTPStatus.MISDIRECTED_REQUEST, "unexpected Host header")
        return False

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._host_ok():
            return
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
        if path == "/api/installer":
            self._installer()
            return
        if path == "/api/allocation":
            self._allocation()
            return
        if path.startswith("/api/jobs/"):
            job = JOBS.get(path.rsplit("/", 1)[-1])
            if job is None:
                self._error(HTTPStatus.NOT_FOUND, "unknown job")
                return
            self._json(job.snapshot())
            return
        self._static(path)

    def _installer(self) -> None:
        try:
            data = media.list_targets()
        except media.MediaError as exc:
            self._json({"targets": [], "wimlib": "", "boot_disk": "", "error": str(exc)})
            return
        self._json(data)

    def _allocation(self) -> None:
        try:
            info = allocation.container_info()
            stats = allocation._stats_from(info)
            existing = allocation.existing_partition(info)
        except allocation.AllocationError as exc:
            self._json({"error": str(exc), "stats": None}, status=HTTPStatus.SERVICE_UNAVAILABLE)
            return
        #
        # Default to a size that leaves macOS headroom rather than the largest
        # allowed: Boot Camp did not start its slider at the maximum either.
        #
        ceiling = int(stats["max_gb"])
        preferred = max(allocation.MIN_WINDOWS_GB, int(ceiling * 0.75))
        default = min(128, max(allocation.MIN_WINDOWS_GB, min(preferred, ceiling)))
        self._json(
            {
                "stats": stats,
                "default_gb": default,
                "plan": allocation.plan_space(default, stats=stats, existing=existing),
            }
        )

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
        if target.name == "index.html":
            body = body.replace(
                TOKEN_PLACEHOLDER.encode("ascii"), TOKEN.encode("ascii")
            )
        self._headers(HTTPStatus.OK, content_type, len(body))
        self.wfile.write(body)

    def _body(self, limit: int = 8192) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "bad length")
            return None
        if length <= 0 or length > limit:
            self._error(HTTPStatus.BAD_REQUEST, "bad body")
            return None
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "expected application/json")
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, "bad json")
            return None
        if not isinstance(payload, dict):
            self._error(HTTPStatus.BAD_REQUEST, "expected an object")
            return None
        return payload

    def _authorised_write(self) -> bool:
        """Reject a write that did not come from this page."""
        supplied = self.headers.get("X-SiliconCamp-Token", "")
        if not secrets.compare_digest(supplied, TOKEN):
            self._error(HTTPStatus.FORBIDDEN, "missing or wrong write token")
            return False
        origin = self.headers.get("Origin")
        if origin is not None:
            port = self.server.server_address[1]
            if origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
                self._error(HTTPStatus.FORBIDDEN, "cross-origin write")
                return False
        return True

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if not self._host_ok():
            return
        path = urlparse(self.path).path
        if path == "/api/installer/inspect":
            self._inspect_iso()
            return
        if path == "/api/installer/plan":
            self._plan_iso()
            return
        if path == "/api/installer/create":
            self._create_media()
            return
        if path == "/api/allocation/plan":
            self._plan_allocation()
            return
        if path == "/api/allocation/rehearse":
            self._rehearse_allocation()
            return
        if path == "/api/allocation/apply":
            self._apply_allocation()
            return
        if path == "/api/allocation/remove":
            self._remove_allocation()
            return
        if path != "/api/check":
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        payload = self._body(limit=4096)
        if payload is None:
            return
        check_id = payload.get("id")
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

    # -- installer media ---------------------------------------------------

    def _inspect_iso(self) -> None:
        payload = self._body()
        if payload is None:
            return
        iso = str(payload.get("iso", "")).strip()
        if not iso:
            self._error(HTTPStatus.BAD_REQUEST, "no ISO path")
            return
        try:
            found = media.inspect_iso(iso)
        except media.MediaError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._json({"iso": str(Path(iso).expanduser()), "payload": found.summary()})

    def _target_from(self, raw: object) -> dict[str, object]:
        if not isinstance(raw, dict):
            raise media.MediaError("no destination")
        kind = str(raw.get("kind", ""))
        if kind == "disk":
            return {"kind": "disk", "device": str(raw.get("device", "")), "label": str(raw.get("label") or "WINDOWS")}
        if kind == "image":
            return {"kind": "image", "path": str(raw.get("path", "")), "label": str(raw.get("label") or "WINDOWS")}
        raise media.MediaError(f"unknown destination kind: {kind!r}")

    def _plan_iso(self) -> None:
        payload = self._body()
        if payload is None:
            return
        iso = str(payload.get("iso", "")).strip()
        try:
            plan = media.plan_iso(iso, target=self._target_from(payload.get("target")))
        except media.MediaError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._json(plan)

    def _create_media(self) -> None:
        if not self._authorised_write():
            return
        payload = self._body()
        if payload is None:
            return
        try:
            iso = str(payload.get("iso", "")).strip()
            target = self._target_from(payload.get("target"))
            confirm = bool(payload.get("confirm"))
            if target["kind"] == "disk" and not confirm:
                raise media.MediaError("erasing a USB disk needs an explicit confirmation")
            if target["kind"] == "image":
                # Fail the request, not a background job, for a path that can
                # never be written.
                media.normalise_image(str(target.get("path", "")))

            def work(log):
                return media.create_media(iso, target=target, confirm=confirm, log=log)

            job_id = JOBS.start("installer", work)
        except (media.MediaError, RuntimeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._json({"job": job_id}, status=HTTPStatus.ACCEPTED)

    # -- space allocation --------------------------------------------------

    def _plan_allocation(self) -> None:
        payload = self._body()
        if payload is None:
            return
        try:
            gib = int(payload.get("gib", 0))
        except (TypeError, ValueError):
            self._error(HTTPStatus.BAD_REQUEST, "bad size")
            return
        try:
            info = allocation.container_info()
            stats = allocation._stats_from(info)
            existing = allocation.existing_partition(info)
        except allocation.AllocationError as exc:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
            return
        self._json(allocation.plan_space(gib, stats=stats, existing=existing))

    def _rehearse_allocation(self) -> None:
        """Run the real resize and restore against a throwaway APFS image."""
        if not self._authorised_write():
            return
        payload = self._body()
        if payload is None:
            return
        try:
            gib = int(payload.get("gb", 0))
        except (TypeError, ValueError):
            self._error(HTTPStatus.BAD_REQUEST, "bad size")
            return
        if gib < allocation.MIN_WINDOWS_GB:
            self._error(
                HTTPStatus.BAD_REQUEST,
                f"Windows 11 Arm64 needs at least {allocation.MIN_WINDOWS_GB} GB",
            )
            return

        def work(log):
            return allocation.rehearse_space(gib, log=log)

        try:
            job_id = JOBS.start("rehearsal", work)
        except RuntimeError as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
            return
        self._json({"job": job_id}, status=HTTPStatus.ACCEPTED)

    def _apply_allocation(self) -> None:
        """Shrink the container and create BOOTCAMP after the user types its name.

        The plan is recomputed here rather than trusted from the page, so a
        container that changed since the page loaded cannot be resized against
        stale numbers.
        """
        if not self._authorised_write():
            return
        payload = self._body()
        if payload is None:
            return
        try:
            gib = int(payload.get("gb", 0))
        except (TypeError, ValueError):
            self._error(HTTPStatus.BAD_REQUEST, "bad size")
            return
        try:
            info = allocation.container_info()
            stats = allocation._stats_from(info)
            existing = allocation.existing_partition(info)
            plan = allocation.plan_space(gib, stats=stats, existing=existing)
        except allocation.AllocationError as exc:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
            return
        if not plan["apply"]["enabled"]:
            self._error(
                HTTPStatus.CONFLICT,
                str(plan["apply"]["reason"] or "the resize is not available"),
            )
            return
        expected = str(info["reference"])
        typed = allocation._normalise_reference(str(payload.get("confirm", "")))
        if typed != expected:
            self._error(
                HTTPStatus.BAD_REQUEST,
                f"type {expected} to confirm this resize",
            )
            return

        def work(log):
            return allocation.apply_space(
                gib, container=info["reference"], confirm=True, log=log
            )

        try:
            job_id = JOBS.start("allocation", work)
        except RuntimeError as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
            return
        self._json({"job": job_id}, status=HTTPStatus.ACCEPTED)

    def _remove_allocation(self) -> None:
        """Delete BOOTCAMP and return its space to macOS.

        Allowed before Windows boots: it only gives capacity back.  The page has
        to name the partition device, so a click alone cannot erase a Windows
        installation.
        """
        if not self._authorised_write():
            return
        payload = self._body()
        if payload is None:
            return
        try:
            info = allocation.container_info()
            existing = allocation.existing_partition(info)
        except allocation.AllocationError as exc:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
            return
        if not existing or not existing.get("device"):
            self._error(
                HTTPStatus.CONFLICT,
                f"no {allocation.PARTITION_NAME} partition on {info['whole_disk']}",
            )
            return
        expected = str(existing["device"]).removeprefix("/dev/")
        typed = allocation._normalise_reference(str(payload.get("confirm", "")))
        if typed != expected:
            self._error(
                HTTPStatus.BAD_REQUEST,
                f"type {expected} to confirm the removal",
            )
            return

        def work(log):
            return allocation.remove_space(
                container=info["reference"], confirm=True, log=log
            )

        try:
            job_id = JOBS.start("removal", work)
        except RuntimeError as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
            return
        self._json({"job": job_id}, status=HTTPStatus.ACCEPTED)


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
