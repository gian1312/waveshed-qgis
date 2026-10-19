"""Map Converter tab -- fuse multiple raster/vector layers into .abt tiles.

Embeddable QWidget for the main AETHER dialog.  Lets users build a
prioritised layer stack (base DEM, high-res overlays, buildings), choose
per-layer extents and target resolutions, and batch-convert everything
to .abt terrain tiles via ``aether_converter ingest``.

The parent dialog must provide ``self.iface`` (QgisInterface).
"""

from __future__ import annotations

import json
import math
import os
import platform
import re
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import QThread, QTimer, pyqtSignal, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
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
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsMapLayerComboBox, QgsRubberBand

from ..core import run_progress, terrain_adapter, terrain_index
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

_SUBPROCESS_FLAGS = (
    subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
)

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


def _snap_bbox(bbox: Dict[str, float], resolutions: List[int]) -> Dict[str, float]:
    """Snap *bbox* outward onto the finest tile grid the run will use.

    Grid size comes from ``terrain_adapter._subtile_degrees`` — the extent
    ladder plus the u16 row-stride guard — the same authority the analysis
    path and the engine's ``plan`` subcommand reproduce.
    """
    finest = min(_subtile_degrees(r) for r in resolutions)
    return {
        "south": math.floor(bbox["south"] / finest) * finest,
        "north": math.ceil(bbox["north"] / finest) * finest,
        "west": math.floor(bbox["west"] / finest) * finest,
        "east": math.ceil(bbox["east"] / finest) * finest,
    }


def _enumerate_tiles(
    bbox: Dict[str, float], resolutions: List[int],
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
    """
    tiles: List[Tuple[int, Dict[str, Any]]] = []
    for res in sorted(resolutions):
        ext = _subtile_degrees(res)
        lat = math.floor(bbox["south"] / ext) * ext
        while lat < bbox["north"]:
            lon = math.floor(bbox["west"] / ext) * ext
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
) -> Tuple[int, int]:
    """Return ``(tile_count, total_mb)`` for the tiles a run would build.

    Prices exactly what the worker will do: the same outward-snapped extent,
    and — unless *overwrite* — without the tiles already sitting in
    *output_dir*, which the worker skips.
    """
    if not resolutions:
        return 0, 0
    tiles = _enumerate_tiles(_snap_bbox(bbox, resolutions), resolutions)
    if output_dir and not overwrite:
        # One listing rather than a stat per tile: this runs on every checkbox
        # toggle and a big area is tens of thousands of tiles.
        try:
            existing = set(os.listdir(output_dir))
        except OSError:
            existing = set()
        # `filename` from _tile_params is the canonical name the worker
        # writes (and skips on) — the estimate must consult the same one.
        tiles = [(res, t) for res, t in tiles
                 if t["filename"] not in existing]
    return len(tiles), _estimate_abt_disk_mb([t for _res, t in tiles])


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
#: mtime from the folder index). A tile whose inputs changed is rebuilt even
#: without "Rebuild existing tiles"; a tile with no record (built before the
#: manifest existed, or from a rendered service) keeps the old rule.
_OUTPUT_MANIFEST = ".waveshed_tiles.json"


def _read_output_manifest(output_dir: str) -> Dict[str, str]:
    try:
        with open(os.path.join(output_dir, _OUTPUT_MANIFEST), "r",
                  encoding="utf-8") as fh:
            doc = json.load(fh)
        return dict(doc.get("tiles") or {})
    except (OSError, ValueError, AttributeError):
        return {}


def _write_output_manifest(output_dir: str, updates: Dict[str, Optional[str]]
                           ) -> None:
    tiles = _read_output_manifest(output_dir)
    for name, fp in updates.items():
        if fp:
            tiles[name] = fp
        else:
            tiles.pop(name, None)
    try:
        with open(os.path.join(output_dir, _OUTPUT_MANIFEST), "w",
                  encoding="utf-8") as fh:
            json.dump({"schema": 1, "tiles": tiles}, fh)
    except OSError:
        pass


def _stack_tile_fingerprints(expected, resolved_entries, buildings_path
                             ) -> Dict[str, Optional[str]]:
    """Per output tile, a fingerprint of everything in the stack that feeds
    it (None for every tile when a rendered entry is in the stack — a
    server answer has no file to fingerprint)."""
    import hashlib
    if any(e.get("kind") == "rendered" for e in resolved_entries):
        return {t["filename"]: None for _r, t in expected}
    btag = ""
    if buildings_path:
        try:
            btag = terrain_adapter.source_fingerprint(buildings_path)
        except Exception:  # noqa: BLE001
            btag = buildings_path
    out: Dict[str, Optional[str]] = {}
    for res, tile in expected:
        sub = _subtile_degrees(res)
        tb = {"north": tile["ul_lat"], "south": tile["ul_lat"] - sub,
              "west": tile["ul_lon"], "east": tile["ul_lon"] + sub}
        parts = []
        for ent in resolved_entries:
            if ent.get("kind") == "files":
                parts.append(terrain_adapter.tile_source_fingerprint(
                    ent["infos"], tb, tile["exact_res_m"]))
            elif ent.get("kind") == "xyz":
                parts.append(terrain_adapter.source_identity(ent.get("uri", "")))
        parts.append(btag)
        out[tile["filename"]] = hashlib.md5(
            "\n".join(parts).encode("utf-8"), usedforsecurity=False).hexdigest()
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
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Ingest jobs for one Map Converter run; ``(jobs, temp files)``.

    One job per enumerated tile (minus the already-existing ones unless
    *overwrite*, or unless the tile is in *rebuild* — its inputs changed
    since it was built, see ``_stack_tile_fingerprints``). ``sources[]`` holds, in UI stack order (entry 0 = highest
    priority), each resolved entry's per-tile contribution:

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
    for res, tile in expected:
        fn = tile["filename"]
        out = os.path.join(output_dir, fn)
        if (not overwrite and os.path.exists(out)
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
                s, temps = build_tile_sources(
                    ent["infos"], tile_bbox, tmp_dir,
                    tag=f"mc_{os.getpid()}_{idx}_{fn}",
                    output_res_m=tile["exact_res_m"])
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
        if buildings_path:
            job["buildings_file"] = buildings_path
        jobs.append(job)
    return jobs, temp_tifs


def _xyz_download_plan(
    entries: List[Dict[str, Any]],
    expected: List[Tuple[int, Dict[str, Any]]],
) -> List[Tuple[int, Dict[str, Any], int, List[Dict[str, Any]]]]:
    """``[(entry index, entry, res, specs)]`` — one shared-pool download call
    per XYZ entry and resolution, the tiles overlapping the entry's extent.
    The ONE enumeration both the download and its progress estimate use."""
    plan = []
    for k, ent in enumerate(entries):
        for res in sorted({r for r, _t in expected}):
            jobs = _jobs_overlapping_extent(
                [(r, t) for r, t in expected if r == res], ent.get("extent"))
            specs = [t for _r, t in jobs]
            if specs:
                plan.append((k, ent, res, specs))
    return plan


def _download_key(k: int, res: int) -> Tuple[str, int, int]:
    return ("download", k, res)


def _estimate_download_work(model: Any, entries: List[Dict[str, Any]],
                            expected: List[Tuple[int, Dict[str, Any]]],
                            pool_check: bool) -> None:
    """Register every download call's source-tile count on *model*.

    *pool_check* False (GUI thread, before the run): pure geometry, every
    tile counted. True (worker): tiles already in the pool cost nothing —
    the same count the downloader's own progress then reports.
    """
    for k, ent, res, specs in _xyz_download_plan(entries, expected):
        model.set_work(_download_key(k, res),
                       terrain_adapter.estimate_pool_download_units(
                           ent["uri"], specs, pool_check=pool_check))


def _convert_work(tiles: List[Dict[str, Any]]) -> float:
    """Convert-phase units of output tiles (``size_px`` each)."""
    return sum(run_progress.tile_blocks(t["size_px"]) for t in tiles) \
        * run_progress.CONVERT_UNITS_PER_BLOCK


def _render_tile_work(tile: Dict[str, Any]) -> float:
    return (run_progress.tile_blocks(tile["size_px"])
            * run_progress.RENDER_UNITS_PER_BLOCK)


class _DownloadProgressSink:
    """``progress_cb`` for one ``ensure_pool_tiles`` call of a run.

    Callable as the plain ``(frac, label)`` callback, plus the
    ``download_event(info)`` hook ``_try_rust_download`` feeds the source
    tile counts into: they drive the run's :class:`run_progress.RunProgress`
    slot for this call, so the whole-run bar counts real tiles, retry passes
    included.
    """

    def __init__(self, model: Any, key: Any, res: int, call_no: int,
                 n_calls: int, outer: Any = None) -> None:
        self._model = model
        self._key = key
        self._res = res
        self._tag = f" [{call_no}/{n_calls}]" if n_calls > 1 else ""
        self._outer = outer
        self._events = False

    def __call__(self, frac: float, label: str) -> None:
        if self._model is not None and not self._events:
            # A downloader that sends no events: the fraction still moves.
            self._model.set_done(self._key, frac * self._model.work(self._key))
        if self._outer is not None:
            self._outer(frac, label)

    def download_event(self, info: Dict[str, Any]) -> None:
        self._events = True
        m = self._model
        if m is None:
            return
        m.set_work(self._key, info["total_units"])
        m.set_done(self._key, info["done_units"],
                   ceiling=info.get("ceiling_units"))
        phase = (f"Downloading terrain {self._res} m"
                 f"{self._tag} z{info.get('zoom', '?')}")
        m.set_phase(phase, {
            "pass": info.get("pass"), "passes": info.get("max_passes"),
            "done": info.get("pass_done"), "total": info.get("pass_total"),
            "unit": "source tiles", "mbps": info.get("mbps"),
            "errors": info.get("errors"),
        }, reset=info.get("event") == "pass_start")


def _download_xyz_entries(
    entries: List[Dict[str, Any]],
    expected: List[Tuple[int, Dict[str, Any]]],
    progress_cb: Any = None,
    should_cancel: Any = None,
    on_start: Any = None,
    progress_model: Any = None,
) -> None:
    """Fill each XYZ entry's ``["pool"]`` via the shared pool downloader.

    Per entry, per resolution: the enumerated tiles overlapping the entry's
    extent go through ``terrain_adapter.ensure_pool_tiles`` (same pool cache
    and gap-repair retries as Site Analysis). *progress_cb(frac, label)*
    receives the aggregate download fraction across ALL entries and
    resolutions; *progress_model* (a ``RunProgress``) gets the real source
    tile counts per call (slot ``("download", entry, res)``, see
    :func:`_estimate_download_work`). Failures propagate (RuntimeError) — no
    render fallback.
    """
    plan = _xyz_download_plan(entries, expected)
    for ent in entries:
        ent["pool"] = {}

    total = sum(len(specs) for _k, _e, _r, specs in plan) or 1
    done = 0
    for call_no, (k, ent, res, specs) in enumerate(plan, start=1):
        def cb(frac: float, label: str, _done=done, _n=len(specs)) -> None:
            if progress_cb is not None:
                progress_cb((_done + frac * _n) / total, label)

        key = _download_key(k, res)
        sink = _DownloadProgressSink(progress_model, key, res, call_no,
                                     len(plan), cb)
        ent["pool"][res] = terrain_adapter.ensure_pool_tiles(
            ent["uri"], specs, res, progress_cb=sink,
            should_cancel=should_cancel, on_start=on_start)
        if progress_model is not None:
            # A pool hit sends nothing; either way this call is over.
            progress_model.finish(key)
        done += len(specs)
        if progress_cb is not None:
            progress_cb(done / total,
                        f"Terrain download: {done}/{total} tiles ready")


# ---------------------------------------------------------------------------
# Layer data model
# ---------------------------------------------------------------------------

class _LayerEntry:
    """One layer in the converter stack."""
    __slots__ = (
        "layer_type", "source_path", "qgis_layer", "crs_authid",
        "native_res_m", "target_resolutions", "extent", "priority",
        "crs_overrides", "decisions", "overlap_priority",
    )

    def __init__(
        self,
        layer_type: str = "raster",
        source_path: str = "",
        qgis_layer: Any = None,
        crs_authid: str = "EPSG:4326",
        native_res_m: Optional[float] = None,
        target_resolutions: Optional[List[int]] = None,
        extent: Optional[Dict[str, float]] = None,
        priority: int = 1,
    ) -> None:
        self.layer_type = layer_type
        self.source_path = source_path
        self.qgis_layer = qgis_layer
        self.crs_authid = crs_authid
        self.native_res_m = native_res_m
        self.target_resolutions = target_resolutions or [30]
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
    XYZ downloads in source tiles (``("download", k, res)``, geometry only —
    no pool stat on the GUI thread; the worker refines it), and the convert
    phase per output tile (``("convert",)``). Phases with nothing to do get
    no weight. *render_jobs* is the run's pending ``(res, tile)`` list.
    """
    xyz: List[Dict[str, Any]] = []
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

    The jobs are the run's enumerated grid minus the tiles that will be
    skipped as existing — rendered servers must not be exported for tiles
    nothing will build. ``(None, [])`` when no raster entry has an extent.
    """
    combined = _union_extent(entries)
    if combined is None:
        return None, []
    jobs = _enumerate_tiles(_snap_bbox(combined, out_res), out_res)
    if not overwrite:
        jobs = [
            (r, t) for r, t in jobs
            if not os.path.exists(os.path.join(output_dir, t["filename"]))
        ]
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


def _kill_proc(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:
        pass


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


class _MapConverterWorker(QThread):
    """Background worker — heavy I/O, terrain download, converter run.

    The main thread passes lightweight config plus the pre-resolved
    acquisition dicts (rendered servers were already exported per tile on
    the main thread); this worker handles:
    - Per-file source infos for local files/folders (CRS detection — never
      reprojection: the converter samples each source in its own CRS)
    - XYZ terrain download via the shared pool
      (terrain_adapter.ensure_pool_tiles — same cache as Site Analysis)
    - Downloading OSM buildings via Overpass API
    - Converting GDB/SHP/GPKG → FGB
    - Building the ingest job JSON (sources[] per tile, stack order)
    - Running the converter through the shared streaming runner
    """

    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    log_line = pyqtSignal(str)
    finished_ok = pyqtSignal(str)
    finished_err = pyqtSignal(str)

    def __init__(self, layers, output_dir, out_res, resolved_sources,
                 parent=None, overwrite=False, progress_model=None):
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
        self._proc = None
        self._temp_tifs: List[str] = []

    def cancel(self):
        self._canceled = True
        if self._proc:
            _kill_proc(self._proc)

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

    def run(self):
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
            building_entries = [e for e in self._layers
                                if e["layer_type"] == "buildings"]

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

            # ---- Phase 2: Build tile job list ----
            if self._canceled:
                return
            self._say("Building tile list...")
            self._log("Phase 2: Building .abt tile jobs...")

            combined = _union_extent(self._layers)
            if combined is None:
                self.finished_err.emit("No layer has an extent set.")
                return

            # Snap to grid, then enumerate — the same two calls the pre-run
            # estimate makes, so the tile count it showed is the one built.
            bbox = _snap_bbox(combined, self._out_res)
            expected = _enumerate_tiles(bbox, self._out_res)

            try:
                converter_exe = find_binary("aether_converter")
            except RuntimeError as exc:
                self.finished_err.emit(str(exc))
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

            # Tiles whose inputs changed since they were built: rebuilt even
            # without "Rebuild existing tiles" (a changed or added file in a
            # folder used to leave the old pixels in place, silently).
            tile_fps = _stack_tile_fingerprints(expected, all_entries,
                                                buildings_path)
            rebuild: set = set()
            unknown = 0
            if not self._overwrite:
                manifest = _read_output_manifest(self._output_dir)
                for _r, t in expected:
                    fn = t["filename"]
                    cur = tile_fps.get(fn)
                    if cur is None or not os.path.exists(
                            os.path.join(self._output_dir, fn)):
                        continue
                    if fn not in manifest:
                        unknown += 1
                    elif manifest[fn] != cur:
                        rebuild.add(fn)
            if rebuild:
                self._log(f"  {len(rebuild)} existing tile(s) are rebuilt — "
                          f"the files that feed them changed since they were "
                          f"built (first: {sorted(rebuild)[0]})")
            if unknown:
                self._log(f"  {unknown} existing tile(s) have no record of "
                          f"their sources (built by an older version) — kept; "
                          f"tick \"Rebuild existing tiles\" to refresh them")

            # Only the tiles this run will actually build: render and job
            # building both skip existing outputs, so downloading terrain
            # for skipped tiles would be pure waste.
            pending = [
                (r, t) for r, t in expected
                if self._overwrite or t["filename"] in rebuild
                or not os.path.exists(
                    os.path.join(self._output_dir, t["filename"]))
            ]
            # Re-estimate the remaining work now that the tile list is final:
            # downloads against the pool (tiles already there cost nothing),
            # conversion over the tiles that will really be built.
            xyz_entries = [e for e in all_entries if e.get("kind") == "xyz"]
            _estimate_download_work(self.progress_model, xyz_entries, pending,
                                    pool_check=True)
            self.progress_model.set_work(
                ("convert",), _convert_work([t for _r, t in pending]))
            self._emit_progress()

            # ---- Download phase: XYZ entries via the shared toolkit pool
            # downloader. Hard errors abort the run — there is no QGIS-render
            # fallback for XYZ any more.
            if xyz_entries:
                self._say("Downloading terrain tiles...")
                self._log(f"Downloading terrain for {len(xyz_entries)} XYZ "
                          f"entr(ies) via the shared pool...")

                def dl_progress(frac: float, label: str) -> None:
                    if self._canceled:
                        return
                    self._emit_progress()
                    self.status.emit(label)

                try:
                    _download_xyz_entries(
                        xyz_entries, pending,
                        progress_cb=dl_progress,
                        progress_model=self.progress_model,
                        # Cancel must reach the running download: polled per
                        # output line (raises ConverterCancelled), and the
                        # subprocess handle is exposed so cancel() can kill
                        # it outright.
                        should_cancel=lambda: self._canceled,
                        on_start=self._register_proc)
                except terrain_adapter.ConverterCancelled:
                    self._log("Cancelled by user.")
                    self._say("Cancelled.", final=True)
                    return
                except RuntimeError as exc:
                    if self._canceled:
                        # cancel() killed the download subprocess; the
                        # resulting "download failed" is not an error.
                        self._log("Cancelled by user.")
                        self._say("Cancelled.", final=True)
                        return
                    self.finished_err.emit(str(exc))
                    return
                finally:
                    self._proc = None
            self.progress_model.finish_where(
                lambda k: isinstance(k, tuple) and k[:1] in (("download",),
                                                             ("render",)))
            self._emit_progress()

            if self._canceled:
                return

            os.makedirs(self._output_dir, exist_ok=True)
            try:
                jobs, mat_temps = _build_tile_jobs(
                    expected, self._output_dir, self._overwrite, all_entries,
                    buildings_path, tempfile.gettempdir(), rebuild=rebuild)
            except RuntimeError as exc:
                self.finished_err.emit(str(exc))
                return
            # Materialized windows join the rendered tifs (already
            # registered above) in the end-of-run cleanup list.
            self._temp_tifs.extend(mat_temps)

            if not jobs:
                self.progress_model.finish_all()
                self._emit_progress()
                self._say("All tiles already exist.", final=True)
                self.finished_ok.emit(self._output_dir)
                return

            self._log(f"Generated {len(jobs)} tile jobs.")

            # ---- Phase 3 (70-100 %): Run converter ----
            if self._canceled:
                return

            # converter_exe was resolved in Phase 2 (the plan cross-check
            # needed it before any job was built).
            total = len(jobs)
            # Exact now: the jobs that will really run, weighted by size.
            convert_units = _convert_work(jobs)
            self.progress_model.set_work(("convert",), convert_units)
            self._say(f"Converting {total} .abt tiles...",
                      "Converting tiles")
            self._log(f"Phase 3: Running converter ({total} tiles)...")

            job_file = os.path.join(
                tempfile.gettempdir(), f"aether_conv_{os.getpid()}.json"
            )
            # Re-made a statement before the write: the output directory and
            # the scratch temp dir were created in an earlier phase, and a
            # directory that vanished in between is what the converter
            # reports as the pathless "os error 3".
            terrain_adapter.ensure_engine_dirs(
                self._output_dir, os.path.dirname(os.path.abspath(job_file)))
            with open(job_file, "w") as fh:
                json.dump(jobs, fh)

            # Fail with the offending PATH rather than letting the converter
            # die on a missing source or output directory with no name in it.
            terrain_adapter.preflight_ingest_jobs(jobs)

            self._log(f"Launching: {converter_exe} ingest")

            no_data_batch = []

            def on_line(line: str) -> None:
                if self._canceled:
                    raise terrain_adapter.ConverterCancelled()
                self._log(line)
                # CONTRACT item 18: a batch no source covers exits 0 with
                # this marker instead of failing — catch it so the success
                # path below can say what the output actually holds.
                if "[Warn] NO-DATA batch:" in line:
                    no_data_batch.append(line)
                prog = run_progress.parse_ingest_progress(line)
                if prog:
                    curr, tot = prog
                    per = convert_units / max(tot, 1)
                    self.progress_model.set_done(
                        ("convert",), curr * per,
                        ceiling=run_progress.next_ingest_report(curr, tot) * per)
                    self.progress_model.set_phase("Converting tiles", {
                        "done": curr, "total": tot, "unit": "tiles"})
                    self._emit_progress()
                    self.status.emit(f"Converting tile {curr}/{tot}")

            try:
                # The one shared subprocess runner (terrain_adapter).
                rc = terrain_adapter.run_converter_streaming(
                    converter_exe, ["ingest", "--job-file", job_file],
                    on_line, on_start=self._register_proc)
            except terrain_adapter.ConverterCancelled:
                self._log("Cancelled by user.")
                self._say("Cancelled.", final=True)
                return
            finally:
                self._proc = None

            try:
                os.remove(job_file)
            except OSError:
                pass

            if self._canceled:
                # cancel() killed the process directly — the kill won the
                # race against the per-line check, so rc is the kill signal
                # (-15), not a converter failure. End cleanly, no error.
                self._log("Cancelled by user.")
                self._say("Cancelled.", final=True)
                return

            if rc != 0:
                msg = f"Converter failed (exit code {rc}). See log."
                self._log(msg)
                self.finished_err.emit(msg)
                return

            # Record what each built tile was made from (whole tiles only —
            # a short one has no valid record and is rebuilt next time).
            built_fps = {}
            for job in jobs:
                name = os.path.basename(job["output_path"])
                whole = terrain_adapter._tile_is_whole(job["output_path"])
                built_fps[name] = tile_fps.get(name) if whole else None
            _write_output_manifest(self._output_dir, built_fps)

            self.progress_model.finish_all()
            self._emit_progress()
            self._say("Done.", final=True)
            if no_data_batch:
                self._log(
                    "WARNING: no source covered any pixel of this area — "
                    "every output tile is no-data (VOID). The Map Converter "
                    "keeps voids by contract; Site Analysis reads such "
                    "ground as 0 m (sea level). Check the sources' extent "
                    "and CRS if you expected terrain here.")
            self._log(f"Conversion complete: {total} tiles.")
            self.finished_ok.emit(self._output_dir)

        except Exception as exc:
            self._log(f"ERROR: {exc}")
            self.finished_err.emit(f"Unexpected error: {exc}")
        finally:
            self._cleanup_temp_tifs()

    def _register_proc(self, proc) -> None:
        """on_start hook: expose the running engine process for cancel()."""
        self._proc = proc

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
        """Resolve a buildings entry to an FGB path.

        The deferred OSM download is this worker's own business (it needs the
        entry's extent and the status signal); everything else is the shared
        vector resolution in ``core.buildings_source`` — the same call the
        Site Analysis path makes, so both tabs hand the converter the same
        kind of file for the same source.
        """
        path = entry["resolved_source"]
        src_crs = entry.get("crs_authid", "") or ""

        # Deferred OSM download.
        if path.startswith("[OSM Download"):
            return self._download_osm_buildings(entry)

        return resolve_buildings_source(path, src_crs, log=self._log)

    def _download_osm_buildings(self, entry: dict) -> Optional[str]:
        """Download buildings from OSM Overpass API."""
        extent = entry.get("extent")
        if not extent:
            self._log("OSM building download skipped: no extent set.")
            return None

        s, n, w, e = extent["south"], extent["north"], extent["west"], extent["east"]
        self._log(f"  Downloading buildings from OSM: "
                  f"S={s:.4f} N={n:.4f} W={w:.4f} E={e:.4f}")
        self.status.emit("Downloading buildings from OSM...")

        try:
            import urllib.request
            import urllib.parse

            query = (
                f'[out:json][timeout:180];'
                f'(way["building"]({s},{w},{n},{e});'
                f'relation["building"]({s},{w},{n},{e}););'
                f'out body;>;out skel qt;'
            )
            url = "https://overpass-api.de/api/interpreter"
            data = urllib.parse.urlencode({"data": query}).encode()
            req = urllib.request.Request(
                url, data=data,
                headers={"User-Agent": "Waveshed-QGIS-Plugin/0.1"},
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                raw = resp.read()

            osm_data = json.loads(raw)
            geojson = MapConverterTab._overpass_to_geojson(osm_data)

            out_dir = fgb_cache_dir()
            geojson_file = os.path.join(out_dir, "osm_buildings.geojson")
            with open(geojson_file, "w") as f:
                json.dump(geojson, f)

            count = len(geojson.get("features", []))
            self._log(f"  Downloaded {count} buildings from OSM.")
            if count == 0:
                return None

            fgb = _convert_to_fgb(geojson_file, out_dir)
            return os.path.abspath(fgb) if fgb else geojson_file

        except Exception as exc:
            self._log(f"  OSM building download failed: {exc}")
            return None

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
_COL_TARGET_RES = 4
_COL_EXTENT = 5
_LAYER_COLS = ["Type", "Source", "CRS", "Native Res", "Target Res", "Extent"]

#: Extent-picker choice for folder rows: the union of the folder's files.
_WHOLE_FOLDER = "Whole folder (all files)"


class MapConverterTab(QWidget):
    """Map Converter tab — fuse layers into .abt terrain tiles."""

    def __init__(self, parent_dialog, parent=None):
        super().__init__(parent)
        self._dialog = parent_dialog
        self.iface = parent_dialog.iface
        self._worker = None
        self._layers: List[_LayerEntry] = []
        self._rect_tool = None
        self._drawing_for_layer: Optional[List[int]] = None
        self._extent_clipboard: Optional[Dict[str, float]] = None
        self._run_model: Optional[run_progress.RunProgress] = None
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
            if res == 30:
                cb.setChecked(True)
            self._res_checks[res] = cb
            res_lay.addWidget(cb)
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
        for cb in self._res_checks.values():
            cb.stateChanged.connect(self._update_estimate)

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
        self._table.cellDoubleClicked.connect(self._on_table_double_clicked)
        self._table.itemSelectionChanged.connect(self._sync_overlap_combo)
        self._combo_overlap.currentIndexChanged.connect(self._on_overlap_changed)

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
        self._update_estimate()
        msg = f"Add All Layers: {len(added)} added"
        if present:
            msg += f", {present} already in the stack"
        if imagery:
            msg += (f", {len(imagery)} skipped as imagery "
                    f"({', '.join(imagery[:5])}"
                    + (", …" if len(imagery) > 5 else "") + ")")
        self._log_msg(msg)
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
            target_resolutions=[30] if not is_buildings else [],
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
            self._update_estimate()
        self._log_msg(f"Added layer: {layer.name()} "
                      f"(native ~{native_res or '?'}m){note}")
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
            target_resolutions=[30],
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
        self._update_estimate()
        skipped = sum(1 for d in decisions.values()
                      if d == terrain_index.DECISION_SKIP)
        self._log_msg(f"Added folder: {folder} ({len(index.entries)} files"
                      + (f", {skipped} skipped" if skipped else "")
                      + f", finest ~{native_res or '?'}m; overlaps: "
                      f"{terrain_index.PRIORITY_LABELS[entry.overlap_priority]}"
                      + ("; extent = whole folder" if entry.extent else
                         "; no usable file, extent not set") + ")")

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

    def _on_download_buildings(self):
        """Add a deferred OSM building download entry.

        The actual download happens at conversion time.  The user sets
        the extent via 'Draw Extent for Layer' after adding.
        """
        self._layers.append(_LayerEntry(
            layer_type="buildings",
            source_path="[OSM Download — set extent, download at convert]",
            crs_authid="EPSG:4326",
            priority=len(self._layers) + 1,
        ))
        self._rebuild_table()
        self._log_msg(
            "Added deferred OSM building download. Select the row and set the "
            "download area with 'Draw Extent for Layer' (draw on the map) or "
            "'Extent from Layer' (reuse a loaded layer's extent, e.g. your DEM). "
            "Buildings will be downloaded when you click Convert."
        )
        QMessageBox.information(
            self, "OSM Buildings Added",
            "A placeholder entry has been added.\n\n"
            "1. Select the new row in the table.\n"
            "2. Define the download area, either:\n"
            "   • 'Draw Extent for Layer' — draw a rectangle on the map, or\n"
            "   • 'Extent from Layer' — reuse a loaded layer's extent "
            "(e.g. your DEM).\n"
            "3. Click 'Convert' — buildings will be downloaded automatically."
        )

    @staticmethod
    def _overpass_to_geojson(data: dict) -> dict:
        """Convert Overpass JSON to a simple GeoJSON FeatureCollection."""
        nodes = {}
        features = []
        for el in data.get("elements", []):
            if el["type"] == "node":
                nodes[el["id"]] = (el["lon"], el["lat"])

        for el in data.get("elements", []):
            if el["type"] != "way" or "building" not in el.get("tags", {}):
                continue
            coords = []
            for nid in el.get("nodes", []):
                if nid in nodes:
                    coords.append(list(nodes[nid]))
            if len(coords) >= 4:
                features.append({
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [coords],
                    },
                    "properties": el.get("tags", {}),
                })

        return {"type": "FeatureCollection", "features": features}

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
        self._update_estimate()
        ext = extent
        self._log_msg(
            f"Extent {how} for row(s) {', '.join(str(r + 1) for r in changed)}"
            f": S={ext['south']:.4f} N={ext['north']:.4f} "
            f"W={ext['west']:.4f} E={ext['east']:.4f}")

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
        self._update_estimate()

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

            # Target resolutions
            if entry.layer_type == "buildings":
                self._set_cell(i, _COL_TARGET_RES, "-")
            else:
                res_str = ",".join(str(r) for r in sorted(entry.target_resolutions))
                self._set_cell(i, _COL_TARGET_RES, f"{res_str}m")

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
    # Target resolution editing (double-click on Target Res cell)
    # ------------------------------------------------------------------

    def _on_table_double_clicked(self, row, col):
        if col != _COL_TARGET_RES:
            return
        if row < 0 or row >= len(self._layers):
            return
        entry = self._layers[row]
        if entry.layer_type == "buildings":
            return

        from qgis.PyQt.QtWidgets import QInputDialog
        available = ", ".join(str(r) for r in sorted(_ABT_EXTENT_DEG))
        current = ",".join(str(r) for r in sorted(entry.target_resolutions))
        text, ok = QInputDialog.getText(
            self, "Target Resolutions",
            f"Enter comma-separated resolutions in metres.\n"
            f"Available: {available}\n"
            f"Native resolution: {entry.native_res_m or '?'}m",
            text=current,
        )
        if not ok:
            return

        try:
            new_res = sorted(set(int(r.strip()) for r in text.split(",")
                                 if r.strip()))
        except ValueError:
            QMessageBox.warning(self, "Invalid", "Enter integers only.")
            return

        valid = set(_ABT_EXTENT_DEG)
        bad = [r for r in new_res if r not in valid]
        if bad:
            QMessageBox.warning(
                self, "Invalid Resolution",
                f"Unsupported resolution(s): {bad}.\n"
                f"Valid: {sorted(valid)}"
            )
            return

        # Warn if upsampling (target finer than native).
        if entry.native_res_m:
            finer = [r for r in new_res if r < entry.native_res_m * 0.9]
            if finer:
                r = QMessageBox.warning(
                    self, "Upsampling Warning",
                    f"Resolution(s) {finer}m are finer than the native "
                    f"resolution (~{entry.native_res_m:.1f}m).\n\n"
                    f"The output will be upsampled (interpolated) — "
                    f"no additional detail will be gained.\n\nProceed?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if r != QMessageBox.StandardButton.Yes:
                    return

        entry.target_resolutions = new_res
        self._rebuild_table()
        self._update_estimate()

    # ------------------------------------------------------------------
    # Estimate
    # ------------------------------------------------------------------

    def _get_output_resolutions(self) -> List[int]:
        return [r for r, cb in self._res_checks.items() if cb.isChecked()]

    def _update_estimate(self):
        out_res = self._get_output_resolutions()
        if not out_res:
            self._lbl_estimate.setText("Select at least one output resolution.")
            return

        # Compute combined extent from all raster layers.
        combined = None
        layers_without_extent = 0
        for entry in self._layers:
            if entry.layer_type == "buildings":
                continue
            if not entry.extent:
                layers_without_extent += 1
                continue
            if combined is None:
                combined = dict(entry.extent)
            else:
                combined["south"] = min(combined["south"], entry.extent["south"])
                combined["north"] = max(combined["north"], entry.extent["north"])
                combined["west"] = min(combined["west"], entry.extent["west"])
                combined["east"] = max(combined["east"], entry.extent["east"])

        if layers_without_extent > 0:
            self._lbl_estimate.setText(
                f"{layers_without_extent} layer(s) have no extent set. "
                f"Select a layer and click 'Draw Extent' or 'Extent from Layer'."
            )
            return

        if combined is None:
            self._lbl_estimate.setText(
                "Add raster layers and set extents to see estimate."
            )
            return

        # Price what the run will actually build: the worker skips tiles that
        # already exist unless "Rebuild existing tiles" is on.
        total_tiles, total_mb = _estimate_tile_count_and_mb(
            combined, out_res,
            output_dir=self._get_output_dir(),
            overwrite=self._chk_overwrite.isChecked(),
        )

        colour = "red" if total_mb > 200_000 else (
            "darkorange" if total_mb > 50_000 else "black")
        self._lbl_estimate.setText(
            f'<span style="color:{colour}">'
            f".abt tiles: {total_tiles:,} | "
            f"Est. disk: {total_mb:,} MB ({total_mb / 1024:.1f} GB)"
            f"</span>"
        )

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

        # Check output resolutions.
        out_res = self._get_output_resolutions()
        if not out_res:
            QMessageBox.warning(
                self, "No Output Resolution",
                "Select at least one output resolution checkbox."
            )
            return

        # Warn if any output res is finer than all input layers.
        finest_input = min(
            (e.native_res_m for e in rasters if e.native_res_m),
            default=None,
        )
        if finest_input:
            upsampled = [r for r in out_res if r < finest_input * 0.9]
            if upsampled:
                r = QMessageBox.warning(
                    self, "Upsampling Warning",
                    f"Output resolution(s) {upsampled}m are finer than "
                    f"the finest input (~{finest_input:.1f}m).\n\n"
                    f"The output will be interpolated — no additional "
                    f"detail will be gained.\n\nProceed?",
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

        self._log.clear()
        self._log_msg("Starting conversion...")

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
        self._worker = _MapConverterWorker(
            self._layers, output_dir, out_res, resolved, self,
            overwrite=self._chk_overwrite.isChecked(),
            progress_model=self._run_model,
        )
        self._worker.progress.connect(self._on_worker_progress)
        self._worker.status.connect(self._on_worker_status)
        self._worker.log_line.connect(self._log_msg)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.finished_err.connect(self._on_error)
        self._worker.start()

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
        self._set_running(False)
        QMessageBox.information(self, "Done", f"Tiles written to:\n{path}")

    def _on_error(self, msg):
        if self._run_model is not None:
            self._run_model.stop("Failed.")
        self._set_running(False)
        QMessageBox.critical(self, "Failed", msg)

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
        """Load a folder of .abt tiles as a QGIS raster layer."""
        folder = QFileDialog.getExistingDirectory(
            self, "Select .abt Tile Folder",
            self._get_output_dir(),
        )
        if not folder:
            return

        abt_files = [
            os.path.join(folder, f) for f in os.listdir(folder)
            if f.lower().endswith(".abt")
        ]
        if not abt_files:
            QMessageBox.warning(self, "No .abt Files",
                                f"No .abt files found in:\n{folder}")
            return

        self._log_msg(f"Inspecting {len(abt_files)} .abt files in {folder}")

        try:
            from ..core import abt

            out_tif = os.path.join(
                tempfile.gettempdir(),
                f"aether_inspect_{os.path.basename(folder)}.tif",
            )
            abt.mosaic_to_geotiff(folder, out_tif, on_log=self._log_msg)

            # Add to QGIS.
            layer_name = f"Terrain: {os.path.basename(folder)}"
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
                self._log_msg(f"Failed to load mosaic as QGIS layer.")
                QMessageBox.warning(self, "Load Failed",
                                    f"Created {out_tif} but could not load.")

        except ImportError as exc:
            QMessageBox.critical(
                self, "Missing Dependency",
                f"numpy is required for terrain inspection:\n{exc}"
            )
        except Exception as exc:
            self._log_msg(f"Inspect failed: {exc}")
            QMessageBox.critical(self, "Inspect Failed", str(exc))

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cancel_worker(self):
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(5000)
        if self._rect_tool:
            self._rect_tool.cleanup()
            self._rect_tool = None
