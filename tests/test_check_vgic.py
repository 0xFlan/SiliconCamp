#!/usr/bin/env python3
"""Regression tests for scripts/check-vgic.py.

The checker's job is to fail when the m1n1-windows vGIC stops honouring the
GICv3 contract Windows relies on, so every test here is either "the shipped
artifact passes" or "this specific defect is rejected". The defects are the ones
this project actually shipped: a redistributor keyed off the faulting CPU
instead of the frame address, a GICR_WAKER write stored verbatim, RWP left set,
LPI capabilities advertised without an ITS, and an inverted AIC mask polarity.

Two kinds of subject:

  * the patch series, which is the shipped artifact and needs no submodule, and
  * the patched source tree, built here in a throwaway git worktree, which is
    what a contributor edits before regenerating a patch.

The tree tests skip when the submodule is not initialised, the same way the ANS2
tests skip without the firmware tree.

Run with: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
CHECKER = os.path.join(ROOT, "scripts", "check-vgic.py")
PATCH_DIR = os.path.join(ROOT, "patches", "m1n1-windows")
M1N1 = os.path.join(ROOT, "third_party", "m1n1-windows")

#: The files the checker reads; a mutated copy only needs these.
SOURCES = (
    "src/hv_vgic.c",
    "src/hv_vgic.h",
    "src/hv_exc.c",
    "src/hv.h",
    "src/guest_msi.c",
    "src/guest_msi.h",
)


def run_checker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, CHECKER, *args],
        capture_output=True,
        text=True,
        check=False,
    )


class PatchSeriesTests(unittest.TestCase):
    """The series as shipped, and deliberately careless regenerations of it."""

    def test_series_satisfies_the_contract(self) -> None:
        result = run_checker("--patches", PATCH_DIR)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("vGIC contract: OK", result.stdout)

    def _mutated_series(self, filename: str, old: str, new: str) -> str:
        """Copy the series, replacing one string in one patch, and return the dir."""
        tmp = tempfile.mkdtemp(prefix="vgic-patches-")
        self.addCleanup(shutil.rmtree, tmp, True)
        for name in os.listdir(PATCH_DIR):
            if not name.endswith(".patch"):
                continue
            with open(os.path.join(PATCH_DIR, name), "r", encoding="utf-8") as handle:
                text = handle.read()
            if name == filename:
                self.assertIn(old, text, f"fixture drifted: {old!r} not in {filename}")
                text = text.replace(old, new)
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as handle:
                handle.write(text)
        return tmp

    def test_series_that_stops_clearing_rwp_is_rejected(self) -> None:
        # RWP is read-only and must read 0 once the write has been applied. A
        # guest that polls it would hang, which is a silent boot stall.
        series = self._mutated_series(
            "0004-vgic-guest-contract.patch",
            "gicd_ctlr_new_val &= ~BIT(31);",
            "gicd_ctlr_new_val |= BIT(31);",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL", result.stdout)

    def test_series_without_frame_decode_is_rejected(self) -> None:
        series = self._mutated_series(
            "0004-vgic-guest-contract.patch",
            "relative_addr = frame_offset % GICR_FRAME_SIZE;",
            "relative_addr = addr - redist_base;",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)

    def test_series_that_readvertises_lpis_is_rejected(self) -> None:
        # Nothing implements an ITS, so an LPIS bit sends the guest looking for
        # command queues that are not there.
        series = self._mutated_series(
            "0004-vgic-guest-contract.patch",
            "distributor->gicd_type_reg = (BIT(22) | BIT(21) | BIT(20) | BIT(19) | BIT(4)",
            "distributor->gicd_type_reg = (BIT(22) | BIT(21) | BIT(20) | BIT(19) | BIT(17) | BIT(4)",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)

    def test_series_that_turns_logging_back_on_is_rejected(self) -> None:
        # A printf() per emulated register access is not slow logging, it is a
        # hang: the guest programs thousands of registers and the console is a
        # UART, so the boot stalls in a way that looks like the bug it was
        # supposed to find.
        series = self._mutated_series(
            "0005-vgic-diagnostics.patch",
            "+#define ENABLE_VGIC_LOGGING 0",
            "+#define ENABLE_VGIC_LOGGING 1",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("UART trace", result.stdout)

    def test_series_without_a_distributor_trace_is_rejected(self) -> None:
        series = self._mutated_series(
            "0005-vgic-diagnostics.patch",
            "+    vgic_diag_record('D', 0, relative_addr, *val, write,",
            "+    (void)relative_addr; (void)val; (void)write;",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("distributor accesses are recorded", result.stdout)

    def test_series_without_no1n_is_rejected(self) -> None:
        # No1N=1 is the only thing stopping a guest from asking the distributor
        # to pick a target PE.  This vGIC has no such selection: on AICv3 the
        # AIC decides, not a register the vGIC can write.
        series = self._mutated_series(
            "0006-vgic-honest-spi-routing.patch",
            "+    distributor->gicd_type_reg = (BIT(25) | BIT(22)",
            "+    distributor->gicd_type_reg = (BIT(22)",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No1N", result.stdout)

    def test_series_that_accepts_irm_is_rejected(self) -> None:
        # With No1N set, IRM=1 is CONSTRAINED UNPREDICTABLE.  Acting on it as
        # though it meant something would route a guest that read No1N anyway,
        # and storing it would make read-back claim a mode that never happens.
        series = self._mutated_series(
            "0006-vgic-honest-spi-routing.patch",
            "+                    route &= ~GICD_IROUTER_IRM;",
            "+                    route = route;",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("IRM=1 is treated as 0", result.stdout)

    def test_series_that_unmasks_on_disable_is_rejected(self) -> None:
        # A raw guest INTID at the AIC means SPI 512 masks AIC 512, not the
        # APCIE source at 1506 that actually raises it.
        series = self._mutated_series(
            "0004-vgic-guest-contract.patch",
            "aic_set_mask(aic_irq, true);",
            "aic_set_mask(irq_num, true);",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FAIL", result.stdout)


    def test_series_that_ignores_the_enable_bit_is_rejected(self) -> None:
        tmp = self._mutated_series(
            "0007-vgic-enable-gating.patch",
            "if (!hv_vgic3_irq_enabled(vintid))",
            "if (0)",
        )
        result = run_checker("--patches", tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refuses an INTID the guest has not enabled", result.stdout)

    def test_series_that_writes_enables_into_the_active_bank_is_rejected(self) -> None:
        tmp = self._mutated_series(
            "0007-vgic-enable-gating.patch",
            "gicr_isenabler0 |= *val;",
            "gicr_isactiver0 |= *val;",
        )
        result = run_checker("--patches", tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GICR_ISENABLER0 is write-1-to-set", result.stdout)

    def test_series_without_queue_deduplication_is_rejected(self) -> None:
        # Without this, every timer tick or MSI while the guest has the INTID
        # disabled enqueues another copy of the same vector.
        tmp = self._mutated_series(
            "0007-vgic-enable-gating.patch",
            "bool virq_queue_push_unique(",
            "bool virq_queue_push_dup(",
        )
        result = run_checker("--patches", tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("the queue can report an INTID", result.stdout)

    def test_series_that_never_drains_after_enabling_is_rejected(self) -> None:
        # Queuing an interrupt the guest has disabled is only half the fix.  If
        # nothing drains when the guest enables it, an edge-triggered source can
        # stay pending until some unrelated interrupt happens to arrive.
        tmp = self._mutated_series(
            "0007-vgic-enable-gating.patch",
            "if (newly_enabled)",
            "if (0)",
        )
        result = run_checker("--patches", tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("enabling an SPI drains an interrupt", result.stdout)

    def test_series_that_drops_the_pending_readback_is_rejected(self) -> None:
        tmp = self._mutated_series(
            "0007-vgic-enable-gating.patch",
            "*val = distributor->gicd_interrupt_set_pending_regs[reg_num];",
            "distributor->gicd_interrupt_set_pending_regs[reg_num];",
        )
        result = run_checker("--patches", tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GICD_ISPENDR reads back the pending bitmap", result.stdout)

    def test_series_that_clears_on_active_is_rejected(self) -> None:
        # GICD_ISACTIVER is write-1-to-set.  The pre-0008 handler cleared the
        # bit, so the firmware self-test's dist.active probe would fail on the
        # first hardware boot with code that had never been executed.
        tmp = self._mutated_series(
            "0008-vgic-distributor-active-bits.patch",
            "value_is_enabler |= BIT(i);",
            "value_is_enabler &= ~BIT(i);",
        )
        result = run_checker("--patches", tmp)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GICD_ISACTIVER sets the active bit", result.stdout)


class PatchedTreeTests(unittest.TestCase):
    """The checker's structural pass, run against real patched sources."""

    patched_tree: str | None = None

    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(os.path.join(M1N1, ".git")):
            raise unittest.SkipTest("m1n1-windows submodule is not initialised")

        cls.worktree_root = tempfile.mkdtemp(prefix="vgic-tree-")
        cls.patched_tree = os.path.join(cls.worktree_root, "tree")
        added = subprocess.run(
            ["git", "-C", M1N1, "worktree", "add", "--quiet", "--detach",
             cls.patched_tree, "HEAD"],
            capture_output=True, text=True, check=False,
        )
        if added.returncode != 0:
            shutil.rmtree(cls.worktree_root, True)
            raise unittest.SkipTest(f"cannot create a worktree: {added.stderr.strip()}")

        patches = sorted(
            os.path.join(PATCH_DIR, name)
            for name in os.listdir(PATCH_DIR)
            if name.endswith(".patch")
        )
        for patch in patches:
            applied = subprocess.run(
                ["git", "-C", cls.patched_tree, "apply", patch],
                capture_output=True, text=True, check=False,
            )
            if applied.returncode != 0:
                cls._teardown_tree()
                raise unittest.SkipTest(
                    f"{os.path.basename(patch)} does not apply: {applied.stderr.strip()}"
                )

    @classmethod
    def _teardown_tree(cls) -> None:
        if cls.patched_tree:
            subprocess.run(
                ["git", "-C", M1N1, "worktree", "remove", "--force", cls.patched_tree],
                capture_output=True, check=False,
            )
        if getattr(cls, "worktree_root", None):
            shutil.rmtree(cls.worktree_root, True)
        cls.patched_tree = None

    @classmethod
    def tearDownClass(cls) -> None:
        cls._teardown_tree()

    def _copy_tree(self) -> str:
        tmp = tempfile.mkdtemp(prefix="vgic-src-")
        self.addCleanup(shutil.rmtree, tmp, True)
        for rel in SOURCES:
            target = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copyfile(os.path.join(self.patched_tree, rel), target)
        return tmp

    def _mutate(self, tree: str, rel: str, old: str, new: str) -> None:
        path = os.path.join(tree, rel)
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn(old, text, f"fixture drifted: {old!r} not in {rel}")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text.replace(old, new))

    def test_patched_tree_satisfies_the_contract(self) -> None:
        result = run_checker("--tree", self.patched_tree)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("vGIC contract: OK", result.stdout)

    def test_redistributor_keyed_off_faulting_cpu_is_rejected(self) -> None:
        tree = self._copy_tree()
        self._mutate(tree, "src/hv_vgic.c",
                     "cpu_num = (u8)frame;", "cpu_num = ctx->cpu_id;")
        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not the CPU that took the fault", result.stdout)

    def test_tree_that_ignores_the_enable_bit_is_rejected(self) -> None:
        tree = self._copy_tree()
        self._mutate(tree, "src/hv_vgic.c",
                     "if (!hv_vgic3_irq_enabled(vintid))\n        return false;",
                     "if (0)\n        return false;")
        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("consults the guest's enable state", result.stdout)

    def test_tree_that_confuses_the_enable_and_active_banks_is_rejected(self) -> None:
        tree = self._copy_tree()
        self._mutate(tree, "src/hv_vgic.c",
                     "sgi_region.gicr_isenabler0 |= *val;",
                     "sgi_region.gicr_isactiver0 |= *val;")
        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ISENABLER0 sets its own bank", result.stdout)

    def test_tree_that_queues_a_copy_per_timer_tick_is_rejected(self) -> None:
        tree = self._copy_tree()
        self._mutate(tree, "src/hv_exc.c",
                     "virq_queue_push_unique(&PERCPU(timer_queue), &pending);",
                     "virq_queue_push(&PERCPU(timer_queue), &pending);")
        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not enqueue a second copy", result.stdout)

    def test_tree_that_never_drains_on_enable_is_rejected(self) -> None:
        tree = self._copy_tree()
        self._mutate(tree, "src/hv_vgic.c",
                     """if (sgi_newly_enabled)
                    hv_vgic3_drain_pending();""",
                     """if (sgi_newly_enabled)
                    (void)0;""")
        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("enabling an SGI/PPI drains a queued interrupt", result.stdout)

    def test_tree_with_a_lock_free_dedupe_scan_is_rejected(self) -> None:
        # Compaction moves entries in place; a lock-free scan can read a slot
        # mid-copy, report a false positive, and drop a real interrupt.
        tree = self._copy_tree()
        self._mutate(tree, "src/hv_vgic.h",
                     """    spin_lock(&q->p_lock);

    bool found = false;""",
                     """    bool found = false;""")
        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dedupe test and the insert share one critical section", result.stdout)

    def test_tree_that_stops_the_drain_at_a_disabled_entry_is_rejected(self) -> None:
        # The pre-drain code broke out of the queue loop on the first refused
        # injection, which would let one disabled INTID block every interrupt
        # behind it.
        tree = self._copy_tree()
        self._mutate(tree, "src/hv_exc.c",
                     "kept++;",
                     "break;")
        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not stop the pass", result.stdout)

    def test_tree_that_drops_the_pending_readback_is_rejected(self) -> None:
        tree = self._copy_tree()
        self._mutate(tree, "src/hv_vgic.c",
                     "*val = distributor->gicd_interrupt_set_pending_regs[reg_num];",
                     "distributor->gicd_interrupt_set_pending_regs[reg_num];")
        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("returns the pending bitmap", result.stdout)

    def test_tree_that_clears_on_isactiver_is_rejected(self) -> None:
        tree = self._copy_tree()
        self._mutate(tree, "src/hv_vgic.c",
                     """                if( (value_is_enabler & BIT(i)) != 0 )
                    continue;
                value_is_enabler |= BIT(i);""",
                     """                value_is_enabler &= ~BIT(i);""")
        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GICD_ISACTIVER is write-1-to-set", result.stdout)

    def test_verbatim_waker_store_is_rejected(self) -> None:
        # Storing the write verbatim leaves ChildrenAsleep set, so a conforming
        # wake sequence polls a bit that never clears.
        tree = self._copy_tree()
        self._mutate(tree, "src/hv_vgic.c",
                     """if (*val & GICR_WAKER_PROCESSOR_SLEEP)
                    redistributors[cpu_num].rd_region.gicr_wake_reg =
                        GICR_WAKER_PROCESSOR_SLEEP | GICR_WAKER_CHILDREN_ASLEEP;
                else
                    redistributors[cpu_num].rd_region.gicr_wake_reg = 0;""",
                     "redistributors[cpu_num].rd_region.gicr_wake_reg = *val;")
        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GICR_WAKER", result.stdout)

    def test_inverted_mask_polarity_is_rejected(self) -> None:
        tree = self._copy_tree()
        self._mutate(tree, "src/hv_vgic.c",
                     "aic_set_mask(aic_irq, true);",
                     "aic_set_mask(aic_irq, false);")
        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("GICD_ICENABLER", result.stdout)

    def test_untranslated_affinity_is_rejected(self) -> None:
        tree = self._copy_tree()
        self._mutate(tree, "src/hv_vgic.c",
                     "aic_set_affinity(aic_irq, cpu_num);",
                     "aic_set_affinity(reg_num + 32, cpu_num);")
        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)

    def test_tree_with_logging_on_is_rejected(self) -> None:
        tree = self._copy_tree()
        self._mutate(tree, "src/hv_vgic.c",
                     "#define ENABLE_VGIC_LOGGING 0",
                     "#define ENABLE_VGIC_LOGGING 1")
        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("compiled out", result.stdout)

    def test_tree_that_prints_instead_of_recording_is_rejected(self) -> None:
        tree = self._copy_tree()
        self._mutate(tree, "src/hv_vgic.c",
                     "    vgic_diag_record('D', 0, relative_addr, *val, write,",
                     "    printf(\"acc\\n\");\n    (void)relative_addr; (void)val; (void)write;\n    if (0) vgic_diag_record('D', 0, relative_addr, *val, write,")
        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no raw printf()", result.stdout)

    def test_tree_with_an_unbudgeted_warning_is_rejected(self) -> None:
        # The budget is the only thing standing between a guest that spins on a
        # missing register and an unbounded flood.
        tree = self._copy_tree()
        self._mutate(tree, "src/hv_vgic.c",
                     'vgic_note(VGIC_NOTE_UNKNOWN, "HV vGIC [ERR]',
                     'vgic_log(VGIC_NOTE_UNKNOWN, "HV vGIC [ERR]')
        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("budgeted vgic_note", result.stdout)

    def test_tree_without_no1n_is_rejected(self) -> None:
        tree = self._copy_tree()
        self._mutate(tree, "src/hv_vgic.c", "BIT(25) | BIT(22)", "BIT(22)")
        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No1N", result.stdout)

    def test_tree_that_keeps_irm_is_rejected(self) -> None:
        tree = self._copy_tree()
        self._mutate(tree, "src/hv_vgic.c",
                     "route &= ~GICD_IROUTER_IRM;", "(void)0;")
        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("IRM=1 is treated as 0", result.stdout)

    def test_unsigned_cpu_number_is_rejected(self) -> None:
        # smp_get_id() returns -1 for an affinity that is not in the ADT, and
        # BIT(-1) is undefined; the type has to keep the sign.
        tree = self._copy_tree()
        self._mutate(tree, "src/hv_vgic.c", "int cpu_num;", "u32 cpu_num;")
        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cpu_num", result.stdout)


if __name__ == "__main__":
    unittest.main()
