#!/usr/bin/env python3
"""
Serial capture for the SiliconCamp hardware bring-up.

The first M4 boot is judged by one artefact: the console log.  m1n1 and the
UEFI firmware both write to the platform UART at 115200 8N1, and the Mac has no
built-in way to save that stream.  This tool opens the USB serial device, puts
it in raw mode, timestamps every line and writes a log that can be attached to a
probe record.

It deliberately does not configure the port beyond 8N1: flow control, DTR/RTS
and break handling belong to the adapter, and a wrong assumption there is
indistinguishable from a silent machine.

CLI::

    python3 serial.py ports
    python3 serial.py capture --device /dev/cu.usbmodem1101 --out m4-boot.log
    python3 serial.py capture --device /dev/cu.usbmodem1101 --seconds 60 --echo
    python3 serial.py verdict --in probes/m4-first-boot.log

Stop a capture with Ctrl-C; the log's footer records the byte and line count so
a truncated log is obvious.

The ``verdict`` command reads a capture and answers the question the first
bring-up session will actually ask: did m1n1 reach the M4 with the expected
topology, did the firmware run its vGIC self-test, and did either of them fail
or fault?  It parses the fixed, line-oriented markers the two programs print
(``HV vGIC:``, ``PSCI DEBUG:``, ``vgic: [PASS]``, ``vgic: RESULT:``) rather
than guessing from prose.  A log that contains none of them is reported as
``incomplete``, never as a pass.
"""

from __future__ import annotations

import argparse
import errno
import glob
import json
import os
import re
import select
import sys
import termios
import time
from datetime import datetime
from pathlib import Path

DEFAULT_BAUD = 115200

#: tty modes commonly used by USB-UART bridges on macOS.  Everything in
#: /dev/cu.* is listed; these are marked as likely console devices first.
#: `/dev/cu.debug-console` and `cu.wlan-debug` are built-in macOS consoles, not
#: USB-UART adapters; matching "debug" made a machine with no adapter at all
#: look ready for a hardware session.
SERIAL_HINTS = ("usbmodem", "usbserial", "slab", "wchusbserial", "serial")

#: Ports macOS creates for itself and that are never the M4 console.
IGNORED = {"cu.Bluetooth-Incoming-Port"}


#: Markers written by m1n1, its PSCI patch and the firmware self-test.  The
#: firmware's format is documented in AppleVgicSelfTestDxe.c ("the log format is
#: deliberately fixed and line-oriented so a captured serial log can be diffed
#: between boots and checked by a script").
STAMP = re.compile(r"^\[(\d\d:\d\d:\d\d\.\d{3})\] (.*)$")
TOPOLOGY = re.compile(
    r"HV vGIC: (\d+) CPUs, (\d+)-bit PA, dist=(0x[0-9a-fA-F]+) "
    r"redist=(0x[0-9a-fA-F]+) its=(0x[0-9a-fA-F]+|\(nil\))"
)
PSCI_CORES = re.compile(r"PSCI DEBUG: Number of cores for PSCI nodes is (\d+)")
PSCI_CLUSTERS = re.compile(r"PSCI DEBUG: Number of clusters for PSCI nodes is (\d+)")
SELFTEST_LINE = re.compile(r"vgic: \[(PASS|FAIL|GAP |SKIP)\] ([^:]+): (.*)")
SELFTEST_RESULT = re.compile(r"vgic: RESULT: (PASS-WITH-GAPS|PASS|FAIL)")
SELFTEST_TOTALS = re.compile(
    r"vgic: totals: pass=(\d+) fail=(\d+) gap=(\d+) skip=(\d+)"
)
FOOTER = re.compile(
    r"^# ended: .* after ([\d.]+)s, (\d+) bytes, (\d+) lines$"
)
HEADER = re.compile(r"^# device: (.*)$")
FAULT_MARKERS = (
    "Unhandled exception",
    "panic",
    "PANIC",
    "ESR_EL",
    "Exception class",
)


class SerialError(RuntimeError):
    """The port cannot be opened, configured or read."""


def list_ports() -> list[dict[str, object]]:
    """Every call-out port, likely USB-UART adapters first."""
    rows: list[dict[str, object]] = []
    for path in sorted(glob.glob("/dev/cu.*")):
        name = os.path.basename(path)
        if name in IGNORED:
            continue
        lower = name.lower()
        rows.append(
            {
                "device": path,
                "name": name,
                "likely_serial": any(hint in lower for hint in SERIAL_HINTS),
            }
        )
    rows.sort(key=lambda row: (not row["likely_serial"], str(row["device"])))
    return rows


def _baud_constant(baud: int) -> int:
    value = getattr(termios, f"B{int(baud)}", None)
    if value is None:
        raise SerialError(f"{baud} baud is not supported by this Python/OS")
    return int(value)


def _configure(fd: int, baud: int) -> None:
    """Raw 8N1, no echo, no flow control, no line discipline."""
    try:
        attrs = termios.tcgetattr(fd)
    except termios.error as exc:
        raise SerialError(f"cannot read the port settings: {exc}") from exc
    speed = _baud_constant(baud)
    attrs[0] = 0  # iflag: no CR/LF translation, no IXON
    attrs[1] = 0  # oflag: no post-processing
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0  # lflag: no echo, no canonical mode, no signals
    attrs[4] = speed
    attrs[5] = speed
    attrs[6][termios.VMIN] = 0
    attrs[6][termios.VTIME] = 1
    try:
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except termios.error as exc:
        raise SerialError(f"cannot configure {baud} 8N1: {exc}") from exc


def _open(device: str) -> int:
    try:
        return os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            raise SerialError(f"no such serial device: {device}") from exc
        if exc.errno in (errno.EACCES, errno.EPERM):
            raise SerialError(f"permission denied opening {device}") from exc
        raise SerialError(f"cannot open {device}: {exc}") from exc


def capture(
    device: str,
    out: str | Path,
    *,
    baud: int = DEFAULT_BAUD,
    seconds: float | None = None,
    stop=None,
    echo: bool = False,
    quiet: bool = False,
    append: bool = False,
) -> dict[str, object]:
    """Read the port until ``stop`` is set or ``seconds`` elapse.

    Lines are timestamped with the host wall clock at the moment the newline is
    read, so the log shows the gap between boot stages; a clock change shows up
    as a step in those timestamps rather than being hidden.  The footer's
    elapsed value uses a monotonic clock, so it remains the real duration even
    if the wall clock moves.  The file is opened in append mode and begins with
    a header; the footer is written on the normal path and when Ctrl-C ends the
    capture.
    """
    started_wall = datetime.now().astimezone()
    started = time.monotonic()
    deadline = None if seconds is None else started + float(seconds)
    log_path = Path(out).expanduser()
    if not log_path.parent.is_dir():
        raise SerialError(f"no such folder for the log: {log_path.parent}")
    if log_path.exists() and not append:
        #
        # Appending would put two boots in one "capture", and the verdict would
        # read them as a single run: a clean first boot could make a later
        # silent failure look like a pass.  A hardware record has to be one
        # file per run unless the caller says otherwise.
        #
        raise SerialError(
            f"{log_path} already exists; choose a new file or pass append=True"
        )

    fd = _open(device)
    try:
        _configure(fd, baud)
    except SerialError:
        os.close(fd)
        raise

    buffer = b""
    lines = 0
    total = 0
    interrupted = False
    try:
        # Line buffering keeps a crash from losing the last few lines; at
        # 115200 baud the flush cost is irrelevant and the evidence matters.
        with log_path.open("a" if append else "w", encoding="utf-8", newline="\n", buffering=1) as log:
            log.write("# SiliconCamp serial capture\n")
            log.write(f"# device: {device}\n# baud: {baud}\n")
            log.write(f"# started: {started_wall.isoformat(timespec='seconds')}\n")
            log.flush()
            if not quiet:
                print(f"capturing {device} at {baud} 8N1 into {log_path}", file=sys.stderr)

            try:
                while True:
                    if stop is not None and stop.is_set():
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    try:
                        ready, _, _ = select.select([fd], [], [], 0.2)
                    except (OSError, ValueError) as exc:
                        raise SerialError(f"cannot wait on {device}: {exc}") from exc
                    if not ready:
                        continue
                    try:
                        chunk = os.read(fd, 4096)
                    except OSError as exc:
                        if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                            continue
                        raise SerialError(f"cannot read {device}: {exc}") from exc
                    if not chunk:
                        continue
                    total += len(chunk)
                    buffer += chunk
                    while b"\n" in buffer:
                        raw, buffer = buffer.split(b"\n", 1)
                        text = raw.rstrip(b"\r").decode("utf-8", "replace")
                        stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                        log.write(f"[{stamp}] {text}\n")
                        lines += 1
                        if echo:
                            print(text, flush=True)
            except KeyboardInterrupt:
                # The footer still has to be written: a capture ended by hand is
                # a normal way to finish a boot log.
                interrupted = True

            # A boot that dies mid-line still has to leave that line in the log.
            if buffer.strip():
                text = buffer.rstrip(b"\r").decode("utf-8", "replace")
                stamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                log.write(f"[{stamp}] {text} (unterminated)\n")
                lines += 1
                buffer = b""
            elapsed = time.monotonic() - started
            log.write(
                f"# ended: {datetime.now().astimezone().isoformat(timespec='seconds')} "
                f"after {elapsed:.1f}s, {total} bytes, {lines} lines\n"
            )
            log.flush()
            if not quiet:
                print(f"captured {total} bytes / {lines} lines in {elapsed:.1f}s", file=sys.stderr)
            return {
                "ok": True,
                "device": device,
                "baud": baud,
                "log": str(log_path),
                "bytes": total,
                "lines": lines,
                "seconds": round(elapsed, 1),
                "interrupted": interrupted,
            }
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def read_capture(path: str | Path) -> dict[str, object]:
    """Read a capture into timestamped rows plus what its header/footer recorded."""
    log_path = Path(path).expanduser()
    if not log_path.is_file():
        raise SerialError(f"no such capture: {log_path}")
    rows: list[tuple[str, str]] = []
    device = ""
    duration: float | None = None
    byte_count = 0
    line_count = 0
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SerialError(f"cannot read {log_path}: {exc}") from exc
    for raw in text.splitlines():
        stamp = STAMP.match(raw)
        if stamp:
            rows.append((stamp.group(1), stamp.group(2)))
            continue
        if raw.startswith("#"):
            header = HEADER.match(raw)
            if header:
                device = header.group(1).strip()
            footer = FOOTER.match(raw)
            if footer:
                duration = float(footer.group(1))
                byte_count = int(footer.group(2))
                line_count = int(footer.group(3))
            continue
        # A line without a timestamp (for example a log pasted from another
        # tool) is still evidence, so it is kept rather than dropped.
        if raw.strip():
            rows.append(("", raw))
    return {
        "log": str(log_path),
        "device": device,
        "duration": duration,
        "bytes": byte_count,
        "lines": line_count or len(rows),
        "rows": rows,
    }


def judge_capture(
    path: str | Path,
    *,
    expect_cpus: int = 10,
    expect_clusters: int = 2,
    min_pa_bits: int = 42,
) -> dict[str, object]:
    """Judge one boot log against the bring-up exit criteria.

    A capture is only a pass when it contains positive evidence for each stage:
    m1n1's vGIC topology, PSCI's agreement with it, and the firmware self-test's
    own RESULT.  Silence is ``incomplete``; a fault or a self-test failure is
    ``fail`` regardless of anything else in the log.
    """
    capture = read_capture(path)
    topology = None
    cores: int | None = None
    clusters: int | None = None
    results = {"pass": 0, "fail": 0, "gap": 0, "skip": 0}
    failed_checks: list[dict[str, str]] = []
    gaps: list[dict[str, str]] = []
    self_result: str | None = None
    totals: dict[str, int] | None = None
    firmware_reached = False
    faults: list[str] = []
    no_cpu_nodes = False

    for _stamp, text in capture["rows"]:
        if text.startswith("vgic:"):
            firmware_reached = True
        found = TOPOLOGY.search(text)
        if found and topology is None:
            topology = {
                "cpus": int(found.group(1)),
                "pa_bits": int(found.group(2)),
                "dist": found.group(3),
                "redist": found.group(4),
                "its": found.group(5),
            }
        if cores is None:
            found = PSCI_CORES.search(text)
            if found:
                cores = int(found.group(1))
        if clusters is None:
            found = PSCI_CLUSTERS.search(text)
            if found:
                clusters = int(found.group(1))
        if "no CPU nodes found" in text:
            no_cpu_nodes = True
        check = SELFTEST_LINE.search(text)
        if check:
            kind = check.group(1).strip()
            entry = {"name": check.group(2).strip(), "detail": check.group(3).strip()}
            if kind == "FAIL":
                results["fail"] += 1
                failed_checks.append(entry)
            elif kind == "GAP":
                results["gap"] += 1
                gaps.append(entry)
            elif kind == "SKIP":
                results["skip"] += 1
            else:
                results["pass"] += 1
        if self_result is None:
            found = SELFTEST_RESULT.search(text)
            if found:
                self_result = found.group(1)
        if totals is None:
            found = SELFTEST_TOTALS.search(text)
            if found:
                totals = {
                    "pass": int(found.group(1)),
                    "fail": int(found.group(2)),
                    "gap": int(found.group(3)),
                    "skip": int(found.group(4)),
                }
        if len(faults) < 12 and any(marker in text for marker in FAULT_MARKERS):
            if text.strip() not in faults:
                faults.append(text.strip())

    if totals:
        # The summary line is authoritative; the per-check counters are a
        # cross-check, not a second opinion.
        results = dict(totals)
    if self_result == "FAIL":
        results["fail"] = max(results["fail"], 1)

    m1n1_reached = bool(topology or cores is not None or no_cpu_nodes)
    failures: list[str] = []
    missing: list[str] = []

    if topology:
        if topology["cpus"] != expect_cpus:
            failures.append(
                f"m1n1 reports {topology['cpus']} CPUs, expected {expect_cpus}"
            )
        if topology["pa_bits"] < min_pa_bits:
            failures.append(
                f"m1n1 reports a {topology['pa_bits']}-bit PA, expected at least "
                f"{min_pa_bits}"
            )
    elif m1n1_reached:
        missing.append("m1n1 started but did not print its vGIC topology line")

    if cores is not None and cores != expect_cpus:
        failures.append(f"PSCI built {cores} cores, expected {expect_cpus}")
    if clusters is not None and clusters != expect_clusters:
        failures.append(f"PSCI built {clusters} clusters, expected {expect_clusters}")
    if no_cpu_nodes:
        failures.append("m1n1 found no CPU nodes in the ADT")
    if m1n1_reached and cores is None:
        missing.append("PSCI did not print its core count")

    if not firmware_reached:
        missing.append("the firmware's vGIC self-test did not run")
    elif self_result is None:
        missing.append("the self-test printed checks but no RESULT line")
    if results["fail"] or failed_checks:
        names = ", ".join(check["name"] for check in failed_checks[:5])
        failures.append(
            "the vGIC self-test failed" + (f": {names}" if names else "")
        )
    elif self_result == "FAIL":
        failures.append("the vGIC self-test reported FAIL")
    if faults:
        failures.append(f"the console contains a fault: {faults[0]}")

    if failures:
        verdict = "fail"
    elif not capture["rows"]:
        verdict = "incomplete"
        missing.append("the capture contains no console output")
    elif not m1n1_reached:
        verdict = "incomplete"
        missing.append("no m1n1 console markers were found")
    elif missing:
        verdict = "incomplete"
    elif self_result == "PASS-WITH-GAPS":
        verdict = "pass-with-gaps"
    else:
        verdict = "pass"

    return {
        "ok": verdict in ("pass", "pass-with-gaps"),
        "verdict": verdict,
        "capture": {k: capture[k] for k in ("log", "device", "duration", "bytes", "lines")},
        "m1n1": topology,
        "psci": {"cores": cores, "clusters": clusters},
        "selftest": {
            "ran": firmware_reached,
            "result": self_result,
            "pass": results["pass"],
            "fail": results["fail"],
            "gap": results["gap"],
            "skip": results["skip"],
            "failed_checks": failed_checks,
            "gaps": gaps,
        },
        "faults": faults,
        "failures": failures,
        "missing": missing,
    }


def format_verdict(report: dict[str, object]) -> str:
    """A short human-readable summary of `judge_capture`."""
    lines = [f"{report['capture']['log']}: {str(report['verdict']).upper()}"]
    topology = report["m1n1"]
    if topology:
        lines.append(
            f"  m1n1: {topology['cpus']} CPUs, {topology['pa_bits']}-bit PA, "
            f"dist={topology['dist']} redist={topology['redist']} its={topology['its']}"
        )
    psci = report["psci"]
    if psci["cores"] is not None:
        lines.append(
            f"  PSCI: {psci['cores']} cores in {psci['clusters']} clusters"
            if psci["clusters"] is not None
            else f"  PSCI: {psci['cores']} cores"
        )
    selftest = report["selftest"]
    if selftest["ran"]:
        lines.append(
            f"  self-test: {selftest['result'] or 'no result'} "
            f"({selftest['pass']} pass, {selftest['fail']} fail, "
            f"{selftest['gap']} gap, {selftest['skip']} skip)"
        )
    for failure in report["failures"]:
        lines.append(f"  FAIL: {failure}")
    for item in report["missing"]:
        lines.append(f"  missing: {item}")
    for check in report["selftest"]["gaps"]:
        lines.append(f"  gap: {check['name']}: {check['detail']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SiliconCamp serial capture")
    sub = parser.add_subparsers(dest="command", required=True)

    listing = sub.add_parser("ports", help="list call-out serial devices")
    listing.add_argument("--json", action="store_true")

    run = sub.add_parser("capture", help="read a serial port into a timestamped log")
    run.add_argument("--device", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    run.add_argument("--seconds", type=float, default=None)
    run.add_argument("--echo", action="store_true")
    run.add_argument(
        "--append",
        action="store_true",
        help="add another run to an existing capture (the verdict then reads both)",
    )

    check = sub.add_parser("verdict", help="judge a captured boot log")
    check.add_argument("--in", dest="input", required=True)
    check.add_argument("--json", action="store_true")
    check.add_argument("--expect-cpus", type=int, default=10)
    check.add_argument("--expect-clusters", type=int, default=2)
    check.add_argument("--min-pa-bits", type=int, default=42)

    args = parser.parse_args(argv)

    try:
        if args.command == "ports":
            rows = list_ports()
            if args.json:
                print(json.dumps(rows, indent=2))
            elif not rows:
                print("no /dev/cu.* devices; connect the USB serial adapter first")
            else:
                for row in rows:
                    mark = "*" if row["likely_serial"] else " "
                    print(f"{mark} {row['device']}")
            return 0
        if args.command == "verdict":
            report = judge_capture(
                args.input,
                expect_cpus=args.expect_cpus,
                expect_clusters=args.expect_clusters,
                min_pa_bits=args.min_pa_bits,
            )
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                print(format_verdict(report))
            return 0 if report["ok"] else 1 if report["verdict"] == "fail" else 2
        result = capture(
            args.device,
            args.out,
            baud=args.baud,
            seconds=args.seconds,
            echo=args.echo,
            append=args.append,
        )
        print(json.dumps(result, indent=2))
        return 130 if result.get("interrupted") else 0
    except KeyboardInterrupt:
        return 130
    except SerialError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
