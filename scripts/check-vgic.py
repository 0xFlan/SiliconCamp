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
    "src/hv.h",
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
        elif c == '"':
            #
            # String literals are blanked: prose in them ("guest attempted to
            # write RES0 bits") must never satisfy a code check.  Character
            # literals are deliberately *not* blanked, because this file's code
            # uses them to name things -- vgic_diag_record('D', ...) -- and a
            # checker that cannot see the 'D' cannot tell which address space a
            # trace call belongs to.  A char literal cannot contain a brace or a
            # comment delimiter, so leaving it intact cannot unbalance the
            # brace matching below.
            #
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
    hv_h = sources["src/hv.h"]
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
        ck.check("BIT(25)" in typer_line.group(1),
                 "GICD_TYPER: No1N (bit 25) is 1; the distributor does not perform "
                 "1-of-N selection, so the guest must not ask for it")
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
    ck.check(re.search(r"#define\s+GICD_IROUTER_IRM\s+BIT\(31\)", vgic_h) is not None,
             "hv_vgic.h: GICD_IROUTER_IRM names bit 31, the bit No1N makes meaningless")
    ck.check(re.search(r"route\s*&=\s*~\s*GICD_IROUTER_IRM", router) is not None,
             "GICD_IROUTER: IRM=1 is treated as 0, a behaviour No1N explicitly permits")
    ck.check(re.search(r"gicd_interrupt_router_regs\s*\[\s*reg_num\s*\]\s*=\s*route\s*;",
                       router) is not None,
             "GICD_IROUTER: the stored value never contains IRM, so read-back "
             "cannot report a mode the distributor will not perform")
    ck.check(re.search(r"MPIDR_AFF[0-3]\s*\(\s*route\s*\)", router) is not None,
             "GICD_IROUTER: the affinity is decoded from the normalised value, "
             "not the raw write")

    ck.section("Enable state: a disabled interrupt is not delivered (patch 0007)")
    enabled = function_body(vgic, "hv_vgic3_irq_enabled")
    ck.check(bool(enabled), "hv_vgic.c: found hv_vgic3_irq_enabled()")
    ck.check(re.search(r"gicd_ctl_reg\s*&\s*BIT\(0\)", enabled) is not None,
             "irq_enabled(): GICD_CTLR.EnableGrp1 gates delivery, because every LR "
             "is injected as Group 1")
    ck.check(re.search(r"gicr_isenabler0\s*&\s*BIT\(", enabled) is not None,
             "irq_enabled(): an SGI/PPI is gated by GICR_ISENABLER0")
    ck.check(re.search(r"gicd_interrupt_set_enable_regs\s*\[\s*bank\s*\]\s*&\s*BIT\(",
                       enabled) is not None,
             "irq_enabled(): an SPI is gated by its GICD_ISENABLER bank")
    ck.check(re.search(r"smp_id\s*\(\s*\)", enabled) is not None,
             "irq_enabled(): the SGI/PPI enable bit is read from the taking PE's "
             "own redistributor frame")
    inject = function_body(vgic, "hv_vgic3_inject_irq")
    ck.check("hv_vgic3_irq_enabled" in inject,
             "inject(): consults the guest's enable state before writing a list register")
    ck.check("free_lr < 0" in inject and re.search(r"return\s+false\s*;", inject) is not None,
             "inject(): reports failure when disabled or out of list registers, so the "
             "caller can keep the interrupt queued")
    ck.check(exc.count("virq_queue_push_unique") >= 4,
             "hv_exc.c: a repeated timer tick, SGI or MSI does not enqueue a second "
             "copy of an INTID that is already pending")
    ck.check("virq_queue_push_unique" in vgic_h,
             "hv_vgic.h: the queue can answer whether an INTID is already pending")
    push_unique = function_body(vgic_h, "virq_queue_push_unique")
    ck.check("spin_lock" in push_unique and "spin_unlock" in push_unique,
             "virq_queue_push_unique(): the dedupe test and the insert share one "
             "critical section, so two CPUs cannot both enqueue the same INTID")
    ck.check("virq_queue_push_locked" in push_unique,
             "virq_queue_push_unique(): the insert goes through the bounded queue "
             "helper rather than a second copy of the ring arithmetic")

    ck.section("Pending state survives a disable, and enabling delivers it (patch 0007)")
    drain = function_body(exc, "hv_vgic3_drain_queue")
    ck.check(bool(drain), "hv_exc.c: found hv_vgic3_drain_queue()")
    ck.check("hv_vgic3_inject_irq" in drain,
             "drain_queue(): delivers through the gated injection API")
    ck.check("break" not in drain,
             "drain_queue(): a disabled entry does not stop the pass; stopping at "
             "the head lets one disabled INTID block every interrupt behind it")
    ck.check("kept" in drain and
             re.search(r"__atomic_store_n\s*\(\s*&\s*q->head", drain) is not None,
             "drain_queue(): the queue is compacted and its head republished, so an "
             "undeliverable entry survives in order")
    ck.check(re.search(r"spin_lock\s*\(\s*&\s*q->p_lock\s*\)", drain) is not None,
             "drain_queue(): compaction holds the queue lock against producers on "
             "other CPUs")
    drain_all = function_body(exc, "hv_vgic3_drain_pending")
    ck.check(drain_all.count("hv_vgic3_drain_queue") >= 3,
             "drain_pending(): drains the timer, SGI and device-interrupt queues")
    ck.check(re.search(r"void\s+hv_vgic3_drain_pending\s*\(\s*void\s*\)\s*;", hv_h) is not None,
             "hv.h: drain_pending() is declared for the vGIC register paths")

    ctlr = case_block(dist, "case GIC_DIST_CTLR:")
    ck.check("group1_enabled" in ctlr and "hv_vgic3_drain_pending" in ctlr,
             "GICD_CTLR: turning EnableGrp1 on drains what was waiting behind it")
    ck.check("newly_enabled" in dist and "hv_vgic3_drain_pending" in dist,
             "GICD_ISENABLER: enabling an SPI drains a queued interrupt for it")
    redist_enable = case_block(redist, "case GIC_REDIST_ISENABLER0:")
    ck.check("sgi_newly_enabled" in redist_enable and
             "hv_vgic3_drain_pending" in redist_enable,
             "GICR_ISENABLER0: enabling an SGI/PPI drains a queued interrupt for it")
    ck.check(re.search(r"^\s*bool\s+vgic_inited\s*;", vgic, re.MULTILINE) is not None,
             "hv_vgic.c: vgic_inited has external linkage, so the drain can tell "
             "when the distributor exists")

    ck.section("Redistributor SGI/PPI banks: set/clear pairs share state (Arm IHI 0069)")
    for case, field, alias, desc in (
        ("case GIC_REDIST_ISENABLER0:", "gicr_isenabler0", "gicr_icenabler0",
         "ISENABLER0 sets"),
        ("case GIC_REDIST_ICENABLER0:", "gicr_isenabler0", "gicr_icenabler0",
         "ICENABLER0 clears"),
        ("case GIC_REDIST_ISPENDR0:", "gicr_ispendr0", "gicr_icpendr0",
         "ISPENDR0 sets"),
        ("case GIC_REDIST_ICPENDR0:", "gicr_ispendr0", "gicr_icpendr0",
         "ICPENDR0 clears"),
        ("case GIC_REDIST_ISACTIVER0:", "gicr_isactiver0", "gicr_icactiver0",
         "ISACTIVER0 sets"),
        ("case GIC_REDIST_ICACTIVER0:", "gicr_isactiver0", "gicr_icactiver0",
         "ICACTIVER0 clears"),
    ):
        block = case_block(redist, case)
        ck.check(bool(block), f"hv_vgic.c: found {case}")
        ck.check(re.search(field + r"\s*(?:\|=|&=\s*~)\s*\*val", block) is not None,
                 f"{desc} its own bank with write-1-to-set/clear semantics")
        ck.check(re.search(alias + r"\s*=\s*\n?\s*redistributors\[", block) is not None,
                 f"{case} mirrors the state into {alias}, which reads the same bitmap")
    ck.check("gicr_isactiver0 = value_is_enabler" not in vgic,
             "redistributor: enable and pending writes do not land in the active bank "
             "(the pre-0007 defect)")
    ck.check(re.search(r"\*val\s*=\s*distributor->gicd_interrupt_set_pending_regs\s*\[\s*reg_num\s*\]",
                       dist) is not None,
             "GICD_ISPENDR: a read returns the pending bitmap instead of leaving *val untouched")
    ck.check("gicd_interrupt_group_regs[i] = 0xFFFFFFFF" in vgic,
             "GICD_IGROUPR: modelled SPIs reset to Group 1, the only group the delivery "
             "path implements")

    ck.section("Diagnostics: the hot path never writes to the UART")
    ck.check(re.search(r"#define\s+ENABLE_VGIC_LOGGING\s+0\b", vgic) is not None,
             "hv_vgic.c: ENABLE_VGIC_LOGGING is 0, so vgic_log() is compiled out")
    ck.check(re.search(r"\[Written\]", vgic) is None,
             "hv_vgic.c: the per-access '[Written]' trace is gone, not merely disabled")
    ck.check(re.search(r"\bstatic\s+void\s+vgic_diag_record\s*\(", vgic) is not None,
             "hv_vgic.c: the hot path records into vgic_diag_record()")
    for name, space in (("handle_vgic_dist_access", "D"),
                        ("handle_vgic_redist_access", "R"),
                        ("handle_vgic_its_access", "I")):
        body = function_body(vgic, name)
        ck.check(re.search(r"vgic_diag_record\s*\(\s*'%s'\s*," % space, body) is not None,
                 f"{name}(): records into the ring as space '{space}'")
        ck.check(re.search(r"\bprintf\s*\(", body) is None,
                 f"{name}(): no raw printf() in the per-access path "
                 f"(vgic_log() expands to nothing when ENABLE_VGIC_LOGGING is 0)")
    ck.check(re.search(r"vgic_diag_record\s*\(\s*'R'\s*,\s*\(\s*u8\s*\)\s*frame", redist) is not None,
             "handle_vgic_redist_access(): the trace carries the frame, so a hang names the vCPU")

    ck.section("Diagnostics: exceptional events are counted and budgeted")
    ck.check(re.search(r"#define\s+VGIC_WARN_BUDGET\s+[1-9]\d*", vgic) is not None,
             "hv_vgic.c: VGIC_WARN_BUDGET bounds how much a broken guest can print")
    wants = function_body(vgic, "vgic_diag_wants_output")
    ck.check("VGIC_WARN_BUDGET" in wants and "suppressed" in wants,
             "vgic_diag_wants_output(): past the budget it counts the line and stays quiet")
    for kind in ("UNKNOWN", "RO", "UNMODELLED"):
        ck.check(re.search(r"vgic_note\s*\(\s*VGIC_NOTE_%s\s*," % kind, vgic) is not None,
                 f"hv_vgic.c: VGIC_NOTE_{kind} is reachable from a call site")
    loud = [m.group(0) for m in re.finditer(r"vgic_log\s*\([^;]*", vgic)
            if re.search(r"\[WARN\]|\[ERROR\]|\[ERR\]", m.group(0))]
    ck.check(not loud,
             "hv_vgic.c: every [WARN]/[ERROR] site is a budgeted vgic_note(), not a vgic_log()"
             + (f" (found {len(loud)} unbudgeted)" if loud else ""))

    ck.section("Diagnostics: the state survives a hang and can be read over the proxy")
    ck.check(re.search(r"^vgic_diag_t\s+vgic_diag\s*;", vgic, re.M) is not None,
             "hv_vgic.c: vgic_diag is a global, so its address is a symbol the proxy can resolve")
    record = function_body(vgic, "vgic_diag_record")
    ck.check("VGIC_RING_SIZE" in record,
             "vgic_diag_record(): the ring is a fixed-size array, not an allocation")
    ck.check(re.search(r"count\s*<\s*VGIC_RING_SIZE", record) is not None,
             "vgic_diag_record(): count saturates, so a full ring is not mistaken for a wrap")
    ck.check("__atomic_fetch_or" in function_body(vgic, "vgic_diag_touch"),
             "vgic_diag_touch(): the touched bitmaps use an atomic OR, so no CPU loses a bit")
    ck.check(re.search(r"redist_touched\s*\[\s*GICR_FRAME_SIZE", vgic) is not None,
             "vgic_diag: the redistributor bitmap is sized from GICR_FRAME_SIZE, "
             "so it cannot drift from the frame decode")


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
    # --- 0005: diagnostics ---------------------------------------------------
    ("src/hv_vgic.c", r"#define\s+ENABLE_VGIC_LOGGING\s+0\b",
     "the per-access trace is compiled out, so no UART write sits on the hot path"),
    ("src/hv_vgic.c", r"#define\s+VGIC_WARN_BUDGET\s+[1-9]\d*",
     "exception output is bounded by a budget"),
    ("src/hv_vgic.c", r"^vgic_diag_t\s+vgic_diag\s*;",
     "vgic_diag is a global symbol, readable over the proxy after a hang"),
    ("src/hv_vgic.c", r"vgic_diag_record\s*\(\s*'D'\s*,",
     "distributor accesses are recorded instead of printed"),
    ("src/hv_vgic.c", r"vgic_diag_record\s*\(\s*'R'\s*,\s*\(\s*u8\s*\)\s*frame\s*,",
     "redistributor accesses are recorded with their frame index"),
    ("src/hv_vgic.c", r"__atomic_fetch_or",
     "the 'was this register ever touched' bitmaps are updated atomically"),
    ("src/hv_vgic.c", r"vgic_note\s*\(\s*VGIC_NOTE_UNKNOWN\s*,",
     "an unknown register is counted and reported through the budget"),
    # --- 0006: honest SPI routing --------------------------------------------
    ("src/hv_vgic.h", r"#define\s+GICD_IROUTER_IRM\s+BIT\(31\)",
     "GICD_IROUTER_IRM names the routing-mode bit No1N makes meaningless"),
    ("src/hv_vgic.c", r"gicd_type_reg\s*=\s*[^;]*BIT\(25\)",
     "GICD_TYPER.No1N is set, so the guest is not invited to use 1-of-N routing"),
    ("src/hv_vgic.c", r"route\s*&=\s*~\s*GICD_IROUTER_IRM",
     "GICD_IROUTER.IRM=1 is treated as 0, consistent with No1N"),
    ("src/hv_vgic.c", r"gicd_interrupt_router_regs\s*\[\s*reg_num\s*\]\s*=\s*route\s*;",
     "the stored routing value never contains IRM, so read-back agrees with the model"),
    # --- 0007: enable-bit gating --------------------------------------------
    ("src/hv_vgic.h", r"bool\s+hv_vgic3_irq_enabled\s*\(\s*u32\s+vintid\s*\)",
     "the enable-state predicate is declared for the injection path"),
    ("src/hv_vgic.c", r"bool\s+hv_vgic3_irq_enabled\s*\(\s*u32\s+vintid\s*\)",
     "the enable-state predicate is implemented"),
    ("src/hv_vgic.c", r"gicd_ctl_reg\s*&\s*BIT\(0\)",
     "GICD_CTLR.EnableGrp1 gates delivery"),
    ("src/hv_vgic.c", r"gicr_isenabler0\s*\|=\s*\*val",
     "GICR_ISENABLER0 is write-1-to-set"),
    ("src/hv_vgic.c", r"gicr_isenabler0\s*&=\s*~\s*\*val",
     "GICR_ICENABLER0 clears the enable state it shares with ISENABLER0"),
    ("src/hv_vgic.c", r"\*val\s*=\s*distributor->gicd_interrupt_set_pending_regs",
     "GICD_ISPENDR reads back the pending bitmap"),
    ("src/hv_vgic.c", r"gicd_interrupt_group_regs\[i\]\s*=\s*0xFFFFFFFF",
     "modelled SPIs report Group 1, the group delivery implements"),
    ("src/hv_exc.c", r"void\s+hv_vgic3_drain_queue\s*\(",
     "the drain compacts the queue instead of stopping at a disabled entry"),
    ("src/hv_exc.c", r"void\s+hv_vgic3_drain_pending\s*\(\s*void\s*\)",
     "all three per-CPU queues have one drain entry point"),
    ("src/hv.h", r"void\s+hv_vgic3_drain_pending\s*\(\s*void\s*\)\s*;",
     "drain_pending() is declared for the vGIC register paths"),
    ("src/hv_vgic.c", r"if\s*\(\s*newly_enabled\s*\)",
     "enabling an SPI drains an interrupt that arrived while it was disabled"),
    ("src/hv_vgic.c", r"if\s*\(\s*sgi_newly_enabled\s*\)",
     "enabling an SGI/PPI drains an interrupt that arrived while it was disabled"),
    ("src/hv_vgic.c", r"if\s*\(\s*group1_enabled\s*\)",
     "enabling Group 1 drains everything that was waiting behind it"),
    ("src/hv_vgic.c", r"^\s*bool\s+vgic_inited\s*;",
     "vgic_inited is linkable from hv_exc.c, where the drain gates on it"),
    ("src/hv_exc.c", r"if\s*\(\s*!\s*hv_vgic3_inject_irq",
     "every injection site handles a refusal by keeping the interrupt queued"),
    ("src/hv_vgic.h", r"bool\s+virq_queue_push_unique\s*\(",
     "the queue can report an INTID that is already pending"),
    ("src/hv_exc.c", r"virq_queue_push_unique\s*\(\s*&PERCPU",
     "a refused interrupt is queued once per INTID, not once per retry"),
    ("src/hv_vgic.c", r"if\s*\(\s*!\s*hv_vgic3_irq_enabled\s*\(\s*vintid\s*\)\s*\)",
     "injection itself refuses an INTID the guest has not enabled"),
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
    # --- 0005: diagnostics ---------------------------------------------------
    ("src/hv_vgic.c", r"#define\s+ENABLE_VGIC_LOGGING\s+1\b",
     "the per-access trace is no longer enabled by default"),
    ("src/hv_vgic.c", r"\[Written\]",
     "the per-access '[Written]' trace lines are gone"),
    # --- 0007: enable-bit gating --------------------------------------------
    ("src/hv_vgic.c", r"redistributors\[cpu_num\]\.sgi_region\.gicr_isactiver0\s*=\s*"
                      r"value_is_enabler\s*;",
     "enable and pending writes no longer land in the active bank"),
    ("src/hv_vgic.c", r"redistributors\[cpu_num\]\.sgi_region\.gicr_isenabler0\s*=\s*\*val\s*;",
     "GICR_ISENABLER0 no longer stores the raw write, where a 0 would clear it"),
    ("src/hv_vgic.c", r"^distributor->gicd_interrupt_set_pending_regs\[reg_num\];$",
     "a GICD_ISPENDR read no longer discards the bitmap"),
    ("src/hv_exc.c", r"if\s*\(\s*hv_vgic3_get_free_lr\(\)\s*!=\s*-1\s*\)",
     "injection no longer depends on list-register availability alone"),
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
    ("src/hv_vgic.c", r"#define\s+ENABLE_VGIC_LOGGING\s+1\b",
     "no patch may turn the per-access UART trace back on"),
    ("src/hv_vgic.c", r'vgic_log\s*\(\s*"HV vGIC \[(WARN|ERR)',
     "an exceptional event must go through the budgeted vgic_note()"),
    ("src/hv_vgic.c", r"gicr_isactiver0\s*=\s*value_is_enabler",
     "enable and pending state must not be maintained in the active bank"),
    ("src/hv_vgic.c", r"redistributors\[cpu_num\]\.sgi_region\.gicr_isenabler0\s*=\s*\*val",
     "GICR_ISENABLER0 is write-1-to-set, never a raw store"),
)


def code_only(line: str) -> str:
    """An added/removed line with comments removed.

    The patches explain themselves, and the explanations name the bugs they
    replaced ("the previous code set BIT(31)").  Matching code only is what keeps
    a forbid-rule from firing on the comment that documents the fix.
    """
    stripped = line.strip()
    #
    # A block-comment continuation starts with "* " or "*/".  A line that
    # starts with "*val" is a dereference, not a comment: treating every
    # leading "*" as prose silently dropped the added "*val = ..." lines from
    # the parsed series, which made a real contract check unfalsifiable.
    #
    if (stripped.startswith("//") or stripped.startswith("/*")
            or re.match(r"^\*(?:\s|/)", stripped)):
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
