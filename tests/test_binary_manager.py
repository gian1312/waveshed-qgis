"""Unit tests for waveshed.core.binary_manager — manifest flow & SHA-256.

Pure logic only: platform detection, asset selection, manifest validation,
and fail-closed checksum verification. No network (QgsBlockingNetworkRequest
is imported lazily and never exercised here).

QGIS stubs provided by conftest.py.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import hashlib
import os
import tempfile
import unittest
from unittest import mock

from waveshed.core import binary_manager as bm


MANIFEST = {
    "schema_version": 1,
    "version": "0.4.2",
    "eula_url": "https://waveshed.io/legal/aether-engine-eula.md",
    "min_plugin_version": "0.1.0",
    "assets": [
        {"platform": "windows", "arch": "x64",
         "filename": "aether-0.4.2-windows-x64.zip", "url": "u-win",
         "size_bytes": 0, "sha256": "ab" * 32},
        {"platform": "linux", "arch": "x64",
         "filename": "aether-0.4.2-linux-x64.zip", "url": "u-lin",
         "size_bytes": 0, "sha256": "cd" * 32},
        {"platform": "macos", "arch": "arm64",
         "filename": "aether-0.4.2-macos-arm64.zip", "url": "u-mac",
         "size_bytes": 0, "sha256": "ef" * 32},
    ],
}


class TestDetectPlatform(unittest.TestCase):
    def _patch(self, system: str, machine: str) -> None:
        p1 = mock.patch.object(bm.platform, "system", return_value=system)
        p2 = mock.patch.object(bm.platform, "machine", return_value=machine)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

    def test_windows_x64(self):
        self._patch("Windows", "AMD64")
        self.assertEqual(bm.detect_platform(), ("windows", "x64"))

    def test_linux_x64(self):
        self._patch("Linux", "x86_64")
        self.assertEqual(bm.detect_platform(), ("linux", "x64"))

    def test_macos_arm64(self):
        self._patch("Darwin", "arm64")
        self.assertEqual(bm.detect_platform(), ("macos", "arm64"))

    def test_intel_macos_dedicated_error(self):
        self._patch("Darwin", "x86_64")
        with self.assertRaises(RuntimeError) as cm:
            bm.detect_platform()
        self.assertIn("Intel macOS is not supported", str(cm.exception))

    def test_unsupported_platform(self):
        self._patch("Linux", "aarch64")
        with self.assertRaises(RuntimeError) as cm:
            bm.detect_platform()
        self.assertIn("Unsupported platform", str(cm.exception))


class TestSelectAsset(unittest.TestCase):
    def test_select_per_platform(self):
        cases = [
            (("windows", "x64"), "aether-0.4.2-windows-x64.zip"),
            (("linux", "x64"), "aether-0.4.2-linux-x64.zip"),
            (("macos", "arm64"), "aether-0.4.2-macos-arm64.zip"),
        ]
        for plat_arch, filename in cases:
            asset = bm.select_asset(MANIFEST, plat_arch)
            self.assertEqual(asset["filename"], filename)

    def test_no_matching_asset_raises(self):
        with self.assertRaises(RuntimeError):
            bm.select_asset(MANIFEST, ("freebsd", "x64"))


class TestValidateManifest(unittest.TestCase):
    def test_valid(self):
        self.assertIs(bm.validate_manifest(MANIFEST), MANIFEST)

    def test_not_a_dict(self):
        with self.assertRaises(RuntimeError):
            bm.validate_manifest([1, 2, 3])

    def test_missing_version(self):
        with self.assertRaises(RuntimeError):
            bm.validate_manifest({"schema_version": 1, "assets": [{}]})

    def test_empty_assets(self):
        with self.assertRaises(RuntimeError):
            bm.validate_manifest({"version": "1", "assets": []})

    def test_unsupported_schema_version(self):
        with self.assertRaises(RuntimeError):
            bm.validate_manifest(
                {"schema_version": 2, "version": "1", "assets": [{}]}
            )


class TestSha256FailClosed(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(prefix="ws_engine_")
        with os.fdopen(fd, "wb") as fh:
            fh.write(b"aether-engine-archive-bytes")
        self.good = hashlib.sha256(b"aether-engine-archive-bytes").hexdigest()

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_matching_digest_passes(self):
        bm.verify_sha256(self.path, self.good)  # must not raise

    def test_missing_or_placeholder_fails(self):
        # None, empty, placeholder text, wrong length, non-hex all fail-closed.
        for bad in (None, "", "PLACEHOLDER", "0" * 63, "z" * 64):
            with self.assertRaises(RuntimeError):
                bm.verify_sha256(self.path, bad)

    def test_mismatched_digest_fails(self):
        with self.assertRaises(RuntimeError):
            bm.verify_sha256(self.path, "0" * 64)

    def test_is_hex_sha256(self):
        self.assertTrue(bm._is_hex_sha256(self.good))
        self.assertFalse(bm._is_hex_sha256(self.good[:-1]))  # 63 chars
        self.assertFalse(bm._is_hex_sha256("z" * 64))
        self.assertFalse(bm._is_hex_sha256(None))


class TestVersionComparison(unittest.TestCase):
    def test_compare_versions(self):
        self.assertEqual(bm.compare_versions("0.1.0", "0.1.0"), 0)
        self.assertEqual(bm.compare_versions("0.1.0", "0.2.0"), -1)
        self.assertEqual(bm.compare_versions("1.0.0", "0.9.9"), 1)
        self.assertEqual(bm.compare_versions("0.1", "0.1.0"), 0)

    def test_is_plugin_outdated(self):
        self.assertFalse(bm.is_plugin_outdated(None))
        self.assertFalse(bm.is_plugin_outdated(""))
        self.assertFalse(bm.is_plugin_outdated("0.0.1"))
        self.assertTrue(bm.is_plugin_outdated("99.0.0"))


class TestClearQuarantine(unittest.TestCase):
    """Post-extraction un-quarantine step (best-effort, never fails install)."""

    def _patch_system(self, system: str) -> None:
        p = mock.patch.object(bm.platform, "system", return_value=system)
        p.start()
        self.addCleanup(p.stop)

    # --- macOS ---------------------------------------------------------------

    def test_macos_runs_xattr_cr_on_target_dir(self):
        self._patch_system("Darwin")
        with mock.patch.object(bm.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
            bm._clear_quarantine("/install/dir", ["/install/dir/aether_core"])

        run.assert_called_once()
        argv, kwargs = run.call_args
        self.assertEqual(argv[0], ["xattr", "-cr", "/install/dir"])
        self.assertEqual(kwargs.get("timeout"), 10)

    def test_macos_missing_xattr_does_not_raise(self):
        self._patch_system("Darwin")
        # xattr binary absent -> subprocess.run raises FileNotFoundError.
        with mock.patch.object(
            bm.subprocess, "run", side_effect=FileNotFoundError("xattr")
        ):
            bm._clear_quarantine("/install/dir", [])  # must not raise

    def test_macos_nonzero_exit_does_not_raise(self):
        self._patch_system("Darwin")
        with mock.patch.object(bm.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="nope")
            bm._clear_quarantine("/install/dir", [])  # must not raise

    def test_macos_timeout_does_not_raise(self):
        self._patch_system("Darwin")
        with mock.patch.object(
            bm.subprocess,
            "run",
            side_effect=bm.subprocess.TimeoutExpired("xattr", 10),
        ):
            bm._clear_quarantine("/install/dir", [])  # must not raise

    # --- Windows -------------------------------------------------------------

    def test_windows_removes_ads_per_file(self):
        self._patch_system("Windows")
        files = [
            r"C:\bin\aether_core.exe",
            r"C:\bin\aether_converter.exe",
            r"C:\bin\aether_export.exe",
        ]
        with mock.patch.object(bm.subprocess, "run") as run, \
                mock.patch.object(bm.os, "remove") as remove:
            bm._clear_quarantine(r"C:\bin", files)

        self.assertEqual(remove.call_count, len(files))
        remove.assert_has_calls(
            [mock.call(f"{f}:Zone.Identifier") for f in files],
            any_order=True,
        )
        run.assert_not_called()  # Windows must not shell out to xattr

    def test_windows_swallows_filenotfound_and_oserror(self):
        self._patch_system("Windows")
        files = [r"C:\bin\aether_core.exe", r"C:\bin\aether_export.exe"]
        with mock.patch.object(
            bm.os, "remove", side_effect=FileNotFoundError
        ) as remove:
            bm._clear_quarantine(r"C:\bin", files)  # must not raise
        self.assertEqual(remove.call_count, len(files))  # continues past errors

        with mock.patch.object(bm.os, "remove", side_effect=OSError):
            bm._clear_quarantine(r"C:\bin", files)  # must not raise

    # --- Linux ---------------------------------------------------------------

    def test_linux_is_a_noop(self):
        self._patch_system("Linux")
        with mock.patch.object(bm.subprocess, "run") as run, \
                mock.patch.object(bm.os, "remove") as remove:
            bm._clear_quarantine("/opt/aether/bin", ["/opt/aether/bin/aether_core"])

        run.assert_not_called()
        remove.assert_not_called()


if __name__ == "__main__":
    unittest.main()
