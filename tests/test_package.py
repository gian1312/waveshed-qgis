"""Unit tests for package.py build exclusion / failure logic.

Pure filesystem simulation — no network, no real binaries.
"""

import os
import zipfile

import pytest

import package as pkg


def _touch(path: str, content: bytes = b"x") -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content)


class TestClassifiers:
    def test_is_secret_flags_credentials(self):
        assert pkg.is_secret("waveshed/vendor_keys.json")
        assert pkg.is_secret("waveshed/api_key.txt")
        assert pkg.is_secret("waveshed/foo.key")
        assert pkg.is_secret("waveshed/foo.pem")
        assert pkg.is_secret("waveshed/api_private_key.bin")

    def test_is_secret_allows_source_and_resources(self):
        # Source code is never a secret, even if it matches "api_key*".
        assert not pkg.is_secret("waveshed/core/api_key.py")
        assert not pkg.is_secret("waveshed/resources/icon.png")
        # "*.bin except icon resources": bundled resource .bin is exempt.
        assert not pkg.is_secret("waveshed/resources/ramp.bin")

    def test_is_binary_artifact(self):
        for ext in (".exe", ".dll", ".so", ".dylib", ".pyd", ".whl"):
            assert pkg.is_binary_artifact("waveshed/x" + ext)
        assert not pkg.is_binary_artifact("waveshed/plugin.py")

    def test_is_skipped_file(self):
        assert pkg.is_skipped_file("waveshed/__pycache__/x.pyc")
        assert pkg.is_skipped_file("waveshed/plugin.pyo")
        assert pkg.is_skipped_file("waveshed/bin/aether_core")
        assert not pkg.is_skipped_file("waveshed/plugin.py")

    def test_is_skipped_file_generated_and_local(self):
        """Exclusions that must hold *inside* the package, not just beside it."""
        assert pkg.is_skipped_file("waveshed/resources_rc.py")
        assert pkg.is_skipped_file("waveshed/gui/dialog_rc.py")
        assert pkg.is_skipped_file("waveshed/deploy.local.ini")
        # Hand-written sources that merely end in "rc" are not resource modules.
        assert not pkg.is_skipped_file("waveshed/core/abt_src.py")

    def test_is_skipped_file_repo_siblings(self):
        """Stated rules, so they survive the walk root ever moving up."""
        for d in (".venv", "tmp", "tests", "dist"):
            assert pkg.is_skipped_file(f"{d}/x.py"), d

    def test_enforce_size(self):
        pkg.enforce_size(19 * 1024 * 1024)  # under the limit: no raise
        with pytest.raises(pkg.BuildError):
            pkg.enforce_size(21 * 1024 * 1024)


class TestIterAndBuild:
    def _make_tree(self, root: str) -> str:
        _touch(os.path.join(root, "waveshed", "__init__.py"))
        _touch(os.path.join(root, "waveshed", "plugin.py"))
        _touch(os.path.join(root, "waveshed", "core", "api_key.py"))
        _touch(os.path.join(root, "waveshed", "resources", "icon.png"))
        _touch(
            os.path.join(root, "waveshed", "metadata.txt"),
            b"[general]\nversion=0.1.0\n",
        )
        _touch(os.path.join(root, "LICENSE"), b"GPL text")
        # Hygiene noise that must be excluded:
        _touch(os.path.join(root, "waveshed", "__pycache__", "plugin.pyc"))
        _touch(os.path.join(root, "waveshed", "bin", "aether_core.exe"))
        _touch(os.path.join(root, "waveshed", "resources_rc.py"))
        return os.path.join(root, "waveshed")

    def test_iter_includes_source_excludes_hygiene(self, tmp_path):
        root = str(tmp_path)
        pkg_dir = self._make_tree(root)
        arcs = sorted(a for _, a in pkg.iter_plugin_files(pkg_dir, root))
        assert arcs == [
            "waveshed/__init__.py",
            "waveshed/core/api_key.py",
            "waveshed/metadata.txt",
            "waveshed/plugin.py",
            "waveshed/resources/icon.png",
        ]

    def test_iter_fails_on_secret(self, tmp_path):
        root = str(tmp_path)
        pkg_dir = self._make_tree(root)
        _touch(os.path.join(root, "waveshed", "vendor_keys.json"))
        with pytest.raises(pkg.BuildError):
            list(pkg.iter_plugin_files(pkg_dir, root))

    def test_iter_fails_on_binary_outside_bin(self, tmp_path):
        root = str(tmp_path)
        pkg_dir = self._make_tree(root)
        _touch(os.path.join(root, "waveshed", "core", "libfoo.so"))
        with pytest.raises(pkg.BuildError):
            list(pkg.iter_plugin_files(pkg_dir, root))

    def test_build_zip_clean(self, tmp_path):
        root = str(tmp_path)
        self._make_tree(root)
        zip_path = pkg.build_zip(root, os.path.join(root, "dist"))
        assert zip_path.endswith("waveshed.0.1.0.zip")
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        assert "waveshed/plugin.py" in names
        assert "waveshed/core/api_key.py" in names
        assert not any(n.endswith(".exe") for n in names)
        assert not any("__pycache__" in n for n in names)

    def test_build_zip_ships_license_inside_plugin_folder(self, tmp_path):
        """plugins.qgis.org requires the licence inside the plugin folder."""
        root = str(tmp_path)
        self._make_tree(root)
        zip_path = pkg.build_zip(root, os.path.join(root, "dist"))
        with zipfile.ZipFile(zip_path) as zf:
            assert zf.read("waveshed/LICENSE") == b"GPL text"
            assert "LICENSE" not in zf.namelist()   # not at the ZIP root

    def test_build_zip_fails_without_license(self, tmp_path):
        root = str(tmp_path)
        self._make_tree(root)
        os.remove(os.path.join(root, "LICENSE"))
        with pytest.raises(pkg.BuildError, match="LICENSE"):
            pkg.build_zip(root, os.path.join(root, "dist"))
        assert not os.listdir(os.path.join(root, "dist"))   # no partial ZIP

    def test_real_license_keeps_gpl_and_carries_the_notices(self):
        """The shipped LICENSE: verbatim GPLv2 plus separate, non-restrictive
        notices (engine is non-commercial; model output, no liability)."""
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "LICENSE"), encoding="utf-8") as fh:
            text = fh.read()
        assert "GNU GENERAL PUBLIC LICENSE\n                       Version 2, June 1991" in text
        assert "END OF TERMS AND CONDITIONS" in text
        assert "NOTICE — MODEL OUTPUT, NO LIABILITY" in text
        assert "does not add any restriction to" in text
        assert "propagation MODEL" in text
        assert "safety-of-life, regulatory, planning or financial" in text
        assert "non-commercial use only" in text
        assert "info@waveshed.io" in text

    def test_read_version(self, tmp_path):
        meta = tmp_path / "metadata.txt"
        meta.write_text("[general]\nname=Waveshed\nversion=1.2.3\n")
        assert pkg.read_version(str(meta)) == "1.2.3"


class TestReleaseUrlCheck:
    """--release: metadata.txt URLs must answer 200 anonymously (no network here)."""

    META = (
        "[general]\nname=Waveshed\nversion=9.9.9\n"
        "tracker=https://example.org/t\nrepository=https://example.org/r\n"
        "homepage=https://example.org/h\n"
    )

    def _meta(self, tmp_path, text=None):
        path = tmp_path / "metadata.txt"
        path.write_text(text or self.META, encoding="utf-8")
        return str(path)

    def test_all_public_passes(self, tmp_path):
        assert pkg.check_public_urls(self._meta(tmp_path), lambda u: 200) == []

    def test_404_repository_fails_with_hint(self, tmp_path):
        probs = pkg.check_public_urls(
            self._meta(tmp_path), lambda u: 404 if u.endswith("/r") else 200)
        assert len(probs) == 1 and "repository=" in probs[0] and "private" in probs[0]

    def test_network_error_is_a_failure_not_a_pass(self, tmp_path):
        def boom(url):
            raise OSError("no route")
        assert len(pkg.check_public_urls(self._meta(tmp_path), boom)) == 3

    def test_missing_and_non_https_keys_fail(self, tmp_path):
        text = "[general]\nhomepage=http://example.org\nrepository=https://example.org/r\n"
        probs = pkg.check_public_urls(self._meta(tmp_path, text), lambda u: 200)
        assert any("tracker= is missing" in p for p in probs)
        assert any("not an https URL" in p for p in probs)

    def test_release_flag_blocks_build(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pkg, "check_public_urls", lambda path: ["repository=x answers HTTP 404"])
        out = tmp_path / "dist"
        assert pkg.main(["--release", str(out)]) == 1
        assert not out.exists() or not any(out.iterdir())
