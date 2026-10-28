#!/usr/bin/env python3
"""Check the GICv3 facts this project's vGIC is written against, using QEMU.

`hv_vgic.c` and `AppleVgicSelfTestDxe` encode claims about how a GICv3
distributor and redistributor are supposed to behave: that the clear-enable
alias reads back the enable state, that a write of 0 to a set-enable register
changes nothing, that a set-pending write is the architectural way to make an
interrupt pending, that GICD_CTLR.DS and ARE cannot be cleared, and that a
redistributor wake sequence terminates.  Those claims are the argument for the
patches in `patches/m1n1-windows/0007-vgic-enable-gating.patch`; a comment in a
C file is not evidence that they are right.

QEMU is an independent GICv3 implementation, and its qtest protocol lets a host
script read and write the machine's MMIO without booting a guest.  This script
drives QEMU's `virt` machine in that mode and asserts the same read-backs the
emulation depends on.  It is not a QEMU conformance suite, and a failure here is
not proof that the emulation is wrong: it means one of the two implementations
changed and the disagreement has to be resolved by reading ARM IHI 0069.

Usage:
    ./scripts/check-gicv3-oracle.py
    ./scripts/check-gicv3-oracle.py --qemu /opt/qemu/bin/qemu-system-aarch64
    ./scripts/check-gicv3-oracle.py --require   # fail instead of skipping

Exit status: 0 when every probe holds (or QEMU is absent without --require),
1 when a probe fails or QEMU cannot be driven.
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

#: `virt` memory map, hw/arm/virt.c.  Both are documented in docs/vgic-oracle.md.
GICD_BASE = 0x08000000
GICR_BASE = 0x080A0000
FRAME_SIZE = 0x20000
SGI_OFFSET = 0x10000

#: Redistributor RD_base registers.
GICR_CTLR = 0x0000
GICR_TYPER = 0x0008
GICR_WAKER = 0x0014

#: Redistributor SGI_base registers.
GICR_IGROUPR0 = 0x0080
GICR_ISENABLER0 = 0x0100
GICR_ICENABLER0 = 0x0180
GICR_ISPENDR0 = 0x0200
GICR_ICPENDR0 = 0x0280
GICR_ISACTIVER0 = 0x0300
GICR_ICACTIVER0 = 0x0380

#: Distributor registers.
GICD_CTLR = 0x0000
GICD_TYPER = 0x0004
GICD_IGROUPR = 0x0080
GICD_ISENABLER = 0x0100
GICD_ICENABLER = 0x0180

GICD_CTLR_ENABLEGRP1 = 1 << 0
GICD_CTLR_ENABLEGRP0 = 1 << 1
GICD_CTLR_ARE = 1 << 4
GICD_CTLR_DS = 1 << 6
GICD_CTLR_RWP = 1 << 31
GICR_TYPER_LAST = 1 << 4
GICR_WAKER_CHILDREN_ASLEEP = 1 << 2

OK_LINE = re.compile(r"^OK(?: 0x([0-9a-fA-F]+))?$")


class QTestError(RuntimeError):
    pass


def parse_ok(line: str) -> int | None:
    """Return the value on an `OK` response line, or raise for a `FAIL`."""
    match = OK_LINE.match(line.strip())
    if match:
        return int(match.group(1), 16) if match.group(1) is not None else None
    if line.startswith("FAIL"):
        raise QTestError(line.strip())
    raise QTestError(f"unexpected qtest response: {line.strip()!r}")


class QTest:
    """A minimal qtest client: one command in, one OK/FAIL line out."""

    def __init__(self, qemu: str, machine: str, cpu: str, smp: int, timeout: float = 15.0):
        self.timeout = timeout
        self.lines: list[str] = []
        self.proc = subprocess.Popen(
            [
                qemu,
                "-M", machine,
                "-cpu", cpu,
                "-smp", str(smp),
                "-accel", "qtest",
                "-display", "none",
                "-monitor", "none",
                "-qtest", "stdio",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert self.proc.stdout is not None and self.proc.stdin is not None
        #
        # A reader thread, not select(): TextIOWrapper buffers a whole chunk,
        # so select() on the pipe can report "no data" while complete lines are
        # already sitting in the Python buffer.  That deadlock is easy to write
        # and hard to see; the queue makes each line's arrival the only event.
        #
        self.queue: queue.Queue[str | None] = queue.Queue()
        self.reader = threading.Thread(target=self._pump, daemon=True)
        self.reader.start()
        self._read_until_prefix("OPENED")

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                self.queue.put(line.rstrip("\n"))
        finally:
            self.queue.put(None)

    def _read_line(self) -> str:
        try:
            line = self.queue.get(timeout=self.timeout)
        except queue.Empty as exc:
            raise QTestError("timed out waiting for qemu") from exc
        if line is None:
            raise QTestError("qemu closed its qtest stream")
        self.lines.append(line)
        return line

    def _read_until_prefix(self, prefix: str) -> str:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            line = self._read_line()
            #
            # qemu's readiness line is `[I 0.000000] OPENED`, so the marker is
            # inside the line rather than at its start.
            #
            if prefix in line:
                return line
        raise QTestError(f"qemu never printed {prefix!r}")

    def _command(self, text: str) -> int | None:
        self.proc.stdin.write(text + "\n")
        self.proc.stdin.flush()
        # The QTest server echoes the command and then answers.  Only the
        # answer line is a protocol response; a stray echo is ignored.
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            line = self._read_line()
            if line.startswith("OK") or line.startswith("FAIL"):
                return parse_ok(line)
        raise QTestError(f"no response to {text!r}")

    def read32(self, address: int) -> int:
        value = self._command(f"readl 0x{address:x}")
        assert value is not None
        return value

    def read64(self, address: int) -> int:
        value = self._command(f"readq 0x{address:x}")
        assert value is not None
        return value

    def write32(self, address: int, value: int) -> None:
        self._command(f"writel 0x{address:x} 0x{value:x}")

    def close(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


class Oracle:
    def __init__(self, qemu: str, machine: str = "virt,gic-version=3",
                 cpu: str = "cortex-a57", smp: int = 2):
        self.qemu = qemu
        self.machine = machine
        self.cpu = cpu
        self.smp = smp
        self.test: QTest | None = None
        self.passes = 0
        self.failures: list[str] = []

    def __enter__(self) -> "Oracle":
        self.test = QTest(self.qemu, self.machine, self.cpu, self.smp)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.test is not None:
            self.test.close()

    def check(self, ok: bool, name: str, detail: str) -> bool:
        if ok:
            self.passes += 1
            print(f"  ok   {name}: {detail}")
        else:
            self.failures.append(name)
            print(f"  FAIL {name}: {detail}")
        return ok

    def redist(self, cpu: int, offset: int) -> int:
        assert self.test is not None
        return GICR_BASE + (cpu * FRAME_SIZE) + SGI_OFFSET + offset

    def run(self) -> int:
        assert self.test is not None
        t = self.test

        print("\nDistributor identity")
        ctlr = t.read32(GICD_BASE + GICD_CTLR)
        self.check((ctlr & GICD_CTLR_DS) != 0, "GICD_CTLR.DS",
                   f"reset value 0x{ctlr:08x} has DS=1 (one security state)")
        self.check((ctlr & GICD_CTLR_ARE) != 0, "GICD_CTLR.ARE",
                   f"reset value 0x{ctlr:08x} has ARE=1 (affinity routing)")
        self.check((ctlr & GICD_CTLR_RWP) == 0, "GICD_CTLR.RWP",
                   "RWP reads 0 once the reset write has landed")
        typer = t.read32(GICD_BASE + GICD_TYPER)
        itlines = (typer & 0x1F) + 1
        self.check(itlines > 0, "GICD_TYPER",
                   f"0x{typer:08x} describes {itlines * 32} INTIDs")

        print("\nGroup enables: the DS=1 field layout")
        base = GICD_CTLR_DS | GICD_CTLR_ARE
        t.write32(GICD_BASE + GICD_CTLR, base)
        t.write32(GICD_BASE + GICD_CTLR, base | GICD_CTLR_ENABLEGRP1)
        ctlr = t.read32(GICD_BASE + GICD_CTLR)
        self.check((ctlr & GICD_CTLR_ENABLEGRP1) != 0,
                   "GICD_CTLR bit 0 is EnableGrp1",
                   f"a write of 0x{base | GICD_CTLR_ENABLEGRP1:08x} reads back "
                   f"0x{ctlr:08x}")

        t.write32(GICD_BASE + GICD_CTLR, base | GICD_CTLR_ENABLEGRP0)
        ctlr = t.read32(GICD_BASE + GICD_CTLR)
        self.check((ctlr & GICD_CTLR_ENABLEGRP1) == 0 and
                   (ctlr & GICD_CTLR_ENABLEGRP0) != 0,
                   "GICD_CTLR bit 1 is EnableGrp0, not Group 1",
                   f"writing bit 1 reads back 0x{ctlr:08x}; a test that used bit 1 "
                   "for Group 1 would measure Group 0")

        t.write32(GICD_BASE + GICD_CTLR, base | (1 << 2))
        ctlr = t.read32(GICD_BASE + GICD_CTLR)
        self.check((ctlr & (1 << 2)) == 0,
                   "GICD_CTLR bit 2 is RAZ/WI under DS=1",
                   f"a write of bit 2 reads back 0x{ctlr:08x}")

        print("\nRedistributor identity (CPU 1 frame)")
        typer64 = t.read64(GICR_BASE + FRAME_SIZE + GICR_TYPER)
        self.check((typer64 & GICR_TYPER_LAST) != 0, "GICR_TYPER.LAST",
                   f"0x{typer64:016x} marks CPU 1 as the last frame")
        self.check((typer64 >> 32) != 0, "GICR_TYPER.Affinity",
                   f"0x{typer64 >> 32:08x} names a non-zero affinity")

        print("\nEnable bank: both aliases read the same state")
        t.write32(GICR_BASE + GICR_ISENABLER0, 0)
        t.write32(self.redist(0, GICR_ICENABLER0), 0xFFFFFFFF)
        isen_before = t.read32(self.redist(0, GICR_ISENABLER0))
        self.check(isen_before == 0, "GICR_ICENABLER0 clears all",
                   "writing 0xFFFFFFFF leaves ISENABLER0 = 0")

        t.write32(self.redist(0, GICR_ISENABLER0), 1 << 2)
        isen = t.read32(self.redist(0, GICR_ISENABLER0))
        icen = t.read32(self.redist(0, GICR_ICENABLER0))
        self.check(isen == 1 << 2, "GICR_ISENABLER0 is write-1-to-set",
                   f"enabling SGI 2 reads back 0x{isen:08x}")
        self.check(icen == isen, "GICR_ICENABLER0 reads the enable state",
                   f"the clear-enable alias reads 0x{icen:08x}, not zero")

        t.write32(self.redist(0, GICR_ISENABLER0), 0)
        isen = t.read32(self.redist(0, GICR_ISENABLER0))
        self.check(isen == 1 << 2, "GICR_ISENABLER0 write of 0 is ignored",
                   f"a raw write of 0 leaves the enable bit set (0x{isen:08x})")

        t.write32(self.redist(0, GICR_ICENABLER0), 1 << 2)
        isen = t.read32(self.redist(0, GICR_ISENABLER0))
        icen = t.read32(self.redist(0, GICR_ICENABLER0))
        self.check(isen == 0 and icen == 0, "GICR_ICENABLER0 clears the enable",
                   f"after a write of 0x4 both aliases read 0x{isen:08x}/0x{icen:08x}")

        print("\nPending bank: a software write is the architectural set")
        t.write32(self.redist(0, GICR_ISPENDR0), 1 << 2)
        ispend = t.read32(self.redist(0, GICR_ISPENDR0))
        icpend = t.read32(self.redist(0, GICR_ICPENDR0))
        self.check(ispend == 1 << 2, "GICR_ISPENDR0 sets pending",
                   f"a write of 0x4 makes SGI 2 pending (0x{ispend:08x})")
        self.check(icpend == ispend, "GICR_ICPENDR0 reads the pending state",
                   f"the clear-pending alias reads 0x{icpend:08x}")

        active = t.read32(self.redist(0, GICR_ISACTIVER0))
        self.check(active == 0, "pending is not active",
                   f"GICR_ISACTIVER0 still reads 0x{active:08x}")

        t.write32(self.redist(0, GICR_ICPENDR0), 1 << 2)
        ispend = t.read32(self.redist(0, GICR_ISPENDR0))
        self.check(ispend == 0, "GICR_ICPENDR0 clears pending",
                   f"after a write of 0x4, ISPENDR0 reads 0x{ispend:08x}")

        print("\nActive bank: the same pair, a separate register")
        t.write32(self.redist(0, GICR_ISACTIVER0), 1 << 3)
        isact = t.read32(self.redist(0, GICR_ISACTIVER0))
        icact = t.read32(self.redist(0, GICR_ICACTIVER0))
        self.check(isact == 1 << 3, "GICR_ISACTIVER0 sets active",
                   f"a write of 0x8 makes SGI 3 active (0x{isact:08x})")
        self.check(icact == isact, "GICR_ICACTIVER0 reads the active state",
                   f"the clear-active alias reads 0x{icact:08x}")

        t.write32(self.redist(0, GICR_ICACTIVER0), 1 << 3)
        isact = t.read32(self.redist(0, GICR_ISACTIVER0))
        self.check(isact == 0, "GICR_ICACTIVER0 clears active",
                   f"after a write of 0x8, ISACTIVER0 reads 0x{isact:08x}")

        print("\nGroup: GICD_CTLR.DS=1 does not make the bitmap RAZ/WI")
        t.write32(GICD_BASE + GICD_IGROUPR + 4, 0x1)
        group = t.read32(GICD_BASE + GICD_IGROUPR + 4)
        self.check(group == 0x1, "GICD_IGROUPR1 is writable",
                   f"setting SPI 32 to Group 1 reads back 0x{group:08x}")

        t.write32(GICD_BASE + GICD_IGROUPR + 4, 0x0)
        group = t.read32(GICD_BASE + GICD_IGROUPR + 4)
        self.check(group == 0x0, "GICD_IGROUPR1 is not forced",
                   f"clearing the bit reads back 0x{group:08x}: DS=1 alone does not "
                   "require Group 1")

        t.write32(GICD_BASE + GICD_IGROUPR, 0xFFFFFFFF)
        group = t.read32(GICD_BASE + GICD_IGROUPR)
        rgroup = t.read32(self.redist(0, GICR_IGROUPR0))
        self.check(group == 0, "GICD_IGROUPR0 is RES0 under ARE",
                   f"an SGI/PPI write is ignored (0x{group:08x}); the group lives in "
                   f"GICR_IGROUPR0 (0x{rgroup:08x})")

        print("\nControl and power: the sequences a guest polls")
        t.write32(GICD_BASE + GICD_CTLR, 0)
        ctlr = t.read32(GICD_BASE + GICD_CTLR)
        self.check((ctlr & (GICD_CTLR_DS | GICD_CTLR_ARE)) ==
                   (GICD_CTLR_DS | GICD_CTLR_ARE),
                   "GICD_CTLR.DS and ARE cannot be cleared",
                   f"a write of 0 reads back 0x{ctlr:08x}")

        t.write32(GICR_BASE + GICR_WAKER, 0)
        waker = t.read32(GICR_BASE + GICR_WAKER)
        self.check((waker & GICR_WAKER_CHILDREN_ASLEEP) == 0,
                   "GICR_WAKER wake completes",
                   f"clearing ProcessorSleep leaves ChildrenAsleep=0 (0x{waker:08x})")

        print()
        if self.failures:
            print(f"GICv3 oracle: FAILED ({len(self.failures)} of "
                  f"{self.passes + len(self.failures)} probes)")
            return 1
        print(f"GICv3 oracle: OK ({self.passes} probes, QEMU {self.qemu})")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--qemu", default=None,
                        help="qemu-system-aarch64 to use (default: found on PATH)")
    parser.add_argument("--machine", default="virt,gic-version=3",
                        help="QEMU machine type (default: virt,gic-version=3)")
    parser.add_argument("--cpu", default="cortex-a57",
                        help="CPU model; qtest still realizes a real CPU (default: cortex-a57)")
    parser.add_argument("--require", action="store_true",
                        help="fail instead of skipping when QEMU is not installed")
    args = parser.parse_args()

    qemu = args.qemu or shutil.which("qemu-system-aarch64")
    if not qemu or not os.path.exists(qemu):
        message = "qemu-system-aarch64 not found"
        if args.require:
            print(f"GICv3 oracle: FAILED ({message})", file=sys.stderr)
            return 1
        print(f"GICv3 oracle: skip ({message}; install qemu to run the reference checks)")
        return 0

    try:
        with Oracle(qemu, args.machine, args.cpu) as oracle:
            return oracle.run()
    except (QTestError, OSError) as exc:
        print(f"GICv3 oracle: FAILED ({exc})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
