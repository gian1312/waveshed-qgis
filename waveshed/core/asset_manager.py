"""Manage emitter/receiver asset definitions stored as JSON files.

Assets represent RF equipment (emitters, receivers) with properties like
frequency, power, antenna gain, antenna patterns, and default height.  Each
asset is persisted as a single JSON file inside the user's assets directory.

Modeled after the MPT SIGMA emitter management pattern, adapted for AETHER.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Optional

from qgis.core import QgsSettings


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
    settings = QgsSettings()
    assets_dir = settings.value("waveshed/assets_dir", _DEFAULT_ASSETS_DIR)
    if not assets_dir:
        assets_dir = _DEFAULT_ASSETS_DIR
    os.makedirs(assets_dir, exist_ok=True)
    return assets_dir


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

    # Recompute ERP for consistency.
    asset["erp_watts"] = compute_erp(
        asset["peak_power_watts"], asset["antenna_gain_dbi"]
    )

    return asset


def save_asset(asset: dict, path: Optional[str] = None) -> str:
    """Save an asset dict to a JSON file.

    Args:
        asset: The asset dictionary to persist.
        path:  Target file path.  When *None*, a path is generated from the
               asset's ``"name"`` field inside the assets directory.

    Returns:
        The absolute path of the written file.
    """
    if path is None:
        name = asset.get("name", "asset")
        filename = _sanitize_filename(name) + ".json"
        path = os.path.join(get_assets_dir(), filename)

    os.makedirs(os.path.dirname(path), exist_ok=True)

    # Strip the internal bookkeeping key before writing.
    data = {k: v for k, v in asset.items() if k != "_path"}

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    return os.path.abspath(path)


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


def _sanitize_filename(name: str) -> str:
    """Turn an arbitrary asset name into a safe, lowercase filename stem.

    Non-alphanumeric characters (except hyphens) are replaced with
    underscores.  Leading/trailing underscores are stripped.
    """
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    sanitized = sanitized.strip("_")
    return sanitized.lower() if sanitized else "asset"
