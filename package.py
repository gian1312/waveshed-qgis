#!/usr/bin/env python3
"""Build a distributable ZIP of the Waveshed QGIS plugin.

Produces ``dist/waveshed.<version>.zip`` (version read from
``waveshed/metadata.txt``) containing the ``waveshed/`` package as the ZIP
root directory — the layout the QGIS Plugin Manager expects.

Hygiene excludes (silently skipped): ``__pycache__`` / ``.pytest_cache`` /
``bin`` directories and ``*.pyc`` / ``*.pyo`` files.

The build HARD-FAILS (raising :class:`BuildError`, non-zero exit) if:
  * a native/binary artifact (.exe/.dll/.so/.dylib/.pyd/.whl) would be zipped,
  * a file matching a secret pattern (*.key, *.pem, *.bin, vendor_keys*,
    api_key*) would be zipped (source ``*.py`` and bundled resource assets are
    never treated as secrets),
  * the finished ZIP exceeds 20 MB.

Uses only the standard library (no external ``zip`` binary required).
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import sys
import zipfile
from typing import Iterator, List, Tuple

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PACKAGE_NAME = "waveshed"

#: Directory names pruned from the walk (never included).
SKIP_DIRS = {"__pycache__", ".pytest_cache", "bin", ".git", ".idea"}

#: File extensions silently skipped.
SKIP_EXTS = {".pyc", ".pyo"}

#: Native artifacts that must never ship — hard FAIL if present.
BINARY_EXTS = {".exe", ".dll", ".so", ".dylib", ".pyd", ".whl"}

#: Secret filename patterns that must never ship — hard FAIL if present.
SECRET_GLOBS = ("*.key", "*.pem", "*.bin", "vendor_keys*", "api_key*")

#: Maximum allowed size of the produced ZIP.
MAX_ZIP_BYTES = 20 * 1024 * 1024


class BuildError(Exception):
    """Raised when the package must not be built (unsafe or oversized)."""


# ---------------------------------------------------------------------------
# Classification helpers (pure — unit tested)
# ---------------------------------------------------------------------------


def _parts(rel_path: str) -> List[str]:
    return os.path.normpath(rel_path).split(os.sep)


def is_skipped_file(rel_path: str) -> bool:
    """True if the file lives under a skipped dir or has a skipped extension."""
    if any(part in SKIP_DIRS for part in _parts(rel_path)):
        return True
    return os.path.splitext(rel_path)[1].lower() in SKIP_EXTS


def is_binary_artifact(rel_path: str) -> bool:
    """True if *rel_path* is a native/binary artifact that must not ship."""
    return os.path.splitext(rel_path)[1].lower() in BINARY_EXTS


def is_secret(rel_path: str) -> bool:
    """True if *rel_path* matches a secret pattern that must not ship.

    Source code (``*.py``, e.g. ``core/api_key.py``) is never a secret, and
    bundled resource assets are exempt from the generic ``*.bin`` rule
    ("*.bin except icon resources").
    """
    base = os.path.basename(rel_path)
    if base.endswith(".py"):
        return False
    in_resources = "resources" in _parts(rel_path)
    for glob in SECRET_GLOBS:
        if fnmatch.fnmatch(base, glob):
            if glob == "*.bin" and in_resources:
                continue
            return True
    return False


def enforce_size(size_bytes: int) -> None:
    """Raise :class:`BuildError` if *size_bytes* exceeds the ZIP limit."""
    if size_bytes > MAX_ZIP_BYTES:
        raise BuildError(
            f"Built ZIP is {size_bytes / 1_048_576:.1f} MB, exceeding the "
            f"{MAX_ZIP_BYTES / 1_048_576:.0f} MB limit."
        )


# ---------------------------------------------------------------------------
# Collection & build
# ---------------------------------------------------------------------------


def iter_plugin_files(
    pkg_dir: str, repo_root: str
) -> Iterator[Tuple[str, str]]:
    """Yield ``(abs_path, arcname)`` for each file to include.

    ``arcname`` is relative to *repo_root* so the archive is rooted at
    ``waveshed/``. Raises :class:`BuildError` on a binary artifact or secret.
    """
    for dirpath, dirnames, filenames in os.walk(pkg_dir):
        # Prune skipped directories in place so we never descend into them.
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in sorted(filenames):
            abs_path = os.path.join(dirpath, name)
            arcname = os.path.relpath(abs_path, repo_root)
            if is_skipped_file(arcname):
                continue
            if is_binary_artifact(arcname):
                raise BuildError(
                    f"Refusing to package a native binary artifact: {arcname}"
                )
            if is_secret(arcname):
                raise BuildError(
                    f"Refusing to package a secret/credential file: {arcname}"
                )
            yield abs_path, arcname


def read_version(metadata_path: str) -> str:
    """Return the ``version=`` value from a metadata.txt file."""
    with open(metadata_path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip().startswith("version="):
                return line.split("=", 1)[1].strip()
    raise BuildError(f"No 'version=' found in {metadata_path}")


def build_zip(repo_root: str, out_dir: str) -> str:
    """Build the plugin ZIP and return its path. Raises :class:`BuildError`."""
    pkg_dir = os.path.join(repo_root, PACKAGE_NAME)
    if not os.path.isdir(pkg_dir):
        raise BuildError(f"Package directory not found: {pkg_dir}")

    version = read_version(os.path.join(pkg_dir, "metadata.txt"))
    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, f"{PACKAGE_NAME}.{version}.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)

    # Collect first (fully validated) so a failure leaves no partial ZIP.
    entries = sorted(iter_plugin_files(pkg_dir, repo_root), key=lambda e: e[1])

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for abs_path, arcname in entries:
            zf.write(abs_path, arcname)

    size = os.path.getsize(zip_path)
    try:
        enforce_size(size)
    except BuildError:
        os.remove(zip_path)
        raise

    _print_summary(zip_path, [a for _, a in entries], size)
    return zip_path


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _print_summary(zip_path: str, arcnames: List[str], size: int) -> None:
    print(f"Built {zip_path}")
    print(f"  Contents ({len(arcnames)} files):")
    for arc in arcnames:
        print(f"    {arc}")
    print(f"  Size:    {size:,} bytes ({size / 1024:.1f} KiB)")
    print(f"  SHA-256: {_sha256(zip_path)}")


def main(argv: List[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    repo_root = os.path.dirname(os.path.abspath(__file__))
    out_dir = argv[0] if argv else os.path.join(repo_root, "dist")
    try:
        build_zip(repo_root, out_dir)
    except BuildError as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
