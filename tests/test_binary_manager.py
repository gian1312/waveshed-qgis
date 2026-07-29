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


class TestReadMachineFingerprint(unittest.TestCase):
    """`aether_core --fingerprint` wrapper (subprocess factored out for tests)."""

    _FP = "ab" * 32  # 64-char lowercase hex

    def _run(self, **run_kwargs):
        """Patch find_binary + subprocess.run; return the patched run mock."""
        fb = mock.patch.object(bm, "find_binary", return_value="/bin/aether_core")
        run = mock.patch.object(bm.subprocess, "run", **run_kwargs)
        fb.start()
        r = run.start()
        self.addCleanup(fb.stop)
        self.addCleanup(run.stop)
        return r

    def test_success_returns_stripped_fingerprint(self):
        run = self._run()
        run.return_value = mock.Mock(returncode=0, stdout=self._FP + "\n", stderr="")

        result = bm.read_machine_fingerprint()

        self.assertEqual(result, self._FP)
        argv, kwargs = run.call_args
        self.assertEqual(argv[0], ["/bin/aether_core", "--fingerprint"])
        self.assertEqual(kwargs.get("timeout"), 10.0)
        self.assertTrue(kwargs.get("capture_output"))

    def test_custom_timeout_is_passed_through(self):
        run = self._run()
        run.return_value = mock.Mock(returncode=0, stdout=self._FP, stderr="")
        bm.read_machine_fingerprint(timeout=2.5)
        _argv, kwargs = run.call_args
        self.assertEqual(kwargs.get("timeout"), 2.5)

    def test_uppercase_hex_is_rejected(self):
        run = self._run()
        run.return_value = mock.Mock(returncode=0, stdout=self._FP.upper(), stderr="")
        with self.assertRaises(RuntimeError):
            bm.read_machine_fingerprint()

    def test_wrong_length_output_is_rejected(self):
        run = self._run()
        run.return_value = mock.Mock(returncode=0, stdout="deadbeef", stderr="")
        with self.assertRaises(RuntimeError):
            bm.read_machine_fingerprint()

    def test_nonzero_exit_raises_with_detail(self):
        run = self._run()
        run.return_value = mock.Mock(returncode=3, stdout="", stderr="kaboom")
        with self.assertRaises(RuntimeError) as cm:
            bm.read_machine_fingerprint()
        self.assertIn("kaboom", str(cm.exception))

    def test_timeout_raises_runtime_error(self):
        self._run(side_effect=bm.subprocess.TimeoutExpired("aether_core", 10))
        with self.assertRaises(RuntimeError) as cm:
            bm.read_machine_fingerprint()
        self.assertIn("timed out", str(cm.exception))

    def test_os_error_raises_runtime_error(self):
        self._run(side_effect=OSError("EACCES"))
        with self.assertRaises(RuntimeError) as cm:
            bm.read_machine_fingerprint()
        self.assertIn("Could not run", str(cm.exception))

    def test_binary_not_found_raises(self):
        # find_binary raises when the engine is not installed -> propagates.
        with mock.patch.object(
            bm, "find_binary", side_effect=RuntimeError("not found")
        ):
            with self.assertRaises(RuntimeError):
                bm.read_machine_fingerprint()

    def test_old_engine_missing_flag_suggests_update(self):
        # An engine built before --fingerprint existed: clap exits 2 and
        # complains about the unknown argument. The user needs to update, not
        # to read a raw "exited 2".
        run = self._run()
        run.return_value = mock.Mock(
            returncode=2,
            stdout="",
            stderr="error: unexpected argument '--fingerprint' found\n"
                   "For more information, try '--help'.",
        )
        with self.assertRaises(RuntimeError) as cm:
            bm.read_machine_fingerprint()
        msg = str(cm.exception)
        self.assertIn("v0.4.2", msg)
        self.assertIn("update", msg.lower())
        self.assertNotIn("exited with code", msg)  # not the raw generic error

    def test_compute_error_is_surfaced_not_reported_as_too_old(self):
        # The current engine understands --fingerprint but cannot compute one
        # (exit 1, [E:...] on stderr). Surface that message; do NOT tell the
        # user to update a binary that is not the problem.
        run = self._run()
        run.return_value = mock.Mock(
            returncode=1,
            stdout="",
            stderr="[E:could not compute machine fingerprint: no /etc/machine-id]",
        )
        with self.assertRaises(RuntimeError) as cm:
            bm.read_machine_fingerprint()
        msg = str(cm.exception)
        self.assertIn("could not compute machine fingerprint", msg)
        self.assertNotIn("v0.4.2", msg)
        self.assertNotIn("update", msg.lower())

    def test_happy_path_still_returns_hex(self):
        # Newest engine: 64-hex on stdout, a hint on stderr, exit 0.
        run = self._run()
        run.return_value = mock.Mock(
            returncode=0, stdout=self._FP + "\n", stderr="(send this to get a license)"
        )
        self.assertEqual(bm.read_machine_fingerprint(), self._FP)


class TestFingerprintErrorMessage(unittest.TestCase):
    """Direct unit tests for the pure classifier (no subprocess involved)."""

    _UPDATE = "v0.4.2"  # marker of the "engine too old" message

    def test_too_old_signatures_all_suggest_update(self):
        cases = [
            # (returncode, stdout, stderr)
            (2, "", "error: unexpected argument '--fingerprint' found"),
            (2, "", "For more information, try '--help'."),
            (1, "", "error: unexpected argument '--fingerprint' found"),
            (1, "", "error: Found argument '--fingerprint' which wasn't expected"),
            (1, "", "unrecognized option '--fingerprint'"),
            (1, "", "run with '--fingerprint --help' to see usage"),
        ]
        for rc, out, err in cases:
            with self.subTest(returncode=rc, stderr=err):
                msg = bm._fingerprint_error_message(rc, out, err)
                self.assertIn(self._UPDATE, msg)
                self.assertIn("update", msg.lower())

    def test_compute_error_is_not_classified_too_old(self):
        # Exit 1 [E:...] compute failure must keep its own surfaced message.
        msg = bm._fingerprint_error_message(
            1, "", "[E:could not compute machine fingerprint: no /etc/machine-id]"
        )
        self.assertIn("could not compute machine fingerprint", msg)
        self.assertNotIn(self._UPDATE, msg)
        self.assertNotIn("update", msg.lower())

    def test_generic_nonzero_preserves_detail(self):
        msg = bm._fingerprint_error_message(3, "", "kaboom")
        self.assertIn("exited with code 3", msg)
        self.assertIn("kaboom", msg)
        self.assertNotIn(self._UPDATE, msg)


if __name__ == "__main__":
    unittest.main()
