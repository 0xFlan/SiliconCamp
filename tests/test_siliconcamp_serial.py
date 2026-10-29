from __future__ import annotations

import importlib.util
import os
import pathlib
import pty
import re
import sys
import tempfile
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "siliconcamp-assistant"


def load_serial():
    spec = importlib.util.spec_from_file_location("siliconcamp_serial", TOOL / "serial.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load serial.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["siliconcamp_serial"] = module
    spec.loader.exec_module(module)
    return module


class SerialCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.serial = load_serial()

    def test_ports_are_listed_with_the_console_first(self) -> None:
        rows = self.serial.list_ports()
        self.assertIsInstance(rows, list)
        self.assertNotIn("cu.Bluetooth-Incoming-Port", [row["name"] for row in rows])
        likely = [row for row in rows if row["likely_serial"]]
        if likely:
            self.assertIn(likely[0], rows[: len(likely)])

    def test_a_builtin_debug_console_is_not_a_usb_adapter(self) -> None:
        # macOS always exposes /dev/cu.debug-console.  Treating that as the
        # hardware console made a machine with no adapter look ready.
        from unittest import mock

        with mock.patch.object(
            self.serial.glob,
            "glob",
            return_value=["/dev/cu.debug-console", "/dev/cu.usbmodem1234"],
        ):
            rows = self.serial.list_ports()
        by_name = {os.path.basename(row["device"]): row for row in rows}
        self.assertFalse(by_name["cu.debug-console"]["likely_serial"])
        self.assertTrue(by_name["cu.usbmodem1234"]["likely_serial"])

    def test_an_unsupported_baud_is_refused(self) -> None:
        with self.assertRaises(self.serial.SerialError):
            self.serial._baud_constant(12345)

    def test_a_missing_device_names_the_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(self.serial.SerialError) as caught:
                self.serial.capture(
                    "/dev/cu.siliconcamp-does-not-exist",
                    pathlib.Path(tmp) / "boot.log",
                    seconds=0.1,
                )
        self.assertIn("/dev/cu.siliconcamp-does-not-exist", str(caught.exception))

    def test_capture_refuses_to_mix_two_boots_into_one_file(self) -> None:
        # A verdict reads one run.  Appending a second boot to the same file
        # could let a clean first run mask a silent second one.
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "m4-boot.log"
            log.write_text("previous run\n", encoding="utf-8")
            with self.assertRaises(self.serial.SerialError) as caught:
                self.serial.capture(
                    "/dev/cu.siliconcamp-does-not-exist", log, seconds=0.1
                )
            self.assertIn("already exists", str(caught.exception))
            self.assertEqual(log.read_text(encoding="utf-8"), "previous run\n")

    def test_capture_timestamps_lines_and_keeps_a_partial_one(self) -> None:
        # A pseudo-terminal is a real serial device as far as termios is
        # concerned: this exercises open, raw mode, select, line assembly and
        # the footer without a USB adapter.
        master, slave = pty.openpty()
        device = os.ttyname(slave)
        stop = threading.Event()
        result: dict = {}
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "m4-boot.log"

            def run() -> None:
                result.update(
                    self.serial.capture(device, log, seconds=5, stop=stop, quiet=True)
                )

            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            time.sleep(0.3)
            payload = b"m1n1: hello\r\nfirmware: 18000.161.10\r\nhalf a line"
            os.write(master, payload)
            time.sleep(0.4)
            stop.set()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            text = log.read_text(encoding="utf-8")

        self.assertIn("# SiliconCamp serial capture", text)
        self.assertIn(f"# device: {device}", text)
        self.assertRegex(text, r"(?m)^# baud: 115200$")
        self.assertRegex(text, r"(?m)^\[\d\d:\d\d:\d\d\.\d{3}\] m1n1: hello$")
        self.assertRegex(text, r"(?m)^\[\d\d:\d\d:\d\d\.\d{3}\] firmware: 18000\.161\.10$")
        self.assertIn("half a line (unterminated)", text)
        self.assertRegex(text, r"(?m)^# ended: .* \d+ bytes, 3 lines$")
        self.assertEqual(result["lines"], 3)
        self.assertEqual(result["bytes"], len(payload))
        self.assertFalse(result["interrupted"])

        os.close(master)
        os.close(slave)


class SerialVerdictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.serial = load_serial()

    def write_log(self, tmp, lines, *, footer=True) -> pathlib.Path:
        path = pathlib.Path(tmp) / "m4-boot.log"
        body = [
            "# SiliconCamp serial capture",
            "# device: /dev/cu.usbmodem-test",
            "# baud: 115200",
            "# started: 2026-10-28T12:00:00+00:00",
        ]
        for index, text in enumerate(lines):
            body.append(f"[12:00:{index:02d}.000] {text}")
        if footer:
            body.append(
                f"# ended: 2026-10-28T12:00:20+00:00 after 12.0s, "
                f"{sum(len(line) for line in lines)} bytes, {len(lines)} lines"
            )
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        return path

    def healthy(self) -> list[str]:
        return [
            "HV vGIC: 10 CPUs, 42-bit PA, dist=0x5000000000 redist=0x5100000000 its=0x5200000000",
            "PSCI DEBUG: topology tree selected",
            "PSCI DEBUG: Number of cores for PSCI nodes is 10",
            "PSCI DEBUG: Number of clusters for PSCI nodes is 2",
            "vgic: ===== vGIC conformance self-test =====",
            "vgic: [PASS] dist.typer: No1N=1, LPIS=0",
            "vgic: [PASS] wake.cpu0: ChildrenAsleep cleared",
            "vgic: totals: pass=2 fail=0 gap=0 skip=0",
            "vgic: RESULT: PASS",
        ]

    def test_a_clean_first_boot_is_a_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_log(tmp, self.healthy())
            report = self.serial.judge_capture(path)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["verdict"], "pass")
        self.assertEqual(report["m1n1"]["cpus"], 10)
        self.assertEqual(report["m1n1"]["pa_bits"], 42)
        self.assertEqual(report["psci"], {"cores": 10, "clusters": 2})
        self.assertEqual(report["selftest"]["result"], "PASS")
        self.assertEqual(report["selftest"]["pass"], 2)
        self.assertIn("PASS", self.serial.format_verdict(report))

    def test_a_self_test_failure_fails_the_boot(self) -> None:
        lines = [
            self.healthy()[0],
            "vgic: [FAIL] redist.cpu3: GICR_WAKER never cleared",
            "vgic: totals: pass=0 fail=1 gap=0 skip=0",
            "vgic: RESULT: FAIL",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            report = self.serial.judge_capture(self.write_log(tmp, lines))
        self.assertFalse(report["ok"])
        self.assertEqual(report["verdict"], "fail")
        self.assertTrue(any("self-test failed" in item for item in report["failures"]))
        self.assertEqual(report["selftest"]["failed_checks"][0]["name"], "redist.cpu3")

    def test_a_fault_fails_the_boot_even_after_a_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self.serial.judge_capture(
                self.write_log(tmp, self.healthy() + ["Unhandled exception, rebooting..."])
            )
        self.assertEqual(report["verdict"], "fail")
        self.assertTrue(any("fault" in item for item in report["failures"]))

    def test_a_wrong_cpu_count_fails_the_topology(self) -> None:
        lines = self.healthy()
        lines[0] = lines[0].replace("10 CPUs", "8 CPUs")
        with tempfile.TemporaryDirectory() as tmp:
            report = self.serial.judge_capture(self.write_log(tmp, lines))
        self.assertEqual(report["verdict"], "fail")
        self.assertTrue(any("8 CPUs" in item for item in report["failures"]))

    def test_gaps_are_reported_without_failing_the_boot(self) -> None:
        lines = [
            self.healthy()[0],
            "PSCI DEBUG: Number of cores for PSCI nodes is 10",
            "PSCI DEBUG: Number of clusters for PSCI nodes is 2",
            "vgic: [GAP ] spi.affinity: AICv3 has no per-SPI target",
            "vgic: totals: pass=0 fail=0 gap=1 skip=0",
            "vgic: RESULT: PASS-WITH-GAPS",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            report = self.serial.judge_capture(self.write_log(tmp, lines))
        self.assertTrue(report["ok"])
        self.assertEqual(report["verdict"], "pass-with-gaps")
        self.assertEqual(report["selftest"]["gaps"][0]["name"], "spi.affinity")
        self.assertIn("gap: spi.affinity", self.serial.format_verdict(report))

    def test_a_silent_capture_is_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = self.serial.judge_capture(self.write_log(tmp, []))
        self.assertFalse(report["ok"])
        self.assertEqual(report["verdict"], "incomplete")
        self.assertTrue(any("no console output" in item for item in report["missing"]))

    def test_m1n1_without_the_firmware_is_incomplete(self) -> None:
        lines = self.healthy()[:4]
        with tempfile.TemporaryDirectory() as tmp:
            report = self.serial.judge_capture(self.write_log(tmp, lines))
        self.assertEqual(report["verdict"], "incomplete")
        self.assertTrue(any("self-test did not run" in item for item in report["missing"]))

    def test_a_truncated_log_without_a_footer_still_parses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_log(tmp, self.healthy(), footer=False)
            report = self.serial.judge_capture(path)
        self.assertEqual(report["verdict"], "pass")
        self.assertIsNone(report["capture"]["duration"])

    def test_the_cli_exit_status_matches_the_verdict(self) -> None:
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as tmp:
            good = self.write_log(tmp, self.healthy())
            bad = pathlib.Path(tmp) / "bad.log"
            bad.write_text(
                "[12:00:00.000] HV vGIC: 10 CPUs, 42-bit PA, dist=0x1 redist=0x2 its=0x3\n"
                "[12:00:01.000] Unhandled exception, rebooting...\n",
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self.serial.main(["verdict", "--in", str(good)]), 0)
                self.assertEqual(self.serial.main(["verdict", "--in", str(bad)]), 1)


if __name__ == "__main__":
    unittest.main()
