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

    def test_read_version(self, tmp_path):
        meta = tmp_path / "metadata.txt"
        meta.write_text("[general]\nname=Waveshed\nversion=1.2.3\n")
        assert pkg.read_version(str(meta)) == "1.2.3"
