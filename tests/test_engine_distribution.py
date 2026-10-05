"""Engine distribution: manifest signature, host pin, staged install, macOS prep,
update check and the notices that point the user at Settings.

The signing helper below is test-only (the plugin verifies, never signs); it is
itself pinned against RFC 8032 test vector 1, so a broken helper cannot make the
signature tests pass by construction.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import base64
import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
import zipfile
from unittest import mock

from qgis.core import QgsSettings

from waveshed.core import binary_manager as bm
from waveshed.core import ed25519

# --------------------------------------------------------------------------
# Test-only Ed25519 signer (RFC 8032), checked against vector 1 below.
# --------------------------------------------------------------------------

RFC1_SECRET = bytes.fromhex(
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
RFC1_PUBLIC = bytes.fromhex(
    "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
RFC1_SIG = bytes.fromhex(
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555"
    "fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")


def _compress(p):
    zinv = pow(p[2], ed25519._P - 2, ed25519._P)
    x, y = p[0] * zinv % ed25519._P, p[1] * zinv % ed25519._P
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _expand(seed):
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def _public(seed):
    return _compress(ed25519._pt_mul(_expand(seed)[0], ed25519._G))


def _sign(seed, msg):
    a, prefix = _expand(seed)
    pub = _compress(ed25519._pt_mul(a, ed25519._G))
    r = ed25519._h_modq(prefix + msg)
    rs = _compress(ed25519._pt_mul(r, ed25519._G))
    s = (r + ed25519._h_modq(rs + pub + msg) * a) % ed25519._Q
    return rs + int.to_bytes(s, 32, "little")


SEED = hashlib.sha256(b"waveshed test manifest key").digest()
PUB_HEX = _public(SEED).hex()
OTHER_SEED = hashlib.sha256(b"someone else").digest()


class TestEd25519(unittest.TestCase):
    def test_rfc8032_vector_1_verifies(self):
        self.assertTrue(ed25519.verify(RFC1_PUBLIC, b"", RFC1_SIG))

    def test_test_signer_reproduces_vector_1(self):
        self.assertEqual(_public(RFC1_SECRET), RFC1_PUBLIC)
        self.assertEqual(_sign(RFC1_SECRET, b""), RFC1_SIG)

    def test_tampered_message_signature_and_key_fail(self):
        self.assertFalse(ed25519.verify(RFC1_PUBLIC, b"x", RFC1_SIG))
        bad = bytearray(RFC1_SIG)
        bad[5] ^= 1
        self.assertFalse(ed25519.verify(RFC1_PUBLIC, b"", bytes(bad)))
        self.assertFalse(ed25519.verify(_public(SEED), b"", RFC1_SIG))

    def test_wrong_lengths_and_non_canonical_s_fail(self):
        self.assertFalse(ed25519.verify(RFC1_PUBLIC[:31], b"", RFC1_SIG))
        self.assertFalse(ed25519.verify(RFC1_PUBLIC, b"", RFC1_SIG[:63]))
        s_too_big = RFC1_SIG[:32] + int.to_bytes(ed25519._Q, 32, "little")
        self.assertFalse(ed25519.verify(RFC1_PUBLIC, b"", s_too_big))


# --------------------------------------------------------------------------
# Manifest signature
# --------------------------------------------------------------------------

def _manifest(version="0.4.8"):
    return {
        "schema_version": 1, "version": version,
        "assets": [{"platform": "linux", "arch": "x64",
                    "url": f"https://releases.waveshed.io/v{version}/a.zip",
                    "sha256": "ab" * 32, "size_bytes": 1}],
    }


def _raw(manifest):
    return (json.dumps(manifest, indent=2) + "\n").encode("utf-8")


def _sig_doc(raw, seed=SEED, context="waveshed-manifest-v1"):
    sig = _sign(seed, bm.MANIFEST_SIG_CONTEXT + raw)
    return json.dumps({"alg": "ed25519", "context": context,
                       "public_key": _public(seed).hex(),
                       "signature": base64.b64encode(sig).decode()}).encode()


class _Fetch:
    """Fake HTTP: url -> (status, body, error)."""

    def __init__(self, routes):
        self.routes = routes
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        return self.routes.get(url, (None, b"", "connection refused"))


def _sig_url(version):
    return bm.MANIFEST_SIG_URL.format(version=version)


class TestManifestSignature(unittest.TestCase):
    def test_valid_signature_by_trusted_key_is_accepted(self):
        m = _manifest()
        raw = _raw(m)
        fetch = _Fetch({_sig_url("0.4.8"): (200, _sig_doc(raw), "")})
        self.assertEqual(bm.verify_manifest_signature(raw, m, [PUB_HEX], fetch), "signed")
        self.assertEqual(fetch.urls,
                         ["https://releases.waveshed.io/manifests/v0.4.8/latest.json.sig"])

    def test_a_single_changed_byte_is_refused(self):
        m = _manifest()
        raw = _raw(m)
        fetch = _Fetch({_sig_url("0.4.8"): (200, _sig_doc(raw), "")})
        with self.assertRaisesRegex(RuntimeError, "does not match"):
            bm.verify_manifest_signature(raw + b" ", m, [PUB_HEX], fetch)

    def test_untrusted_key_is_refused(self):
        m = _manifest()
        raw = _raw(m)
        fetch = _Fetch({_sig_url("0.4.8"): (200, _sig_doc(raw, OTHER_SEED), "")})
        with self.assertRaisesRegex(RuntimeError, "does not trust"):
            bm.verify_manifest_signature(raw, m, [PUB_HEX], fetch)

    def test_no_trusted_keys_refuses_every_signed_manifest(self):
        m = _manifest()
        raw = _raw(m)
        fetch = _Fetch({_sig_url("0.4.8"): (200, _sig_doc(raw), "")})
        with self.assertRaisesRegex(RuntimeError, "does not trust"):
            bm.verify_manifest_signature(raw, m, [], fetch)

    def test_malformed_signature_files_are_refused(self):
        m = _manifest()
        raw = _raw(m)
        bodies = [b"not json", b"[]", _sig_doc(raw, context="other"),
                  json.dumps({"alg": "ed25519", "context": "waveshed-manifest-v1",
                              "public_key": PUB_HEX, "signature": "@@"}).encode()]
        for body in bodies:
            fetch = _Fetch({_sig_url("0.4.8"): (200, body, "")})
            with self.assertRaisesRegex(RuntimeError, "malformed"):
                bm.verify_manifest_signature(raw, m, [PUB_HEX], fetch)

    def test_missing_signature_is_accepted_only_up_to_the_last_unsigned_release(self):
        old = _manifest(bm.LAST_UNSIGNED_ENGINE)
        fetch = _Fetch({_sig_url(bm.LAST_UNSIGNED_ENGINE): (404, b"", "Not Found")})
        self.assertEqual(
            bm.verify_manifest_signature(_raw(old), old, [PUB_HEX], fetch),
            "unsigned-legacy")
        new = _manifest("0.4.8")
        fetch = _Fetch({_sig_url("0.4.8"): (404, b"", "Not Found")})
        with self.assertRaisesRegex(RuntimeError, "no signature"):
            bm.verify_manifest_signature(_raw(new), new, [PUB_HEX], fetch)

    def test_network_error_on_the_signature_refuses_with_try_again(self):
        m = _manifest()
        with self.assertRaisesRegex(RuntimeError, "try again"):
            bm.verify_manifest_signature(_raw(m), m, [PUB_HEX], _Fetch({}))

    def test_a_non_semver_version_never_reaches_the_network(self):
        for version in ("0.4.8/../../x", "latest", "", "1.2"):
            m = _manifest()
            m["version"] = version
            fetch = _Fetch({})
            with self.assertRaisesRegex(RuntimeError, "X.Y.Z"):
                bm.verify_manifest_signature(_raw(m), m, [PUB_HEX], fetch)
            self.assertEqual(fetch.urls, [])

    def test_fetch_manifest_checks_the_exact_bytes_it_received(self):
        m = _manifest()
        raw = _raw(m)
        routes = {bm.MANIFEST_URL: (200, raw, ""),
                  _sig_url("0.4.8"): (200, _sig_doc(raw), "")}
        with mock.patch("waveshed.core.release_keys.MANIFEST_PUBLIC_KEYS", (PUB_HEX,)):
            self.assertEqual(bm.fetch_manifest(fetch=_Fetch(routes))["version"], "0.4.8")
            # Same JSON, different bytes (re-serialised): the signature no longer holds.
            routes[bm.MANIFEST_URL] = (200, json.dumps(m).encode(), "")
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                bm.fetch_manifest(fetch=_Fetch(routes))

    def test_fetch_manifest_network_failure(self):
        with self.assertRaisesRegex(RuntimeError, "Failed to fetch"):
            bm.fetch_manifest(fetch=_Fetch({}))


# --------------------------------------------------------------------------
# Staged install
# --------------------------------------------------------------------------

def _engine_zip(path, version="0.4.8", names=bm.REQUIRED_BINARIES, extra=None):
    script = f"#!/bin/sh\necho 'aether_core {version}'\n"
    with zipfile.ZipFile(path, "w") as zf:
        for name in names:
            body = script if name == "aether_core" else f"#!/bin/sh\necho '{name} 1.0.0'\n"
            zf.writestr(name + bm._EXE_SUFFIX, body)
        for arc, body in (extra or {}).items():
            zf.writestr(arc, body)
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


@unittest.skipIf(os.name == "nt", "fake engines are POSIX shell scripts")
class TestStagedInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.target = os.path.join(self.tmp, "bin")
        os.makedirs(self.target)
        self.zip = os.path.join(self.tmp, "src.zip")
        p = mock.patch.object(QgsSettings, "_store", {})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(bm, "check_prerequisites", return_value=[])
        p.start()
        self.addCleanup(p.stop)

    def _old_install(self):
        for name in bm.REQUIRED_BINARIES:
            with open(os.path.join(self.target, name), "w") as fh:
                fh.write("OLD " + name)
        with open(os.path.join(self.target, "license.key"), "w") as fh:
            fh.write("KEY")

    def _snapshot(self):
        out = {}
        for root, _d, files in os.walk(self.target):
            for f in files:
                p = os.path.join(root, f)
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, self.target)] = fh.read()
        return out

    def _install(self, sha, version="0.4.8", url=None):
        def fake_download(u, dest, progress_cb=None):
            shutil.copyfile(self.zip, dest)
        asset = {"url": url or f"https://releases.waveshed.io/v{version}/a.zip",
                 "sha256": sha}
        with mock.patch.object(bm, "_download_to_file", side_effect=fake_download):
            return bm.download_engine({"version": version}, asset, target_dir=self.target)

    def test_update_swaps_in_new_binaries_and_keeps_other_files(self):
        self._old_install()
        sha = _engine_zip(self.zip, extra={"THIRD_PARTY/NOTICE.txt": "n"})
        self.assertEqual(self._install(sha), self.target)
        snap = self._snapshot()
        self.assertIn(b"aether_core 0.4.8", snap["aether_core"])
        self.assertEqual(snap["license.key"], b"KEY")
        self.assertIn(os.path.join("THIRD_PARTY", "NOTICE.txt"), snap)
        self.assertTrue(os.stat(os.path.join(self.target, "aether_core")).st_mode & stat.S_IXUSR)
        leftovers = [f for f in os.listdir(self.target)
                     if f.startswith(bm.STAGING_PREFIX) or bm.BACKUP_INFIX in f]
        self.assertEqual(leftovers, [])
        self.assertEqual(QgsSettings().value("waveshed/installed_engine_version"), "0.4.8")
        self.assertEqual(QgsSettings().value("waveshed/binary_dir"), self.target)

    def _assert_untouched(self, before):
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(os.listdir(self.target).__len__(), len(before))
        self.assertIsNone(QgsSettings().value("waveshed/installed_engine_version"))

    def test_version_mismatch_leaves_the_old_install_intact(self):
        self._old_install()
        before = self._snapshot()
        sha = _engine_zip(self.zip, version="0.4.6")
        with self.assertRaisesRegex(RuntimeError, "promised 0.4.8"):
            self._install(sha)
        self._assert_untouched(before)

    def test_incomplete_archive_leaves_the_old_install_intact(self):
        self._old_install()
        before = self._snapshot()
        sha = _engine_zip(self.zip, names=("aether_core", "aether_export"))
        with self.assertRaisesRegex(RuntimeError, "aether_converter"):
            self._install(sha)
        self._assert_untouched(before)

    def test_engine_that_cannot_run_is_not_installed(self):
        self._old_install()
        before = self._snapshot()
        with zipfile.ZipFile(self.zip, "w") as zf:
            for name in bm.REQUIRED_BINARIES:
                zf.writestr(name, "#!/bin/sh\nexit 3\n")
        with open(self.zip, "rb") as fh:
            sha = hashlib.sha256(fh.read()).hexdigest()
        with self.assertRaisesRegex(RuntimeError, "did not answer --version"):
            self._install(sha)
        self._assert_untouched(before)

    def test_failure_during_the_swap_rolls_back(self):
        self._old_install()
        before = self._snapshot()
        sha = _engine_zip(self.zip)
        real_replace = os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            if bm.STAGING_PREFIX in str(src):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise PermissionError("simulated lock")
            return real_replace(src, dst)

        with mock.patch.object(bm.os, "replace", side_effect=flaky_replace):
            with self.assertRaisesRegex(RuntimeError, "previous engine was restored"):
                self._install(sha)
        self._assert_untouched(before)

    def test_archive_from_another_host_is_refused_before_download(self):
        sha = _engine_zip(self.zip)
        with mock.patch.object(bm, "_download_to_file") as dl:
            with self.assertRaisesRegex(RuntimeError, "only accepted from"):
                bm.download_engine({"version": "0.4.8"},
                                   {"url": "https://evil.example/a.zip", "sha256": sha},
                                   target_dir=self.target)
            dl.assert_not_called()

    def test_windows_locked_binary_is_refused_untouched(self):
        self._old_install()
        before = self._snapshot()
        staging = tempfile.mkdtemp(dir=self.target, prefix=bm.STAGING_PREFIX)
        with open(os.path.join(staging, "aether_core"), "w") as fh:
            fh.write("NEW")
        real_open = open

        def locked_open(path, mode="r", *a, **kw):
            if mode == "r+b" and str(path).endswith("aether_core"):
                raise PermissionError(32, "being used by another process")
            return real_open(path, mode, *a, **kw)

        with mock.patch.object(bm.platform, "system", return_value="Windows"), \
                mock.patch("builtins.open", side_effect=locked_open):
            with self.assertRaisesRegex(RuntimeError, "in use"):
                bm._install_staged(staging, self.target)
        shutil.rmtree(staging)
        self.assertEqual(self._snapshot(), before)

    def test_stale_backups_and_staging_dirs_are_cleaned_up(self):
        os.makedirs(os.path.join(self.target, bm.STAGING_PREFIX + "x"))
        stale = os.path.join(self.target, "aether_core" + bm.BACKUP_INFIX + "dead")
        with open(stale, "w") as fh:
            fh.write("x")
        bm._cleanup_stale_backups(self.target)
        self.assertEqual(os.listdir(self.target), [])


# --------------------------------------------------------------------------
# macOS preparation and hints
# --------------------------------------------------------------------------

class TestMacosPreparation(unittest.TestCase):
    def test_darwin_sets_exec_bit_and_clears_quarantine(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "aether_core")
            with open(path, "w") as fh:
                fh.write("x")
            os.chmod(path, 0o644)
            with mock.patch.object(bm.platform, "system", return_value="Darwin"), \
                    mock.patch.object(bm.subprocess, "run") as run:
                bm.prepare_engine_dir(d)
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0], ["xattr", "-cr", d])
            self.assertTrue(os.stat(path).st_mode & stat.S_IXUSR)

    def test_other_platforms_and_missing_dirs_do_nothing(self):
        with mock.patch.object(bm.subprocess, "run") as run:
            with mock.patch.object(bm.platform, "system", return_value="Linux"):
                bm.prepare_engine_dir("/tmp")
            with mock.patch.object(bm.platform, "system", return_value="Darwin"):
                bm.prepare_engine_dir("/no/such/dir")
                bm.prepare_engine_dir(None)
        run.assert_not_called()

    def test_probe_prepares_the_dir_before_launching(self):
        order = []
        with mock.patch.object(bm, "prepare_engine_dir",
                               side_effect=lambda d: order.append("prep")), \
                mock.patch.object(bm.subprocess, "run",
                                  side_effect=lambda *a, **k: order.append("run") or
                                  mock.Mock(returncode=0, stdout="aether_core 0.4.8\n")):
            self.assertEqual(bm.probe_engine_version("/x"), "0.4.8")
        self.assertEqual(order, ["prep", "run"])

    def test_gatekeeper_kill_gets_the_xattr_command(self):
        with mock.patch.object(bm.platform, "system", return_value="Darwin"):
            hint = bm.engine_error_hint("zsh: killed: 9  aether_core")
            self.assertIn('xattr -cr "', hint)
            self.assertEqual(bm.macos_launch_hint("ordinary failure", "/b"), "")
            self.assertIn('xattr -cr "/b"', bm.macos_launch_hint("", "/b", force=True))
        with mock.patch.object(bm.platform, "system", return_value="Linux"):
            self.assertEqual(bm.macos_launch_hint("killed: 9", "/b", force=True), "")


class TestEngineMessageAction(unittest.TestCase):
    def test_existing_messages_map_to_the_right_button(self):
        with mock.patch.object(bm, "read_engine_version", return_value="0.4.2"), \
                mock.patch.object(bm, "find_binary", return_value="/x/aether_core"):
            with self.assertRaises(bm.EngineTooOldError) as ctx:
                bm.check_engine_for_job(90)
        self.assertEqual(bm.engine_message_action(str(ctx.exception)), "update")
        self.assertEqual(bm.engine_message_action(
            bm.engine_error_hint("[E:Unsupported resolution 90m]")), "update")
        with mock.patch.object(bm, "missing_binaries", return_value=["aether_core"]):
            self.assertEqual(bm.engine_message_action(bm.binaries_warning()), "install")
        self.assertIsNone(bm.engine_message_action("Terrain download failed"))


# --------------------------------------------------------------------------
# Update check
# --------------------------------------------------------------------------

class TestUpdateCheck(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(QgsSettings, "_store", {})
        p.start()
        self.addCleanup(p.stop)

    def _check(self, installed, available="0.4.8"):
        with mock.patch.object(bm, "discover_binary_dir", return_value="/e"), \
                mock.patch.object(bm, "probe_engine_version", return_value=installed), \
                mock.patch.object(bm, "fetch_manifest", return_value=_manifest(available)):
            return bm.check_for_engine_update()

    def test_newer_release_is_reported_once(self):
        info = self._check("0.4.7")
        self.assertTrue(info.update_available)
        self.assertTrue(bm.should_notify_update(info))
        bm.mark_update_notified(info.available)
        self.assertFalse(bm.should_notify_update(info))
        self.assertTrue(bm.should_notify_update(self._check("0.4.7", "0.4.9")))

    def test_same_or_older_release_is_not_an_update(self):
        self.assertFalse(self._check("0.4.8").update_available)
        self.assertFalse(self._check("0.5.0").update_available)

    def test_unknown_installed_version_falls_back_then_counts_as_old(self):
        QgsSettings().setValue("waveshed/installed_engine_version", "0.4.8")
        self.assertFalse(self._check(None).update_available)
        QgsSettings._store.clear()
        info = self._check(None)
        self.assertIsNone(info.installed)
        self.assertTrue(info.update_available)

    def test_no_engine_means_no_update_check(self):
        with mock.patch.object(bm, "discover_binary_dir", return_value=None), \
                mock.patch.object(bm, "fetch_manifest") as fm:
            self.assertIsNone(bm.check_for_engine_update())
        fm.assert_not_called()

    def test_auto_check_setting(self):
        self.assertTrue(bm.auto_update_check_enabled())
        for off in (False, "false", "0"):
            QgsSettings().setValue(bm.UPDATE_CHECK_KEY, off)
            self.assertFalse(bm.auto_update_check_enabled())
        QgsSettings().setValue(bm.UPDATE_CHECK_KEY, "true")
        self.assertTrue(bm.auto_update_check_enabled())


# --------------------------------------------------------------------------
# Notices (first run, update, run errors)
# --------------------------------------------------------------------------

from waveshed.gui import engine_notices as en  # noqa: E402


class _Bar:
    def __init__(self):
        self.pushed = []

    def createMessage(self, title, text):
        widget = mock.Mock()
        widget.text = text
        return widget

    def pushWidget(self, widget, level, duration):
        self.pushed.append(widget)


class _Button:
    instances = []

    def __init__(self, parent=None):
        self.text = None
        self.slots = []
        self.clicked = mock.Mock()
        self.clicked.connect.side_effect = self.slots.append
        _Button.instances.append(self)

    def setText(self, t):
        self.text = t


class TestEngineNotices(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(QgsSettings, "_store", {})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(en, "QPushButton", _Button)
        p.start()
        self.addCleanup(p.stop)
        _Button.instances.clear()
        self.bar = _Bar()
        self.iface = mock.Mock()
        self.iface.messageBar.return_value = self.bar
        self.opened = []
        self.notices = en.EngineNotices(self.iface, self.opened.append)

    def test_first_run_without_engine_points_at_settings_and_downloads_nothing(self):
        with mock.patch.object(bm, "discover_binary_dir", return_value=None), \
                mock.patch.object(bm, "download_engine") as dl, \
                mock.patch.object(en, "_UpdateCheckThread") as thread:
            self.notices.run_startup_checks()
        self.assertEqual(len(self.bar.pushed), 1)
        self.assertIn("needs the Aether engine", self.bar.pushed[0].text)
        self.assertEqual(_Button.instances[0].text, "Open Settings")
        _Button.instances[0].slots[0]()
        self.assertEqual(self.opened, [False])
        dl.assert_not_called()
        thread.assert_not_called()

    def test_installed_engine_starts_the_update_check_unless_disabled(self):
        with mock.patch.object(bm, "discover_binary_dir", return_value="/e"), \
                mock.patch.object(en.threading, "Thread"), \
                mock.patch.object(en, "_UpdateCheckThread") as thread:
            self.notices.run_startup_checks()
            thread.return_value.start.assert_called_once()
            thread.reset_mock()
            QgsSettings().setValue(bm.UPDATE_CHECK_KEY, False)
            self.notices.run_startup_checks()
            thread.assert_not_called()
        self.assertEqual(self.bar.pushed, [])

    def test_start_schedules_once_and_never_runs_inline(self):
        with mock.patch.object(en, "QTimer") as timer, \
                mock.patch.object(self.notices, "run_startup_checks") as run:
            self.notices.start()
            self.notices.start()
        timer.singleShot.assert_called_once()
        run.assert_not_called()

    def test_update_notice_once_per_release_and_button_starts_download_flow(self):
        info = bm.EngineUpdateInfo("0.4.7", "0.4.8", True, {})
        self.notices.on_update_info(info)
        self.notices.on_update_info(info)
        self.assertEqual(len(self.bar.pushed), 1)
        self.assertIn("0.4.8 is available", self.bar.pushed[0].text)
        self.assertEqual(_Button.instances[0].text, "Update")
        _Button.instances[0].slots[0]()
        self.assertEqual(self.opened, [True])
        self.notices.on_update_info(None)
        self.notices.on_update_info(bm.EngineUpdateInfo("0.4.8", "0.4.8", False, {}))
        self.assertEqual(len(self.bar.pushed), 1)


class _Host:
    def __init__(self):
        self.calls = []

    def parent(self):
        return None

    def show_engine_settings(self, start_download=False):
        self.calls.append(start_download)


class _Child:
    def __init__(self, parent):
        self._parent = parent

    def parent(self):
        return self._parent


class TestShowRunError(unittest.TestCase):
    def _run(self, message, click_go=True, host=True):
        h = _Host()
        widget = _Child(_Child(h) if host else None)
        box = mock.Mock()
        go = object()
        box.addButton.side_effect = lambda *a: go if isinstance(a[0], str) else object()
        box.clickedButton.return_value = go if click_go else None
        with mock.patch.object(en, "QMessageBox") as mb:
            mb.return_value = box
            en.show_run_error(widget, "Analysis Failed", message)
        return h, box, mb

    def test_too_old_engine_offers_update_that_starts_the_download_flow(self):
        h, box, mb = self._run("... Update it via Settings → Download binaries ...")
        self.assertEqual(box.addButton.call_args_list[0].args[0], "Update engine")
        self.assertEqual(h.calls, [True])

    def test_missing_engine_offers_open_settings(self):
        h, box, _ = self._run('Open Settings and use "Download Binaries".')
        self.assertEqual(box.addButton.call_args_list[0].args[0], "Open Settings")
        self.assertEqual(h.calls, [False])

    def test_close_does_nothing(self):
        h, _, _ = self._run("Update via Settings → Download binaries", click_go=False)
        self.assertEqual(h.calls, [])

    def test_other_errors_and_hostless_widgets_get_a_plain_box(self):
        for message, host in (("Terrain download failed", True),
                              ("Update via Settings → Download binaries", False)):
            h, box, mb = self._run(message, host=host)
            mb.critical.assert_called_once()
            mb.assert_not_called()
            self.assertEqual(h.calls, [])


# --------------------------------------------------------------------------
# Settings dialog / main dialog / plugin wiring
# --------------------------------------------------------------------------

from waveshed.gui.settings_dialog import SettingsDialog  # noqa: E402
from waveshed.gui.main_dialog import AetherMainDialog  # noqa: E402
import qgis.core as _qgis_core  # noqa: E402

if not hasattr(_qgis_core, "QgsApplication"):  # plugin.py imports it; stubs lack it
    _qgis_core.QgsApplication = mock.MagicMock()
import qgis.PyQt.QtWidgets as _qtw  # noqa: E402

if not hasattr(_qtw, "QAction"):  # Qt5 location; the stubs have neither
    _qtw.QAction = mock.MagicMock()
from waveshed.plugin import AetherPlugin  # noqa: E402


class _Label:
    def __init__(self):
        self.text_value = ""
        self.style = ""

    def setText(self, t):
        self.text_value = t

    def setStyleSheet(self, s):
        self.style = s


class _Edit:
    def __init__(self, t=""):
        self.t = t

    def text(self):
        return self.t

    def setText(self, t):
        self.t = t


class TestSettingsDialogEngineVersion(unittest.TestCase):
    def _dlg(self, binary_dir):
        dlg = SettingsDialog.__new__(SettingsDialog)
        dlg._binary_dir_edit = _Edit(binary_dir)
        dlg._engine_version_label = _Label()
        dlg._available_version = None
        dlg._btn_check_update = mock.Mock()
        return dlg

    def test_label_shows_installed_and_latest(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "aether_core" + bm._EXE_SUFFIX), "w").close()
            dlg = self._dlg(d)
            with mock.patch.object(bm, "probe_engine_version", return_value="0.4.7"):
                dlg._refresh_engine_version()
                self.assertIn("Installed engine: 0.4.7", dlg._engine_version_label.text_value)
                self.assertIn("not checked", dlg._engine_version_label.text_value)
                dlg._on_update_info(_manifest("0.4.8"), {}, "")
                self.assertIn("Latest release: 0.4.8", dlg._engine_version_label.text_value)
                self.assertIn("update available", dlg._engine_version_label.text_value)
                dlg._available_version = "0.4.7"
                dlg._refresh_engine_version()
                self.assertNotIn("update available", dlg._engine_version_label.text_value)

    def test_start_engine_download_runs_the_consent_flow(self):
        dlg = self._dlg("")
        with mock.patch.object(SettingsDialog, "_download_binaries") as flow:
            dlg.start_engine_download()
        flow.assert_called_once_with()

    def test_browse_and_auto_detect_prepare_the_dir(self):
        dlg = self._dlg("")
        dlg._refresh_binary_status = lambda: None
        with mock.patch.object(bm, "prepare_engine_dir") as prep, \
                mock.patch("waveshed.gui.settings_dialog.QFileDialog") as fd, \
                mock.patch.object(bm, "discover_binary_dir", return_value="/auto"):
            fd.getExistingDirectory.return_value = "/picked"
            dlg._browse_binary_dir()
            dlg._auto_detect_binaries()
        self.assertEqual([c.args[0] for c in prep.call_args_list], ["/picked", "/auto"])


class TestMainDialogAndPluginWiring(unittest.TestCase):
    def test_show_engine_settings_selects_tab_and_optionally_downloads(self):
        dlg = AetherMainDialog.__new__(AetherMainDialog)
        dlg.tabs = mock.Mock()
        dlg.settings_tab = object()
        dlg._settings_widget = mock.Mock()
        dlg.show_engine_settings()
        dlg.tabs.setCurrentWidget.assert_called_with(dlg.settings_tab)
        dlg._settings_widget.start_engine_download.assert_not_called()
        dlg.show_engine_settings(start_download=True)
        dlg._settings_widget.start_engine_download.assert_called_once_with()

    def test_plugin_opens_the_dialog_then_the_settings(self):
        plugin = AetherPlugin(mock.Mock())
        dialog = mock.Mock()

        def fake_open():
            plugin._main_dialog = dialog

        with mock.patch.object(plugin, "_open_main_dialog", side_effect=fake_open):
            plugin.open_engine_settings(start_download=True)
        dialog.show_engine_settings.assert_called_once_with(start_download=True)


if __name__ == "__main__":
    unittest.main()
