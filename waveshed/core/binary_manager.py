"""Manage discovery, download, and verification of AETHER binaries.

Handles aether_core, aether_converter, and aether_export — the three
Rust CLI tools that perform all computation.  This module never imports
GUI code; progress feedback is delivered through plain callbacks.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import platform
import shutil
import stat
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

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

#: Every engine archive must be served from here. The manifest is signed, but
#: pinning the host as well means even a manifest accepted under the
#: pre-signing grace rule (:data:`LAST_UNSIGNED_ENGINE`) cannot point the
#: download anywhere else.
RELEASES_URL_PREFIX = "https://releases.waveshed.io/"

#: Detached signature of the manifest for one engine version. Versioned (and
#: immutable) rather than ``manifests/latest.json.sig`` so a manifest and its
#: signature can never be fetched from two different releases mid-upload.
MANIFEST_SIG_URL = "https://releases.waveshed.io/manifests/v{version}/latest.json.sig"

#: Domain-separation prefix of the manifest signature: the signed message is
#: this prefix followed by the exact manifest bytes as served.
MANIFEST_SIG_CONTEXT = b"waveshed-manifest-v1\0"

#: Newest engine release published before manifests were signed. A manifest
#: for this version or older may come without a signature (HTTP 404 on the
#: .sig); anything newer must carry a valid one. Never raise this value.
LAST_UNSIGNED_ENGINE = "0.4.7"

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


def missing_binaries(names: Sequence[str] = REQUIRED_BINARIES) -> List[str]:
    """Names from *names* that cannot be resolved, in the order given.

    Same resolution order as :func:`find_binary`, so a name absent here is one
    that a run would fail on.
    """
    absent = []
    for name in names:
        try:
            find_binary(name)
        except RuntimeError:
            absent.append(name)
    return absent


def _searched_locations() -> List[str]:
    """The places :func:`discover_binary_dir` looks, for a diagnostic message."""
    return [
        f"Settings → engine directory: {QgsSettings().value('waveshed/binary_dir', None) or '(not set)'}",
        f"AETHER_BIN_DIR: {os.environ.get('AETHER_BIN_DIR') or '(not set)'}",
        f"Default install: {DEFAULT_INSTALL_DIR}",
        f"Plugin bin/: {_plugin_bin_dir()}",
        "System PATH",
    ]


def binaries_warning(names: Sequence[str] = REQUIRED_BINARIES) -> Optional[str]:
    """A message naming the engine binaries that are missing, or None.

    Worth checking before a run starts rather than when a binary is first
    needed: terrain preparation silently falls back to the slow QGIS raster
    path when ``aether_converter`` cannot be found, so without this the user
    waits through the whole extract before anything mentions a missing engine.
    """
    absent = missing_binaries(names)
    if not absent:
        return None
    return (
        "The Aether engine is not installed, or the plugin cannot find it.\n\n"
        f"Missing: {', '.join(absent)}\n\n"
        "Searched:\n  " + "\n  ".join(_searched_locations()) + "\n\n"
        "Open Settings and use \"Download Binaries\", or point the engine "
        "directory at an existing install."
    )


def _fingerprint_error_message(returncode: int, stdout: str, stderr: str) -> str:
    """Classify a failed ``aether_core --fingerprint`` run into a user message.

    Pure (no subprocess or IO) so it can be unit-tested directly. It draws the
    important distinction between an engine that is *too old* to understand the
    ``--fingerprint`` flag and a genuine fingerprint *computation* failure:

    * Too old — the flag postdates this binary, so clap rejects it as an
      unknown argument (typically exit code 2, stderr like
      ``error: unexpected argument '--fingerprint' found``). The fix is to
      update the engine, so we say exactly that.
    * Compute failure — the current engine understands the flag but cannot
      build a fingerprint, reported as exit 1 with ``[E:...]`` on stderr. That
      message is already actionable, so we surface it verbatim rather than
      wrongly telling the user to update a binary that is not the problem.

    The ``--help`` heuristic is deliberately paired with a ``fingerprint``
    mention so it matches clap's "try '--help'" footer without also matching a
    compute error, whose text mentions "fingerprint" but never ``--help``.
    """
    detail = (stderr or stdout or "").strip()
    blob = f"{stdout}\n{stderr}".lower()

    engine_too_old = (
        returncode == 2
        or "unexpected argument" in blob
        or "unrecognized" in blob
        or "for more information, try" in blob
        or ("--help" in blob and "fingerprint" in blob)
        or ("error:" in blob and "argument" in blob)
    )
    if engine_too_old:
        return (
            "This Aether engine build does not support fingerprint reporting "
            "(needs engine v0.4.2 or newer). Update the engine binaries via "
            "Settings → Download binaries, then try again."
        )

    return (
        f"aether_core --fingerprint exited with code {returncode}"
        + (f": {detail}" if detail else ".")
    )


def engine_error_hint(output: str, returncode: Optional[int] = None) -> str:
    """Return an actionable hint for a known ``aether_core`` failure, or ``""``.

    Pure (no subprocess or IO) so it can be unit-tested directly. Appended to
    the engine's own ``[E:...]`` tail by every caller that runs a job, so the
    user learns *what to do* rather than only what the engine refused.

    Known case — ``[E:Unsupported resolution 90m. Allowed: [2.0, 5.0, 10.0,
    30.0]]``: engines up to v0.4.2 carried a hardcoded resolution list on the
    native GPU path that contradicted the engine contract (any positive value)
    and the plugin's own offer of 90 m / 250 m. The list is gone in newer
    builds, so the fix is an engine update, not a plugin setting.
    """
    if "unsupported resolution" in (output or "").lower():
        return (
            "\n\nThe installed Aether engine build only accepts 2, 5, 10 and "
            "30 m on the GPU path (a hardcoded list removed in engine "
            f"{COARSE_RESOLUTION_MIN_ENGINE}). Update the engine via Settings "
            "\u2192 Download binaries, or pick one of those resolutions for "
            "this run."
        )
    if macos_oom_kill(output, returncode):
        return (
            "\n\nmacOS most likely stopped the engine because the run ran out "
            "of memory (the engine was already running when it was killed). "
            "Reduce the range or use a coarser resolution, or lower the "
            "memory budget in Settings; Activity Monitor (Memory tab) shows "
            "the pressure."
        )
    return macos_launch_hint(output, force=returncode in _SIGKILL_CODES)


#: Exit codes of a SIGKILL'd child: -9 from ``subprocess``, 137 via a shell.
_SIGKILL_CODES = (-9, 137)

#: Fragments only Gatekeeper/quarantine produces (not a bare SIGKILL).
_MACOS_QUARANTINE_SIGNS = (
    "cannot be opened", "cannot be verified", "not verified",
    "malicious software", "quarantine", "operation not permitted",
)


def macos_oom_kill(output: str, returncode: Optional[int]) -> bool:
    """True when a macOS engine SIGKILL looks like memory pressure (jetsam).

    Gatekeeper kills at launch, before the engine prints anything; an engine
    that produced output was already running, so a SIGKILL is most likely the
    system reclaiming memory. A quarantine-specific message overrides this.
    """
    if platform.system() != "Darwin" or returncode not in _SIGKILL_CODES:
        return False
    text = (output or "").lower()
    if any(sign in text for sign in _MACOS_QUARANTINE_SIGNS):
        return False
    return any(
        line.strip() and "killed" not in line for line in text.splitlines()
    )


#: Output fragments of a macOS launch that Gatekeeper (quarantine) blocked or
#: killed: the shell's "Killed: 9", the system dialog text, a SIGKILL'd child.
_MACOS_BLOCKED_SIGNS = (
    "killed: 9", "sigkill", "signal 9", "cannot be opened",
    "operation not permitted", "cannot be verified", "not verified",
    "malicious software", "quarantine",
)


def macos_launch_hint(output: str, binary_dir: Optional[str] = None,
                      force: bool = False) -> str:
    """The manual quarantine fix, when a macOS engine launch looks blocked.

    Waveshed ships no notarized engine (by decision); quarantine clearing is
    what makes it run (:func:`prepare_engine_dir`). Should that not have
    worked, the user gets the one command that fixes it. *force* skips the
    output match (a launch killed by a signal has no output to match).
    Returns ``""`` off macOS or when nothing points at Gatekeeper.
    """
    if platform.system() != "Darwin":
        return ""
    text = (output or "").lower()
    if not force and not any(sign in text for sign in _MACOS_BLOCKED_SIGNS):
        return ""
    directory = binary_dir or discover_binary_dir() or DEFAULT_INSTALL_DIR
    return (
        "\n\nmacOS may have blocked the Aether engine (download quarantine). "
        "Open Terminal and run:\n"
        f'    xattr -cr "{directory}"\n'
        "then try again."
    )


def engine_message_action(message: str) -> Optional[str]:
    """What a user-facing engine message asks the user to do, for a button.

    ``"update"`` when it tells them to update the engine (too old, unsupported
    resolution, no ``--fingerprint``), ``"install"`` when the engine is missing,
    else ``None``. Every such message names the Settings "Download binaries"
    step, which is what this keys on.
    """
    text = (message or "").lower()
    if "download binaries" not in text:
        return None
    return "update" if "update" in text else "install"


def read_machine_fingerprint(timeout: float = 10.0) -> str:
    """Return this machine's Aether engine fingerprint (64-char lowercase hex).

    Locates ``aether_core`` via :func:`find_binary` and runs
    ``aether_core --fingerprint`` — a probe that prints the hardware
    fingerprint and exits 0 without needing a license. The fingerprint is what
    a user sends to request a machine-locked (node-locked) license key.

    Parameters
    ----------
    timeout:
        Seconds to wait for the probe before giving up (default 10).

    Raises
    ------
    RuntimeError
        If the binary cannot be located, the process exits non-zero or times
        out, or stdout is not a 64-character lowercase-hex string.
    """
    exe = find_binary("aether_core")
    try:
        result = subprocess.run(
            [exe, "--fingerprint"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"aether_core --fingerprint timed out after {timeout:g}s."
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"Could not run aether_core --fingerprint: {exc}"
        ) from exc

    if result.returncode != 0:
        message = _fingerprint_error_message(
            result.returncode, result.stdout or "", result.stderr or ""
        )
        QgsMessageLog.logMessage(message, TAG, Qgis.MessageLevel.Warning)
        raise RuntimeError(message)

    fingerprint = (result.stdout or "").strip()
    if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
        raise RuntimeError(
            "aether_core --fingerprint did not return a valid 64-character "
            f"hex fingerprint (got {fingerprint!r})."
        )

    QgsMessageLog.logMessage(
        f"Machine fingerprint: {fingerprint}", TAG, Qgis.MessageLevel.Info
    )
    return fingerprint


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


#: Callable[[str], tuple[Optional[int], bytes, str]] — (HTTP status or None,
#: body, error message or "") — the seam the signature check fetches through.
HttpGet = Callable[[str], "tuple[Optional[int], bytes, str]"]


def _http_get(url: str) -> tuple[Optional[int], bytes, str]:
    """GET *url* through the QGIS network stack (proxy settings honoured).

    Returns ``(status, body, error)``: *status* is the HTTP status code (None
    when no response arrived), *error* is empty on success. Never raises for
    network or HTTP failures — callers decide what a 404 means.
    """
    from qgis.core import QgsBlockingNetworkRequest
    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtNetwork import QNetworkRequest

    request = QgsBlockingNetworkRequest()
    err = request.get(QNetworkRequest(QUrl(url)))
    reply = request.reply()
    status = None
    try:
        code = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        status = int(code) if code is not None else None
    except (TypeError, ValueError, AttributeError):
        status = None
    body = bytes(reply.content())
    if err != QgsBlockingNetworkRequest.ErrorCode.NoError:
        return status, body, request.errorMessage() or f"HTTP {status}"
    return status, body, ""


_SEMVER_RE = re.compile(r"\d+\.\d+\.\d+")


def verify_manifest_signature(
    raw: bytes,
    manifest: dict,
    trusted_keys: Optional[Sequence[str]] = None,
    fetch: Optional[HttpGet] = None,
) -> str:
    """Check the detached Ed25519 signature of the manifest bytes *raw*.

    The signature lives at :data:`MANIFEST_SIG_URL` for the manifest's own
    version and covers ``MANIFEST_SIG_CONTEXT + raw``. Rules:

    * valid signature by a key in *trusted_keys* (default
      :data:`release_keys.MANIFEST_PUBLIC_KEYS`) → accepted;
    * bad signature, untrusted key or malformed signature file → refused;
    * no signature (HTTP 404) → accepted only for a manifest version up to
      :data:`LAST_UNSIGNED_ENGINE` (releases published before signing), with
      a warning; refused for anything newer;
    * any other failure to fetch the signature → refused ("try again").

    Returns ``"signed"`` or ``"unsigned-legacy"``. Raises
    :class:`RuntimeError` when the manifest must not be used.
    """
    from . import ed25519, release_keys

    if trusted_keys is None:
        trusted_keys = release_keys.MANIFEST_PUBLIC_KEYS
    fetch = fetch or _http_get

    version = str(manifest.get("version", "")).strip()
    if not _SEMVER_RE.fullmatch(version):
        raise RuntimeError(
            f"Refusing the release manifest: version {version!r} is not a "
            "plain X.Y.Z version."
        )
    sig_url = MANIFEST_SIG_URL.format(version=version)
    status, body, error = fetch(sig_url)

    if status == 404:
        if compare_versions(version, LAST_UNSIGNED_ENGINE) <= 0:
            QgsMessageLog.logMessage(
                f"Release manifest {version} is unsigned (published before "
                f"manifest signing); accepted under the pre-signing rule "
                f"(<= {LAST_UNSIGNED_ENGINE}). SHA-256 still protects every archive.",
                TAG, Qgis.MessageLevel.Warning,
            )
            return "unsigned-legacy"
        raise RuntimeError(
            f"Refusing the release manifest for engine {version}: it has no "
            "signature. Every release after "
            f"{LAST_UNSIGNED_ENGINE} must be signed. If a release is being "
            "published right now, try again in a few minutes."
        )
    if error:
        raise RuntimeError(
            f"Could not fetch the release manifest signature ({sig_url}): "
            f"{error}. The manifest was not used; please try again later."
        )

    try:
        doc = json.loads(body.decode("utf-8"))
        if not isinstance(doc, dict):
            raise ValueError("not a JSON object")
        if doc.get("alg") != "ed25519" or doc.get("context") != "waveshed-manifest-v1":
            raise ValueError("unsupported algorithm or context")
        public_key = bytes.fromhex(str(doc.get("public_key", "")))
        signature = base64.b64decode(str(doc.get("signature", "")), validate=True)
        if len(public_key) != 32 or len(signature) != 64:
            raise ValueError("wrong key or signature length")
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise RuntimeError(
            f"Refusing the release manifest: its signature file is malformed ({exc})."
        ) from exc

    trusted = {k.strip().lower() for k in trusted_keys}
    if public_key.hex() not in trusted:
        raise RuntimeError(
            "Refusing the release manifest: it is signed by a key this plugin "
            "does not trust. Update the Waveshed plugin, or contact "
            "info@waveshed.io if this persists."
        )
    if not ed25519.verify(public_key, MANIFEST_SIG_CONTEXT + raw, signature):
        raise RuntimeError(
            "Refusing the release manifest: its signature does not match. The "
            "manifest may have been altered in transit; nothing was downloaded."
        )
    QgsMessageLog.logMessage(
        f"Release manifest {version} signature verified (key {public_key.hex()[:16]}…).",
        TAG, Qgis.MessageLevel.Info,
    )
    return "signed"


def parse_manifest_bytes(raw: bytes) -> dict:
    """Parse and structurally validate manifest bytes (no signature check)."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Release manifest is not valid JSON: {exc}") from exc
    return validate_manifest(data)


def fetch_manifest(url: str = MANIFEST_URL, fetch: Optional[HttpGet] = None) -> dict:
    """Fetch, validate and signature-check the release manifest from waveshed.io.

    The single entry point for every manifest consumer (Download, the startup
    update check, Settings → Check for updates), so none of them can use an
    unverified manifest. Uses :class:`QgsBlockingNetworkRequest` so the QGIS
    network stack (and the user's proxy configuration) is honoured. Safe to
    call from a worker thread.

    Raises
    ------
    RuntimeError
        On network error, a malformed manifest, or a failed signature check
        (see :func:`verify_manifest_signature`).
    """
    fetch = fetch or _http_get
    QgsMessageLog.logMessage(
        f"Fetching engine manifest {url}", TAG, Qgis.MessageLevel.Info
    )
    status, raw, error = fetch(url)
    if error:
        raise RuntimeError(
            f"Failed to fetch the release manifest from {url}: {error}"
        )
    data = parse_manifest_bytes(raw)
    verify_manifest_signature(raw, data, fetch=fetch)
    return data


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
    if err != QgsBlockingNetworkRequest.ErrorCode.NoError:
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
    the engine EULA first (handled by the Settings dialog), and must pass a
    manifest that came through :func:`fetch_manifest` (signature-checked).

    Pipeline: host pin -> download -> size check (warn-only) -> SHA-256
    (fail-closed) -> extract into a staging dir inside *target_dir* -> all
    binaries present -> chmod +x -> clear OS download quarantine ->
    ``revocation.bin`` (fail-soft) -> ``aether_core --version`` must report the
    manifest's version -> swap the staged files into place -> persist settings
    -> prerequisites (logged).

    The swap is all-or-nothing (:func:`_install_staged`): a failure at any
    step leaves the previous install exactly as it was, so an update can never
    leave a mix of old and new binaries behind.

    Returns the install directory. Raises :class:`RuntimeError` on any hard
    failure (the downloaded archive and the staging dir are always removed).
    """
    target_dir = os.path.expanduser(target_dir)
    os.makedirs(target_dir, exist_ok=True)
    _cleanup_stale_backups(target_dir)

    url = asset.get("url")
    if not url:
        raise RuntimeError("Selected manifest asset has no download URL.")
    if not str(url).startswith(RELEASES_URL_PREFIX):
        raise RuntimeError(
            f"Refusing to download the engine from {url}: engine archives are "
            f"only accepted from {RELEASES_URL_PREFIX}."
        )
    version = manifest.get("version", "?")

    # Nothing in the manifest is allowed to choose a filesystem path (it was
    # unsigned for releases up to LAST_UNSIGNED_ENGINE, and defence in depth
    # after). Joining asset["filename"] onto the temp dir let a value like
    # "../../home/<user>/.bashrc" resolve outside it, and the response body is
    # written there BEFORE verify_sha256 runs, with the `finally` below then
    # deleting it: an arbitrary file clobber-and-delete driven by a network
    # response. Sanitise it to a basename for the log, and download to a name
    # we generate. mkstemp also creates the file exclusively, so the temp path
    # cannot be pre-created as a symlink to somewhere else.
    asset_name = os.path.basename(str(asset.get("filename") or "")).strip()
    handle, zip_path = tempfile.mkstemp(prefix="aether-engine-", suffix=".zip")
    os.close(handle)

    QgsMessageLog.logMessage(
        f"Downloading Aether engine {version} "
        f"({asset_name or 'engine archive'}) from {url}",
        TAG, Qgis.MessageLevel.Info,
    )

    # 1. Download (remove the temp file on any failure)
    try:
        _download_to_file(url, zip_path, progress_cb)
    except Exception:
        if os.path.exists(zip_path):
            os.remove(zip_path)
        raise

    # Staged inside the target so the final moves stay on one filesystem
    # (os.replace is then a rename, never a copy) and need no write access to
    # the target's parent.
    staging = tempfile.mkdtemp(prefix=STAGING_PREFIX, dir=target_dir)
    try:
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

            # Windows: clear any Mark-of-the-Web on the downloaded zip *before*
            # extracting, so the extracted files cannot inherit it. No-op
            # elsewhere and when the stream is absent.
            if platform.system() == "Windows":
                _clear_quarantine(staging, [zip_path])

            # 4. Extract into the staging dir
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    zf.extractall(staging)
                    extracted_files = [
                        os.path.join(staging, name)
                        for name in zf.namelist()
                        if not name.endswith("/")
                    ]
            except zipfile.BadZipFile as exc:
                raise RuntimeError(
                    f"Downloaded file is not a valid ZIP archive: {exc}"
                ) from exc
        finally:
            if os.path.exists(zip_path):
                os.remove(zip_path)

        # 5. The archive must hold the whole engine
        missing = [b for b in REQUIRED_BINARIES if not _dir_has_binary(staging, b)]
        if missing:
            raise RuntimeError(
                "The downloaded engine archive is incomplete (missing: "
                f"{', '.join(missing)}). Nothing was installed."
            )

        # 6. Executable bit + OS download quarantine (macOS xattr / Windows
        #    Mark-of-the-Web; a no-op on Linux).
        _make_executable(staging)
        _clear_quarantine(staging, extracted_files)

        # 7. Signed licence revocation list from the manifest -> revocation.bin
        #    (fail-soft: absent/malformed is logged, never fatal). Written into
        #    the staging dir so it is swapped in together with the binaries.
        write_revocation_file(manifest, staging)

        # 8. The staged engine must run and be the version we were promised.
        _verify_staged_version(staging, str(version))

        # 9. Swap into place (all-or-nothing)
        _install_staged(staging, target_dir)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    # 10. Persist install location and engine version
    settings = QgsSettings()
    settings.setValue("waveshed/binary_dir", target_dir)
    settings.setValue("waveshed/installed_engine_version", version)

    QgsMessageLog.logMessage(
        f"Aether engine {version} installed to {target_dir}",
        TAG, Qgis.MessageLevel.Info,
    )

    # 11. Prerequisites (GPU drivers etc.) — reported, the install stands.
    check_prerequisites(target_dir)

    return target_dir


#: Prefix of the staging dir an engine archive is extracted into.
STAGING_PREFIX = ".waveshed-staging-"

#: Infix of the backup name a replaced file gets during the swap.
BACKUP_INFIX = ".waveshed-old-"


def _make_executable(directory: str) -> None:
    """Set the executable bit on every required binary in *directory* (POSIX).

    Archives that went through a browser or a non-POSIX unzip can lose the
    bit; MPT_SIGMA's KADAS plugin does the same on every load.
    """
    if platform.system() == "Windows":
        return
    for name in REQUIRED_BINARIES:
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path):
                current = os.stat(path).st_mode
                os.chmod(path, current | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError as exc:
            QgsMessageLog.logMessage(
                f"Could not make {path} executable: {exc}",
                TAG, Qgis.MessageLevel.Warning,
            )


def prepare_engine_dir(binary_dir: Optional[str]) -> None:
    """Make an engine directory runnable on macOS: ``chmod +x`` + clear quarantine.

    Waveshed ships the engine ad-hoc signed and NOT notarized (a deliberate
    decision), so a copy that reached the disk through a browser carries the
    ``com.apple.quarantine`` attribute and Gatekeeper refuses to launch it.
    Our own download clears it, but a user who unzipped a browser download and
    pointed Settings at it (Browse, Auto-detect, ``AETHER_BIN_DIR``) would be
    stuck — so, like MPT_SIGMA, this runs at plugin load, after Browse /
    Auto-detect, and before every ``--version`` probe. Best effort, never
    raises; a no-op on Windows and Linux (Linux needs only the executable bit,
    which a ZIP extract by us already sets).
    """
    if not binary_dir or platform.system() != "Darwin":
        return
    try:
        if not os.path.isdir(binary_dir):
            return
        _make_executable(binary_dir)
        _clear_quarantine(binary_dir, [])
    except Exception as exc:  # noqa: BLE001 — must never break a caller
        QgsMessageLog.logMessage(
            f"Could not prepare the engine directory {binary_dir}: {exc}",
            TAG, Qgis.MessageLevel.Warning,
        )


def _verify_staged_version(staging: str, expected: str) -> None:
    """``aether_core --version`` from *staging* must name *expected*.

    A staged engine that cannot run at all, or reports another version than
    the manifest promised, is never swapped in.
    """
    exe = os.path.join(staging, _binary_name("aether_core"))
    try:
        result = subprocess.run(
            [exe, "--version"], capture_output=True, text=True,
            timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(
            f"The downloaded engine could not be started ({exc}). Nothing was "
            "installed." + macos_launch_hint(str(exc), staging, force=True)
        ) from exc
    found = parse_engine_version(result.stdout) if result.returncode == 0 else None
    if found is None:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"The downloaded engine did not answer --version (exit "
            f"{result.returncode}{': ' + detail if detail else ''}). Nothing "
            "was installed." + macos_launch_hint(
                detail, staging, force=result.returncode < 0)
        )
    if _SEMVER_RE.fullmatch(expected) and compare_versions(found, expected) != 0:
        raise RuntimeError(
            f"The downloaded engine reports version {found}, but the release "
            f"manifest promised {expected}. Nothing was installed."
        )
    QgsMessageLog.logMessage(
        f"Staged engine answers aether_core {found}", TAG, Qgis.MessageLevel.Info,
    )


def _staged_files(staging: str) -> list[str]:
    """Every file below *staging*, as paths relative to it."""
    out = []
    for root, _dirs, files in os.walk(staging):
        for name in files:
            out.append(os.path.relpath(os.path.join(root, name), staging))
    return sorted(out)


def _assert_not_in_use(target_dir: str, rel_files: Sequence[str]) -> None:
    """Windows: refuse up front when a file to be replaced is locked.

    A running ``aether_core.exe`` (or a DLL it loaded) cannot be opened for
    writing, so it is detected here before anything is touched. POSIX can
    replace a running binary safely, so this is Windows-only.
    """
    if platform.system() != "Windows":
        return
    for rel in rel_files:
        path = os.path.join(target_dir, rel)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r+b"):
                pass
        except OSError as exc:
            raise RuntimeError(
                f"Cannot update the engine: {path} is in use or read-only "
                f"({exc}). Wait for running analyses to finish (or close "
                "them), then try again. The current engine was left as it was."
            ) from exc


def _install_staged(staging: str, target_dir: str) -> None:
    """Move every staged file into *target_dir*, all-or-nothing.

    Phase 1 renames each file that will be replaced to a backup name, phase 2
    moves the staged files in. Any failure rolls both phases back, so the
    previous install is left exactly as it was. Files in *target_dir* the
    archive does not contain (a ``license.key`` next to ``aether_core``, a
    previous ``revocation.bin``) are never touched. Backups are deleted at the
    end; one that cannot be deleted yet (Windows keeps a renamed running
    binary locked) is removed by the next install (:func:`_cleanup_stale_backups`).
    """
    rel_files = _staged_files(staging)
    _assert_not_in_use(target_dir, rel_files)

    token = os.urandom(4).hex()
    backups: list[tuple[str, str]] = []
    moved: list[str] = []
    try:
        for rel in rel_files:
            dst = os.path.join(target_dir, rel)
            if os.path.lexists(dst):
                bak = dst + BACKUP_INFIX + token
                os.rename(dst, bak)
                backups.append((dst, bak))
        for rel in rel_files:
            dst = os.path.join(target_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(os.path.join(staging, rel), dst)
            moved.append(dst)
    except OSError as exc:
        for dst in moved:
            try:
                os.remove(dst)
            except OSError:
                pass
        for dst, bak in reversed(backups):
            try:
                os.replace(bak, dst)
            except OSError as restore_exc:
                QgsMessageLog.logMessage(
                    f"Could not restore {dst} from {bak}: {restore_exc}",
                    TAG, Qgis.MessageLevel.Critical,
                )
        raise RuntimeError(
            f"Installing the engine into {target_dir} failed ({exc}). The "
            "previous engine was restored. If an analysis is running, wait for "
            "it to finish and try again."
        ) from exc

    for _dst, bak in backups:
        try:
            os.remove(bak)
        except OSError:
            QgsMessageLog.logMessage(
                f"Old engine file {bak} is still in use; it is removed by the "
                "next install.", TAG, Qgis.MessageLevel.Info,
            )


def _cleanup_stale_backups(target_dir: str) -> None:
    """Remove leftovers of earlier installs: backups and staging dirs."""
    try:
        for root, dirs, files in os.walk(target_dir):
            for name in list(dirs):
                if name.startswith(STAGING_PREFIX):
                    shutil.rmtree(os.path.join(root, name), ignore_errors=True)
                    dirs.remove(name)
            for name in files:
                if BACKUP_INFIX in name:
                    try:
                        os.remove(os.path.join(root, name))
                    except OSError:
                        pass
    except OSError:
        pass


#: File the engine reads the signed licence revocation list from (next to
#: ``aether_core``; licence format v3, AETHER ``Design Documents/License-v3.md``).
REVOCATION_FILE = "revocation.bin"


def write_revocation_file(manifest: dict, target_dir: str) -> Optional[str]:
    """Write the manifest's signed ``revocation`` list as ``revocation.bin``.

    The manifest carries ``"revocation": {"key_id", "seq", "blob"}`` with
    ``blob`` = base64 of ``key_id u8 || seq u32 || count u16 ||
    licence_id[16 * count] || Ed25519 signature``. Only the layout is checked
    here -- the engine verifies the signature and ignores a list older than
    one it has already seen -- so writing it cannot weaken anything.

    Fail-soft: an absent or malformed field is logged and the install goes on
    (any existing ``revocation.bin`` is left alone). Returns the written path,
    or ``None``.
    """
    rev = manifest.get("revocation") if isinstance(manifest, dict) else None
    if rev is None:
        QgsMessageLog.logMessage(
            "Release manifest has no licence revocation list (nothing to write).",
            TAG, Qgis.MessageLevel.Info,
        )
        return None
    try:
        if not isinstance(rev, dict) or not isinstance(rev.get("blob"), str):
            raise ValueError("no base64 'blob' string")
        blob = base64.b64decode(rev["blob"], validate=True)
        if len(blob) < 71:
            raise ValueError(f"{len(blob)} bytes is too short")
        seq = int.from_bytes(blob[1:5], "big")
        count = int.from_bytes(blob[5:7], "big")
        if len(blob) != 7 + 16 * count + 64:
            raise ValueError("length does not match its entry count")
        if rev.get("key_id") is not None and rev.get("key_id") != blob[0]:
            raise ValueError("key_id does not match the blob")
        if rev.get("seq") is not None and rev.get("seq") != seq:
            raise ValueError("seq does not match the blob")
        path = os.path.join(target_dir, REVOCATION_FILE)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, path)
    except (ValueError, binascii.Error, OSError) as exc:
        QgsMessageLog.logMessage(
            f"Skipping the licence revocation list from the manifest: {exc}",
            TAG, Qgis.MessageLevel.Warning,
        )
        return None
    QgsMessageLog.logMessage(
        f"Wrote licence revocation list (seq {seq}, {count} entries) to {path}",
        TAG, Qgis.MessageLevel.Info,
    )
    return path


def _clear_quarantine(target_dir: str, files: list) -> None:
    """Clear OS download-quarantine marks so freshly downloaded binaries run.

    Strictly best-effort and platform-specific: every failure is logged and
    swallowed so it can never fail an otherwise-successful install.

    macOS
        Browsers and Gatekeeper tag downloaded files with a
        ``com.apple.quarantine`` extended attribute, and a quarantined binary
        refuses to launch until it is cleared. The Aether Rust binaries are
        ad-hoc signed, so clearing quarantine is the only blocker between the
        download and a working engine. ``xattr -cr`` clears it recursively.
    Windows
        Strip the Mark-of-the-Web (the NTFS ``Zone.Identifier`` alternate data
        stream) from each path in *files*; deleting the stream is a no-op when
        it is absent. Our own download path never sets MotW, but this covers a
        zip a user fetched via a browser and pointed the plugin at, and costs
        nothing.
    Linux
        Nothing to do beyond the executable bit already set by the caller.
    """
    system = platform.system()

    if system == "Darwin":
        try:
            result = subprocess.run(
                ["xattr", "-cr", str(target_dir)],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except Exception as exc:  # noqa: BLE001 - must never fail the install
            QgsMessageLog.logMessage(
                f"Could not clear macOS quarantine on {target_dir}: {exc} "
                f"(run 'xattr -cr' manually if the engine will not launch).",
                TAG, Qgis.MessageLevel.Warning,
            )
            return
        if result.returncode == 0:
            QgsMessageLog.logMessage(
                f"Cleared macOS quarantine attribute on {target_dir}",
                TAG, Qgis.MessageLevel.Info,
            )
        else:
            QgsMessageLog.logMessage(
                f"xattr -cr exited {result.returncode} on {target_dir}: "
                f"{(result.stderr or '').strip()} "
                f"(run 'xattr -cr' manually if the engine will not launch).",
                TAG, Qgis.MessageLevel.Warning,
            )

    elif system == "Windows":
        for path in files:
            try:
                os.remove(f"{path}:Zone.Identifier")
            except (FileNotFoundError, OSError):
                # No Mark-of-the-Web stream present (the normal case) — no-op.
                pass

    # Linux: nothing to do — the executable bit set after extraction suffices.


#: First aether_core that solves resolutions outside the legacy GPU list.
#: Engines up to 0.4.2 carried a hardcoded ``[2, 5, 10, 30]`` on the native
#: GPU path (aether-tools docs/CONTRACT.md §1.5, capability gates).
COARSE_RESOLUTION_MIN_ENGINE = "0.4.3"

#: What an engine older than :data:`COARSE_RESOLUTION_MIN_ENGINE` accepts.
LEGACY_ENGINE_RESOLUTIONS = (2, 5, 10, 30)


class EngineTooOldError(RuntimeError):
    """The installed engine cannot run this job; the message says what to do."""


def parse_engine_version(output: str) -> Optional[str]:
    """Extract the semver from ``aether_core --version`` output, else ``None``.

    The contract line is ``aether_core <semver>``. Anything else — clap's
    "unexpected argument" from a build too old to know the flag, an empty
    stdout, a crash banner — reads as *unknown*, which callers must treat as
    the oldest known engine, never as the newest.
    """
    for line in (output or "").splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[0] == "aether_core":
            candidate = parts[1].lstrip("v")
            if re.fullmatch(r"\d+(\.\d+)+", candidate):
                return candidate
    return None


def read_engine_version(timeout: float = 5.0) -> Optional[str]:
    """Return the installed ``aether_core`` version, or ``None`` if unknown.

    Runs ``aether_core --version`` (no license, no GPU). Never raises: a
    missing binary or an engine too old for the flag both yield ``None``.
    """
    try:
        exe = find_binary("aether_core")
    except Exception:  # noqa: BLE001 — a probe must never take the run down
        return None
    return probe_engine_version(os.path.dirname(exe), timeout=timeout)


def probe_engine_version(binary_dir: str, timeout: float = 5.0) -> Optional[str]:
    """``aether_core --version`` of the engine in *binary_dir*, or ``None``.

    Prepares the directory first on macOS (:func:`prepare_engine_dir`), so a
    quarantined copy is fixed before it is launched. Never raises.
    """
    prepare_engine_dir(binary_dir)
    exe = os.path.join(binary_dir, _binary_name("aether_core"))
    try:
        result = subprocess.run(
            [exe, "--version"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        # A fresh .exe is often still being scanned by antivirus on its first
        # launches; say so instead of silently reporting "unknown".
        QgsMessageLog.logMessage(
            f"aether_core --version did not answer within {timeout:g} s ({exe}); "
            "the engine version is reported as unknown for now.",
            TAG, Qgis.MessageLevel.Warning,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — a probe must never take the run down
        QgsMessageLog.logMessage(
            f"aether_core --version could not be started ({exe}): {exc}",
            TAG, Qgis.MessageLevel.Warning,
        )
        return None
    if result.returncode != 0:
        hint = macos_launch_hint(result.stderr or "", binary_dir,
                                 force=result.returncode < 0)
        detail = (result.stderr or result.stdout or "").strip()[:300]
        QgsMessageLog.logMessage(
            f"aether_core --version failed (exit {result.returncode})"
            f"{': ' + detail if detail else ''}.{hint}",
            TAG, Qgis.MessageLevel.Warning,
        )
        return None
    version = parse_engine_version(result.stdout)
    if version is None:
        QgsMessageLog.logMessage(
            f"aether_core --version gave no version line: {(result.stdout or '').strip()[:200]!r}",
            TAG, Qgis.MessageLevel.Warning,
        )
    return version


def installed_engine_version(binary_dir: Optional[str]) -> Optional[str]:
    """Version of the engine in *binary_dir*: its own ``--version`` answer,
    else the version recorded when Waveshed installed it into that same dir.

    The recorded value is only trusted for the directory it was recorded for
    (a hand-picked other directory may hold anything). It covers a probe that
    timed out -- a freshly downloaded .exe being scanned by antivirus -- right
    after an install whose version was already verified in staging.
    """
    if not binary_dir:
        return None
    probed = probe_engine_version(binary_dir)
    if probed:
        return probed
    settings = QgsSettings()
    recorded_dir = str(settings.value("waveshed/binary_dir", "") or "")
    recorded = str(settings.value("waveshed/installed_engine_version", "") or "") or None
    if recorded and recorded_dir and _same_dir(recorded_dir, binary_dir):
        return recorded
    return None


def _same_dir(a: str, b: str) -> bool:
    norm = lambda p: os.path.normcase(os.path.realpath(os.path.expanduser(p)))  # noqa: E731
    return norm(a) == norm(b)


def engine_supports_resolution(
    engine_version: Optional[str], resolution_m: float,
) -> bool:
    """True if an engine of *engine_version* solves *resolution_m*.

    ``None`` (unknown / pre-``--version`` build) is the oldest known engine.
    """
    if engine_version and compare_versions(
        engine_version, COARSE_RESOLUTION_MIN_ENGINE,
    ) >= 0:
        return True
    return float(resolution_m) in {float(r) for r in LEGACY_ENGINE_RESOLUTIONS}


def check_engine_for_job(resolution_m: float) -> str:
    """Pre-flight before any terrain is fetched: log the engine, gate the job.

    Logs the engine path and version on every run (so a stale local deploy
    is visible in the Log Messages panel), and raises
    :class:`EngineTooOldError` when the installed engine cannot solve
    *resolution_m* — BEFORE the terrain download, which for a 90 m / 250 m
    run is the expensive part of a job the engine would then refuse anyway.

    Returns the version string that was logged (``"unknown"`` if the engine
    predates ``--version``), for callers that want to show it.
    """
    try:
        exe = find_binary("aether_core")
    except Exception:  # noqa: BLE001 — the run's own find_binary reports it
        exe = "aether_core"
    version = read_engine_version()
    shown = version or "unknown (build predates --version)"
    QgsMessageLog.logMessage(
        f"Engine: {exe} — aether_core {shown}", TAG, Qgis.MessageLevel.Info,
    )
    if not engine_supports_resolution(version, resolution_m):
        allowed = ", ".join(str(r) for r in LEGACY_ENGINE_RESOLUTIONS)
        raise EngineTooOldError(
            f"The installed Aether engine (aether_core {shown}) cannot run a "
            f"{resolution_m:g} m analysis: it accepts only {allowed} m on the "
            f"GPU path. Engine {COARSE_RESOLUTION_MIN_ENGINE} or newer is "
            "required. Update it via Settings \u2192 Download binaries, or pick "
            "one of those resolutions. Nothing was downloaded."
        )
    return shown


# ---------------------------------------------------------------------------
# Engine update check
# ---------------------------------------------------------------------------

#: QgsSettings switch for the once-per-session startup update check (default on).
UPDATE_CHECK_KEY = "waveshed/engine_update_check"

#: QgsSettings: the engine version the user was last told about, so each new
#: release produces exactly one startup notice.
UPDATE_NOTIFIED_KEY = "waveshed/engine_update_notified_version"


def auto_update_check_enabled() -> bool:
    """True unless the user switched the startup update check off."""
    value = QgsSettings().value(UPDATE_CHECK_KEY, True)
    if isinstance(value, str):
        return value.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(value)


@dataclass
class EngineUpdateInfo:
    """Installed vs. published engine version."""

    installed: Optional[str]
    available: str
    update_available: bool
    manifest: dict


def check_for_engine_update(fetch: Optional[HttpGet] = None) -> Optional[EngineUpdateInfo]:
    """Compare the installed engine with the (signature-checked) manifest.

    Returns ``None`` when no engine is installed (the first-run notice covers
    that case). The installed version is what the engine itself answers to
    ``--version`` — the directory may have been replaced since our last
    download — falling back to the version recorded at download time. An
    engine whose version cannot be determined predates ``--version`` and is
    reported as updatable. Raises :class:`RuntimeError` on a network or
    signature failure; safe to call from a worker thread.
    """
    binary_dir = discover_binary_dir()
    if binary_dir is None:
        return None
    installed = installed_engine_version(binary_dir)
    manifest = fetch_manifest(fetch=fetch)
    available = str(manifest["version"])
    newer = installed is None or compare_versions(available, installed) > 0
    return EngineUpdateInfo(installed, available, newer, manifest)


def should_notify_update(info: Optional[EngineUpdateInfo]) -> bool:
    """True when *info* is an update the user has not been told about yet."""
    if info is None or not info.update_available:
        return False
    return str(QgsSettings().value(UPDATE_NOTIFIED_KEY, "") or "") != info.available


def mark_update_notified(version: str) -> None:
    """Remember that the notice for *version* was shown."""
    QgsSettings().setValue(UPDATE_NOTIFIED_KEY, version)


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
