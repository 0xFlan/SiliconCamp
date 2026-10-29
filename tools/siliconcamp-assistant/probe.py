#!/usr/bin/env python3
"""
First-boot probe records for SiliconCamp.

A hardware session produces one thing that cannot be recreated later: the
console log.  Everything else - the machine it came from, the images that were
booted, the verdict - has to be captured at the same time or the log is an
anecdote.  This tool writes that bundle and can verify it afterwards:

    probes/20261028T164100Z-first-boot/
        capture.log       the serial log, byte-for-byte
        verdict.json      serial.py judge_capture() output
        verdict.txt       the same verdict, for reading
        machine.json      model, chip, OS, build, kernel, boot args
        artifacts.json    built images and whether their hashes are pinned
        repo.json         commit, branch and whether the tree was clean
        README.md         the summary a person reads first
        MANIFEST.json     SHA-256 of every file above

`verify` re-hashes the bundle and re-judges the capture, so a truncated or
edited record is reported instead of being mistaken for evidence.

The preflight is deliberately strict: a missing serial adapter or a firmware
image that does not match its pinned hash is a failure, not a warning, because
the hardware session cannot start without them.

CLI::

    python3 probe.py preflight
    python3 probe.py record --in probes/m4-first-boot.log
    python3 probe.py record --in capture.log --label "second boot"
    python3 probe.py verify --dir probes/20261028T164100Z-first-boot
    python3 probe.py list
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PROBES = ROOT / "probes"

FORMAT = "siliconcamp.probe/1"

#: The images a hardware session boots, and where a local build puts them.
ARTIFACTS = (
    ("m1n1-windows.bin", "third_party/m1n1-windows/build/m1n1.bin"),
    ("m1n1-windows.macho", "third_party/m1n1-windows/build/m1n1.macho"),
    (
        "MAC16_3_EFI.fd",
        "third_party/ntasp-firmware/Build/Mac16_3-AARCH64/DEBUG_CLANGPDB/FV/MAC16_3_EFI.fd",
    ),
)

REQUIRED_FILES = (
    "capture.log",
    "verdict.json",
    "verdict.txt",
    "machine.json",
    "artifacts.json",
    "repo.json",
    "README.md",
    "MANIFEST.json",
)


class ProbeError(RuntimeError):
    """The record cannot be written or does not verify."""


# ---------------------------------------------------------------------------
# Command plumbing.
# ---------------------------------------------------------------------------


def _run(argv: list[str], *, timeout: float = 10.0) -> str:
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(*args: str) -> str:
    return _run(["git", "--no-pager", *args], timeout=5.0)


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def _safe_label(label: str) -> str:
    text = re.sub(r"[^a-z0-9._-]+", "-", label.strip().lower())
    # A label of ".." must not become a directory name; strip the characters
    # that make one, then fall back rather than producing an empty slug.
    text = text.strip(".-")[:40].strip(".-")
    return text or "first-boot"


# ---------------------------------------------------------------------------
# The judge is imported from serial.py under a private name, so a system
# pyserial installation cannot shadow it.
# ---------------------------------------------------------------------------


def load_serial():
    name = "siliconcamp_serial_probe"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE / "serial.py")
    if spec is None or spec.loader is None:
        raise ProbeError("cannot load serial.py next to probe.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Facts captured beside the log.
# ---------------------------------------------------------------------------


def machine_facts() -> dict[str, object]:
    """What machine produced this log.

    No serial number, hostname or user name: the bundle is meant to be
    attachable to an issue without redaction.
    """
    return {
        "model": _run(["sysctl", "-n", "hw.model"]),
        "chip": _run(["sysctl", "-n", "machdep.cpu.brand_string"]),
        "macos": platform.mac_ver()[0],
        "macos_build": _run(["sw_vers", "-buildVersion"]),
        "arch": platform.machine(),
        "kernel": platform.release(),
        "boot_args": _run(["sysctl", "-n", "kern.bootargs"]),
        "python": platform.python_version(),
    }


def artifact_facts() -> dict[str, object]:
    """The images this checkout would boot, against artifacts/SHA256SUMS."""
    expected: dict[str, str] = {}
    sums = ROOT / "artifacts" / "SHA256SUMS"
    if sums.exists():
        for line in sums.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) == 2 and len(parts[0]) == 64:
                expected[parts[1]] = parts[0].lower()

    rows = []
    for name, rel in ARTIFACTS:
        path = ROOT / rel
        row = {
            "name": name,
            "path": rel,
            "exists": path.is_file(),
            "bytes": path.stat().st_size if path.is_file() else 0,
            "expected": expected.get(name, ""),
            "actual": "",
            "status": "missing",
        }
        if path.is_file():
            try:
                row["actual"] = _sha256(path)
            except OSError:
                row["actual"] = ""
            if not row["expected"]:
                row["status"] = "unpinned"
            elif row["actual"] == row["expected"]:
                row["status"] = "match"
            else:
                row["status"] = "mismatch"
        rows.append(row)
    return {
        "pinned_from": "artifacts/SHA256SUMS",
        "artifacts": rows,
        "ready": bool(rows) and all(row["status"] == "match" for row in rows),
    }


def repo_facts() -> dict[str, object]:
    porcelain = _git("status", "--porcelain")
    return {
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "commit": _git("rev-parse", "HEAD"),
        "describe": _git("describe", "--tags", "--always"),
        "dirty": bool(porcelain.strip()),
        "dirty_paths": [
            line[3:].strip()
            for line in porcelain.splitlines()
            if line.strip() and not line.startswith("??")
        ],
    }


# ---------------------------------------------------------------------------
# Preflight.
# ---------------------------------------------------------------------------


def preflight() -> dict[str, object]:
    """Everything machine-checkable about the first hardware session.

    Returns a list of checks with ``pass``/``fail``/``warn``.  The session is
    ready only when nothing failed; a warning is something a person has to
    judge (a second Mac cannot be detected from here).
    """
    checks: list[dict[str, str]] = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    facts = machine_facts()
    model = str(facts["model"])
    if model == "Mac16,3":
        add("machine", "pass", f"{model}: the captured J624 / T8132 target")
    elif model:
        add(
            "machine",
            "fail",
            f"{model} is not Mac16,3; the firmware tables describe a different machine",
        )
    else:
        add("machine", "fail", "cannot read hw.model")

    artifacts = artifact_facts()
    rows = artifacts["artifacts"]
    missing = [row["name"] for row in rows if row["status"] == "missing"]
    wrong = [row["name"] for row in rows if row["status"] in ("mismatch", "unpinned")]
    if missing:
        add("artifacts", "fail", "missing: " + ", ".join(missing) + " (build them first)")
    elif wrong:
        add("artifacts", "fail", "not pinned or wrong: " + ", ".join(wrong))
    else:
        add("artifacts", "pass", "every boot image matches artifacts/SHA256SUMS")

    try:
        ports = load_serial().list_ports()
    except Exception as exc:  # noqa: BLE001 - report, do not crash the preflight
        ports = []
        add("serial", "fail", f"cannot list serial ports: {exc}")
    else:
        likely = [row for row in ports if row.get("likely_serial")]
        if likely:
            add("serial", "pass", "found: " + ", ".join(str(row["device"]) for row in likely))
        elif ports:
            add(
                "serial",
                "fail",
                "no USB-UART look-alike among "
                + ", ".join(str(row["device"]) for row in ports),
            )
        else:
            add("serial", "fail", "no /dev/cu.* devices; connect the USB TTL adapter")

    probes = PROBES
    try:
        probes.mkdir(parents=True, exist_ok=True)
        probe = probes / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        add("capture directory", "fail", f"cannot write {probes}: {exc}")
    else:
        add("capture directory", "pass", str(probes))

    space = shutil.disk_usage(ROOT)
    if space.free >= 512 * 1024 * 1024:
        add("disk space", "pass", f"{space.free // (1024 ** 3)} GiB free")
    else:
        add("disk space", "fail", f"{space.free // (1024 ** 2)} MiB free; a log needs very little")

    if shutil.which("python3"):
        add("python", "pass", platform.python_version())
    else:
        add("python", "fail", "python3 is not on PATH")

    # These cannot be detected from software; they stay visible so the list
    # does not look complete when it is not.
    add("second Mac", "warn", "DFU recovery host and data-capable cable must be on hand")
    add("boot security", "warn", "bputil / kmutil changes need explicit owner approval")

    failed = [check for check in checks if check["status"] == "fail"]
    return {
        "ready": not failed,
        "checks": checks,
        "failures": len(failed),
        "warnings": len([check for check in checks if check["status"] == "warn"]),
        "machine": facts,
        "artifacts": artifacts,
    }


def format_preflight(report: dict[str, object]) -> str:
    lines = ["Hardware session preflight"]
    for check in report["checks"]:
        lines.append(f"  {check['status'].upper():4} {check['name']}: {check['detail']}")
    lines.append(
        "ready" if report["ready"] else f"NOT READY ({report['failures']} failure(s))"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Records.
# ---------------------------------------------------------------------------


def _render_readme(summary: dict[str, object], report: dict[str, object]) -> str:
    selftest = report["selftest"]
    lines = [
        f"# Probe record: {summary['label']}",
        "",
        f"Created: {summary['created']}",
        f"Verdict: **{str(report['verdict']).upper()}**",
        "",
        "## Boot",
        "",
    ]
    topology = report["m1n1"]
    if topology:
        lines.append(
            f"- m1n1: {topology['cpus']} CPUs, {topology['pa_bits']}-bit PA, "
            f"dist `{topology['dist']}`, redist `{topology['redist']}`, ITS `{topology['its']}`"
        )
    psci = report["psci"]
    if psci["cores"] is not None:
        lines.append(
            f"- PSCI: {psci['cores']} cores in {psci['clusters']} clusters"
            if psci["clusters"] is not None
            else f"- PSCI: {psci['cores']} cores"
        )
    if selftest["ran"]:
        lines.append(
            f"- vGIC self-test: {selftest['result'] or 'no result'} "
            f"({selftest['pass']} pass, {selftest['fail']} fail, "
            f"{selftest['gap']} gap, {selftest['skip']} skip)"
        )
    else:
        lines.append("- vGIC self-test: not reached")
    lines.append("")

    if report["failures"]:
        lines += ["## Failures", ""] + [f"- {item}" for item in report["failures"]] + [""]
    if report["missing"]:
        lines += ["## Missing evidence", ""] + [f"- {item}" for item in report["missing"]] + [""]
    if selftest["failed_checks"]:
        lines += ["## Failed checks", ""]
        for check in selftest["failed_checks"]:
            lines.append(f"- `{check['name']}`: {check['detail']}")
        lines.append("")
    if selftest["gaps"]:
        lines += ["## Gaps", ""]
        for check in selftest["gaps"]:
            lines.append(f"- `{check['name']}`: {check['detail']}")
        lines.append("")

    machine = summary["machine"]
    lines += [
        "## Machine",
        "",
        f"- Model: {machine['model'] or 'unknown'}",
        f"- Chip: {machine['chip'] or 'unknown'}",
        f"- macOS: {machine['macos'] or 'unknown'} ({machine['macos_build'] or 'unknown build'})",
        f"- Kernel: {machine['kernel'] or 'unknown'}",
        f"- Commit: `{summary['repo']['commit'][:12]}` on {summary['repo']['branch'] or 'unknown'}"
        + (" (dirty)" if summary["repo"]["dirty"] else ""),
        "",
        "## Files",
        "",
        "| File | SHA-256 |",
        "|---|---|",
    ]
    for name, digest in summary["files"].items():
        lines.append(f"| `{name}` | `{digest}` |")
    lines += [
        "",
        "Verify this record with:",
        "",
        "```",
        f"python3 tools/siliconcamp-assistant/probe.py verify --dir {summary['directory']}",
        "```",
        "",
    ]
    return "\n".join(lines)


def record(
    capture: str | Path,
    *,
    out: str | Path | None = None,
    label: str = "first boot",
    log=print,
) -> dict[str, object]:
    """Write one immutable bundle for a captured boot log."""
    source = Path(capture).expanduser()
    if not source.is_file():
        raise ProbeError(f"no such capture: {source}")
    created = datetime.now(timezone.utc)
    directory = (
        Path(out).expanduser()
        if out
        else PROBES / f"{created:%Y%m%dT%H%M%SZ}-{_safe_label(label)}"
    )
    if directory.exists():
        raise ProbeError(f"refusing to overwrite an existing record: {directory}")

    judge = load_serial()
    report = judge.judge_capture(source)
    machine = machine_facts()
    artifacts = artifact_facts()
    repo = repo_facts()

    directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".probe-", dir=str(directory.parent)))
    try:
        shutil.copyfile(source, staging / "capture.log")
        _write_json(staging / "verdict.json", report)
        (staging / "verdict.txt").write_text(
            judge.format_verdict(report) + "\n", encoding="utf-8"
        )
        _write_json(staging / "machine.json", machine)
        _write_json(staging / "artifacts.json", artifacts)
        _write_json(staging / "repo.json", repo)

        files = {
            name: _sha256(staging / name)
            for name in REQUIRED_FILES
            if name != "MANIFEST.json" and name != "README.md"
        }
        # README is written before it is hashed, below.
        summary = {
            "format": FORMAT,
            "label": label,
            "created": created.isoformat(timespec="seconds"),
            "directory": str(directory),
            "verdict": report["verdict"],
            "machine": machine,
            "repo": repo,
            "artifacts_ready": artifacts["ready"],
            "files": files,
        }
        (staging / "README.md").write_text(
            _render_readme(summary, report), encoding="utf-8"
        )
        files["README.md"] = _sha256(staging / "README.md")
        summary["files"] = files
        _write_json(staging / "MANIFEST.json", summary)

        staging.rename(directory)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    log(f"record: {directory}")
    log(f"verdict: {report['verdict']}")
    log(f"files: {len(summary['files']) + 1} (+ MANIFEST.json)")
    return {
        "ok": True,
        "directory": str(directory),
        "verdict": report["verdict"],
        "manifest": summary,
    }


def verify(directory: str | Path, *, log=print) -> dict[str, object]:
    """Re-hash a record and re-judge its capture."""
    base = Path(directory).expanduser()
    manifest_path = base / "MANIFEST.json"
    if not manifest_path.is_file():
        raise ProbeError(f"no MANIFEST.json in {base}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProbeError(f"cannot read {manifest_path}: {exc}") from exc
    if manifest.get("format") != FORMAT:
        raise ProbeError(f"{base} is not a {FORMAT} record")

    problems: list[str] = []
    for name in REQUIRED_FILES:
        if not (base / name).is_file():
            problems.append(f"missing {name}")
    for name, digest in (manifest.get("files") or {}).items():
        path = base / name
        if not path.is_file():
            problems.append(f"missing {name}")
            continue
        if _sha256(path) != digest:
            problems.append(f"{name} does not match its recorded hash")

    verdict_value = ""
    verdict_path = base / "verdict.json"
    if verdict_path.is_file():
        try:
            stored = json.loads(verdict_path.read_text(encoding="utf-8"))
            verdict_value = str(stored.get("verdict", ""))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"cannot read verdict.json: {exc}")
    if not verdict_value:
        problems.append("verdict.json has no verdict")
    elif verdict_value != str(manifest.get("verdict", "")):
        problems.append("verdict.json disagrees with MANIFEST.json")

    # Re-judge the capture so a bundle whose log was replaced with a different
    # log of the same size is not accepted by hashes alone.  A changed log has
    # changed hashes too, so this mostly catches a consistently re-written
    # bundle; it also proves the stored verdict was derived from this log.
    capture = base / "capture.log"
    if capture.is_file():
        try:
            again = load_serial().judge_capture(capture)
        except Exception as exc:  # noqa: BLE001 - report as a failed record
            problems.append(f"cannot judge capture.log: {exc}")
        else:
            if verdict_value and again["verdict"] != verdict_value:
                problems.append(
                    f"capture.log now judges {again['verdict']}, not {verdict_value}"
                )

    log(f"verify: {base}: {'OK' if not problems else 'FAILED'}")
    for problem in problems:
        log(f"  {problem}")
    return {
        "ok": not problems,
        "directory": str(base),
        "verdict": verdict_value,
        "problems": problems,
    }


def list_records(root: str | Path | None = None) -> list[dict[str, object]]:
    base = Path(root).expanduser() if root else PROBES
    rows: list[dict[str, object]] = []
    if not base.is_dir():
        return rows
    for entry in sorted(base.iterdir()):
        manifest = entry / "MANIFEST.json"
        if not entry.is_dir() or not manifest.is_file():
            continue
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("format") != FORMAT:
            continue
        rows.append(
            {
                "directory": str(entry),
                "created": data.get("created", ""),
                "label": data.get("label", ""),
                "verdict": data.get("verdict", ""),
                "artifacts_ready": data.get("artifacts_ready", False),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# CLI.
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SiliconCamp first-boot probe records")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("preflight", help="check the hardware session is ready")
    check.add_argument("--json", action="store_true")

    make = sub.add_parser("record", help="bundle a captured boot log")
    make.add_argument("--in", dest="input", required=True)
    make.add_argument("--out", default="")
    make.add_argument("--label", default="first boot")
    make.add_argument("--json", action="store_true")

    proof = sub.add_parser("verify", help="re-hash and re-judge a record")
    proof.add_argument("--dir", required=True)
    proof.add_argument("--json", action="store_true")

    listing = sub.add_parser("list", help="list records")
    listing.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            report = preflight()
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                print(format_preflight(report))
            return 0 if report["ready"] else 2
        if args.command == "record":
            result = record(
                args.input,
                out=args.out or None,
                label=args.label,
                log=lambda _: None,
            )
            print(json.dumps(result, indent=2) if args.json else result["directory"])
            return 0
        if args.command == "verify":
            result = verify(args.dir, log=lambda _: None)
            print(json.dumps(result, indent=2) if args.json else json.dumps(result, indent=2))
            return 0 if result["ok"] else 1
        rows = list_records()
        if args.json:
            print(json.dumps(rows, indent=2))
        elif not rows:
            print("no probe records; run `probe.py record --in <capture>` first")
        else:
            for row in rows:
                print(f"{row['created']}  {str(row['verdict']).upper():16} {row['directory']}")
        return 0
    except ProbeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
