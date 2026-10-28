import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "tools" / "siliconcamp-assistant" / "server.py"


def load_server():
    spec = importlib.util.spec_from_file_location(
        "siliconcamp_assistant_server", SERVER
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SERVER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AssistantStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = load_server()

    def test_status_has_the_panel_shape(self) -> None:
        status = self.server.build_status()
        self.assertEqual(status["product"], "SiliconCamp Assistant")
        self.assertTrue(status["preview"])
        self.assertEqual(len(status["artifacts"]), 3)
        self.assertEqual(len(status["blockers"]), 5)
        self.assertTrue(status["checks"])
        self.assertIn("machine", status)
        self.assertIn("repo", status)
        self.assertIn("roadmap", status)

    def test_artifact_names_are_the_pinned_names(self) -> None:
        # The UI must report every artifact SHA256SUMS pins, even when a local
        # build has not produced it yet (the row is then "missing").
        pinned = self.server.expected_hashes()
        names = {row["name"] for row in self.server.artifact_status()}
        self.assertEqual(len(pinned), 3)
        self.assertEqual(names, set(pinned))

    def test_a_skip_is_not_a_pass(self) -> None:
        # verify-patches.sh exits 0 and prints "skip:" when a submodule is not
        # initialised; the panel must not paint that green.
        ok, skipped = self.server.classify_check(
            0, "skip: m1n1-windows (submodule not initialized)\n"
        )
        self.assertFalse(ok)
        self.assertTrue(skipped)
        ok, skipped = self.server.classify_check(0, "Patch verification: OK\n")
        self.assertTrue(ok)
        self.assertFalse(skipped)
        ok, skipped = self.server.classify_check(1, "vGIC contract: FAILED\n")
        self.assertFalse(ok)
        self.assertFalse(skipped)

    def test_dirty_submodules_are_not_reported_as_source_edits(self) -> None:
        # Porcelain uses ` M` for a submodule with modified content, the same
        # code a modified file gets; the panel must not call that a source edit.
        info = self.server.repo_info()
        declared = self.server._submodule_paths()
        self.assertIn("third_party/m1n1-windows", declared)
        self.assertTrue(set(info["submodules_modified"]).issubset(declared))
        for line in info["source_modified"]:
            self.assertNotIn(line[3:].strip(), declared)

    def test_diagnostics_are_a_fixed_allow_list(self) -> None:
        # There is no request-supplied command: every entry is an argv list
        # rooted in the repository's own checkers.
        for label, argv, timeout in self.server.CHECKS.values():
            self.assertTrue(label)
            self.assertIsInstance(argv, list)
            self.assertTrue(argv)
            self.assertGreater(timeout, 0)
            self.assertTrue(all(isinstance(part, str) for part in argv))


if __name__ == "__main__":
    unittest.main()
