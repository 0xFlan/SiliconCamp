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

Stop a capture with Ctrl-C; the log's footer records the byte and line count so
a truncated log is obvious.
"""

from __future__ import annotations

import argparse
import errno
import glob
import json
import os
import select
import sys
import termios
import time
from datetime import datetime
from pathlib import Path

DEFAULT_BAUD = 115200

#: tty modes commonly used by USB-UART bridges on macOS.  Everything in
#: /dev/cu.* is listed; these are marked as likely console devices first.
SERIAL_HINTS = ("usbmodem", "usbserial", "slab", "wchusbserial", "serial", "debug")

#: Ports macOS creates for itself and that are never the M4 console.
IGNORED = {"cu.Bluetooth-Incoming-Port"}


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
) -> dict[str, object]:
    """Read the port until ``stop`` is set or ``seconds`` elapse.

    Lines are timestamped at the moment the newline is read, with a monotonic
    clock, so the log shows the gap between boot stages even if the host clock
    changes.  The file is opened in append mode and begins with a header; the
    footer is written on the normal path and when Ctrl-C ends the capture.
    """
    started_wall = datetime.now().astimezone()
    started = time.monotonic()
    deadline = None if seconds is None else started + float(seconds)
    log_path = Path(out).expanduser()
    if not log_path.parent.is_dir():
        raise SerialError(f"no such folder for the log: {log_path.parent}")

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
        with log_path.open("a", encoding="utf-8", newline="\n") as log:
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
        result = capture(
            args.device,
            args.out,
            baud=args.baud,
            seconds=args.seconds,
            echo=args.echo,
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
