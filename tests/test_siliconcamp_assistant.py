from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import plistlib
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer


ROOT = pathlib.Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "siliconcamp-assistant"
SERVER = TOOL / "server.py"


def load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations through sys.modules[cls.__module__].
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def load_server():
    return load_module("siliconcamp_assistant_server", SERVER)


# The media engine shells out to hdiutil/diskutil; CI runs on Linux, where the
# pure-logic tests still run and these are skipped.
HAVE_DISK_TOOLS = (
    sys.platform == "darwin"
    and pathlib.Path("/usr/bin/hdiutil").exists()
    and pathlib.Path("/usr/sbin/diskutil").exists()
)


def find_free_port() -> int:
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class AssistantStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = load_server()

    def test_status_has_the_walkthrough_payload(self) -> None:
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
            parts = line.split(maxsplit=1)
            self.assertTrue(parts)
            self.assertNotIn(parts[1] if len(parts) > 1 else parts[0], declared)

    def test_diagnostics_are_a_fixed_allow_list(self) -> None:
        # There is no request-supplied command: every entry is an argv list
        # rooted in the repository's own checkers.
        for label, argv, timeout in self.server.CHECKS.values():
            self.assertTrue(label)
            self.assertIsInstance(argv, list)
            self.assertTrue(argv)
            self.assertGreater(timeout, 0)
            self.assertTrue(all(isinstance(part, str) for part in argv))

    def test_walkthrough_steps_are_in_boot_camp_order(self) -> None:
        html = (TOOL / "web" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(
            re.findall(r'data-pane="([^"]+)"', html),
            [
                "intro",
                "tasks",
                "readiness",
                "installer",
                "allocation",
                "safety",
                "review",
                "install",
            ],
        )
        # The final Install control must stay disabled, and the walkthrough
        # must identify itself as an application, not a scrolling dashboard.
        self.assertRegex(
            html,
            r'<button class="button default install-button" type="button" disabled>',
        )
        self.assertIn('role="application"', html)

    def test_walkthrough_loads_only_local_assets(self) -> None:
        html = (TOOL / "web" / "index.html").read_text(encoding="utf-8")
        assets = re.findall(r'(?:src|href)="([^"]+)"', html)
        self.assertTrue(assets)
        for asset in assets:
            self.assertTrue(
                asset.startswith("/") or asset.startswith("#"),
                f"non-local asset reference: {asset}",
            )
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)

    def test_write_token_is_required_by_the_page_and_the_server(self) -> None:
        html = (TOOL / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('name="siliconcamp-token" content="__SILICONCAMP_TOKEN__"', html)
        script = (TOOL / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("X-SiliconCamp-Token", script)

    def test_native_bundle_is_described(self) -> None:
        info = plistlib.loads((TOOL / "mac" / "Info.plist").read_bytes())
        self.assertEqual(info["CFBundleIdentifier"], "com.siliconcamp.assistant")
        self.assertEqual(info["CFBundleExecutable"], "SiliconCampAssistant")
        self.assertTrue((TOOL / "mac" / "SiliconCampAssistant.swift").is_file())
        self.assertTrue(os.access(TOOL / "build-app.sh", os.X_OK))


class MediaPayloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.media = load_module("siliconcamp_media", TOOL / "media.py")

    def payload_tree(self, root: pathlib.Path, install_bytes: int = 4096) -> None:
        (root / "efi" / "boot").mkdir(parents=True, exist_ok=True)
        (root / "sources").mkdir(parents=True, exist_ok=True)
        (root / "efi" / "boot" / "bootaa64.efi").write_bytes(b"MZ" + b"\0" * 64)
        (root / "sources" / "boot.wim").write_bytes(b"w" * 1024)
        with (root / "sources" / "install.wim").open("wb") as handle:
            handle.truncate(install_bytes)

    def test_a_windows_arm64_tree_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.payload_tree(root)
            payload = self.media.scan_payload(root)
            self.assertEqual(payload.boot_efi, "efi/boot/bootaa64.efi")
            self.assertEqual(payload.boot_wim, "sources/boot.wim")
            self.assertEqual(payload.install_kind, "wim")
            self.assertFalse(payload.needs_split)
            self.assertEqual(payload.file_count, 3)

    def test_case_insensitive_required_paths_are_found(self) -> None:
        # ISO9660 media is often upper-case; the check must not depend on it.
        # APFS here is case-insensitive, so only the file's own case can change.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.payload_tree(root)
            (root / "efi" / "boot" / "bootaa64.efi").rename(
                root / "efi" / "boot" / "BootAA64.EFI"
            )
            payload = self.media.scan_payload(root)
            self.assertEqual(payload.boot_efi, "efi/boot/BootAA64.EFI")
            self.assertEqual(payload.boot_efi.lower(), "efi/boot/bootaa64.efi")

    def test_a_tree_without_bootaa64_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.payload_tree(root)
            (root / "efi" / "boot" / "bootaa64.efi").unlink()
            with self.assertRaises(self.media.MediaError) as caught:
                self.media.scan_payload(root)
            self.assertIn("BOOTAA64.EFI", str(caught.exception))

    def test_a_tree_without_boot_wim_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.payload_tree(root)
            (root / "sources" / "boot.wim").unlink()
            with self.assertRaises(self.media.MediaError):
                self.media.scan_payload(root)

    def test_files_differing_only_by_case_are_rejected(self) -> None:
        # FAT32 is case-insensitive, so both cannot exist on the destination.
        # The walk is mocked because this Mac's APFS cannot hold both names.
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.payload_tree(root)
            sources = root / "sources"
            walked = [
                (str(root / "efi" / "boot"), [], ["bootaa64.efi"]),
                (str(sources), [], ["boot.wim", "install.wim", "Install.wim"]),
            ]
            with mock.patch.object(self.media.os, "walk", return_value=walked):
                with self.assertRaises(self.media.MediaError) as caught:
                    self.media.scan_payload(root)
            self.assertIn("case", str(caught.exception))

    def test_an_install_wim_over_fat32_needs_splitting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.payload_tree(root, install_bytes=self.media.FAT32_FILE_LIMIT + 1)
            payload = self.media.scan_payload(root)
            self.assertTrue(payload.needs_split)
            self.assertEqual(payload.install_kind, "wim")

    def test_an_oversized_boot_wim_is_a_blocker_not_a_split(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.payload_tree(root)
            with (root / "sources" / "boot.wim").open("wb") as handle:
                handle.truncate(self.media.FAT32_FILE_LIMIT + 1)
            payload = self.media.scan_payload(root)
            self.assertIn("sources/boot.wim", payload.oversized)
            plan = self.media.plan_media(
                payload, target={"kind": "image", "path": str(pathlib.Path(tmp) / "x.sparseimage")}
            )
            self.assertTrue(any("cannot be split" in b for b in plan["blockers"]))

    def test_split_wim_without_wimlib_blocks_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.payload_tree(root, install_bytes=self.media.FAT32_FILE_LIMIT + 1)
            payload = self.media.scan_payload(root)
            plan = self.media.plan_media(
                payload,
                target={"kind": "image", "path": str(root / "out.sparseimage")},
                splitter="",
            )
            self.assertTrue(any("wimlib" in b for b in plan["blockers"]))
            with self.assertRaises(self.media.MediaError):
                self.media._write_media(
                    payload,
                    target={"kind": "image", "path": str(root / "out.sparseimage")},
                    splitter="",
                    log=lambda _: None,
                )
            self.assertFalse((root / "out.sparseimage").exists())

    def test_image_targets_must_be_absolute_and_new(self) -> None:
        image = self.media
        with self.assertRaises(image.MediaError):
            image.normalise_image("relative.sparseimage")
        with tempfile.TemporaryDirectory() as tmp:
            existing = pathlib.Path(tmp) / "taken.sparseimage"
            existing.write_bytes(b"")
            with self.assertRaises(image.MediaError):
                image.normalise_image(str(existing))
            with self.assertRaises(image.MediaError):
                image.normalise_image(str(pathlib.Path(tmp) / "plain.txt"))

    def test_an_internal_disk_is_never_a_media_target(self) -> None:
        payload_target = {"kind": "disk", "device": "/dev/disk0"}
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.payload_tree(root)
            payload = self.media.scan_payload(root)
            if not HAVE_DISK_TOOLS:
                self.skipTest("needs diskutil")
            with self.assertRaises(self.media.MediaError) as caught:
                self.media.plan_media(payload, target=payload_target)
            self.assertIn("not an external USB disk", str(caught.exception))

    def test_a_device_write_needs_an_explicit_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            self.payload_tree(root)
            payload = self.media.scan_payload(root)
            if not HAVE_DISK_TOOLS:
                self.skipTest("needs diskutil")
            with self.assertRaises(self.media.MediaError):
                self.media._write_media(
                    payload,
                    target={"kind": "disk", "device": "/dev/disk0"},
                    confirm=False,
                    log=lambda _: None,
                )


@unittest.skipUnless(HAVE_DISK_TOOLS, "needs hdiutil and diskutil")
class MediaImageTests(unittest.TestCase):
    """The whole media pipeline, on a disk image instead of a USB stick."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.media = load_module("siliconcamp_media_image", TOOL / "media.py")

    def payload_tree(self, root: pathlib.Path, install_bytes: int = 64 * 1024) -> None:
        (root / "efi" / "boot").mkdir(parents=True, exist_ok=True)
        (root / "sources").mkdir(parents=True, exist_ok=True)
        (root / "boot").mkdir(parents=True, exist_ok=True)
        (root / "efi" / "boot" / "bootaa64.efi").write_bytes(b"MZ" + os.urandom(512))
        (root / "sources" / "boot.wim").write_bytes(os.urandom(32 * 1024))
        (root / "boot" / "BCD").write_bytes(os.urandom(4096))
        with (root / "sources" / "install.wim").open("wb") as handle:
            handle.truncate(install_bytes)

    def test_media_is_partitioned_formatted_copied_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "iso"
            root.mkdir()
            self.payload_tree(root)
            payload = self.media.scan_payload(root)
            image = pathlib.Path(tmp) / "media.sparseimage"

            events: list[str] = []
            report = self.media._write_media(
                payload,
                target={"kind": "image", "path": str(image), "label": "SCMEDIA"},
                log=events.append,
            )

            self.assertTrue(report["ok"])
            self.assertTrue(report["verified"]["ok"])
            self.assertEqual(report["verified"]["missing"], [])
            self.assertEqual(report["verified"]["mismatched"], [])
            self.assertTrue(image.exists())
            self.assertTrue(any("verified" in line for line in events))

            # Read the media back the way a firmware would: the removable-media
            # boot path must be on the volume, and the macOS metadata that the
            # FAT driver writes must not be part of the payload.
            mount, device = self.media.mount_iso(image)
            try:
                base = pathlib.Path(mount)
                self.assertTrue((base / "efi" / "boot" / "bootaa64.efi").is_file())
                self.assertTrue((base / "sources" / "boot.wim").is_file())
                self.assertTrue((base / "sources" / "install.wim").is_file())
                stray = [
                    path.name
                    for path in base.rglob("*")
                    if path.name.startswith("._") or path.name == ".fseventsd"
                ]
                self.assertEqual(stray, [])
            finally:
                self.media.detach(device)

    def test_the_media_is_one_mbr_fat32_partition(self) -> None:
        # The shape Microsoft documents for a flash drive, and the one that does
        # not lose 200 MB to an empty EFI System Partition.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "iso"
            root.mkdir()
            self.payload_tree(root)
            payload = self.media.scan_payload(root)
            image = pathlib.Path(tmp) / "media.sparseimage"
            report = self.media._write_media(
                payload,
                target={"kind": "image", "path": str(image), "label": "SCMEDIA"},
                log=lambda _: None,
            )
            self.assertTrue(report["ok"], report)
            mount, device = self.media.mount_iso(image)
            try:
                listing = self.media._plist([self.media.DISKUTIL, "list", "-plist", device])
                rows = listing.get("AllDisksAndPartitions", [])
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].get("Content"), "FDisk_partition_scheme")
                partitions = rows[0].get("Partitions", [])
                self.assertEqual(len(partitions), 1)
                self.assertEqual(partitions[0].get("Content"), "DOS_FAT_32")
                info = self.media._plist([self.media.DISKUTIL, "info", "-plist", mount])
                self.assertEqual(info.get("FilesystemType"), "msdos")
                self.assertEqual(info.get("VolumeName"), "SCMEDIA")
            finally:
                self.media.detach(device)

    def test_a_failed_build_does_not_leave_an_image_behind(self) -> None:
        # Leaving a partial image would also block the retry, because the engine
        # refuses to overwrite a file it did not create.
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "iso"
            root.mkdir()
            self.payload_tree(root)
            payload = self.media.scan_payload(root)
            image = pathlib.Path(tmp) / "broken.sparseimage"
            failed = {
                "ok": False,
                "missing": ["sources/boot.wim"],
                "mismatched": [],
                "checked": 1,
                "hashed": 0,
                "size_only": 0,
            }
            with mock.patch.object(self.media, "verify_media", return_value=failed):
                with self.assertRaises(self.media.MediaError):
                    self.media._write_media(
                        payload,
                        target={"kind": "image", "path": str(image), "label": "SCMEDIA"},
                        log=lambda _: None,
                    )
            self.assertFalse(image.exists())

    def test_an_oversized_install_wim_is_split_onto_the_media(self) -> None:
        # A fake splitter stands in for wimlib-imagex: the branch under test is
        # that install.wim is replaced by install.swm parts, never copied whole,
        # and that the parts are what the verification pass reads back.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / "iso"
            root.mkdir()
            self.payload_tree(root, install_bytes=self.media.FAT32_FILE_LIMIT + 4096)
            payload = self.media.scan_payload(root)
            self.assertTrue(payload.needs_split)

            splitter = pathlib.Path(tmp) / "fake-wimlib"
            splitter.write_text(
                "#!/bin/sh\n"
                "# $1=split $2=source $3=destination $4=part size\n"
                "cp \"$2\" \"$3\"\n"
                "printf 'part' > \"$(dirname \"$3\")/install2.swm\"\n"
            )
            splitter.chmod(0o755)

            image = pathlib.Path(tmp) / "media.sparseimage"
            report = self.media._write_media(
                payload,
                target={"kind": "image", "path": str(image), "label": "SCMEDIA"},
                splitter=str(splitter),
                log=lambda _: None,
            )
            self.assertTrue(report["ok"], report)
            self.assertEqual(report["verified"]["missing"], [])
            self.assertTrue(report["copied"]["split"])

            mount, device = self.media.mount_iso(image)
            try:
                base = pathlib.Path(mount)
                self.assertTrue((base / "sources" / "install.swm").is_file())
                self.assertTrue((base / "sources" / "install2.swm").is_file())
                self.assertFalse((base / "sources" / "install.wim").exists())
            finally:
                self.media.detach(device)


class AllocationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.allocation = load_module("siliconcamp_allocation", TOOL / "allocation.py")

    def fake_host(self, free_gb: int = 100, capacity_gb: int = 500) -> dict:
        free = free_gb * 10**9
        reserve = max(self.allocation.RESERVE_GB * 10**9, int(free * self.allocation.RESERVE_FRACTION))
        return {
            "container": "disk3",
            "disk": "/dev/disk3",
            "volume": "Macintosh HD",
            "capacity": capacity_gb * 10**9,
            "free": free,
            "reserve": reserve,
            "max_bytes": max(0, free - reserve),
            "max_gb": max(0, free - reserve) // 10**9,
            "minimum_gb": self.allocation.MIN_WINDOWS_GB,
            "source": "test",
        }

    def test_the_reserve_is_a_fifth_of_a_tight_disk(self) -> None:
        host = self.fake_host(free_gb=100)
        self.assertEqual(host["reserve"], 20 * 10**9)
        self.assertEqual(host["max_gb"], 80)

    def test_a_bigger_disk_keeps_a_tenth(self) -> None:
        host = self.fake_host(free_gb=400)
        self.assertEqual(host["reserve"], 40 * 10**9)

    def test_windows_needs_at_least_sixty_four_gigabytes(self) -> None:
        plan = self.allocation.plan_space(32, stats=self.fake_host())
        self.assertFalse(plan["fit"])
        self.assertTrue(any("at least 64" in b for b in plan["blockers"]))

    def test_a_partition_larger_than_the_free_space_is_refused(self) -> None:
        plan = self.allocation.plan_space(200, stats=self.fake_host(free_gb=100))
        self.assertFalse(plan["fit"])
        self.assertTrue(any("can be allocated" in b for b in plan["blockers"]))

    def test_a_fitting_plan_reports_the_partition_and_what_is_left(self) -> None:
        plan = self.allocation.plan_space(64, stats=self.fake_host(free_gb=100))
        self.assertTrue(plan["fit"])
        self.assertEqual(plan["requested_gb"], 64)
        partition = plan["layout"][0]
        self.assertEqual(partition["name"], "BOOTCAMP")
        self.assertEqual(partition["format"], "FAT32")
        self.assertEqual(partition["bytes"], 64 * 10**9)
        self.assertEqual(plan["after"]["free"], 36 * 10**9)

    def test_the_resize_is_disabled_until_windows_can_boot(self) -> None:
        plan = self.allocation.plan_space(64, stats=self.fake_host())
        self.assertFalse(self.allocation.BOOT_PATH_READY)
        self.assertFalse(plan["apply"]["enabled"])
        self.assertTrue(plan["apply"]["reason"])

    def test_the_server_default_leaves_macos_headroom(self) -> None:
        # The slider must not start at the maximum the container could give up.
        server = load_server()
        for ceiling, expected in ((79, 64), (200, 128), (64, 64)):
            preferred = max(server.allocation.MIN_WINDOWS_GB, int(ceiling * 0.75))
            default = min(128, max(server.allocation.MIN_WINDOWS_GB, min(preferred, ceiling)))
            self.assertEqual(default, expected, f"ceiling {ceiling}")

    @unittest.skipUnless(sys.platform == "darwin", "needs diskutil")
    def test_this_mac_reports_its_apfs_container(self) -> None:
        stats = self.allocation.read_host_space()
        self.assertTrue(stats["container"])
        self.assertGreater(stats["capacity"], 0)
        self.assertGreaterEqual(stats["free"], 0)
        self.assertGreater(stats["reserve"], 0)
        self.assertLessEqual(stats["max_bytes"], stats["free"])


class ServerWriteGuardTests(unittest.TestCase):
    """The loopback server must not be drivable by another web page."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server_module = load_server()
        cls.server_module.JOBS = cls.server_module.Jobs()
        port = find_free_port()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", port), cls.server_module.Handler)
        cls.port = port
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def post(self, path: str, body: dict, headers: dict | None = None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_a_write_without_the_token_is_forbidden(self) -> None:
        status, payload = self.post(
            "/api/installer/create",
            {"iso": "/tmp/none.iso", "target": {"kind": "image", "path": "/tmp/x.sparseimage"}},
        )
        self.assertEqual(status, 403)
        self.assertIn("token", payload["error"])

    def test_a_write_from_another_origin_is_forbidden(self) -> None:
        status, payload = self.post(
            "/api/installer/create",
            {"iso": "/tmp/none.iso", "target": {"kind": "image", "path": "/tmp/x.sparseimage"}},
            headers={
                "X-SiliconCamp-Token": self.server_module.TOKEN,
                "Origin": "https://example.com",
            },
        )
        self.assertEqual(status, 403)
        self.assertIn("cross-origin", payload["error"])

    def test_an_unknown_destination_kind_is_rejected(self) -> None:
        status, payload = self.post(
            "/api/installer/plan",
            {"iso": "/tmp/none.iso", "target": {"kind": "network"}},
            headers={"X-SiliconCamp-Token": self.server_module.TOKEN},
        )
        self.assertEqual(status, 400)
        self.assertIn("unknown destination", payload["error"])

    def test_the_page_carries_the_token_and_no_placeholder(self) -> None:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=10) as response:
            html = response.read().decode("utf-8")
        self.assertNotIn("__SILICONCAMP_TOKEN__", html)
        self.assertIn(self.server_module.TOKEN, html)

    def test_the_api_status_endpoint_stays_readable(self) -> None:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/api/status", timeout=10
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(payload["product"], "SiliconCamp Assistant")


if __name__ == "__main__":
    unittest.main()
