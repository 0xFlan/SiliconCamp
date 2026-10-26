#!/usr/bin/env python3
"""Tests for scripts/soc-coverage.py.

The report is only useful if it reflects the tree rather than a hardcoded
opinion, so these tests build a synthetic firmware tree and check what the
script says about it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS_DIR)
SCRIPT = os.path.join(ROOT, "scripts", "soc-coverage.py")


def run(tree: str) -> dict:
    result = subprocess.run(
        [sys.executable, SCRIPT, "--tree", tree, "--json"],
        capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(result.stdout + result.stderr)
    return json.loads(result.stdout)


def machine(report: dict, model: str) -> dict:
    found = [m for m in report["machines"] if m["model"] == model]
    if not found:
        raise AssertionError(f"{model} missing from the device table")
    return found[0]


class TestCoverage(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tree = self.tmp.name
        os.makedirs(os.path.join(self.tree, "Silicon", "Apple"))
        os.makedirs(os.path.join(self.tree, "Platform"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def touch_package(self, *parts: str) -> None:
        os.makedirs(os.path.join(self.tree, *parts), exist_ok=True)

    def test_empty_tree_reports_nothing_present(self) -> None:
        report = run(self.tree)
        self.assertEqual(report["families_found"], [])
        self.assertTrue(all(not m["silicon"] for m in report["machines"]))

    def test_silicon_package_covers_its_family(self) -> None:
        self.touch_package("Silicon", "Apple", "T8132FamilyPkg")
        report = run(self.tree)
        self.assertTrue(machine(report, "Mac16,3")["silicon"])
        # The M4 family is one SoC, so every T8132 machine is covered.
        self.assertTrue(machine(report, "Mac16,2")["silicon"])
        # M4 Pro/Max is a different SoC and stays uncovered.
        self.assertFalse(machine(report, "Mac16,5")["silicon"])

    def test_platform_package_matches_by_model_identifier(self) -> None:
        self.touch_package("Silicon", "Apple", "T8132FamilyPkg")
        self.touch_package("Platform", "Mac16_3Pkg")
        report = run(self.tree)
        self.assertTrue(machine(report, "Mac16,3")["platform"])
        self.assertFalse(machine(report, "Mac16,2")["platform"])

    def test_m1_ultra_is_not_covered_by_the_pro_max_family(self) -> None:
        # T600X covers T6000/T6001; the Ultra (T6002) is its own die.
        self.touch_package("Silicon", "Apple", "T600XFamilyPkg")
        report = run(self.tree)
        self.assertTrue(machine(report, "MacBookPro18,1")["silicon"])
        self.assertFalse(machine(report, "Mac13,2")["silicon"])


if __name__ == "__main__":
    unittest.main()
