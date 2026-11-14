"""Manage emitter/receiver asset definitions stored as JSON files.

Assets represent RF equipment (emitters, receivers) with properties like
frequency, power, antenna gain, antenna patterns, and default height.  Each
asset is persisted as a single JSON file inside the user's assets directory.

Modeled after the MPT SIGMA emitter management pattern, adapted for AETHER.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
from pathlib import Path
from typing import Optional

from qgis.core import QgsSettings

from .job_builder import MIN_ANTENNA_AGL_M


# -- Default values -----------------------------------------------------------

_DEFAULT_ASSETS_DIR = os.path.join(str(Path.home()), ".aether", "assets")

_DEFAULT_ASSET = {
    "name": "New Asset",
    "frequency_mhz": 433.0,
    "peak_power_watts": 10.0,
    "antenna_gain_dbi": 0.0,
    "erp_watts": 7.7,
    "default_height_m": 30.0,
    "default_height_mode": "AGL",
    "polarization": 0,
    "azimuth_pattern": {"data": []},
    "elevation_pattern": {"data": []},
    "fraction_situations": 0.5,
    "fraction_time": 0.5,
}


# -- Default height validation -----------------------------------------------


def default_height_error(height_m: float, mode: str) -> Optional[str]:
    """Return a message if an asset's default height is below the AGL floor.

    Args:
        height_m: The asset's ``default_height_m``.
        mode: The asset's ``default_height_mode`` (``"AGL"`` or ``"AMSL"``).

    Returns:
        An error string naming the field and the floor, or None when the height
        is acceptable. AMSL heights always return None — they are absolute
        elevations and are legitimately zero or negative.
    """
    if (mode or "").upper() != "AGL":
        return None
    try:
        height = float(height_m)
    except (TypeError, ValueError):
        return (
            f"default_height_m is not a number ({height_m!r}). It must be at "
            f"least {MIN_ANTENNA_AGL_M:.1f} m AGL."
        )
    if height < MIN_ANTENNA_AGL_M:
        return (
            f"default_height_m is {height:.2f} m AGL, below the "
            f"{MIN_ANTENNA_AGL_M:.1f} m minimum antenna height above ground "
            f"level. Raise it to at least {MIN_ANTENNA_AGL_M:.1f} m, or set "
            f"default_height_mode to AMSL if this is an absolute elevation."
        )
    return None


# -- ERP computation ---------------------------------------------------------


def compute_erp(peak_power_watts: float, antenna_gain_dbi: float) -> float:
    """Compute ERP in watts from peak power and antenna gain.

    Formula:
        EIRP(dBm) = 10 * log10(peak_power_watts * 1000) + antenna_gain_dbi
        ERP(dBm)  = EIRP(dBm) - 2.15
        erp_watts = 10 ^ ((ERP_dBm - 30) / 10)

    Args:
        peak_power_watts: Transmitter peak power in watts (must be > 0).
        antenna_gain_dbi: Antenna gain in dBi.

    Returns:
        ERP in watts.  Returns 0.0 if peak_power_watts <= 0.
    """
    if peak_power_watts <= 0.0:
        return 0.0

    eirp_dbm = 10.0 * math.log10(peak_power_watts * 1000.0) + antenna_gain_dbi
    erp_dbm = eirp_dbm - 2.15
    erp_watts = math.pow(10.0, (erp_dbm - 30.0) / 10.0)
    return erp_watts


# -- Public API ---------------------------------------------------------------


def get_assets_dir() -> str:
    """Return the assets directory path, creating it if it does not exist.

    The path is read from ``QgsSettings("waveshed/assets_dir")``.  When no
    setting has been stored, ``~/.aether/assets/`` is used as the default.
    """
    assets_dir = _assets_dir_setting()
    os.makedirs(assets_dir, exist_ok=True)
    return assets_dir


def _assets_dir_setting() -> str:
    """The configured assets directory (not created)."""
    assets_dir = QgsSettings().value("waveshed/assets_dir", _DEFAULT_ASSETS_DIR)
    return str(assets_dir) if assets_dir else _DEFAULT_ASSETS_DIR


def is_in_store(path: str, assets_dir: Optional[str] = None) -> bool:
    """True when *path* is a file directly inside the assets directory.

    Only those files are listed by :func:`list_assets` (the store is not
    scanned recursively), so only they reach the 360° and P2P tabs.
    """
    store = assets_dir or get_assets_dir()
    parent = os.path.dirname(os.path.abspath(path))
    return _same_path(parent, store)


def assets_signature(assets_dir: Optional[str] = None) -> tuple:
    """Cheap fingerprint of the store: its path + (name, size, mtime_ns) of
    every ``*.json`` in it.

    One ``os.scandir`` and no file read (the stat is free on Windows), so a
    tab can ask "did the store change?" when it is shown and re-read the
    assets only when it did. A missing or unreadable directory gives
    ``(dir, None)``; nothing is created.
    """
    store = assets_dir or _assets_dir_setting()
    entries = []
    try:
        with os.scandir(store) as it:
            for entry in it:
                if not entry.name.lower().endswith(".json"):
                    continue
                with contextlib.suppress(OSError):  # vanished while listing
                    if entry.is_file():
                        st = entry.stat()
                        entries.append((entry.name, st.st_size, st.st_mtime_ns))
    except OSError:
        return (store, None)
    return (store, tuple(sorted(entries)))


# Duplicate names already reported, so a refresh on every tab switch does not
# repeat the same warning: (name, paths) pairs.
_DUPLICATES_LOGGED: set = set()


def resolve_assets(assets: Optional[list[dict]] = None) -> dict[str, dict]:
    """THE name → asset rule shared by the 360° and P2P tabs.

    Assets are taken in file-path order and the FIRST file with a name wins;
    a later file carrying the same name is ignored (logged once per set of
    files). Unnamed assets are left out. Without *assets*, the store is
    listed.
    """
    if assets is None:
        assets = list_assets()
    by_name: dict[str, dict] = {}
    duplicates: dict[str, list[str]] = {}
    ordered = sorted(
        assets, key=lambda a: os.path.normcase(str(a.get("_path") or "")))
    for asset in ordered:
        name = asset.get("name") or ""
        if not name:
            continue
        if name in by_name:
            duplicates.setdefault(
                name, [str(by_name[name].get("_path") or "")],
            ).append(str(asset.get("_path") or ""))
            continue
        by_name[name] = asset
    for name, paths in duplicates.items():
        key = (name, tuple(paths))
        if key in _DUPLICATES_LOGGED:
            continue
        _DUPLICATES_LOGGED.add(key)
        _log(
            f"assets: {len(paths)} files carry the name \"{name}\" — "
            f"{paths[0]} is used, ignored: {', '.join(paths[1:])}. "
            "Rename or delete the extra file(s) in the Assets tab.",
        )
    return by_name


def unique_asset_path(
    name: str,
    assets_dir: Optional[str] = None,
    keep: Optional[str] = None,
) -> str:
    """A path in the store for an asset called *name* that overwrites nothing.

    ``<stem>.json``, else ``<stem>_2.json``, ``<stem>_3.json``, … where
    ``<stem>`` is the sanitized name. *keep* (an existing file of the asset
    being saved) counts as free, so re-saving under a name that maps to its
    own file stays in place.
    """
    store = assets_dir or get_assets_dir()
    stem = _sanitize_filename(name)
    counter = 1
    while True:
        filename = f"{stem}.json" if counter == 1 else f"{stem}_{counter}.json"
        path = os.path.join(store, filename)
        if keep and _same_path(path, keep):
            return os.path.abspath(keep)
        if not os.path.lexists(path):
            return os.path.abspath(path)
        counter += 1


def list_assets() -> list[dict]:
    """Scan the assets directory for ``.json`` files and load each one.

    Returns a list of asset dicts.  Every dict has an extra ``"_path"`` key
    that points to the source file on disk, so callers can save back or
    delete the asset later.

    Files that cannot be parsed as valid JSON are silently skipped.
    """
    assets_dir = get_assets_dir()
    assets: list[dict] = []

    for filename in sorted(os.listdir(assets_dir)):
        if not filename.lower().endswith(".json"):
            continue
        filepath = os.path.join(assets_dir, filename)
        if not os.path.isfile(filepath):
            continue
        try:
            asset = load_asset(filepath)
            asset["_path"] = filepath
            assets.append(asset)
        except (json.JSONDecodeError, OSError):
            # Skip corrupt or unreadable files.
            continue

    return assets


def load_asset(path: str) -> dict:
    """Load a single asset JSON file and return it as a dict.

    Handles backward compatibility: old-format files missing the new schema
    fields are filled in with sensible defaults so that callers always see
    the full schema.

    Args:
        path: Absolute path to the ``.json`` file.

    Returns:
        The asset dictionary with all schema fields present.

    Raises:
        OSError: If the file cannot be read.
        json.JSONDecodeError: If the file is not valid JSON.
    """
    with open(path, "r", encoding="utf-8") as f:
        asset = json.load(f)

    # --- Backward compatibility with old schema ---

    # Old field: "power_watts" -> new field: "peak_power_watts"
    if "peak_power_watts" not in asset and "power_watts" in asset:
        asset["peak_power_watts"] = asset.pop("power_watts")
    elif "peak_power_watts" not in asset:
        asset["peak_power_watts"] = _DEFAULT_ASSET["peak_power_watts"]

    # Old fields: "az_pattern_file" / "el_pattern_file" were paths.
    # New schema uses inline pattern data.  If old fields exist, keep them
    # but ensure the new fields are also present.
    if "azimuth_pattern" not in asset:
        asset["azimuth_pattern"] = {"data": []}
    if "elevation_pattern" not in asset:
        asset["elevation_pattern"] = {"data": []}

    # Fill missing new-schema fields with defaults.
    for key, default_val in _DEFAULT_ASSET.items():
        if key not in asset:
            asset[key] = default_val

    # A hand-edited file can carry a sub-minimum AGL default height, which would
    # then pre-fill every new site row with a height the job builder rejects.
    # Raise it here and say so, rather than raising: ``list_assets`` catches only
    # JSON/OS errors, so an exception would make the asset vanish from the picker
    # with no explanation. The authoritative rejection still stands at job build
    # time (``job_builder._validate_antenna_heights``) — this only stops a bad
    # file from seeding bad rows.
    height_error = default_height_error(
        asset["default_height_m"], asset["default_height_mode"],
    )
    if height_error is not None:
        asset["default_height_m"] = MIN_ANTENNA_AGL_M
        with contextlib.suppress(Exception):  # no QGIS available (unit tests)
            from qgis.core import Qgis, QgsMessageLog
            QgsMessageLog.logMessage(
                f"asset {os.path.basename(path)}: {height_error} "
                f"Using {MIN_ANTENNA_AGL_M:.1f} m instead.",
                "Waveshed", Qgis.MessageLevel.Warning,
            )

    # Recompute ERP for consistency.
    asset["erp_watts"] = compute_erp(
        asset["peak_power_watts"], asset["antenna_gain_dbi"]
    )

    return asset


def save_asset(asset: dict, path: Optional[str] = None) -> str:
    """Save an asset dict to a JSON file.

    Args:
        asset: The asset dictionary to persist.
        path:  Target file path, written (replaced) as given.  When *None*,
               a path is derived from the asset's ``"name"`` inside the
               assets directory that never overwrites an existing file
               (:func:`unique_asset_path`).

    Returns:
        The absolute path of the written file.

    The file is written to a temporary file beside the target and renamed
    over it, so a failed write leaves the previous file as it was.
    """
    if path is None:
        path = unique_asset_path(asset.get("name") or "asset")

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    # Strip the internal bookkeeping key before writing.
    data = {k: v for k, v in asset.items() if k != "_path"}

    _write_json_atomic(path, data)
    return os.path.abspath(path)


def save_renamed_asset(asset: dict, old_path: str) -> str:
    """Save an asset whose name changed: the file follows the name.

    A file inside the store is written to a fresh path derived from the new
    name (:func:`unique_asset_path`; its own path counts as free, so a name
    that maps to the same file is saved in place), and only then is the old
    file removed — write first, delete second, so a failure keeps the old
    file. A file outside the store is saved in place.

    Returns:
        The absolute path the asset now lives at.
    """
    if not is_in_store(old_path):
        return save_asset(asset, path=old_path)
    new_path = unique_asset_path(asset.get("name") or "asset", keep=old_path)
    saved = save_asset(asset, path=new_path)
    if not _same_path(saved, old_path):
        try:
            with contextlib.suppress(FileNotFoundError):  # already gone
                os.remove(old_path)
        except OSError as exc:
            _log(
                f"assets: renamed asset saved to {saved}, but the old file "
                f"{old_path} could not be removed ({exc}).",
            )
    return saved


def delete_asset(path: str) -> None:
    """Delete an asset file from disk.

    Args:
        path: Absolute path to the ``.json`` file to remove.

    Raises:
        OSError: If the file cannot be deleted.
    """
    os.remove(path)


def create_default_asset(name: str = "New Asset") -> dict:
    """Return a new asset dict populated with sensible defaults.

    Args:
        name: Display name for the new asset.

    Returns:
        A fresh asset dictionary (not yet saved to disk).
    """
    asset = dict(_DEFAULT_ASSET)
    # Deep-copy mutable nested dicts so callers don't share state.
    asset["azimuth_pattern"] = {"data": list(_DEFAULT_ASSET["azimuth_pattern"]["data"])}
    asset["elevation_pattern"] = {"data": list(_DEFAULT_ASSET["elevation_pattern"]["data"])}
    asset["name"] = name
    asset["erp_watts"] = compute_erp(asset["peak_power_watts"], asset["antenna_gain_dbi"])
    return asset


# -- Helpers ------------------------------------------------------------------


def _same_path(a: str, b: str) -> bool:
    """Path equality the way the file system sees it (case on Windows)."""
    return (os.path.normcase(os.path.abspath(a))
            == os.path.normcase(os.path.abspath(b)))


def _write_json_atomic(path: str, data: dict) -> None:
    """Write *data* to a temp file beside *path*, then rename it over *path*.

    The temp name does not end in ``.json``, so a half-written file is never
    listed as an asset; it is removed when anything fails.
    """
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):  # nothing written, or already gone
            os.remove(tmp)
        raise


def _log(message: str) -> None:
    with contextlib.suppress(Exception):  # no QGIS available (unit tests)
        from qgis.core import Qgis, QgsMessageLog
        QgsMessageLog.logMessage(message, "Waveshed", Qgis.MessageLevel.Warning)


def _sanitize_filename(name: str) -> str:
    """Turn an arbitrary asset name into a safe, lowercase filename stem.

    Non-alphanumeric characters (except hyphens) are replaced with
    underscores.  Leading/trailing underscores are stripped.
    """
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    sanitized = sanitized.strip("_")
    return sanitized.lower() if sanitized else "asset"
