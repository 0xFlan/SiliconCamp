#!/usr/bin/env python3
"""Verify the guest-visible GICv3 contract of the m1n1-windows vGIC.

The vGIC emulates a GICv3 distributor and redistributors while Apple's hardware
provides the CPU interface.  UEFI is a lenient guest: EDK2's `ArmGicV3Dxe`
scans redistributor frames only until it finds the boot CPU's affinity, never
writes `GICR_WAKER`, and never polls `RWP`.  Windows is not lenient.  Its HAL
walks every redistributor, wakes each PE through `GICR_WAKER`, and treats a
register that never settles as a hang.  A vGIC that passes UEFI can therefore
still stop the Windows kernel before it prints anything, which is exactly the
failure this checker exists to prevent from being reintroduced silently.

The checks are architectural, not stylistic: each one corresponds to a rule in
ARM IHI 0069 that a guest is entitled to rely on, and to a bug this project
shipped at some point.  Every failure prints the rule, so the fix is obvious
without reading this file.

Two modes, because the repository ships patches rather than a vendored tree:

    --patches DIR   (default) assert that the cumulative patch series *adds* the
                    fixes and *removes* the bugs.  Needs no submodule, no
                    compiler, and no hardware, so CI runs it on every push.

    --tree PATH     apply the same contract to materialised sources, which is
                    how the checks were written and how a future contributor
                    should verify an edit before regenerating a patch.

Usage:
    ./scripts/check-vgic.py                       # patches/m1n1-windows
    ./scripts/check-vgic.py --patches patches/m1n1-windows
    ./scripts/check-vgic.py --tree third_party/m1n1-windows

Exit status is 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATCHES = os.path.join(ROOT, "patches", "m1n1-windows")
DEFAULT_TREE = os.path.join(ROOT, "third_party", "m1n1-windows")

#: Source files the contract spans, relative to the m1n1 tree root.
SOURCES = (
    "src/hv_vgic.c",
    "src/hv_vgic.h",
    "src/hv_exc.c",
    "src/guest_msi.c",
    "src/guest_msi.h",
)


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
# C source helpers.
#
# Comments and literals are blanked rather than removed so that every offset in
# the stripped text is also an offset in the original.  That matters because the
# vGIC comments deliberately name the bugs they replaced ("the previous code set
# BIT(31)"), and a checker that matched those would fail on its own explanation.
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
        elif c in "\"'":
            quote = c
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == quote:
                    j += 1
                    break
                j += 1
            blank(i, j)
            i = j
        else:
            i += 1
    return "".join(out)


def match_brace(text: str, open_index: int) -> int:
    """Index of the '}' matching the '{' at open_index, or -1."""
    depth = 0
    for i in range(open_index, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def block_from(text: str, index: int) -> str:
    """The brace-delimited block that starts at or after index."""
    open_index = text.find("{", index)
    if open_index < 0:
        return ""
    close = match_brace(text, open_index)
    if close < 0:
        return ""
    return text[open_index:close + 1]


def function_body(text: str, name: str) -> str:
    """The body of the definition of `name`, comments and literals blanked."""
    match = re.search(r"\b" + re.escape(name) + r"\s*\(", text)
    if not match:
        return ""
    # Walk to the matching ')' of the parameter list, then take the body.
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
    return block_from(text, i)


def slice_after(text: str, anchor: str) -> str:
    """The brace block following the first occurrence of anchor."""
    index = text.find(anchor)
    if index < 0:
        return ""
    return block_from(text, index + len(anchor))


def case_block(text: str, anchor: str) -> str:
    """A `case ...:` arm, from the label to its terminating `break;`.

    Switch arms in this file are written without braces, so brace matching would
    run past the arm and pick up an unrelated block.  Depth is tracked from the
    label so a nested `if {}` inside the arm is still handled.
    """
    index = text.find(anchor)
    if index < 0:
        return ""
    depth = 0
    i = index
    while i < len(text):
        char = text[i]
        if char == "{":
            depth += 1
        elif char == "}":
            if depth == 0:
                return text[index:i]
            depth -= 1
        elif depth == 0 and text.startswith("break;", i):
            return text[index:i + len("break;")]
        i += 1
    return text[index:]


def mask_calls(text: str) -> list[str]:
    """First argument of every aic_set_mask() call, comments blanked."""
    return [m.group(1).strip() for m in re.finditer(r"aic_set_mask\s*\(\s*([^,()]+)", text)]


def affinity_calls(text: str) -> list[str]:
    """First argument of every aic_set_affinity() call, comments blanked."""
    return [m.group(1).strip() for m in re.finditer(r"aic_set_affinity\s*\(\s*([^,()]+)", text)]


def strip_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text)


# --------------------------------------------------------------------------
# The contract, applied to materialised sources.
# --------------------------------------------------------------------------

def check_tree(ck: Checker, tree: str) -> None:
    sources: dict[str, str] = {}
    for rel in SOURCES:
        path = os.path.join(tree, rel)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                sources[rel] = strip_c(handle.read())
        except OSError as exc:
            ck.check(False, f"read {rel}: {exc}")
            return

    vgic = sources["src/hv_vgic.c"]
    vgic_h = sources["src/hv_vgic.h"]
    exc = sources["src/hv_exc.c"]
    msi = sources["src/guest_msi.c"]
    msi_h = sources["src/guest_msi.h"]

    redist = function_body(vgic, "handle_vgic_redist_access")
    dist = function_body(vgic, "handle_vgic_dist_access")

    ck.section("Redistributor framing (ARM IHI 0069: one GICR frame per PE, 0x20000 bytes)")
    ck.check(re.search(r"#define\s+GICR_FRAME_SIZE\s+0x20000\b", vgic_h) is not None,
             "hv_vgic.h: GICR_FRAME_SIZE is 0x20000")
    ck.check(bool(redist), "hv_vgic.c: found handle_vgic_redist_access()")
    ck.check(re.search(r"\bframe\s*=\s*frame_offset\s*/\s*GICR_FRAME_SIZE\b", redist) is not None,
             "redistributor: the frame index is computed from the faulting address")
    ck.check("% GICR_FRAME_SIZE" in redist,
             "redistributor: the register offset is the address within the frame "
             "(modulo GICR_FRAME_SIZE), not the address above the base")
    ck.check(re.search(r"if\s*\(\s*frame\s*>=\s*num_cpus\s*\)", redist) is not None,
             "redistributor: a frame index past num_cpus is rejected instead of aliasing")
    ck.check(re.search(r"\bcpu_num\s*=\s*\(\s*u8\s*\)\s*frame\b", redist) is not None,
             "redistributor: cpu_num is the frame index, so the address selects the vCPU")
    ck.check("ctx->cpu_id" not in redist,
             "redistributor: cpu_num is not the CPU that took the fault "
             "(that made every frame after the first unreachable)")
    ck.check(re.search(r"hv_map_hook\s*\(\s*redist_base\s*,\s*handle_vgic_redist_access\s*,\s*"
                       r"\(?\s*GICR_FRAME_SIZE\s*\*\s*num_cpus", vgic) is not None,
             "hv_vgic.c: the redistributor hook spans GICR_FRAME_SIZE * num_cpus")

    ck.section("GICD_CTLR / GICR_CTLR: RWP is never left set")
    ck.check("is_rwp_to_be_set" not in vgic and "is_uwp_to_be_set" not in vgic,
             "hv_vgic.c: no deferred-RWP bookkeeping (RWP would never clear from a poll)")
    ck.check(re.search(r"gicd_ctlr_new_val\s*&=\s*~BIT\(31\)", dist) is not None,
             "GICD_CTLR: RWP (bit 31) is explicitly cleared")
    ck.check(re.search(r"gicd_ctl_reg\s*\|=\s*BIT\(31\)", vgic) is None,
             "GICD_CTLR: no path sets RWP (bit 31)")
    ck.check(re.search(r"gicr_ctlr_new_val\s*&=\s*~BIT\(3\)", redist) is not None,
             "GICR_CTLR: RWP (bit 3) is explicitly cleared")
    ck.check(re.search(r"gicr_ctlr_new_val\s*\|=\s*BIT\(31\)", redist) is None,
             "GICR_CTLR: bit 31 (UWP) is not written; it is RES0 when VLPIS=0")

    ck.section("GICR_WAKER: a guest can wake a PE")
    waker = case_block(redist, "case GIC_REDIST_WAKER:")
    ck.check(bool(waker), "hv_vgic.c: found the GICR_WAKER write case")
    ck.check(re.search(r"gicr_wake_reg\s*=\s*\*val\b", waker) is None,
             "GICR_WAKER: the write is not stored verbatim (ChildrenAsleep is read-only)")
    ck.check("GICR_WAKER_PROCESSOR_SLEEP" in waker,
             "GICR_WAKER: ProcessorSleep is decoded from the write")
    ck.check(re.search(r"gicr_wake_reg\s*=\s*0\b", waker) is not None,
             "GICR_WAKER: clearing ProcessorSleep clears ChildrenAsleep, so the poll terminates")
    ck.check(re.search(r"#define\s+GICR_WAKER_PROCESSOR_SLEEP\s+BIT\(1\)", vgic_h) is not None
             and re.search(r"#define\s+GICR_WAKER_CHILDREN_ASLEEP\s+BIT\(2\)", vgic_h) is not None,
             "hv_vgic.h: ProcessorSleep is bit 1 and ChildrenAsleep is bit 2")
    ck.check(re.search(r"gicr_wake_reg\s*=\s*\(?\s*GICR_WAKER_PROCESSOR_SLEEP\s*\|\s*"
                       r"GICR_WAKER_CHILDREN_ASLEEP", vgic) is not None,
             "hv_vgic.c: the reset value is ProcessorSleep|ChildrenAsleep")

    ck.section("Capabilities: advertise only what is implemented")
    typer_line = re.search(r"distributor->gicd_type_reg\s*=\s*([^;]+);", vgic)
    ck.check(typer_line is not None, "hv_vgic.c: GICD_TYPER is programmed")
    if typer_line:
        ck.check("BIT(17)" not in typer_line.group(1),
                 "GICD_TYPER: LPIS (bit 17) is 0; there is no ITS behind it")
    ck.check(re.search(r"gicr_typer\s*\|=\s*BIT\(0\)", vgic) is None,
             "GICR_TYPER: PLPIS (bit 0) is 0; physical LPIs are not implemented")
    ck.check(re.search(r"gicr_typer\s*\|=\s*BIT\(3\)", vgic) is None,
             "GICR_TYPER: DirectLPI (bit 3) is 0; GICR_SETLPIR is a no-op")
    ck.check(re.search(r"gicr_typer\s*\|=\s*BIT\(5\)", vgic) is None,
             "GICR_TYPER: DPGS (bit 5) is 0; GICR_CTLR.DPG* are not implemented")
    ck.check(re.search(r"gicr_typer\s*\|=\s*BIT\(4\)", vgic) is not None,
             "GICR_TYPER: LAST (bit 4) is still set, so software can end its walk")

    ck.section("Interrupt mask/affinity: guest INTIDs are translated to AIC numbers")
    ck.check(re.search(r"\bu32\s+guest_msi_aic_irq\s*\(\s*u32\s+intid\s*\)",
                       msi) is not None,
             "guest_msi.c: defines guest_msi_aic_irq()")
    ck.check(re.search(r"u32\s+guest_msi_aic_irq\s*\(\s*u32\s+intid\s*\)\s*;",
                       msi_h) is not None,
             "guest_msi.h: declares guest_msi_aic_irq()")

    helper = function_body(msi, "guest_msi_aic_irq")
    ck.check("GUEST_MSI_SPI_BASE" in helper and "GUEST_MSI_SPI_END" in helper,
             "guest_msi_aic_irq(): maps the MSI window by its SPI bounds")
    ck.check("guest_msi.aic_base" in helper,
             "guest_msi_aic_irq(): the MSI window resolves to guest_msi.aic_base + index, "
             "not to the INTID itself")
    ck.check(re.search(r"intid\s*>=\s*32\b", helper) is not None,
             "guest_msi_aic_irq(): INTIDs 0-31 (SGI/PPI) have no AIC line")

    for name, text in (("hv_vgic.c", vgic), ("hv_exc.c", exc)):
        args = mask_calls(text)
        ck.check(bool(args) and all(arg == "aic_irq" for arg in args),
                 f"{name}: every aic_set_mask() call passes a translated AIC number "
                 f"(found {sorted(set(args)) or 'none'})")

    isen = slice_after(vgic, "relative_addr >= GIC_DIST_ISENABLER0")
    icen = slice_after(vgic, "relative_addr >= GIC_DIST_ICENABLER0")
    ck.check("aic_set_mask(aic_irq, false)" in strip_ws(isen),
             "GICD_ISENABLER: enabling an INTID unmasks its AIC source (false)")
    ck.check("aic_set_mask(aic_irq, true)" in strip_ws(icen),
             "GICD_ICENABLER: disabling an INTID masks its AIC source (true), "
             "not unmasks it")

    router = case_block(dist, "case GIC_DIST_IROUTER32 ... GIC_DIST_IROUTER1019:")
    ck.check(re.search(r"\bint\s+cpu_num\b", router) is not None,
             "GICD_IROUTER: cpu_num is signed, so smp_get_id()'s -1 stays -1")
    ck.check(re.search(r"cpu_num\s*>=\s*0", router) is not None,
             "GICD_IROUTER: an affinity that is not in the ADT is skipped "
             "(BIT(-1) is undefined)")
    ck.check(re.search(r"guest_msi_aic_irq\s*\(\s*reg_num\s*\+\s*32\s*\)", router) is not None,
             "GICD_IROUTER: the routed INTID is translated before aic_set_affinity()")
    affinity_args = affinity_calls(vgic)
    ck.check(bool(affinity_args) and all(arg == "aic_irq" for arg in affinity_args),
             "hv_vgic.c: every aic_set_affinity() call passes a translated AIC number "
             f"(found {sorted(set(affinity_args)) or 'none'})")


# --------------------------------------------------------------------------
# The same contract, expressed against the shipped patch series.
#
# These are deliberately about the diff, not about prose: `adds` proves the fix
# is present as new code, `removes` proves the buggy line was taken out rather
# than shadowed, and `forbids` applies to every patch so a later one cannot put
# the bug back.
# --------------------------------------------------------------------------

#: (file, regex, description)
MUST_ADD = (
    ("src/hv_vgic.h", r"#define\s+GICR_FRAME_SIZE\s+0x20000",
     "GICR_FRAME_SIZE is defined as 0x20000"),
    ("src/hv_vgic.h", r"#define\s+GICR_WAKER_PROCESSOR_SLEEP\s+BIT\(1\)",
     "GICR_WAKER.ProcessorSleep is bit 1"),
    ("src/hv_vgic.h", r"#define\s+GICR_WAKER_CHILDREN_ASLEEP\s+BIT\(2\)",
     "GICR_WAKER.ChildrenAsleep is bit 2"),
    ("src/hv_vgic.c", r"frame\s*=\s*frame_offset\s*/\s*GICR_FRAME_SIZE",
     "the redistributor frame index comes from the address"),
    ("src/hv_vgic.c", r"relative_addr\s*=\s*frame_offset\s*%\s*GICR_FRAME_SIZE",
     "the register offset is taken within the frame"),
    ("src/hv_vgic.c", r"if\s*\(\s*frame\s*>=\s*num_cpus\s*\)",
     "an out-of-range redistributor frame is rejected"),
    ("src/hv_vgic.c", r"cpu_num\s*=\s*\(\s*u8\s*\)\s*frame\s*;",
     "cpu_num is the frame index, not the faulting CPU"),
    ("src/hv_vgic.c", r"gicd_ctlr_new_val\s*&=\s*~BIT\(31\)",
     "GICD_CTLR.RWP is cleared, so a polling guest makes progress"),
    ("src/hv_vgic.c", r"gicr_ctlr_new_val\s*&=\s*~BIT\(3\)",
     "GICR_CTLR.RWP (bit 3) is cleared"),
    ("src/hv_vgic.c", r"if\s*\(\s*\*val\s*&\s*GICR_WAKER_PROCESSOR_SLEEP\s*\)",
     "GICR_WAKER decodes ProcessorSleep before deciding the sleep state"),
    ("src/hv_vgic.c", r"gicr_wake_reg\s*=\s*0\s*;",
     "clearing ProcessorSleep clears ChildrenAsleep immediately"),
    ("src/hv_vgic.c", r"aic_set_mask\s*\(\s*aic_irq\s*,\s*true\s*\)",
     "GICD_ICENABLER masks the AIC source"),
    ("src/hv_vgic.c", r"aic_set_mask\s*\(\s*aic_irq\s*,\s*false\s*\)",
     "GICD_ISENABLER unmasks the AIC source"),
    ("src/hv_vgic.c", r"guest_msi_aic_irq\s*\(\s*reg_num\s*\+\s*32\s*\)",
     "GICD_IROUTER translates the INTID before touching the AIC"),
    ("src/hv_vgic.c", r"cpu_num\s*>=\s*0",
     "GICD_IROUTER rejects an affinity that smp_get_id() cannot resolve"),
    ("src/hv_vgic.c", r"int\s+cpu_num\s*;",
     "GICD_IROUTER keeps cpu_num signed so -1 survives"),
    ("src/hv_vgic.c", r"hv_map_hook\s*\(\s*redist_base\s*,\s*handle_vgic_redist_access\s*,\s*"
                      r"\(?\s*GICR_FRAME_SIZE\s*\*\s*num_cpus",
     "the redistributor hook covers every frame"),
    ("src/hv_exc.c", r"guest_msi_aic_irq\s*\(\s*intd\s*\)",
     "the LR-EOI path translates the INTID before unmasking"),
    ("src/guest_msi.c", r"u32\s+guest_msi_aic_irq\s*\(\s*u32\s+intid\s*\)",
     "guest_msi_aic_irq() is the inverse of guest_msi_intid()"),
    ("src/guest_msi.h", r"u32\s+guest_msi_aic_irq\s*\(\s*u32\s+intid\s*\)\s*;",
     "guest_msi_aic_irq() is declared for the vGIC and exception paths"),
)

MUST_REMOVE = (
    ("src/hv_vgic.c", r"cpu_num\s*=\s*ctx->cpu_id\s*;",
     "the redistributor no longer keys state off the faulting CPU"),
    ("src/hv_vgic.c", r"gicr_wake_reg\s*=\s*\*val\s*;",
     "GICR_WAKER no longer stores the write verbatim"),
    ("src/hv_vgic.c", r"gicd_ctl_reg\s*\|=\s*BIT\(31\)\s*;",
     "no path leaves GICD_CTLR.RWP set"),
    ("src/hv_vgic.c", r"gicr_ctlr_new_val\s*\|=\s*BIT\(31\)\s*;",
     "GICR_CTLR no longer writes the RES0 UWP bit"),
    ("src/hv_vgic.c", r"gicr_typer\s*\|=\s*BIT\(5\)\s*;",
     "GICR_TYPER no longer advertises DPGS"),
    ("src/hv_vgic.c", r"gicr_typer\s*\|=\s*BIT\(3\)\s*;",
     "GICR_TYPER no longer advertises DirectLPI"),
    ("src/hv_vgic.c", r"gicr_typer\s*\|=\s*BIT\(0\)\s*;",
     "GICR_TYPER no longer advertises PLPIS"),
    ("src/hv_vgic.c", r"aic_set_mask\s*\(\s*irq_num\s*,\s*false\s*\)\s*;",
     "no path passes a raw guest INTID to aic_set_mask()"),
    ("src/hv_vgic.c", r"\[AIC\]: masking irq %d\\n",
     "GICD_ICENABLER no longer routes through the unmask path"),
    ("src/hv_vgic.c", r"aic_set_affinity\s*\(\s*reg_num\s*\+\s*32\s*,\s*cpu_num\s*\)\s*;",
     "GICD_IROUTER no longer routes a guest INTID as an AIC number"),
    ("src/hv_vgic.c", r"BIT\(17\)\s*\|\s*BIT\(4\)|BIT\(19\)\s*\|\s*BIT\(17\)",
     "GICD_TYPER no longer advertises LPIs"),
    ("src/hv_exc.c", r"aic_set_mask\s*\(\s*intd\s*,\s*false\s*\)",
     "the LR-EOI path no longer passes a guest INTID to the AIC"),
)

MUST_NOT_ADD = (
    ("src/hv_vgic.c", r"cpu_num\s*=\s*ctx->cpu_id",
     "the redistributor must not key state off the faulting CPU"),
    ("src/hv_vgic.c", r"gicd_ctl_reg\s*\|=\s*BIT\(31\)",
     "GICD_CTLR.RWP must not be left set"),
    ("src/hv_vgic.c", r"gicr_ctlr_new_val\s*\|=\s*BIT\(31\)",
     "GICR_CTLR bit 31 (UWP) is RES0 and must not be written"),
    ("src/hv_vgic.c", r"is_rwp_to_be_set|is_uwp_to_be_set",
     "deferred RWP bookkeeping makes a polling guest hang"),
    ("src/hv_vgic.c", r"gicr_typer\s*\|=\s*BIT\([035]\)\s*;",
     "GICR_TYPER must not advertise PLPIS, DirectLPI or DPGS"),
    ("src/hv_vgic.c", r"gicd_type_reg\s*=\s*[^;]*BIT\(17\)",
     "GICD_TYPER.LPIS must not be advertised without an ITS"),
    ("src/hv_vgic.c", r"aic_set_mask\s*\(\s*(irq_num|intd|reg_num)\b",
     "aic_set_mask() takes an AIC number, never a raw guest INTID"),
    ("src/hv_vgic.c", r"aic_set_affinity\s*\(\s*(irq_num|intd|reg_num)\b",
     "aic_set_affinity() takes an AIC number, never a raw guest INTID"),
    ("src/hv_exc.c", r"aic_set_mask\s*\(\s*intd\b",
     "aic_set_mask() takes an AIC number, never a raw guest INTID"),
)


def code_only(line: str) -> str:
    """An added/removed line with comments removed.

    The patches explain themselves, and the explanations name the bugs they
    replaced ("the previous code set BIT(31)").  Matching code only is what keeps
    a forbid-rule from firing on the comment that documents the fix.
    """
    stripped = line.strip()
    if stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
        return ""
    stripped = re.sub(r"/\*.*?\*/", "", stripped)
    stripped = re.sub(r"//.*$", "", stripped)
    return stripped


def read_series(patch_dir: str, ck: Checker) -> dict[str, dict[str, list[tuple[str, str]]]] | None:
    """Parse every patch into {file: {'add': [(patch, line)], 'del': [...]}}."""
    paths = sorted(glob.glob(os.path.join(patch_dir, "*.patch")))
    if not paths:
        ck.check(False, f"no patches found in {patch_dir}")
        return None

    files: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for path in paths:
        name = os.path.basename(path)
        current = None
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.rstrip("\n")
                if line.startswith("diff --git "):
                    match = re.match(r"diff --git a/(\S+) b/(\S+)", line)
                    current = match.group(2) if match else None
                    if current:
                        files.setdefault(current, {"add": [], "del": []})
                    continue
                if current is None:
                    continue
                if line.startswith("+") and not line.startswith("+++"):
                    code = code_only(line[1:])
                    if code:
                        files[current]["add"].append((name, code))
                elif line.startswith("-") and not line.startswith("---"):
                    code = code_only(line[1:])
                    if code:
                        files[current]["del"].append((name, code))
    return files


def check_patches(ck: Checker, patch_dir: str) -> None:
    files = read_series(patch_dir, ck)
    if files is None:
        return

    def find(kind: str, rel: str, pattern: str) -> list[tuple[str, str]]:
        regex = re.compile(pattern)
        return [entry for entry in files.get(rel, {}).get(kind, [])
                if regex.search(entry[1])]

    ck.section("The series adds the guest-contract fixes")
    for rel, pattern, description in MUST_ADD:
        hits = find("add", rel, pattern)
        ck.check(bool(hits), f"{rel}: {description}"
                             + (f" [{hits[0][0]}]" if hits else ""))

    ck.section("The series removes the defects it claims to")
    for rel, pattern, description in MUST_REMOVE:
        hits = find("del", rel, pattern)
        ck.check(bool(hits), f"{rel}: {description}"
                             + (f" [{hits[0][0]}]" if hits else ""))

    ck.section("No patch reintroduces a defect")
    for rel, pattern, description in MUST_NOT_ADD:
        hits = find("add", rel, pattern)
        where = ", ".join(sorted({name for name, _ in hits}))
        ck.check(not hits, f"{rel}: {description}" + (f" (added by {where})" if hits else ""))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--patches", metavar="DIR", default=None,
                        help=f"verify a cumulative patch series (default: {DEFAULT_PATCHES})")
    parser.add_argument("--tree", metavar="PATH", default=None,
                        help=f"verify materialised sources (for example {DEFAULT_TREE})")
    args = parser.parse_args()

    ck = Checker()
    if args.tree:
        print(f"vGIC contract: tree {args.tree}")
        check_tree(ck, args.tree)
    else:
        patch_dir = args.patches or DEFAULT_PATCHES
        print(f"vGIC contract: patch series {patch_dir}")
        check_patches(ck, patch_dir)

    print()
    if ck.failures:
        print(f"vGIC contract: FAILED ({len(ck.failures)} of {ck.passes + len(ck.failures)} checks)")
        return 1
    print(f"vGIC contract: OK ({ck.passes} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
