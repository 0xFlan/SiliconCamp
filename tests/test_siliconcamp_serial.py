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


if __name__ == "__main__":
    unittest.main()
