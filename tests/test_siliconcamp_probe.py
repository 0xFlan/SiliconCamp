from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "siliconcamp-assistant"


def load_probe():
    spec = importlib.util.spec_from_file_location("siliconcamp_probe", TOOL / "probe.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load probe.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["siliconcamp_probe"] = module
    spec.loader.exec_module(module)
    return module


HEALTHY = [
    "HV vGIC: 10 CPUs, 42-bit PA, dist=0x5000000000 redist=0x5100000000 its=0x5200000000",
    "PSCI DEBUG: Number of cores for PSCI nodes is 10",
    "PSCI DEBUG: Number of clusters for PSCI nodes is 2",
    "vgic: ===== vGIC conformance self-test =====",
    "vgic: [PASS] dist.typer: No1N=1, LPIS=0",
    "vgic: totals: pass=1 fail=0 gap=0 skip=0",
    "vgic: RESULT: PASS",
]


def write_capture(directory: pathlib.Path, lines=None) -> pathlib.Path:
    body = [
        "# SiliconCamp serial capture",
        "# device: /dev/cu.usbmodem-test",
        "# baud: 115200",
        "# started: 2026-10-28T12:00:00+00:00",
    ]
    for index, text in enumerate(lines or HEALTHY):
        body.append(f"[12:00:{index:02d}.000] {text}")
    body.append(
        f"# ended: 2026-10-28T12:00:20+00:00 after 12.0s, "
        f"{sum(len(line) for line in (lines or HEALTHY))} bytes, "
        f"{len(lines or HEALTHY)} lines"
    )
    path = directory / "m4-first-boot.log"
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return path


class ProbeRecordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.probe = load_probe()

    def test_a_record_contains_the_evidence_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            capture = write_capture(pathlib.Path(tmp))
            out = pathlib.Path(tmp) / "record"
            result = self.probe.record(capture, out=out, label="first boot")
            self.assertEqual(result["verdict"], "pass")
            for name in self.probe.REQUIRED_FILES:
                self.assertTrue((out / name).is_file(), name)
            manifest = json.loads((out / "MANIFEST.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["format"], self.probe.FORMAT)
            self.assertEqual(manifest["verdict"], "pass")
            self.assertIn("README.md", manifest["files"])
            self.assertNotIn("MANIFEST.json", manifest["files"])
            self.assertEqual(
                (out / "capture.log").read_bytes(), capture.read_bytes()
            )
            readme = (out / "README.md").read_text(encoding="utf-8")
            self.assertIn("PASS", readme)
            self.assertIn("Mac16,3" if "Mac16,3" in readme else "Model:", readme)

            checked = self.probe.verify(out)
            self.assertTrue(checked["ok"], checked)
            self.assertEqual(checked["verdict"], "pass")

    def test_a_second_record_refuses_to_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            capture = write_capture(pathlib.Path(tmp))
            out = pathlib.Path(tmp) / "record"
            self.probe.record(capture, out=out)
            with self.assertRaises(self.probe.ProbeError) as caught:
                self.probe.record(capture, out=out)
            self.assertIn("refusing to overwrite", str(caught.exception))

    def test_a_tampered_capture_does_not_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            capture = write_capture(pathlib.Path(tmp))
            out = pathlib.Path(tmp) / "record"
            self.probe.record(capture, out=out)
            with (out / "capture.log").open("a", encoding="utf-8") as handle:
                handle.write("[12:00:59.000] injected line\n")
            checked = self.probe.verify(out)
            self.assertFalse(checked["ok"])
            self.assertTrue(
                any("capture.log" in problem for problem in checked["problems"]),
                checked["problems"],
            )

    def test_a_missing_file_does_not_verify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            capture = write_capture(pathlib.Path(tmp))
            out = pathlib.Path(tmp) / "record"
            self.probe.record(capture, out=out)
            (out / "machine.json").unlink()
            checked = self.probe.verify(out)
            self.assertFalse(checked["ok"])
            self.assertIn("missing machine.json", checked["problems"])

    def test_the_manifest_and_verdict_disagree_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            capture = write_capture(pathlib.Path(tmp))
            out = pathlib.Path(tmp) / "record"
            self.probe.record(capture, out=out)
            manifest = json.loads((out / "MANIFEST.json").read_text(encoding="utf-8"))
            manifest["verdict"] = "fail"
            (out / "MANIFEST.json").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
            checked = self.probe.verify(out)
            self.assertFalse(checked["ok"])
            self.assertTrue(
                any("disagrees" in problem for problem in checked["problems"]),
                checked["problems"],
            )

    def test_records_are_listed_with_their_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            capture = write_capture(pathlib.Path(tmp))
            out = pathlib.Path(tmp) / "record"
            self.probe.record(capture, out=out)
            rows = self.probe.list_records(pathlib.Path(tmp))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "pass")
        self.assertEqual(rows[0]["label"], "first boot")

    def test_labels_cannot_escape_the_probe_directory(self) -> None:
        self.assertEqual(self.probe._safe_label("../../etc/passwd"), "etc-passwd")
        self.assertEqual(self.probe._safe_label(""), "first-boot")

    def test_preflight_has_machine_checkable_and_manual_checks(self) -> None:
        report = self.probe.preflight()
        names = {check["name"] for check in report["checks"]}
        self.assertIn("machine", names)
        self.assertIn("artifacts", names)
        self.assertIn("serial", names)
        self.assertIn("second Mac", names)
        self.assertIn("boot security", names)
        self.assertEqual(report["ready"], not report["failures"])
        self.assertIsInstance(report["machine"]["model"], str)
