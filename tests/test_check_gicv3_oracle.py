#!/usr/bin/env python3
"""Regression tests for scripts/check-gicv3-oracle.py.

The parser tests are the part that must always run: the oracle's whole value is
that a `FAIL` from qemu is not read as a value.  The live test drives QEMU when
it is installed and skips otherwise, because the oracle is a development and CI
aid, not a dependency of building the project.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
SCRIPT = os.path.join(ROOT, "scripts", "check-gicv3-oracle.py")


def load_oracle():
    spec = importlib.util.spec_from_file_location("check_gicv3_oracle", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.oracle = load_oracle()

    def test_ok_with_a_value_is_parsed_as_hex(self) -> None:
        self.assertEqual(self.oracle.parse_ok("OK 0x0000000000000050"), 0x50)

    def test_ok_without_a_value_is_a_write_acknowledgement(self) -> None:
        self.assertIsNone(self.oracle.parse_ok("OK"))

    def test_fail_is_not_a_value(self) -> None:
        with self.assertRaises(self.oracle.QTestError):
            self.oracle.parse_ok("FAIL unknown command")

    def test_garbage_is_rejected(self) -> None:
        with self.assertRaises(self.oracle.QTestError):
            self.oracle.parse_ok("0xdeadbeef")


@unittest.skipUnless(shutil.which("qemu-system-aarch64"),
                     "qemu-system-aarch64 is not installed")
class LiveOracleTests(unittest.TestCase):
    """The oracle against the installed QEMU; skipped when it is absent."""

    def test_the_installed_gicv3_agrees_with_the_contract(self) -> None:
        result = subprocess.run(
            [sys.executable, SCRIPT],
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("GICv3 oracle: OK", result.stdout)
        # The two facts the enable-gating patch is built on, named so a failure
        # says which one moved rather than just "the oracle failed".
        self.assertIn("GICR_ICENABLER0 reads the enable state", result.stdout)
        self.assertIn("GICD_IGROUPR1 is writable", result.stdout)


if __name__ == "__main__":
    unittest.main()
