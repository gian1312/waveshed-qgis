"""Manage discovery, download, and verification of AETHER binaries.

Handles aether_core, aether_converter, and aether_export — the three
Rust CLI tools that perform all computation.  This module never imports
GUI code; progress feedback is delivered through plain callbacks.
"""

from __future__ import annotations

import os
import platform
import shutil
import stat
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, List, Optional

from qgis.core import Qgis, QgsMessageLog, QgsSettings

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TAG = "AETHER"  # QgsMessageLog tag

REQUIRED_BINARIES = ("aether_core", "aether_converter", "aether_export")

_EXE_SUFFIX = ".exe" if platform.system() == "Windows" else ""

PLATFORM_MAP: dict[tuple[str, str], str] = {
    ("Windows", "AMD64"):  "aether-windows-x64.zip",
    ("Linux",   "x86_64"): "aether-linux-x64.zip",
    ("Darwin",  "arm64"):  "aether-macos-arm64.zip",
    ("Darwin",  "x86_64"): "aether-macos-x64.zip",
}

GITHUB_RELEASE_URL = (
    "https://github.com/aether-rf/aether/releases/latest/download"
)

DEFAULT_INSTALL_DIR = os.path.join(Path.home(), ".aether", "bin")

#: Callable[[int, int, int], None]  –  (block_num, block_size, total_size)
ProgressCallback = Callable[[int, int, int], None]


# ---------------------------------------------------------------------------
# Binary discovery
# ---------------------------------------------------------------------------

def _binary_name(name: str) -> str:
    """Return the platform-specific filename for a binary."""
    return name + _EXE_SUFFIX


def _dir_has_binary(directory: str, name: str) -> bool:
    """Return True if *directory* contains the named binary."""
    return os.path.isfile(os.path.join(directory, _binary_name(name)))


def _dir_has_all_binaries(directory: str) -> bool:
    """Return True if *directory* contains every required binary."""
    return all(_dir_has_binary(directory, b) for b in REQUIRED_BINARIES)


def _search_path_for(name: str) -> Optional[str]:
    """Search the system PATH for *name* and return its directory, or None."""
    exe = shutil.which(_binary_name(name))
    if exe is not None:
        return os.path.dirname(os.path.abspath(exe))
    return None


def _plugin_bin_dir() -> str:
    """Return ``<plugin_dir>/bin/``."""
    plugin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(plugin_dir, "bin")


def discover_binary_dir() -> Optional[str]:
    """Find the directory containing all AETHER binaries.

    The search order (first match wins):

    1. User-configured path in QgsSettings (``aether/binary_dir``).
    2. ``AETHER_BIN_DIR`` environment variable.
    3. System ``PATH`` (looks for ``aether_core``).
    4. Default install location ``~/.aether/bin/``.
    5. Plugin install directory ``<plugin_dir>/bin/``.

    Returns the directory path, or ``None`` if no location has all three
    required binaries.
    """
    candidates: list[Optional[str]] = [
        # 1. Plugin settings
        QgsSettings().value("aether/binary_dir", None),
        # 2. Environment variable
        os.environ.get("AETHER_BIN_DIR"),
        # 3. PATH – locate via the primary binary
        _search_path_for("aether_core"),
        # 4. Default install directory
        DEFAULT_INSTALL_DIR,
        # 5. Plugin bin/ directory
        _plugin_bin_dir(),
    ]

    for candidate in candidates:
        if candidate and os.path.isdir(candidate) and _dir_has_all_binaries(candidate):
            QgsMessageLog.logMessage(
                f"Binaries found in {candidate}",
                TAG,
                Qgis.MessageLevel.Info,
            )
            return candidate

    return None


def find_binary(name: str) -> str:
    """Return the full path to the binary *name*.

    Parameters
    ----------
    name:
        One of ``aether_core``, ``aether_converter``, or ``aether_export``
        (without the ``.exe`` suffix on Windows — it is appended
        automatically).

    Raises
    ------
    RuntimeError
        If the binary cannot be located on any of the searched paths.
    """
    binary_dir = discover_binary_dir()
    if binary_dir is not None:
        full_path = os.path.join(binary_dir, _binary_name(name))
        if os.path.isfile(full_path):
            return full_path

    # Fallback: try individual PATH lookup
    exe = shutil.which(_binary_name(name))
    if exe is not None:
        return os.path.abspath(exe)

    raise RuntimeError(
        f"AETHER binary '{name}' not found. Please configure the binary "
        f"directory in Settings or run 'Download Binaries'."
    )


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _detect_platform_zip() -> str:
    """Return the ZIP filename appropriate for the running OS/arch.

    Raises
    ------
    RuntimeError
        If the current platform is not supported.
    """
    system = platform.system()
    machine = platform.machine()
    zip_name = PLATFORM_MAP.get((system, machine))
    if zip_name is None:
        raise RuntimeError(
            f"Unsupported platform: {system} {machine}. "
            f"Supported: {', '.join(f'{s}/{m}' for s, m in PLATFORM_MAP)}"
        )
    return zip_name


def download_binaries(
    target_dir: str = DEFAULT_INSTALL_DIR,
    progress_cb: Optional[ProgressCallback] = None,
) -> str:
    """Download and extract AETHER binaries for the current platform.

    Parameters
    ----------
    target_dir:
        Directory to extract binaries into.  Created if it does not
        exist.  Defaults to ``~/.aether/bin/``.
    progress_cb:
        Optional callback ``(block_num, block_size, total_size)`` passed
        to :func:`urllib.request.urlretrieve`.  Suitable for driving a
        ``QProgressBar``::

            def on_progress(block_num, block_size, total_size):
                if total_size > 0:
                    pct = min(100, int(block_num * block_size * 100 / total_size))
                    progress_bar.setValue(pct)

    Returns
    -------
    str
        Absolute path to *target_dir* after successful extraction and
        verification.

    Raises
    ------
    RuntimeError
        On network error, extraction failure, or post-download
        verification failure.
    """
    target_dir = os.path.expanduser(target_dir)
    os.makedirs(target_dir, exist_ok=True)

    zip_name = _detect_platform_zip()
    url = f"{GITHUB_RELEASE_URL}/{zip_name}"
    zip_path = os.path.join(tempfile.gettempdir(), zip_name)

    QgsMessageLog.logMessage(
        f"Downloading {url} ...", TAG, Qgis.MessageLevel.Info
    )

    # 1. Download
    try:
        urllib.request.urlretrieve(url, zip_path, reporthook=progress_cb)
    except Exception as exc:
        raise RuntimeError(
            f"Failed to download AETHER binaries from {url}: {exc}"
        ) from exc

    # 2. Extract
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(target_dir)
    except zipfile.BadZipFile as exc:
        raise RuntimeError(
            f"Downloaded file is not a valid ZIP archive: {exc}"
        ) from exc
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)

    # 3. Set executable permissions (Linux / macOS)
    if platform.system() != "Windows":
        for name in REQUIRED_BINARIES:
            path = os.path.join(target_dir, name)
            if os.path.exists(path):
                current = os.stat(path).st_mode
                os.chmod(path, current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # 4. Persist the path in plugin settings
    QgsSettings().setValue("aether/binary_dir", target_dir)

    QgsMessageLog.logMessage(
        f"Binaries extracted to {target_dir}", TAG, Qgis.MessageLevel.Info
    )

    # 5. Verify
    issues = check_prerequisites(target_dir)
    missing = [i for i in issues if "not found" in i.lower()]
    if missing:
        raise RuntimeError(
            "Download succeeded but verification failed:\n"
            + "\n".join(missing)
        )

    return target_dir


# ---------------------------------------------------------------------------
# Prerequisites check
# ---------------------------------------------------------------------------

def check_prerequisites(binary_dir: str) -> List[str]:
    """Check that all runtime dependencies are satisfied.

    Parameters
    ----------
    binary_dir:
        Directory that should contain the AETHER binaries.

    Returns
    -------
    list[str]
        Human-readable issues.  An empty list means everything is OK.
    """
    issues: list[str] = []

    # --- Required binaries ---------------------------------------------------
    for name in REQUIRED_BINARIES:
        exe = _binary_name(name)
        path = os.path.join(binary_dir, exe)
        if not os.path.isfile(path):
            issues.append(f"Binary not found: {path}")

    # --- Platform-specific runtime deps --------------------------------------
    system = platform.system()

    if system == "Windows":
        dxc = os.path.join(binary_dir, "dxcompiler.dll")
        if not os.path.isfile(dxc):
            # Optional — GPU still works via Vulkan backend without it.
            # Only log, don't report as an issue.
            QgsMessageLog.logMessage(
                "dxcompiler.dll not found — DirectX shader compilation "
                "unavailable, GPU will use Vulkan backend instead",
                TAG,
                Qgis.MessageLevel.Info,
            )

    if system == "Linux":
        try:
            subprocess.run(
                ["vulkaninfo", "--summary"],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except FileNotFoundError:
            issues.append(
                "Vulkan drivers not detected (vulkaninfo not found). "
                "GPU mode may not work. "
                "Install: apt install libvulkan1 mesa-vulkan-drivers"
            )
        except subprocess.TimeoutExpired:
            issues.append(
                "vulkaninfo timed out — Vulkan driver may be misconfigured"
            )

    if issues:
        for issue in issues:
            QgsMessageLog.logMessage(issue, TAG, Qgis.MessageLevel.Warning)

    return issues
