#!/usr/bin/env python3
"""Regression tests for scripts/check-vgic-selftest.py.

`AppleVgicSelfTestDxe` runs before the operating system and reads the emulated
interrupt controller back.  It is the only thing in the image that does, so its
failure modes are unusually expensive: a poll that never completes looks like a
boot that stops two lines into the log, and a read of an unmodelled
redistributor offset is a fatal guest abort rather than a wrong value.

Every test here is therefore either "the shipped artifact passes" or "this
specific way of breaking it is rejected".  The defects are the ones the driver
was written to avoid: an unbounded wait, a handler that does not write
ICC_EOIR1_EL1, a GICR_TYPER2 read (offset 0xC is the upper half of the 64-bit
GICR_TYPER), a doorbell write to an address that is not mapped into the guest,
and a GICR_WAKER write that reuses a stale read.

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
CHECKER = os.path.join(ROOT, "scripts", "check-vgic-selftest.py")
PATCH_DIR = os.path.join(ROOT, "patches", "ntasp-firmware")
FIRMWARE = os.path.join(ROOT, "third_party", "ntasp-firmware")
PATCH = "0003-vgic-selftest.patch"


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
        self.assertIn("vGIC self-test: OK", result.stdout)

    def _mutated_series(self, old: str, new: str) -> str:
        tmp = tempfile.mkdtemp(prefix="vgic-selftest-")
        self.addCleanup(shutil.rmtree, tmp, True)
        for name in os.listdir(PATCH_DIR):
            if not name.endswith(".patch"):
                continue
            with open(os.path.join(PATCH_DIR, name), "r", encoding="utf-8") as handle:
                text = handle.read()
            if name == PATCH:
                self.assertIn(old, text, f"fixture drifted: {old!r} not in {name}")
                text = text.replace(old, new)
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as handle:
                handle.write(text)
        return tmp

    def test_series_with_an_unbounded_wait_is_rejected(self) -> None:
        # A self-test that can hang is worse than no self-test: the symptom is
        # indistinguishable from the boot bug it was meant to find.
        series = self._mutated_series(
            "+  for (Elapsed = 0; Elapsed < TimeoutUs; Elapsed += 100) {",
            "+  while (TRUE) {",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no while loop", result.stdout)

    def test_series_with_an_unbounded_poll_without_a_timeout_is_rejected(self) -> None:
        series = self._mutated_series(
            "+  for (Elapsed = 0; Elapsed < TimeoutUs; Elapsed += 100) {",
            "+  for (Elapsed = 0; Elapsed >= 0; Elapsed += 100) {",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("bounded by TimeoutUs", result.stdout)

    def test_series_whose_handler_does_not_eoi_is_rejected(self) -> None:
        # ArmGicDxe only writes ICC_EOIR1_EL1 itself when no handler is
        # registered, so a handler that does not EOI leaves the list register
        # active and the second interrupt never arrives.
        series = self._mutated_series(
            "+    mInterrupt->EndOfInterrupt (mInterrupt, Source);",
            "+    (void)0;",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("EndOfInterrupt", result.stdout)

    def test_series_that_reads_an_unmodelled_register_is_rejected(self) -> None:
        # GICR_TYPER2 does not exist, and the redistributor hook does not
        # tolerate an unknown offset: the read is a fatal guest abort.
        series = self._mutated_series(
            "+#define GICR_TYPER   0x0008",
            "+#define GICR_TYPER   0x0008\n+#define GICR_TYPER2  0x000C\n+#define GICR_TYPER_2 (MmioRead32 (Base + GICR_TYPER2))",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("modelled set", result.stdout)

    def test_series_that_writes_the_doorbell_is_rejected(self) -> None:
        # 0xfffff000 is the MSI doorbell in the ADT, but it is not mapped into
        # the guest, so a write there is an unmapped-IPA abort.
        series = self._mutated_series(
            "+  Mpidr         = ArmReadMpidr ();",
            "+  MmioWrite32 (0xfffff000, 0);\n+  Mpidr         = ArmReadMpidr ();",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("doorbell", result.stdout)

    def test_series_that_keeps_irm_is_rejected(self) -> None:
        # Patch 0006 of the m1n1 series makes an IRM write read back as 0.  If
        # the self-test stopped requiring that, it would stop detecting the
        # regression the patch exists for.
        series = self._mutated_series(
            "+  if ((ReadBack & GICD_IROUTER_IRM) == 0) {",
            "+  if ((ReadBack & GICD_IROUTER_IRM) != 0) {",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("IRM", result.stdout)

    def test_series_with_a_stale_waker_write_is_rejected(self) -> None:
        # This is the bug patch 0004 of the m1n1 series fixed: writing a value
        # that was read while ProcessorSleep was still set re-sets it, and the
        # guest's ChildrenAsleep poll never finishes.
        series = self._mutated_series(
            "+    MmioWrite32 (Base + GICR_WAKER, Waker | GICR_WAKER_PROCESSOR_SLEEP);",
            "+    MmioWrite32 (Base + GICR_WAKER, GICR_WAKER_PROCESSOR_SLEEP);",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("read-modify-write", result.stdout)

    def test_series_without_the_aic_guard_is_rejected(self) -> None:
        # The AIC build has no vGIC at all, so a component that is not behind
        # the guard would be built into an image it cannot describe.
        series = self._mutated_series(
            "+!if $(AIC_BUILD) == FALSE\n",
            "+\n",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)

    def test_series_that_drops_a_pcd_declaration_is_rejected(self) -> None:
        series = self._mutated_series(
            "+  gArmTokenSpaceGuid.PcdArmArchTimerVirtIntrNum\n",
            "+\n",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("INF [Pcd] list", result.stdout)

    def test_series_that_uses_a_64_bit_format_is_rejected(self) -> None:
        # PrintLib has no %ll; it would print the literal text.
        series = self._mutated_series(
            '+    "vgic: dist = 0x%lx, redist = 0x%lx, %u frames declared, boot MPIDR 0x%lx\\n",',
            '+    "vgic: dist = 0x%llx, redist = 0x%llx, %u frames declared, boot MPIDR 0x%llx\\n",',
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("%ll", result.stdout)

    def test_series_without_the_hal_teardown_is_rejected(self) -> None:
        # The Windows HAL writes 0xFFFFFFFF to every GICD_ICENABLER bank.  A
        # replay that stopped doing it would no longer exercise the INTID->AIC
        # translation at full width, which is the point of the group.
        series = self._mutated_series(
            "+    MmioWrite32 (Dist + GICD_ICENABLER + 4 * (FirstBank + Index), 0xFFFFFFFF);",
            "+    (void)0;",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("0xFFFFFFFF teardown", result.stdout)

    def test_series_that_leaves_the_hal_replay_unrestored_is_rejected(self) -> None:
        series = self._mutated_series(
            "+  MmioWrite32 (Dist + GICD_CTLR, SavedCtlr);",
            "+  (void)0;",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restored from the value that was read", result.stdout)

    def test_series_that_never_re_enables_the_gated_sgi_is_rejected(self) -> None:
        # Leaving the bit clear after the negative test would make a later
        # driver's SGI silently undeliverable, and the test would still pass.
        series = self._mutated_series(
            "+  MmioWrite32 (BootFrameBase + GICR_SGI_ISENABLER, Bit);",
            "+  (void)0;",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("re-enables the source", result.stdout)

    def test_series_that_checks_the_wrong_group_enable_bit_is_rejected(self) -> None:
        # Bit 1 is EnableGrp0 under DS=1.  Both bits are set at reset, so the
        # wrong constant passes on an untouched device and only fails when a
        # guest clears Group 1 while leaving Group 0 alone.
        series = self._mutated_series(
            "+#define GICD_CTLR_ENABLEGRP1NS  (1U << 0)",
            "+#define GICD_CTLR_ENABLEGRP1NS  (1U << 1)",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("EnableGrp1 is bit 0 under DS=1", result.stdout)

    def test_series_without_the_pending_across_disable_probe_is_rejected(self) -> None:
        # gating.sgi proves a disabled SGI is not delivered; it does not prove
        # the refused interrupt was kept.  Only gating.pending, which enables
        # the bit and waits without resending, can tell those apart.
        series = self._mutated_series(
            '+      "gating.pending",',
            '+      "gating.other",',
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stayed pending across a disable", result.stdout)

    def test_series_without_the_enable_write_zero_probe_is_rejected(self) -> None:
        # Writing 0 to GICR_ISENABLER0 is what separates a write-1-to-set
        # register from a raw store; without it the probe cannot see the defect.
        series = self._mutated_series(
            "+  MmioWrite32 (SgiBase + GICR_SGI_ISENABLER, 0);",
            "+  (void)0;",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("writes 0 to GICR_ISENABLER0", result.stdout)

    def test_series_that_leaves_the_bank_probe_unrestored_is_rejected(self) -> None:
        series = self._mutated_series(
            "+  MmioWrite32 (SgiBase + GICR_SGI_IGROUPR0, OriginalGroup);",
            "+  (void)0;",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("restores the enable bit and the group register", result.stdout)

    def test_series_that_clears_ds_or_are_in_the_replay_is_rejected(self) -> None:
        # DS and ARE cannot be cleared on this platform; doing it in the replay
        # would model a reset the vGIC does not perform.
        series = self._mutated_series(
            "+  MmioWrite32 (Dist + GICD_CTLR, SavedCtlr & ~GICD_CTLR_ENABLEGRP1NS);",
            "+  MmioWrite32 (Dist + GICD_CTLR, 0);",
        )
        result = run_checker("--patches", series)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("only EnableGrp1NS is cleared", result.stdout)


@unittest.skipUnless(os.path.isdir(FIRMWARE), "firmware submodule not initialised")
class FirmwareTreeTests(unittest.TestCase):
    """The same contract against the materialised tree a contributor edits."""

    def test_tree_satisfies_the_contract(self) -> None:
        result = run_checker("--tree", FIRMWARE)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("vGIC self-test: OK", result.stdout)

    def _copy_tree(self) -> str:
        tmp = tempfile.mkdtemp(prefix="vgic-selftest-tree-")
        self.addCleanup(shutil.rmtree, tmp, True)
        for rel in (
            "Silicon/Apple/AppleSiliconPkg/Drivers/AppleVgicSelfTestDxe",
            "Platform/Mac16_3Pkg",
        ):
            source = os.path.join(FIRMWARE, rel)
            if os.path.isdir(source):
                shutil.copytree(source, os.path.join(tmp, rel))
        return tmp

    def _mutate(self, tree: str, rel: str, old: str, new: str) -> None:
        path = os.path.join(tree, rel)
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn(old, text, f"fixture drifted: {old!r} not in {rel}")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text.replace(old, new, 1))

    def test_tree_that_resends_before_the_pending_wait_is_rejected(self) -> None:
        # A resend would satisfy the wait even if the vGIC had discarded the
        # interrupt, which is exactly the defect gating.pending exists to see.
        tree = self._copy_tree()
        driver = ("Silicon/Apple/AppleSiliconPkg/Drivers/AppleVgicSelfTestDxe/"
                  "AppleVgicSelfTestDxe.c")
        self._mutate(
            tree,
            driver,
            """  mIrqCount = 0;
  MmioWrite32 (BootFrameBase + GICR_SGI_ISENABLER, Bit);

  if (VgicWaitCount (&mIrqCount, 1, VGIC_GATE_WINDOW_US)) {""",
            """  mIrqCount = 0;
  MmioWrite32 (BootFrameBase + GICR_SGI_ISENABLER, Bit);
  IccSgi1r (VgicSelfSgi1r (Mpidr, VGIC_TEST_SGI_2));

  if (VgicWaitCount (&mIrqCount, 1, VGIC_GATE_WINDOW_US)) {""",
            )
        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not send a second SGI before waiting", result.stdout)

    def test_tree_without_the_enable_write_zero_probe_is_rejected(self) -> None:
        tree = self._copy_tree()
        driver = ("Silicon/Apple/AppleSiliconPkg/Drivers/AppleVgicSelfTestDxe/"
                  "AppleVgicSelfTestDxe.c")
        self._mutate(tree, driver,
                     "MmioWrite32 (SgiBase + GICR_SGI_ISENABLER, 0);",
                     "(void)0;")
        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("writes 0 to GICR_ISENABLER0", result.stdout)

    def test_tree_that_asserts_is_rejected(self) -> None:
        tree = self._copy_tree()
        self._mutate(
            tree,
            "Silicon/Apple/AppleSiliconPkg/Drivers/AppleVgicSelfTestDxe/AppleVgicSelfTestDxe.c",
            "    VgicSkip (\"setup\", \"no vGIC PCDs on this platform; nothing to test\");",
            "    ASSERT_EFI_ERROR (EFI_NOT_FOUND);",
        )
        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ASSERT", result.stdout)

    def test_tree_that_moves_the_driver_after_the_timer_is_rejected(self) -> None:
        # TimerDxe registers a handler for the virtual timer PPI, and the
        # protocol refuses a second registration, so the self-test has to own
        # the PPI first.
        tree = self._copy_tree()
        fdf = os.path.join(tree, "Platform/Mac16_3Pkg/Mac16_3.fdf")
        with open(fdf, "r", encoding="utf-8") as handle:
            text = handle.read()
        driver = "  INF AppleSiliconPkg/Drivers/AppleVgicSelfTestDxe/AppleVgicSelfTestDxe.inf\n"
        self.assertIn(driver, text)
        text = text.replace(driver, "", 1)
        timer = "  INF ArmPkg/Drivers/TimerDxe/TimerDxe.inf\n"
        self.assertIn(timer, text)
        text = text.replace(timer, timer + driver, 1)
        with open(fdf, "w", encoding="utf-8") as handle:
            handle.write(text)

        result = run_checker("--tree", tree)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("precedes TimerDxe", result.stdout)


if __name__ == "__main__":
    unittest.main()
