#!/usr/bin/env python3
"""Check the vGIC conformance self-test that ships in the firmware image.

`AppleVgicSelfTestDxe` is unusual for this project: it is firmware that runs
*before* the operating system and reads the emulated interrupt controller back,
so a mistake in it is not a wrong answer, it is a stopped boot.  Three classes of
mistake are worth a checker rather than a review:

  * it can hang.  A self-test that spins on a register the vGIC never updates is
    worse than no self-test, and the symptom (a boot that stops on the second
    line of the log) looks exactly like the bug it was meant to find.  Every
    wait is therefore required to be a bounded poll in a `for` loop whose header
    carries an `Elapsed <` bound, and the file may not contain a `while` at all.

  * it can stop the boot some other way: `ASSERT`, `CpuDeadLoop`, `gBS->Stall`,
    or a registered interrupt handler that does not write `ICC_EOIR1_EL1`, which
    leaves the virtual CPU interface with no free list registers after the first
    interrupt.

  * it can touch a register this hypervisor does not model.  The distributor
    hook tolerates an unknown offset; the redistributor hook returns false, and a
    false from a stage-2 hook is a fatal guest abort.  The redistributor offsets
    this driver may name are enumerated below for that reason, and GICR_TYPER2
    in particular does not exist (offset 0xC is the upper half of the 64-bit
    GICR_TYPER).

The checks also pin the *claims*: the No1N and IRM-write behaviour that patch
0006 of the m1n1 series implements, the GICR_TYPER.LAST-terminated walk, the
MSI window against the distributor's INTID count, and the log format that
docs/vgic-selftest.md documents.

Two modes, because the repository ships patches rather than a vendored tree:

    --patches DIR   (default) assert the series still adds the driver and wires
                    it in behind `!if $(AIC_BUILD) == FALSE` before TimerDxe.
    --tree PATH     apply the same checks to materialised sources.

Usage:
    ./scripts/check-vgic-selftest.py
    ./scripts/check-vgic-selftest.py --tree third_party/ntasp-firmware

Exit status is 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATCHES = os.path.join(ROOT, "patches", "ntasp-firmware")
DEFAULT_TREE = os.path.join(ROOT, "third_party", "ntasp-firmware")

DRIVER_DIR = "Silicon/Apple/AppleSiliconPkg/Drivers/AppleVgicSelfTestDxe"
DRIVER_C = DRIVER_DIR + "/AppleVgicSelfTestDxe.c"
DRIVER_INF = DRIVER_DIR + "/AppleVgicSelfTestDxe.inf"

#: The component path as the DSC and FDF spell it: relative to the package root.
DSC_COMPONENT = "AppleSiliconPkg/Drivers/AppleVgicSelfTestDxe/AppleVgicSelfTestDxe.inf"
DSC = "Platform/Mac16_3Pkg/Mac16_3.dsc"
FDF = "Platform/Mac16_3Pkg/Mac16_3.fdf"

#: Redistributor offsets the driver may name.  Everything here is a register or
#: bit the vGIC models; anything else is either a fatal abort (unmodelled
#: register) or a register that does not exist (offset 0xC is GICR_TYPER[63:32]).
ALLOWED_GICR = {
    "GICR_CTLR",
    "GICR_CTLR_RESERVED_WRITE_MASK",
    "GICR_FRAME_SIZE",
    "GICR_SGI_ICACTIVER",
    "GICR_SGI_ICENABLER",
    "GICR_SGI_ICPENDR",
    "GICR_SGI_IGROUPR0",
    "GICR_SGI_ISACTIVER",
    "GICR_SGI_ISENABLER",
    "GICR_SGI_ISPENDR",
    "GICR_TYPER",
    "GICR_TYPER_AFFINITY_MASK",
    "GICR_TYPER_DIRECTLPI",
    "GICR_TYPER_DPGS",
    "GICR_TYPER_LAST",
    "GICR_TYPER_PLPIS",
    "GICR_TYPER_PROCNUM_MASK",
    "GICR_TYPER_PROCNUM_SHIFT",
    "GICR_TYPER_VLPIS",
    "GICR_WAKER",
    "GICR_WAKER_CHILDREN_ASLEEP",
    "GICR_WAKER_PROCESSOR_SLEEP",
}


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passes = 0

    def check(self, ok: bool, message: str) -> bool:
        if ok:
            self.passes += 1
            print(f"  ok   {message}")
        else:
            print(f"  FAIL {message}")
            self.failures.append(message)
        return ok

    @staticmethod
    def section(title: str) -> None:
        print(f"\n{title}")


# --------------------------------------------------------------------------
# C helpers.  Comments and literals are blanked, not removed, so an offset into
# the stripped text is an offset into the original.  That matters because this
# file's comments name the things it refuses to do ("GICR_TYPER2 does not
# exist", "no software path can raise an SPI") and a checker that read them as
# code would fail on its own documentation.
# --------------------------------------------------------------------------

def strip_c(text: str) -> str:
    out = list(text)
    i, n = 0, len(text)

    def blank(start: int, end: int) -> None:
        for k in range(start, end):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        c = text[i]
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            blank(i, end)
            i = end
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            end = text.find("\n", i)
            end = n if end < 0 else end
            blank(i, end)
            i = end
        elif c == '"':
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == '"':
                    j += 1
                    break
                j += 1
            blank(i, j)
            i = j
        else:
            i += 1
    return "".join(out)


def match_brace(text: str, open_index: int) -> int:
    depth = 0
    for i in range(open_index, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def function_body(text: str, name: str) -> str:
    """Body of the definition of `name`, comments and literals blanked."""
    match = re.search(r"\b" + re.escape(name) + r"\s*\(", text)
    if not match:
        return ""
    depth = 0
    i = match.end() - 1
    while i < len(text):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    open_index = text.find("{", i)
    if open_index < 0:
        return ""
    close = match_brace(text, open_index)
    if close < 0:
        return ""
    return text[open_index:close + 1]


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def has(body: str, pattern: str) -> bool:
    """Whitespace-insensitive regex search."""
    return re.search(pattern, squash(body)) is not None


# --------------------------------------------------------------------------
# The contract.
# --------------------------------------------------------------------------

def check_driver(ck: Checker, source: str, inf: str) -> None:
    code = strip_c(source)
    # Format strings live inside literals, which strip_c() blanks, so the log
    # format is checked against the raw text.
    raw = source

    ck.section("The image actually runs the test")
    ck.check("AppleVgicSelfTestInitialize" in code,
             "the driver has an entry point")
    ck.check(re.search(r"#include\s+<Protocol/HardwareInterrupt\.h>", code) is not None,
             "it includes the interrupt protocol header, not a local copy")
    ck.check(re.search(r"gEfiCpuArchProtocolGuid\s+AND\s+gHardwareInterruptProtocolGuid",
                       inf) is not None,
             "INF: the depex waits for the CPU arch and interrupt protocols")
    ck.check(re.search(r"#include\s+<Library/TimerLib\.h>", code) is not None,
             "it includes TimerLib, so the waits are real microseconds")

    ck.section("The test cannot hang")
    ck.check(re.search(r"\bwhile\s*\(", code) is None,
             "the driver contains no while loop")
    ck.check(re.search(r"\bfor\s*\(\s*;\s*;", code) is None,
             "the driver contains no for (;;) loop")

    wait_count = function_body(code, "VgicWaitCount")
    wait_reg = function_body(code, "VgicWaitRegister")
    for name, body in (("VgicWaitCount", wait_count), ("VgicWaitRegister", wait_reg)):
        ck.check(bool(body), f"{name}() exists")
        ck.check(has(body, r"Elapsed\s*<\s*TimeoutUs"),
                 f"{name}(): the poll is bounded by TimeoutUs")
        ck.check("TimeoutUs" in body and body.count("return") >= 2,
                 f"{name}(): returns a result on both the success and the timeout path")

    # Every MicroSecondDelay() must sit inside a for-loop with an Elapsed bound.
    delays = [m.start() for m in re.finditer(r"MicroSecondDelay\s*\(", code)]
    ck.check(bool(delays), "the driver uses MicroSecondDelay() for its waits")
    loose = []
    for index in delays:
        loop = code.rfind("for", 0, index)
        header = code[loop:index] if loop >= 0 else ""
        brace = header.find("{")
        header = header[:brace] if brace >= 0 else header
        if not re.search(r"Elapsed\s*<", header):
            line = code[:index].count("\n") + 1
            loose.append(line)
    ck.check(not loose,
             "every MicroSecondDelay() call is inside a loop with an Elapsed bound"
             + (f" (line {', '.join(map(str, loose))})" if loose else ""))

    ck.section("The test cannot stop the boot")
    # ASSERT_EFI_ERROR() and ASSERT_EFI_ERROR_RETURN() are ASSERT() with a status
    # argument, and they stop the boot just as abruptly, so the pattern covers
    # the whole family rather than the bare macro.
    for pattern, what in (
        (r"\bASSERT(?:_[A-Z0-9_]+)?\s*\(", "ASSERT"),
        (r"\bCpuDeadLoop\s*\(", "CpuDeadLoop"),
        (r"gBS\s*->\s*Stall\s*\(", "gBS->Stall"),
    ):
        ck.check(re.search(pattern, code) is None, f"no {what}, which would stop the boot")

    handler = function_body(code, "VgicTestHandler")
    ck.check(bool(handler), "VgicTestHandler() exists")
    ck.check(has(handler, r"EndOfInterrupt\s*\("),
             "the handler writes EndOfInterrupt itself (ArmGicDxe only EOI's when no "
             "handler is registered)")

    registers = re.findall(r"RegisterInterruptSource\s*\(([^;]*?)\)\s*;", squash(code))
    unregisters = [r for r in registers if r.rstrip().endswith("NULL")]
    registrations = [r for r in registers if not r.rstrip().endswith("NULL")]
    ck.check(len(unregisters) >= len(registrations) and bool(registrations),
             f"every registered handler is unregistered again "
             f"({len(registrations)} registered, {len(unregisters)} unregistered)")

    ck.section("The test only names registers this vGIC models")
    named = set(re.findall(r"\b(GICR_[A-Z0-9_]+)\b", code))
    unexpected = sorted(named - ALLOWED_GICR)
    ck.check(not unexpected,
             "no redistributor register outside the modelled set"
             + (f" (found {', '.join(unexpected)})" if unexpected else ""))
    ck.check("GICR_TYPER2" not in code,
             "GICR_TYPER2 is never read: offset 0xC is GICR_TYPER[63:32], and an "
             "unmodelled redistributor offset is a fatal guest abort")
    ck.check("0xfffff000" not in code.lower() and "0xFFFFF000" not in code,
             "nothing writes the MSI doorbell, which is not mapped into the guest")

    walk = function_body(code, "VgicWalkRedistributors")
    ck.check(bool(walk), "VgicWalkRedistributors() exists")
    ck.check(has(walk, r"Typer\s*&\s*GICR_TYPER_LAST"),
             "the walk ends at GICR_TYPER.LAST instead of running off the mapped frames")
    ck.check(has(walk, r"Index\s*<\s*CoreCount"),
             "the walk is bounded by the platform's core count as well")

    frames = function_body(code, "VgicCheckRedistributorFrames")
    writes = re.findall(r"MmioWrite32\s*\(\s*Base\s*\+\s*GICR_WAKER\s*,\s*([^;]*)\)", squash(frames))
    reads = re.findall(r"=\s*MmioRead32\s*\(\s*Base\s*\+\s*GICR_WAKER\s*\)", squash(frames))
    ck.check(bool(writes) and len(reads) >= len(writes) and all("Waker" in w for w in writes),
             "every GICR_WAKER write is a read-modify-write from a fresh read, so "
             "ChildrenAsleep is not preserved from a stale value")
    ck.check(has(frames, r"GICR_CTLR_RESERVED_WRITE_MASK"),
             "GICR_CTLR is checked against the RAZ/WI/RES0 mask")

    ck.section("The test restores what it changes")
    control = function_body(code, "VgicCheckDistributorControl")
    ck.check(has(control, r"MmioWrite32\s*\(\s*Dist\s*\+\s*GICD_CTLR\s*,\s*Original\s*\)"),
             "GICD_CTLR is written back from the value that was read")
    spi = function_body(code, "VgicCheckSpiRegisters")
    ck.check(has(spi, r"MmioWrite32\s*\(\s*PriorityWord\s*,\s*OriginalPriority\s*\)"),
             "the priority word is restored after the probe")
    router = function_body(code, "VgicCheckIrouter")
    ck.check(has(router, r"MmioWrite64\s*\(\s*Address\s*,\s*Original\s*\)"),
             "GICD_IROUTER is restored after the probe")

    ck.section("The SGI/PPI bank probe measures set/clear pairs, not write history")
    banks = function_body(code, "VgicCheckRedistributorBanks")
    ck.check(bool(banks), "VgicCheckRedistributorBanks() exists")
    for macro, desc in (
        ("GICR_SGI_ISENABLER", "enable set"),
        ("GICR_SGI_ICENABLER", "enable clear"),
        ("GICR_SGI_ISPENDR", "pending set"),
        ("GICR_SGI_ICPENDR", "pending clear"),
        ("GICR_SGI_ISACTIVER", "active set"),
        ("GICR_SGI_ICACTIVER", "active clear"),
    ):
        ck.check(macro in banks, f"the probe touches {macro} ({desc})")
    ck.check(has(banks, r"MmioWrite32\s*\(\s*SgiBase\s*\+\s*GICR_SGI_ISENABLER\s*,\s*0\s*\)"),
             "the probe writes 0 to GICR_ISENABLER0 and requires the bit to stay set, "
             "which is what separates write-1-to-set from a raw store")
    ck.check(has(banks, r"OriginalEnable") and has(banks, r"OriginalPending") and
             has(banks, r"OriginalActive") and has(banks, r"OriginalGroup"),
             "the probe saves the state it changes")
    ck.check(has(banks, r"MmioWrite32\s*\(\s*SgiBase\s*\+\s*GICR_SGI_ICENABLER\s*,\s*Bit\s*\)") and
             has(banks, r"MmioWrite32\s*\(\s*SgiBase\s*\+\s*GICR_SGI_IGROUPR0\s*,\s*OriginalGroup\s*\)"),
             "the probe restores the enable bit and the group register before returning")
    ck.check(code.count("VgicCheckRedistributorBanks") >= 2 and
             has(code, r"VgicCheckRedistributorBanks\s*\([^;]*BootFrame"),
             "the entry point runs the probe on the boot PE's own frame")

    ck.section("The Windows HAL replay is bounded and reversible")
    hal = function_body(code, "VgicCheckWindowsHalSequence")
    ck.check(bool(hal), "VgicCheckWindowsHalSequence() exists")
    ck.check(has(hal, r"VGIC_MSI_BANKS"),
             "the replay walks the MSI window bank by bank, not one hard-coded register")
    ck.check(has(hal, r"MmioWrite32\s*\(\s*Dist\s*\+\s*GICD_ICENABLER[^,]*,\s*0xFFFFFFFF\s*\)"),
             "it writes the HAL's 0xFFFFFFFF teardown to GICD_ICENABLER")
    ck.check(has(hal, r"SavedCtlr\s*&\s*~\s*GICD_CTLR_ENABLEGRP1NS"),
             "only EnableGrp1NS is cleared; DS and ARE are never written to zero")
    ck.check(has(hal, r"MmioWrite32\s*\(\s*Dist\s*\+\s*GICD_CTLR\s*,\s*SavedCtlr\s*\)"),
             "GICD_CTLR is restored from the value that was read")
    ck.check(has(hal, r"MmioWrite32\s*\(\s*Dist\s*\+\s*GICD_ISENABLER[^,]*,\s*SavedEnable"),
             "every ICENABLER bank is restored from its saved enables")
    ck.check(has(hal, r"SavedRouteFirst") and has(hal, r"SavedRouteLast"),
             "both probed IROUTER values are saved and restored")
    ck.check(has(hal, r"VgicGap\s*\(") and '"windows.grp1"' in raw,
             "a group disable the device ignores is reported as a gap, not silently accepted")

    ck.section("GICD_CTLR group enables use the DS=1 bit layout")
    ck.check(
        re.search(r"#define\s+GICD_CTLR_ENABLEGRP1NS\s+\(\s*1U\s*<<\s*0\s*\)", code) is not None,
        "EnableGrp1 is bit 0 under DS=1; bit 1 is EnableGrp0, and checking it would "
        "measure the wrong group",
    )

    ck.section("No1N and IRM agree with what the vGIC implements")
    ck.check(re.search(r"#define\s+GICD_TYPER_NO1N\s+\(1U\s*<<\s*25\)", code) is not None,
             "GICD_TYPER.No1N is named as bit 25")
    caps = function_body(code, "VgicCheckDistributorCaps")
    ck.check(has(caps, r"Typer\s*&\s*GICD_TYPER_NO1N"),
             "the capability check reads No1N")
    ck.check(has(router, r"Affinity\s*\|?\s*GICD_IROUTER_IRM"),
             "the test writes IROUTER with IRM = 1")
    ck.check(has(router, r"ReadBack\s*&\s*GICD_IROUTER_IRM\s*\)\s*==\s*0"),
             "and requires IRM to read back as 0")
    ck.check(has(caps, r"NumIntIds\s*==\s*1024"),
             "the distributor must describe 1024 INTIDs")
    ck.check(has(caps, r"\(\s*VGIC_MSI_SPI_BASE\s*\+\s*VGIC_MSI_VECTORS\s*\)\s*<=\s*NumIntIds"),
             "the MSI window is checked to fit inside that INTID space")

    ck.section("The enable gate is measured in both directions")
    gating = function_body(code, "VgicCheckSgiGating")
    ck.check(bool(gating), "VgicCheckSgiGating() exists")
    ck.check(has(gating, r"MmioWrite32\s*\(\s*BootFrameBase\s*\+\s*GICR_SGI_ICENABLER\s*,\s*Bit\s*\)"),
             "the test disables the SGI behind the interrupt protocol's back")
    ck.check(has(gating, r"MmioRead32\s*\(\s*BootFrameBase\s*\+\s*GICR_SGI_ISENABLER\s*\)"),
             "it confirms the disable took effect before measuring delivery")
    ck.check(has(gating, r"VgicGap\s*\(") and '"gating.sgi"' in raw,
             "a delivered-but-disabled SGI is reported as a gap, not accepted")
    ck.check(has(gating, r"MmioWrite32\s*\(\s*BootFrameBase\s*\+\s*GICR_SGI_ISENABLER\s*,\s*Bit\s*\)"),
             "it re-enables the source before returning")
    ck.check(has(gating, r"VgicFail\s*\(") and '"gating.rearm"' in raw,
             "a gate that stays shut after re-enabling is a failure: too strict is "
             "as wrong as too loose")

    #
    # gating.pending is the only probe that distinguishes "queued while
    # disabled" from "dropped while disabled".  It has to enable the bit and
    # then wait without sending again; a resend would satisfy the wait even if
    # the refused interrupt had been discarded.
    #
    pending_at = raw.find('"gating.pending"')
    ck.check(pending_at > 0,
             "the test measures an SGI that stayed pending across a disable "
             "(gating.pending)")
    before_pending = raw[:pending_at] if pending_at > 0 else ""
    enable_at = before_pending.rfind("GICR_SGI_ISENABLER")
    resend_at = before_pending.rfind("IccSgi1r")
    ck.check(enable_at > resend_at >= 0,
             "gating.pending re-enables the bit after the refused send")
    ck.check(has(before_pending[enable_at:], r"VgicWaitCount"),
             "gating.pending waits for delivery after that write")
    ck.check(before_pending.find("IccSgi1r", enable_at) < 0,
             "gating.pending does not send a second SGI before waiting: a resend "
             "would hide an interrupt the vGIC dropped instead of queueing")

    #
    # The group gate is the other half of the predicate: every list register
    # is injected as Group 1, so clearing GICD_CTLR.EnableGrp1 with the SGI's
    # own bit set has to stop delivery.  Restoring it has to deliver the SGI
    # that was sent in between, without a resend.
    #
    #
    # function_body() blanks string literals, so the probe names have to be
    # located in the raw slice, not in the blanked body used for structure.
    # Slice from this function to the next one so the Windows HAL replay's own
    # GICD_CTLR write cannot satisfy these checks by accident.
    #
    gating_start = raw.find("VgicCheckSgiGating")
    gating_end = raw.find("VgicCheckPpiDelivery", gating_start)
    gating_raw = (
        raw[gating_start:gating_end]
        if gating_start > 0 and gating_end > gating_start
        else ""
    )
    group_at = gating_raw.find('"gating.group1"')
    ck.check(gating_start > 0 and group_at > 0,
             "the test measures delivery while GICD_CTLR.EnableGrp1 is clear "
             "(gating.group1)")
    before_group = gating_raw[:group_at] if group_at > 0 else ""
    group_disable_at = before_group.rfind(
        "SavedCtlr & ~GICD_CTLR_ENABLEGRP1NS"
    )
    group_send_at = before_group.rfind("IccSgi1r")
    ck.check(group_disable_at > 0 and group_send_at > group_disable_at,
             "gating.group1 clears EnableGrp1 before sending the SGI")
    ck.check(has(
                 before_group[group_disable_at:],
                 r"SavedCtlr\s*&\s*~\s*GICD_CTLR_ENABLEGRP1NS"),
             "gating.group1 clears only EnableGrp1, preserving DS and ARE")
    ck.check(has(before_group[group_disable_at:], r"VgicWaitCount"),
             "gating.group1 waits before concluding the SGI was not delivered")
    ck.check(has(
                 gating,
                 r"MmioWrite32\s*\(\s*DistBase\s*\+\s*GICD_CTLR\s*,\s*SavedCtlr\s*\)"),
             "gating.group1 restores GICD_CTLR from the value that was read")
    group_pending_at = gating_raw.find('"gating.group1-pending"')
    ck.check(group_pending_at > group_at,
             "the test measures that the Group 1 refusal stayed pending "
             "(gating.group1-pending)")
    if group_pending_at > group_at:
        restore_at = gating_raw.find("GICD_CTLR, SavedCtlr)", group_at)
        between = gating_raw[restore_at:group_pending_at] if restore_at > 0 else ""
        ck.check(restore_at > 0 and "IccSgi1r" not in between,
                 "gating.group1-pending does not send a second SGI before "
                 "waiting: a resend would hide an interrupt the vGIC dropped")

    ck.section("Delivery uses the platform's own interrupt numbers")
    ck.check("PcdArmArchTimerVirtIntrNum" in code,
             "the virtual timer PPI comes from PcdArmArchTimerVirtIntrNum")
    ppi = function_body(code, "VgicCheckPpiDelivery")
    ck.check(bool(ppi) and "VirtTimerIntId" in ppi,
             "the PPI delivery test uses that PCD value")
    ck.check(has(ppi, r"CntvSetCtl\s*\(\s*0x2\s*\)"),
             "the virtual timer is left disabled for TimerDxe")

    ck.section("The log format is the documented one")
    ck.check(re.search(r'"vgic: %a %a: %a\\n"', raw) is not None,
             "every result line is printed as 'vgic: [KIND] name: detail'")
    for kind in ("[PASS]", "[FAIL]", "[GAP ]", "[SKIP]"):
        ck.check(f'"{kind}"' in raw, f"the {kind} result kind is emitted")
    for pattern, what in (
        (r'"vgic: RESULT: FAIL', "RESULT: FAIL"),
        (r'"vgic: RESULT: PASS-WITH-GAPS', "RESULT: PASS-WITH-GAPS"),
        (r'"vgic: RESULT: PASS\\n', "RESULT: PASS"),
        (r'"vgic: totals: pass=%u fail=%u gap=%u skip=%u', "the totals line"),
    ):
        ck.check(re.search(pattern, raw) is not None, f"the summary reports {what}")

    ck.section("The trace is printable by EDK2's PrintLib")
    ck.check("%ll" not in raw,
             "no %ll format specifier, which EDK2's PrintLib does not implement")

    ck.section("The INF declares what the code uses")
    gets = set(re.findall(r"\bPcdGet(?:8|16|32|64)\s*\(\s*([A-Za-z0-9_]+)\s*\)", code))
    declared = set(re.findall(r"^\s*g[A-Za-z0-9_]+\.(Pcd[A-Za-z0-9_]+)\s*$", inf,
                              re.MULTILINE))
    ck.check(bool(gets), "the driver reads at least one PCD")
    ck.check(gets == declared,
             f"the INF [Pcd] list matches the PCDs the code reads "
             f"(code {sorted(gets)}, INF {sorted(declared)})")


def check_tree(ck: Checker, tree: str) -> None:
    try:
        with open(os.path.join(tree, DRIVER_C), "r", encoding="utf-8", errors="replace") as handle:
            source = handle.read()
        with open(os.path.join(tree, DRIVER_INF), "r", encoding="utf-8", errors="replace") as handle:
            inf = handle.read()
    except OSError as exc:
        ck.check(False, f"read the driver: {exc}")
        return

    check_driver(ck, source, inf)

    ck.section("The platform builds it into the image")
    tree_dsc = read(os.path.join(tree, DSC))
    tree_fdf = read(os.path.join(tree, FDF))

    ck.check(DSC_COMPONENT in tree_dsc,
             "the DSC lists the driver as a component")
    dsc_index = tree_dsc.find(DSC_COMPONENT)
    guard = tree_dsc.rfind("!if $(AIC_BUILD) == FALSE", 0, dsc_index)
    endif = tree_dsc.find("!endif", dsc_index)
    ck.check(guard >= 0 and 0 <= dsc_index < endif,
             "the component is inside the AIC_BUILD == FALSE block (the AIC build has no vGIC)")

    ck.check(DSC_COMPONENT in tree_fdf,
             "the FDF places the driver in the DXE FV")
    fdf_driver = tree_fdf.find(DSC_COMPONENT)
    fdf_timer = tree_fdf.find("INF ArmPkg/Drivers/TimerDxe/TimerDxe.inf")
    ck.check(0 <= fdf_driver < fdf_timer,
             "the driver precedes TimerDxe in the FV, so it owns the virtual timer PPI first")


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


# --------------------------------------------------------------------------
# Patch-series mode.
# --------------------------------------------------------------------------

def series_files(patch_dir: str) -> dict[str, list[str]]:
    """{path: [raw added lines, in series order]} for every file the series adds."""
    added: dict[str, list[str]] = {}
    for path in sorted(glob.glob(os.path.join(patch_dir, "*.patch"))):
        current = None
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.rstrip("\n")
                if line.startswith("diff --git "):
                    match = re.match(r"diff --git a/(\S+) b/(\S+)", line)
                    current = match.group(2) if match else None
                    continue
                if current is None:
                    continue
                if line.startswith("+") and not line.startswith("+++"):
                    added.setdefault(current, []).append(line[1:])
    return added


def series_hunks(patch_dir: str, wanted: str) -> list[list[tuple[str, str]]]:
    """Hunks that touch `wanted`, each a list of (prefix, line)."""
    hunks: list[list[tuple[str, str]]] = []
    for path in sorted(glob.glob(os.path.join(patch_dir, "*.patch"))):
        current = None
        hunk: list[tuple[str, str]] | None = None
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.rstrip("\n")
                if line.startswith("diff --git "):
                    match = re.match(r"diff --git a/(\S+) b/(\S+)", line)
                    current = match.group(2) if match else None
                    hunk = None
                    continue
                if current != wanted:
                    continue
                if line.startswith("@@"):
                    hunk = []
                    hunks.append(hunk)
                    continue
                if hunk is None or not line:
                    continue
                if line[0] in " +-":
                    hunk.append((line[0], line[1:]))
    return hunks


def check_patches(ck: Checker, patch_dir: str) -> None:
    added = series_files(patch_dir)

    if DRIVER_C not in added or DRIVER_INF not in added:
        ck.check(False, f"the series does not add {DRIVER_C} and {DRIVER_INF}")
        return

    check_driver(ck, "\n".join(added[DRIVER_C]) + "\n", "\n".join(added[DRIVER_INF]) + "\n")

    ck.section("The platform build wires it in")
    dsc = [h for h in series_hunks(patch_dir, DSC)
           if any(DSC_COMPONENT in text for _, text in h)]
    ck.check(bool(dsc), f"a {DSC} hunk adds the component")
    if dsc:
        text = [t for _, t in dsc[0]]
        index = next(i for i, t in enumerate(text) if DSC_COMPONENT in t)
        guard = [i for i, t in enumerate(text) if "!if $(AIC_BUILD) == FALSE" in t]
        ck.check(any(i < index for i in guard),
                 "the component is behind !if $(AIC_BUILD) == FALSE")

    fdf = [h for h in series_hunks(patch_dir, FDF)
           if any(DSC_COMPONENT in text for _, text in h)]
    ck.check(bool(fdf), f"a {FDF} hunk adds the driver to the FV")
    if fdf:
        lines = fdf[0]
        index = next(i for i, (_, t) in enumerate(lines) if DSC_COMPONENT in t)
        ck.check(any("TimerDxe" in t for _, t in lines[index + 1:]),
                 "the driver is placed before TimerDxe in the FV")
        ck.check(any(t.strip() == "!if $(AIC_BUILD) == FALSE" for _, t in lines[:index]),
                 "the FV entry is behind !if $(AIC_BUILD) == FALSE")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--patches", metavar="DIR", default=None,
                        help=f"verify a patch series (default: {DEFAULT_PATCHES})")
    parser.add_argument("--tree", metavar="PATH", default=None,
                        help=f"verify materialised sources (for example {DEFAULT_TREE})")
    args = parser.parse_args()

    ck = Checker()
    if args.tree:
        print(f"vGIC self-test: tree {args.tree}")
        check_tree(ck, args.tree)
    else:
        patch_dir = args.patches or DEFAULT_PATCHES
        print(f"vGIC self-test: patch series {patch_dir}")
        check_patches(ck, patch_dir)

    print()
    if ck.failures:
        print(f"vGIC self-test: FAILED ({len(ck.failures)} of {ck.passes + len(ck.failures)} checks)")
        return 1
    print(f"vGIC self-test: OK ({ck.passes} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
