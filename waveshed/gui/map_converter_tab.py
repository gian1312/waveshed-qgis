"""Map Converter tab -- fuse multiple raster/vector layers into .abt tiles.

Embeddable QWidget for the main AETHER dialog.  Lets users build a
prioritised layer stack (base DEM, high-res overlays, buildings), choose
per-layer extents and the output resolutions, and batch-convert everything
to .abt terrain tiles via ``aether_converter ingest``.

The parent dialog must provide ``self.iface`` (QgisInterface).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import QThread, QTimer, pyqtSignal, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QApplication,
    QPlainTextEdit,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsMapLayerProxyModel,
    QgsMessageLog,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsRectangle,
    QgsSettings,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapLayerComboBox, QgsRubberBand

from ..core import (openfreemap, osm_buildings, output_estimate,
                    hashes, process_memory, run_progress, terrain_adapter,
                    terrain_index)
from . import terrain_folder_scan
from ..core.binary_manager import find_binary
# The vector→FlatGeobuf machinery lives in core/ (business logic, no GUI): the
# Site Analysis path needs it just as much, and two copies of a conversion
# cache key is two caches. Imported back under its own names so this module
# reads exactly as it did when it owned them.
from ..core.buildings_source import (  # noqa: F401 — re-exported for tests
    _convert_to_fgb,
    _fgb_cache_name,
    _fgb_translate_options,
    _split_sublayer,
    fgb_cache_dir,
    resolve_buildings_source,
)
from ..core.layer_utils import classify_raster_layer
from ..core.terrain_adapter import (
    ABT_EXTENT_DEG,
    _estimate_abt_disk_mb,
    _expand_bbox,
    _subtile_degrees,
    _tile_params,
    bboxes_intersect,
    build_tile_sources,
)

TAG = "Waveshed"

#: The engine launch flags (no console window on Windows), one definition.
_SUBPROCESS_FLAGS = terrain_adapter._SUBPROCESS_FLAGS

#: QgsSettings key of the "Keep pool tiles" box (default off: the pool
#: tiles a run downloads are removed once it has succeeded).
_KEEP_POOL_KEY = "waveshed/map_converter/keep_pool_tiles"

#: Tooltip of that box.
_KEEP_POOL_TIP = (
    "Keep the copy of each downloaded terrain tile in the shared terrain "
    "pool (the terrain cache Site Analysis reads).\n\n"
    "The pool copy only pays off when Site Analysis later runs from the "
    "same XYZ source over this area; otherwise it doubles the disk use of "
    "the conversion.\n\n"
    "Off (default): after a SUCCESSFUL run, the pool tiles this run "
    "downloaded are deleted (they are kept after Stop or a failure, so the "
    "next run can reuse them). Pool tiles that were there before the run "
    "and the downloaded source tiles (<cache>/xyz, what makes a rerun "
    "free) are never deleted.")


def _keep_pool_tiles_setting() -> bool:
    """The stored "Keep pool tiles" choice (default False)."""
    value = QgsSettings().value(_KEEP_POOL_KEY, False)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)

# .abt tile geographic extent per resolution.  Controls how large each
# .abt file is on disk (pixels = extent_deg / (res_m / 111111)) and, more
# importantly, how much of aether_core's single terrain-atlas allocation one
# tile consumes — see core.terrain_adapter.ABT_EXTENT_DEG, which is now the
# one definition for both this tab and the analysis path.  Aliased under the
# old private name because this module refers to it throughout.
_ABT_EXTENT_DEG = ABT_EXTENT_DEG


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_layer_filters(combo) -> None:
    """Restrict *combo* to raster + vector layers, without the deprecation.

    ``setFilters`` has two overloads. The old one takes
    ``QgsMapLayerProxyModel.Filters`` and is deprecated; the current one takes
    ``Qgis.LayerFilters``. OR-ing two ``Qgis.LayerFilter`` members can yield a
    plain int under sip, which then binds to the *deprecated* overload — so
    passing modern enum members is not by itself enough to avoid the warning.
    Constructing ``Qgis.LayerFilters`` explicitly pins the modern overload.

    Ordered newest-first; each step falls through on the QGIS versions where
    that name does not exist.
    """
    try:
        combo.setFilters(
            Qgis.LayerFilters(
                Qgis.LayerFilter.RasterLayer | Qgis.LayerFilter.VectorLayer
            )
        )
        return
    except (AttributeError, TypeError):
        pass
    try:
        combo.setFilters(
            Qgis.LayerFilter.RasterLayer | Qgis.LayerFilter.VectorLayer
        )
        return
    except (AttributeError, TypeError):
        pass
    combo.setFilters(
        QgsMapLayerProxyModel.Filter.RasterLayer
        | QgsMapLayerProxyModel.Filter.VectorLayer
    )


def _abt_tile_name(res_m: int, ul_lat: float, ul_lon: float) -> str:
    """Output file name for one Map Converter tile.

    Thin wrapper over the ONE naming scheme both terrain paths share —
    ``terrain_adapter._tile_params``' ``tile_N{lat:.2f}E{lon:.2f}_{res}m.abt``.
    The tab used to have its own ``Tile_…_r16sint.abt`` spelling, which made
    Map-Converter output invisible to anything expecting the canonical name.
    """
    return f"tile_N{ul_lat:.2f}E{ul_lon:.2f}_{res_m}m.abt"


#: First aether_converter whose ``plan`` snaps the bbox with the epsilon and
#: the rounded edge of :func:`_snap_bbox` (toolkit 0.2.9). Older or unknown →
#: the bare floor/ceil, so the cross-check keeps agreeing with converters
#: <= 0.2.8.
_SNAP_EPSILON_MIN_VERSION = "0.2.9"

#: That epsilon, in grid steps.
_SNAP_EPS = 1e-9

#: Decimals a snapped edge is rounded to (epsilon snap only).
_SNAP_EDGE_DECIMALS = 9


def _snap_bbox(bbox: Dict[str, float], resolutions: List[int],
               epsilon: bool = False) -> Dict[str, float]:
    """Snap *bbox* outward onto the finest tile grid the run will use.

    Grid size comes from ``terrain_adapter._subtile_degrees`` — the extent
    ladder plus the u16 row-stride guard — the same authority the analysis
    path and the engine's ``plan`` subcommand reproduce.

    *epsilon* (the run's converter is >= :data:`_SNAP_EPSILON_MIN_VERSION`,
    :func:`_converter_snaps_with_epsilon`): ``idx = floor(x / finest +
    1e-9)`` for south/west, ``ceil(x / finest - 1e-9)`` for north/east, edge
    ``round(idx * finest, 9)`` — the toolkit's ``plan`` snaps with the
    identical formula from 0.2.9. The epsilon keeps an edge ON the grid whose
    quotient lands a hair past it where it is (S 47.3 / 0.1 =
    472.99999999999994 used to add a row of tiles wholly outside the area);
    the rounding makes the edge the grid line itself, where the enumeration's
    6-decimal steps arrive (E 11.6466: 117 * 0.1 = 11.700000000000001 kept a
    69th 2 m column, E11.70, under ``lon < east``). Without it the bare
    floor/ceil of <= 0.2.8. Both sides must always snap alike: the pre-run
    cross-check aborts on any difference. :func:`_enumerate_tiles` takes the
    same *epsilon*.
    """
    finest = min(_subtile_degrees(r) for r in resolutions)
    if not epsilon:
        return {
            "south": math.floor(bbox["south"] / finest) * finest,
            "north": math.ceil(bbox["north"] / finest) * finest,
            "west": math.floor(bbox["west"] / finest) * finest,
            "east": math.ceil(bbox["east"] / finest) * finest,
        }

    def line(idx: int) -> float:
        return round(idx * finest, _SNAP_EDGE_DECIMALS)
    return {
        "south": line(math.floor(bbox["south"] / finest + _SNAP_EPS)),
        "north": line(math.ceil(bbox["north"] / finest - _SNAP_EPS)),
        "west": line(math.floor(bbox["west"] / finest + _SNAP_EPS)),
        "east": line(math.ceil(bbox["east"] / finest - _SNAP_EPS)),
    }


def _converter_snaps_with_epsilon(converter_exe: str) -> bool:
    """Whether the converter's ``plan`` snaps with :func:`_snap_bbox`'s
    epsilon (``--version`` >= :data:`_SNAP_EPSILON_MIN_VERSION`, probed
    through ``binary_manager.probe_converter_version`` like the other
    gates; unknown = no)."""
    from ..core import binary_manager
    try:
        version = binary_manager.probe_converter_version(converter_exe)
        return (version is not None and binary_manager.compare_versions(
            version, _SNAP_EPSILON_MIN_VERSION) >= 0)
    except Exception:  # noqa: BLE001 — a probe never takes the run down
        return False


def _enumerate_tiles(
    bbox: Dict[str, float], resolutions: List[int], epsilon: bool = False,
) -> List[Tuple[int, Dict[str, Any]]]:
    """Every ``(resolution, tile_params)`` a conversion of *bbox* produces.

    One definition for the pre-run estimate and for the worker that builds the
    jobs.  They used to walk the grid separately, over *different* extents (the
    worker snaps outward first), so the estimate could name a different number
    of tiles than the run then built.

    Tile geometry comes from ``terrain_adapter`` — ``_subtile_degrees`` for
    the extent (ladder + ``_ABT_MAX_SIZE_PX`` u16 row-stride guard) and
    ``_tile_params`` for the sizing — the same formulas the analysis path
    uses and the engine's ``plan`` subcommand cross-checks.

    *epsilon* (with a bbox from ``_snap_bbox(epsilon=True)``, converter >=
    0.2.9 — ``plan.rs`` does the same, toolkit CONTRACT changelog 43): each
    resolution's first row/column is ``floor(edge / ext + 1e-9) * ext``. The
    rounded edge IS the grid line (47.3), and the bare ``floor(47.3 / 0.1)``
    = 472 would start a whole step out — the row the snap's epsilon just
    removed. Without it the bare ``floor(edge / ext) * ext`` of <= 0.2.8.
    """
    def start(edge: float, ext: float) -> float:
        if epsilon:
            return math.floor(edge / ext + _SNAP_EPS) * ext
        return math.floor(edge / ext) * ext

    tiles: List[Tuple[int, Dict[str, Any]]] = []
    for res in sorted(resolutions):
        ext = _subtile_degrees(res)
        lat = start(bbox["south"], ext)
        while lat < bbox["north"]:
            lon = start(bbox["west"], ext)
            while lon < bbox["east"]:
                tiles.append((res, _tile_params(ext, lat, lon, res)))
                lon = round(lon + ext, 6)
            lat = round(lat + ext, 6)
    return tiles


def _estimate_tile_count_and_mb(
    bbox: Dict[str, float],
    resolutions: List[int],
    output_dir: str = "",
    overwrite: bool = True,
    entries: Optional[List[Any]] = None,
) -> Tuple[int, int]:
    """Return ``(tile_count, total_mb)`` for the tiles a run would build.

    Prices exactly what the worker will do: the same outward-snapped extent,
    without the tiles outside every terrain row's extent of *entries* (the
    stack's rows; None = one row whose extent is *bbox*), which the worker
    skips (:func:`_tiles_within_sources`), and — unless *overwrite* —
    without the WHOLE tiles already sitting in *output_dir*, which the
    worker skips too (a short file is rebuilt, so it is priced;
    :func:`_output_tile_whole`). The per-drive disk need with the
    fingerprint rebuilds, the pool and the temp copies is
    :func:`_estimate_disk_need`.
    """
    if not resolutions:
        return 0, 0
    tiles = _enumerate_tiles(_snap_bbox(bbox, resolutions), resolutions)
    tiles, _outside = _tiles_within_sources(
        tiles, entries if entries is not None
        else [{"layer_type": "raster", "extent": bbox}])
    if output_dir and not overwrite:
        # One listing rather than a stat per tile: this runs on every checkbox
        # toggle and a big area is tens of thousands of tiles.
        listing = terrain_adapter._pool_listing(output_dir)
        # `filename` from _tile_params is the canonical name the worker
        # writes (and skips on) — the estimate must consult the same one.
        tiles = [(res, t) for res, t in tiles
                 if not _output_tile_whole(output_dir, t, listing)]
    return len(tiles), _estimate_abt_disk_mb([t for _res, t in tiles])


def _output_tile_whole(output_dir: str, tile: Dict[str, Any],
                       listing: Optional[Dict[str, Any]] = None) -> bool:
    """True when *output_dir* holds tile *tile* whole — at least header +
    ``size × stride`` bytes for its spec (``terrain_adapter._abt_whole_bytes``),
    or, for a shorter file, whole by its own header: the pool's rule
    (``terrain_adapter._listed_tile_whole``). The "exists" skip of the Map
    Converter: a short file (a killed run's, an empty one) is rebuilt, never
    kept. *listing* (one ``terrain_adapter._pool_listing`` of the directory)
    avoids a stat per tile."""
    if listing is not None:
        return terrain_adapter._listed_tile_whole(listing, tile, output_dir)
    path = os.path.join(output_dir, tile["filename"])
    try:
        if not os.path.isfile(path):
            return False
        if os.path.getsize(path) >= terrain_adapter._abt_whole_bytes(
                int(tile["size_px"])):
            return True
    except (OSError, KeyError, TypeError, ValueError):
        return False
    return terrain_adapter._tile_is_whole(path)


# Folder scanning: terrain_adapter.list_terrain_files is the ONE folder
# scanner (recursive, sorted ascending — earlier-sorted filename wins for
# overlapping files). Behaviour change vs the tab's old flat os.listdir scan:
# files in SUBFOLDERS are now included, same as Site Analysis.


def _detect_resolution(layer) -> Optional[float]:
    """Auto-detect a raster layer's ground resolution in metres.

    Thin alias: the body moved to
    :func:`terrain_adapter.layer_native_resolution_m` so Site Analysis and
    this tab share ONE detection (and one export-resolution rule below).
    """
    return terrain_adapter.layer_native_resolution_m(layer)


# XYZ native resolution: terrain_adapter.xyz_native_resolution_m is the ONE
# helper (resolve_zmax-based — validated, clamped to what the service really
# publishes). The tab's old copy trusted the raw zmax parameter.


def _detect_layer_type(layer) -> str:
    """Classify a layer as 'terrain', 'imagery', or 'vector'."""
    if isinstance(layer, QgsVectorLayer):
        return "vector"
    if not isinstance(layer, QgsRasterLayer):
        return "unknown"
    # Shared, XYZ-aware classifier: elevation-encoded XYZ tiles (Terrarium,
    # Mapbox Terrain-RGB) count as terrain even though they are RGB.
    kind = classify_raster_layer(layer)
    return "terrain" if kind == "dem" else kind


def _folder_index(folder: str):
    """The folder's footprint index, refreshed (synchronous — worker threads
    and the torture runner only; the tab scans through
    gui/terrain_folder_scan, off the GUI thread)."""
    from ..core import terrain_index
    index = terrain_index.get_index(folder)
    index.refresh(max_age_s=terrain_index.FRESH_S)
    return index


def _detect_folder_crs(folder: str) -> Optional[str]:
    """The folder's dominant CRS (most files), from the folder index.

    Display / suggestion only: each file is read in its OWN CRS, and a file
    without one is never given this one silently (the tab asks)."""
    try:
        from .terrain_folder_scan import suggested_crs
        return suggested_crs(_folder_index(folder))
    except Exception:  # noqa: BLE001
        return None


def _index_finest_res(index) -> Optional[float]:
    vals = [e["px_m"] for e in index.entries.values()
            if e.get("readable") and e.get("px_m")]
    return round(min(vals), 1) if vals else None


def _detect_folder_resolution(folder: str) -> Optional[float]:
    """The folder's finest ground resolution in metres, from the index."""
    try:
        return _index_finest_res(_folder_index(folder))
    except Exception:  # noqa: BLE001
        return None


#: Hidden manifest in a Map Converter output directory: per tile, the
#: fingerprint of the stack inputs it was built from (files: path + size +
#: mtime from the folder index) and — schema 2 — what buildings it carries
#: (``terrain_adapter.BUILDINGS_*``, read by Site Analysis and P2P when the
#: folder is prepared terrain). A tile whose inputs changed is rebuilt even
#: without "Rebuild existing tiles"; a tile with no record (built before the
#: manifest existed, or from a rendered service) keeps the old rule. The
#: format lives in core (``terrain_adapter.read_tiles_manifest`` /
#: ``write_tiles_manifest``).
_OUTPUT_MANIFEST = terrain_adapter.TILES_MANIFEST


def _read_output_manifest(output_dir: str) -> Dict[str, str]:
    """name → fingerprint record (schema 1 and 2 alike)."""
    return terrain_adapter.read_tiles_manifest(output_dir).tiles


#: ONE lock around every read-modify-write of an output manifest (the
#: ingest batches of a run record their tiles one after the other).
_MANIFEST_LOCK = terrain_adapter._TILES_MANIFEST_LOCK


def _write_output_manifest(output_dir: str, updates: Dict[str, Optional[str]],
                           buildings: Optional[Dict[str, Optional[str]]] = None
                           ) -> None:
    """Merge *updates* (name → fingerprint; a false value drops the record)
    and their *buildings* records (name → ``terrain_adapter.BUILDINGS_*``;
    none given = unknown) into *output_dir*'s manifest (schema 2,
    ``terrain_adapter.write_tiles_manifest``: read-modify-write under one
    lock, temp file + ``replace_with_retry``; best effort)."""
    terrain_adapter.write_tiles_manifest(output_dir, updates, buildings)


def _stop_if_cancelled(should_cancel: Optional[Callable[[], bool]],
                       scratch: Optional[List[str]] = None) -> None:
    """The per-tile Cancel poll of the worker's long loops: raise
    :class:`terrain_adapter.ConverterCancelled` when *should_cancel()* is
    true, removing *scratch* first — a loop that raises never hands its
    temp files to the caller's cleanup."""
    if should_cancel is None or not should_cancel():
        return
    for path in scratch or ():
        try:
            os.remove(path)
        except OSError:
            pass
    raise terrain_adapter.ConverterCancelled()


def _files_index(ent: Dict[str, Any]) -> Optional[Any]:
    """The run's ONE ``terrain_adapter.FootprintIndex`` of a ``files`` entry
    (built on first use — the worker builds it before the fingerprints;
    kept on the entry, so the fingerprints, the XYZ cover check and the job
    build all match each tile against the files once)."""
    infos = ent.get("infos")
    idx = ent.get("index")
    if idx is None or idx.infos is not infos:
        idx = ent["index"] = terrain_adapter.footprint_index(infos)
    return idx


def _tile_bbox_of(res: int, tile: Dict[str, Any]) -> Dict[str, float]:
    """WGS84 bbox of one enumerated output tile — the arithmetic the
    fingerprints and the job build use (same floats → the index's kept
    match is reused)."""
    sub = _subtile_degrees(res)
    return {"north": tile["ul_lat"], "south": tile["ul_lat"] - sub,
            "west": tile["ul_lon"], "east": tile["ul_lon"] + sub}


# ---------------------------------------------------------------------------
# Buildings of a run (Add Buildings… file, Download Buildings… = OpenFreeMap)
# ---------------------------------------------------------------------------

#: Source of a Download Buildings… row. Unchanged since the Overpass days
#: (2026-10-22: the row fetches OpenFreeMap z14 tiles now), so a stack made
#: then still means "OSM buildings over this row's extent".
_OSM_PLACEHOLDER = "[OSM Download — set extent, download at convert]"
_OSM_MARKER = "[OSM Download"

#: Progress slot and model activity of the OpenFreeMap building fetch.
_BUILDINGS_SLOT = ("buildings",)
_BUILDINGS_ACTIVITY = "Downloading buildings (OpenFreeMap z14)"
#: The step a batch's slot names while its converter waits for its view's
#: marker (aether_converter >= 0.2.12): ``Converting band 2/3: waiting for
#: building tiles``.
_WAITING_STEP = "waiting for building tiles"

#: How far past its own edge a z14 tile's buildings can reach an output
#: tile (``osm_buildings.REACH_DEG``: the vector-tile buffer, taken at 1/16
#: tile).
_OSM_REACH_DEG = osm_buildings.REACH_DEG

#: Manifest record of a tile an ingest batch wrote while its buildings
#: could not be verified: never a fingerprint, so the next run rebuilds it
#: ("no record → kept" must not keep it). Its buildings record is
#: ``terrain_adapter.BUILDINGS_UNVERIFIED``.
_BUILDINGS_UNVERIFIED = "buildings-unverified"


def _is_osm_source(source: Any) -> bool:
    """Is *source* a Download Buildings… row's (resolved) source?"""
    return isinstance(source, str) and source.startswith(_OSM_MARKER)


def _is_osm_row(entry: Any) -> bool:
    """A Download Buildings… row — a ``_LayerEntry`` or the worker's dict."""
    if isinstance(entry, dict):
        return (entry.get("layer_type") == "buildings"
                and (_is_osm_source(entry.get("resolved_source"))
                     or _is_osm_source(entry.get("source_path"))))
    return (getattr(entry, "layer_type", None) == "buildings"
            and _is_osm_source(getattr(entry, "source_path", "")))


def _fmt_bbox(b: Dict[str, float]) -> str:
    return (f"S={b['south']:.4f} N={b['north']:.4f} "
            f"W={b['west']:.4f} E={b['east']:.4f}")


def _osm_rows_problem(layers: List[Any]) -> Optional[str]:
    """Why the Download Buildings… rows of *layers* cannot run, else None:
    a row without an extent (it would download nothing), or one whose
    extent misses the terrain extent (no tile would get its buildings).
    The worker refuses with it; ``_on_run`` asks the same up front."""
    combined = _union_extent(layers)
    for k, e in enumerate(layers, start=1):
        if not _is_osm_row(e):
            continue
        extent = e["extent"] if isinstance(e, dict) else e.extent
        if not extent:
            return (f"The OSM buildings row (row {k}) has no extent. Select "
                    f"it and use 'Draw Extent for Layer' or 'Extent from "
                    f"Layer' — its extent is the area whose buildings are "
                    f"downloaded.")
        if combined is not None and not bboxes_intersect(extent, combined):
            return (f"The OSM buildings row (row {k}) covers "
                    f"{_fmt_bbox(extent)}, outside the terrain extent "
                    f"{_fmt_bbox(combined)} — no tile would get its "
                    f"buildings. Set its extent over the terrain.")
    return None


#: The Download Buildings… rows of a run: OpenFreeMap z14 vector tiles over
#: the rows' extents — ``osm_buildings.BuildingCover`` (the ONE cover rule,
#: shared with Site Analysis; ``cover``, ``members``, ``tag``, ``needed``,
#: ``view_names``, ``present`` set by the fetch).
_OsmBuildings = osm_buildings.BuildingCover


class _RunBuildings:
    """What a run's ingest jobs burn in: the converted building FILE of an
    Add Buildings… row (``buildings_file``) and/or the OpenFreeMap tiles of
    the Download Buildings… rows (``buildings_pbf_dir`` — the converter
    field, source id and store Site Analysis uses). :meth:`tag` is the
    tile's buildings part of its stack fingerprint; a file alone gives the
    same string the plain path always gave."""

    def __init__(self, file: Optional[str] = None,
                 osm: Optional[_OsmBuildings] = None) -> None:
        self.file = file or None
        self.osm = osm
        self._file_tag = ""
        if self.file:
            try:
                self._file_tag = terrain_adapter.source_fingerprint(self.file)
            except Exception:  # noqa: BLE001
                self._file_tag = self.file

    def tag(self, res: int, tile: Dict[str, Any]) -> str:
        parts = [self._file_tag] if self._file_tag else []
        if self.osm is not None:
            osm = self.osm.tag(res, tile)
            if osm:
                parts.append(osm)
        return "+".join(parts)


def _buildings_spec(buildings: Any) -> Optional[_RunBuildings]:
    """*buildings* as a :class:`_RunBuildings` (None, a plain converted file
    path — older callers and tests — or one already)."""
    if buildings is None or isinstance(buildings, _RunBuildings):
        return buildings
    return _RunBuildings(file=buildings) if buildings else None


def _buildings_record(buildings: Any, res: int, tile: Dict[str, Any]) -> str:
    """A built tile's buildings record for the manifest (schema 2): what its
    job burned — ``file`` (a building file row), ``openfreemap:z14`` (the
    OpenFreeMap rows reach it — every z14 tile it needs was fetched or
    answered "no data"), both joined by "+", or ``none``. Written only for
    a batch whose converter report was checked clean."""
    spec = _buildings_spec(buildings)
    parts = []
    if spec is not None and spec.file:
        parts.append(terrain_adapter.BUILDINGS_FILE)
    if spec is not None and spec.osm is not None and spec.osm.members(res,
                                                                      tile):
        parts.append(terrain_adapter.BUILDINGS_OSM)
    return "+".join(parts) or terrain_adapter.BUILDINGS_NONE


def _stack_tile_fingerprints(
    expected, resolved_entries, buildings_path,
    should_cancel: Optional[Callable[[], bool]] = None,
    files_only: Optional[Dict[str, Optional[str]]] = None,
) -> Dict[str, Optional[str]]:
    """Per output tile, a fingerprint of everything in the stack that feeds
    it (None for every tile when a rendered entry is in the stack — a
    server answer has no file to fingerprint).

    *buildings_path*: the run's buildings (:func:`_buildings_spec` — None, a
    converted file, or a :class:`_RunBuildings`); their part of each tile's
    fingerprint is :meth:`_RunBuildings.tag` (OpenFreeMap: per tile, the
    z14 tiles that reach it — known before the fetch).

    *files_only* (a dict, filled in place): per tile the fingerprint of the
    Phase-2 stack alone (:func:`_phase2_entries` — the files above the first
    XYZ entry, no pool component), tagged ``_FILES_COMPLETE_TAG`` so it never
    equals a full-stack fingerprint. It is the manifest record of a tile the
    files completed in Phase 2: a later run accepts it while those files are
    unchanged, whatever the pool below them does. None where the Phase-2
    stack holds no files entry.

    *should_cancel()* is polled per tile (a big folder stack makes this a
    long loop); true → :class:`terrain_adapter.ConverterCancelled`."""
    if any(e.get("kind") == "rendered" for e in resolved_entries):
        if files_only is not None:
            files_only.update({t["filename"]: None for _r, t in expected})
        return {t["filename"]: None for _r, t in expected}
    n_prefix = len(_phase2_entries(resolved_entries))
    prefix_files = any(e.get("kind") == "files"
                       for e in resolved_entries[:n_prefix])
    spec = _buildings_spec(buildings_path)
    out: Dict[str, Optional[str]] = {}
    for res, tile in expected:
        _stop_if_cancelled(should_cancel)
        btag = spec.tag(res, tile) if spec is not None else ""
        sub = _subtile_degrees(res)
        tb = {"north": tile["ul_lat"], "south": tile["ul_lat"] - sub,
              "west": tile["ul_lon"], "east": tile["ul_lon"] + sub}
        parts = []
        for ent in resolved_entries:
            if ent.get("kind") == "files":
                parts.append(terrain_adapter.tile_source_fingerprint(
                    ent["infos"], tb, tile["exact_res_m"],
                    index=_files_index(ent)))
            elif ent.get("kind") == "xyz":
                parts.append(terrain_adapter.source_identity(ent.get("uri", "")))
        if files_only is not None:
            files_only[tile["filename"]] = hashes.md5("\n".join(
                parts[:n_prefix] + [btag, _FILES_COMPLETE_TAG]).encode(
                    "utf-8")).hexdigest() \
                if prefix_files else None
        parts.append(btag)
        out[tile["filename"]] = hashes.md5(
            "\n".join(parts).encode("utf-8")).hexdigest()
    return out


# NB: file-vs-extent filtering is generic now — per-file WGS84 bounds come
# from terrain_adapter.source_file_info (GDAL geotransform + a QGIS coordinate
# transform for ANY CRS). The historical LV95-only approximation and the
# CRS special-casing that surrounded it are gone.


# ---------------------------------------------------------------------------
# Pre-run plan cross-check (engine `plan` subcommand)
# ---------------------------------------------------------------------------

_PLAN_SCHEMA = "aether-plan/1"

#: Exact user-facing message when the installed engine has no `plan`
#: subcommand — contract text, matched by tests.
_ENGINE_PREDATES_MSG = ("engine binaries predate this plugin version — "
                        "update them")

# clap error spellings across versions for an unknown subcommand.
_UNKNOWN_SUBCOMMAND_HINTS = (
    "unrecognized subcommand", "unknown subcommand", "invalid subcommand",
    "wasn't expected", "wasn't recognized",
)


def _run_plan(converter_exe: str, bbox: Dict[str, float],
              resolutions: List[int]) -> Dict[str, Any]:
    """Run ``aether_converter plan`` for *bbox*/*resolutions*; parsed JSON.

    *bbox* must be the RAW (unsnapped) bbox — the engine snaps it once
    itself, and snapping is not idempotent on this float grid (see the
    caller in ``_MapConverterWorker.run``).

    Hard errors only: a binary without the subcommand aborts with
    ``_ENGINE_PREDATES_MSG``; any other failure or unparseable output aborts
    with the engine's own words. There is no fallback — running without the
    cross-check is exactly the silent-drift failure it exists to prevent.
    """
    cmd = [
        converter_exe, "plan",
        "--south", repr(float(bbox["south"])),
        "--north", repr(float(bbox["north"])),
        "--west", repr(float(bbox["west"])),
        "--east", repr(float(bbox["east"])),
        "--resolutions", ",".join(str(r) for r in sorted(resolutions)),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              creationflags=_SUBPROCESS_FLAGS)
    except OSError as exc:
        raise RuntimeError(
            f"Could not run the engine's plan check ({converter_exe}): {exc}"
        ) from exc

    if proc.returncode != 0:
        err = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
        if any(h in err.lower() for h in _UNKNOWN_SUBCOMMAND_HINTS):
            raise RuntimeError(
                f"{_ENGINE_PREDATES_MSG}.\n\nThe installed aether_converter "
                f"({converter_exe}) has no 'plan' subcommand, so the plugin "
                "cannot verify that it will produce the same tile grid. "
                "Update the engine binaries (Settings → Download binaries)."
            )
        raise RuntimeError(
            f"aether_converter plan failed (exit {proc.returncode}):\n"
            f"{err[-2000:]}"
        )

    try:
        doc = json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError(
            f"aether_converter plan produced unparseable output ({exc}). "
            f"Engine: {converter_exe}"
        ) from exc
    if not isinstance(doc, dict) or doc.get("schema") != _PLAN_SCHEMA:
        raise RuntimeError(
            f"aether_converter plan returned schema "
            f"{doc.get('schema') if isinstance(doc, dict) else type(doc)!r}, "
            f"expected {_PLAN_SCHEMA!r}. Engine: {converter_exe}"
        )
    return doc


def _plan_cross_check(converter_exe: str, bbox: Dict[str, float],
                      resolutions: List[int],
                      expected: List[Tuple[int, Dict[str, Any]]]) -> None:
    """Abort (RuntimeError) unless the engine's plan matches *expected*.

    *bbox* is the RAW combined bbox (both sides snap it exactly once, from
    bit-identical input); *expected* is the Python enumeration
    (``_enumerate_tiles`` over the Python-snapped bbox — the equality of the
    two enumerations is precisely what the check guarantees).
    Tile count and filenames must agree exactly — a mismatch
    means plugin and engine disagree about tile geometry, and every tile the
    run wrote would land on the wrong grid.
    """
    doc = _run_plan(converter_exe, bbox, resolutions)
    ours = sorted(t["filename"] for _res, t in expected)
    theirs = sorted(t.get("filename", "?") for t in doc.get("tiles", []))
    count_theirs = doc.get("tile_count")
    if count_theirs == len(ours) and theirs == ours:
        return
    only_ours = [n for n in ours if n not in set(theirs)]
    only_theirs = [n for n in theirs if n not in set(ours)]
    detail = ""
    if only_ours:
        detail += f"\nFirst tile only the plugin has: {only_ours[0]}"
    if only_theirs:
        detail += f"\nFirst tile only the engine has: {only_theirs[0]}"
    raise RuntimeError(
        "Tile-grid mismatch between plugin and engine: the plugin "
        f"enumerates {len(ours)} tile(s), the engine's plan reports "
        f"{count_theirs} ({len(theirs)} filenames).{detail}\n\n"
        "The run was aborted — converting would write tiles on a grid the "
        "engine disagrees with. Update the engine binaries and the plugin "
        "to matching versions."
    )


def _build_tile_jobs(
    expected: List[Tuple[int, Dict[str, Any]]],
    output_dir: str,
    overwrite: bool,
    resolved_entries: List[Dict[str, Any]],
    buildings_path: Optional[str],
    tmp_dir: str,
    rebuild: Optional[set] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    pbf_dir: Optional[str] = None,
    pbf_ready: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Ingest jobs for one Map Converter run; ``(jobs, temp files)``.

    Buildings (*buildings_path*, see :func:`_buildings_spec`): a converted
    file goes into every job as ``buildings_file``; with OpenFreeMap rows,
    every job whose tile their z14 tiles reach (:meth:`_OsmBuildings.members`)
    gets ``buildings_pbf_dir`` = *pbf_dir* — the batch's view of the
    fetched tiles (contract §9a: an ingest field, decoded once per batch) —
    and *pbf_ready* (``buildings_pbf_ready`` / ``buildings_pbf_wait_s``:
    the batch is launched before its tiles are in, aether_converter >=
    0.2.12, toolkit CONTRACT item 48; on >= 0.2.14, changelog 51,
    *pbf_dir* is THE store and *pbf_ready* ``buildings_pbf_list`` /
    ``buildings_pbf_wait_s`` — never both markers).

    *should_cancel()* is polled per tile (a materialized window can take
    seconds); true → :class:`terrain_adapter.ConverterCancelled`, with the
    temp files made so far removed (the caller never receives the list).

    One job per enumerated tile (minus the already-existing WHOLE ones —
    :func:`_output_tile_whole` — unless *overwrite*, or unless the tile is in
    *rebuild* — its inputs changed since it was built, see
    ``_stack_tile_fingerprints``). The two-phase worker passes its own tile
    lists with *overwrite* True: Phase 2 the files above the first XYZ entry
    into its scratch dir, Phase 3 the whole stack. ``sources[]`` holds, in UI
    stack order (entry 0 = highest priority), each resolved entry's per-tile
    contribution:

    * ``{"kind": "files", "infos": […]}`` — bounds+halo-filtered files via
      the shared ``build_tile_sources`` (same rule as Site Analysis);
    * ``{"kind": "xyz", "pool": {res: {filename: path}}}`` — the downloaded
      pool ``.abt`` for this very tile. NO ``crs``/``nodata`` on the entry:
      .abt sources are self-describing and the converter hard-errors on
      either field (contract A1);
    * ``{"kind": "rendered", "tiles": {filename: path}}`` — the per-tile
      QGIS render (always WGS84).

    Deliberately NO ``void_fill_m``: Map Converter tiles keep VOID for
    no-data.
    """
    jobs: List[Dict[str, Any]] = []
    temp_tifs: List[str] = []
    spec = _buildings_spec(buildings_path)
    for res, tile in expected:
        _stop_if_cancelled(should_cancel, temp_tifs)
        fn = tile["filename"]
        out = os.path.join(output_dir, fn)
        if (not overwrite and _output_tile_whole(output_dir, tile)
                and fn not in (rebuild or ())):
            continue
        sub = _subtile_degrees(res)
        tile_bbox = {
            "north": tile["ul_lat"], "south": tile["ul_lat"] - sub,
            "west": tile["ul_lon"], "east": tile["ul_lon"] + sub,
        }
        sources: List[Dict[str, Any]] = []
        for idx, ent in enumerate(resolved_entries):
            kind = ent.get("kind")
            if kind == "files":
                # The same index the fingerprints used: this tile's files
                # were matched there already, the match is reused.
                s, temps = build_tile_sources(
                    ent["infos"], tile_bbox, tmp_dir,
                    tag=f"mc_{os.getpid()}_{idx}_{fn}",
                    output_res_m=tile["exact_res_m"],
                    index=_files_index(ent))
                sources.extend(s)
                temp_tifs.extend(temps)
            elif kind == "xyz":
                pool_path = ent.get("pool", {}).get(res, {}).get(fn)
                if pool_path:
                    # Self-describing .abt — adding "crs" here would be a
                    # converter hard error by contract.
                    sources.append({"path": os.path.abspath(pool_path)})
            elif kind == "rendered":
                tif = ent.get("tiles", {}).get(fn)
                if tif:
                    sources.append({"path": os.path.abspath(tif),
                                    "crs": "EPSG:4326"})
        job = {
            "output_path": os.path.abspath(out),
            "format": "r16sint",
            "ul_lat": tile["ul_lat"], "ul_lon": tile["ul_lon"],
            "resolution_m": float(tile["exact_res_m"]),
            "size_px": tile["size_px"],
            "sources": sources,
        }
        if spec is not None and spec.file:
            job["buildings_file"] = spec.file
        if (pbf_dir and spec is not None and spec.osm is not None
                and spec.osm.members(res, tile)):
            job["buildings_pbf_dir"] = os.path.abspath(pbf_dir)
            if pbf_ready:
                job.update(pbf_ready)
        jobs.append(job)
    return jobs, temp_tifs


def _xyz_download_plan(
    entries: List[Dict[str, Any]],
    expected: List[Tuple[int, Dict[str, Any]]],
) -> List[Tuple[int, Dict[str, Any], int, List[Tuple[int, Dict[str, Any]]]]]:
    """``[(entry index, entry, zoom, [(res, tile)])]`` — ONE shared-pool
    download call per XYZ entry and source ZOOM: the tiles of *expected*
    overlapping the entry's extent, grouped by the zoom the downloader
    fetches each at (``terrain_adapter._download_specs_by_zoom``: the tile's
    own centre latitude and resolution, capped at the service's zmax — its
    rule, not a copy). Resolutions that map to one zoom share the call, so
    2 m and 5 m fetch their z15 source tiles once (one job, each spec with
    its own ``resolution_m`` / ``size_px``); zooms ascending per entry.
    The worker passes the tiles it downloads (concurrently with Phase 2:
    those the files do not cover; afterwards: the holes Phase 2 left).
    The ONE enumeration the download, its progress estimate and the disk
    need use; entries that share a pool are downloaded by one call per zoom
    (:func:`_pool_calls`)."""
    plan = []
    for k, ent in enumerate(entries):
        jobs = _jobs_overlapping_extent(expected, ent.get("extent"))
        if not jobs:
            continue
        by_name = {t["filename"]: (r, t) for r, t in jobs}
        groups = terrain_adapter._download_specs_by_zoom(
            ent.get("uri", ""), [t for _r, t in jobs])
        for zoom in sorted(groups):
            plan.append((k, ent, zoom,
                         [by_name[s["filename"]] for s in groups[zoom]]))
    return plan


def _plan_pool(item: Tuple[int, Dict[str, Any], int, Any]) -> str:
    """The pool directory a :func:`_xyz_download_plan` element downloads
    into (``terrain_adapter._pool_dir`` of its entry's URI — two spellings
    of one service share it)."""
    return terrain_adapter._pool_dir(item[1].get("uri", ""))


def _pool_calls(
    plan: List[Tuple[int, Dict[str, Any], int, List[Tuple[int, Any]]]],
) -> List[Tuple[Tuple[int, Dict[str, Any], int, List[Tuple[int, Any]]],
                List[Tuple[Dict[str, Any], List[Tuple[int, Any]]]]]]:
    """The download calls *plan* makes: ONE per (pool, zoom). Entries that
    download into one pool (the same XYZ layer twice in a stack) share it —
    two jobs would write the same pool tiles at once. Each element is
    ``((k, entry, zoom, group), [(member entry, its own group), …])``: the
    first entry of that pool and zoom leads (its URI, progress slot and
    activity), *group* is the members' tiles merged and deduplicated by
    output name; each member then gets the pool paths of ITS tiles only (an
    entry's ``["pool"]`` is what puts it into a tile's ``sources[]``, so its
    own extent still decides). :func:`_download_plan_calls` and
    :func:`_estimate_download_work` both iterate this."""
    calls: List[Any] = []
    at: Dict[Tuple[str, int], int] = {}
    for k, ent, zoom, group in plan:
        key = (_plan_pool((k, ent, zoom, group)), zoom)
        if key not in at:
            at[key] = len(calls)
            calls.append((k, ent, zoom, [], set(), []))
        _k, _e, _z, merged, names, members = calls[at[key]]
        for res, tile in group:
            if tile["filename"] not in names:
                names.add(tile["filename"])
                merged.append((res, tile))
        members.append((ent, group))
    return [((k, ent, zoom, merged), members)
            for k, ent, zoom, merged, _names, members in calls]


def _plan_resolutions(group: List[Tuple[int, Dict[str, Any]]]) -> str:
    """``"2, 5 m"`` — the output resolutions one download call serves."""
    return ", ".join(str(r) for r in sorted({r for r, _t in group})) + " m"


# ---------------------------------------------------------------------------
# Latitude bands (2026-10-23): the download, and the Phase-3 batches that
# follow it, band by band
# ---------------------------------------------------------------------------

#: The lowest band height, in degrees.
_BAND_MIN_DEG = 0.25


def _band_height(resolutions: List[int]) -> float:
    """Height in degrees of the latitude bands a run downloads and converts
    in: the coarsest output tile extent of the run (``_subtile_degrees`` —
    ``ABT_EXTENT_DEG`` of the coarsest checked resolution), at least
    :data:`_BAND_MIN_DEG`. Bands lie on that grid (multiples of the
    height), so a band holds whole tiles of the coarsest resolution."""
    return max([_BAND_MIN_DEG] + [_subtile_degrees(r) for r in resolutions])


def _band_of(res: int, tile: Dict[str, Any], height: float) -> int:
    """The band of output tile *tile*: the row of its CENTRE latitude on
    the band grid (larger = further north) — a tile finer than the band
    that straddles a band edge (2 m tiles in 0.25 deg bands) belongs to the
    band holding its centre."""
    centre = tile["ul_lat"] - _subtile_degrees(res) / 2.0
    return int(math.floor(centre / height))


def _band_calls(calls: List[Any], height: float
                ) -> List[Tuple[int, List[Any]]]:
    """The download calls of :func:`_pool_calls` split into latitude
    bands: ``[(band, [call, …])]``, north to south, each band's calls
    finest zoom first (a coarse job after the fine one over the same ground
    — the toolkit derives coarse source tiles from the fine ones it has
    cached). A call keeps its lead entry (URI, progress slot, activity) in
    every band; each member keeps only its own tiles of that band."""
    by_band: Dict[int, List[Any]] = {}
    for (k, ent, zoom, group), members in calls:
        split: Dict[int, List[Tuple[int, Dict[str, Any]]]] = {}
        for res, tile in group:
            split.setdefault(_band_of(res, tile, height), []).append(
                (res, tile))
        for band, tiles in split.items():
            names = {t["filename"] for _r, t in tiles}
            own = [(m, [(r, t) for r, t in g if t["filename"] in names])
                   for m, g in members]
            by_band.setdefault(band, []).append(
                ((k, ent, zoom, tiles), [(m, g) for m, g in own if g]))
    return [(band, sorted(by_band[band], key=lambda c: -c[0][2]))
            for band in sorted(by_band, reverse=True)]


# ---------------------------------------------------------------------------
# Two-phase conversion (2026-10-21): files first, the pool only where needed
# ---------------------------------------------------------------------------

#: First aether_converter whose batch ingest prints ``[Tile] <name>
#: covered=<a>/<b>`` per written tile. Older or unknown → no Phase 2: every
#: tile overlapping an XYZ entry gets its pool tile, as before.
_TILE_LINE_MIN_VERSION = "0.2.7"

#: Phase 2 writes into ``<output_dir>/.phase1-<pid>/`` (the converter's
#: atomic temp + rename happens inside it); only complete tiles are moved
#: into the output dir, everything else is deleted with the directory.
_PHASE2_SCRATCH_PREFIX = ".phase1-"

#: Mixed into a files-only manifest fingerprint (see
#: :func:`_stack_tile_fingerprints`): a tile built from files alone without
#: its completeness proven (a stack with no XYZ entry) never matches it.
_FILES_COMPLETE_TAG = "files-complete"


def _converter_reports_tiles(converter_exe: str) -> bool:
    """Whether the converter prints per-tile ``[Tile]`` coverage lines
    (``--version`` >= :data:`_TILE_LINE_MIN_VERSION`; unknown = no)."""
    from ..core import binary_manager
    try:
        version = binary_manager.probe_converter_version(converter_exe)
        return (version is not None and binary_manager.compare_versions(
            version, _TILE_LINE_MIN_VERSION) >= 0)
    except Exception:  # noqa: BLE001 — a probe never takes the run down
        return False


def _converter_waits_for_buildings(converter_exe: Optional[str]) -> bool:
    """Whether the converter's INGEST waits for ``buildings_pbf_ready``
    (``binary_manager.converter_supports_ingest_buildings_ready``:
    aether_converter >= 0.2.12, toolkit CONTRACT item 48; unknown = no) — a
    batch is then launched before its building tiles are in."""
    from ..core import binary_manager
    try:
        return bool(converter_exe) and bool(
            binary_manager.converter_supports_ingest_buildings_ready(
                converter_exe))
    except Exception:  # noqa: BLE001 — a probe never takes the run down
        return False


def _converter_lists_buildings(converter_exe: Optional[str]) -> bool:
    """Whether the converter reads ``buildings_pbf_list``
    (``binary_manager.converter_supports_buildings_list``:
    aether_converter >= 0.2.14, toolkit CONTRACT changelog 51; unknown =
    no) — a batch launched before its building tiles is then handed a
    list of them, read straight from the store: no view is linked."""
    from ..core import binary_manager
    try:
        return bool(converter_exe) and bool(
            binary_manager.converter_supports_buildings_list(converter_exe))
    except Exception:  # noqa: BLE001 — a probe never takes the run down
        return False


def _phase2_entries(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The stack entries a Phase-2 job reads: everything ABOVE the first XYZ
    entry (the whole stack when there is none). Sources below it are
    outranked by the pool wherever the pool has data, so only a tile those
    entries cover completely is byte-identical to its full-stack build."""
    for i, ent in enumerate(entries):
        if ent.get("kind") == "xyz":
            return entries[:i]
    return list(entries)


def _prefix_file_stacks(entries: List[Dict[str, Any]]
                        ) -> List[Tuple[List[Dict[str, Any]], Any]]:
    return [(e["infos"], _files_index(e)) for e in _phase2_entries(entries)
            if e.get("kind") == "files" and e.get("infos")]


def _overlaps_xyz(entries: List[Dict[str, Any]],
                  tiles: List[Tuple[int, Dict[str, Any]]]
                  ) -> List[Tuple[int, Dict[str, Any]]]:
    """The *tiles* at least one XYZ entry of the stack would download."""
    xyz = [e for e in entries if e.get("kind") == "xyz"]
    return [rt for rt in tiles
            if any(_jobs_overlapping_extent([rt], e.get("extent"))
                   for e in xyz)]


def _partition_pending(tiles: List[Tuple[int, Dict[str, Any]]],
                       entries: List[Dict[str, Any]],
                       should_cancel: Optional[Callable[[], bool]] = None,
                       memo: Optional[Dict[Any, Any]] = None,
                       ) -> Tuple[List[Tuple[int, Dict[str, Any]]],
                                  List[Tuple[int, Dict[str, Any]]]]:
    """``(proven, download)`` of the *tiles* an XYZ entry overlaps — THE
    partition of the worker (2026-10-21) and of the up-front estimates.

    PROVEN: the RECTANGLES of the Phase-2 stack's files (the files above the
    first XYZ entry, :func:`_phase2_entries`) cover the tile plus its halo
    (``terrain_adapter.rect_cover_contributors``, geometry only) — converted
    from those files first (Phase 2), the converter's ``[Tile]`` count says
    whether they really have no hole. Every other XYZ-overlapped tile
    (no file contributor, a partial one, a second CRS, a rotated file) needs
    the pool — its download starts at once, concurrently with Phase 2.
    Tiles no XYZ entry overlaps are in neither list (built from the stack
    in Phase 3, no pool). *memo* keeps per-tile verdicts across GUI
    refreshes of one stack; *should_cancel* is polled per tile."""
    stacks = _prefix_file_stacks(entries)
    candidates = _overlaps_xyz(entries, tiles)
    if not stacks:
        return [], candidates
    proven, download = [], []
    for res, tile in candidates:
        _stop_if_cancelled(should_cancel)
        key = ("cover", tile["filename"])
        covered = memo.get(key) if memo is not None else None
        if covered is None:
            covered = terrain_adapter.rect_cover_contributors(
                stacks, _tile_bbox_of(res, tile),
                tile["exact_res_m"]) is not None
            if memo is not None:
                memo[key] = covered
        (proven if covered else download).append((res, tile))
    return proven, download


def _pool_estimate_tiles(tiles: List[Tuple[int, Dict[str, Any]]],
                         entries: List[Dict[str, Any]],
                         should_cancel: Optional[Callable[[], bool]] = None,
                         memo: Optional[Dict[Any, Any]] = None,
                         ) -> List[Tuple[int, Dict[str, Any]]]:
    """The tiles of *tiles* the pool download is started for at once — the
    ``download`` half of :func:`_partition_pending` (the holes Phase 2 then
    finds in proven tiles — no-data inside the files — add a small second
    pass). The progress estimate and the disk need up front."""
    return _partition_pending(tiles, entries, should_cancel, memo)[1]


def _tiles_to_build(
    expected: List[Tuple[int, Dict[str, Any]]],
    output_dir: str,
    overwrite: bool,
    tile_fps: Dict[str, Optional[str]],
    files_fps: Optional[Dict[str, Optional[str]]] = None,
) -> Tuple[List[Tuple[int, Dict[str, Any]]], set, int]:
    """``(tiles to build, rebuilt by fingerprint, kept without a record)`` —
    THE rule of the worker and of the pre-run disk need.

    Built: everything with *overwrite*; else every tile not in *output_dir*
    at its full length (:func:`_output_tile_whole`), and every whole tile
    whose manifest record is neither its full-stack fingerprint
    (*tile_fps*) nor its files-only one (*files_fps*, a tile the files
    completed in Phase 2). A tile without a record is kept (counted); a
    tile without a current fingerprint (rendered stack) is kept."""
    if overwrite:
        return list(expected), set(), 0
    listing = terrain_adapter._pool_listing(output_dir)
    manifest = _read_output_manifest(output_dir)
    pending: List[Tuple[int, Dict[str, Any]]] = []
    rebuild: set = set()
    unknown = 0
    for res, tile in expected:
        fn = tile["filename"]
        if not _output_tile_whole(output_dir, tile, listing):
            pending.append((res, tile))
            continue
        cur = tile_fps.get(fn)
        if cur is None:
            continue
        rec = manifest.get(fn)
        if rec is None:
            unknown += 1
        elif rec != cur and rec != (files_fps or {}).get(fn):
            rebuild.add(fn)
            pending.append((res, tile))
    return pending, rebuild, unknown


#: Labels of the per-drive disk need (the estimate label and the refusals).
_NEED_OUTPUT = "output tiles"
_NEED_POOL = "terrain pool"
_NEED_RAW = "raw tiles"
_NEED_TEMP = "temporary copies"


def _disk_items(
    build: List[Tuple[int, Dict[str, Any]]],
    entries: List[Dict[str, Any]],
    output_dir: str,
    tmp_dir: str,
    download: Optional[List[Tuple[int, Dict[str, Any]]]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    memo: Optional[Dict[Any, Any]] = None,
) -> List[Tuple[str, str, int]]:
    """``(label, directory, bytes)`` a conversion still writes: the output
    tiles of *build* (whole ``.abt`` bytes each,
    ``terrain_adapter._abt_whole_bytes``), the pool tiles of *download* not
    already in their pool (one listing per pool), the raw source tiles their
    download adds to the source-tile cache (``terrain_adapter.raw_tile_bytes``
    in ``<cache>/xyz``, an upper bound; only when something is fetched), and
    the window copies the files entries make for *build* (``materialize``
    routed files only, ``terrain_adapter.materialized_window_bytes``) in
    *tmp_dir*. Grouped per drive by ``output_estimate.drive_needs``."""
    whole = terrain_adapter._abt_whole_bytes
    items = [(_NEED_OUTPUT, output_dir,
              sum(whole(int(t["size_px"])) for _r, t in build))]
    xyz = [e for e in entries if e.get("kind") == "xyz"]
    pool_need: Dict[str, int] = {}
    listings: Dict[str, Dict[str, Any]] = {}
    # Per pool (= per service): the tiles its download fetches source tiles
    # for, each once — the raw cache is keyed by service and zoom.
    fetched: Dict[str, Tuple[str, List[Dict[str, Any]]]] = {}
    seen: set = set()
    for _k, ent, _zoom, group in _xyz_download_plan(xyz, download or []):
        pool = terrain_adapter._pool_dir(ent.get("uri", ""))
        if pool not in listings:
            listings[pool] = terrain_adapter._pool_listing(pool)
            pool_need.setdefault(pool, 0)
        for _r, t in group:
            _stop_if_cancelled(should_cancel)
            if (pool, t["filename"]) in seen:
                continue
            seen.add((pool, t["filename"]))
            if not terrain_adapter._listed_tile_ready(listings[pool], t, pool):
                pool_need[pool] += whole(int(t["size_px"]))
                fetched.setdefault(pool, (ent.get("uri", ""), []))[1].append(t)
    items.extend((_NEED_POOL, pool, n) for pool, n in pool_need.items())
    items.extend(_raw_tile_items(fetched))
    temp = 0
    for pos, ent in enumerate(entries):
        if ent.get("kind") != "files" or not any(
                i.get("materialize") for i in ent.get("infos") or ()):
            continue
        for res, tile in build:
            _stop_if_cancelled(should_cancel)
            key = ("temp", pos, tile["filename"])
            n = memo.get(key) if memo is not None else None
            if n is None:
                n = terrain_adapter.materialized_window_bytes(
                    ent["infos"], _tile_bbox_of(res, tile),
                    tile["exact_res_m"], index=_files_index(ent))
                if memo is not None:
                    memo[key] = n
            temp += n
    items.append((_NEED_TEMP, tmp_dir, temp))
    return items


def _raw_tile_items(fetched: Dict[str, Tuple[str, List[Dict[str, Any]]]]
                    ) -> List[Tuple[str, str, int]]:
    """The "raw tiles" item of :func:`_disk_items`: what the downloads of
    *fetched* (``{pool: (uri, tiles to fetch)}``) add to the source-tile
    cache ``<cache>/xyz`` (``terrain_adapter.raw_tile_bytes``); none when
    nothing is fetched."""
    raw = sum(terrain_adapter.raw_tile_bytes(uri, tiles)
              for uri, tiles in fetched.values())
    return [(_NEED_RAW, terrain_adapter.xyz_cache_dir(), raw)] if raw else []


def _disk_refusal(items: List[Tuple[str, str, int]], what: str
                  ) -> Optional[str]:
    """The refusal text when *items* do not fit (need > free − 2 GB on a
    drive), else None."""
    short = output_estimate.short_drives(output_estimate.drive_needs(items))
    return output_estimate.refusal_text(short, what) if short else None


def _download_key(k: int, zoom: int, stage: int = 1) -> Tuple[Any, ...]:
    """Progress slot of XYZ entry *k*'s download call at *zoom*:
    ``("download", k, zoom)`` for the download that runs next to Phase 2
    (its bands report into it one after the other, :class:`_SlotLedger`),
    ``("download", k, zoom, 2)`` for the second pass over the holes Phase 2
    left (a finished slot takes no new work)."""
    return ("download", k, zoom) if stage == 1 else ("download", k, zoom,
                                                     stage)


#: Progress slot of Phase 2 (tiles converted from the local files first).
#: Also the model's activity key of Phase 2 while the downloads run next
#: to it, and its batch id (job file ``aether_conv_<pid>_files.json``).
_CONVERT_FILES = ("convert", "files")

#: The Phase-3 work no batch holds yet: sized with every Phase-3 tile up
#: front; a batch moves its tiles' work into its own slot
#: ``("convert", <batch id>)`` when it forms, so the run's total never
#: changes and the bar keeps its monotonic rule.
_CONVERT_PENDING = ("convert",)


def _download_activity(k: int, derive: bool = False) -> Tuple[Any, ...]:
    """The model's activity key of XYZ entry *k*'s concurrent download:
    ``("download", k)`` for its fetch lane (each band's finest zoom — the
    only lane with :data:`SERIAL_ZOOMS_ENV`), ``("download", k, "derive")``
    for its derive lane (the coarser zooms, :class:`_DownloadBranch`) — two
    activities, so the label names both running passes."""
    return ("download", k, "derive") if derive else ("download", k)


#: The A/B switch of the two download lanes per pool (2026-10-25):
#: ``WAVESHED_SERIAL_ZOOMS=1`` runs every call of a band one after the
#: other on one lane, as before (:class:`_DownloadBranch`). Read once per
#: run (:func:`_serial_zooms_requested`), the mode logged once.
SERIAL_ZOOMS_ENV = "WAVESHED_SERIAL_ZOOMS"


def _serial_zooms_requested() -> bool:
    """Whether :data:`SERIAL_ZOOMS_ENV` asks for the one-lane download
    (``1``, also ``true`` / ``yes`` / ``on``, any case)."""
    return os.environ.get(SERIAL_ZOOMS_ENV, "").strip().lower() in (
        "1", "true", "yes", "on")


def _lanes_line(serial: bool) -> str:
    """The run's one log line naming its download mode."""
    if serial:
        return (f"  download lanes: one per pool — every zoom of a band in "
                f"sequence ({SERIAL_ZOOMS_ENV}=1)")
    return (f"  download lanes: two per pool — each band's finest zoom "
            f"(fetch lane) runs beside the coarser zooms of the band before "
            f"last (derive lane: a band's coarser passes wait for the next "
            f"band's finest, so the source tiles on a band edge derive from "
            f"both sides in every run); {SERIAL_ZOOMS_ENV}=1 runs every "
            f"zoom in sequence")


def _reset_download_work(model: Any) -> None:
    """Zero every ``("download", …)`` slot before it is sized again (an
    earlier estimate may name calls the new tile list no longer makes)."""
    for key in model.keys():
        if isinstance(key, tuple) and key[:1] == ("download",):
            model.set_work(key, 0)


def _estimate_download_work(model: Any, entries: List[Dict[str, Any]],
                            expected: List[Tuple[int, Dict[str, Any]]],
                            pool_check: bool,
                            should_cancel: Optional[Callable[[], bool]] = None,
                            stage: int = 1,
                            height: Optional[float] = None,
                            ledger: Optional["_SlotLedger"] = None) -> None:
    """Register every download call's source-tile count on *model* — one
    slot per call of :func:`_pool_calls` (an (entry, zoom) of
    :func:`_xyz_download_plan`; entries sharing a pool count once, under the
    leading entry's slot).

    *pool_check* False (GUI thread, before the run): pure geometry, every
    tile counted. True (worker): tiles already in the pool cost nothing —
    the same count the downloader's own progress then reports.
    *should_cancel* is polled per pool-checked tile (raises
    :class:`terrain_adapter.ConverterCancelled`).

    *height* + *ledger* (the worker's banded download): each call is
    estimated per latitude band (:func:`_band_calls` — one job per band,
    each fetching its own source tiles), the estimates go to *ledger* (the
    calls of one slot then report one after the other into it) and the
    slot is sized as their sum; one pool listing per pool.
    """
    calls = _pool_calls(_xyz_download_plan(entries, expected))
    if height is None or ledger is None:
        for (k, ent, zoom, group), _members in calls:
            model.set_work(_download_key(k, zoom, stage),
                           terrain_adapter.estimate_pool_download_units(
                               ent["uri"], [t for _r, t in group],
                               pool_check=pool_check,
                               should_cancel=should_cancel))
        return
    listings: Dict[str, Dict[str, Any]] = {}
    for band, band_calls in _band_calls(calls, height):
        for (k, ent, zoom, group), _members in band_calls:
            specs = [t for _r, t in group]
            if pool_check:
                pool = terrain_adapter._pool_dir(ent.get("uri", ""))
                if pool not in listings:
                    listings[pool] = terrain_adapter._pool_listing(pool)
                todo = []
                for t in specs:
                    _stop_if_cancelled(should_cancel)
                    if not terrain_adapter._listed_tile_ready(
                            listings[pool], t, pool):
                        todo.append(t)
                specs = todo
            units = (terrain_adapter.estimate_pool_download_units(
                ent["uri"], specs, pool_check=False) if specs else 0.0)
            ledger.add(_download_key(k, zoom, stage), band, units)
    for (k, _ent, zoom, _group), _members in calls:
        key = _download_key(k, zoom, stage)
        model.set_work(key, ledger.total(key))


def _convert_work(tiles: List[Dict[str, Any]]) -> float:
    """Convert-phase units of output tiles (``size_px`` each)."""
    return sum(run_progress.tile_blocks(t["size_px"]) for t in tiles) \
        * run_progress.CONVERT_UNITS_PER_BLOCK


def _render_tile_work(tile: Dict[str, Any]) -> float:
    return (run_progress.tile_blocks(tile["size_px"])
            * run_progress.RENDER_UNITS_PER_BLOCK)


class _LedgerCall:
    """One running call of a banded slot (:meth:`_SlotLedger.begin`): its
    own estimate and what it reported last — its total, its done count
    (never back) and the done count its next report is certain to reach
    (None: not known)."""

    __slots__ = ("ledger", "key", "est", "total", "done", "ceiling")

    def __init__(self, ledger: "_SlotLedger", key: Any, est: float) -> None:
        self.ledger = ledger
        self.key = key
        self.est = est
        self.total = est
        self.done = 0.0
        self.ceiling: Optional[float] = None


class _SlotLedger:
    """The download slots of a banded download (2026-10-23): slot
    ``("download", k, zoom)`` is served by one call per latitude band. The
    slot's total is what its ended calls accounted for (*base*) + the
    estimates of the calls still to come (*rest*) + what its RUNNING calls
    report; its done count is *base* + the running calls' done counts — so
    its total stays the sum of its calls, its done count only grows, and it
    is complete after its last band: the bar keeps the monotonic
    remaining-work rule. Since 2026-10-25 a pool downloads on two lanes
    (:class:`_DownloadBranch`), so two calls of ONE slot can run at once
    (a zoom that is one band's finest and the band before's coarser): each
    running call is kept apart (:class:`_LedgerCall`), neither's progress
    is lost or counted twice. Thread-safe; the model is updated under the
    ledger's lock, so two lanes never apply stale sums."""

    def __init__(self, model: Any) -> None:
        self._model = model
        self._lock = threading.Lock()
        self._base: Dict[Any, float] = {}
        self._rest: Dict[Any, float] = {}
        self._est: Dict[Tuple[Any, Any], float] = {}
        self._live: Dict[Any, List[_LedgerCall]] = {}

    def add(self, key: Any, band: Any, units: float) -> None:
        """Register the estimate of *key*'s call for *band*."""
        with self._lock:
            self._est[(key, band)] = self._est.get((key, band), 0.0) + units
            self._rest[key] = self._rest.get(key, 0.0) + units
            self._base.setdefault(key, 0.0)

    def total(self, key: Any) -> float:
        with self._lock:
            return self._work_locked(key)

    def _work_locked(self, key: Any) -> float:
        return (self._base.get(key, 0.0) + self._rest.get(key, 0.0)
                + sum(c.total for c in self._live.get(key, ())))

    def _done_locked(self, key: Any) -> float:
        return self._base.get(key, 0.0) + sum(c.done for c in
                                              self._live.get(key, ()))

    def _ceiling_locked(self, key: Any) -> Optional[float]:
        live = self._live.get(key, ())
        if all(c.ceiling is None for c in live):
            return None
        return self._base.get(key, 0.0) + sum(
            c.done if c.ceiling is None else c.ceiling for c in live)

    def begin(self, key: Any, band: Any) -> _LedgerCall:
        """*key*'s call for *band* starts: its estimate leaves *rest* and
        the call reports on its own (:meth:`report`) until :meth:`end`."""
        with self._lock:
            est = self._est.pop((key, band), 0.0)
            self._rest[key] = max(0.0, self._rest.get(key, 0.0) - est)
            self._base.setdefault(key, 0.0)
            call = _LedgerCall(self, key, est)
            self._live.setdefault(key, []).append(call)
            return call

    def report(self, call: _LedgerCall, done: float,
               total: Optional[float] = None,
               ceiling: Optional[float] = None) -> None:
        """*call*'s counts (absolute, of the call alone): *done*, and — when
        the downloader said so — its *total* and *ceiling*; the slot shows
        the sum of every call of it."""
        with self._lock:
            if call not in self._live.get(call.key, ()):
                return                          # ended: accounted for
            if total is not None:
                call.total = max(0.0, float(total))
            call.done = max(call.done, float(done))
            call.ceiling = None if ceiling is None else float(ceiling)
            if self._model is not None:
                if total is not None:
                    self._model.set_work(call.key, self._work_locked(call.key))
                self._model.set_done(call.key, self._done_locked(call.key),
                                     ceiling=self._ceiling_locked(call.key))

    def end(self, call: _LedgerCall, units: float) -> None:
        """*call* is over, having accounted for *units*."""
        with self._lock:
            live = self._live.get(call.key, [])
            if call in live:
                live.remove(call)
            self._base[call.key] = (self._base.get(call.key, 0.0)
                                    + max(0.0, units))
            if self._model is not None:
                self._model.set_done(call.key, self._done_locked(call.key),
                                     ceiling=self._ceiling_locked(call.key))
                self._model.set_work(call.key, self._work_locked(call.key))


class _DownloadProgressSink:
    """``progress_cb`` for one ``ensure_pool_tiles`` call of a run.

    Callable as the plain ``(frac, label)`` callback, plus the
    ``download_event(info)`` hook ``_try_rust_download`` feeds the source
    tile counts into: they drive the run's :class:`run_progress.RunProgress`
    slot for this call, so the whole-run bar counts real tiles, retry passes
    included. *what* names the output resolutions the call serves
    (``"2, 5 m"``; an int is one resolution). With *activity* the counters
    go to that concurrent activity of the model, not to its phase — the
    download then runs next to Phase 2 and the label names both. *tag*
    replaces the ``[i/n]`` call note (a band: ``" band 2/3"``). *base* /
    *rest* / *est*: counts reported on top of *base* and below *rest*;
    *est* is the call's own estimate (what a call without events accounts
    for). *call*: a call of a banded slot (:meth:`_SlotLedger.begin`) — its
    counts go to the ledger, which shows the slot as the sum of every
    running call of it (two download lanes may serve one slot at once);
    *est* defaults to the call's estimate.
    """

    def __init__(self, model: Any, key: Any, what: Any, call_no: int,
                 n_calls: int, outer: Any = None,
                 activity: Any = None, tag: Optional[str] = None,
                 base: float = 0.0, rest: float = 0.0,
                 est: Optional[float] = None,
                 call: Optional[_LedgerCall] = None) -> None:
        self._model = model
        self._key = key
        self._what = f"{what} m" if isinstance(what, int) else str(what)
        self._tag = (tag if tag is not None else
                     f" [{call_no}/{n_calls}]" if n_calls > 1 else "")
        self._outer = outer
        self._activity = activity
        self._events = False
        self._base = float(base)
        self._rest = float(rest)
        self._call = call
        self._est = est if est is not None or call is None else call.est
        self._last = 0.0

    def units(self) -> float:
        """The units this call accounted for (its last reported total, or
        its estimate when the downloader sent no events)."""
        if self._events:
            return self._last
        return float(self._est or 0.0)

    def __call__(self, frac: float, label: str) -> None:
        if self._model is not None and not self._events:
            # A downloader that sends no events: the fraction still moves.
            if self._call is not None:
                self._call.ledger.report(
                    self._call, frac * max(0.0, float(self._est or 0.0)))
            else:
                call = (self._est if self._est is not None
                        else self._model.work(self._key) - self._base
                        - self._rest)
                self._model.set_done(self._key,
                                     self._base + frac * max(0.0, call))
        if self._outer is not None:
            self._outer(frac, label)

    def download_event(self, info: Dict[str, Any]) -> None:
        self._events = True
        self._last = max(self._last, float(info["total_units"]),
                         float(info["done_units"]))
        m = self._model
        if m is None:
            return
        ceiling = info.get("ceiling_units")
        if self._call is not None:
            self._call.ledger.report(self._call, info["done_units"],
                                     total=info["total_units"],
                                     ceiling=ceiling)
        else:
            base = self._base
            m.set_work(self._key, base + info["total_units"] + self._rest)
            m.set_done(self._key, base + info["done_units"],
                       ceiling=None if ceiling is None else base + ceiling)
        phase = (f"Downloading terrain z{info.get('zoom', '?')} "
                 f"({self._what}){self._tag}")
        detail = {
            "pass": info.get("pass"), "passes": info.get("max_passes"),
            "done": info.get("pass_done"), "total": info.get("pass_total"),
            "unit": "source tiles", "mbps": info.get("mbps"),
            "errors": info.get("errors"),
            "repair": True if info.get("repair") else None,
        }
        reset = info.get("event") == "pass_start"
        if self._activity is not None:
            m.set_activity(self._activity, phase, detail, reset=reset)
        else:
            m.set_phase(phase, detail, reset=reset)


def _download_xyz_entries(
    entries: List[Dict[str, Any]],
    expected: List[Tuple[int, Dict[str, Any]]],
    progress_cb: Any = None,
    should_cancel: Any = None,
    on_start: Any = None,
    progress_model: Any = None,
    stage: int = 1,
) -> None:
    """Fill each XYZ entry's ``["pool"]`` (``{res: {name: path}}``, merged
    into what it holds) via the shared pool downloader, one pool and zoom
    after the other (:func:`_download_plan_calls`).

    Per entry, per ZOOM (:func:`_xyz_download_plan`; entries sharing a pool
    merged, :func:`_pool_calls`): the enumerated tiles overlapping the
    entry's extent go through
    ``terrain_adapter.ensure_pool_tiles`` in ONE call — one converter job
    whose specs carry their own resolution (same pool cache and gap-repair
    retries as Site Analysis). *progress_cb(frac, label)* receives the
    aggregate download fraction across ALL calls; *progress_model* (a
    ``RunProgress``) gets the real source tile counts per call (slot
    :func:`_download_key`, see :func:`_estimate_download_work`). Failures
    propagate (RuntimeError) — no render fallback.
    """
    _download_plan_calls(_xyz_download_plan(entries, expected), progress_cb,
                         should_cancel, on_start, progress_model, stage)


def _download_plan_calls(
    plan: List[Tuple[int, Dict[str, Any], int, List[Tuple[int, Any]]]],
    progress_cb: Any = None,
    should_cancel: Any = None,
    on_start: Any = None,
    progress_model: Any = None,
    stage: int = 1,
    activity: Any = None,
) -> None:
    """Run the download calls of *plan* (elements of
    :func:`_xyz_download_plan`) in order — one per pool and zoom
    (:func:`_pool_calls`: entries sharing a pool are served by one job);
    *activity*: the model activity their counters go to (the worker's
    concurrent download of one pool)."""
    for _k, ent, _z, _g in plan:
        ent.setdefault("pool", {})
    _run_pool_calls(_pool_calls(plan), progress_cb, should_cancel, on_start,
                    progress_model, stage, activity)


def _run_pool_calls(
    calls: List[Any],
    progress_cb: Any = None,
    should_cancel: Any = None,
    on_start: Any = None,
    progress_model: Any = None,
    stage: int = 1,
    activity: Any = None,
    tag: Optional[str] = None,
    ledger: Optional[_SlotLedger] = None,
    band: Any = None,
    on_pool_tiles: Optional[Callable[[str, List[str]], None]] = None,
    on_call_end: Optional[Callable[[Any], None]] = None,
    lane_tag: bool = False,
) -> None:
    """Run download *calls* (elements of :func:`_pool_calls`, or one band's
    of :func:`_band_calls`) one after the other through the shared
    downloader, filling each member entry's ``["pool"]`` with ITS tiles.
    *tag* notes the calls in the label (a band); with *ledger* the calls
    are *band*'s of their banded slots (:class:`_SlotLedger`), else each
    call finishes its slot. *on_pool_tiles(pool dir, names)* hears which
    pool tiles each call downloaded (``PoolTiles.fetched``; the tiles that
    were in the pool already are not named). *on_call_end(call)* hears of
    each call once its tiles are in place (the per-band timing line).
    *lane_tag* (two lanes per pool: another pass logs into the same panel
    at the same time): each call's panel lines are prefixed with its zoom,
    ``[z14] ``, through ``terrain_adapter.set_panel_tag`` for this thread
    (the lane's) and for the call only."""
    total = sum(len(group) for (_k, _e, _z, group), _m in calls) or 1
    done = 0
    for call_no, ((k, ent, zoom, group), members) in enumerate(calls,
                                                               start=1):
        def cb(frac: float, label: str, _done=done, _n=len(group)) -> None:
            if progress_cb is not None:
                progress_cb((_done + frac * _n) / total, label)

        key = _download_key(k, zoom, stage)
        call = ledger.begin(key, band) if ledger is not None else None
        sink = _DownloadProgressSink(progress_model, key,
                                     _plan_resolutions(group), call_no,
                                     len(calls), cb, activity=activity,
                                     tag=tag, call=call)
        if lane_tag:
            terrain_adapter.set_panel_tag(f"[z{zoom}] ")
        try:
            mapping = terrain_adapter.ensure_pool_tiles(
                ent["uri"], [t for _r, t in group],
                min(r for r, _t in group), progress_cb=sink,
                should_cancel=should_cancel, on_start=on_start)
        finally:
            if lane_tag:
                terrain_adapter.set_panel_tag(None)
        fetched = list(getattr(mapping, "fetched", ()) or ())
        if on_pool_tiles is not None and fetched:
            on_pool_tiles(getattr(mapping, "pool_dir", "")
                          or terrain_adapter._pool_dir(ent.get("uri", "")),
                          fetched)
        for member, own in members:
            pool = member.setdefault("pool", {})
            for res, tile in own:
                path = mapping.get(tile["filename"])
                if path:
                    pool.setdefault(res, {})[tile["filename"]] = path
        if call is not None:
            ledger.end(call, sink.units())
        elif progress_model is not None:
            # A pool hit sends nothing; either way this call is over.
            progress_model.finish(key)
        done += len(group)
        if progress_cb is not None:
            progress_cb(done / total,
                        f"Terrain download: {done}/{total} tiles ready")
        if on_call_end is not None:
            on_call_end(((k, ent, zoom, group), members))


# ---------------------------------------------------------------------------
# Layer data model
# ---------------------------------------------------------------------------

class _LayerEntry:
    """One layer in the converter stack.

    The run's output resolutions are the tab's "Generate tiles at"
    checkboxes, for every row alike; the per-row "Target Res" column was
    never read by a run and is gone (2026-10-21). *target_resolutions* is
    still accepted — and ignored — so a caller or saved stack that carries
    it loads silently."""
    __slots__ = (
        "layer_type", "source_path", "qgis_layer", "crs_authid",
        "native_res_m", "extent", "priority",
        "crs_overrides", "decisions", "overlap_priority",
    )

    def __init__(
        self,
        layer_type: str = "raster",
        source_path: str = "",
        qgis_layer: Any = None,
        crs_authid: str = "EPSG:4326",
        native_res_m: Optional[float] = None,
        target_resolutions: Optional[List[int]] = None,   # ignored (legacy)
        extent: Optional[Dict[str, float]] = None,
        priority: int = 1,
    ) -> None:
        del target_resolutions
        self.layer_type = layer_type
        self.source_path = source_path
        self.qgis_layer = qgis_layer
        self.crs_authid = crs_authid
        self.native_res_m = native_res_m
        self.extent = extent
        self.priority = priority
        # Folder entries only. crs_overrides: {file: CRS the user assigned}
        # (CRS-less files never inherit crs_authid, which is display only for
        # a folder); decisions: {file: "skip"|"use"} from the pre-flight
        # prompt; overlap_priority: "finest" | "name" (None = the folder's
        # stored setting).
        self.crs_overrides: Dict[str, str] = {}
        self.decisions: Dict[str, str] = {}
        self.overlap_priority: Optional[str] = None


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Acquisition router + main-thread resolve/render phase
# ---------------------------------------------------------------------------

def _estimate_run_work(model: Any, layers: List[_LayerEntry],
                       render_jobs: List[Tuple[int, Dict[str, Any]]]) -> None:
    """Register the whole run's estimated work on *model* before it starts.

    One bar for the run, weighted by work (``core/run_progress.py``):
    rendered servers per tile (``("render", i)``, only entries that render),
    XYZ downloads in source tiles (``("download", k, zoom)`` per pool and
    zoom, geometry only — no pool stat on the GUI thread; the worker
    re-sizes them from its partition), and the convert phase per output
    tile (``("convert",)``), and the OpenFreeMap building tiles of the
    Download Buildings… rows (:data:`_BUILDINGS_SLOT`, one unit per z14
    tile of their extents; the worker re-sizes it to the tiles it fetches).
    Phases with nothing to do get no weight. *render_jobs* is the run's
    pending ``(res, tile)`` list.
    """
    xyz: List[Dict[str, Any]] = []
    osm_tiles = sum(openfreemap.estimate_tile_count(e.extent)
                    for e in layers if _is_osm_row(e) and e.extent)
    if osm_tiles:
        model.set_work(_BUILDINGS_SLOT, osm_tiles
                       * run_progress.DOWNLOAD_UNITS_PER_SOURCE_TILE)
    for i, e in enumerate(layers):
        kind = _entry_kind(e)
        if kind == "rendered":
            model.set_work(("render", i), sum(
                _render_tile_work(t)
                for _r, t in _jobs_overlapping_extent(render_jobs, e.extent)))
        elif kind == "xyz":
            src = (e.qgis_layer.source() if e.qgis_layer is not None
                   else e.source_path)
            xyz.append({"uri": src, "extent": e.extent})
    _estimate_download_work(model, xyz, render_jobs, pool_check=False)
    model.set_work(("convert",), _convert_work([t for _r, t in render_jobs]))


def _estimate_stack(layers: List[_LayerEntry], cache: Dict[Any, Any]
                    ) -> Tuple[List[Dict[str, Any]], bool, bool]:
    """``(entries, exact, buildings)`` — the raster stack resolved as the
    worker's ``_resolve_raster`` will, from what the GUI thread already
    holds: a folder's infos from its IN-MEMORY index (scanned off-thread by
    Add Folder / the pre-flight; never refreshed here), a single file's from
    one probe (cached by path, size and mtime), XYZ and rendered entries as
    such. *exact* is False when a files entry could not be read that way
    (its tiles then have no files in the estimate); *buildings* when the
    stack has a buildings FILE entry (its converted file is not known
    before the run, so no fingerprint can be compared — OpenFreeMap rows
    are: their fingerprint part is known up front). *cache* is the tab's: the
    infos and their FootprintIndex survive refreshes of an unchanged entry
    (and only those — the rest is dropped)."""
    entries: List[Dict[str, Any]] = []
    exact, buildings = True, False
    used: set = set()
    for e in layers:
        kind = _entry_kind(e)
        if kind == "buildings":
            buildings = buildings or not _is_osm_row(e)
            continue
        if kind == "xyz":
            src = (e.qgis_layer.source() if e.qgis_layer is not None
                   else e.source_path)
            entries.append({"kind": "xyz", "uri": src, "extent": e.extent})
            continue
        if kind == "rendered":
            entries.append({"kind": "rendered", "tiles": {}})
            continue
        try:
            key = _estimate_infos(e, cache)
        except Exception:  # noqa: BLE001 — an estimate never blocks a run
            key = None
        if key is None:
            exact = False
            continue
        used.add(key)
        infos, index = cache[key]
        entries.append({"kind": "files", "infos": infos, "index": index,
                        "est_key": key})
    for key in [k for k in cache if isinstance(k, tuple)
                and k[:1] in (("folder",), ("file",)) and k not in used]:
        del cache[key]
    return entries, exact, buildings


def _estimate_infos(e: _LayerEntry, cache: Dict[Any, Any]) -> Optional[Any]:
    """Cache key of one files entry's ``(infos, FootprintIndex)`` for
    :func:`_estimate_stack`, computed on first use (None: unknown). A
    folder's in-memory index is read under its lock: the estimate runs in
    its own thread (:class:`_EstimateThread`), and a refresh — a folder
    scan, a Site Analysis run — changes the entries in place under it."""
    extent = tuple(sorted((e.extent or {}).items()))
    if os.path.isdir(e.source_path or ""):
        index = terrain_index.get_index(e.source_path)
        mode = (e.overlap_priority
                or terrain_index.get_overlap_priority(e.source_path))
        with index.lock:
            if not index.entries:
                return None
            key = ("folder", e.source_path, mode,
                   tuple(sorted((e.decisions or {}).items())),
                   tuple(sorted((e.crs_overrides or {}).items())), extent,
                   id(index.entries), len(index.entries), index.refreshed_at)
            if key not in cache:
                infos, _skipped = terrain_index.source_infos(
                    e.source_path, mode, e.decisions or {},
                    e.crs_overrides or {}, index)
                cache[key] = _filtered_infos(infos, e.extent)
    else:
        st = os.stat(e.source_path)
        key = ("file", e.source_path, e.crs_authid or "", st.st_size,
               st.st_mtime_ns, extent)
        if key not in cache:
            cache[key] = _filtered_infos([terrain_adapter.file_layer_info(
                e.source_path, crs_authid=e.crs_authid or None)], e.extent)
    return key


def _filtered_infos(infos: List[Dict[str, Any]],
                    extent: Optional[Dict[str, float]]) -> Tuple[Any, Any]:
    """``(infos the entry's extent admits, their FootprintIndex)`` — the
    worker's ``_resolve_raster`` filter."""
    if extent:
        infos = [i for i in infos
                 if bboxes_intersect(i["wgs84_bounds"],
                                     _expand_bbox(extent, i["halo_deg"]))]
    return infos, terrain_adapter.footprint_index(infos)


def _estimate_disk_need(layers: List[_LayerEntry], out_res: List[int],
                        output_dir: str, overwrite: bool,
                        tmp_dir: Optional[str] = None,
                        cache: Optional[Dict[str, Any]] = None,
                        should_cancel: Optional[Callable[[], bool]] = None,
                        ) -> Optional[Tuple[int, List[Any]]]:
    """``(output tiles to build, per-drive needs)`` of a run, before it
    starts (the label's :class:`_EstimateThread`, on plain copies of the
    rows — :func:`_plain_entry` — and :meth:`MapConverterTab._on_run` on
    the GUI thread; both with the tab's *cache*, under its lock).

    The worker's rules: the tiles to build (none outside every terrain
    row's extent — :func:`_tiles_within_sources`; new, short, and rebuilt by
    fingerprint — :func:`_tiles_to_build`; tiles with a record are kept when
    the stack cannot be fingerprinted here: buildings, an unread folder),
    the pool tiles not in the pool yet for the tiles the files above the
    first XYZ entry do not cover by rectangles (:func:`_pool_estimate_tiles`
    — the worker's partition: what it downloads next to Phase 2; holes
    Phase 2 finds add a small second pass), the raw source tiles that
    download adds to ``<cache>/xyz`` ("raw tiles", an upper bound —
    :func:`_disk_items`), and the window copies; whole ``.abt`` bytes each,
    per drive of output dir, pool, source-tile cache and %TEMP%
    (``output_estimate.drive_needs``). None when no raster entry has an
    extent. *should_cancel()* is polled per tile (fingerprints, partition,
    window copies): true → ``terrain_adapter.ConverterCancelled`` (a newer
    request has made the label's estimate stale).
    """
    if cache is None:
        cache = {}
    combined = _union_extent(layers)
    if combined is None or not out_res:
        return None
    expected, _outside = _tiles_within_sources(
        _enumerate_tiles(_snap_bbox(combined, out_res), out_res), layers)
    entries, exact, buildings = _estimate_stack(layers, cache)
    # Per-tile verdicts (rectangles, window copies) belong to ONE stack.
    sig = tuple((e.get("kind"), e.get("est_key"), e.get("uri"),
                 repr(e.get("extent"))) for e in entries)
    if cache.get("memo_sig") != sig:
        cache["memo_sig"], cache["memo"] = sig, {}
    memo = cache["memo"]
    tile_fps: Dict[str, Optional[str]] = {}
    files_fps: Dict[str, Optional[str]] = {}
    if not overwrite and exact and not buildings:
        manifest = _read_output_manifest(output_dir)
        listing = terrain_adapter._pool_listing(output_dir)
        recorded = [(r, t) for r, t in expected
                    if t["filename"] in manifest
                    and _output_tile_whole(output_dir, t, listing)]
        if recorded:
            osm_extents = [e.extent for e in layers
                           if _is_osm_row(e) and e.extent]
            osm_key = ("osm", repr(osm_extents))
            for key in [k for k in cache if isinstance(k, tuple)
                        and k[:1] == ("osm",) and k != osm_key]:
                del cache[key]
            if osm_extents and osm_key not in cache:
                cache[osm_key] = _OsmBuildings(osm_extents)
            spec = (_RunBuildings(osm=cache[osm_key]) if osm_extents
                    else None)
            tile_fps = _stack_tile_fingerprints(recorded, entries, spec,
                                                should_cancel=should_cancel,
                                                files_only=files_fps)
    build, _rebuild, _unknown = _tiles_to_build(
        expected, output_dir, overwrite, tile_fps, files_fps)
    download = _pool_estimate_tiles(build, entries,
                                    should_cancel=should_cancel, memo=memo)
    items = _disk_items(build, entries, output_dir,
                        tmp_dir or tempfile.gettempdir(), download=download,
                        should_cancel=should_cancel, memo=memo)
    return len(build), output_estimate.drive_needs(items)


# ---------------------------------------------------------------------------
# The estimate label, off the GUI thread (owner, 2026-11-07: "the checkboxes
# lag until the size estimate is done" — every toggle ran the tile count and
# the per-drive need on the GUI thread)
# ---------------------------------------------------------------------------

#: Debounce of the label (ms): the requests within it — toggles, rows added,
#: extents set — make ONE estimate.
_ESTIMATE_DEBOUNCE_MS = 150

#: The label from a request until its estimate answers.
_ESTIMATING_TEXT = "Estimating tiles and disk space…"


class _PlainLayer:
    """What the estimate reads of a row's QGIS layer — ``source()`` — read
    on the GUI thread when the request is taken (a QgsRasterLayer is never
    touched by the estimate thread). A layer that could not be read (deleted)
    raises the same error where the estimate reads it: the disk need's
    warning path, as it was on the GUI thread."""
    __slots__ = ("_source", "_error")

    def __init__(self, layer: Any) -> None:
        self._source = ""
        self._error: Optional[BaseException] = None
        try:
            self._source = str(layer.source())
        except Exception as exc:  # noqa: BLE001 — raised again where read
            self._error = exc

    def source(self) -> str:
        if self._error is not None:
            raise self._error
        return self._source


def _plain_entry(e: _LayerEntry) -> _LayerEntry:
    """A copy of stack row *e* the estimate thread may read: plain values
    only — the QGIS layer as its source string (:class:`_PlainLayer`), the
    extent, file decisions and CRS answers copied, a raster row's overlap
    rule resolved here (``terrain_index.get_overlap_priority`` reads
    QgsSettings). :func:`_estimate_tile_count_and_mb` and
    :func:`_estimate_disk_need` read it exactly as the row (same cache keys
    in ``_est_cache``)."""
    c = _LayerEntry(
        layer_type=e.layer_type, source_path=e.source_path,
        qgis_layer=(_PlainLayer(e.qgis_layer) if e.qgis_layer is not None
                    else None),
        crs_authid=e.crs_authid, native_res_m=e.native_res_m,
        extent=dict(e.extent) if e.extent else e.extent,
        priority=e.priority)
    c.crs_overrides = dict(e.crs_overrides or {})
    c.decisions = dict(e.decisions or {})
    c.overlap_priority = e.overlap_priority
    if (c.overlap_priority is None and e.layer_type != "buildings"
            and e.qgis_layer is None and e.source_path):
        c.overlap_priority = terrain_index.get_overlap_priority(
            e.source_path)
    return c


class _EstimateJob:
    """One request of the estimate label, as plain values read on the GUI
    thread (:meth:`MapConverterTab._estimate_job`): the stack as
    :func:`_plain_entry` copies, their combined bbox, the checked
    resolutions, the output directory, "Rebuild existing tiles", "Keep pool
    tiles", the tab's ``_est_cache`` and the lock it is touched under. Its
    result is shown only while *generation* is the tab's latest request;
    *superseded* is set by a newer one (the estimate stops at its next
    per-tile poll)."""
    __slots__ = ("generation", "layers", "combined", "out_res", "output_dir",
                 "overwrite", "keep_pool", "cache", "lock", "superseded")

    def __init__(self, generation: int, layers: List[_LayerEntry],
                 out_res: List[int], output_dir: str, overwrite: bool,
                 keep_pool: bool, cache: Dict[Any, Any], lock: Any) -> None:
        self.generation = generation
        self.layers = layers
        self.combined = _union_extent(layers)
        self.out_res = list(out_res)
        self.output_dir = output_dir
        self.overwrite = bool(overwrite)
        self.keep_pool = bool(keep_pool)
        self.cache = cache
        self.lock = lock
        self.superseded = threading.Event()


class _EstimateResult:
    """What :func:`_run_estimate` found for :attr:`job`: ``tiles`` / ``mb``
    (:func:`_estimate_tile_count_and_mb`; None when it failed), ``need``
    (:func:`_estimate_disk_need`; None when it failed or there is none),
    the failures' texts (``errors``) and ``superseded`` (stopped for a newer
    request — never shown)."""
    __slots__ = ("job", "tiles", "mb", "need", "errors", "superseded")

    def __init__(self, job: _EstimateJob) -> None:
        self.job = job
        self.tiles: Optional[int] = None
        self.mb: Optional[int] = None
        self.need: Optional[Tuple[int, List[Any]]] = None
        self.errors: List[str] = []
        self.superseded = False


def _run_estimate(job: _EstimateJob) -> _EstimateResult:
    """The label's work for *job* — the body of :class:`_EstimateThread`
    (and of :meth:`MapConverterTab.flush_estimate`, inline): the tile count
    and MB (:func:`_estimate_tile_count_and_mb`), then the per-drive need
    (:func:`_estimate_disk_need`) with the tab's cache held under the job's
    lock (``_on_run`` prices with the same cache on the GUI thread). A
    failure of either is kept as its text — the ``disk estimate failed: …``
    warning — never raised. A newer request stops the need at its next
    per-tile poll (``superseded``)."""
    result = _EstimateResult(job)
    try:
        result.tiles, result.mb = _estimate_tile_count_and_mb(
            job.combined, job.out_res, output_dir=job.output_dir,
            overwrite=job.overwrite, entries=job.layers)
    except Exception as exc:  # noqa: BLE001 — informative only
        result.errors.append(str(exc) or type(exc).__name__)
    if job.superseded.is_set():
        result.superseded = True
        return result
    try:
        with job.lock:
            result.need = _estimate_disk_need(
                job.layers, job.out_res, job.output_dir, job.overwrite,
                cache=job.cache, should_cancel=job.superseded.is_set)
    except terrain_adapter.ConverterCancelled:
        result.superseded = True
    except Exception as exc:  # noqa: BLE001 — informative only
        result.errors.append(str(exc) or type(exc).__name__)
    return result


def _estimate_answer(job: _EstimateJob) -> _EstimateResult:
    """:func:`_run_estimate`, and whatever escapes it as the result's
    failure — what the thread and :meth:`MapConverterTab.flush_estimate`
    run: an estimate never takes the GUI down."""
    try:
        return _run_estimate(job)
    except Exception as exc:  # noqa: BLE001 — never take the GUI down
        result = _EstimateResult(job)
        result.errors.append(str(exc) or type(exc).__name__)
        return result


#: Estimate threads still running. A tab closed mid-estimate neither waits
#: for it nor destroys a running QThread: the thread stays referenced here
#: and its result is dropped (the tab's slots are gone) — as
#: ``settings_dialog._LIVE_PROBES``.
_LIVE_ESTIMATES: set = set()


class _EstimateThread(QThread):
    """The estimate label's worker: runs :attr:`job`
    (:func:`_estimate_answer`) and emits :attr:`estimated` with its
    :class:`_EstimateResult`. ONE per tab, started again for each job by
    :meth:`MapConverterTab._start_estimate` — at most one estimate runs, at
    most one waits (``_est_pending``)."""

    estimated = pyqtSignal(object)

    def __init__(self) -> None:
        super().__init__()
        self.job: Optional[_EstimateJob] = None

    def run(self) -> None:  # noqa: D401 — Qt override
        job = self.job
        if job is None:
            return
        result = _estimate_answer(job)
        # Nothing of the job stays with the thread (a thread outliving its
        # tab keeps no stack or cache alive).
        self.job = None
        self.estimated.emit(result)


def _classify_acquisition(source: str) -> str:
    """Route a raster source string: ``'xyz' | 'file' | 'rendered'``.

    * ``xyz`` — elevation tile service; fetched by the toolkit downloader
      into the shared pool (NO QGIS render — rendering XYZ through QGIS is
      the aliasing/UI-freeze path this router removes).
    * ``file`` — local file, folder or GDAL ``/vsi*`` path; ingested
      directly.
    * ``rendered`` — a server QGIS must render (WMS/WMTS/ArcGIS/…);
      exported per TILE via ``terrain_adapter._export_via_qgis``.
    """
    if "type=xyz" in source:
        return "xyz"
    if (os.path.isfile(source) or os.path.isdir(source)
            or source.startswith("/vsi")):
        return "file"
    return "rendered"


def _bounds_union(bounds) -> Optional[Dict[str, float]]:
    """WGS84 union of an iterable of ``{south, north, west, east}`` dicts
    (falsy items skipped); None when there is none."""
    combined: Optional[Dict[str, float]] = None
    for b in bounds:
        if not b:
            continue
        if combined is None:
            combined = {k: float(b[k]) for k in ("south", "north",
                                                 "west", "east")}
            continue
        combined["south"] = min(combined["south"], b["south"])
        combined["north"] = max(combined["north"], b["north"])
        combined["west"] = min(combined["west"], b["west"])
        combined["east"] = max(combined["east"], b["east"])
    return combined


def _apply_extent(entries: List[Any], extent: Optional[Dict[str, float]],
                  rows) -> List[int]:
    """Give each row in *rows* its OWN copy of *extent*; the rows changed.

    Copies, never shares: a later edit of one row's extent must not move
    another's. Out-of-range rows are ignored.
    """
    if not extent:
        return []
    changed = []
    for r in sorted(set(rows)):
        if 0 <= r < len(entries):
            entries[r].extent = dict(extent)
            changed.append(r)
    return changed


def _folder_extent(index: Any, decisions: Optional[Dict[str, str]] = None,
                   crs_overrides: Optional[Dict[str, str]] = None
                   ) -> Optional[Dict[str, float]]:
    """Union of the WGS84 bounds of every file the run would use.

    Read from the folder's footprint index (already built off-thread by
    ``terrain_folder_scan.scan_folder``) — no file is opened here. Files the
    user skipped and unusable ones do not count; a CRS-less file counts in
    the CRS the user assigned to it (the same rule as
    ``terrain_index.source_infos``).
    """
    decisions = decisions or {}
    crs_overrides = crs_overrides or {}
    bounds = []
    for p, e in index.entries.items():
        if decisions.get(p) == terrain_index.DECISION_SKIP:
            continue
        prob = terrain_index.problem_of(e)
        if prob is not None:
            if prob[0] == terrain_index.KIND_NOCRS and p in crs_overrides:
                e = terrain_index.with_crs(e, crs_overrides[p])
                if e is None:
                    continue
            elif not (prob[0] == terrain_index.KIND_SUSPECT
                      and decisions.get(p) == terrain_index.DECISION_USE):
                continue
        bounds.append(e.get("wgs84_bounds"))
    return _bounds_union(bounds)


def _union_extent(entries) -> Optional[Dict[str, float]]:
    """WGS84 union of the raster entries' extents; None when none has one.

    The ONE implementation of the run's combined bbox. ``_on_run``, the
    worker and the torture runner all take their AOI from here — the run
    aborts when they disagree, so they must not be three copies.
    Entries may be ``_LayerEntry`` objects (main thread) or the worker's
    plain dicts; both spell ``layer_type`` and ``extent``.
    """
    def raster_extents():
        for e in entries:
            layer_type = (e["layer_type"] if isinstance(e, dict)
                          else e.layer_type)
            if layer_type != "buildings":
                yield e["extent"] if isinstance(e, dict) else e.extent
    return _bounds_union(raster_extents())


def _pending_render_jobs(entries, out_res, output_dir, overwrite):
    """``(combined bbox, (res, tile) jobs)`` for a run over *entries*.

    The jobs are the run's enumerated grid minus the tiles outside every
    terrain row's extent (:func:`_tiles_within_sources`) and the tiles that
    will be skipped as existing — rendered servers must not be exported for
    tiles nothing will build. ``(None, [])`` when no raster entry has an
    extent.
    """
    combined = _union_extent(entries)
    if combined is None:
        return None, []
    jobs, _outside = _tiles_within_sources(
        _enumerate_tiles(_snap_bbox(combined, out_res), out_res), entries)
    if not overwrite:
        listing = terrain_adapter._pool_listing(output_dir)
        jobs = [(r, t) for r, t in jobs
                if not _output_tile_whole(output_dir, t, listing)]
    return combined, jobs


def _entry_kind(entry: _LayerEntry) -> str:
    """Acquisition kind for a stack entry (``'buildings'`` for vectors)."""
    if entry.layer_type == "buildings":
        return "buildings"
    if os.path.isdir(entry.source_path):
        return "file"
    src = (entry.qgis_layer.source() if entry.qgis_layer is not None
           else entry.source_path)
    return _classify_acquisition(src)


def _jobs_overlapping_extent(
    render_jobs: List[Tuple[int, Dict[str, Any]]],
    extent: Optional[Dict[str, float]],
) -> List[Tuple[int, Dict[str, Any]]]:
    """The ``(res, tile)`` jobs whose tile bbox intersects *extent*."""
    if not extent:
        return list(render_jobs)
    out = []
    for res, tile in render_jobs:
        sub = _subtile_degrees(res)
        tile_bbox = {"north": tile["ul_lat"], "south": tile["ul_lat"] - sub,
                     "west": tile["ul_lon"], "east": tile["ul_lon"] + sub}
        if bboxes_intersect(tile_bbox, extent):
            out.append((res, tile))
    return out


def _tiles_within_sources(
    tiles: List[Tuple[int, Dict[str, Any]]],
    entries: List[Any],
) -> Tuple[List[Tuple[int, Dict[str, Any]]], List[Tuple[int, Dict[str, Any]]]]:
    """``(kept, dropped)`` of *tiles* — THE rule for the tiles a run builds
    (owner, 2026-11-07): a tile whose bbox intersects no TERRAIN row's
    extent (:func:`_jobs_overlapping_extent`) is dropped. Terrain rows are
    the xyz / rendered-server / file / folder rows; a row without an extent
    is the whole service and overlaps every tile; building rows never count.
    The coarse grids snap outward on their own lines (5 / 10 m 0.25°, 30 m
    0.5°, 90 m 1°), so whole tiles can lie past every extent — fed by no
    source, they were converted empty (the owner's run of 2026-11-05: 56 of
    2,438 tiles, about 2 GB, no buildings). A tile that reaches into an
    extent stays. *entries* are the stack's rows, ``_LayerEntry`` objects or
    the worker's dicts (as :func:`_union_extent`). Applied AFTER the grid
    cross-check (the plan keeps the full grid) and by the pre-run estimate.
    Both lists keep the enumeration's order; a kept tile is the very
    ``(res, tile)`` of *tiles*."""
    left = list(tiles)
    for e in entries:
        if not left:
            break
        if isinstance(e, dict):
            layer_type, extent = e.get("layer_type"), e.get("extent")
        else:
            layer_type, extent = e.layer_type, e.extent
        if layer_type == "buildings":
            continue
        inside = {t["filename"]
                  for _r, t in _jobs_overlapping_extent(left, extent)}
        left = [rt for rt in left if rt[1]["filename"] not in inside]
    if not left:
        return list(tiles), []
    out = {t["filename"] for _r, t in left}
    return [rt for rt in tiles if rt[1]["filename"] not in out], left


def _skipped_tiles_line(dropped: List[Tuple[int, Dict[str, Any]]]) -> str:
    """The ONE log line of the tiles :func:`_tiles_within_sources` dropped:
    ``  56 tile(s) outside every source skipped (the 5 m, 10 m grids reach
    past the layers' extents): 28 × 5 m, 28 × 10 m``."""
    per_res: Dict[int, int] = {}
    for res, _t in dropped:
        per_res[res] = per_res.get(res, 0) + 1
    grids = ", ".join(f"{r} m" for r in sorted(per_res))
    reach = "grids reach" if len(per_res) > 1 else "grid reaches"
    return (f"  {len(dropped):,} tile(s) outside every source skipped (the "
            f"{grids} {reach} past the layers' extents): "
            + ", ".join(f"{per_res[r]:,} × {r} m" for r in sorted(per_res)))


def _entry_name(entry: _LayerEntry) -> str:
    """How a stack row is named in a message: the QGIS layer's name, else
    the file / folder name (a service URI shortened)."""
    layer = entry.qgis_layer
    if layer is not None and hasattr(layer, "name"):
        try:
            return str(layer.name())
        except RuntimeError:            # the layer was deleted
            pass
    src = entry.source_path or ""
    base = os.path.basename(src.rstrip("/\\"))
    if base and not ("type=xyz" in src or "://" in src):
        return base
    return src if len(src) <= 60 else src[:57] + "..."


#: An output resolution is finer than a source only below this fraction of
#: the source's native resolution (finer by more than 10 %): the run's
#: upsampling warnings and the Auto pick (:func:`auto_output_resolutions`)
#: share it.
_FINER_THAN_SOURCE = 0.9


def _upsampling_warnings(layers: List[_LayerEntry],
                         out_res: List[int]) -> List[str]:
    """One line per raster source that a checked output resolution is
    finer than (beyond 10 % of its native resolution) — that output is
    interpolated wherever this source fills it. Said at run start, for the
    run's checked resolutions (no per-row resolution exists)."""
    lines = []
    for e in layers:
        if e.layer_type == "buildings" or not e.native_res_m:
            continue
        finer = sorted(r for r in out_res
                       if r < e.native_res_m * _FINER_THAN_SOURCE)
        if finer:
            lines.append(
                f"'{_entry_name(e)}' (native ~{e.native_res_m:.1f} m): the "
                f"{', '.join(f'{r} m' for r in finer)} output is finer than "
                f"this source — interpolated where it fills, no detail "
                f"gained")
    return lines


# ---------------------------------------------------------------------------
# Auto output resolutions (owner, 2026-11-07: "improve the auto pick to a
# reasonable range, especially with more than one coverage layer")
# ---------------------------------------------------------------------------

#: The rings of Site Analysis's adaptive profile every finer base steps
#: through (``range_profile.adaptive_bands``: the base to 7 km, then up the
#: ladder to 30 m and the far step ``ADAPTIVE_FAR_STEP_M`` = 90 m).
_AUTO_RINGS_M = (30, 90)

#: The Auto pick when the stack names no resolution (no terrain row, or
#: none with a known one) — the box ticked when the tab opens.
_AUTO_DEFAULT_M = 30


def _finest_terrain_res(entries: List[Any]) -> Optional[float]:
    """The finest native resolution (m) of the TERRAIN rows of *entries*
    (xyz / rendered / file / folder; building rows never count) — each
    row's ``native_res_m``, filled when it was added: a folder's from its
    in-memory index (:func:`_index_finest_res`), an XYZ layer's from
    ``terrain_adapter.xyz_native_resolution_m``, any other layer's from
    :func:`_detect_resolution`. None when no terrain row has one."""
    vals = []
    for e in entries:
        if getattr(e, "layer_type", None) == "buildings":
            continue
        res = getattr(e, "native_res_m", None)
        try:
            if res is not None and float(res) > 0:
                vals.append(float(res))
        except (TypeError, ValueError):
            continue
    return min(vals) if vals else None


def _tile_resolution_for(native_m: float) -> int:
    """*native_m* snapped UP to a tile resolution (a key of
    ``_ABT_EXTENT_DEG``): the finest one that is not finer than the source —
    finer only beyond 10 % (:data:`_FINER_THAN_SOURCE`, the run's
    upsampling rule): 2.0 → 2, 4.8 → 5, 9 → 10, 25 → 30, 30.9
    (1″ at the equator) → 30, 85 → 90, 92.6 (3″) → 90, past 100 → 250;
    finer than 2 → 2."""
    keys = sorted(_ABT_EXTENT_DEG)
    for res in keys:
        if res >= native_m * _FINER_THAN_SOURCE:
            return res
    return keys[-1]


def auto_output_resolutions(entries: List[Any]) -> List[int]:
    """The output resolutions the Auto pick ticks for stack *entries* (rows,
    ``_LayerEntry``): the finest native resolution of the terrain rows
    (:func:`_finest_terrain_res`) snapped up to a tile resolution
    (:func:`_tile_resolution_for`), plus the adaptive profile's 30 m and
    90 m rings coarser than it (:data:`_AUTO_RINGS_M`); no terrain row with a
    known resolution → [30].

    ONE fine resolution for the whole stack, not one per layer: the owner's
    stack (swisstopo 2 m folder + Mapzen 4.8 m XYZ over the same extent)
    gives [2, 30, 90] — a 2 m tile then exists everywhere, so the analysis
    never picks a 5 m one; ticked by hand as 2/5/10/30/90 it built 616 tiles
    of 5 m and 10 m (24.1 GB) no profile uses. 2.0 → [2, 30, 90], 4.8 →
    [5, 30, 90], 25 → [30, 90], 85 → [90], 300 → [250], 0.5 → [2, 30, 90]."""
    finest = _finest_terrain_res(entries)
    if finest is None:
        return [_AUTO_DEFAULT_M]
    fine = _tile_resolution_for(finest)
    return [fine] + [r for r in _AUTO_RINGS_M if r > fine]


def _auto_resolutions_reason(entries: List[Any]) -> str:
    """Why :func:`auto_output_resolutions` gives what it gives:
    ``finest source 2.0 m; the analysis profile's 30 m and 90 m rings``."""
    finest = _finest_terrain_res(entries)
    if finest is None:
        return "no terrain row with a known resolution"
    rings = auto_output_resolutions(entries)[1:]
    text = f"finest source {finest:.1f} m"
    if rings:
        text += (f"; the analysis profile's "
                 f"{' and '.join(f'{r} m' for r in rings)} "
                 f"ring{'s' if len(rings) > 1 else ''}")
    return text


def _auto_resolutions_line(entries: List[Any]) -> str:
    """The ONE log line of an applied Auto pick: ``  output resolutions:
    auto 2 m, 30 m, 90 m (finest source 2.0 m; the analysis profile's 30 m
    and 90 m rings)``."""
    res = auto_output_resolutions(entries)
    return (f"  output resolutions: auto {', '.join(f'{r} m' for r in res)} "
            f"({_auto_resolutions_reason(entries)})")


def _auto_resolutions_tip(entries: List[Any]) -> str:
    """The Auto button's tooltip: the rule, and what it gives for *entries*."""
    res = auto_output_resolutions(entries)
    return (
        "Tick the output resolutions the stack calls for: the finest native "
        "resolution of the terrain rows (folder, file, XYZ or map service — "
        "building rows do not count), snapped up to a tile resolution "
        "(2, 5, 10, 30, 90, 250 m; a source up to 10 % coarser still counts "
        "as that resolution), plus the 30 m and 90 m rings of Site "
        "Analysis' adaptive profile where coarser. ONE fine resolution for "
        "the whole stack, so the analysis never picks a coarser fine tile "
        "where a finer one could be built. No terrain row with a known "
        "resolution: 30 m.\n\n"
        f"This stack: {', '.join(f'{r} m' for r in res)} "
        f"({_auto_resolutions_reason(entries)}).\n\n"
        "Applied by itself whenever a row is added or removed or an extent "
        "is set, until you tick a box by hand; Auto applies it again and "
        "turns that back on.")


def _render_resolution_m(entry: _LayerEntry,
                         render_jobs: List[Tuple[int, Dict[str, Any]]]) -> float:
    """Resolution a rendered server is exported at, in metres.

    Delegates to :func:`terrain_adapter.render_resolution_m` — the ONE rule
    both tabs must share (the finer of native and finest requested output),
    so a rendered server hands the converter the same pixels whichever tab
    asked. This is what removes the WMS aliasing the old whole-extent
    nearest-neighbour export had.
    """
    finest_out = float(min((r for r, _t in render_jobs), default=30))
    return terrain_adapter.render_resolution_m(entry.native_res_m, finest_out)


class _ResolveCancelled(Exception):
    """Internal: the user cancelled the resolve/render progress dialog."""


def _render_tiles_via_qgis(
    entry: _LayerEntry,
    render_jobs: List[Tuple[int, Dict[str, Any]]],
    on_tile: Any = None,
) -> Dict[str, str]:
    """Render a WMS/WMTS/ArcGIS layer per TILE; ``{tile filename: tif path}``.

    ``terrain_adapter._export_via_qgis`` is the ONLY renderer (main-thread —
    QGIS map layers are not thread-safe, which is why this runs during the
    resolve phase). Each enumerated tile overlapping the entry's extent is
    rendered to its own WGS84 GeoTIFF (tile bbox + ~4 output-px halo) at
    :func:`_render_resolution_m`. *on_tile(filename)* ticks the progress
    dialog after each tile.

    Any failure is a HARD error naming the layer — the old code swallowed
    every exception and silently passed the raw source string onward, which
    the converter could not open.
    """
    layer = entry.qgis_layer
    name = (layer.name() if layer is not None and hasattr(layer, "name")
            else entry.source_path)
    if layer is None:
        raise RuntimeError(
            f"Layer '{entry.source_path}' is a rendered map service but is "
            "not loaded in QGIS, so it cannot be rendered. Add it to the "
            "project and re-add it to the converter stack."
        )
    jobs = _jobs_overlapping_extent(render_jobs, entry.extent)
    render_res = _render_resolution_m(entry, render_jobs)
    out: Dict[str, str] = {}
    for res, tile in jobs:
        fn = tile["filename"]
        sub = _subtile_degrees(res)
        tile_bbox = {"north": tile["ul_lat"], "south": tile["ul_lat"] - sub,
                     "west": tile["ul_lon"], "east": tile["ul_lon"] + sub}
        res_deg = tile["exact_res_m"] / 111_111.0
        sub_bbox = _expand_bbox(tile_bbox, res_deg * 4.0)
        dest = os.path.join(
            tempfile.gettempdir(),
            f"aether_render_{os.getpid()}_{id(entry)}_{fn}.tif",
        )
        try:
            terrain_adapter._export_via_qgis(layer, dest, sub_bbox,
                                             render_res)
        except _ResolveCancelled:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Rendering layer '{name}' failed for tile {fn}: {exc}\n\n"
                "The run was aborted — a silently skipped layer would "
                "convert the wrong terrain. Check the service (login, "
                "network, extent) and run again."
            ) from exc
        out[fn] = dest
        if on_tile is not None:
            on_tile(fn)
    return out


def _resolve_source_on_main_thread(
    entry: _LayerEntry,
    render_jobs: Optional[List[Tuple[int, Dict[str, Any]]]] = None,
    on_tile: Any = None,
) -> Any:
    """Resolve one stack entry on the main thread (QGIS layers live here).

    Buildings entries keep the plain-path contract (vector pipeline).
    Raster entries return an acquisition dict routed by
    :func:`_classify_acquisition`:

    * ``{"kind": "file", "path": …}`` — local file/folder//vsi, untouched.
    * ``{"kind": "xyz", "uri": …, "extent": …}`` — NO render; the worker
      downloads through the shared toolkit pool.
    * ``{"kind": "rendered", "tiles": {filename: tif}}`` — per-tile QGIS
      render (:func:`_render_tiles_via_qgis`), done HERE because map layers
      are main-thread-only.

    Failures are hard errors — the old catch-all that silently passed the
    raw source path onward is gone.
    """
    layer = entry.qgis_layer
    src = layer.source() if layer is not None else entry.source_path

    if entry.layer_type == "buildings":
        # Vector sources: plain path, conversion happens in the worker.
        if layer is not None and (os.path.isfile(src)
                                  or src.startswith("/vsi")):
            return src
        return entry.source_path

    if os.path.isdir(entry.source_path):
        return {"kind": "file", "path": entry.source_path}

    kind = _classify_acquisition(src)
    if kind == "file":
        return {"kind": "file", "path": src}
    # Fail loudly BEFORE acquiring from ANY service that classifies as
    # imagery — the xyz downloader would Terrarium-decode a picture into
    # garbage terrain, and a rendered (WMS/WMTS/ArcGIS) export would ingest
    # colour bytes as metres. The add-time "Possible Imagery — Add anyway?"
    # prompt lets a user keep such a layer in the stack for inspection;
    # converting it is where the line is drawn, whichever branch it takes.
    if (layer is not None
            and classify_raster_layer(layer) == "imagery"):
        lname = layer.name() if hasattr(layer, "name") else src
        if kind == "xyz":
            raise RuntimeError(
                f"Layer '{lname}' classifies as imagery, not elevation — "
                "it cannot be converted to terrain. Its XYZ tiles are an "
                "RGB picture; decoding them as elevation would produce "
                "garbage. Use an elevation-encoded service (Terrarium / "
                "Mapbox Terrain-RGB) or remove the layer from the stack."
            )
        raise RuntimeError(
            f"Layer '{lname}' classifies as imagery, not elevation — it "
            "cannot be converted to terrain. A rendered map service "
            "(WMS / WMTS / ArcGIS) returns a picture of whatever the "
            "server drew; ingesting it would read colour values as "
            "metres. Use an elevation source (a DEM file or folder, a "
            "WCS coverage, or a Terrain-RGB tile service) or remove the "
            "layer from the stack."
        )
    if kind == "xyz":
        return {"kind": "xyz", "uri": src,
                "extent": dict(entry.extent) if entry.extent else None}
    return {"kind": "rendered",
            "tiles": _render_tiles_via_qgis(entry, render_jobs or [],
                                            on_tile)}


def _kill_procs(procs: List[Any], timeout: float = 3.0) -> None:
    """Terminate every process of *procs* at once, then wait for them —
    *timeout* seconds for all together, not per process — and kill what is
    left. Through ``Popen`` only (``terminate``/``kill`` are the platform's
    own: TerminateProcess on Windows, SIGTERM/SIGKILL on Linux and macOS);
    a process that already ended is left alone by ``Popen`` itself."""
    for proc in procs:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001 — best effort, as before
            pass
    deadline = time.monotonic() + timeout
    for proc in procs:
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass


def _kill_proc(proc: subprocess.Popen) -> None:
    _kill_procs([proc])


def resolve_sources_with_progress(layers, parent=None, render_jobs=None,
                                  on_progress=None, progress_model=None):
    """Resolve every layer on the main thread, visibly, per-tile for renders.

    *render_jobs* is the run's enumerated ``(res, tile)`` list (minus tiles
    that will be skipped as existing); rendered servers are exported per
    tile here, and the dialog ticks per TILE — the old per-layer tick froze
    at 0 for the whole multi-minute export. *on_progress(frac)* is called
    after every tick; *progress_model* (the run's ``RunProgress``) is
    advanced per rendered tile on slot ``("render", i)`` — the tab's ONE bar
    (see :func:`_estimate_run_work`).

    This work cannot move to the worker: rendering goes through a
    ``QgsRasterLayer``, and QGIS map layers are main-thread-only.

    Returns the resolved list, or ``None`` if the user cancelled. Hard
    errors (render failures, unloaded layers) propagate as RuntimeError.
    The dialog is window-modal so pumping events here cannot re-enter Run.
    """
    render_jobs = render_jobs or []
    weights = []
    for entry in layers:
        if _entry_kind(entry) == "rendered":
            weights.append(
                max(1, len(_jobs_overlapping_extent(render_jobs,
                                                    entry.extent))))
        else:
            weights.append(1)
    total = sum(weights) or 1

    dlg = QProgressDialog("Preparing layers…", "Cancel", 0, total, parent)
    dlg.setWindowTitle("Map Converter")
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    # Show at once: the first layer is often the slowest, and a dialog that
    # only appears after the default 4 s delay is the freeze all over again.
    dlg.setMinimumDuration(0)
    dlg.setValue(0)
    QApplication.processEvents()

    done = 0

    tile_work = {t["filename"]: _render_tile_work(t) for _r, t in render_jobs}

    def tick(label: Optional[str] = None, slot: Any = None,
             fn: Optional[str] = None) -> None:
        nonlocal done
        done += 1
        if progress_model is not None and slot is not None:
            progress_model.set_phase("Rendering map service tiles",
                                     {"done": done, "total": total,
                                      "unit": "tiles"})
            progress_model.advance(slot, tile_work.get(fn, 0.0))
        dlg.setValue(min(done, total))
        if label:
            dlg.setLabelText(label)
        if on_progress is not None:
            on_progress(done / total)
        QApplication.processEvents()
        if dlg.wasCanceled():
            raise _ResolveCancelled()

    resolved = []
    try:
        for i, entry in enumerate(layers):
            name = os.path.basename(entry.source_path or "") or f"layer {i + 1}"
            dlg.setLabelText(f"Preparing {name} ({i + 1}/{len(layers)})…")
            QApplication.processEvents()
            if dlg.wasCanceled():
                return None
            if _entry_kind(entry) == "rendered":
                resolved.append(_resolve_source_on_main_thread(
                    entry, render_jobs,
                    on_tile=lambda fn, _n=name, _i=i: tick(
                        f"Rendering {_n}: {fn}", ("render", _i), fn)))
                if progress_model is not None:
                    progress_model.finish(("render", i))
            else:
                resolved.append(
                    _resolve_source_on_main_thread(entry, render_jobs))
                tick()
    except _ResolveCancelled:
        return None
    finally:
        dlg.close()
    return resolved


class _RunFailed(Exception):
    """Internal: the run ends with this message (``finished_err``). Raised
    where a phase fails (logged there as before); :meth:`_MapConverterWorker.run`
    reports it once every concurrent branch has been stopped."""


class _Batch:
    """One Phase-3 batch of the worker's ingest queue: *tiles* to convert
    with the whole stack, *bid* its id (progress slot ``("convert", bid)``,
    job file ``aether_conv_<pid>_<bid>.json``), *what* its name in the
    label — ``"band 2/3"``, ``"Phase-2 holes"``, or ``""`` for the tiles no
    download feeds. *ready_t*: ``time.perf_counter()`` when it was queued
    (the batch's timing line)."""

    __slots__ = ("id", "tiles", "what", "ready_t")

    def __init__(self, bid: int, tiles: List[Tuple[int, Dict[str, Any]]],
                 what: str) -> None:
        self.id = bid
        self.tiles = tiles
        self.what = what
        self.ready_t = time.perf_counter()


def _possessive(what: str) -> str:
    """``"band 3/4's"``, ``"Phase-2 holes'"`` — a unit's name in the timing
    line."""
    return what + ("'" if what.endswith("s") else "'s")


class _DownloadBranch:
    """The download into ONE pool, next to Phase 2 and the ingest queue
    (2026-10-21: the download no longer waits for the local conversion;
    2026-10-23: per latitude band; 2026-10-25: on two lanes).

    Its UNITS are ``(("band", b), calls)`` — band *b*'s calls of
    :func:`_band_calls`, finest zoom first (the bands come north to south)
    — or ``(("holes",), calls)``, the second pass over the tiles Phase 2
    left with holes, which the worker puts in front of the next unit
    (:meth:`push_front`) once Phase 2 has spoken.

    TWO LANES, one thread each (owner's run, 2026-10-24: the z15 passes ran
    at line rate, 27 MB/s, while the coarser passes — served from the raw
    tile cache, the toolkit derives z14/z12/z11 from the cached z15 tiles —
    read and wrote the disk with the network idle, ~150 s of a 750 s
    lane). The FETCH lane runs each unit's FINEST call, unit after unit;
    as soon as it has ended, the unit's remaining (coarser) calls go to
    the DERIVE lane, which runs them unit after unit in the order the fetch
    lane finished them. ONE BAND BEHIND (2026-11-05): a band's coarser
    calls are held (:meth:`_held_for`) until the NEXT band's finest call
    has landed — the toolkit derives a coarse source tile only when the
    cache holds all four finer children, and a tile straddling the band
    edge has two of them in the next band; held, those edge tiles derive
    from z15 like the interior ones in every run, instead of being derived
    or fetched (the service's own, differently resampled tile) by the race
    with the concurrent z15 pass — so band *b*'s coarser passes run beside
    band *b*+2's finest one, the last two bands' after the fetch lane is
    done (owner's run 2026-11-05: about a minute of 18). The hole unit is
    not held (its tiles lie anywhere; the derive lane takes the first unit
    that may run). A unit with one call is done when that call is. The
    worker hears of a unit (:meth:`_MapConverterWorker._unit_done`) only
    once BOTH lanes are done with it: its Phase-3 batch needs every zoom.
    *serial* (``WAVESHED_SERIAL_ZOOMS=1``, :func:`_serial_zooms_requested`):
    ONE lane, each unit's calls one after the other — the order before
    2026-10-25, call for call.

    Every call is one converter process through the shared downloader
    (``ensure_pool_tiles`` → ``run_converter_streaming``) with the worker's
    ``on_start`` hook, so Stop kills both lanes' processes with every other
    one. Each lane is its own progress activity
    (:func:`_download_activity`; two calls of one banded slot are kept
    apart by the :class:`_SlotLedger`). The branch ends (:attr:`ended`,
    the worker told once) when it is closed (:meth:`close`) and both lanes
    are out of work. Entries with different pools download in several
    branches at once; entries sharing a pool share its branch
    (:func:`_pool_calls`). *k* is the pool's first entry (the progress
    activities). A failure in either lane stops every other branch and
    process (``_MapConverterWorker._halt``; the first failure is the
    branch's :attr:`error`, reported by the worker once all have ended); a
    stop is quiet. Per unit with coarser calls one timing line
    (:meth:`_timing_line`).
    """

    def __init__(self, worker: "_MapConverterWorker", k: int, pool: str,
                 units: List[Any], closed: bool = False,
                 serial: bool = False) -> None:
        self.k = k
        self.pool = pool
        self.serial = bool(serial)
        self.error: Optional[str] = None
        self.cancelled = False
        #: Set (under the worker's scheduler lock) when both lanes are done.
        self.ended = False
        self._worker = worker
        # The fetch lane's units; the derive lane's (unit key, its coarser
        # calls, the fetch record of its finest call).
        self._units: "deque[Any]" = deque(units)
        self._derive: "deque[Tuple[Any, List[Any], List[Any]]]" = deque()
        self._closed = closed
        # The fetch lane still runs: more derive work may come.
        self._fetching = True
        self._lanes = 1 if self.serial else 2
        # Every finest call of the fetch lane: [unit name, zoom, start,
        # end or None, unit key] (perf_counter) — what ran beside a derive
        # pass, and which band's finest a held unit waits for.
        self._fetch_log: List[List[Any]] = []
        # The unit the fetch lane took but has not started yet (between
        # _next_unit and _fetch_started): a band here still holds the
        # band before it.
        self._pending_key: Optional[Tuple[Any, ...]] = None
        # id(record) -> (band waited for, perf_counter the hold began) of
        # the derive-queue items held by _held_for.
        self._held: Dict[int, Tuple[str, float]] = {}
        self._cv = threading.Condition()
        self._thread = threading.Thread(
            target=self._run_fetch, name=f"waveshed-download-{k}",
            daemon=True)
        self._derive_thread = (None if self.serial else threading.Thread(
            target=self._run_derive, name=f"waveshed-derive-{k}",
            daemon=True))

    def start(self) -> None:
        self._thread.start()
        if self._derive_thread is not None:
            self._derive_thread.start()

    def join(self) -> None:
        self._thread.join()
        if self._derive_thread is not None:
            self._derive_thread.join()

    def alive(self) -> bool:
        return self._thread.is_alive() or (
            self._derive_thread is not None
            and self._derive_thread.is_alive())

    def push_front(self, unit: Any) -> None:
        """Run *unit* next on the fetch lane — after the unit fetching now
        (with *serial*: after the unit downloading now)."""
        with self._cv:
            self._units.appendleft(unit)
            self._cv.notify_all()

    def close(self) -> None:
        """No more units will come: the branch ends after the last one."""
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    # -- The lanes ------------------------------------------------------

    def _next_unit(self, activity: Any) -> Optional[Any]:
        w = self._worker
        with self._cv:
            idle = False
            while not self._units and not self._closed and not w._stopping():
                if not idle and not self.serial:
                    # Nothing to fetch now: no stale pass in the label.
                    w.progress_model.end_activity(activity)
                    idle = True
                self._cv.wait(0.2)
            if w._stopping() or not self._units:
                return None
            unit = self._units.popleft()
            self._pending_key = unit[0]
            return unit

    def _held_for(self, item: Tuple[Any, List[Any], List[Any]]
                  ) -> Optional[str]:
        """Why unit *item*'s coarser passes wait — the name of the band
        whose finest call must land first — or None: they may run. A band
        waits for the next band on the fetch lane (started after its own
        finest call: running, or still queued); the last band, the hole
        unit, and everything once the fetch lane is done run at once.
        Under ``_cv``."""
        key, _calls, record = item
        if key[0] != "band" or not self._fetching:
            return None
        after = False
        for r in self._fetch_log:
            if r is record:
                after = True
            elif after and r[4][0] == "band":
                return None if r[3] is not None else r[0]
        pending = self._pending_key
        if pending is not None and pending[0] == "band":
            return self._worker._unit_what(pending)
        for u_key, _c in self._units:
            if u_key[0] == "band":
                return self._worker._unit_what(u_key)
        return None

    def _next_derive(self, activity: Any
                     ) -> Optional[Tuple[Any, List[Any], List[Any],
                                         Optional[Tuple[str, float]]]]:
        """The next unit whose coarser passes may run — the first of the
        derive queue not held (:meth:`_held_for`) — with ``(band waited
        for, seconds held)`` when it was held, else None; None when the
        run stops or no more work can come."""
        w = self._worker
        with self._cv:
            idle = False
            while not w._stopping():
                pick = None
                for item in self._derive:
                    why = self._held_for(item)
                    if why is None:
                        pick = item
                        break
                    self._held.setdefault(id(item[2]),
                                          (why, time.perf_counter()))
                if pick is not None:
                    self._derive.remove(pick)
                    held = self._held.pop(id(pick[2]), None)
                    return (*pick, (held[0], time.perf_counter() - held[1])
                            if held else None)
                if not self._derive and not self._fetching:
                    return None
                if not idle:
                    w.progress_model.end_activity(activity)
                    idle = True
                self._cv.wait(0.2)
            return None

    def _fetch_started(self, key: Tuple[Any, ...], calls: List[Any]
                       ) -> List[Any]:
        record = [self._worker._unit_what(key), calls[0][0][2],
                  time.perf_counter(), None, key]
        with self._cv:
            self._fetch_log.append(record)
            self._pending_key = None
        return record

    def _fetch_ended(self, record: List[Any]) -> None:
        with self._cv:
            if record[3] is None:
                record[3] = time.perf_counter()
            self._cv.notify_all()

    def _run_fetch(self) -> None:
        w = self._worker
        activity = _download_activity(self.k)
        try:
            while True:
                unit = self._next_unit(activity)
                if unit is None:
                    break
                key, calls = unit
                if self.serial:
                    self._serial_unit(key, calls, activity)
                    continue
                record = self._fetch_started(key, calls)
                w._download_unit(key, calls[:1], activity)
                self._fetch_ended(record)
                if len(calls) > 1:
                    with self._cv:
                        self._derive.append((key, calls[1:], record))
                        self._cv.notify_all()
                else:
                    w._unit_done(self.pool, key)
            if w._stopping():
                self.cancelled = True
        except terrain_adapter.ConverterCancelled:
            self._cancelled_itself()
        except Exception as exc:  # noqa: BLE001 — reported by the worker
            self._failed(exc)
        finally:
            self._lane_ended(activity, fetch=True)

    def _serial_unit(self, key: Tuple[Any, ...], calls: List[Any],
                     activity: Any) -> None:
        """One unit on the one lane (*serial*): its calls one after the
        other, as before the two lanes."""
        record = self._fetch_started(key, calls)
        ends: List[float] = []

        def call_end(_call: Any) -> None:
            if not ends:
                self._fetch_ended(record)
            ends.append(time.perf_counter())

        self._worker._download_unit(key, calls, activity,
                                    on_call_end=call_end)
        if len(calls) > 1 and ends:
            self._landed(key, record, ends[0], ends[-1])
        else:
            self._worker._unit_done(self.pool, key)

    def _run_derive(self) -> None:
        w = self._worker
        activity = _download_activity(self.k, derive=True)
        try:
            while True:
                item = self._next_derive(activity)
                if item is None:
                    break
                key, calls, record, held = item
                started = time.perf_counter()
                w._download_unit(key, calls, activity)
                self._landed(key, record, started, time.perf_counter(),
                             held)
            if w._stopping():
                self.cancelled = True
        except terrain_adapter.ConverterCancelled:
            self._cancelled_itself()
        except Exception as exc:  # noqa: BLE001 — reported by the worker
            self._failed(exc)
        finally:
            self._lane_ended(activity, fetch=False)

    def _landed(self, key: Tuple[Any, ...], record: List[Any],
                started: float, ended: float,
                held: Optional[Tuple[str, float]] = None) -> None:
        """Both lanes are done with unit *key*: its timing line, then the
        worker hears of it (its batch forms)."""
        self._worker._log(self._timing_line(record, started, ended, held))
        self._worker._unit_done(self.pool, key)

    def _timing_line(self, record: List[Any], started: float,
                     ended: float,
                     held: Optional[Tuple[str, float]] = None) -> str:
        """``  timing: band 2/4: z15 landed at +X s, coarser passes landed
        at +Y s, ran beside band 3/4's z15: yes`` — *record* the unit's
        finest call, *started* / *ended* its coarser passes (zero = the
        run's start). "yes" when a finest call of another unit ran on the
        fetch lane while they did (named: the first such); else the next
        one to come is named, or none was downloading. *held* = ``(band,
        seconds)`` the coarser passes waited for that band's finest call
        (:meth:`_held_for`): ``; held 61.0 s until band 3/4's z15
        landed`` is appended."""
        t0 = self._worker._t0
        what, zoom, _start, fetched = record[:4]
        with self._cv:
            others = [r for r in self._fetch_log if r is not record]
            beside = [r for r in others
                      if r[2] < ended and (r[3] is None or r[3] > started)]
            later = [r for r in others if r[2] >= ended]
            queued = self._units[0] if self._units else None
        if beside or later:
            other = (beside or later)[0]
            tail = (f"ran beside {_possessive(other[0])} z{other[1]}: "
                    + ("yes" if beside else "no"))
        elif queued is not None:
            tail = (f"ran beside "
                    f"{_possessive(self._worker._unit_what(queued[0]))} "
                    f"z{queued[1][0][0][2]}: no")
        else:
            tail = (f"ran beside another band's z{zoom}: no (none was "
                    f"downloading)")
        landed = (fetched if fetched is not None else started) - t0
        if held is not None:
            tail += (f"; held {held[1]:.1f} s until {_possessive(held[0])} "
                     f"z{zoom} landed")
        return (f"  timing: {what}: z{zoom} landed at +{landed:.1f} s, "
                f"coarser passes landed at +{ended - t0:.1f} s, {tail}")

    # -- Ends -----------------------------------------------------------

    def _cancelled_itself(self) -> None:
        self.cancelled = True
        if not self._worker._stopping():
            # A download that cancelled itself: the run ends as a Cancel
            # (quietly) — nothing converts on without it.
            self._worker._halt()

    def _failed(self, exc: Exception) -> None:
        w = self._worker
        if w._stopping():
            # Killed by Stop, by the other lane's or another branch's
            # failure: the resulting "download failed" is not this lane's
            # error.
            self.cancelled = True
            return
        with self._cv:
            first = self.error is None
            if first:
                self.error = (str(exc) if isinstance(exc, RuntimeError)
                              else f"Unexpected error: {exc}")
        if first and not isinstance(exc, RuntimeError):
            w._log(f"ERROR: {exc}")
        w._halt()

    def _lane_ended(self, activity: Any, fetch: bool) -> None:
        w = self._worker
        w.progress_model.end_activity(activity)
        with self._cv:
            if fetch:
                self._fetching = False
            self._lanes -= 1
            last = self._lanes == 0
            self._cv.notify_all()
        if last:
            w._branch_ended(self)


class _BuildingsRunFailed(_RunFailed):
    """Internal: an ingest batch's buildings could not be verified (a tile
    the converter could not decode, or no burn reported). Tiles the batch
    wrote into the output dir are recorded :data:`_BUILDINGS_UNVERIFIED`."""


#: Stops an ingest batch whose converter could not decode building tiles
#: (it only warns and goes on) — raised from the line handler when the
#: run's ``osm_buildings.BurnMonitor`` says so, so the runner kills the
#: process before a tile is written.
_UndecodableBuildings = osm_buildings.UndecodableBuildings


#: ``FetchFailed.cause`` of a fetch none of whose tiles has data
#: (``openfreemap.fetch_building_tiles``, through ``osm_buildings``).
_NO_BUILDING_DATA = osm_buildings.NO_DATA_CAUSE


class _BuildingsBranch(osm_buildings.BuildingsFetch):
    """The run's OpenFreeMap z14 building fetch, in its own thread
    (2026-10-22; replaced the Overpass query) — since 2026-10-23 the shared
    ``osm_buildings.BuildingsFetch`` (Site Analysis's download runs the
    same thread) bound to this worker: its log, progress slot, status line
    and halt. Behaviour below unchanged.

    Phase 1 used to send ONE Overpass query for the whole extent and wait
    for it before the tile list: 16.6 deg² answered empty after 107 s, the
    run logged "Downloaded 0 buildings" and went on WITHOUT buildings, and
    the terrain download had waited for nothing. Now the z14 tiles the
    pending output tiles need (:meth:`_OsmBuildings.needed`) are fetched in
    parallel into the store Site Analysis uses (``osm_buildings.ensure_tiles``,
    the one strict fetch of both tabs), next to the terrain download and
    the ingest queue, in *groups* in the order the ingest batches come
    (2026-10-23): Phase 2's tiles first, then the tiles no download feeds,
    then the bands north to south. An ingest batch waits only for ITS names
    (:meth:`wait_for`, from ``_MapConverterWorker._pbf_view``): a set of the
    names in the store (or known to have no data) and a Condition, announced
    after each group — the last one after ``Buildings ready``. A batch also
    waits until some tile of the fetch has data (a fetch where none has is
    a failure — no batch may burn "no buildings" before that is ruled out).
    On aether_converter >= 0.2.12 (2026-10-25, toolkit CONTRACT item 48)
    the batch does not wait: it is launched at once with its view's marker
    fields, and this thread links its names into the view and writes the
    marker as soon as they are in, by the same rule
    (:meth:`link_when_ready`; at once when they are in already) — a view
    that cannot be completed fails the fetch in its own words. On >= 0.2.14
    (CONTRACT changelog 51) nothing is linked: it writes the batch's LIST
    by that rule (:meth:`list_when_ready`), the converter reads the store.
    Progress: slot :data:`_BUILDINGS_SLOT` (one unit per tile to fetch), the
    activity "Downloading buildings (OpenFreeMap z14)", a log line every
    10 % — over the whole fetch. A failure — a tile still failing after its
    retries, an undecodable answer, no tile with data at all — stops every
    other branch (``_MapConverterWorker._halt``) and ends the run with its
    error (whole-fetch counts); Stop ends it quietly.
    """

    def __init__(self, worker: "_MapConverterWorker", osm: _OsmBuildings,
                 groups: List[List[Tuple[int, int, int]]]) -> None:
        self.osm = osm
        self._worker = worker
        self._units = run_progress.DOWNLOAD_UNITS_PER_SOURCE_TILE
        self._emitted = 0.0
        # The fetch, its order, its waits and its verdict are the shared
        # thread's (no data at all = a failure here); this worker adds its
        # log, its progress slot / status line and its halt.
        super().__init__(
            groups, should_cancel=worker._stopping, log=worker._log,
            cover=osm, require_data=True, on_begin=self._begin,
            on_start=self._fetch_started, on_progress=self._fetch_progress,
            on_fetched=self._fetched, on_failed=self._failed,
            on_end=self._ended, failure_text=self._failure_text_for)

    def _begin(self) -> None:
        self._worker._log(f"Downloading buildings (OpenFreeMap z14): "
                          f"{len(self.tiles):,} tiles for "
                          f"{_fmt_bbox(self.osm.bbox())}")
        self._worker.progress_model.set_activity(
            _BUILDINGS_SLOT, _BUILDINGS_ACTIVITY, reset=True)

    def _fetch_started(self, cached: int, todo: int) -> None:
        model = self._worker.progress_model
        model.set_work(_BUILDINGS_SLOT, todo * self._units)
        model.set_activity(_BUILDINGS_SLOT, _BUILDINGS_ACTIVITY,
                           {"done": 0, "total": todo, "unit": "tiles"},
                           reset=True)
        self._worker._emit_progress()

    def _fetch_progress(self, done: int, todo: int, nbytes: int,
                        mbps: float) -> None:
        w = self._worker
        w.progress_model.set_done(_BUILDINGS_SLOT, done * self._units)
        w.progress_model.set_activity(_BUILDINGS_SLOT, _BUILDINGS_ACTIVITY,
                                      {"done": done, "total": todo,
                                       "unit": "tiles", "mbps": mbps})
        now = time.monotonic()
        if now - self._emitted >= 0.2 or done == todo:
            self._emitted = now
            w._emit_progress()
            w.status.emit(f"Downloading buildings (OpenFreeMap z14): "
                          f"{done:,}/{todo:,} tiles")

    def _fetched(self, result: Any) -> None:
        w = self._worker
        w.progress_model.finish(_BUILDINGS_SLOT)
        w._emit_progress()
        extra = [f"{result.cached:,} from the cache"] if result.cached \
            else []
        if result.empty:
            extra.append(f"{result.empty:,} without data")
        w._log(f"Buildings ready: {len(result.present):,} tiles "
               f"({result.nbytes / 1e6:,.0f} MB) in "
               f"{time.monotonic() - self.started:.0f} s"
               + (f" ({', '.join(extra)})" if extra else ""))

    def _failed(self, cause: str) -> None:
        if self.error == cause:     # a batch's view or marker (own words)
            self._worker._log(f"ERROR: {cause}")
        else:
            self._worker._log(f"ERROR: Buildings download failed "
                              f"(OpenFreeMap z14): {cause}")
        self._worker._halt()

    def _ended(self) -> None:
        self._worker.progress_model.end_activity(_BUILDINGS_SLOT)

    @staticmethod
    def _failure_text_for(cause: str) -> str:
        return (f"Buildings download failed (OpenFreeMap z14): {cause}."
                f"\n\nThe run was stopped — terrain without the requested "
                f"buildings is not written. The tiles already downloaded "
                f"stay in the cache; run again to fetch the rest.")


def _merge_disk_items(*lists: List[Tuple[str, str, int]]
                      ) -> List[Tuple[str, str, int]]:
    """The disk need of phases that follow one another, per (label, dir):
    output, pool and raw source tiles add up; the window copies count once
    at the largest (each phase removes its own before the next makes new
    ones)."""
    out: Dict[Tuple[str, str], int] = {}
    for items in lists:
        for label, path, n in items:
            key = (label, path)
            out[key] = (max(out.get(key, 0), n) if label == _NEED_TEMP
                        else out.get(key, 0) + n)
    order = {_NEED_OUTPUT: 0, _NEED_POOL: 1, _NEED_RAW: 2, _NEED_TEMP: 3}
    return [(label, path, n) for (label, path), n in
            sorted(out.items(), key=lambda kv: order.get(kv[0][0], 4))]


def _what_is_written(phase2: bool, download: bool, rest: bool) -> str:
    """The refusal's "Not enough disk space for …" object at the check
    before Phase 2 and the concurrent download."""
    parts = []
    if phase2:
        parts.append("the tiles built from local files")
    if download:
        parts.append("the terrain download")
    if rest or not parts:
        parts.append("the remaining tiles")
    return (parts[0] if len(parts) == 1
            else ", ".join(parts[:-1]) + " and " + parts[-1])


def _plan_zooms(plan: List[Any]) -> str:
    """``"z12, z15"`` — the zooms of a download plan."""
    return ", ".join(f"z{z}" for z in sorted({z for _k, _e, z, _g in plan}))


class _MapConverterWorker(QThread):
    """Background worker — heavy I/O, terrain download, converter run.

    The main thread passes lightweight config plus the pre-resolved
    acquisition dicts (rendered servers were already exported per tile on
    the main thread); this worker handles:
    - Per-file source infos for local files/folders (CRS detection — never
      reprojection: the converter samples each source in its own CRS)
    - XYZ terrain download via the shared pool
      (terrain_adapter.ensure_pool_tiles — same cache as Site Analysis),
      started at once next to Phase 2, one branch per pool
      (:class:`_DownloadBranch`; the same XYZ layer twice in the stack is
      one pool): per latitude band (:func:`_band_height`, north to south)
      one job per zoom, finest first — the finest on the branch's fetch
      lane, the coarser ones on its derive lane beside the next band's
      finest (``WAVESHED_SERIAL_ZOOMS=1``: one lane); the second pass over
      Phase 2's holes is an extra unit of the pool's fetch lane after the
      unit it fetches now
    - The ingest QUEUE, in this thread: Phase 2 first, then a Phase-3
      batch per band as soon as every pool tile its tiles need is in place
      (and the tiles no download feeds, and the hole tiles once their
      second download has landed) — one converter ingest at a time, while
      the downloads go on (:meth:`_drain_queue`)
    - OSM buildings (Download Buildings… rows): the OpenFreeMap z14 tiles
      the pending tiles need, fetched in parallel next to the terrain
      download and the ingest queue, in batch order (:class:`_BuildingsBranch`,
      the Site Analysis store); each ingest batch carries ITS tiles as
      ``buildings_pbf_dir`` and waits only for those
    - Converting GDB/SHP/GPKG → FGB
    - Building the ingest job JSON (sources[] per tile, stack order)
    - Running the converter through the shared streaming runner

    Every engine process of a run — Phase 2's ingest, each band's
    download, the second download pass, each Phase-3 batch — registers
    through :meth:`_register_proc`; :meth:`cancel` kills them all.
    """

    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    log_line = pyqtSignal(str)
    finished_ok = pyqtSignal(str)
    finished_err = pyqtSignal(str)

    def __init__(self, layers, output_dir, out_res, resolved_sources,
                 parent=None, overwrite=False, progress_model=None,
                 keep_pool_tiles=False):
        super().__init__(parent)
        # The run's ONE progress model (core/run_progress.py). The tab hands
        # in the one its resolve/render phase already advanced; a caller
        # without one (torture runner, tests) gets a fresh model the worker
        # estimates itself.
        self.progress_model = progress_model or run_progress.RunProgress()
        # Deep-copy the layer config so the main thread can't mutate it.
        #
        # `resolved_sources` is resolved by the caller, on the main thread,
        # via `resolve_sources_with_progress`. Doing it here instead meant a
        # potentially multi-minute export ran inside a constructor on the GUI
        # thread — the plugin looked frozen and could not be cancelled.
        self._layers = [
            {
                "layer_type": e.layer_type,
                "source_path": e.source_path,
                "crs_authid": e.crs_authid,
                "native_res_m": e.native_res_m,
                "extent": dict(e.extent) if e.extent else None,
                "priority": e.priority,
                "crs_overrides": dict(getattr(e, "crs_overrides", {}) or {}),
                "decisions": dict(getattr(e, "decisions", {}) or {}),
                "overlap_priority": getattr(e, "overlap_priority", None),
                # qgis_layer can't be passed to another thread.
                "resolved_source": src,
            }
            for e, src in zip(layers, resolved_sources)
        ]
        self._output_dir = output_dir
        self._out_res = out_res
        self._overwrite = overwrite
        self._canceled = False
        # A concurrent branch failed: every other one is stopped like a
        # Stop, and the run ends with that branch's error.
        self._halted = False
        # Every engine process of the run (several at once while the
        # downloads run next to Phase 2); `_proc` is the latest one.
        self._procs: List[Any] = []
        self._procs_lock = threading.Lock()
        self._proc = None
        self._branches: List[_DownloadBranch] = []
        # The run's buildings (file and/or OpenFreeMap) and their fetch.
        self._buildings: Optional[_RunBuildings] = None
        self._bld_branch: Optional[_BuildingsBranch] = None
        # aether_converter >= 0.2.12 (CONTRACT item 48): a batch is launched
        # before its building tiles are in, with its view's marker fields;
        # the views handed out so (_marker_view) get them (_ready_fields).
        self._pbf_marker = False
        self._marker_views: set = set()
        # aether_converter >= 0.2.14 (CONTRACT changelog 51): no view at
        # all — the batch reads the store through its list (_listed_batch).
        self._pbf_list = False
        # What the converter said about them over every batch (one ingest
        # at a time, so one monitor), and the tiles this run recorded with
        # OpenFreeMap buildings (the end-of-run verdict may revoke them).
        self._burns = osm_buildings.BurnMonitor()
        self._osm_recorded: List[str] = []
        self._temp_tifs: List[str] = []
        # Phase-2 scratch dirs of this run (removed on every exit).
        self._scratch_dirs: List[str] = []
        # The ingest queue and what each Phase-3 tile still waits for — all
        # under `_sched` (the download threads complete units, this thread
        # takes batches): `_p3_left` the Phase-3 tiles no batch holds yet,
        # `_needs` per tile the download units (pool, unit key) it waits
        # for, `_waiting` the reverse.
        self._sched = threading.Condition()
        self._queue: "deque[_Batch]" = deque()
        self._p3_left: Dict[str, Tuple[int, Dict[str, Any]]] = {}
        self._p3_order: Dict[str, int] = {}
        self._needs: Dict[str, set] = {}
        self._waiting: Dict[Any, set] = {}
        self._unfed: List[str] = []
        self._batch_seq = 0
        self._band_pos: Dict[int, int] = {}
        self._ledger: Optional[_SlotLedger] = None
        # WAVESHED_SERIAL_ZOOMS=1: one download lane per pool, as before
        # 2026-10-25 (read once per run, in run()).
        self._serial_zooms = False
        # The run's start (time.perf_counter), the zero of its timing lines.
        self._t0 = time.perf_counter()
        # "Keep pool tiles" off (the default): the pool tiles THIS run
        # downloaded ({pool dir: names}, filled by the download threads)
        # are removed once the run has succeeded (_drop_run_pool_tiles).
        self._keep_pool_tiles = bool(keep_pool_tiles)
        self._pool_built: Dict[str, set] = {}
        self._pool_built_lock = threading.Lock()

    def cancel(self):
        """Stop: kill EVERY engine process of the run — Phase 2's ingest
        and each concurrent download alike (all registered through
        :meth:`_register_proc`). The worker then ends quietly."""
        self._canceled = True
        self._kill_all()

    def _halt(self) -> None:
        """A concurrent branch failed: stop all the others like Stop does."""
        self._halted = True
        self._kill_all()

    def _stopping(self) -> bool:
        """``should_cancel`` of every phase and branch: Stop, or a branch
        failed."""
        return self._canceled or self._halted

    def _kill_all(self) -> None:
        with self._procs_lock:
            procs = list(self._procs)
        _kill_procs(procs)

    @staticmethod
    def _parse_progress(line: str) -> Optional[Tuple[int, int]]:
        m = re.search(r"Progress:\s*(\d+)/(\d+)", line)
        return (int(m.group(1)), int(m.group(2))) if m else None

    def _log(self, msg):
        self.log_line.emit(msg)

    def _say(self, text: str, phase: Optional[str] = None,
             final: bool = False) -> None:
        """Status line (signal, unchanged texts) + the model's phase."""
        if final:
            self.progress_model.stop(text)
        else:
            self.progress_model.set_phase(phase or text)
        self.status.emit(text)

    def _emit_progress(self) -> None:
        self.progress.emit(self.progress_model.percent())

    def _download_progress(self, frac: float, label: str) -> None:
        """``progress_cb`` of every download call (concurrent branches and
        the second pass): the bar from the model, the downloader's label as
        the status."""
        if self._stopping():
            return
        self._emit_progress()
        self.status.emit(label)

    def _check_disk(self, items: List[Tuple[str, str, int]],
                    what: str) -> None:
        refusal = _disk_refusal(items, what)
        if refusal:
            self._log(f"ERROR: {refusal}")
            raise _RunFailed(refusal)

    # -- Downloads, band by band, one thread per pool ---------------------

    def _pool_units(self, plan: List[Any], height: float
                    ) -> Dict[str, Tuple[List[int], List[Any]]]:
        """``{pool: (entry indices, units)}`` of download *plan*: per pool
        its calls (:func:`_pool_calls`) split into latitude bands
        (:func:`_band_calls`), one unit ``(("band", b), calls)`` per band,
        north to south, each band's calls finest zoom first."""
        by_pool: Dict[str, List[Any]] = {}
        for item in plan:
            by_pool.setdefault(_plan_pool(item), []).append(item)
        out: Dict[str, Tuple[List[int], List[Any]]] = {}
        for pool, items in by_pool.items():
            entries = sorted({k for k, _e, _z, _g in items})
            out[pool] = (entries, [(("band", band), calls) for band, calls
                                   in _band_calls(_pool_calls(items), height)])
        return out

    def _start_downloads(self, units_by_pool: Dict[str, Any],
                         closed: bool) -> None:
        """One :class:`_DownloadBranch` per POOL, started now. XYZ entries
        that download into one pool (the same layer twice in the stack)
        share a branch — its calls serve them all (:func:`_pool_calls`);
        two branches would write the same pool tiles at once. *closed*: no
        unit will be added later (no Phase 2, so no hole pass)."""
        for pool, (entries, units) in units_by_pool.items():
            if len(entries) > 1:
                self._log(
                    f"  XYZ entries {', '.join(str(k + 1) for k in entries)} "
                    f"use one terrain pool (the same service) — one download "
                    f"serves them all")
            self._add_branch(entries[0], pool, units, closed)

    def _add_branch(self, k: int, pool: str, units: List[Any],
                    closed: bool) -> "_DownloadBranch":
        branch = _DownloadBranch(self, k, pool, units, closed=closed,
                                 serial=self._serial_zooms)
        with self._sched:
            first = not self._branches
            self._branches.append(branch)
        if first:
            # Which download mode runs (the A/B switch), once per run.
            self._log(_lanes_line(self._serial_zooms))
        branch.start()
        return branch

    def _unit_what(self, key: Tuple[Any, ...]) -> str:
        """A download unit's name — ``"band 2/3"``, ``"Phase-2 holes"`` —
        in its batch's label and in the timing lines."""
        if key[0] == "band":
            return (f"band {self._band_pos.get(key[1], 0)}/"
                    f"{len(self._band_pos)}")
        return "Phase-2 holes"

    def _download_unit(self, key: Tuple[Any, ...], calls: List[Any],
                       activity: Any,
                       on_call_end: Optional[Callable[[Any], None]] = None
                       ) -> None:
        """Run *calls* of one unit of a pool's branch (in one of its
        lanes): a band's calls into the banded slots (:class:`_SlotLedger`),
        or the hole pass's. *on_call_end* hears of each call as it ends."""
        common = dict(progress_cb=self._download_progress,
                      should_cancel=self._stopping,
                      on_start=self._register_proc,
                      progress_model=self.progress_model, activity=activity,
                      on_pool_tiles=self._note_pool_tiles,
                      on_call_end=on_call_end,
                      # Two lanes log into one panel: tag each pass's lines
                      # with its zoom (one lane: the lines as before).
                      lane_tag=not self._serial_zooms)
        if key[0] == "band":
            _run_pool_calls(
                calls, tag=f" band {self._band_pos.get(key[1], 0)}/"
                           f"{len(self._band_pos)}",
                ledger=self._ledger, band=key[1], **common)
        else:
            _run_pool_calls(calls, stage=2, tag=" (Phase-2 holes)", **common)

    def _note_pool_tiles(self, pool: str, names: List[str]) -> None:
        """``on_pool_tiles`` of every download call (its branch's thread):
        the pool tiles that call downloaded — THIS run built them."""
        if names:
            with self._pool_built_lock:
                self._pool_built.setdefault(pool, set()).update(names)

    def _drop_run_pool_tiles(self) -> None:
        """"Keep pool tiles" off (the default): after a SUCCESSFUL run, the
        pool tiles this run downloaded (:meth:`_note_pool_tiles`) leave the
        pool with their ``.src`` / ``.rebuild`` sidecars — the copy only pays
        off for a Site Analysis run from the same XYZ source and otherwise
        doubles the disk use. Never after Stop or a failure (the next run
        may reuse them); tiles that were in the pool before the run and the
        raw source-tile cache (``<cache>/xyz``, what makes reruns and
        derivation free) are never touched."""
        with self._pool_built_lock:
            built = {pool: sorted(names)
                     for pool, names in self._pool_built.items() if names}
            self._pool_built = {}
        if not built:
            return
        removed = freed = linked = 0
        for pool, names in sorted(built.items()):
            got = terrain_adapter.remove_pool_tiles(pool, names)
            removed += got.removed
            freed += got.freed_bytes
            linked += got.linked
        self._log(
            f"Removed {removed:,} pool tile(s) this run downloaded "
            f"({freed / 1024 ** 2:,.0f} MB freed"
            + (f"; {linked:,} of them are still linked from a terrain view"
               if linked else "")
            + ") — tick \"Keep pool tiles\" to keep them for Site Analysis "
              "runs from the same source; the downloaded source tiles stay "
              "cached")

    def _unit_done(self, pool: str, key: Tuple[Any, ...]) -> None:
        """A download unit of *pool* has landed — every call of it, on both
        lanes of its branch (called from the lane that finished it last):
        the pending Phase-3 tiles whose pool tiles are ALL in place now
        form a batch at the end of the ingest queue — the tiles that waited
        for this unit only (a tile under several pools waits for each
        pool's unit), and the first time also the tiles no download
        feeds."""
        tag = (pool, key)
        with self._sched:
            ready = []
            for name in self._waiting.pop(tag, ()):
                need = self._needs.get(name)
                if need is None:
                    continue
                need.discard(tag)
                if not need:
                    del self._needs[name]
                    rt = self._p3_left.pop(name, None)
                    if rt is not None:
                        ready.append(rt)
            for name in self._unfed:
                rt = self._p3_left.pop(name, None)
                if rt is not None:
                    ready.append(rt)
            self._unfed = []
            if ready:
                self._add_batch(ready, key)
            self._sched.notify_all()

    def _branch_ended(self, branch: "_DownloadBranch") -> None:
        with self._sched:
            branch.ended = True
            self._sched.notify_all()

    def _add_batch(self, tiles: List[Tuple[int, Dict[str, Any]]],
                   key: Optional[Tuple[Any, ...]]) -> None:
        """Queue a Phase-3 batch (caller holds ``_sched``); its tiles' work
        moves from :data:`_CONVERT_PENDING` into its own slot."""
        self._batch_seq += 1
        bid = self._batch_seq
        tiles = sorted(tiles, key=lambda rt: self._p3_order.get(
            rt[1]["filename"], 0))
        what = "" if key is None else self._unit_what(key)
        model = self.progress_model
        work = _convert_work([t for _r, t in tiles])
        model.set_work(_CONVERT_PENDING,
                       max(0.0, model.work(_CONVERT_PENDING) - work))
        model.set_work(("convert", bid), work)
        self._queue.append(_Batch(bid, tiles, what))
        self._sched.notify_all()

    def _init_phase3(self, pending: List[Tuple[int, Dict[str, Any]]],
                     phase3: List[Tuple[int, Dict[str, Any]]],
                     units_by_pool: Dict[str, Any]) -> None:
        """What each Phase-3 tile waits for: the band unit of every pool its
        XYZ entries download it into. The tiles no download feeds (no XYZ
        entry overlaps them) have every pool tile they need in place: they
        join the first band batch — or, when nothing downloads, are queued
        at once (the first batch after Phase 2)."""
        with self._sched:
            self._p3_order = {t["filename"]: i
                              for i, (_r, t) in enumerate(pending)}
            self._p3_left = {t["filename"]: (r, t) for r, t in phase3}
            for pool, (_entries, units) in units_by_pool.items():
                for key, calls in units:
                    tag = (pool, key)
                    for (_k, _e, _z, group), _members in calls:
                        for _r, t in group:
                            name = t["filename"]
                            self._needs.setdefault(name, set()).add(tag)
                            self._waiting.setdefault(tag, set()).add(name)
            self._unfed = [t["filename"] for _r, t in phase3
                           if not self._needs.get(t["filename"])]
            if self._unfed and not self._waiting:
                ready = [self._p3_left.pop(name) for name in self._unfed]
                self._unfed = []
                self._add_batch(ready, None)

    def _unfed_tiles(self) -> List[Tuple[int, Dict[str, Any]]]:
        """The Phase-3 tiles no download feeds (they ride with the first
        band batch)."""
        with self._sched:
            return [self._p3_left[n] for n in self._unfed
                    if n in self._p3_left]

    def _in_flight_downloads(self, download: List[Tuple[int, Dict[str, Any]]]
                             ) -> List[Tuple[int, Dict[str, Any]]]:
        """The tiles of *download* whose band unit has not landed yet."""
        with self._sched:
            waiting = set()
            for (_pool, key), names in self._waiting.items():
                if key[0] == "band":
                    waiting |= names
        return [rt for rt in download if rt[1]["filename"] in waiting]

    def _schedule_holes(self, queued: List[Tuple[int, Dict[str, Any]]],
                        xyz_entries: List[Dict[str, Any]],
                        all_entries: List[Dict[str, Any]],
                        download: List[Tuple[int, Dict[str, Any]]],
                        tmp_dir: str) -> None:
        """Phase 2 has spoken: the tiles it left with holes (*queued*) get a
        second, small download — ONE extra unit on each pool's branch, its
        finest call next on the fetch lane (after the call fetching now),
        its coarser ones on the derive lane after that (one lane: after the
        band it downloads now) — and then their own batch. Nothing is added
        after this, so every branch is closed.
        Disk is checked first: what is still to be written (the tiles no
        batch has converted yet, the pool tiles of the downloads still to
        land) — the refusal names the terrain download."""
        holes = _overlaps_xyz(all_entries, queued)
        plan_holes = _xyz_download_plan(xyz_entries, holes)
        model = self.progress_model
        model.add_work(_CONVERT_PENDING,
                       _convert_work([t for _r, t in queued]))
        if plan_holes:
            with self._sched:
                remaining = (list(self._p3_left.values())
                             + [rt for b in self._queue for rt in b.tiles])
            # The band downloads still under way write pool tiles too (the
            # ones already listed in the pool cost nothing).
            self._check_disk(
                _disk_items(remaining + list(queued), all_entries,
                            self._output_dir, tmp_dir,
                            download=(list(holes)
                                      + self._in_flight_downloads(download)),
                            should_cancel=self._stopping),
                "the terrain download and the remaining tiles")
            _estimate_download_work(model, xyz_entries, holes,
                                    pool_check=True,
                                    should_cancel=self._stopping, stage=2)
            self._emit_progress()
            self._say("Downloading terrain tiles...")
            self._log(f"Downloading terrain for the {len(holes)} tile(s) "
                      f"Phase 2 left with holes via the shared pool (after "
                      f"each pool's current band)...")
        by_pool: Dict[str, List[Any]] = {}
        for item in plan_holes:
            by_pool.setdefault(_plan_pool(item), []).append(item)
        units = {pool: (("holes",), sorted(_pool_calls(items),
                                           key=lambda c: -c[0][2]))
                 for pool, items in by_pool.items()}
        with self._sched:
            for res, tile in queued:
                self._p3_left[tile["filename"]] = (res, tile)
            for pool, (key, calls) in units.items():
                tag = (pool, key)
                for (_k, _e, _z, group), _members in calls:
                    for _r, t in group:
                        name = t["filename"]
                        self._needs.setdefault(name, set()).add(tag)
                        self._waiting.setdefault(tag, set()).add(name)
            ready = [rt for rt in queued
                     if not self._needs.get(rt[1]["filename"])]
            for _r, t in ready:
                self._p3_left.pop(t["filename"], None)
            if ready:
                self._add_batch(ready, ("holes",))
            branches = {b.pool: b for b in self._branches if not b.ended}
        for pool, unit in units.items():
            branch = branches.get(pool)
            if branch is not None:
                branch.push_front(unit)
            else:
                self._add_branch(min(k for k, _e, _z, _g in by_pool[pool]),
                                 pool, [unit], closed=True)
        for branch in branches.values():
            branch.close()

    # -- The ingest queue ----------------------------------------------------

    def _next_batch(self) -> Optional["_Batch"]:
        """The next batch of the ingest queue, waiting while a download that
        can still add one runs; None once the queue is empty and every
        download branch has ended. Stop or a failed branch →
        ``ConverterCancelled`` (the run then reports the branch's error)."""
        said = False
        while True:
            with self._sched:
                if self._stopping():
                    raise terrain_adapter.ConverterCancelled()
                if self._queue:
                    return self._queue.popleft()
                if all(b.ended for b in self._branches):
                    return None
                if said:
                    self._sched.wait(0.2)
                    continue
            said = True
            self._say("Downloading terrain tiles...")

    def _drain_queue(self, converter_exe: str,
                     all_entries: List[Dict[str, Any]],
                     tile_fps: Dict[str, Optional[str]],
                     tmp_dir: str) -> Tuple[int, int, bool]:
        """Run the ingest queue's Phase-3 batches one at a time (never two
        converter ingests at once) while the downloads go on, until it is
        empty and no download can add to it. ``(tiles converted, batches,
        every batch was NO-DATA)``."""
        n_tiles = n_batches = 0
        no_data = True
        while True:
            batch = self._next_batch()
            if batch is None:
                break
            result, n = self._run_batch(converter_exe, batch, all_entries,
                                        tile_fps, tmp_dir)
            n_tiles += n
            n_batches += 1
            no_data = no_data and bool(result["no_data"])
        return n_tiles, n_batches, no_data and n_batches > 0

    def _run_batch(self, converter_exe: str, batch: "_Batch",
                   all_entries: List[Dict[str, Any]],
                   tile_fps: Dict[str, Optional[str]],
                   tmp_dir: str) -> Tuple[Dict[str, Any], int]:
        """One Phase-3 batch: its building tiles (waits for THOSE only,
        :meth:`_pbf_batch` — on aether_converter >= 0.2.12 it does not wait:
        the converter does, for its view's marker; on >= 0.2.14 for its
        list, no view linked), the disk check for its
        own tiles and window copies, its jobs (whole stack, into the output
        dir — job for job the one-shot build), one
        converter ingest into slot ``("convert", id)`` (job file
        ``aether_conv_<pid>_<id>.json``, label "Converting band 2/3: a/b
        tiles"), its tiles' manifest records. Its window copies are removed
        when it ends."""
        tiles = batch.tiles
        note = f" ({batch.what})" if batch.what else ""
        started = time.perf_counter()
        # "building view" in the timing line: on >= 0.2.14 the list's
        # registration (nothing linked), on 0.2.12 the view's.
        pbf_dir, pbf_fields = self._pbf_batch(tiles)
        viewed = time.perf_counter()
        self._check_disk(
            _disk_items(tiles, all_entries, self._output_dir, tmp_dir,
                        should_cancel=self._stopping),
            f"the remaining tiles{note}")
        checked = time.perf_counter()
        try:
            jobs, temps = _build_tile_jobs(
                tiles, self._output_dir, True, all_entries, self._buildings,
                tmp_dir, should_cancel=self._stopping, pbf_dir=pbf_dir,
                pbf_ready=pbf_fields)
        except RuntimeError as exc:
            raise _RunFailed(str(exc))
        built = time.perf_counter()
        self._temp_tifs.extend(temps)
        slot = ("convert", batch.id)
        phase = (f"Converting {batch.what}" if batch.what
                 else "Converting tiles")

        def timing_line() -> None:
            # The batch's timing lane (logged right before its converter
            # starts, never parsed): when its band landed, when the queue
            # took it, and where the time until the launch went.
            t0 = self._t0
            self._log(
                f"  timing: {batch.what or f'batch {batch.id}'} ready at "
                f"+{batch.ready_t - t0:.1f} s, started at "
                f"+{started - t0:.1f} s (queue wait "
                f"{max(0.0, started - batch.ready_t):.1f} s), building view "
                f"{viewed - started:.1f} s, disk check {checked - viewed:.1f} "
                f"s, jobs {built - checked:.1f} s, launch prep "
                f"{time.perf_counter() - built:.1f} s")

        try:
            if self._stopping():
                raise terrain_adapter.ConverterCancelled()
            self._say(f"Converting {batch.what}: {len(jobs)} .abt tiles..."
                      if batch.what else
                      f"Converting {len(jobs)} .abt tiles...", phase)
            self._log(f"Phase 3{note}: running the converter "
                      f"({len(jobs)} tiles)...")
            try:
                result = self._run_ingest(converter_exe, jobs, slot, phase,
                                          activity=slot, batch=batch.id,
                                          before_launch=timing_line)
            except _BuildingsRunFailed:
                # Written into the output dir with buildings missing: a
                # record that is never a fingerprint, so the next run
                # rebuilds them ("no record" would keep them).
                self._record_unverified(
                    [os.path.basename(j["output_path"]) for j in jobs
                     if os.path.exists(j["output_path"])])
                raise
            # Record what each built tile was made from (whole tiles only —
            # a short one has no valid record and is rebuilt next time) and,
            # the batch's buildings report being clean, what it carries.
            by_name = {t["filename"]: (r, t) for r, t in tiles}
            built: Dict[str, Optional[str]] = {}
            records: Dict[str, Optional[str]] = {}
            for job in jobs:
                name = os.path.basename(job["output_path"])
                whole = terrain_adapter._tile_is_whole(job["output_path"])
                built[name] = tile_fps.get(name) if whole else None
                if name in by_name:
                    records[name] = _buildings_record(self._buildings,
                                                      *by_name[name])
            self._record_built(built, records)
        finally:
            for path in temps:
                try:
                    os.remove(path)
                except OSError:
                    pass
            gone = set(temps)
            self._temp_tifs = [t for t in self._temp_tifs if t not in gone]
        self._emit_progress()
        return result, len(jobs)

    def _record_built(self, fps: Dict[str, Optional[str]],
                      records: Dict[str, Optional[str]]) -> None:
        """Manifest records of tiles a batch built — their fingerprints and
        (schema 2) buildings records, written after the batch's converter
        report was checked; the ones carrying OpenFreeMap buildings are
        remembered for the end-of-run verdict (:meth:`_judge_burns`)."""
        _write_output_manifest(self._output_dir, fps, records)
        self._osm_recorded.extend(
            name for name, rec in records.items()
            if fps.get(name) and rec
            and terrain_adapter.BUILDINGS_OSM in rec.split("+"))

    def _record_unverified(self, names: List[str]) -> None:
        """Tiles written while their buildings could not be verified: a
        fingerprint record that is never a fingerprint (the next run
        rebuilds them) and the buildings record ``unverified``."""
        if names:
            _write_output_manifest(
                self._output_dir,
                {n: _BUILDINGS_UNVERIFIED for n in names},
                {n: terrain_adapter.BUILDINGS_UNVERIFIED for n in names})

    def _judge_burns(self, expected: List[Tuple[int, Dict[str, Any]]],
                     built: List[Tuple[int, Dict[str, Any]]]) -> None:
        """The run-level zero-drawn rule (``BurnMonitor.nothing_drawn``):
        OpenFreeMap buildings decoded over the run's batches but none drawn
        → the run fails and this run's OpenFreeMap tiles are recorded
        unverified (rebuilt next run). One batch at the edge of the
        buildings legitimately draws none, so only the whole run is judged
        — and only when it built EVERY output tile the buildings reach: a
        run that rebuilt one edge tile next to tiles an earlier run drew
        the buildings on is no evidence (logged, not judged). Every report
        "no buildings in extent" is a warning: the fetched tiles hold no
        building on this terrain."""
        burns = self._burns
        osm = self._buildings.osm if self._buildings is not None else None
        if osm is None or not burns.reports:
            return
        if burns.empty:
            self._log(f"  WARNING: buildings: the {burns.pbf_tiles:,} "
                      f"OpenFreeMap tile(s) handed to the converter hold no "
                      f"building (\"no buildings in extent\") — the tiles "
                      f"were converted without any")
            return
        problem = burns.nothing_drawn()
        if problem is None:
            return
        reached = {t["filename"] for r, t in expected if osm.members(r, t)}
        left = reached - {t["filename"] for _r, t in built}
        if left:
            self._log(f"  buildings: none drawn on this run's tiles — not "
                      f"judged: {len(left)} tile(s) the buildings reach "
                      f"were built by an earlier run (first: "
                      f"{sorted(left)[0]})")
            return
        self._record_unverified(sorted(set(self._osm_recorded)))
        msg = (f"{problem} Move the Download Buildings… row's extent over "
               f"buildings on the terrain, or remove the row.")
        self._log(f"ERROR: {msg}")
        raise _RunFailed(msg)

    def _all_branches(self) -> List[Any]:
        """Every concurrent branch: the downloads and the building fetch."""
        with self._sched:
            branches = list(self._branches)
        return branches + ([self._bld_branch]
                           if self._bld_branch is not None else [])

    def _branch_error(self) -> Optional[str]:
        return next((b.error for b in self._all_branches() if b.error), None)

    def _join_downloads(self) -> None:
        """Wait for every download branch. A failed one ends the run with
        its error (it stopped the others already); a stopped one is a
        Cancel."""
        with self._sched:
            branches = list(self._branches)
        for branch in branches:
            branch.join()
        err = self._branch_error()
        if err is not None:
            raise _RunFailed(err)
        if self._stopping() or any(b.cancelled for b in branches):
            raise terrain_adapter.ConverterCancelled()

    def _stop_branches(self) -> None:
        """No branch outlives the run: stop and join whatever still runs."""
        branches = self._all_branches()
        if any(b.alive() for b in branches):
            self._halt()
        for branch in branches:
            branch.join()

    def _start_buildings(self, groups: List[List[Tuple[int, Dict[str, Any]]]],
                         converter_exe: Optional[str] = None) -> None:
        """Start the OpenFreeMap fetch of the z14 tiles the run's batches
        need, in the order the batches come (*groups* of output tiles:
        Phase 2's, the tiles no download feeds, then each band north to
        south) — before the terrain download starts; it runs next to that
        download and the ingest queue. With *converter_exe* >= 0.2.12
        (:func:`_converter_waits_for_buildings`) no batch waits for it: each
        is launched at once and waits for its view's marker
        (:meth:`_marker_view`) — on >= 0.2.14
        (:func:`_converter_lists_buildings`) for its list, no view linked
        (:meth:`_listed_batch`)."""
        osm = self._buildings.osm if self._buildings is not None else None
        if osm is None:
            return
        tile_groups = [osm.needed(group) for group in groups]
        if not any(tile_groups):
            self._log("  buildings (OpenFreeMap z14): none of the tiles to "
                      "build lies in the buildings extent — nothing to "
                      "download")
            self.progress_model.set_work(_BUILDINGS_SLOT, 0)
            return
        self._pbf_marker = _converter_waits_for_buildings(converter_exe)
        self._pbf_list = (self._pbf_marker
                          and _converter_lists_buildings(converter_exe))
        if self._pbf_list:
            self._log(f"  buildings: every batch is launched at once — "
                      f"aether_converter >= 0.2.14 waits for the list of its "
                      f"building tiles and reads them from "
                      f"{terrain_adapter.osm_buildings_store_dir()}")
        elif self._pbf_marker:
            self._log(f"  buildings: every batch is launched at once — "
                      f"aether_converter >= 0.2.12 waits for its building "
                      f"tiles ({osm_buildings.READY_MARKER} in its view)")
        self._bld_branch = _BuildingsBranch(self, osm, tile_groups)
        self._bld_branch.start()

    def _await_buildings(self) -> None:
        """Join the WHOLE building fetch (the end of the run: its verdict —
        e.g. no tile with data at all — is the run's). Its failure ends the
        run with its error (it stopped the other branches); a stop is a
        Cancel. A batch waits only for its own tiles (:meth:`_pbf_view`)."""
        branch = self._bld_branch
        if branch is None:
            return
        branch.join()
        if branch.error is not None:
            raise _RunFailed(branch.error)
        if branch.cancelled or self._stopping():
            raise terrain_adapter.ConverterCancelled()

    def _pbf_batch(self, tiles: List[Tuple[int, Dict[str, Any]]]
                   ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """``(buildings_pbf_dir, the extra fields of its jobs)`` of one
        ingest batch over *tiles* — what :func:`_build_tile_jobs` gets as
        *pbf_dir* / *pbf_ready*. On aether_converter >= 0.2.14
        (:attr:`_pbf_list`, CONTRACT changelog 51): THE store and
        ``buildings_pbf_list`` + ``buildings_pbf_wait_s``
        (:meth:`_listed_batch`; nothing linked, the batch does not wait);
        a list an earlier run left that cannot be removed → as below. Else
        :meth:`_pbf_view` and its :meth:`_ready_fields`."""
        if self._pbf_list:
            osm = (self._buildings.osm if self._buildings is not None
                   else None)
            branch = self._bld_branch
            if osm is None or branch is None:
                return None, None
            needed = {openfreemap.tile_filename(z, x, y)
                      for z, x, y in osm.needed(tiles)}
            if not needed:
                return None, None
            listed = self._listed_batch(osm, branch, tiles, needed)
            if listed is not None:
                return (terrain_adapter.osm_buildings_store_dir(),
                        {"buildings_pbf_list": os.path.abspath(listed),
                         "buildings_pbf_wait_s": osm_buildings.READY_WAIT_S})
        view = self._pbf_view(tiles)
        return view, self._ready_fields(view)

    def _pbf_view(self, tiles: List[Tuple[int, Dict[str, Any]]]
                  ) -> Optional[str]:
        """The ``buildings_pbf_dir`` of one ingest batch over *tiles*: a
        view holding exactly the fetched tiles they need (the converter
        decodes the whole directory once per batch). Waits until THOSE
        tiles are in the store (:meth:`_BuildingsBranch.wait_for`) — the
        fetch goes on with the next batches' tiles; its failure or a stop
        ends the wait (and the run). None without OpenFreeMap rows or when
        no fetched tile reaches *tiles*. On aether_converter >= 0.2.12
        (:attr:`_pbf_marker`) it does not wait: :meth:`_marker_view`."""
        osm = self._buildings.osm if self._buildings is not None else None
        branch = self._bld_branch
        if osm is None or branch is None:
            return None
        needed = {openfreemap.tile_filename(z, x, y)
                  for z, x, y in osm.needed(tiles)}
        if not needed:
            return None
        if self._pbf_marker:
            view = self._marker_view(osm, branch, tiles, needed)
            if view is not None:
                return view
        if not branch.ready_for(needed):
            self._log("  waiting for the building tiles (OpenFreeMap z14) "
                      "before converting...")
            self.status.emit("Waiting for the building tiles...")
        branch.wait_for(needed, self._stopping)
        if branch.error is not None:
            raise _RunFailed(branch.error)
        if branch.cancelled or self._stopping():
            raise terrain_adapter.ConverterCancelled()
        names = osm.view_names(tiles)
        if not names:
            return None
        try:
            view = osm_buildings.batch_view(names)
        except (RuntimeError, OSError) as exc:
            msg = f"Buildings: {exc}"
            self._log(f"ERROR: {msg}")
            raise _RunFailed(msg)
        self._log(f"  buildings: {len(names):,} OpenFreeMap tile(s) for "
                  f"{len(tiles)} output tile(s) → {view}")
        return view

    def _marker_view(self, osm: _OsmBuildings, branch: "_BuildingsBranch",
                     tiles: List[Tuple[int, Dict[str, Any]]],
                     needed: set) -> Optional[str]:
        """The batch's view WITHOUT waiting (aether_converter >= 0.2.12,
        toolkit CONTRACT item 48): named now from the tiles it NEEDS, a
        marker an earlier batch or run left removed
        (``osm_buildings.ready_view``); the fetch thread links the batch's
        tiles with data into it and THEN writes the marker as soon as they
        are in — at once, here, when they are in already
        (:meth:`_BuildingsBranch.link_when_ready`). The batch is launched
        meanwhile with the marker fields (:meth:`_ready_fields`); its
        converter waits for the marker at its burn. A fetch failure halts
        the run (the waiting converter is killed with the others). None
        when the old marker cannot be removed: the batch waits instead."""
        names = sorted(needed)
        view = osm_buildings.ready_view(names)
        if view is None:
            stale = os.path.join(terrain_adapter.osm_buildings_view_dir(
                names, marker=osm_buildings.READY_MARKER),
                osm_buildings.READY_MARKER)
            self._log(f"  WARNING: buildings: {stale} (left by an earlier "
                      f"run) could not be removed — this batch waits for "
                      f"its building tiles instead")
            return None
        self._log(f"  buildings: {len(names):,} OpenFreeMap tile(s) for "
                  f"{len(tiles)} output tile(s) → {view} — converted now, "
                  f"the converter waits for {osm_buildings.READY_MARKER} "
                  f"there")

        def linked(done_view: str, n: int) -> None:
            self._log(f"  buildings: {n:,} OpenFreeMap tile(s) linked into "
                      f"{done_view}, {osm_buildings.READY_MARKER} written — "
                      f"the converter burns them now")

        try:
            branch.link_when_ready(needed, view,
                                   lambda: osm.view_names(tiles),
                                   on_linked=linked)
        except (RuntimeError, OSError) as exc:
            msg = str(exc)
            self._log(f"ERROR: {msg}")
            raise _RunFailed(msg)
        if branch.error is not None:
            raise _RunFailed(branch.error)
        if branch.cancelled or self._stopping():
            raise terrain_adapter.ConverterCancelled()
        self._marker_views.add(view)
        return view

    def _listed_batch(self, osm: _OsmBuildings, branch: "_BuildingsBranch",
                      tiles: List[Tuple[int, Dict[str, Any]]],
                      needed: set) -> Optional[str]:
        """The batch's LIST without waiting (aether_converter >= 0.2.14,
        toolkit CONTRACT changelog 51) — the list mode of
        :meth:`_marker_view`: named now from the tiles it NEEDS, a list an
        earlier batch or run left removed (``osm_buildings.ready_list``);
        the fetch thread writes it — atomically, exactly the batch's tiles
        with data — as soon as they are in, at once, here, when they are
        in already (:meth:`_BuildingsBranch.list_when_ready`). Nothing is
        linked: the converter reads the listed tiles straight from the
        store, and waits for the list at its burn. A fetch failure halts
        the run (the waiting converter is killed with the others); a list
        that cannot be written there is the fetch's error. None when the
        old list cannot be removed: the batch takes the view instead."""
        names = sorted(needed)
        path = osm_buildings.ready_list(names)
        if path is None:
            self._log(f"  WARNING: buildings: "
                      f"{terrain_adapter.osm_buildings_list_path(names)} "
                      f"(left by an earlier run) could not be removed — this "
                      f"batch's building tiles are linked into a view "
                      f"instead")
            return None
        self._log(f"  buildings: {len(names):,} OpenFreeMap tile(s) for "
                  f"{len(tiles)} output tile(s) — converted now, the "
                  f"converter waits for their list {path}")

        def listed(done_path: str, n: int) -> None:
            self._log(f"  buildings: {n:,} OpenFreeMap tile(s) listed for "
                      f"the converter in {done_path} — the converter burns "
                      f"them now")

        try:
            branch.list_when_ready(needed, path,
                                   lambda: osm.view_names(tiles),
                                   on_listed=listed)
        except (RuntimeError, OSError) as exc:
            msg = str(exc)
            self._log(f"ERROR: {msg}")
            raise _RunFailed(msg)
        if branch.error is not None:
            raise _RunFailed(branch.error)
        if branch.cancelled or self._stopping():
            raise terrain_adapter.ConverterCancelled()
        return path

    def _ready_fields(self, view: Optional[str]) -> Optional[Dict[str, Any]]:
        """The marker fields of the jobs carrying *view* when it is a view
        handed out by :meth:`_marker_view` (``buildings_pbf_ready`` /
        ``buildings_pbf_wait_s``), else None (the jobs as before)."""
        if view is None or view not in self._marker_views:
            return None
        return {"buildings_pbf_ready": osm_buildings.READY_MARKER,
                "buildings_pbf_wait_s": osm_buildings.READY_WAIT_S}

    def _undecodable_buildings(self, bad: List[Tuple[str, str]]
                               ) -> "_BuildingsRunFailed":
        """The run's error for building tiles the converter could not
        decode; they leave the cache (store and views) so the next run
        fetches them again."""
        msg = osm_buildings.drop_undecodable(bad)
        self._log(f"ERROR: {msg}")
        return _BuildingsRunFailed(msg)

    def run(self):
        self._t0 = time.perf_counter()
        # The A/B switch of the download lanes, read once per run.
        self._serial_zooms = _serial_zooms_requested()
        # Once per run: QGIS's own resident memory, beside the converters'
        # peak lines (owner's run 2026-11-07: 31 GB, the converters ~3 GB
        # each, ~20 GB attributed to nothing).
        mem_line = process_memory.memory_line()
        if mem_line:
            self._log(mem_line)
        try:
            # Every folder index is re-checked once per run (stat only for
            # unchanged files), so a file changed since the last run is seen.
            terrain_adapter.terrain_index.expire_all()
            # ---- Phase 1: Resolve ALL layers in parallel ----
            self._say("Preparing layers...")
            self._log("Phase 1: Resolving layers (parallel)...")
            self._emit_progress()

            raster_entries = [e for e in self._layers
                              if e["layer_type"] != "buildings"]
            # Download Buildings… rows are not resolved here: their
            # OpenFreeMap tiles are fetched once the tile list is known,
            # next to the terrain download (_start_buildings) — Phase 1
            # used to wait 107 s for an Overpass answer that came back
            # empty. Only building FILES are converted here.
            building_entries = [e for e in self._layers
                                if e["layer_type"] == "buildings"
                                and not _is_osm_row(e)]
            osm_rows = [e for e in self._layers if _is_osm_row(e)]
            problem = _osm_rows_problem(self._layers)
            if problem:
                self._log(f"ERROR: {problem}")
                self.finished_err.emit(problem)
                return
            if len(building_entries) > 1:
                # The converter burns ONE buildings_file per job; picking
                # one of several rows silently dropped the others.
                msg = (f"The stack has {len(building_entries)} building "
                       f"file rows; a run burns one building file (or one "
                       f"folder of .fgb parts). Merge them into one folder "
                       f"or remove all but one.")
                self._log(f"ERROR: {msg}")
                self.finished_err.emit(msg)
                return

            from concurrent.futures import ThreadPoolExecutor, as_completed

            buildings_path = None
            total_tasks = len(raster_entries) + len(building_entries)
            done_tasks = 0

            with ThreadPoolExecutor(max_workers=4) as pool:
                # Submit all layer resolutions in parallel.
                raster_futures = {
                    pool.submit(self._resolve_raster, e): i
                    for i, e in enumerate(raster_entries)
                }
                building_futures = {
                    pool.submit(self._resolve_buildings, e): i
                    for i, e in enumerate(building_entries)
                }

                # Collect raster results (maintain order by sorting later).
                raster_results = {}
                for fut in as_completed(raster_futures):
                    if self._canceled:
                        return
                    idx = raster_futures[fut]
                    try:
                        raster_results[idx] = fut.result()
                    except Exception as exc:
                        # Hard error, not warn-and-continue: silently dropping
                        # a stack entry converts the wrong terrain.
                        self._log(f"  ERROR resolving raster layer {idx}: {exc}")
                        self.finished_err.emit(
                            f"Layer {idx + 1} could not be prepared: {exc}")
                        return
                    done_tasks += 1
                    self._say(f"Resolved layer {done_tasks}/{total_tasks}",
                              "Preparing layers")

                # Collect building results.
                for fut in as_completed(building_futures):
                    if self._canceled:
                        return
                    try:
                        bp = fut.result()
                        if bp:
                            buildings_path = bp
                    except Exception as exc:
                        # Hard error, same rule as rasters: running on
                        # WITHOUT the requested buildings and reporting
                        # success is a silently wrong result.
                        self._log(f"  ERROR resolving buildings: {exc}")
                        self.finished_err.emit(
                            f"Buildings could not be prepared: {exc}")
                        return
                    done_tasks += 1

            # Reassemble resolved entries in UI stack order: entry 0 sits on
            # top of the stack and is therefore the HIGHEST priority —
            # sources[] array order is priority order (first valid sample
            # wins), so this ordered list goes straight into every tile job.
            all_entries: List[Dict[str, Any]] = []
            for i in sorted(raster_results.keys()):
                all_entries.append(raster_results[i])

            if not all_entries:
                self.finished_err.emit("No raster layers resolved to files.")
                return

            # Rendered temp tifs belong to this run — clean them up with the
            # materialized scratch files when the run ends.
            for ent in all_entries:
                if ent.get("kind") == "rendered":
                    self._temp_tifs.extend(ent.get("tiles", {}).values())

            self._log(f"  Stack: {len(all_entries)} raster entr(ies)")
            if buildings_path:
                self._log(f"  Buildings: {buildings_path}")
            osm = (_OsmBuildings([e["extent"] for e in osm_rows])
                   if osm_rows else None)
            if osm is not None:
                self._log(f"  Buildings: OpenFreeMap z14 over "
                          f"{_fmt_bbox(osm.bbox())} ({len(osm.cover):,} "
                          f"tiles) — fetched next to the terrain download")
            self._buildings = (_RunBuildings(buildings_path, osm)
                               if buildings_path or osm is not None
                               else None)

            # ---- Tile job list ----
            if self._canceled:
                return
            self._say("Building tile list...")
            self._log("Building the .abt tile list...")

            combined = _union_extent(self._layers)
            if combined is None:
                self.finished_err.emit("No layer has an extent set.")
                return

            # The converter first: from 0.2.9 its `plan` snaps with an
            # epsilon (_snap_bbox), and the grid below must be ITS grid.
            try:
                converter_exe = find_binary("aether_converter")
                no_converter = None
            except RuntimeError as exc:
                converter_exe, no_converter = None, exc
            epsilon = (converter_exe is not None
                       and _converter_snaps_with_epsilon(converter_exe))

            # Snap to grid, then enumerate — the same two calls the pre-run
            # estimate makes, so the tile count it showed is the one built
            # (with a converter >= 0.2.9 its epsilon and rounded edge can only
            # drop a row or column the estimate counted wholly outside the
            # area).
            bbox = _snap_bbox(combined, self._out_res, epsilon=epsilon)
            expected = _enumerate_tiles(bbox, self._out_res, epsilon=epsilon)
            if no_converter is not None:
                self.finished_err.emit(str(no_converter))
                return

            # Pre-run cross-check: the engine must enumerate the SAME grid.
            # Fails loudly — a mismatch (or an engine without `plan`) aborts
            # before anything is written.
            #
            # Pass the RAW combined bbox, never the snapped one: the engine
            # snaps once itself, and floor/ceil on this float grid is NOT
            # idempotent (e.g. ceil(47.400000000000006/0.1) = 475 — 516 of
            # 3601 0.1-grid values move a step when re-snapped), so handing
            # it an already-snapped bbox double-snaps on its side and the
            # check falsely aborts. Both sides snapping exactly once from
            # bit-identical input is what makes the equality meaningful.
            self._say("Cross-checking tile grid with the engine...")
            try:
                _plan_cross_check(converter_exe, combined, self._out_res,
                                  expected)
            except RuntimeError as exc:
                self.finished_err.emit(str(exc))
                return

            # Tiles outside every source (owner, 2026-11-07): the coarse
            # grids snap outward on their own lines and can reach whole
            # tiles past every layer's extent — fed by nothing, they were
            # converted empty. Dropped AFTER the cross-check (the plan keeps
            # the full grid), before the fingerprints, the stale check and
            # the partition; the pre-run estimate drops the same tiles.
            expected, outside = _tiles_within_sources(expected, self._layers)
            if outside:
                self._log(_skipped_tiles_line(outside))

            # ONE footprint index per folder/file entry for the whole run:
            # each tile's files are matched once — here, for the stale check
            # — and the XYZ cover check and the job build reuse that match.
            # (A full scan tested every file against every tile, twice:
            # 43,650 files x 2,610 tiles took ~190 s.)
            n_files = 0
            for ent in all_entries:
                if ent.get("kind") == "files":
                    _files_index(ent)
                    n_files += len(ent.get("infos") or ())
            if n_files:
                self._say(f"Matching {n_files:,} files to "
                          f"{len(expected):,} tiles...", "Matching files")

            # Tiles whose inputs changed since they were built: rebuilt even
            # without "Rebuild existing tiles" (a changed or added file in a
            # folder used to leave the old pixels in place, silently). A tile
            # the files completed in Phase 2 carries its files-only
            # fingerprint and stays valid while those files are unchanged.
            cancelled = self._stopping
            files_fps: Dict[str, Optional[str]] = {}
            tile_fps = _stack_tile_fingerprints(
                expected, all_entries, self._buildings,
                should_cancel=cancelled, files_only=files_fps)
            pending, rebuild, unknown = _tiles_to_build(
                expected, self._output_dir, self._overwrite, tile_fps,
                files_fps)
            if rebuild:
                self._log(f"  {len(rebuild)} existing tile(s) are rebuilt — "
                          f"the files or buildings that feed them changed "
                          f"since they were built (first: "
                          f"{sorted(rebuild)[0]})")
            if unknown:
                self._log(f"  {unknown} existing tile(s) have no record of "
                          f"their sources (built by an older version) — kept; "
                          f"tick \"Rebuild existing tiles\" to refresh them")

            if not pending:
                self.progress_model.finish_all()
                self._emit_progress()
                self._say("All tiles already exist.", final=True)
                self.finished_ok.emit(self._output_dir)
                return

            # ---- Partition, then everything that can run at once. PROVEN
            # tiles (the files above the first XYZ entry cover them plus
            # their halo by rectangles) are converted from those files ALONE
            # (Phase 2); the converter's [Tile] coverage line says which came
            # out complete — final, no pool tile fetched for them. Every
            # other tile an XYZ entry overlaps needs the pool: its download
            # starts NOW, one thread per pool (= XYZ service), latitude band
            # by band (north to south, one job per zoom, finest first), while
            # Phase 2 converts; a band's tiles are converted with the whole
            # stack (Phase 3) as soon as their pool tiles are in place — one
            # ingest at a time, while the next band downloads. The holes
            # Phase 2 finds get a second, small download and their own
            # batch. (The owner's swissALTI3D + terrarium run: 17 min of
            # Phase 2 before the first byte was fetched, and 389 of its 920
            # tiles needed the pool anyway; then nothing was converted
            # until the last zoom of the whole area was down.)
            xyz_entries = [e for e in all_entries if e.get("kind") == "xyz"]
            proven, download = _partition_pending(pending, all_entries,
                                                  should_cancel=cancelled)
            if proven and not _converter_reports_tiles(converter_exe):
                self._log(
                    f"  Phase 2 skipped: this aether_converter prints no "
                    f"per-tile coverage (needs {_TILE_LINE_MIN_VERSION}) — "
                    f"the pool is downloaded for every tile")
                proven, download = [], _overlaps_xyz(all_entries, pending)
            in_phase2 = {t["filename"] for _r, t in proven}
            phase3 = [(r, t) for r, t in pending
                      if t["filename"] not in in_phase2]
            plan_now = _xyz_download_plan(xyz_entries, download)
            height = _band_height(self._out_res)
            units_by_pool = self._pool_units(plan_now, height)
            bands = sorted({key[1] for _e, units in units_by_pool.values()
                            for key, _c in units}, reverse=True)
            self._band_pos = {b: i for i, b in enumerate(bands, start=1)}
            if proven or plan_now:
                self._log(f"  {len(proven)} tile(s) covered by the local "
                          f"files → Phase 2; {len(download)} tile(s) need "
                          f"the pool → downloaded now")
            _reset_download_work(self.progress_model)
            self._ledger = _SlotLedger(self.progress_model)
            _estimate_download_work(self.progress_model, xyz_entries,
                                    download, pool_check=True,
                                    should_cancel=cancelled, height=height,
                                    ledger=self._ledger)
            self.progress_model.set_work(
                _CONVERT_FILES, _convert_work([t for _r, t in proven]))
            self.progress_model.set_work(
                _CONVERT_PENDING, _convert_work([t for _r, t in phase3]))
            self._emit_progress()

            os.makedirs(self._output_dir, exist_ok=True)
            tmp_dir = tempfile.gettempdir()
            # Disk, before anything is written: Phase 2's tiles and copies,
            # the pool tiles downloaded next to them, and the other tiles.
            self._check_disk(
                _merge_disk_items(
                    _disk_items(proven, _phase2_entries(all_entries),
                                self._output_dir, tmp_dir,
                                should_cancel=cancelled),
                    _disk_items(phase3, all_entries, self._output_dir,
                                tmp_dir, download=download,
                                should_cancel=cancelled)),
                _what_is_written(bool(proven), bool(plan_now), bool(phase3)))

            # What each Phase-3 tile waits for (its pools' band units); the
            # tiles no download feeds ride with the first band batch (with
            # nothing to download: the first batch after Phase 2).
            self._init_phase3(pending, phase3, units_by_pool)

            # The building tiles start downloading NOW, in their own thread,
            # in the order the batches come: neither the terrain download nor
            # Phase 2 waits for them — a batch's job build waits for ITS
            # tiles only (_pbf_batch); on a converter >= 0.2.12 not even that
            # (the converter waits for its view's marker; >= 0.2.14 for its
            # list, no view linked).
            with self._sched:
                first = [rt for b in self._queue for rt in b.tiles]
            band_groups = [[rt for rt in download
                            if _band_of(rt[0], rt[1], height) == b]
                           for b in bands]
            if band_groups:
                band_groups[0] = self._unfed_tiles() + band_groups[0]
            self._start_buildings([proven, first] + band_groups,
                                  converter_exe)

            # ---- Concurrent: the downloads start, Phase 2 converts ----
            if plan_now:
                zooms = sorted({z for _k, _e, z, _g in plan_now},
                               reverse=True)
                self._log(
                    f"Downloading terrain for {len(download)} tile(s) from "
                    f"{len({k for k, _e, _z, _g in plan_now})} XYZ entr(ies) "
                    f"via the shared pool: {len(bands)} band(s) × "
                    + ", ".join(f"z{z}" for z in zooms)
                    + f" — per pool one job per band and zoom, finest zoom "
                      f"first, the {height:g}° bands north to south"
                    + (", next to Phase 2" if proven else ""))
                if not self._keep_pool_tiles:
                    self._log("  the pool tiles this run downloads are "
                              "removed once it has succeeded (\"Keep pool "
                              "tiles\" is off)")
                if not proven:
                    self._say("Downloading terrain tiles...")
                self._start_downloads(units_by_pool, closed=not proven)
            from_files = 0
            if proven:
                note = (f"; downloading terrain for {len(download)} tile(s) "
                        f"({_plan_zooms(plan_now)})" if plan_now else "...")
                queued = self._run_phase2(
                    converter_exe, proven, all_entries, self._buildings,
                    files_fps, tmp_dir, note=note, concurrent=bool(plan_now))
                from_files = len(proven) - len(queued)
                self._schedule_holes(queued, xyz_entries, all_entries,
                                     download, tmp_dir)

            # ---- Phase 3: the ingest queue, batch by batch, while the
            # downloads go on; the run ends when it is drained and every
            # branch has ended.
            n3, n_batches, no_data = self._drain_queue(
                converter_exe, all_entries, tile_fps, tmp_dir)
            self._join_downloads()
            self._await_buildings()
            self._judge_burns(expected, pending)
            with self._sched:
                left = sorted(self._p3_left)
            if left:
                msg = (f"Internal error: {len(left)} tile(s) were never "
                       f"converted (first: {left[0]}) — please report this.")
                self._log(f"ERROR: {msg}")
                raise _RunFailed(msg)

            self.progress_model.finish_all()
            self._emit_progress()
            self._say("Done.", final=True)
            if n_batches:
                self._log(f"Phase 3: {n3} tile(s) converted with the whole "
                          f"stack in {n_batches} batch(es).")
            if no_data and not from_files:
                self._log(
                    "WARNING: no source covered any pixel of this area — "
                    "every output tile is no-data (VOID). The Map Converter "
                    "keeps voids by contract; Site Analysis reads such "
                    "ground as 0 m (sea level). Check the sources' extent "
                    "and CRS if you expected terrain here.")
            self._log(f"Conversion complete: {n3 + from_files} tiles.")
            if not self._keep_pool_tiles:
                self._drop_run_pool_tiles()
            self.finished_ok.emit(self._output_dir)

        except _RunFailed as exc:
            # A phase failed: every concurrent branch is stopped first, then
            # the run ends with that error.
            self._stop_branches()
            self.finished_err.emit(str(exc))
        except terrain_adapter.ConverterCancelled:
            # Stop (or a per-tile loop saw it), or a concurrent branch
            # failed and stopped this one: once every branch has ended, the
            # branch's error — else the same quiet end as a cancelled run.
            self._stop_branches()
            err = self._branch_error()
            if err is not None:
                self.finished_err.emit(err)
            else:
                self._log("Cancelled by user.")
                self._say("Cancelled.", final=True)
        except Exception as exc:
            self._stop_branches()
            self._log(f"ERROR: {exc}")
            self.finished_err.emit(f"Unexpected error: {exc}")
        finally:
            self._stop_branches()
            self._proc = None
            self._cleanup_temp_tifs()
            self._cleanup_scratch()

    def _run_ingest(self, converter_exe: str, jobs: List[Dict[str, Any]],
                    slot: Any, phase: str,
                    activity: Any = None,
                    batch: Any = None,
                    before_launch: Optional[Callable[[], None]] = None,
                    ) -> Dict[str, Any]:
        """One ``aether_converter ingest`` batch through the shared runner,
        its ``Progress:`` lines into progress *slot* (model phase *phase*;
        with *activity*, that concurrent activity of the model instead) —
        and, for a batch whose jobs carry ``buildings_pbf_dir``, the
        converter's building-decode lines before them (the first
        :data:`run_progress.BUILDING_DECODE_SHARE` of the slot; the label
        says ``decoding building tiles n/m``) — and, for jobs carrying
        ``buildings_pbf_ready`` (aether_converter >= 0.2.12) or
        ``buildings_pbf_list`` (>= 0.2.14), its wait for the view's marker
        / the list (label ``waiting for building tiles``; the
        end-of-batch line adds the seconds the converter waited).
        *before_launch* is called right before the converter starts (the
        batch's timing line).
        Its job file is ``%TEMP%/aether_conv_<pid>_<batch>.json`` (*batch*:
        the batch id — ``files`` for Phase 2; default the slot's last part),
        removed when the batch ends, whatever the outcome.

        Returns ``{"tiles": {output name: (covered_px, total_px)},
        "no_data": [lines]}`` — ``tiles`` from the ``[Tile]`` lines (empty
        on a converter older than 0.2.7). Raises
        :class:`terrain_adapter.ConverterCancelled` when the run is being
        stopped (Stop, or a concurrent branch failed) and :class:`_RunFailed`
        when the converter failed — :class:`_BuildingsRunFailed` when jobs
        carry ``buildings_pbf_dir`` and the converter could not decode one
        of its tiles (``[Warn] buildings <tile>.pbf: …``, printed while it
        decodes the directory before the first tile: the batch is killed at
        the next line, the tiles leave the cache) or reported no burn at
        all (no ``[Info] buildings_pbf_dir:`` line) — both read by the
        run's ``osm_buildings.BurnMonitor`` (:attr:`_burns`, whose totals
        the end of the run judges). Those per-tile lines are counted into
        one summary line, not logged.
        """
        if batch is None:
            batch = (slot[-1] if isinstance(slot, tuple) and len(slot) > 1
                     else 0)
        job_file = os.path.join(
            tempfile.gettempdir(), f"aether_conv_{os.getpid()}_{batch}.json"
        )
        # Re-made a statement before the write: the output directory and
        # the scratch temp dir were created in an earlier phase, and a
        # directory that vanished in between is what the converter
        # reports as the pathless "os error 3".
        terrain_adapter.ensure_engine_dirs(
            *sorted({os.path.dirname(j["output_path"]) for j in jobs}),
            os.path.dirname(os.path.abspath(job_file)))
        # Fail with the offending PATH rather than letting the converter
        # die on a missing source or output directory with no name in it.
        # Before the write, so every exit after it is the converter
        # call's, whose `finally` removes the job file.
        terrain_adapter.preflight_ingest_jobs(jobs)
        with open(job_file, "w") as fh:
            json.dump(jobs, fh)

        self._log(f"Launching: {converter_exe} ingest")
        units = _convert_work(jobs)
        self.progress_model.set_work(slot, units)
        model = self.progress_model

        def show(detail: Dict[str, Any], reset: bool = False) -> None:
            if activity is not None:
                model.set_activity(activity, phase, detail, reset=reset)
            else:
                model.set_phase(phase, detail, reset=reset)

        if activity is not None:
            show({"done": 0, "total": len(jobs), "unit": "tiles"}, reset=True)
        tiles: Dict[str, Tuple[int, int]] = {}
        no_data: List[str] = []
        # Buildings of the batch's buildings_pbf_dir, read by the run's ONE
        # BurnMonitor (osm_buildings, shared with Site Analysis): the
        # converter decodes the directory once, BEFORE the first tile, and
        # only warns about a tile it cannot read — so the batch is stopped
        # at the first line after such warnings (nothing written yet), and
        # a batch that reports no burn at all is no success either.
        pbf_jobs = sum(1 for j in jobs if j.get("buildings_pbf_dir"))
        burns = self._burns
        burns.begin(pbf_jobs)
        # That decode is the first part of the batch's work (a fixed share,
        # run_progress.BUILDING_DECODE_SHARE): from toolkit 0.2.9 the
        # converter prints `[Rust] Decoding building tiles: n/m` every 1,000
        # tiles and `[Rust] Building tiles decoded: …` at its end — the bar
        # moves (and creeps) through it and the label names it, instead of
        # standing still until the first `Progress:` line (20,808 tiles on
        # the owner's run). Without those lines the first `Progress:` line
        # moves past the share at once.
        decode_units = (units * run_progress.BUILDING_DECODE_SHARE
                        if pbf_jobs else 0.0)
        per_tile_units = (units - decode_units) / max(len(jobs), 1)
        decoding = [False]
        # A batch launched before its building tiles are in (aether_converter
        # >= 0.2.12, CONTRACT item 48: its jobs carry buildings_pbf_ready;
        # >= 0.2.14, changelog 51: buildings_pbf_list) waits for its view's
        # marker / its list at the burn — `[Buildings] waiting for the
        # building tiles (…)` until `… ready after S s`, the monitor's
        # "wait" lines: the slot says so meanwhile (_WAITING_STEP).
        marker = any(j.get("buildings_pbf_ready")
                     or j.get("buildings_pbf_list") for j in jobs)
        waiting = [False]
        # The last `Progress:` counts: the label after a wait or the decode.
        last = [0, len(jobs)]

        def on_decode(done: int, total: int) -> None:
            per = decode_units / max(total, 1)
            model.set_done(
                slot, done * per,
                ceiling=run_progress.next_decode_report(done, total) * per)
            show({"step": "decoding building tiles", "done": done,
                  "total": total}, reset=not decoding[0])
            decoding[0] = True
            waiting[0] = False          # decoding: the marker is there
            self._emit_progress()
            self.status.emit(f"Decoding building tiles {done:,}/{total:,}")

        def on_decoded() -> None:
            model.set_done(slot, decode_units, ceiling=decode_units
                           + run_progress.next_ingest_report(0, len(jobs))
                           * per_tile_units)
            show({"done": last[0], "total": last[1], "unit": "tiles"},
                 reset=True)
            decoding[0] = False
            self._emit_progress()

        def on_wait(start: bool) -> None:
            if start:
                waiting[0] = True
                show({"step": _WAITING_STEP}, reset=True)
                self.status.emit("Waiting for the building tiles...")
            elif waiting[0]:
                waiting[0] = False
                show({"done": last[0], "total": last[1], "unit": "tiles"},
                     reset=True)
            self._emit_progress()

        def on_line(line: str) -> None:
            if self._stopping():
                raise terrain_adapter.ConverterCancelled()
            if pbf_jobs:
                if burns.feed(line):
                    raise _UndecodableBuildings()
                if burns.last_kind == "wait":
                    self._log(line)
                    on_wait(line.startswith(osm_buildings.WAITING_PREFIX))
                    return
                if burns.last_kind in ("undecodable", "decoding"):
                    self._log(line)
                    decode = run_progress.parse_building_decode(line)
                    if decode is not None:
                        on_decode(*decode)
                    return
                if burns.last_kind == "report":
                    return            # counted and summed, never per tile
                if run_progress.parse_buildings_decoded(line) is not None:
                    self._log(line)
                    on_decoded()
                    return
            tile = terrain_adapter.parse_tile_line(line)
            if tile is not None:
                # Parsed, not logged: one line per tile would bury the log.
                tiles[tile[0]] = (tile[1], tile[2])
                return
            if terrain_adapter.parse_tile_voids_line(line) is not None:
                # The tile's void count (aether_converter >= 0.2.11), one
                # per tile: the output keeps its voids, nothing reads it.
                return
            if line.startswith("[Rust] tile "):
                # The converter's per-tile timing diagnostic (one per tile,
                # changelog 31): never the panel — 920 tiles flooded it. The
                # `[Rust] run: …` summary line stays.
                return
            self._log(line)
            # CONTRACT item 18: a batch no source covers exits 0 with
            # this marker instead of failing — catch it so the success
            # path can say what the output actually holds.
            if "[Warn] NO-DATA batch:" in line:
                no_data.append(line)
            prog = run_progress.parse_ingest_progress(line)
            if prog:
                curr, tot = prog
                last[0], last[1] = curr, tot
                per = (units - decode_units) / max(tot, 1)
                model.set_done(
                    slot, decode_units + curr * per,
                    ceiling=decode_units
                    + run_progress.next_ingest_report(curr, tot) * per)
                if not waiting[0]:     # the wait's label stays until it ends
                    show({"done": curr, "total": tot, "unit": "tiles"},
                         reset=decoding[0])
                    decoding[0] = False
                self._emit_progress()
                if not waiting[0]:
                    self.status.emit(f"Converting tile {curr}/{tot}")

        rc: Optional[int] = None
        try:
            if before_launch is not None:
                before_launch()
            # The one shared subprocess runner (terrain_adapter).
            rc = terrain_adapter.run_converter_streaming(
                converter_exe, ["ingest", "--job-file", job_file],
                on_line, on_start=self._register_proc)
        except _UndecodableBuildings:
            pass            # killed by the runner; reported below
        finally:
            if activity is not None:
                self.progress_model.end_activity(activity)
            # Every exit — done, failed, cancelled, killed: the %TEMP%
            # job file is per-PID scratch nothing else ever removes.
            try:
                os.remove(job_file)
            except OSError:
                pass

        if burns.batch_bad and not self._stopping():
            raise self._undecodable_buildings(burns.batch_bad)
        if self._stopping():
            # cancel() (or a failed concurrent branch) killed the process
            # directly — the kill won the race against the per-line check,
            # so rc is the kill signal (-15), not a converter failure.
            raise terrain_adapter.ConverterCancelled()

        if rc != 0:
            msg = f"Converter failed (exit code {rc}). See log."
            self._log(msg)
            raise _RunFailed(msg)
        if pbf_jobs:
            msg = burns.end(tiles=len(jobs))
            if msg:
                self._log(f"ERROR: {msg}")
                raise _BuildingsRunFailed(msg)
            self._log(f"  buildings: burned onto {burns.batch_reports:,} of "
                      f"{len(jobs):,} tile(s) — {burns.batch_drawn:,} "
                      f"building(s) drawn, {burns.batch_raised:,} px raised"
                      + (f" — the converter waited "
                         f"{burns.batch_waited_s:.1f} s for them"
                         if marker else ""))
        self.progress_model.finish(slot)
        return {"tiles": tiles, "no_data": no_data}

    def _run_phase2(self, converter_exe: str,
                    phase2: List[Tuple[int, Dict[str, Any]]],
                    all_entries: List[Dict[str, Any]],
                    buildings_path: Any,
                    files_fps: Dict[str, Optional[str]],
                    tmp_dir: str, note: str = "...",
                    concurrent: bool = False,
                    ) -> List[Tuple[int, Dict[str, Any]]]:
        """Phase 2: convert *phase2* (the PROVEN tiles) from the files above
        the first XYZ entry alone, into ``<output>/.phase1-<pid>/``. A tile
        whose ``[Tile]`` line says ``covered == total`` is moved into the
        output dir (final; manifest = its files-only fingerprint); every
        other one (a hole, or no line for it) is deleted and returned for
        the pool — as is a complete tile whose move Windows still refused
        after ``terrain_adapter.replace_with_retry`` (logged; Phase 3
        converts it with the whole stack). *concurrent*: the downloads run
        meanwhile (the progress label names both; *note* completes the
        status line). *buildings_path*: the run's buildings
        (:func:`_buildings_spec`) — the batch's job build waits for ITS
        OpenFreeMap tiles (:meth:`_pbf_batch`; fetched first; on
        aether_converter >= 0.2.12 the converter waits instead, for its
        view's marker — on >= 0.2.14 for its list), the downloads keep
        running. Phase 2 is the first
        entry of the ingest queue: no
        Phase-3 batch runs before it ends (job file
        ``aether_conv_<pid>_files.json``). Raises ``ConverterCancelled`` /
        :class:`_RunFailed` (the scratch dir goes with the run's cleanup)."""
        prefix = _phase2_entries(all_entries)
        scratch = os.path.join(self._output_dir,
                               f"{_PHASE2_SCRATCH_PREFIX}{os.getpid()}")
        # A scratch dir left by a run that was killed outright holds
        # nothing anyone will promote.
        for name in os.listdir(self._output_dir):
            stale = os.path.join(self._output_dir, name)
            if (name.startswith(_PHASE2_SCRATCH_PREFIX) and stale != scratch
                    and os.path.isdir(stale)):
                shutil.rmtree(stale, ignore_errors=True)
        self._scratch_dirs.append(scratch)
        os.makedirs(scratch, exist_ok=True)
        pbf_dir, pbf_fields = self._pbf_batch(phase2)
        try:
            jobs, temps = _build_tile_jobs(
                phase2, scratch, True, prefix, buildings_path, tmp_dir,
                should_cancel=self._stopping, pbf_dir=pbf_dir,
                pbf_ready=pbf_fields)
        except RuntimeError as exc:
            raise _RunFailed(str(exc))
        self._temp_tifs.extend(temps)
        self._say(f"Converting {len(jobs)} tiles from local files{note}",
                  "Converting tiles from local files")
        self._log(f"Phase 2: converting {len(jobs)} tile(s) from the local "
                  f"files alone — the pool is fetched only where they leave "
                  f"a hole")
        coverage: Dict[str, Tuple[int, int]] = {}
        if jobs:
            result = self._run_ingest(
                converter_exe, jobs, _CONVERT_FILES,
                "Converting tiles from local files",
                activity=_CONVERT_FILES if concurrent else None,
                batch=_CONVERT_FILES[1])
            coverage = result["tiles"]
        built = {os.path.basename(j["output_path"]) for j in jobs}
        promoted: Dict[str, Optional[str]] = {}
        queued: List[Tuple[int, Dict[str, Any]]] = []
        for res, tile in phase2:
            name = tile["filename"]
            path = os.path.join(scratch, name)
            cov = coverage.get(name)
            if (name in built and cov is not None and cov[1] > 0
                    and cov[0] >= cov[1]
                    and _output_tile_whole(scratch, tile)):
                try:
                    terrain_adapter.replace_with_retry(
                        path, os.path.join(self._output_dir, name))
                    promoted[name] = files_fps.get(name)
                    continue
                except OSError as exc:
                    self._log(f"  WARNING: could not move {name} into the "
                              f"output folder ({exc}) — it is converted "
                              f"again with the whole stack")
            try:
                os.remove(path)
            except OSError:
                pass
            queued.append((res, tile))
        if promoted:
            self._record_built(promoted, {
                t["filename"]: _buildings_record(buildings_path, r, t)
                for r, t in phase2 if t["filename"] in promoted})
        shutil.rmtree(scratch, ignore_errors=True)
        self._scratch_dirs.remove(scratch)
        # Phase 3 makes its own window copies (same names); these are done.
        for path in temps:
            try:
                os.remove(path)
            except OSError:
                pass
        done_temps = set(temps)
        self._temp_tifs = [t for t in self._temp_tifs if t not in done_temps]
        self.progress_model.finish(_CONVERT_FILES)
        self._emit_progress()
        self._log(f"Phase 2: {len(phase2)} tiles from local files — "
                  f"{len(promoted)} complete, {len(queued)} need the pool")
        return queued

    def _cleanup_scratch(self) -> None:
        """Remove this run's Phase-2 scratch dirs (cancel, failure, crash):
        nothing from Phase 2 stays in the output dir unpromoted."""
        for path in self._scratch_dirs:
            shutil.rmtree(path, ignore_errors=True)
        self._scratch_dirs = []

    def _register_proc(self, proc) -> None:
        """on_start hook of EVERY engine process of the run: kept for
        cancel(), which kills them all.

        A cancel() (or a branch failure) that landed after the last poll
        but before this launch found no process to kill, so the process is
        killed here instead — otherwise an ingest ran on until its first
        progress line (10 tiles). Processes that have ended (``returncode``
        set by the runner) are dropped from the list.
        """
        with self._procs_lock:
            self._procs = [p for p in self._procs
                           if getattr(p, "returncode", None) is None]
            self._procs.append(proc)
            self._proc = proc
        if self._stopping():
            _kill_procs([proc])

    def _cleanup_temp_tifs(self):
        """Remove the per-tile materialized GeoTIFF scratch files."""
        for tf in self._temp_tifs:
            try:
                if os.path.exists(tf):
                    os.remove(tf)
            except OSError:
                pass
        self._temp_tifs = []

    # -- Heavy I/O methods (run on worker thread) -------------------------

    def _resolve_raster(self, entry: dict) -> Dict[str, Any]:
        """Resolve one raster stack entry to an acquisition dict.

        ``{"kind": "files", "infos": […]}`` for local files/folders (per-file
        source infos, generic CRS, entry-extent-filtered);
        ``{"kind": "xyz", …}`` and ``{"kind": "rendered", …}`` pass through
        from the main-thread router — the download happens later in
        ``run()`` (needs the enumerated tile grid), the render already
        happened on the main thread.

        No reprojection of any kind: the converter samples every source in
        its own CRS, and a CRS it rejects is a hard error surfaced verbatim.
        """
        resolved = entry["resolved_source"]
        # Back-compat: a plain string is a local path (older callers/tests).
        if isinstance(resolved, str):
            resolved = {"kind": "file", "path": resolved}

        kind = resolved.get("kind")
        if kind == "xyz":
            self._log(f"  XYZ service (shared toolkit download): "
                      f"{resolved['uri'][:80]}")
            return {"kind": "xyz", "uri": resolved["uri"],
                    "extent": resolved.get("extent") or entry.get("extent")}
        if kind == "rendered":
            tiles = resolved.get("tiles", {})
            self._log(f"  Rendered service: {len(tiles)} pre-rendered "
                      f"tile(s) from the main thread")
            return {"kind": "rendered", "tiles": tiles}

        source = resolved["path"]
        crs = entry["crs_authid"] or ""
        extent = entry.get("extent")

        if os.path.isdir(source):
            self._log(f"  Folder: {source} (recursive, from its footprint "
                      f"index)")
            # Each file in its OWN CRS. The entry's crs_authid is display
            # only for a folder: a CRS-less file is georeferenced ONLY by a
            # CRS the user assigned to it (crs_overrides) — it used to get
            # the first file's CRS silently and land kilometres off. Problem
            # files the user did not answer are a hard error naming them.
            infos = terrain_adapter.folder_source_infos(
                source, decisions=entry.get("decisions") or {},
                crs_overrides=entry.get("crs_overrides") or {},
                mode=entry.get("overlap_priority"),
                should_cancel=lambda: self._canceled, log=self._log)
        else:
            self._log(f"  File: {os.path.basename(source)}")
            # A single-file entry carries the LAYER's authid — authoritative,
            # since a user can deliberately override a layer's CRS in QGIS.
            infos = [terrain_adapter.file_layer_info(
                         source, crs_authid=crs or None)]

        if extent:
            before = len(infos)
            infos = [
                i for i in infos
                if bboxes_intersect(i["wgs84_bounds"],
                                    _expand_bbox(extent, i["halo_deg"]))
            ]
            self._log(f"  {len(infos)}/{before} file(s) overlap the extent")
        return {"kind": "files", "infos": infos}

    def _resolve_buildings(self, entry: dict) -> Optional[str]:
        """Resolve a building FILE entry (Add Buildings…) to an FGB path —
        the shared vector resolution in ``core.buildings_source``, the same
        call the Site Analysis path makes, so both tabs hand the converter
        the same kind of file for the same source.

        Download Buildings… rows never come here: the run fetches their
        OpenFreeMap tiles itself (:class:`_BuildingsBranch`)."""
        path = entry["resolved_source"]
        src_crs = entry.get("crs_authid", "") or ""
        if _is_osm_source(path):
            raise RuntimeError(
                "an OSM buildings row is downloaded by the run, not "
                "resolved as a file")
        return resolve_buildings_source(path, src_crs, log=self._log)

# ---------------------------------------------------------------------------
# Rectangle-draw map tool
# ---------------------------------------------------------------------------

class _RectangleDrawTool:
    """Two-click rectangle on the map canvas with live rubber band preview.

    Click once to set the first corner, move the mouse to see the
    rectangle, click again to confirm.  Right-click or Escape cancels.
    """

    def __init__(self, iface, callback):
        from qgis.gui import QgsMapToolEmitPoint
        self._iface = iface
        self._callback = callback
        self._canvas = iface.mapCanvas()
        self._prev_tool = self._canvas.mapTool()
        self._first_point_canvas = None   # in canvas CRS (for rubber band)
        self._first_point_wgs84 = None    # in WGS84 (for callback)

        # Rubber band — visible orange rectangle.
        self._rb = QgsRubberBand(self._canvas, QgsWkbTypes.GeometryType.PolygonGeometry)
        self._rb.setColor(QColor(255, 120, 0, 100))
        self._rb.setWidth(2)
        self._rb.setLineStyle(Qt.PenStyle.DashLine)

        # We subclass QgsMapToolEmitPoint inline by monkey-patching
        # canvasMoveEvent and canvasReleaseEvent.
        self._tool = QgsMapToolEmitPoint(self._canvas)
        self._tool.setCursor(Qt.CursorShape.CrossCursor)
        self._tool.canvasReleaseEvent = self._on_release
        self._tool.canvasMoveEvent = self._on_move
        self._tool.keyPressEvent = self._on_key
        self._canvas.setMapTool(self._tool)

    # -- Coordinate helpers ------------------------------------------------

    def _to_wgs84(self, canvas_point):
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        map_crs = self._canvas.mapSettings().destinationCrs()
        if map_crs != wgs84:
            xform = QgsCoordinateTransform(
                map_crs, wgs84, QgsProject.instance()
            )
            return xform.transform(canvas_point)
        return canvas_point

    # -- Events ------------------------------------------------------------

    def _on_release(self, event):
        if event.button() == Qt.MouseButton.RightButton:
            self._cancel()
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return

        point = self._tool.toMapCoordinates(event.pos())

        if self._first_point_canvas is None:
            # First click — record start corner.
            self._first_point_canvas = point
            self._first_point_wgs84 = self._to_wgs84(point)
        else:
            # Second click — finalize rectangle.
            pt2 = self._to_wgs84(point)
            s = min(self._first_point_wgs84.y(), pt2.y())
            n = max(self._first_point_wgs84.y(), pt2.y())
            w = min(self._first_point_wgs84.x(), pt2.x())
            e = max(self._first_point_wgs84.x(), pt2.x())
            # Clear the preview once the rectangle is committed — the extent
            # now lives in the layer entry and is shown there, so leaving the
            # band on the canvas only accumulates stale outlines across draws.
            self._rb.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
            self._restore()
            self._callback(s, n, w, e)

    def _on_move(self, event):
        """Update rubber band as mouse moves after first click."""
        if self._first_point_canvas is None:
            return
        current = self._tool.toMapCoordinates(event.pos())
        self._draw_rect(self._first_point_canvas, current)

    def _on_key(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self._cancel()

    # -- Rubber band -------------------------------------------------------

    def _draw_rect(self, p1, p2):
        """Draw a rectangle from two opposite corners (canvas CRS)."""
        self._rb.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
        self._rb.addPoint(QgsPointXY(p1.x(), p1.y()), False)
        self._rb.addPoint(QgsPointXY(p2.x(), p1.y()), False)
        self._rb.addPoint(QgsPointXY(p2.x(), p2.y()), False)
        self._rb.addPoint(QgsPointXY(p1.x(), p2.y()), False)
        self._rb.addPoint(QgsPointXY(p1.x(), p1.y()), True)  # close + update

    # -- Cleanup -----------------------------------------------------------

    def _cancel(self):
        self._rb.reset()
        self._restore()

    def _restore(self):
        if self._prev_tool:
            self._canvas.setMapTool(self._prev_tool)
        else:
            self._canvas.unsetMapTool(self._tool)

    def cleanup(self):
        """Remove rubber band from canvas."""
        if self._rb:
            try:
                self._rb.reset()
                self._canvas.scene().removeItem(self._rb)
            except Exception:
                pass
            self._rb = None


# ===========================================================================
# Main tab widget
# ===========================================================================

_COL_TYPE = 0
_COL_SOURCE = 1
_COL_CRS = 2
_COL_NATIVE_RES = 3
_COL_EXTENT = 4
#: No per-row resolution: the run's resolutions are the "Generate tiles
#: at" checkboxes (the "Target Res" column was never read by a run).
_LAYER_COLS = ["Type", "Source", "CRS", "Native Res", "Extent"]

#: Extent-picker choice for folder rows: the union of the folder's files.
_WHOLE_FOLDER = "Whole folder (all files)"


def _inspect_output_path(folder: str, resolutions: List[int]) -> str:
    """``%TEMP%/aether_inspect_<folder>_<res>m[+<res>m].tif`` for the Inspect
    .abt preview. An older preview of the same choice is replaced; one QGIS
    still holds open (Windows refuses the delete) gets a numbered sibling."""
    base = os.path.basename(os.path.normpath(folder)) or "abt"
    tag = "+".join(f"{r}m" for r in sorted(resolutions))
    stem = os.path.join(tempfile.gettempdir(), f"aether_inspect_{base}_{tag}")
    path, n = stem + ".tif", 1
    while os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            n += 1
            path = f"{stem}_{n}.tif"
            continue
        try:
            os.remove(path + ".aux.xml")    # QGIS's statistics of the old one
        except OSError:
            pass
    return path


def _ask_inspect_choice(parent: Any, plan: Any) -> Optional[List[int]]:
    """The Inspect .abt dialog: the folder's resolutions (tile count, MB,
    what share of the folder each covers — ``abt_inspect.group_label``),
    the default choice ticked (``plan.default``), and what the current
    choice reads (``abt_inspect.choice_note`` — with the warning when the
    chosen tiles hold more than 2 GB). Returns the ticked resolutions, or
    None on Cancel."""
    from ..core import abt, abt_inspect

    dlg = QDialog(parent)
    dlg.setWindowTitle("Inspect .abt")
    lay = QVBoxLayout(dlg)
    head = QLabel(
        f"{plan.folder}\n\n{plan.tile_count:,} tile(s) at "
        f"{len(plan.groups)} resolution(s). Which should the preview show? "
        f"Only their tiles are read.")
    head.setWordWrap(True)
    lay.addWidget(head)
    boxes: Dict[int, Any] = {}
    for group in plan.groups:
        cb = QCheckBox(abt_inspect.group_label(group))
        cb.setChecked(group.res_m in plan.default)
        lay.addWidget(cb)
        boxes[group.res_m] = cb
    if plan.unreadable:
        lay.addWidget(QLabel(f"{len(plan.unreadable):,} .abt file(s) have no "
                             f"readable header and are left out."))
    note = QLabel("")
    note.setWordWrap(True)
    lay.addWidget(note)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
    ok = buttons.button(QDialogButtonBox.StandardButton.Ok)

    def chosen() -> List[int]:
        return [res for res, cb in boxes.items() if cb.isChecked()]

    def refresh(*_args: Any) -> None:
        picked = chosen()
        note.setText(abt_inspect.choice_note(plan, picked,
                                             abt.INSPECT_MAX_DIM))
        ok.setEnabled(bool(picked))

    for cb in boxes.values():
        cb.toggled.connect(refresh)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    lay.addWidget(buttons)
    refresh()
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return None
    return chosen() or None


class MapConverterTab(QWidget):
    """Map Converter tab — fuse layers into .abt terrain tiles."""

    def __init__(self, parent_dialog, parent=None):
        super().__init__(parent)
        self._dialog = parent_dialog
        self.iface = parent_dialog.iface
        self._worker = None
        # Set by _on_done/_on_error: the current run reported its own end.
        self._run_reported = False
        self._layers: List[_LayerEntry] = []
        self._rect_tool = None
        self._drawing_for_layer: Optional[List[int]] = None
        self._extent_clipboard: Optional[Dict[str, float]] = None
        self._run_model: Optional[run_progress.RunProgress] = None
        # Pre-run disk estimate: file infos + per-tile verdicts of the
        # current stack (see _estimate_disk_need), touched by the estimate
        # thread and _on_run under _est_lock.
        self._est_cache: Dict[str, Any] = {}
        # The estimate label's requests: debounce timer, generation, the
        # running / pending job (see _update_estimate).
        self._init_estimate()
        # "Generate tiles at" follows the stack (auto_output_resolutions)
        # until a box is ticked by hand; _res_applying marks the rule's own
        # ticks (see _on_res_toggled).
        self._res_user_touched = False
        self._res_applying = False
        # Re-renders the bar/label between converter lines (extrapolation up
        # to the next certain report, ETA countdown).
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(500)
        self._progress_timer.timeout.connect(self._refresh_progress)

        self._build_ui()
        self._connect_signals()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)

        # ---- Layer Stack ----
        grp = QGroupBox("Layer Stack (highest priority on top)")
        gl = QVBoxLayout(grp)

        add_row = QHBoxLayout()
        self._combo_add = QgsMapLayerComboBox()
        _set_layer_filters(self._combo_add)
        self._combo_add.setAllowEmptyLayer(True)
        self._combo_add.setShowCrs(True)
        self._combo_add.setMinimumWidth(200)
        add_row.addWidget(self._combo_add, 3)

        self._btn_add_layer = QPushButton("Add Layer")
        self._btn_add_layer.setToolTip("Add QGIS layer (DEM or buildings).")
        add_row.addWidget(self._btn_add_layer)

        self._btn_add_all_layers = QPushButton("Add All Layers")
        self._btn_add_all_layers.setToolTip(
            "Add every loaded elevation raster layer that is not in the "
            "stack yet, each with its own layer extent.\nLayers that look "
            "like imagery are skipped; map services (XYZ / WMS) get no "
            "extent — set it with Draw Extent or Extent from Layer.")
        add_row.addWidget(self._btn_add_all_layers)

        self._btn_add_folder = QPushButton("Add Folder...")
        self._btn_add_folder.setToolTip("Add a folder of GeoTIFF/DEM files.")
        add_row.addWidget(self._btn_add_folder)

        self._btn_add_buildings = QPushButton("Add Buildings...")
        self._btn_add_buildings.setToolTip(
            "Add building footprints from file (FGB/SHP/GPKG/GDB) or folder."
        )
        add_row.addWidget(self._btn_add_buildings)

        self._btn_download_buildings = QPushButton("Download Buildings...")
        self._btn_download_buildings.setToolTip(
            "Download building footprints from OpenStreetMap for a bbox."
        )
        add_row.addWidget(self._btn_download_buildings)

        self._btn_inspect = QPushButton("Inspect .abt...")
        self._btn_inspect.setToolTip(
            "Load a folder of .abt tiles as a terrain layer in QGIS."
        )
        add_row.addWidget(self._btn_inspect)

        gl.addLayout(add_row)

        # Table
        self._table = QTableWidget(0, len(_LAYER_COLS))
        self._table.setHorizontalHeaderLabels(_LAYER_COLS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # Extended: Ctrl/Shift-click several rows, then Draw Extent, Extent
        # from Layer, Paste extent or Remove act on all of them. Up/Down and
        # the overlap rule act on the current row.
        self._table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.horizontalHeader().setSectionResizeMode(
            _COL_SOURCE, QHeaderView.ResizeMode.Stretch
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setMinimumHeight(130)
        gl.addWidget(self._table)

        btn_row = QHBoxLayout()
        self._btn_up = QPushButton("\u25b2 Up")
        self._btn_down = QPushButton("\u25bc Down")
        self._btn_remove = QPushButton("Remove")
        self._btn_draw_extent = QPushButton("Draw Extent for Layer")
        self._btn_draw_extent.setToolTip(
            "Draw a rectangle on the map to set the extent of the selected "
            "row(s)."
        )
        self._btn_layer_extent = QPushButton("Extent from Layer")
        self._btn_layer_extent.setToolTip(
            "Set the selected row(s)' extent from a loaded layer's "
            "geographic bounds — or, for folder rows, from all files of the "
            "folder."
        )
        self._btn_extent_all = QPushButton("Apply Extent to All")
        self._btn_extent_all.setToolTip(
            "Copy the current row's extent to every other row.\n"
            "Right-click the table to copy / paste an extent between rows."
        )
        btn_row.addWidget(self._btn_up)
        btn_row.addWidget(self._btn_down)
        btn_row.addWidget(self._btn_draw_extent)
        btn_row.addWidget(self._btn_layer_extent)
        btn_row.addWidget(self._btn_extent_all)
        btn_row.addWidget(QLabel("Where files overlap:"))
        self._combo_overlap = QComboBox()
        for mode in (terrain_index.PRIORITY_FINEST, terrain_index.PRIORITY_NAME):
            self._combo_overlap.addItem(terrain_index.PRIORITY_LABELS[mode], mode)
        self._combo_overlap.setToolTip(
            "For the selected FOLDER: which file wins where two files of the "
            "folder overlap.\n\nFinest resolution wins (default): the file "
            "with the smallest pixel size (in metres) is used; equal "
            "resolutions fall back to file name order.\nFile name order: the "
            "alphabetically first path wins — a coarse filler named before a "
            "fine dataset then beats it (the log warns).")
        self._combo_overlap.setEnabled(False)
        btn_row.addWidget(self._combo_overlap)
        btn_row.addStretch()
        btn_row.addWidget(self._btn_remove)
        gl.addLayout(btn_row)

        root.addWidget(grp)

        # ---- Output Resolutions ----
        res_grp = QGroupBox("Output .abt Resolutions")
        res_lay = QHBoxLayout(res_grp)
        res_lay.addWidget(QLabel("Generate tiles at:"))
        self._res_checks: Dict[int, QCheckBox] = {}
        for res in sorted(_ABT_EXTENT_DEG):
            ext = _ABT_EXTENT_DEG.get(res, 1.0)
            sz = _tile_params(ext, 0.0, 0.0, res)["size_px"]
            cb = QCheckBox(f"{res}m")
            cb.setToolTip(
                f"{res}m resolution, {ext}\u00b0 geographic extent per tile, "
                f"{sz}\u00d7{sz} px"
            )
            # 30 m until the first row arrives; then the Auto rule
            # (auto_output_resolutions) until a box is ticked by hand.
            if res == _AUTO_DEFAULT_M:
                cb.setChecked(True)
            self._res_checks[res] = cb
            res_lay.addWidget(cb)
        self._btn_auto_res = QPushButton("Auto")
        self._btn_auto_res.setToolTip(_auto_resolutions_tip(self._layers))
        res_lay.addWidget(self._btn_auto_res)
        res_lay.addStretch()
        self._chk_overwrite = QCheckBox("Rebuild existing tiles")
        self._chk_overwrite.setToolTip(
            "Overwrite .abt tiles that already exist in the output directory, "
            "and discard cached reprojections.\n\n"
            "Tiles are normally skipped when a file of the same name is "
            "present, and tile names encode only position and resolution — not "
            "the source, the export pitch or the plugin version. So after "
            "changing any of those, an existing tile is silently reused and "
            "you keep looking at the old pixels. Tick this to force a rebuild."
        )
        res_lay.addWidget(self._chk_overwrite)
        self._chk_keep_pool = QCheckBox("Keep pool tiles")
        self._chk_keep_pool.setToolTip(_KEEP_POOL_TIP)
        self._chk_keep_pool.setChecked(_keep_pool_tiles_setting())
        res_lay.addWidget(self._chk_keep_pool)
        root.addWidget(res_grp)

        # ---- Output ----
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Output:"))
        self._edit_output = QLineEdit()
        self._edit_output.setPlaceholderText(terrain_adapter.get_cache_dir())
        self._edit_output.setToolTip("Directory for .abt output files.")
        out_row.addWidget(self._edit_output, 1)
        self._btn_browse = QPushButton("Browse...")
        out_row.addWidget(self._btn_browse)
        root.addLayout(out_row)

        # Wired only now: the estimate reads the output directory and the
        # rebuild flag, both of which are built above but *after* the
        # resolution checkboxes.
        self._connect_res_checks()

        # ---- Estimate ----
        self._lbl_estimate = QLabel("")
        self._lbl_estimate.setWordWrap(True)
        root.addWidget(self._lbl_estimate)

        # ---- Run / Cancel ----
        run_row = QHBoxLayout()
        self._btn_run = QPushButton("Convert")
        self._btn_run.setMinimumHeight(32)
        self._btn_cancel = QPushButton("Cancel")
        self._btn_cancel.setEnabled(False)
        run_row.addStretch()
        run_row.addWidget(self._btn_run)
        run_row.addWidget(self._btn_cancel)
        root.addLayout(run_row)

        # ---- Progress bar ----
        self._progress = QProgressBar()
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        root.addWidget(self._progress)

        self._lbl_status = QLabel("Ready.")
        root.addWidget(self._lbl_status)

        # ---- Log area ----
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.setMaximumHeight(150)
        self._log.setPlaceholderText("Converter output will appear here...")
        root.addWidget(self._log)

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def _connect_signals(self):
        self._btn_add_layer.clicked.connect(self._on_add_layer)
        self._btn_add_all_layers.clicked.connect(self._on_add_all_layers)
        self._btn_extent_all.clicked.connect(self._on_apply_extent_to_all)
        self._table.customContextMenuRequested.connect(
            self._on_table_context_menu)
        self._btn_add_folder.clicked.connect(self._on_add_folder)
        self._btn_add_buildings.clicked.connect(self._on_add_buildings)
        self._btn_download_buildings.clicked.connect(self._on_download_buildings)
        self._btn_up.clicked.connect(self._on_move_up)
        self._btn_down.clicked.connect(self._on_move_down)
        self._btn_remove.clicked.connect(self._on_remove)
        self._btn_draw_extent.clicked.connect(self._on_draw_extent)
        self._btn_layer_extent.clicked.connect(self._on_set_layer_extent)
        self._btn_browse.clicked.connect(self._on_browse_output)
        self._btn_run.clicked.connect(self._on_run)
        self._btn_cancel.clicked.connect(self._on_cancel)
        self._btn_inspect.clicked.connect(self._on_inspect_abt)
        self._btn_auto_res.clicked.connect(self._on_auto_resolutions)
        self._chk_keep_pool.toggled.connect(self._on_keep_pool_toggled)
        # The estimate skips the tiles already built unless this is ticked.
        self._chk_overwrite.toggled.connect(self._update_estimate)
        self._table.itemSelectionChanged.connect(self._sync_overlap_combo)
        self._combo_overlap.currentIndexChanged.connect(self._on_overlap_changed)

    def _connect_res_checks(self) -> None:
        """Every "Generate tiles at" box → :meth:`_on_res_toggled`."""
        for cb in self._res_checks.values():
            cb.stateChanged.connect(self._on_res_toggled)

    # ------------------------------------------------------------------
    # Add layers
    # ------------------------------------------------------------------

    def _on_add_layer(self):
        layer = self._combo_add.currentLayer()
        if layer is None:
            QMessageBox.warning(self, "No Layer", "Select a layer first.")
            return
        self._add_qgis_layer(layer)

    def _layer_in_stack(self, layer) -> bool:
        """True if *layer* (same QGIS layer, or same source) is a row."""
        src = layer.source()
        for e in self._layers:
            if e.qgis_layer is layer:
                return True
            try:
                if e.qgis_layer is not None and e.qgis_layer.id() == layer.id():
                    return True
            except RuntimeError:       # the row's layer was deleted
                pass
            if e.source_path and e.source_path == src:
                return True
        return False

    def _on_add_all_layers(self):
        """Add every loaded elevation raster not in the stack yet.

        The same candidates the layer combo offers (raster layers of the
        project), minus vectors (buildings stay a deliberate Add), minus
        layers that classify as imagery (Add Layer asks about those one by
        one; a bulk add must not slip a picture into the terrain stack).
        Each row starts with its own layer extent (see ``_add_qgis_layer``).
        """
        added, present, imagery = [], 0, []
        for layer in QgsProject.instance().mapLayers().values():
            if not isinstance(layer, QgsRasterLayer):
                continue
            if self._layer_in_stack(layer):
                present += 1
                continue
            if _detect_layer_type(layer) == "imagery":
                imagery.append(layer.name())
                continue
            if self._add_qgis_layer(layer, ask_imagery=False,
                                    with_extent=True, refresh=False):
                added.append(layer.name())
        self._rebuild_table()
        msg = f"Add All Layers: {len(added)} added"
        if present:
            msg += f", {present} already in the stack"
        if imagery:
            msg += (f", {len(imagery)} skipped as imagery "
                    f"({', '.join(imagery[:5])}"
                    + (", …" if len(imagery) > 5 else "") + ")")
        self._log_msg(msg)
        self._on_stack_changed()
        if not added:
            QMessageBox.information(
                self, "Add All Layers",
                "No elevation raster layer to add." + (
                    f"\n{present} loaded layer(s) are already in the stack."
                    if present else "") + (
                    f"\n{len(imagery)} layer(s) look like imagery — add "
                    f"those with Add Layer if they really are elevation."
                    if imagery else ""))

    @staticmethod
    def _layer_wgs84_extent(layer) -> Optional[Dict[str, float]]:
        """*layer*'s extent in WGS84 (``{south, north, west, east}``)."""
        ext = layer.extent()
        if ext is None or ext.isEmpty():
            return None
        crs = layer.crs()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        if crs.isValid() and crs != wgs84:
            xform = QgsCoordinateTransform(crs, wgs84, QgsProject.instance())
            ext = xform.transformBoundingBox(ext)
        return {"south": ext.yMinimum(), "north": ext.yMaximum(),
                "west": ext.xMinimum(), "east": ext.xMaximum()}

    def _add_qgis_layer(self, layer, ask_imagery: bool = True,
                        with_extent: bool = False,
                        refresh: bool = True) -> bool:
        """Append *layer* as a stack row; False when the user declined.

        *with_extent* pre-fills the row with the layer's own WGS84 extent —
        for local files only: a map service's "extent" is the whole service
        (an XYZ layer is the world), which would turn a run into a download
        of everything.
        """
        ltype = _detect_layer_type(layer)
        if ltype == "imagery" and ask_imagery:
            r = QMessageBox.warning(
                self, "Possible Imagery",
                f"'{layer.name()}' looks like imagery (RGB), not a DEM.\n"
                f"Elevation data needs single-band Float32/Int16.\n\nAdd anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                return False

        is_buildings = isinstance(layer, QgsVectorLayer)

        # Detect resolution.
        native_res = None
        if not is_buildings:
            source = layer.source()
            if "type=xyz" in source:
                native_res = terrain_adapter.xyz_native_resolution_m(source)
            else:
                native_res = _detect_resolution(layer)

        entry = _LayerEntry(
            layer_type="buildings" if is_buildings else "raster",
            source_path=layer.source(),
            qgis_layer=layer,
            crs_authid=(layer.crs().authid()
                        if layer.crs().isValid() else "EPSG:4326"),
            native_res_m=native_res,
            priority=len(self._layers) + 1,
        )
        note = ""
        if with_extent and not is_buildings:
            if _classify_acquisition(layer.source()) == "file":
                try:
                    entry.extent = self._layer_wgs84_extent(layer)
                except Exception as exc:  # noqa: BLE001 — row stays unset
                    note = f"; extent not set ({exc})"
            else:
                note = ("; map service — set its extent with Draw Extent or "
                        "Extent from Layer")
        self._layers.append(entry)
        if refresh:
            self._rebuild_table()
        self._log_msg(f"Added layer: {layer.name()} "
                      f"(native ~{native_res or '?'}m){note}")
        if refresh:
            self._on_stack_changed()
        return True

    def _on_add_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select DEM/GeoTIFF Folder", "",
        )
        if not folder:
            return
        # The folder index is built in a worker thread behind a progress
        # dialog (a 2,500-file folder used to be opened file by file on the
        # GUI thread). Recursive, sorted, zip members included — the same
        # scanner the worker (and Site Analysis) uses.
        try:
            scan = terrain_folder_scan.scan_folder(self, folder)
        except RuntimeError as exc:
            QMessageBox.critical(self, "Folder could not be read", str(exc))
            return
        if scan is None:
            return
        index = scan["index"]
        if not index.entries:
            QMessageBox.warning(self, "No Terrain Files",
                                f"No elevation rasters in:\n{folder}\n"
                                f"(searched recursively; .vrt files are "
                                f"ignored — load a .vrt as a layer)")
            return

        # CRS-less files: asked, never inherited. Unusable / 8-bit files:
        # one skip prompt, default Cancel.
        answer = terrain_folder_scan.resolve_folder_problems(
            self, folder, index, ask_crs=True)
        if answer is None:
            return
        crs_overrides, decisions = answer

        crs_id = terrain_folder_scan.suggested_crs(index) or "?"
        authids = {e.get("crs_authid") for e in index.entries.values()
                   if e.get("readable") and e.get("crs_authid")}
        if len(authids) > 1:
            crs_id = f"mixed ({len(authids)})"
        native_res = _index_finest_res(index)

        entry = _LayerEntry(
            layer_type="raster", source_path=folder,
            crs_authid=crs_id, native_res_m=native_res,
            priority=len(self._layers) + 1,
        )
        entry.crs_overrides = crs_overrides
        entry.decisions = decisions
        entry.overlap_priority = terrain_index.get_overlap_priority(folder)
        # The whole folder by default: every usable file's bounds, from the
        # index the scan above just built (no further file access).
        entry.extent = _folder_extent(index, decisions, crs_overrides)
        self._layers.append(entry)
        self._rebuild_table()
        self._table.selectRow(len(self._layers) - 1)
        skipped = sum(1 for d in decisions.values()
                      if d == terrain_index.DECISION_SKIP)
        self._log_msg(f"Added folder: {folder} ({len(index.entries)} files"
                      + (f", {skipped} skipped" if skipped else "")
                      + f", finest ~{native_res or '?'}m; overlaps: "
                      f"{terrain_index.PRIORITY_LABELS[entry.overlap_priority]}"
                      + ("; extent = whole folder" if entry.extent else
                         "; no usable file, extent not set") + ")")
        self._on_stack_changed()

    def _selected_folder_entry(self) -> Optional[_LayerEntry]:
        row = self._table.currentRow()
        if 0 <= row < len(self._layers):
            e = self._layers[row]
            if e.layer_type == "raster" and os.path.isdir(e.source_path or ""):
                return e
        return None

    def _sync_overlap_combo(self, *_args) -> None:
        """Enable the overlap rule for a selected FOLDER entry and show its
        current value."""
        e = self._selected_folder_entry()
        self._combo_overlap.blockSignals(True)
        self._combo_overlap.setEnabled(e is not None)
        mode = (e.overlap_priority if e is not None and e.overlap_priority
                else terrain_index.PRIORITY_FINEST)
        idx = self._combo_overlap.findData(mode)
        self._combo_overlap.setCurrentIndex(max(idx, 0))
        self._combo_overlap.blockSignals(False)

    def _on_overlap_changed(self, _index: int) -> None:
        e = self._selected_folder_entry()
        if e is None:
            return
        mode = self._combo_overlap.currentData()
        e.overlap_priority = mode
        terrain_index.set_overlap_priority(e.source_path, mode)
        self._log_msg(f"{e.source_path}: where files overlap, "
                      f"{terrain_index.PRIORITY_LABELS[mode]}")

    def _preflight_folders(self) -> bool:
        """Re-scan every folder entry (off the GUI thread) and ask about any
        problem file the user has not answered yet. False = cancelled."""
        for e in self._layers:
            if e.layer_type != "raster" or not os.path.isdir(e.source_path or ""):
                continue
            try:
                scan = terrain_folder_scan.scan_folder(self, e.source_path)
            except RuntimeError as exc:
                QMessageBox.critical(self, "Folder could not be read",
                                     str(exc))
                return False
            if scan is None:
                return False
            answer = terrain_folder_scan.resolve_folder_problems(
                self, e.source_path, scan["index"], e.crs_overrides,
                e.decisions, ask_crs=True)
            if answer is None:
                return False
            e.crs_overrides, e.decisions = answer
        return True

    def _on_add_buildings(self):
        # Show a dialog with two clearly labeled buttons.
        msg = QMessageBox(self)
        msg.setWindowTitle("Add Buildings")
        msg.setText("How do you want to add building footprints?")
        msg.setInformativeText(
            "File: single vector file (FGB, SHP, GPKG, GeoJSON)\n"
            "Folder: directory of .fgb parts, or an ESRI .gdb"
        )
        btn_file = msg.addButton("Select File...", QMessageBox.ButtonRole.AcceptRole)
        btn_folder = msg.addButton("Select Folder...", QMessageBox.ButtonRole.AcceptRole)
        msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        msg.exec()

        clicked = msg.clickedButton()
        if clicked == btn_file:
            path, _ = QFileDialog.getOpenFileName(
                self, "Select Building File", "",
                "Vector files (*.fgb *.shp *.gpkg *.geojson);;All (*)",
            )
        elif clicked == btn_folder:
            path = QFileDialog.getExistingDirectory(
                self, "Select Buildings Folder (.fgb parts or .gdb)", ""
            )
        else:
            return
        if not path:
            return

        # Detect CRS.
        crs_id = self._detect_vector_crs(path)
        if not crs_id:
            crs_id = self._ask_crs(f"CRS unknown for:\n{path}")
            if not crs_id:
                return

        # Check if GDB — needs conversion to FGB.
        is_gdb = False
        if path.lower().endswith(".gdb") and os.path.isdir(path):
            is_gdb = True
        elif os.path.isdir(path):
            try:
                is_gdb = any(f.lower().endswith(".gdbtable")
                             for f in os.listdir(path))
            except OSError:
                pass
        if is_gdb:
            self._log_msg(f"GDB detected: {path} — will convert to FGB.")

        self._layers.append(_LayerEntry(
            layer_type="buildings", source_path=path,
            crs_authid=crs_id, priority=len(self._layers) + 1,
        ))
        self._rebuild_table()
        self._log_msg(f"Added buildings: {path}")
        self._on_stack_changed()

    def _on_download_buildings(self):
        """Add a deferred OSM building download entry.

        The actual download happens at conversion time — OpenFreeMap z14
        vector tiles over the row's extent, the source and cache Site
        Analysis uses, fetched next to the terrain download. The user sets
        the extent via 'Draw Extent for Layer' after adding.
        """
        self._layers.append(_LayerEntry(
            layer_type="buildings",
            source_path=_OSM_PLACEHOLDER,
            crs_authid="EPSG:4326",
            priority=len(self._layers) + 1,
        ))
        self._rebuild_table()
        self._log_msg(
            "Added deferred OSM building download. Select the row and set the "
            "download area with 'Draw Extent for Layer' (draw on the map) or "
            "'Extent from Layer' (reuse a loaded layer's extent, e.g. your DEM). "
            "Buildings will be downloaded when you click Convert (OpenFreeMap "
            "z14 vector tiles, the source Site Analysis uses)."
        )
        self._on_stack_changed()
        QMessageBox.information(
            self, "OSM Buildings Added",
            "A placeholder entry has been added.\n\n"
            "1. Select the new row in the table.\n"
            "2. Define the download area, either:\n"
            "   • 'Draw Extent for Layer' — draw a rectangle on the map, or\n"
            "   • 'Extent from Layer' — reuse a loaded layer's extent "
            "(e.g. your DEM).\n"
            "3. Click 'Convert' — buildings will be downloaded automatically "
            "(OpenFreeMap z14 tiles, next to the terrain download). If they "
            "cannot be downloaded, the conversion stops with the reason."
        )

    def _detect_vector_crs(self, path: str) -> Optional[str]:
        """Try to detect CRS of a vector file or directory."""
        try:
            target = path
            if os.path.isdir(path):
                # GDB directories are loadable directly by OGR.
                if path.lower().endswith(".gdb"):
                    target = path
                else:
                    for fn in sorted(os.listdir(path)):
                        if fn.lower().endswith((".fgb", ".shp", ".gpkg", ".geojson")):
                            target = os.path.join(path, fn)
                            break
            tmp = QgsVectorLayer(target, "tmp", "ogr")
            if tmp.isValid() and tmp.crs().isValid():
                return tmp.crs().authid()
        except Exception:
            pass
        return None

    def _ask_crs(self, context: str) -> Optional[str]:
        # Generic starter suggestions only — the combo is editable, and ANY
        # EPSG authid works: the converter reprojects every CRS itself.
        common = ["EPSG:4326", "EPSG:32632", "EPSG:32633",
                  "EPSG:3857", "EPSG:25832", "EPSG:25833"]
        from qgis.PyQt.QtWidgets import QInputDialog
        crs, ok = QInputDialog.getItem(
            self, "Select CRS", f"{context}\n\nSelect CRS:", common, 0, True
        )
        if not ok or not crs.strip():
            return None
        test = QgsCoordinateReferenceSystem(crs.strip())
        if not test.isValid():
            QMessageBox.critical(self, "Invalid CRS", f"'{crs}' is invalid.")
            return None
        return crs.strip()

    # ------------------------------------------------------------------
    # Layer table
    # ------------------------------------------------------------------

    def _on_move_up(self):
        row = self._table.currentRow()
        if row <= 0:
            return
        self._layers[row - 1], self._layers[row] = (
            self._layers[row], self._layers[row - 1])
        self._renumber()
        self._rebuild_table()
        self._table.selectRow(row - 1)

    def _on_move_down(self):
        row = self._table.currentRow()
        if row < 0 or row >= len(self._layers) - 1:
            return
        self._layers[row], self._layers[row + 1] = (
            self._layers[row + 1], self._layers[row])
        self._renumber()
        self._rebuild_table()
        self._table.selectRow(row + 1)

    def _selected_rows(self) -> List[int]:
        """Selected table rows, ascending (the current row if none is)."""
        rows = set()
        sel = self._table.selectionModel()
        if sel is not None:
            rows = {ix.row() for ix in sel.selectedRows()}
        if not rows:
            cur = self._table.currentRow()
            if cur >= 0:
                rows = {cur}
        return sorted(r for r in rows if 0 <= r < len(self._layers))

    def _set_extent_rows(self, extent: Optional[Dict[str, float]],
                         rows: List[int], how: str) -> None:
        """Set *extent* on *rows*, refresh the table + estimate, log it."""
        changed = _apply_extent(self._layers, extent, rows)
        if not changed:
            return
        keep = self._selected_rows()
        self._rebuild_table()
        self._reselect(keep)
        ext = extent
        self._log_msg(
            f"Extent {how} for row(s) {', '.join(str(r + 1) for r in changed)}"
            f": S={ext['south']:.4f} N={ext['north']:.4f} "
            f"W={ext['west']:.4f} E={ext['east']:.4f}")
        self._on_stack_changed()

    def _reselect(self, rows: List[int]) -> None:
        """Select *rows* again after a table rebuild."""
        from qgis.PyQt.QtCore import QItemSelectionModel
        sel = self._table.selectionModel()
        if sel is None or not rows:
            return
        self._table.clearSelection()
        for r in rows:
            if 0 <= r < self._table.rowCount():
                sel.select(self._table.model().index(r, 0),
                           QItemSelectionModel.SelectionFlag.Select
                           | QItemSelectionModel.SelectionFlag.Rows)

    def _on_apply_extent_to_all(self, _checked: bool = False,
                                row: Optional[int] = None) -> None:
        """Copy one row's extent (the current row) to every other row."""
        if row is None:
            row = self._table.currentRow()
        if not (0 <= row < len(self._layers)):
            QMessageBox.information(self, "Select Layer",
                                    "Select the row whose extent to copy.")
            return
        ext = self._layers[row].extent
        if not ext:
            QMessageBox.information(
                self, "No Extent",
                f"Row {row + 1} has no extent to copy yet. Set it with "
                f"'Draw Extent for Layer' or 'Extent from Layer'.")
            return
        others = [r for r in range(len(self._layers)) if r != row]
        self._set_extent_rows(ext, others, f"copied from row {row + 1}")

    def _on_table_context_menu(self, pos) -> None:
        """Right-click: copy / paste / apply-to-all an extent."""
        row = self._table.rowAt(pos.y())
        if row >= 0 and row not in self._selected_rows():
            self._table.selectRow(row)
        if row < 0:
            row = self._table.currentRow()
        rows = self._selected_rows()
        src = (self._layers[row].extent
               if 0 <= row < len(self._layers) else None)
        menu = QMenu(self)
        act_copy = menu.addAction("Copy extent")
        act_copy.setEnabled(bool(src))
        n = len(rows)
        act_paste = menu.addAction(
            f"Paste extent to {n} selected row{'s' if n != 1 else ''}")
        act_paste.setEnabled(bool(self._extent_clipboard) and n > 0)
        act_all = menu.addAction("Apply extent to all rows")
        act_all.setEnabled(bool(src) and len(self._layers) > 1)
        chosen = menu.exec(self._table.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == act_copy and src:
            self._extent_clipboard = dict(src)
            self._log_msg(f"Extent of row {row + 1} copied.")
        elif chosen == act_paste and self._extent_clipboard:
            self._set_extent_rows(self._extent_clipboard, rows, "pasted")
        elif chosen == act_all:
            self._on_apply_extent_to_all(row=row)

    def _on_remove(self):
        rows = self._selected_rows()
        if not rows:
            return
        for row in reversed(rows):
            del self._layers[row]
        self._renumber()
        self._rebuild_table()
        self._on_stack_changed()

    def _renumber(self):
        for i, e in enumerate(self._layers):
            e.priority = i + 1

    def _rebuild_table(self):
        self._table.setRowCount(len(self._layers))
        for i, entry in enumerate(self._layers):
            # Type
            self._set_cell(i, _COL_TYPE,
                           "Buildings" if entry.layer_type == "buildings"
                           else "Raster")
            # Source
            display = entry.source_path
            if entry.qgis_layer:
                display = entry.qgis_layer.name()
            elif len(display) > 50:
                display = "..." + display[-47:]
            self._set_cell(i, _COL_SOURCE, display, entry.source_path)

            # CRS
            self._set_cell(i, _COL_CRS, entry.crs_authid or "?")

            # Native resolution
            self._set_cell(i, _COL_NATIVE_RES,
                           f"{entry.native_res_m:.1f}m"
                           if entry.native_res_m else "-")

            # Extent
            if entry.extent:
                ext = entry.extent
                self._set_cell(
                    i, _COL_EXTENT,
                    f"{ext['south']:.2f}-{ext['north']:.2f}N "
                    f"{ext['west']:.2f}-{ext['east']:.2f}E",
                )
            else:
                self._set_cell(i, _COL_EXTENT, "not set")

        self._table.resizeColumnsToContents()

    def _set_cell(self, row, col, text, tooltip=None):
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        if tooltip:
            item.setToolTip(tooltip)
        self._table.setItem(row, col, item)

    # ------------------------------------------------------------------
    # Per-layer extent
    # ------------------------------------------------------------------

    def _on_draw_extent(self):
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self, "Select Layer",
                                    "Select a layer row first.")
            return
        self._drawing_for_layer = rows
        parent = self._dialog
        if hasattr(parent, "showMinimized"):
            parent.showMinimized()
        self._rect_tool = _RectangleDrawTool(self.iface, self._on_extent_drawn)

    def _on_extent_drawn(self, s, n, w, e):
        rows = self._drawing_for_layer
        if isinstance(rows, int):          # a single row, as it used to be
            rows = [rows]
        if rows:
            self._set_extent_rows(
                {"south": s, "north": n, "west": w, "east": e}, rows, "drawn")
        self._drawing_for_layer = None
        parent = self._dialog
        if hasattr(parent, "showNormal"):
            parent.showNormal()
            parent.raise_()
            parent.activateWindow()

    def _choose_project_layer_for_extent(self, preferred=None,
                                         whole_folder: bool = False):
        """Prompt for a loaded project layer to source an extent from.

        *preferred*, when given, is preselected in the list (e.g. the row's
        own layer) — but the user can still pick any other loaded layer.
        *whole_folder* adds :data:`_WHOLE_FOLDER` ("Whole folder (all
        files)") as the first, preselected choice — for folder rows.
        Returns the chosen QgsRasterLayer/QgsVectorLayer, ``_WHOLE_FOLDER``,
        or None if cancelled or no suitable layer is loaded.
        """
        from qgis.PyQt.QtWidgets import QInputDialog

        layers = [
            lyr for lyr in QgsProject.instance().mapLayers().values()
            if isinstance(lyr, (QgsRasterLayer, QgsVectorLayer))
        ]
        if not layers and not whole_folder:
            QMessageBox.information(
                self, "No Layers",
                "No raster or vector layers are loaded to take an extent "
                "from.\nLoad a layer (e.g. your DEM), or use 'Draw Extent "
                "for Layer' to draw the area on the map."
            )
            return None

        names = [lyr.name() for lyr in layers]
        preselect = 0
        if preferred is not None:
            try:
                preselect = layers.index(preferred)
            except ValueError:
                preselect = 0
        if whole_folder:
            names.insert(0, _WHOLE_FOLDER)
            preselect = 0
        name, ok = QInputDialog.getItem(
            self, "Extent from Layer",
            "Use the geographic extent of which layer?",
            names, preselect, False,
        )
        if not ok or not name:
            return None
        if whole_folder and name == _WHOLE_FOLDER:
            return _WHOLE_FOLDER
        for lyr in layers:
            if lyr.name() == name:
                return lyr
        return None

    def _on_set_layer_extent(self):
        rows = self._selected_rows()
        if not rows:
            QMessageBox.information(self, "Select Layer",
                                    "Select a layer row first.")
            return
        cur = self._table.currentRow()
        entry = self._layers[cur if cur in rows else rows[0]]
        folders = all(self._layers[r].layer_type == "raster"
                      and os.path.isdir(self._layers[r].source_path or "")
                      for r in rows)
        # Always let the user choose which layer's extent to use, preselecting
        # this row's own layer when it has one. This makes the button useful
        # for every row type — a building/folder row can borrow the DEM's
        # extent, and any row can be re-bounded to a different layer. Folder
        # rows also offer their own files ("Whole folder (all files)").
        layer = self._choose_project_layer_for_extent(
            preferred=entry.qgis_layer, whole_folder=folders)
        if layer is None:
            return
        if layer == _WHOLE_FOLDER:
            self._set_whole_folder_extent(rows)
            return
        ext = self._layer_wgs84_extent(layer)
        if ext is None:
            QMessageBox.warning(self, "No Extent",
                                f"'{layer.name()}' has an empty extent.")
            return
        self._set_extent_rows(ext, rows, f"from layer {layer.name()}")

    def _set_whole_folder_extent(self, rows: List[int]) -> None:
        """Each folder row's extent = all of its usable files.

        The folder is re-read through its index first (off the GUI thread,
        unchanged files are only stat'ed), so files added since Add Folder
        count too.
        """
        for r in rows:
            e = self._layers[r]
            try:
                scan = terrain_folder_scan.scan_folder(self, e.source_path)
            except RuntimeError as exc:
                QMessageBox.critical(self, "Folder could not be read",
                                     str(exc))
                return
            if scan is None:
                return
            ext = _folder_extent(scan["index"], e.decisions, e.crs_overrides)
            if ext is None:
                QMessageBox.warning(
                    self, "No Usable Files",
                    f"No usable file with known bounds in:\n{e.source_path}")
                continue
            self._set_extent_rows(ext, [r], "= whole folder")

    # ------------------------------------------------------------------
    # Estimate
    # ------------------------------------------------------------------

    def _get_output_resolutions(self) -> List[int]:
        return [r for r, cb in self._res_checks.items() if cb.isChecked()]

    def _keep_pool_tiles(self) -> bool:
        """"Keep pool tiles": the box, else the stored choice."""
        box = getattr(self, "_chk_keep_pool", None)
        return bool(box.isChecked()) if box is not None \
            else _keep_pool_tiles_setting()

    def _on_keep_pool_toggled(self, checked: bool) -> None:
        QgsSettings().setValue(_KEEP_POOL_KEY, bool(checked))
        self._update_estimate()

    # -- the estimate label: requested here, computed off the GUI thread --

    def _init_estimate(self) -> None:
        """The estimate label's request state — set up by ``__init__``, and
        on first use for a tab built without it (the tests): the request
        generation, the lock ``_est_cache`` is touched under (estimate thread
        and ``_on_run``), the single-shot debounce timer, the pending and the
        running job, the worker thread (made on first use)."""
        if getattr(self, "_est_timer", None) is not None:
            return
        if getattr(self, "_est_cache", None) is None:
            self._est_cache = {}
        self._est_generation = 0
        self._est_lock = threading.Lock()
        self._est_requested = False
        self._est_pending: Optional[_EstimateJob] = None
        self._est_running: Optional[_EstimateJob] = None
        self._est_thread: Optional[_EstimateThread] = None
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(_ESTIMATE_DEBOUNCE_MS)
        timer.timeout.connect(self._on_estimate_timer)
        self._est_timer = timer

    def _estimate_precheck(self) -> Optional[str]:
        """The label's answer when no estimate is needed — no resolution
        ticked, a raster row without extent, no raster row — else None.
        Reads no file: answered at once, on the GUI thread."""
        if not self._get_output_resolutions():
            return "Select at least one output resolution."
        without = sum(1 for e in self._layers
                      if e.layer_type != "buildings" and not e.extent)
        if without:
            return (f"{without} layer(s) have no extent set. "
                    f"Select a layer and click 'Draw Extent' or 'Extent "
                    f"from Layer'.")
        if _union_extent(self._layers) is None:
            return "Add raster layers and set extents to see estimate."
        return None

    def _update_estimate(self, *_args) -> None:
        """Refresh the estimate label — never computing on the GUI thread
        (owner, 2026-11-07: the resolution boxes lagged until the estimate
        was done). Every call makes the requests before it stale (the
        generation counter; a running estimate is told to stop, a pending
        one dropped). What needs no file is answered at once
        (:meth:`_estimate_precheck`); otherwise the label says
        :data:`_ESTIMATING_TEXT` and the single-shot timer
        (:data:`_ESTIMATE_DEBOUNCE_MS`, restarted by every call) takes the
        request (:meth:`_on_estimate_timer`)."""
        self._init_estimate()
        self._est_generation += 1
        if self._est_running is not None:
            self._est_running.superseded.set()
        self._est_pending = None
        text = self._estimate_precheck()
        if text is not None:
            self._est_requested = False
            self._est_timer.stop()
            self._lbl_estimate.setText(text)
            return
        self._lbl_estimate.setText(_ESTIMATING_TEXT)
        self._est_requested = True
        self._est_timer.start()

    def _estimate_job(self) -> _EstimateJob:
        """The current request as plain values (GUI thread): what the two
        estimates read of the rows (:func:`_plain_entry`), the ticked
        resolutions, output directory and both boxes."""
        return _EstimateJob(
            self._est_generation, [_plain_entry(e) for e in self._layers],
            self._get_output_resolutions(), self._get_output_dir(),
            self._chk_overwrite.isChecked(), self._keep_pool_tiles(),
            self._est_cache, self._est_lock)

    def _on_estimate_timer(self) -> None:
        """The debounce ran out: take the request's inputs now and run it —
        at once when no estimate runs, else as THE pending job (replacing an
        older one), started when the running one has ended."""
        if not self._est_requested:
            return
        self._est_requested = False
        job = self._estimate_job()
        if self._est_running is not None:
            self._est_pending = job
            return
        self._start_estimate(job)

    def _start_estimate(self, job: _EstimateJob) -> None:
        """Run *job* in the tab's :class:`_EstimateThread` (made on first
        use, started again for every job; its result comes back through
        :meth:`_on_estimated`, its end through :meth:`_on_estimate_finished`)."""
        thread = self._est_thread
        if thread is None:
            thread = self._est_thread = _EstimateThread()
            thread.estimated.connect(self._on_estimated)
            thread.finished.connect(self._on_estimate_finished)
        self._est_running = job
        thread.job = job
        _LIVE_ESTIMATES.add(thread)
        thread.start()

    def _on_estimate_finished(self, *_args) -> None:
        """The estimate thread has ended (after its result): start the
        pending job — unless a newer request made it stale (that one starts
        from its own timer)."""
        _LIVE_ESTIMATES.discard(self._est_thread)
        self._est_running = None
        job, self._est_pending = self._est_pending, None
        if job is not None and job.generation == self._est_generation:
            self._start_estimate(job)

    def flush_estimate(self) -> None:
        """Run the requested estimate NOW, in this thread, and show it — the
        synchronous hook of the tests (the stub QThread never runs). The
        debounce is skipped and the inputs are taken as the timer takes
        them; a job waiting for the running one is run instead when no
        newer request is open. Nothing requested → nothing done."""
        self._init_estimate()
        if self._est_requested:
            self._est_requested = False
            self._est_timer.stop()
            job = self._estimate_job()
        elif self._est_pending is not None:
            job, self._est_pending = self._est_pending, None
        else:
            return
        self._on_estimated(_estimate_answer(job))

    def _on_estimated(self, result: _EstimateResult) -> None:
        """An estimate answered (the thread's signal, or
        :meth:`flush_estimate`): shown only while it answers the latest
        request — a stale or stopped one is dropped."""
        if (result.superseded
                or result.job.generation != getattr(self, "_est_generation",
                                                    None)):
            return
        self._show_estimate(result)

    def _show_estimate(self, result: _EstimateResult) -> None:
        """The label for *result*: ``.abt tiles: N | Est. disk: X MB (Y
        GB)`` (orange past 50 GB, red past 200 GB), then one line per drive
        (red when short). A failure is the ``disk estimate failed: …``
        warning in the QGIS log: without the disk need the drive lines are
        missing; without the tile count the label says it failed."""
        for msg in result.errors:
            QgsMessageLog.logMessage(f"disk estimate failed: {msg}", TAG,
                                     Qgis.MessageLevel.Warning)
        if result.tiles is None or result.mb is None:
            why = result.errors[0] if result.errors else "?"
            self._lbl_estimate.setText(f"Estimate failed: {why}")
            return
        # Priced what the run will actually build: the worker skips tiles
        # outside every layer's extent, and tiles that already exist unless
        # "Rebuild existing tiles" is on.
        total_tiles, total_mb = result.tiles, result.mb
        colour = "red" if total_mb > 200_000 else (
            "darkorange" if total_mb > 50_000 else "black")
        text = (f'<span style="color:{colour}">'
                f".abt tiles: {total_tiles:,} | "
                f"Est. disk: {total_mb:,} MB ({total_mb / 1024:.1f} GB)"
                f"</span>")
        # Per drive: output tiles + pool tiles still to fetch + the raw
        # source tiles their download caches + temp copies, against what is
        # free there (red when it would not fit). The pool tiles a run
        # downloads exist while it runs, so they stay in the need; with
        # "Keep pool tiles" off they go at its end.
        removed = (" (pool tiles removed at the end)"
                   if not result.job.keep_pool else "")
        est = result.need
        for need in (est[1] if est else ()):
            if not need.need:
                continue
            c = "red" if need.short else "black"
            note = removed if _NEED_POOL in need.labels else ""
            text += (f'<br><span style="color:{c}">'
                     f"{output_estimate.format_drive_need(need)}{note}"
                     f"</span>")
        self._lbl_estimate.setText(text)

    def _stop_estimate(self) -> None:
        """Closing: no request is taken any more and a running estimate is
        told to stop; nothing waits for it (it stays referenced in
        :data:`_LIVE_ESTIMATES` until it ends)."""
        timer = getattr(self, "_est_timer", None)
        if timer is None:
            return
        timer.stop()
        self._est_requested = False
        self._est_pending = None
        if self._est_running is not None:
            self._est_running.superseded.set()

    # -- "Generate tiles at": the Auto pick --

    def _on_res_toggled(self, *_args) -> None:
        """A "Generate tiles at" box changed. By hand: the boxes stop
        following the stack until Auto is pressed (``_res_user_touched``);
        the estimate is refreshed (off the GUI thread). The Auto rule's own
        ticks (``_res_applying``) change neither — its caller refreshes once."""
        if getattr(self, "_res_applying", False):
            return
        self._res_user_touched = True
        self._update_estimate()

    def _apply_auto_resolutions(self, log: bool = False) -> List[int]:
        """Tick exactly :func:`auto_output_resolutions` of the stack (the
        rule's own change: auto mode stays on) and name the result in the
        Auto button's tooltip; one log line (:func:`_auto_resolutions_line`)
        when the ticks changed, or always with *log* (the Auto button). The
        estimate is not refreshed here."""
        res = auto_output_resolutions(self._layers)
        changed = False
        self._res_applying = True
        try:
            for r, cb in self._res_checks.items():
                want = r in res
                if bool(cb.isChecked()) != want:
                    cb.setChecked(want)
                    changed = True
        finally:
            self._res_applying = False
        self._update_auto_tip()
        if changed or log:
            self._log_msg(_auto_resolutions_line(self._layers))
        return res

    def _update_auto_tip(self) -> None:
        """The Auto button's tooltip: the rule and this stack's result."""
        btn = getattr(self, "_btn_auto_res", None)
        if btn is not None:
            btn.setToolTip(_auto_resolutions_tip(self._layers))

    def _on_stack_changed(self) -> None:
        """A row was added or removed, or an extent set: in auto mode (no
        box ticked by hand since the last Auto) the boxes follow the stack
        (:meth:`_apply_auto_resolutions`); the estimate is refreshed."""
        if getattr(self, "_res_user_touched", False):
            self._update_auto_tip()
        else:
            self._apply_auto_resolutions()
        self._update_estimate()

    def _on_auto_resolutions(self, *_args) -> None:
        """Auto: tick the rule's resolutions for the stack, log them, and
        follow the stack again on every change until a box is ticked by
        hand; the estimate is refreshed."""
        self._res_user_touched = False
        self._apply_auto_resolutions(log=True)
        self._update_estimate()

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _on_browse_output(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Output Directory",
            self._edit_output.text() or self._get_output_dir(),
        )
        if folder:
            self._edit_output.setText(folder)

    def _get_output_dir(self) -> str:
        text = self._edit_output.text().strip()
        # The DEFAULT follows the terrain-cache setting (waveshed/cache_dir);
        # it used to hardcode ~/.aether/cache and ignore a relocated cache.
        # A path the user typed always wins.
        return (os.path.expanduser(text) if text
                else terrain_adapter.get_cache_dir())

    def refresh_settings(self) -> None:
        """Re-read what this tab derives from QgsSettings (cheap, idempotent).

        Called by the main dialog when the Settings tab saves. The output
        directory DEFAULT (placeholder + empty-field fallback) follows
        ``waveshed/cache_dir``; a user-typed output path is user state and
        stays untouched. The estimate is recomputed because its
        skip-existing scan prices against the (possibly relocated) default.
        Download connections/passes and binary discovery are read per run,
        never cached here.
        """
        self._edit_output.setPlaceholderText(terrain_adapter.get_cache_dir())
        self._update_estimate()

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def _on_run(self):
        # Validate layers.
        if not self._layers:
            QMessageBox.warning(self, "No Layers", "Add layers first.")
            return
        rasters = [e for e in self._layers if e.layer_type == "raster"]
        if not rasters:
            QMessageBox.warning(self, "No Raster", "Need at least one DEM.")
            return

        # Check extents.
        missing = [e for e in rasters if not e.extent]
        if missing:
            QMessageBox.warning(
                self, "Missing Extent",
                f"{len(missing)} raster layer(s) have no extent.\n"
                f"Select each and use 'Draw Extent' or 'Extent from Layer'."
            )
            return
        # An OSM buildings row needs its download area — without one (or
        # one off the terrain) it would download nothing, silently.
        osm_problem = _osm_rows_problem(self._layers)
        if osm_problem:
            QMessageBox.warning(self, "OSM Buildings Extent", osm_problem)
            return

        # Check output resolutions.
        out_res = self._get_output_resolutions()
        if not out_res:
            QMessageBox.warning(
                self, "No Output Resolution",
                "Select at least one output resolution checkbox."
            )
            return

        # Finer than native, per source — checked at run start for the
        # checked resolutions (the removed per-row editor asked on every
        # edit): one line per source, logged with the run. The confirmation
        # dialog asks only when NO source has that detail (an output finer
        # than the finest input), and lists the lines.
        upsampling = _upsampling_warnings(rasters, out_res)
        finest_input = min(
            (e.native_res_m for e in rasters if e.native_res_m),
            default=None,
        )
        if finest_input:
            upsampled = [r for r in out_res
                         if r < finest_input * _FINER_THAN_SOURCE]
            if upsampled:
                r = QMessageBox.warning(
                    self, "Upsampling Warning",
                    f"Output resolution(s) {upsampled}m are finer than "
                    f"the finest input (~{finest_input:.1f}m).\n\n"
                    + "".join(f"• {line}\n" for line in upsampling)
                    + "\nThe output will be interpolated — no additional "
                    "detail will be gained.\n\nProceed?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if r != QMessageBox.StandardButton.Yes:
                    return

        # Building Z validation.
        for entry in self._layers:
            if entry.layer_type != "buildings" or not entry.qgis_layer:
                continue
            if isinstance(entry.qgis_layer, QgsVectorLayer):
                if not QgsWkbTypes.hasZ(entry.qgis_layer.wkbType()):
                    r = QMessageBox.warning(
                        self, "Buildings Without 3D",
                        f"'{entry.qgis_layer.name()}' has no Z coordinates.\n"
                        f"2D buildings will be silently ignored.\nContinue?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No,
                    )
                    if r != QMessageBox.StandardButton.Yes:
                        return

        # Folder entries: files added/changed since Add Folder are read now
        # (off the GUI thread) and any new problem file is asked about.
        if not self._preflight_folders():
            self._lbl_status.setText("Cancelled.")
            return

        output_dir = self._get_output_dir()

        # The disk must hold what the run writes (output tiles, pool tiles
        # still to fetch, the raw source tiles their download caches, temp
        # copies — per drive, 2 GB kept free): refused
        # with the same numbers as the estimate label, before anything is
        # rendered or fetched. The worker re-checks with exact numbers. The
        # label's cache is shared with its estimate thread: under its lock
        # (a running estimate is waited for, then its infos are reused).
        self._init_estimate()
        try:
            with self._est_lock:
                est = _estimate_disk_need(
                    self._layers, out_res, output_dir,
                    self._chk_overwrite.isChecked(),
                    cache=self._est_cache)
        except Exception as exc:  # noqa: BLE001 — an estimate never blocks
            est = None
            QgsMessageLog.logMessage(f"disk estimate failed: {exc}", TAG,
                                     Qgis.MessageLevel.Warning)
        short = output_estimate.short_drives(est[1]) if est else []
        if short:
            msg = output_estimate.refusal_text(short, "this conversion")
            QMessageBox.critical(self, "Not enough disk space", msg)
            self._lbl_status.setText("Not enough disk space.")
            return

        self._log.clear()
        self._log_msg("Starting conversion...")
        for line in upsampling:
            self._log_msg(f"WARNING: {line}")

        # Rendered servers must be exported through QGIS on this thread
        # before the worker can touch them — per TILE, so enumerate the
        # run's grid now (the same _snap_bbox/_enumerate_tiles the worker
        # repeats). Tiles that will be skipped as existing are not rendered.
        _combined, render_jobs = _pending_render_jobs(
            self._layers, out_res, output_dir,
            self._chk_overwrite.isChecked())

        self._log_msg("Preparing layers (rendered servers render per tile)...")
        # ONE bar for the whole run, weighted by the work each phase really
        # does (core/run_progress.py) — estimated up front here, refined by
        # the worker as soon as each total is known.
        self._run_model = run_progress.RunProgress()
        _estimate_run_work(self._run_model, self._layers, render_jobs)
        self._run_model.set_phase("Preparing layers")
        self._progress.setValue(0)
        self._progress_timer.start()
        try:
            resolved = resolve_sources_with_progress(
                self._layers, self, render_jobs=render_jobs,
                on_progress=lambda _frac: self._refresh_progress(),
                progress_model=self._run_model,
            )
        except RuntimeError as exc:
            # Hard error (render failure, unloaded layer): blocking dialog,
            # nothing runs.
            self._progress_timer.stop()
            self._log_msg(f"ERROR: {exc}")
            QMessageBox.critical(self, "Layer preparation failed", str(exc))
            self._lbl_status.setText("Failed.")
            return
        if resolved is None:
            self._progress_timer.stop()
            self._log_msg("Cancelled while preparing layers.")
            self._lbl_status.setText("Cancelled.")
            return

        self._set_running(True)
        self._run_reported = False
        self._worker = worker = _MapConverterWorker(
            self._layers, output_dir, out_res, resolved, self,
            overwrite=self._chk_overwrite.isChecked(),
            progress_model=self._run_model,
            keep_pool_tiles=self._keep_pool_tiles(),
        )
        worker.progress.connect(self._on_worker_progress)
        worker.status.connect(self._on_worker_status)
        worker.log_line.connect(self._log_msg)
        worker.finished_ok.connect(self._on_done)
        worker.finished_err.connect(self._on_error)
        # A cancelled worker returns without emitting either signal above;
        # the thread's own end is what gives Convert back then.
        worker.finished.connect(lambda: self._on_worker_finished(worker))
        worker.start()

    # ------------------------------------------------------------------
    # Worker callbacks
    # ------------------------------------------------------------------

    def _refresh_progress(self) -> None:
        """Bar + label from the run model (timer, and after every signal).

        The bar only ever moves forward here: the model is monotonic, and a
        queued worker signal that arrives after a timer tick cannot pull it
        back either.
        """
        model = self._run_model
        if model is None:
            return
        pct, label = model.snapshot()
        if pct > self._progress.value():
            self._progress.setValue(pct)
        if label:
            self._lbl_status.setText(label)

    def _on_worker_progress(self, pct: int) -> None:
        if pct > self._progress.value():
            self._progress.setValue(pct)
        self._refresh_progress()

    def _on_worker_status(self, text: str) -> None:
        if self._run_model is None:
            self._lbl_status.setText(text)
        else:
            self._refresh_progress()

    def _on_done(self, path):
        self._run_reported = True
        self._set_running(False)
        QMessageBox.information(self, "Done", f"Tiles written to:\n{path}")

    def _on_error(self, msg):
        self._run_reported = True
        if self._run_model is not None:
            self._run_model.stop("Failed.")
        self._set_running(False)
        QMessageBox.critical(self, "Failed", msg)

    def _on_worker_finished(self, worker) -> None:
        """QThread.finished of *worker*: a run neither done nor failed was
        cancelled — stop the model and give Convert back.

        A late signal of an older worker must not touch a newer run.
        """
        if worker is not self._worker or self._run_reported:
            return
        if self._run_model is not None:
            self._run_model.stop("Cancelled.")
        self._set_running(False)

    def _on_cancel(self):
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            if self._run_model is not None:
                self._run_model.set_phase("Cancelling...")
            self._lbl_status.setText("Cancelling...")

    def _set_running(self, running):
        self._btn_run.setEnabled(not running)
        self._btn_cancel.setEnabled(running)
        # The bar is reset when a run STARTS (_on_run), before the render
        # phase moves it — resetting here, after that phase, walked it back.
        if not running:
            self._progress_timer.stop()
            self._refresh_progress()

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------

    def _log_msg(self, msg: str):
        self._log.appendPlainText(msg)
        QgsMessageLog.logMessage(msg, TAG, Qgis.MessageLevel.Info)

    # ------------------------------------------------------------------
    # Terrain inspector
    # ------------------------------------------------------------------

    def _on_inspect_abt(self):
        """Load a folder of .abt tiles as a QGIS raster layer — nothing of
        it on the GUI thread: the folder's resolutions from the header index
        (``abt_inspect.plan_folder``), the choice of resolutions
        (:func:`_ask_inspect_choice`; default the coarsest one covering the
        folder), then the mosaic of ONLY those tiles at most
        ``abt.INSPECT_MAX_DIM`` px, with internal overviews
        (``abt.mosaic_to_geotiff``), behind the cancellable progress dialog
        (``terrain_folder_scan.run_task``). It used to mosaic every tile at
        the finest resolution here, synchronously: 2,438 tiles, ~60 GB of
        reads, a frozen QGIS."""
        folder = QFileDialog.getExistingDirectory(
            self, "Select .abt Tile Folder",
            self._get_output_dir(),
        )
        if not folder:
            return
        from ..core import abt, abt_inspect

        try:
            plan = terrain_folder_scan.run_task(
                self, "Reading the .abt tile headers",
                lambda _progress, cancelled: abt_inspect.plan_folder(
                    folder, cancelled))
        except RuntimeError as exc:
            self._log_msg(f"Inspect failed: {exc}")
            QMessageBox.critical(self, "Inspect Failed", str(exc))
            return
        if plan is None:
            self._log_msg("Inspect .abt cancelled.")
            return
        if not plan.groups:
            QMessageBox.warning(self, "No .abt Files",
                                f"No readable .abt files found in:\n{folder}")
            return
        self._log_msg(
            f"Inspecting {plan.tile_count:,} .abt tile(s) in {folder}: "
            + ", ".join(f"{g.res_m} m × {g.count:,}" for g in plan.groups)
            + (f"; {len(plan.unreadable):,} without a readable header"
               if plan.unreadable else ""))
        chosen = _ask_inspect_choice(self, plan)
        if not chosen:
            return
        headers = abt_inspect.chosen_headers(plan, chosen)
        out_tif = _inspect_output_path(folder, chosen)
        notes: List[str] = []

        def work(progress, cancelled):
            try:
                return abt.mosaic_to_geotiff(
                    folder, out_tif, max_dim=abt.INSPECT_MAX_DIM,
                    on_log=notes.append, should_cancel=cancelled,
                    headers=headers, overviews=True, progress=progress)
            except abt.MosaicCanceled:
                return None

        try:
            result = terrain_folder_scan.run_task(
                self, "Building the terrain preview", work)
        except RuntimeError as exc:
            for line in notes:
                self._log_msg(line)
            self._log_msg(f"Inspect failed: {exc}")
            QMessageBox.critical(self, "Inspect Failed", str(exc))
            return
        for line in notes:
            self._log_msg(line)
        if result is None:
            self._log_msg("Inspect .abt cancelled — no preview was written.")
            return
        res_text = " + ".join(f"{r} m" for r in sorted(chosen))
        self._log_msg(
            f"Inspect .abt: {result.tiles_read:,} tile(s) read ({res_text}), "
            f"{result.bytes_read / 1024 ** 2:,.1f} MB read in "
            f"{result.seconds:.1f} s → {out_tif} ({result.grid.width:,} × "
            f"{result.grid.height:,} px"
            + (f", overviews {result.overviews[0]}–{result.overviews[-1]}"
               if result.overviews else "") + ")")

        # Add to QGIS.
        layer_name = (f"Terrain: {os.path.basename(os.path.normpath(folder))}"
                      f" ({res_text})")
        rl = QgsRasterLayer(out_tif, layer_name)
        if rl.isValid():
            # Local import: result_loader pulls heavy rendering classes that
            # the test stubs don't provide; only needed on this code path.
            from ..core.result_loader import (
                GROUP_TERRAIN,
                add_layer_to_project,
            )
            add_layer_to_project(rl, GROUP_TERRAIN)
            self._log_msg(f"Added terrain layer: {layer_name}")
        else:
            self._log_msg("Failed to load mosaic as QGIS layer.")
            QMessageBox.warning(self, "Load Failed",
                                f"Created {out_tif} but could not load.")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cancel_worker(self):
        self._stop_estimate()
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(5000)
        if self._rect_tool:
            self._rect_tool.cleanup()
            self._rect_tool = None
