"""Manage discovery, download, and verification of AETHER binaries.

Handles aether_core, aether_converter, and aether_export — the three
Rust CLI tools that perform all computation.  This module never imports
GUI code; progress feedback is delivered through plain callbacks.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Callable, List, Optional

from qgis.core import Qgis, QgsMessageLog, QgsSettings

# NOTE: QgsBlockingNetworkRequest and the Qt network classes are imported
# lazily inside the fetch/download functions so this module stays importable
# under the lightweight test stubs (which do not provide them).

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TAG = "Waveshed"  # QgsMessageLog tag

REQUIRED_BINARIES = ("aether_core", "aether_converter", "aether_export")

_EXE_SUFFIX = ".exe" if platform.system() == "Windows" else ""

#: URL of the release manifest served by waveshed.io.
MANIFEST_URL = "https://waveshed.io/releases/latest.json"

#: Maps (platform.system(), platform.machine()) to the engine
#: (platform, arch) pair used in the manifest's asset entries. Intel macOS
#: is intentionally absent — the engine ships for Apple Silicon only.
PLATFORM_MAP: dict[tuple[str, str], tuple[str, str]] = {
    ("Windows", "AMD64"):  ("windows", "x64"),
    ("Linux",   "x86_64"): ("linux",   "x64"),
    ("Darwin",  "arm64"):  ("macos",   "arm64"),
}

DEFAULT_INSTALL_DIR = os.path.join(Path.home(), ".aether", "bin")

#: Fallback plugin version used if metadata.txt cannot be parsed.
_FALLBACK_PLUGIN_VERSION = "0.1.0"

#: Callable[[int, int], None]  –  (bytes_received, bytes_total)
ProgressCallback = Callable[[int, int], None]


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

    1. User-configured path in QgsSettings (``waveshed/binary_dir``).
    2. ``AETHER_BIN_DIR`` environment variable.
    3. System ``PATH`` (looks for ``aether_core``).
    4. Default install location ``~/.aether/bin/``.
    5. Plugin install directory ``<plugin_dir>/bin/``.

    Returns the directory path, or ``None`` if no location has all three
    required binaries.
    """
    candidates: list[Optional[str]] = [
        # 1. Plugin settings
        QgsSettings().value("waveshed/binary_dir", None),
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
# Plugin version (for manifest min_plugin_version checks)
# ---------------------------------------------------------------------------

def _read_plugin_version() -> str:
    """Return the plugin version from metadata.txt (fallback constant on error)."""
    meta = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "metadata.txt",
    )
    try:
        with open(meta, encoding="utf-8") as fh:
            for line in fh:
                if line.strip().startswith("version="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return _FALLBACK_PLUGIN_VERSION


def _version_tuple(version: str) -> tuple[int, ...]:
    """Parse a dotted version into an int tuple; non-numeric parts become 0."""
    parts: list[int] = []
    for chunk in str(version).strip().split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def compare_versions(a: str, b: str) -> int:
    """Return -1, 0 or 1 for ``a`` <, ==, > ``b`` (dotted numeric versions)."""
    ta, tb = _version_tuple(a), _version_tuple(b)
    length = max(len(ta), len(tb))
    ta = ta + (0,) * (length - len(ta))
    tb = tb + (0,) * (length - len(tb))
    return (ta > tb) - (ta < tb)


def is_plugin_outdated(min_plugin_version: Optional[str]) -> bool:
    """True if *min_plugin_version* is newer than the installed plugin."""
    if not min_plugin_version:
        return False
    return compare_versions(_read_plugin_version(), min_plugin_version) < 0


# ---------------------------------------------------------------------------
# Manifest fetch & asset selection
# ---------------------------------------------------------------------------

def detect_platform() -> tuple[str, str]:
    """Return the engine ``(platform, arch)`` pair for the running system.

    Raises
    ------
    RuntimeError
        If the platform is unsupported. Intel macOS gets a dedicated message
        because the Aether engine ships for Apple Silicon (arm64) only.
    """
    system = platform.system()
    machine = platform.machine()
    if system == "Darwin" and machine != "arm64":
        raise RuntimeError(
            "Intel macOS is not supported. The Aether engine is available "
            "for macOS on Apple Silicon (arm64) only."
        )
    pair = PLATFORM_MAP.get((system, machine))
    if pair is None:
        raise RuntimeError(
            f"Unsupported platform: {system} {machine}. Supported platforms: "
            f"Windows x64, Linux x64, macOS arm64."
        )
    return pair


def validate_manifest(data: object) -> dict:
    """Validate the parsed manifest and return it.

    Raises
    ------
    RuntimeError
        If the manifest is malformed or an unsupported schema version.
    """
    if not isinstance(data, dict):
        raise RuntimeError("Malformed release manifest (expected a JSON object).")
    schema = data.get("schema_version")
    if schema is not None and schema != 1:
        raise RuntimeError(
            f"Unsupported manifest schema_version {schema!r} (expected 1). "
            f"Please update the Waveshed plugin."
        )
    if not data.get("version"):
        raise RuntimeError("Release manifest is missing the 'version' field.")
    assets = data.get("assets")
    if not isinstance(assets, list) or not assets:
        raise RuntimeError("Release manifest contains no assets.")
    return data


def select_asset(
    manifest: dict, plat_arch: Optional[tuple[str, str]] = None
) -> dict:
    """Return the manifest asset for the current (or given) platform.

    Parameters
    ----------
    manifest:
        A validated manifest dict.
    plat_arch:
        Optional ``(platform, arch)`` override, mainly for tests. When
        omitted it is resolved with :func:`detect_platform`.

    Raises
    ------
    RuntimeError
        If no asset matches the platform.
    """
    plat, arch = plat_arch if plat_arch is not None else detect_platform()
    for asset in manifest.get("assets", []):
        if asset.get("platform") == plat and asset.get("arch") == arch:
            return asset
    raise RuntimeError(
        f"The release manifest has no Aether engine build for {plat}/{arch}."
    )


def fetch_manifest(url: str = MANIFEST_URL) -> dict:
    """Fetch and validate the release manifest from waveshed.io.

    Uses :class:`QgsBlockingNetworkRequest` so the QGIS network stack (and the
    user's proxy configuration) is honoured. Safe to call from a worker thread.

    Raises
    ------
    RuntimeError
        On network error or a malformed manifest.
    """
    from qgis.core import QgsBlockingNetworkRequest
    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtNetwork import QNetworkRequest

    QgsMessageLog.logMessage(
        f"Fetching engine manifest {url}", TAG, Qgis.MessageLevel.Info
    )

    request = QgsBlockingNetworkRequest()
    err = request.get(QNetworkRequest(QUrl(url)))
    if err != QgsBlockingNetworkRequest.NoError:
        raise RuntimeError(
            f"Failed to fetch the release manifest from {url}: "
            f"{request.errorMessage()}"
        )

    raw = bytes(request.reply().content())
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Release manifest is not valid JSON: {exc}") from exc

    return validate_manifest(data)


# ---------------------------------------------------------------------------
# SHA-256 verification (mandatory, fail-closed)
# ---------------------------------------------------------------------------

def _is_hex_sha256(value: object) -> bool:
    """True if *value* is a 64-character hexadecimal string."""
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def verify_sha256(file_path: str, expected: object) -> None:
    """Verify *file_path* against the expected SHA-256 digest.

    Fail-closed: a missing, non-hexadecimal (e.g. placeholder), or mismatched
    digest raises :class:`RuntimeError`. Callers delete the file on failure.
    """
    if not _is_hex_sha256(expected):
        raise RuntimeError(
            "Refusing to install: the release manifest does not provide a "
            "valid SHA-256 checksum for this asset (missing or placeholder). "
            "Please try again later or contact waveshed.io."
        )

    digest = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    actual = digest.hexdigest()

    if actual.lower() != expected.lower():
        raise RuntimeError(
            "SHA-256 verification failed for the downloaded engine archive "
            f"(expected {expected.lower()}, got {actual}). The download was "
            "aborted."
        )


# ---------------------------------------------------------------------------
# Download & install
# ---------------------------------------------------------------------------

def _download_to_file(
    url: str, dest_path: str, progress_cb: Optional[ProgressCallback] = None
) -> None:
    """Download *url* to *dest_path* via QgsBlockingNetworkRequest."""
    from qgis.core import QgsBlockingNetworkRequest
    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtNetwork import QNetworkRequest

    request = QgsBlockingNetworkRequest()
    if progress_cb is not None:
        request.downloadProgress.connect(
            lambda received, total: progress_cb(int(received), int(total))
        )
    err = request.get(QNetworkRequest(QUrl(url)))
    if err != QgsBlockingNetworkRequest.NoError:
        raise RuntimeError(
            f"Failed to download the Aether engine from {url}: "
            f"{request.errorMessage()}"
        )
    with open(dest_path, "wb") as fh:
        fh.write(bytes(request.reply().content()))


def download_engine(
    manifest: dict,
    asset: dict,
    target_dir: str = DEFAULT_INSTALL_DIR,
    progress_cb: Optional[ProgressCallback] = None,
) -> str:
    """Download, verify, and install the Aether engine described by *asset*.

    This performs the download only. Callers MUST obtain the user's consent to
    the engine EULA first (handled by the Settings dialog).

    Pipeline: download -> size check (warn-only) -> SHA-256 (fail-closed) ->
    extract -> chmod +x (non-Windows) -> persist settings -> check
    prerequisites -> best-effort ``aether_core --version`` handshake.

    Returns the install directory. Raises :class:`RuntimeError` on any hard
    failure (the partially-downloaded archive is always removed).
    """
    target_dir = os.path.expanduser(target_dir)
    os.makedirs(target_dir, exist_ok=True)

    url = asset.get("url")
    if not url:
        raise RuntimeError("Selected manifest asset has no download URL.")
    filename = asset.get("filename") or "aether-engine.zip"
    version = manifest.get("version", "?")
    zip_path = os.path.join(tempfile.gettempdir(), filename)

    QgsMessageLog.logMessage(
        f"Downloading Aether engine {version} from {url}",
        TAG, Qgis.MessageLevel.Info,
    )

    # 1. Download (remove the temp file on any failure)
    try:
        _download_to_file(url, zip_path, progress_cb)
    except Exception:
        if os.path.exists(zip_path):
            os.remove(zip_path)
        raise

    try:
        # 2. Size check (warn-only)
        expected_size = asset.get("size_bytes")
        if isinstance(expected_size, int) and expected_size > 0:
            actual_size = os.path.getsize(zip_path)
            if actual_size != expected_size:
                QgsMessageLog.logMessage(
                    f"Downloaded size {actual_size} != manifest size_bytes "
                    f"{expected_size} (continuing).",
                    TAG, Qgis.MessageLevel.Warning,
                )

        # 3. SHA-256 (mandatory, fail-closed)
        verify_sha256(zip_path, asset.get("sha256"))

        # 4. Extract
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

    # 5. Executable permissions (Linux / macOS)
    if platform.system() != "Windows":
        for name in REQUIRED_BINARIES:
            path = os.path.join(target_dir, name)
            if os.path.exists(path):
                current = os.stat(path).st_mode
                os.chmod(path, current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # 6. Persist install location and engine version
    settings = QgsSettings()
    settings.setValue("waveshed/binary_dir", target_dir)
    settings.setValue("waveshed/installed_engine_version", version)

    QgsMessageLog.logMessage(
        f"Aether engine {version} extracted to {target_dir}",
        TAG, Qgis.MessageLevel.Info,
    )

    # 7. Verify prerequisites
    issues = check_prerequisites(target_dir)
    missing = [i for i in issues if "not found" in i.lower()]
    if missing:
        raise RuntimeError(
            "Download succeeded but verification failed:\n" + "\n".join(missing)
        )

    # 8. Best-effort version handshake (never fails the install)
    _version_handshake(target_dir)

    return target_dir


def _version_handshake(binary_dir: str) -> None:
    """Best-effort ``aether_core --version`` probe; logs, never raises.

    Older engine builds may not support ``--version`` — a failure here must
    never fail the install.
    """
    exe = os.path.join(binary_dir, _binary_name("aether_core"))
    try:
        result = subprocess.run(
            [exe, "--version"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        out = (result.stdout or result.stderr or "").strip()
        QgsMessageLog.logMessage(
            f"aether_core --version -> {out or '(no output)'}",
            TAG, Qgis.MessageLevel.Info,
        )
    except Exception as exc:  # noqa: BLE001 - handshake must never fail install
        QgsMessageLog.logMessage(
            f"aether_core --version handshake skipped: {exc}",
            TAG, Qgis.MessageLevel.Info,
        )


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
