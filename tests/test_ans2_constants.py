"""Regression tests for scripts/check-ans2-constants.py.

The checker's value is that it fails when the driver and m1n1 disagree, so the
tests here do two things: prove it passes on the tree as committed, and prove it
fails on a header with one constant changed.  The second half is the important
one -- a checker that only ever prints ok is indistinguishable from a checker
that does nothing.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check-ans2-constants.py"
M1N1 = ROOT / "third_party" / "m1n1-windows" / "src"
DRIVER = (
    ROOT
    / "third_party"
    / "ntasp-firmware"
    / "Silicon"
    / "Apple"
    / "AppleSiliconPkg"
    / "Drivers"
    / "AppleNANDStorageDxe"
    / "AppleNandStorage.h"
)


def load_checker_module():
    """Import scripts/check-ans2-constants.py, whose name is not an identifier."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("check_ans2_constants", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExpressionEvaluator(unittest.TestCase):
    """The checker has to understand how these headers spell constants."""

    def setUp(self) -> None:
        self.module = load_checker_module()

    def test_hex_and_decimal(self) -> None:
        self.assertEqual(self.module.evaluate("0x1c", {}), 0x1C)
        self.assertEqual(self.module.evaluate("64", {}), 64)

    def test_edk2_bit_names(self) -> None:
        self.assertEqual(self.module.evaluate("BIT16", self.module.EDK2_BITS), 1 << 16)

    def test_bit_and_genmask(self) -> None:
        self.assertEqual(self.module.evaluate("BIT(51)", {}), 1 << 51)
        self.assertEqual(self.module.evaluate("GENMASK(59, 52)", {}), 0xFF << 52)

    def test_function_macro_body(self) -> None:
        self.assertEqual(self.module.evaluate("0x40 + 4 * (idx)", {"idx": 0}), 0x40)
        self.assertEqual(self.module.evaluate("0x40 + 4 * (idx)", {"idx": 3}), 0x4C)

    def test_c_integer_suffixes_are_stripped(self) -> None:
        defines = {"X": ([], self.module.INT_SUFFIX_RE.sub(r"\1", "0x14ULL"))}
        self.assertEqual(defines["X"][1], "0x14")

    def test_mask_bounds(self) -> None:
        self.assertEqual(self.module.mask_bounds(0xFF << 52), (52, 59))
        self.assertEqual(self.module.mask_bounds(1 << 51), (51, 51))


@unittest.skipUnless(M1N1.is_dir(), "m1n1 submodule is not checked out")
class TreeAgreement(unittest.TestCase):
    def test_constants_match_m1n1(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("ANS2 constants: OK", result.stdout)


@unittest.skipUnless(M1N1.is_dir(), "m1n1 submodule is not checked out")
class MutationsAreCaught(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.header = Path(self.temporary.name) / "AppleNandStorage.h"
        shutil.copyfile(DRIVER, self.header)

    def run_checker(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--driver", str(self.header)],
            capture_output=True,
            text=True,
            check=False,
        )

    def mutate(self, pattern: str, replacement: str) -> None:
        text = self.header.read_text(encoding="utf-8")
        mutated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
        self.assertEqual(count, 1, msg=f"pattern {pattern!r} matched {count} times")
        self.header.write_text(mutated, encoding="utf-8")

    def test_unmutated_header_passes(self) -> None:
        result = self.run_checker()
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)

    def test_detects_a_changed_register_offset(self) -> None:
        self.mutate(r"^#define NVME_CC\s+0x14$", "#define NVME_CC                0x18")
        result = self.run_checker()
        self.assertEqual(result.returncode, 1, msg=result.stdout)
        self.assertIn("NVME_CC", result.stdout)

    def test_detects_a_changed_field_position(self) -> None:
        self.mutate(
            r"^#define RTKIT_MGMT_TYPE_START\s+52$",
            "#define RTKIT_MGMT_TYPE_START  53",
        )
        result = self.run_checker()
        self.assertEqual(result.returncode, 1, msg=result.stdout)
        self.assertIn("MGMT_TYPE", result.stdout)

    def test_detects_a_changed_asc_mailbox(self) -> None:
        self.mutate(
            r"^#define ASC_MBOX_I2A_RECV0\s+0x830$",
            "#define ASC_MBOX_I2A_RECV0    0x810",
        )
        result = self.run_checker()
        self.assertEqual(result.returncode, 1, msg=result.stdout)
        self.assertIn("ASC_MBOX_I2A_RECV0", result.stdout)


class MissingTreeIsNotAnError(unittest.TestCase):
    def test_skips_when_m1n1_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            # A path that does not exist is "not checked out"; an empty
            # directory with the submodule present but sources missing is an
            # error, which is what the next test pins.
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--m1n1", str(Path(temporary) / "absent")],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertIn("skip", result.stdout)

    # Reaching the m1n1 sources with a driver header present is exactly the
    # state the repository describes, so without the firmware submodule this
    # test would only re-test the "header not found" path.  The checks job runs
    # without submodules; the patch-applicability job runs this suite with the
    # patched firmware tree and exercises it.
    @unittest.skipUnless(DRIVER.exists(), "ntasp-firmware submodule not checked out")
    def test_missing_sources_in_a_present_tree_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--m1n1", temporary],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 1, msg=result.stdout + result.stderr)
        self.assertIn("missing", result.stderr)


if __name__ == "__main__":
    unittest.main()
