#!/usr/bin/env python3
"""Build a distributable ZIP of the Waveshed QGIS plugin.

Produces ``dist/waveshed.<version>.zip`` (version read from
``waveshed/metadata.txt``) containing the ``waveshed/`` package as the ZIP
root directory — the layout the QGIS Plugin Manager expects. Repo-root files
listed in ``ROOT_FILES`` (the ``LICENSE``, which plugins.qgis.org requires
inside the plugin folder) are copied in as ``waveshed/<name>``; a missing one
fails the build.

Hygiene excludes (silently skipped): the directories in ``SKIP_DIRS``
(``__pycache__``, ``.pytest_cache``, ``bin``, ``.venv``, ``tmp``, ``tests``,
``dist``, …), the extensions in ``SKIP_EXTS`` (``*.pyc`` / ``*.pyo``) and the
name patterns in ``SKIP_GLOBS`` (generated ``*_rc.py``, ``deploy.local.ini``).

The build WARNS loudly (stderr, the ZIP is still built) when a packaged text
file still contains ``?prv``, the waveshed.io private-preview flag set in
``waveshed/core/site_links.py`` (TODO(release): remove it before publishing).

The build HARD-FAILS (raising :class:`BuildError`, non-zero exit) if:
  * a native/binary artifact (.exe/.dll/.so/.dylib/.pyd/.whl) would be zipped,
  * a file matching a secret pattern (*.key, *.pem, *.bin, vendor_keys*,
    api_key*) would be zipped (source ``*.py`` and bundled resource assets are
    never treated as secrets),
  * the finished ZIP exceeds 20 MB.

With ``--release`` (the pre-publish run for plugins.qgis.org) the build also
fails unless every ``homepage`` / ``tracker`` / ``repository`` URL in
metadata.txt answers HTTP 200 to an anonymous request — the registry and its
users see those links without credentials, so a private or missing repository
(which GitHub reports as 404) must block the upload. Plain builds stay offline.

``--release`` also fails unless ``waveshed/core/release_keys.py`` lists at
least one trusted manifest-signing key (64 hex): a public build without one
would refuse every signed engine release. Plain builds only warn.

Usage: ``python package.py [--release] [OUT_DIR]``

Uses only the standard library (no external ``zip`` binary required).
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import re
import os
import sys
import urllib.error
import urllib.request
import zipfile
from typing import Iterator, List, Tuple

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PACKAGE_NAME = "waveshed"

#: Directory names pruned from the walk (never included).
#:
#: The walk starts at ``waveshed/``, so the repo-root siblings (``.venv``,
#: ``tmp``, ``tests``, ``dist``) are already out of reach — they are listed
#: anyway so the exclusion is a stated rule rather than an accident of where
#: the walk happens to begin.
SKIP_DIRS = {
    "__pycache__", ".pytest_cache", "bin", ".git", ".idea",
    ".venv", "venv", "tmp", "tests", "dist",
}

#: Repo-root files copied into the plugin folder of the ZIP (required).
#: The engine EULA is deliberately NOT bundled: the plugin does not contain the
#: engine, and the canonical EULA text lives on waveshed.io (shown in full in
#: the download consent dialog). LICENSE carries the pointer to it.
ROOT_FILES = ("LICENSE",)

#: File extensions silently skipped.
SKIP_EXTS = {".pyc", ".pyo"}

#: Filename patterns silently skipped, matched against the basename.
#:
#: ``*_rc.py`` are Qt resource modules compiled from ``.qrc``. Unlike the
#: entries above these really can appear *inside* ``waveshed/`` — nothing in
#: the plugin imports one today, and shipping a stale generated module is how
#: a package starts disagreeing with its own sources.
SKIP_GLOBS = ("*_rc.py", "deploy.local.ini")

#: Native artifacts that must never ship — hard FAIL if present.
BINARY_EXTS = {".exe", ".dll", ".so", ".dylib", ".pyd", ".whl"}

#: Secret filename patterns that must never ship — hard FAIL if present.
SECRET_GLOBS = ("*.key", "*.pem", "*.bin", "vendor_keys*", "api_key*")

#: Maximum allowed size of the produced ZIP.
MAX_ZIP_BYTES = 20 * 1024 * 1024

#: Marker of the waveshed.io private-preview links (core/site_links.py).
#: TODO(release): the published plugin must not contain it — the build warns.
PREVIEW_MARKER = "?prv"

#: Extensions scanned for :data:`PREVIEW_MARKER` (text files only).
PREVIEW_SCAN_EXTS = {".py", ".txt", ".html", ".md", ".json", ".ini", ".cfg"}


class BuildError(Exception):
    """Raised when the package must not be built (unsafe or oversized)."""


# ---------------------------------------------------------------------------
# Classification helpers (pure — unit tested)
# ---------------------------------------------------------------------------


def _parts(rel_path: str) -> List[str]:
    return os.path.normpath(rel_path).split(os.sep)


def is_skipped_file(rel_path: str) -> bool:
    """True if the file is excluded by directory, extension or name pattern."""
    if any(part in SKIP_DIRS for part in _parts(rel_path)):
        return True
    if os.path.splitext(rel_path)[1].lower() in SKIP_EXTS:
        return True
    base = os.path.basename(rel_path)
    return any(fnmatch.fnmatch(base, glob) for glob in SKIP_GLOBS)


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


def find_preview_links(entries: List[Tuple[str, str]]) -> List[str]:
    """Return the arcnames of packaged text files that contain ``?prv``.

    ``?prv`` unlocks the private preview of waveshed.io pages; a published
    plugin must link the public pages (see ``core/site_links.py``).
    """
    hits = []
    for abs_path, arcname in entries:
        if os.path.splitext(arcname)[1].lower() not in PREVIEW_SCAN_EXTS:
            continue
        with open(abs_path, encoding="utf-8", errors="replace") as fh:
            if PREVIEW_MARKER in fh.read():
                hits.append(arcname)
    return hits


def _warn_preview_links(hits: List[str]) -> None:
    if not hits:
        return
    bar = "!" * 72
    lines = [
        bar,
        "WARNING: the packaged plugin still links waveshed.io preview pages",
        f"({PREVIEW_MARKER!r} found). Do NOT publish this ZIP. Before release set",
        "PREVIEW_QUERY = \"\" in waveshed/core/site_links.py. Files:",
        *(f"    {h}" for h in hits),
        bar,
    ]
    print("\n".join(lines), file=sys.stderr)


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
    entries = list(iter_plugin_files(pkg_dir, repo_root))
    for name in ROOT_FILES:
        src = os.path.join(repo_root, name)
        if not os.path.isfile(src):
            raise BuildError(f"Required file missing from the repo root: {name}")
        entries.append((src, f"{PACKAGE_NAME}/{name}"))
    entries.sort(key=lambda e: e[1])

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
    _warn_preview_links(find_preview_links(entries))
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


#: metadata.txt keys whose URLs must resolve publicly before a release upload.
PUBLIC_URL_KEYS = ("homepage", "tracker", "repository")


def _http_status(url: str) -> int:
    """Anonymous GET (redirects followed); the final HTTP status."""
    req = urllib.request.Request(url, headers={"User-Agent": "waveshed-package-check"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def check_public_urls(metadata_path: str, status_of=_http_status) -> List[str]:
    """Problems with the metadata.txt URLs QGIS users see (empty = all public).

    ``status_of(url) -> int`` is injectable for tests; network errors count as
    problems, never as a pass.
    """
    urls = {}
    with open(metadata_path, encoding="utf-8") as fh:
        for line in fh:
            key, sep, value = line.partition("=")
            if sep and key.strip() in PUBLIC_URL_KEYS:
                urls[key.strip()] = value.strip()
    problems = []
    for key in PUBLIC_URL_KEYS:
        url = urls.get(key)
        if not url:
            problems.append(f"{key}= is missing from metadata.txt")
            continue
        if not url.startswith("https://"):
            problems.append(f"{key}={url} is not an https URL")
            continue
        try:
            status = status_of(url)
        except Exception as exc:  # noqa: BLE001 - any failure blocks the release
            problems.append(f"{key}={url} could not be fetched: {exc}")
            continue
        if status != 200:
            hint = " (private or missing repository?)" if status == 404 else ""
            problems.append(f"{key}={url} answers HTTP {status}{hint}")
        else:
            print(f"  ok  {key}={url}")
    return problems


#: Module holding the trusted manifest-signing public keys (machine-edited).
RELEASE_KEYS_FILE = os.path.join(PACKAGE_NAME, "core", "release_keys.py")


def read_manifest_keys(repo_root: str) -> List[str]:
    """The ``MANIFEST_PUBLIC_KEYS`` tuple of release_keys.py, parsed (not imported).

    Raises :class:`BuildError` when the file is missing, the assignment is not
    a literal tuple of strings, or a key is not 64 lowercase hex characters.
    """
    path = os.path.join(repo_root, RELEASE_KEYS_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
    except (OSError, SyntaxError) as exc:
        raise BuildError(f"Cannot read {RELEASE_KEYS_FILE}: {exc}") from exc
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else (
            node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None)
        if isinstance(target, ast.Name) and target.id == "MANIFEST_PUBLIC_KEYS":
            try:
                keys = ast.literal_eval(node.value)
            except ValueError as exc:
                raise BuildError(f"MANIFEST_PUBLIC_KEYS is not a literal: {exc}") from exc
            if not isinstance(keys, tuple) or not all(isinstance(k, str) for k in keys):
                raise BuildError("MANIFEST_PUBLIC_KEYS must be a tuple of strings")
            bad = [k for k in keys if not re.fullmatch(r"[0-9a-f]{64}", k)]
            if bad:
                raise BuildError(f"MANIFEST_PUBLIC_KEYS has malformed keys: {bad}")
            return list(keys)
    raise BuildError(f"No MANIFEST_PUBLIC_KEYS assignment in {RELEASE_KEYS_FILE}")


def check_manifest_keys(repo_root: str, release: bool) -> None:
    """Release: at least one trusted manifest key is required. Else: warn."""
    keys = read_manifest_keys(repo_root)
    if keys:
        print(f"  ok  {len(keys)} trusted manifest key(s) in {RELEASE_KEYS_FILE}")
        return
    message = (f"{RELEASE_KEYS_FILE} lists no trusted manifest-signing key; "
               "this plugin would refuse every signed engine release")
    if release:
        raise BuildError(message)
    print(f"WARNING: {message}. Do NOT publish this ZIP.", file=sys.stderr)


def main(argv: List[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    release = "--release" in argv
    argv = [a for a in argv if a != "--release"]
    repo_root = os.path.dirname(os.path.abspath(__file__))
    out_dir = argv[0] if argv else os.path.join(repo_root, "dist")
    try:
        if release:
            print("Release check: metadata.txt URLs must resolve publicly")
            problems = check_public_urls(
                os.path.join(repo_root, PACKAGE_NAME, "metadata.txt"))
            if problems:
                raise BuildError("; ".join(problems))
        check_manifest_keys(repo_root, release)
        build_zip(repo_root, out_dir)
    except BuildError as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
