"""Shared helpers for reasoning about QGIS map layers.

Kept GUI-free so both the processing tabs and the map-converter tab can use
the same DEM/imagery classification without duplicating heuristics.
"""

from __future__ import annotations

from typing import Optional

from .min_alt import REF_AGL, normalize_reference

# Custom-property keys stamped on every layer the plugin itself adds to the
# project. Used to keep our own *result* outputs (coverage, P2P) out of the DEM
# source pickers — a coverage raster is never a valid terrain input. Terrain
# mosaics we create (role "terrain") ARE valid DEMs, so they are kept.
_OUTPUT_PROP = "aether/output"
_ROLE_PROP = "aether/role"
_MODEL_PROP = "aether/model"

# Custom properties describing a MIN_ALT surface. They survive a project
# save/reload, which is what lets the Altitude Explorer pick up where it left
# off in a session it did not start.
_REF_PROP = "aether/altitude_ref"      # "AGL" | "AMSL"
_DERIVED_FROM_PROP = "aether/derived_from"  # layer id this was computed from
_TERRAIN_PROP = "aether/terrain_dir"   # .abt directory the run used


def mark_aether_output(layer, role: str = "") -> None:
    """Stamp *layer* as plugin-created (with an optional *role*) so DEM pickers
    can tell our result rasters from terrain we generated."""
    try:
        layer.setCustomProperty(_OUTPUT_PROP, "1")
        if role:
            layer.setCustomProperty(_ROLE_PROP, role)
    except Exception:
        pass


def mark_aether_model(layer, model: str) -> None:
    """Stamp the propagation model (``LOS`` / ``ITM`` / ``MIN_ALT`` …) that
    produced *layer* so downstream tools (e.g. the Altitude Explorer) can find
    the layers they know how to drive."""
    try:
        if model:
            layer.setCustomProperty(_MODEL_PROP, str(model).upper())
    except Exception:
        pass


def aether_model(layer) -> str:
    """Return the propagation model stamped on *layer* (upper-case), or ""."""
    try:
        return str(layer.customProperty(_MODEL_PROP, "") or "").upper()
    except Exception:
        return ""


def is_min_alt_layer(layer) -> bool:
    """True if *layer* is a plugin MIN_ALT (minimum-LOS-altitude) raster."""
    return aether_model(layer) == "MIN_ALT"


def _set_prop(layer, key: str, value: str) -> None:
    try:
        if value:
            layer.setCustomProperty(key, str(value))
    except Exception:
        pass


def _get_prop(layer, key: str) -> str:
    try:
        return str(layer.customProperty(key, "") or "")
    except Exception:
        return ""


def mark_altitude_reference(layer, reference: str) -> None:
    """Stamp what a MIN_ALT layer's metres are measured from ("AGL"/"AMSL")."""
    _set_prop(layer, _REF_PROP, normalize_reference(reference))


def altitude_reference(layer) -> str:
    """Return a MIN_ALT layer's altitude reference, defaulting to ``"AGL"``.

    Layers produced before the above-sea-level view existed carry no stamp, and
    the solver only ever emitted AGL — so an unstamped layer is an AGL one.
    """
    return normalize_reference(_get_prop(layer, _REF_PROP) or REF_AGL)


def mark_derived_from(layer, source_layer_id: str) -> None:
    """Record which layer *layer* was computed from (e.g. its AGL original)."""
    _set_prop(layer, _DERIVED_FROM_PROP, source_layer_id)


def derived_from(layer) -> str:
    """Return the id of the layer this one was computed from, or ""."""
    return _get_prop(layer, _DERIVED_FROM_PROP)


def mark_terrain_dir(layer, terrain_dir: str) -> None:
    """Record the ``.abt`` terrain directory a result was computed over."""
    _set_prop(layer, _TERRAIN_PROP, terrain_dir)


def terrain_dir(layer) -> str:
    """Return the ``.abt`` terrain directory stamped on *layer*, or ""."""
    return _get_prop(layer, _TERRAIN_PROP)


def is_aether_output(layer) -> bool:
    """True if *layer* was added by the plugin (see :func:`mark_aether_output`)."""
    try:
        return str(layer.customProperty(_OUTPUT_PROP, "")) == "1"
    except Exception:
        return False


def aether_role(layer) -> str:
    """Return the plugin role stamped on *layer* ("coverage"/"p2p"/"terrain"), or ""."""
    try:
        return str(layer.customProperty(_ROLE_PROP, "") or "")
    except Exception:
        return ""


def hide_from_dem_picker(layer) -> bool:
    """True if *layer* is one of our own result rasters that should not be
    offered as a DEM source. Terrain mosaics we generate are kept (they are
    valid elevation), everything else we produced (coverage, P2P) is hidden.
    """
    return is_aether_output(layer) and aether_role(layer) != "terrain"

# Keywords that identify an XYZ tile source as encoded *elevation* data
# (valid terrain) rather than an ordinary basemap. Mapzen Terrarium and
# Mapbox Terrain-RGB are RGB-encoded, so band-count heuristics misfire on
# them — they must be recognised as DEMs, not imagery.
_TERRAIN_URL_HINTS = (
    "terrarium", "terrain-rgb", "terrain_rgb", "terrainrgb", "mapzen",
    "elevation", "mapbox.terrain", "/dem/", "_dem", "aws-terrain",
)
_IMAGERY_URL_HINTS = (
    "openstreetmap", "osm", "satellite", "aerial", "ortho", "imagery",
    "worldimagery", "world_imagery", "street", "basemap", "google",
    "bing", "esri", "carto", "stamen",
)


def classify_raster_layer(layer) -> str:
    """Classify *layer* as ``'dem'``, ``'imagery'``, or ``'unknown'``.

    Elevation analysis needs single-band Float32/Int16 rasters, or an XYZ
    tile source that *encodes* elevation (Mapzen Terrarium, Mapbox
    Terrain-RGB). Ordinary RGB basemaps (OpenStreetMap, satellite/aerial
    imagery) are **not** valid terrain even though they are raster layers —
    this returns ``'imagery'`` for those so callers can warn the user.
    Vector layers and anything without a data provider return ``'unknown'``.
    """
    source = ""
    try:
        source = (layer.source() or "").lower()
    except Exception:
        source = ""

    # XYZ tile layers are RGB-encoded regardless of purpose, so band/type
    # heuristics can't tell terrain from imagery — decide by the URL/params.
    if "type=xyz" in source:
        if any(h in source for h in _TERRAIN_URL_HINTS):
            return "dem"
        if any(h in source for h in _IMAGERY_URL_HINTS):
            return "imagery"
        if "terrain" in source:  # e.g. interpretation=terrainrgb
            return "dem"
        return "imagery"  # unknown XYZ basemap — safer to flag than to trust

    provider = None
    try:
        provider = layer.dataProvider()
    except Exception:
        provider = None
    if provider is None:
        return "unknown"

    try:
        if provider.bandCount() >= 3:
            return "imagery"
    except Exception:
        pass

    try:
        from qgis.core import Qgis
        if provider.dataType(1) == Qgis.DataType.Byte:
            return "imagery"
    except Exception:
        pass

    return "dem"


def dem_layer_warning(layer) -> Optional[str]:
    """Return a user-facing warning if *layer* is not usable as a DEM, else None."""
    if classify_raster_layer(layer) != "imagery":
        return None
    try:
        name = layer.name()
    except Exception:
        name = "This layer"
    return (
        f"'{name}' looks like a map/image layer (RGB or basemap tiles), not a "
        f"DEM.\n\nElevation data is required for propagation analysis — use a "
        f"single-band Float32/Int16 DEM, a terrain folder, or an elevation XYZ "
        f"source (e.g. Mapzen Terrarium)."
    )
