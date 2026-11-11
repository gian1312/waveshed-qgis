"""Site Analysis tab widget -- multi-site, multi-altitude coverage analysis.

Embeddable QWidget designed for use as a tab inside the main AETHER dialog.
Supports both LOS (pure geometric line-of-sight) and LOSS (signal-strength)
modes, with per-site parameters and multiple receiver altitudes.

The parent dialog must provide:
- ``self.iface``           QgisInterface
- ``self.get_mode()``      returns "LOS" or "LOSS"
- ``self.get_loss_model()`` returns "ITM"
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import re
import subprocess
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import QThread, pyqtSignal, Qt
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
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
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsMessageLog,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
    QgsWkbTypes,
)
from qgis.gui import QgsRubberBand
from qgis.PyQt.QtGui import QColor

from ..core.asset_manager import compute_erp, list_assets, load_asset
from ..core.attribution import source_credit
from ..core.binary_manager import engine_error_hint, find_binary
from ..core import api_key
from ..core import engine_log
from ..core import terrain_adapter

# Hide console windows on Windows.
#: The engine launch flags (no console window on Windows), one definition.
_SUBPROCESS_FLAGS = terrain_adapter._SUBPROCESS_FLAGS
from ..core.job_builder import (
    INVALID,
    MIN_ANTENNA_AGL_M,
    VALID_RESOLUTIONS,
    CoverageParams,
    antenna_height_error,
    build_coverage_job,
    format_model_warnings,
    model_warnings,
    write_job_file,
)
from ..core.layer_utils import dem_layer_warning, hide_from_dem_picker
from ..core import output_estimate
from ..core.output_estimate import output_space_problem
from ..core.result_loader import (
    GROUP_COVERAGE,
    add_layer_to_project,
    load_coverage_result,
)
from ..core.binary_manager import setup_warning
from ..core.terrain_adapter import (
    XYZ_CACHE_DIRNAME,
    TerrainPrep,
    analysis_bbox,
    build_terrain_prep,
    buildings_identity,
    get_cache_dir,
    get_source_resolution_info,
    is_abt_tile_dir,
    prepare_terrain,
    prepare_terrain_profile,
    prepared_buildings,
    reset_terrain_warnings,
    run_region_union,
    select_abt_tiles,
    terrain_coverage_warning,
    terrain_plan,
    terrain_plan_profile,
    terrain_size_warning,
)
from ..core import abt_index
from ..core import range_profile as rp
from ..core import terrain_index
from . import terrain_dirs
from . import terrain_folder_scan
from .engine_notices import setup_title, show_run_error
from .height_inputs import MIN_AMSL_M, bind_height_mode
from .output_dir import SITE_OUTPUT_DIR_KEY, bind_output_dir, save_output_dir
from .map_tools import activate_point_capture
from .range_profile_dialog import RangeProfileDialog

TAG = "Waveshed"

#: Prepared folders whose "carries buildings although unticked" notice was
#: shown this session (normalised paths).
_BUILDINGS_NOTICE_FOLDERS: set = set()

#: The status tip / message-bar hint of "Include buildings" on a prepared
#: .abt folder.
_PREPARED_BUILDINGS_HINT = (
    "The terrain is a prepared .abt folder: buildings are baked into its "
    "tiles when the Map Converter builds them — 'Include buildings' cannot "
    "add or remove them here.")


def _log_terrain_plan(cached: int, missing: int, download_mb: int,
                      cached_mb: int, partial: int = 0,
                      build_deg2: float = 0.0, tile_deg2: float = 0.0,
                      source_files: Optional[int] = None,
                      source_mb: Optional[int] = None,
                      raw_mb: int = 0, buildings_mb: int = 0) -> None:
    """Record what the run will reuse vs build, before it starts.

    Without this the log jumps straight to per-tile work and there is no way
    to tell a cache that is working from one that is silently rebuilding.
    A file source builds only the region of a tile a site needs (a partial
    pool tile): *partial* of the *missing* tiles are such builds, reading
    *build_deg2* of their *tile_deg2* deg²; *source_files* / *source_mb* (when
    the folder index priced them) are the files those regions read.
    *raw_mb*: the most the XYZ download adds to the raw source-tile cache;
    *buildings_mb*: the most the OpenFreeMap building tiles of those builds
    add to the building store.
    """
    msg = (f"terrain plan: {cached + missing} tile(s) needed — {cached} pooled "
           f"(~{cached_mb} MB reused), {missing} to build (~{download_mb} MB)")
    if raw_mb:
        msg += f" + up to ~{raw_mb} MB of downloaded source tiles cached"
    if buildings_mb:
        msg += f" + up to ~{buildings_mb} MB of building tiles"
    if partial:
        msg += (f"; {partial} of them partial — {build_deg2:.3f} deg² to "
                f"build of {tile_deg2:.3f} deg² of tiles")
    if source_files is not None:
        msg += (f"; reading {source_files} source file(s), "
                f"~{source_mb or 0} MB")
    QgsMessageLog.logMessage(msg, TAG, Qgis.MessageLevel.Info)

def _plan_jobs(unique: List[CoverageParams], plan_source: str,
               plan_buildings: Optional[str],
               region_union: Dict[str, List[Dict[str, float]]],
               source_infos: Any = None) -> Dict[str, Any]:
    """The pre-run terrain estimate over a run's distinct jobs, priced as
    the worker will build them: per tile the job's region (a file source
    builds only that part of a pool tile) together with the run's
    *region_union*. *source_infos* (the folder index — never read on the GUI
    thread) also checks fingerprints and prices the files the regions read
    (``terrain_adapter.terrain_plan``)."""
    out: Dict[str, Any] = {
        "download_mb": 0, "raw_mb": 0, "buildings_mb": 0, "cached_mb": 0,
        "tiles_cached": 0, "tiles_missing": 0, "tiles_partial": 0,
        "build_deg2": 0.0, "tile_deg2": 0.0, "source_files": None,
        "source_mb": None}
    for params in unique:
        if params.range_profile:
            plan = terrain_plan_profile(
                plan_source, params.tx_lat, params.tx_lon,
                params.range_profile, params.az_start, params.az_end,
                plan_buildings,
                band_buildings=params.range_profile_buildings,
                output_res_m=params.resolution_m,
                source_infos=source_infos,
                region_union=region_union,
            )
        else:
            plan = terrain_plan(
                plan_source, params.tx_lat, params.tx_lon,
                params.max_range_km, params.resolution_m,
                params.az_start, params.az_end,
                plan_buildings if all(params.range_profile_buildings or [True])
                else None,
                source_infos=source_infos,
                region_union=region_union,
            )
        out["download_mb"] += plan["download_mb"]
        out["raw_mb"] += plan.get("raw_mb", 0)
        out["buildings_mb"] += plan.get("buildings_mb", 0)
        out["cached_mb"] += plan["total_mb"] - plan["download_mb"]
        for k in ("tiles_cached", "tiles_missing", "tiles_partial",
                  "build_deg2", "tile_deg2"):
            out[k] += plan.get(k, 0)
        for k in ("source_files", "source_mb"):
            if plan.get(k) is not None:
                out[k] = (out[k] or 0) + plan[k]
    return out


def _disk_refusal(jobs: List[Tuple[CoverageParams, str]],
                  pool_mb: int, output_dir: str,
                  cache_dir: Optional[str] = None,
                  free_of: Any = None, raw_mb: int = 0,
                  buildings_mb: int = 0) -> Optional[str]:
    """The refusal text when a run's writes do not fit, else None: the
    coverage rasters (``output_estimate.engine_output_bytes``) on the output
    drive, the pool tiles :func:`terrain_adapter.terrain_plan` prices
    (*pool_mb*, its ``download_mb``), the raw source tiles their XYZ
    download caches (*raw_mb*, its ``raw_mb`` — "raw tiles" in
    ``<cache>/xyz``) and the OpenFreeMap building tiles they fetch
    (*buildings_mb*, its ``buildings_mb`` — "building tiles", an upper
    bound) on the cache drive — summed when they share a drive, refused
    when the need exceeds free − 2 GB (``output_estimate.drive_needs``).
    *free_of* (tests) replaces the free-space probe."""
    rasters = [(name, p.model, p.max_range_km, p.resolution_m, p.az_start,
                p.az_end) for p, name in jobs]
    raster_bytes = sum(output_estimate.engine_output_bytes(*r[1:])
                       for r in rasters)
    cache_root = cache_dir or get_cache_dir()
    items = [("coverage rasters", output_dir, raster_bytes),
             ("terrain pool", cache_root,
              max(0, int(pool_mb)) * 1024 * 1024)]
    if int(raw_mb or 0) > 0:
        items.append(("raw tiles",
                      os.path.join(cache_root, XYZ_CACHE_DIRNAME),
                      int(raw_mb) * 1024 * 1024))
    if int(buildings_mb or 0) > 0:
        # THE building store lives in the cache's pool directory.
        items.append(("building tiles", cache_root,
                      int(buildings_mb) * 1024 * 1024))
    kw = {"free_of": free_of} if free_of is not None else {}
    needs = output_estimate.drive_needs(items, **kw)
    short = output_estimate.short_drives(needs)
    if not short:
        return None
    text = output_estimate.refusal_text(short, "this analysis")
    out_drive = needs[0]
    if out_drive in short and rasters and out_drive.free is not None:
        # The raster is usually what does not fit: name a resolution that
        # would (the biggest job at a coarser pixel, the rest unchanged).
        sizes = [(output_estimate.engine_output_bytes(*r[1:]), r)
                 for r in rasters]
        biggest, (label, model, rng, res, a0, a1) = max(sizes,
                                                        key=lambda x: x[0])
        budget = (out_drive.free - output_estimate.DISK_RESERVE_BYTES
                  - (out_drive.need - biggest))
        fit = output_estimate.coarsest_fitting_resolution(
            model, rng, budget, VALID_RESOLUTIONS, a0, a1)
        if fit is not None and fit != res:
            text += (f"\n\nAt {rng:g} km, {fit} m pixels would fit for "
                     f"{label}.")
    return text


#: ``_plan_jobs``' totals for a pre-built ``.abt`` folder: nothing to build.
_PREBUILT_TOTALS: Dict[str, Any] = {
    "download_mb": 0, "raw_mb": 0, "buildings_mb": 0, "cached_mb": 0,
    "tiles_cached": 0, "tiles_missing": 0, "tiles_partial": 0,
    "build_deg2": 0.0, "tile_deg2": 0.0, "source_files": None,
    "source_mb": None}


def _abt_job_selections(terrain_dir: str,
                        unique: List[CoverageParams]) -> List[Any]:
    """Each distinct job's pre-built tile selection, with the bands the
    worker's terrain step passes (``select_abt_tiles`` is memoised, so the
    worker reuses these). Off the GUI thread."""
    out = []
    for p in unique:
        bands = p.range_profile or [(p.max_range_km * 1000.0, p.resolution_m)]
        out.append(select_abt_tiles(terrain_dir, p.tx_lat, p.tx_lon,
                                    list(bands), p.az_start, p.az_end))
    return out


def _prebuilt_checks(terrain_dir: str, unique: List[CoverageParams],
                     ) -> Tuple[Optional[List[Any]], List[str], Any]:
    """A pre-built ``.abt`` folder's part of :func:`_terrain_dir_checks`:
    ``(every job's tile selection or None, substitution lines, buildings of
    the chosen tiles or None)`` — each step logged and skipped on failure
    (a pre-check never blocks a run)."""
    sels: Optional[List[Any]] = None
    subs: List[str] = []
    bstate = None
    try:
        sels = _abt_job_selections(terrain_dir, unique)
    except Exception as exc:  # noqa: BLE001 — a pre-check never blocks
        QgsMessageLog.logMessage(
            f"pre-built terrain selection skipped: {exc}", TAG,
            Qgis.MessageLevel.Warning)
    for sel in sels or ():
        subs.extend(x for x in sel.substitutions if x not in subs)
    if sels is not None:
        try:
            bstate = prepared_buildings(
                terrain_dir, [pth for sel in sels for pth in sel.paths])
        except Exception as exc:  # noqa: BLE001 — never blocks a run
            QgsMessageLog.logMessage(
                f"pre-built terrain buildings check skipped: {exc}", TAG,
                Qgis.MessageLevel.Warning)
    return sels, subs, bstate


def _terrain_dir_checks(terrain_dir: str, union: Dict[str, float],
                        res_m: Optional[float],
                        unique: List[CoverageParams], prebuilt: bool,
                        plan_source: str, plan_buildings: Optional[str],
                        region_union: Dict[str, List[Dict[str, float]]],
                        ) -> Tuple[Optional[str], Optional[Dict[str, Any]],
                                   List[str], Any, Optional[TerrainPrep]]:
    """The terrain-folder pre-check, in the coverage check's worker thread:
    ``(coverage warning, priced totals or None, substitution lines,
    buildings of the chosen tiles or None, the run's TerrainPrep or None)``.

    Raster folder: the run's ONE preparation of the folder
    (``terrain_adapter.build_terrain_prep``: the source infos in overlap
    order and one FootprintIndex, over the index check the Run click just
    made), from which the coverage warning and the totals are computed —
    and which the worker's terrain step uses instead of reading the folder
    again. None when the folder cannot be read so (no usable file, an
    unanswered problem file): the coverage check reads the index on its
    own, an estimate never blocks a run, and the worker's own read says
    what is wrong. Pre-built ``.abt`` folder: every
    job's tile selection (headers through the folder's header index — the
    GUI thread never opens a tile), whose uncovered cells the coverage
    warning names and whose coarser-than-needed substitutions the GUI asks
    about, and what the Map Converter recorded about the buildings of
    exactly those tiles (``terrain_adapter.prepared_buildings`` — the
    buildings dialogs)."""
    sels: Optional[List[Any]] = None
    subs: List[str] = []
    bstate = None
    prep: Optional[TerrainPrep] = None
    if prebuilt:
        sels, subs, bstate = _prebuilt_checks(terrain_dir, unique)
    else:
        try:
            prep = build_terrain_prep(terrain_dir)
        except Exception:  # noqa: BLE001 — a pre-check never blocks a run
            prep = None
    # Buildings of a pre-built folder are the buildings dialogs' business
    # (they read what the tiles carry); the coverage warning stays coverage.
    cov = terrain_coverage_warning(
        terrain_dir, union, resolution_m=res_m, abt_selections=sels,
        infos=prep.infos if prep is not None else None)
    priced = None
    if prep is not None:
        priced = _plan_jobs(unique, plan_source, plan_buildings, region_union,
                            (prep.infos, False, prep.index))
        prep.coverage, prep.plan = cov, priced
    return cov, priced, subs, bstate, prep


# Column definitions -- identical for both LOS and LOSS modes.
# Freq / ERP come from the selected asset, not from separate table columns.
_SITE_COLUMNS = [
    "Asset",
    "Location",
    "Height (m)",
    "Height Mode",
    "AZ Rotation (deg)",
    "AZ Start (deg)",
    "AZ End (deg)",
    "Range (km)",
]

# Column indices for convenience.
_COL_ASSET = 0
_COL_LOCATION = 1
_COL_HEIGHT = 2
_COL_HEIGHT_MODE = 3
_COL_AZ_ROTATION = 4
_COL_AZ_START = 5
_COL_AZ_END = 6
_COL_RANGE = 7

# Widths for the widget-bearing columns. Wide enough for the widget plus its
# header text — "AZ Rotation (deg)" is the long one, and a column narrower than
# its own header reads as broken even when the widget inside still works.
_COL_WIDTHS = {
    _COL_ASSET: 150,
    _COL_HEIGHT: 90,
    _COL_HEIGHT_MODE: 100,
    _COL_AZ_ROTATION: 130,
    _COL_AZ_START: 110,
    _COL_AZ_END: 110,
    _COL_RANGE: 95,
}

#: No column may shrink below this, however narrow the dialog gets.
_COL_MIN_WIDTH = 70

# Altitude table columns.
_ALT_COLUMNS = ["Altitude (m)", "Reference"]


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

def _kill_proc(proc: subprocess.Popen) -> None:
    """Stop's kill, on the GUI thread — never blocks:
    ``terrain_adapter.kill_engine_process`` (POSIX SIGTERM; Windows
    TerminateProcess on the handle + a fire-and-forget ``taskkill /T`` for
    children)."""
    terrain_adapter.kill_engine_process(proc)


class _SiteAnalysisWorker(QThread):
    """Run the coverage pipeline for each (site x altitude) combination."""

    progress = pyqtSignal(int)           # 0-100
    status = pyqtSignal(str)             # human-readable step description
    finished_ok = pyqtSignal(list)       # list of (tif_path, model, display_name)
    finished_err = pyqtSignal(str)       # error message

    def __init__(
        self,
        jobs: List[Tuple[CoverageParams, str]],
        dem_layer: QgsRasterLayer,
        output_dir: str,
        terrain_dir: str = "",
        parent: Optional[QWidget] = None,
        osm_buildings: bool = False,
        terrain_prep: Optional[TerrainPrep] = None,
    ) -> None:
        super().__init__(parent)
        self.jobs = jobs
        self.dem_layer = dem_layer
        self.output_dir = output_dir
        self.terrain_dir = terrain_dir
        self.osm_buildings = osm_buildings
        #: The run's preparation of *terrain_dir* (``_terrain_dir_checks``),
        #: shared by every job while it is current — None: each job reads
        #: the folder itself (as the torture runner's worker does).
        self.terrain_prep = terrain_prep
        self._canceled = False
        self._proc: Optional[subprocess.Popen] = None

    def cancel(self) -> None:
        self._canceled = True
        if self._proc is not None:
            _kill_proc(self._proc)

    def _register_proc(self, proc: subprocess.Popen) -> None:
        """Terrain on_start hook: expose each engine process to cancel().

        A cancel() that landed after the last poll but before this launch
        found no process to kill, so the process is killed here instead —
        otherwise it would run to the end behind a pressed Stop.
        """
        self._proc = proc
        if self._canceled:
            _kill_proc(proc)

    def _parse_wedge_progress(self, line: str) -> Optional[int]:
        """Extract percentage from lines like ``Wedge 120/360``."""
        m = re.search(r"Wedge\s+(\d+)/(\d+)", line)
        if m:
            current, total = int(m.group(1)), int(m.group(2))
            if total > 0:
                return int(current * 100 / total)
        return None

    def run(self) -> None:  # noqa: C901 -- sequential pipeline
        results: List[Tuple[str, str, str]] = []
        total_jobs = len(self.jobs)
        if total_jobs == 0:
            self.finished_err.emit("No jobs to run.")
            return

        try:
            self.progress.emit(0)
            os.makedirs(self.output_dir, exist_ok=True)
            # Once per run, so the "range exceeds DEM extent" notice is shown a
            # single time rather than once per site/height.
            reset_terrain_warnings()

            # Pre-flight before any terrain is fetched, once per distinct
            # (resolution, profiled) combination in the run.
            from ..core import binary_manager as _bm
            for res, profiled in sorted({(p.resolution_m, bool(p.range_profile))
                                         for p, _ in self.jobs}):
                _bm.check_engine_for_job(res, range_profile=profiled)

            # Once per run: QGIS's own resident memory, beside the
            # converters' peak lines (owner's run 2026-11-07: 31 GB, the
            # converters ~3 GB each, ~20 GB attributed to nothing).
            from ..core import process_memory
            mem_line = process_memory.memory_line()
            if mem_line:
                QgsMessageLog.logMessage(mem_line, TAG, Qgis.MessageLevel.Info)

            # The jobs are prepared one after the other, but a tile several
            # of them share must be built ONCE, for all of them: every
            # region each job needs, per tile (file sources build only those
            # regions — terrain_adapter.resolve_pool_tile).
            region_union = run_region_union([p for p, _ in self.jobs])

            for job_idx, (params, display_name) in enumerate(self.jobs):
                if self._canceled:
                    return

                # Progress range for this job within overall 0-100.
                job_base = int(job_idx * 100 / total_jobs)
                job_span = int(100 / total_jobs)

                def _job_progress(pct: int) -> int:
                    return job_base + int(pct * job_span / 100)

                job_label = f"[{job_idx + 1}/{total_jobs}] {display_name}"

                # ---- 2. Prepare terrain ----
                self.status.emit(f"{job_label}: Downloading terrain...")
                self.progress.emit(_job_progress(5))

                from ..core import binary_manager as bm
                # Stop must reach the terrain step: the flag is polled per
                # tile / band / output line, and every engine process it
                # starts (download, ingest) is handed to cancel() to kill —
                # the ingest prints progress only every 10 tiles.
                try:
                    if params.range_profile:
                        # Adaptive / custom: each band's tiles at its own step.
                        abt_dir = prepare_terrain_profile(
                            dem_layer=self.dem_layer,
                            tx_lat=params.tx_lat,
                            tx_lon=params.tx_lon,
                            bands=params.range_profile,
                            binary_manager=bm,
                            terrain_dir=self.terrain_dir or None,
                            az_start=params.az_start,
                            az_end=params.az_end,
                            osm_buildings=self.osm_buildings,
                            band_buildings=params.range_profile_buildings,
                            buildings_notes=(
                                params.range_profile_buildings_notes),
                            should_cancel=lambda: self._canceled,
                            on_start=self._register_proc,
                            output_res_m=params.resolution_m,
                            region_union=region_union,
                            terrain_prep=self.terrain_prep,
                        )
                    else:
                        abt_dir = prepare_terrain(
                            dem_layer=self.dem_layer,
                            tx_lat=params.tx_lat,
                            tx_lon=params.tx_lon,
                            max_range_km=params.max_range_km,
                            resolution_m=params.resolution_m,
                            binary_manager=bm,
                            terrain_dir=self.terrain_dir or None,
                            az_start=params.az_start,
                            az_end=params.az_end,
                            # A Custom site whose range ends inside the first
                            # band carries that band's flag (_build_jobs).
                            osm_buildings=self.osm_buildings and all(
                                params.range_profile_buildings or [True]),
                            should_cancel=lambda: self._canceled,
                            on_start=self._register_proc,
                            region_union=region_union,
                            terrain_prep=self.terrain_prep,
                        )
                except terrain_adapter.ConverterCancelled:
                    return
                finally:
                    self._proc = None

                if self._canceled:
                    return

                # ---- 3. Build job config ----
                self.status.emit(f"{job_label}: Building job configuration...")
                self.progress.emit(_job_progress(15))

                job_output_dir = os.path.join(self.output_dir, params.output_name)
                os.makedirs(job_output_dir, exist_ok=True)

                job_config = build_coverage_job(params, abt_dir, job_output_dir)
                job_file = write_job_file(
                    job_config, job_output_dir, params.output_name,
                )

                if self._canceled:
                    return

                # ---- 4. Run aether_core ----
                self.status.emit(f"{job_label}: Running aether_core...")
                self.progress.emit(_job_progress(20))

                core_exe = find_binary("aether_core")
                env = os.environ.copy()
                env["RUST_LOG"] = "info"
                # aether_core is licensed — inject the validated API key
                # (raises ApiKeyError -> surfaced by the worker on failure).
                api_key.apply_license_env(env)

                # Streams through the one shared subprocess runner
                # (terrain_adapter.run_converter_streaming). Every line of
                # aether_core's console goes to today's engine log file; the
                # QGIS Log Messages panel (AETHER tag) gets the ones a user
                # reads — engine_log.is_diagnostic keeps the solver dump, the
                # polar sample and the profiling table out of it.
                stderr_lines: list[str] = []
                elog = engine_log.EngineLog(
                    display_name,
                    lambda text: QgsMessageLog.logMessage(
                        text, TAG, Qgis.MessageLevel.Info))

                def on_start(proc) -> None:
                    self._proc = proc  # expose for kill

                def on_line(line: str) -> None:
                    if self._canceled:
                        raise terrain_adapter.ConverterCancelled()
                    stderr_lines.append(line)
                    elog.line(line)
                    pct = self._parse_wedge_progress(line)
                    if pct is not None:
                        self.progress.emit(_job_progress(20 + int(pct * 0.6)))

                try:
                    rc = terrain_adapter.run_converter_streaming(
                        core_exe, ["--config", job_file], on_line,
                        on_start=on_start, env=env)
                except terrain_adapter.ConverterCancelled:
                    return
                finally:
                    self._proc = None
                    elog.close()
                if self._canceled:
                    # cancel() killed the process directly — rc is the kill
                    # signal, not an engine failure. End quietly.
                    return
                if rc != 0:
                    tail = "\n".join(stderr_lines[-20:])
                    raise RuntimeError(bm.engine_failure_message(
                        "aether_core", rc, tail, subject=display_name,
                        hint=engine_error_hint(tail, rc),
                        log_path=elog.path))

                if self._canceled:
                    return

                # ---- 5. Run aether_export ----
                self.status.emit(f"{job_label}: Exporting to GeoTIFF...")
                self.progress.emit(_job_progress(85))

                export_exe = find_binary("aether_export")

                bit_file = os.path.join(
                    job_output_dir, params.output_name + ".bit",
                )
                tiles_file = os.path.join(
                    job_output_dir, params.output_name + ".tiles",
                )
                input_file = (
                    bit_file if os.path.isfile(bit_file) else tiles_file
                )

                json_sidecar = os.path.join(
                    job_output_dir, params.output_name + ".json",
                )
                tif_path = os.path.join(
                    job_output_dir, params.output_name + ".tif",
                )

                if bm.engine_exported_tif(stderr_lines, tif_path, input_file):
                    # aether_core exported it already — once is enough.
                    QgsMessageLog.logMessage(
                        f"GeoTIFF already written by aether_core: {tif_path}",
                        TAG, Qgis.MessageLevel.Info)
                    result = None
                else:
                    result = subprocess.run(  # nosec B603 — aether_export from binary_manager, args are our own
                        [
                            export_exe,
                            "-i", input_file,
                            "-j", json_sidecar,
                            "-o", tif_path,
                        ],
                        capture_output=True,
                        text=True,
                        creationflags=_SUBPROCESS_FLAGS,
                    )
                if result is not None and result.returncode != 0:
                    raise RuntimeError(bm.with_details(
                        f"The analysis of {display_name} finished, but its "
                        "result could not be written as a GeoTIFF.\n\n"
                        "Check that the output folder is writable and has "
                        "free space, then run again.",
                        f"aether_export failed (exit {result.returncode}):\n"
                        f"{(result.stderr or '').strip()}"))

                results.append((tif_path, params.model, display_name))
                self.progress.emit(_job_progress(100))

            self.progress.emit(100)
            self.finished_ok.emit(results)

        except Exception as exc:
            self.finished_err.emit(str(exc))
        finally:
            # However the run ends: the finished worker stays on the tab,
            # its terrain preparation (~27 MB for 43,650 files) does not.
            self.terrain_prep = None


# ---------------------------------------------------------------------------
# Site Analysis Tab
# ---------------------------------------------------------------------------

class SiteAnalysisTab(QWidget):
    """Multi-site, multi-altitude coverage analysis tab.

    Designed to be embedded as a tab inside the main AETHER dialog.
    The *parent_dialog* must expose ``iface``, ``get_mode()``, and
    ``get_loss_model()``.
    """

    def __init__(
        self, parent_dialog: QWidget, parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._dialog = parent_dialog
        self.iface = parent_dialog.iface

        self._worker: Optional[_SiteAnalysisWorker] = None
        self._pick_tools: list = []  # prevent GC of active map tools
        self._site_coords: Dict[int, Tuple[float, float]] = {}
        self._current_mode: str = "LOS"

        # Cache of loaded asset dicts, keyed by name.
        self._asset_cache: Dict[str, dict] = {}

        # Rubber band for showing site positions on the map.
        self._sites_rb = QgsRubberBand(
            self.iface.mapCanvas(), QgsWkbTypes.GeometryType.PointGeometry,
        )
        self._sites_rb.setColor(QColor(0, 200, 255, 200))
        self._sites_rb.setWidth(3)
        self._sites_rb.setIconSize(12)

        self._build_ui()
        self._connect_signals()
        self._populate_raster_layers()
        # Keep the DEM combo in sync as project layers are added/removed, so a
        # DEM loaded after this dialog opens becomes selectable immediately.
        _proj = QgsProject.instance()
        _proj.layersAdded.connect(self._on_project_layers_changed)
        _proj.layersRemoved.connect(self._on_project_layers_changed)
        self._refresh_asset_cache()
        # Start with one empty site row so the user can type coordinates right
        # away instead of having to press "Add" first.  This must run *after*
        # _refresh_asset_cache(), or the row's Asset dropdown would be built
        # from an empty cache.
        self._add_site_row_internal()
        # Apply initial mode and update DEM info.
        self.set_mode(self._current_mode)
        self._on_dem_changed()

    # ==================================================================
    # Settings refresh
    # ==================================================================

    def refresh_settings(self) -> None:
        """Re-read what this tab derives from QgsSettings (cheap, idempotent).

        Called by the main dialog when the Settings tab saves. Covers:
        * the "Local directory: …" DEM entry (``waveshed/terrain_dir``) —
          ``_populate_raster_layers`` re-reads it and preserves the current
          selection;
        * the asset dropdowns in the sites table (``waveshed/assets_dir``
          contents) — repopulated per row, each row's choice preserved.
        User-entered state (coordinates, heights, chosen rows) is untouched.
        """
        self._populate_raster_layers()
        self._refresh_asset_cache()
        self._refresh_asset_combos()

    # ==================================================================
    # Asset helpers
    # ==================================================================

    def _refresh_asset_cache(self) -> None:
        """Reload assets from disk and rebuild the name-keyed cache."""
        self._asset_cache.clear()
        for asset in list_assets():
            name = asset.get("name", "")
            if name:
                self._asset_cache[name] = asset

    def _refresh_asset_combos(self) -> None:
        """Repopulate every site row's asset dropdown from the cache.

        Preserves each row's current choice (an asset that vanished from the
        store falls back to the blank entry — visibly, not silently).
        """
        names = self._asset_names()
        for row in range(self.sites_table.rowCount()):
            combo = self.sites_table.cellWidget(row, _COL_ASSET)
            if not isinstance(combo, QComboBox):
                continue
            current = combo.currentText()
            blocked = combo.blockSignals(True)
            combo.clear()
            combo.addItem("")  # blank default
            combo.addItems(names)
            idx = combo.findText(current)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            combo.blockSignals(blocked)

    def _asset_names(self) -> List[str]:
        """Return sorted list of cached asset names."""
        return sorted(self._asset_cache.keys())

    def _get_asset(self, name: str) -> Optional[dict]:
        """Return asset dict by name, or None."""
        return self._asset_cache.get(name)

    # ==================================================================
    # UI construction
    # ==================================================================

    def _build_ui(self) -> None:
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(6, 6, 6, 6)

        # ---- Sites section ----
        main_layout.addWidget(self._build_sites_section())

        # ---- Altitudes section ----
        main_layout.addWidget(self._build_altitudes_section())

        # ---- Analysis parameters ----
        main_layout.addWidget(self._build_analysis_section())

        # ---- ITM parameters (shown only in LOSS+ITM) ----
        main_layout.addWidget(self._build_itm_section())

        # ---- Progress + Run ----
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        main_layout.addWidget(self.progress_bar)

        self.status_label = QLabel("")
        self.status_label.setVisible(False)
        main_layout.addWidget(self.status_label)

        run_layout = QHBoxLayout()
        run_layout.addStretch()
        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setVisible(False)
        run_layout.addWidget(self.btn_stop)
        self.btn_run = QPushButton("Run Analysis")
        run_layout.addWidget(self.btn_run)
        main_layout.addLayout(run_layout)

        main_layout.addStretch()

    # -- Sites section -----------------------------------------------------

    def _build_sites_section(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        # Header row with label and buttons.
        header = QHBoxLayout()
        header.addWidget(QLabel("Sites"))
        header.addStretch()
        self.btn_add_site = QPushButton("Add")
        self.btn_remove_site = QPushButton("Remove")
        header.addWidget(self.btn_add_site)
        header.addWidget(self.btn_remove_site)
        layout.addLayout(header)

        # Sites table.
        self.sites_table = QTableWidget()
        self.sites_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.sites_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.sites_table.verticalHeader().setVisible(False)
        self.sites_table.verticalHeader().setDefaultSectionSize(36)
        self._setup_sites_columns()
        layout.addWidget(self.sites_table)

        return container

    def _setup_sites_columns(self) -> None:
        """Configure sites table columns (same for all modes)."""
        self.sites_table.setColumnCount(len(_SITE_COLUMNS))
        self.sites_table.setHorizontalHeaderLabels(_SITE_COLUMNS)
        header = self.sites_table.horizontalHeader()

        # Every data column holds a *cell widget* (combo/spin), not item data.
        # `ResizeToContents` measures item data, which those columns do not
        # have, so it collapses them to near-nothing — which is why Asset and
        # AZ Rotation were unusable the moment Propagation Loss unhid them.
        # Size them explicitly and let Location absorb the slack instead.
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setMinimumSectionSize(_COL_MIN_WIDTH)
        for col, width in _COL_WIDTHS.items():
            self.sites_table.setColumnWidth(col, width)
        header.setSectionResizeMode(_COL_LOCATION, QHeaderView.ResizeMode.Stretch)

    # -- Altitudes section -------------------------------------------------

    def _build_altitudes_section(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        header = QHBoxLayout()
        header.addWidget(QLabel("Altitudes"))
        header.addStretch()
        self.btn_add_alt = QPushButton("Add")
        self.btn_remove_alt = QPushButton("Remove")
        header.addWidget(self.btn_add_alt)
        header.addWidget(self.btn_remove_alt)
        layout.addLayout(header)

        self.alt_table = QTableWidget()
        self.alt_table.setColumnCount(len(_ALT_COLUMNS))
        self.alt_table.setHorizontalHeaderLabels(_ALT_COLUMNS)
        self.alt_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch,
        )
        self.alt_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.alt_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.alt_table.verticalHeader().setVisible(False)
        layout.addWidget(self.alt_table)

        # Shown instead of the table in MIN_ALT mode, where a single run already
        # covers every altitude so per-altitude rows would only duplicate work.
        self.lbl_alt_min_alt_note = QLabel(
            "LOS Floor mode computes the lowest line-of-sight "
            "altitude for every location in one pass — receiver altitudes are "
            "not needed here. Use the Altitude Explorer to view any altitude "
            "afterwards."
        )
        self.lbl_alt_min_alt_note.setWordWrap(True)
        self.lbl_alt_min_alt_note.setStyleSheet("color: gray; font-size: 11px;")
        self.lbl_alt_min_alt_note.setVisible(False)
        layout.addWidget(self.lbl_alt_min_alt_note)

        # Add the default altitude row (1.5 m AGL).
        self._add_altitude_row(1.5, "AGL")

        self._altitudes_container = container
        return container

    # -- Analysis parameters -----------------------------------------------

    def _build_analysis_section(self) -> QGroupBox:
        group = QGroupBox("Analysis Parameters")
        layout = QFormLayout(group)

        # DEM Layer, with "Folder…" to pick a local terrain folder.
        dem_row = QHBoxLayout()
        self.combo_dem = QComboBox()
        self.combo_dem.currentIndexChanged.connect(self._on_dem_changed)
        dem_row.addWidget(self.combo_dem, stretch=1)
        self.btn_dem_folder = QPushButton(terrain_dirs.FOLDER_BUTTON_TEXT)
        self.btn_dem_folder.setToolTip(terrain_dirs.FOLDER_BUTTON_TOOLTIP)
        dem_row.addWidget(self.btn_dem_folder)
        layout.addRow("DEM Layer:", dem_row)

        # Source info (read-only)
        self.lbl_dem_info = QLabel("")
        self.lbl_dem_info.setStyleSheet("color: gray; font-size: 11px;")
        layout.addRow("", self.lbl_dem_info)

        # Resolution — Fixed, or a profile that coarsens with distance
        # (core.range_profile): Adaptive = this resolution to 7 km, then
        # stepping up to 90 m over the next 5 km; Custom = drawn by the user.
        self.combo_resolution = QComboBox()
        self.combo_resolution.addItems([str(r) for r in VALID_RESOLUTIONS])
        self.combo_resolution.setCurrentIndex(2)  # default 10
        self.radio_res_fixed = QRadioButton("Fixed")
        self.radio_res_adaptive = QRadioButton("Adaptive")
        self.radio_res_custom = QRadioButton("Custom…")
        self.radio_res_fixed.setChecked(True)
        self.radio_res_fixed.setToolTip("The chosen resolution over the whole range.")
        self.radio_res_adaptive.setToolTip(
            "The chosen resolution out to 7 km, then coarser steps up to 90 m "
            "over the next 5 km, 90 m beyond. Only the near field needs the "
            "fine resolution; the far field is computed much faster. The map "
            "keeps the chosen pixel size everywhere (one layer per site). "
            "Needs engine "
            f"{rp.RANGE_PROFILE_MIN_ENGINE} or newer."
        )
        self.radio_res_custom.setToolTip(
            "Draw your own resolution-against-distance profile."
        )
        self._res_group = QButtonGroup(self)
        for btn in (self.radio_res_fixed, self.radio_res_adaptive, self.radio_res_custom):
            self._res_group.addButton(btn)
        #: Custom profile [(until_m, step_m), ...] for the base resolution it
        #: was drawn for; None until the user draws one.
        self._custom_bands: Optional[list] = None
        #: Custom profile's per-band buildings choices, keyed by band step
        #: (core.range_profile.band_buildings); steps not in it follow the
        #: default rule (on below 90 m). Reset with _custom_bands.
        self._custom_buildings: Dict[int, bool] = {}
        self._res_mode_before_custom = rp.MODE_FIXED
        res_row = QHBoxLayout()
        res_row.setContentsMargins(0, 0, 0, 0)
        res_row.addWidget(self.combo_resolution)
        res_row.addWidget(self.radio_res_fixed)
        res_row.addWidget(self.radio_res_adaptive)
        res_row.addWidget(self.radio_res_custom)
        res_row.addStretch()
        res_widget = QWidget()
        res_widget.setLayout(res_row)
        layout.addRow("Resolution (m):", res_widget)
        self.lbl_res_profile = QLabel("")
        self.lbl_res_profile.setStyleSheet("color: gray; font-size: 11px;")
        self.lbl_res_profile.setWordWrap(True)
        layout.addRow("", self.lbl_res_profile)

        # Output pixel — the map's grid, chosen explicitly (never inferred).
        # Fixed: the resolution itself. Adaptive / Custom: any size from the
        # first step up; the steps keep sampling the rays as drawn.
        self.combo_output_res = QComboBox()
        self.combo_output_res.setToolTip(
            "Pixel size of the result map. One size for the whole map.")
        self.lbl_output_res = QLabel("")
        self.lbl_output_res.setStyleSheet("color: gray; font-size: 11px;")
        self.lbl_output_res.setWordWrap(True)
        layout.addRow("Output pixel (m):", self.combo_output_res)
        layout.addRow("", self.lbl_output_res)
        self._fill_output_res()
        self.combo_output_res.currentIndexChanged.connect(
            lambda _i: self._refresh_output_res())
        self.combo_resolution.currentIndexChanged.connect(self._on_resolution_changed)
        self.radio_res_fixed.toggled.connect(
            lambda on: self._on_res_mode_toggled(on, rp.MODE_FIXED))
        self.radio_res_adaptive.toggled.connect(
            lambda on: self._on_res_mode_toggled(on, rp.MODE_ADAPTIVE))
        self.radio_res_custom.clicked.connect(self._on_custom_resolution)

        # Buildings — fetched from OpenFreeMap and burned into the terrain
        # surface, so they obstruct exactly like terrain does.
        self.chk_buildings = QCheckBox("Include buildings (OpenFreeMap)")
        self.chk_buildings.setToolTip(
            "Download OpenStreetMap building footprints and treat them as part "
            "of the terrain.\n\n"
            "Buildings are only published at zoom 14, so this adds a sizeable "
            "download — the buildings of every terrain tile the run builds, "
            "fetched once and reused. If they cannot all be downloaded the "
            "run stops with the reason; it never builds terrain without "
            "them.\n\n"
            "A prepared .abt terrain folder keeps the buildings it was built "
            "with: this box cannot add them there.\n\n"
            "With Adaptive / Custom resolution this is the master switch: "
            f"Adaptive skips buildings on bands of {rp.BUILDINGS_SKIP_STEP_M} m "
            "and coarser; Custom sets them per band (same default). Unchecked, "
            "no band gets buildings.\n\n"
            + source_credit("OpenFreeMap")
        )
        layout.addRow("Buildings:", self.chk_buildings)
        self.chk_buildings.toggled.connect(self._on_buildings_toggled)

        # Compute Backend
        self.combo_backend = QComboBox()
        self.combo_backend.addItems(["AUTO", "GPU", "CPU"])
        layout.addRow("Backend:", self.combo_backend)

        # Earth Radius Mode — applies to all propagation models (LOS included),
        # not just ITM. FOUR_THIRDS (normal) is the default; ADVANCED enables
        # the refractivity-based effective-radius model.
        self.combo_earth_radius = QComboBox()
        self.combo_earth_radius.addItems(["FOUR_THIRDS", "ADVANCED"])
        self.combo_earth_radius.setToolTip(
            "Earth-curvature model for horizon/diffraction geometry.\n"
            "FOUR_THIRDS: standard 4/3-earth refraction (normal, recommended).\n"
            "ADVANCED: derive effective radius from surface refractivity."
        )
        layout.addRow("Earth Radius Mode:", self.combo_earth_radius)

        # Output Directory
        dir_layout = QHBoxLayout()
        self.line_output_dir = QLineEdit()
        # Remembered across dialog opens (the dialog is rebuilt each time).
        bind_output_dir(self.line_output_dir, SITE_OUTPUT_DIR_KEY)
        dir_layout.addWidget(self.line_output_dir)
        self.btn_browse_dir = QPushButton("Browse...")
        dir_layout.addWidget(self.btn_browse_dir)
        layout.addRow("Output Dir:", dir_layout)

        return group

    # -- ITM parameters ----------------------------------------------------

    def _build_itm_section(self) -> QGroupBox:
        self.itm_group = QGroupBox("ITM Parameters")
        self.itm_group.setVisible(False)
        layout = QFormLayout(self.itm_group)

        # Ground Permittivity
        self.spin_eps = QDoubleSpinBox()
        self.spin_eps.setRange(0.0, 100.0)
        self.spin_eps.setValue(15.0)
        self.spin_eps.setDecimals(2)
        layout.addRow("Ground Permittivity:", self.spin_eps)

        # Ground Conductivity
        self.spin_sgm = QDoubleSpinBox()
        self.spin_sgm.setRange(0.0001, 10.0)
        self.spin_sgm.setValue(0.005)
        self.spin_sgm.setDecimals(4)
        layout.addRow("Ground Conductivity:", self.spin_sgm)

        # Surface Refractivity
        self.spin_ens = QDoubleSpinBox()
        self.spin_ens.setRange(200.0, 450.0)
        self.spin_ens.setValue(301.0)
        self.spin_ens.setDecimals(1)
        layout.addRow("Surface Refractivity:", self.spin_ens)

        # Radio Climate
        self.combo_climate = QComboBox()
        self.combo_climate.addItems([
            "Equatorial (1)",
            "Continental Subtropical (2)",
            "Maritime Subtropical (3)",
            "Desert (4)",
            "Continental Temperate (5)",
            "Maritime Temperate (6)",
        ])
        self.combo_climate.setCurrentIndex(4)  # Continental Temperate
        layout.addRow("Radio Climate:", self.combo_climate)

        # Polarization
        self.combo_pol = QComboBox()
        self.combo_pol.addItems(["Horizontal (0)", "Vertical (1)"])
        layout.addRow("Polarization:", self.combo_pol)

        # Confidence
        self.spin_conf = QDoubleSpinBox()
        self.spin_conf.setRange(0.01, 0.99)
        self.spin_conf.setValue(0.50)
        self.spin_conf.setDecimals(2)
        self.spin_conf.setSingleStep(0.05)
        layout.addRow("Confidence:", self.spin_conf)

        # Reliability
        self.spin_rel = QDoubleSpinBox()
        self.spin_rel.setRange(0.01, 0.99)
        self.spin_rel.setValue(0.50)
        self.spin_rel.setDecimals(2)
        self.spin_rel.setSingleStep(0.05)
        layout.addRow("Reliability:", self.spin_rel)

        return self.itm_group

    # ==================================================================
    # Signal wiring
    # ==================================================================

    def _connect_signals(self) -> None:
        self.btn_add_site.clicked.connect(self._on_add_site)
        self.btn_remove_site.clicked.connect(self._on_remove_site)
        self.btn_add_alt.clicked.connect(self._on_add_altitude)
        self.btn_remove_alt.clicked.connect(self._on_remove_altitude)
        self.btn_browse_dir.clicked.connect(self._on_browse_dir)
        self.btn_dem_folder.clicked.connect(self._on_pick_terrain_dir)
        self.btn_run.clicked.connect(self._on_run)
        self.btn_stop.clicked.connect(self._on_stop)

    # ==================================================================
    # Populate raster layers
    # ==================================================================

    @staticmethod
    def _dem_entry_key(entry) -> str:
        """Stable identity for a combo entry (layer id, or dir path string)."""
        if isinstance(entry, str):
            return entry
        try:
            return entry.id()
        except Exception:
            return entry.name()

    def _current_dem_key(self) -> Optional[str]:
        idx = self.combo_dem.currentIndex()
        if getattr(self, "_dem_layers", None) and 0 <= idx < len(self._dem_layers):
            return self._dem_entry_key(self._dem_layers[idx])
        return None

    def _populate_raster_layers(self, select_dir: str = "") -> None:
        """Fill the DEM combo: the local terrain folders (Settings → Local
        terrain dir, then the folders picked with "Folder…" —
        ``terrain_dirs.combo_folders``), then the project's raster layers.

        Preserves the current selection across refills so a live update (a
        layer added or removed in the project) does not reset the user's
        choice; *select_dir* (a folder just picked) is selected instead.
        """
        prev_key = self._current_dem_key()

        self.combo_dem.blockSignals(True)
        self.combo_dem.clear()
        self._dem_layers: list = []  # QgsRasterLayer or str (path)

        terrain_dirs.add_folder_entries(self.combo_dem, self._dem_layers)

        for layer in QgsProject.instance().mapLayers().values():
            # Skip our own result rasters (coverage/P2P) — they are never valid
            # DEM inputs and only clutter the picker. Terrain we generated is
            # kept (it IS elevation data).
            if isinstance(layer, QgsRasterLayer) and not hide_from_dem_picker(layer):
                self.combo_dem.addItem(layer.name())
                self._dem_layers.append(layer)

        # Select the folder just picked, else restore the previous selection
        # if it still exists.
        picked = terrain_dirs.folder_index(self._dem_layers, select_dir)
        if picked >= 0:
            self.combo_dem.setCurrentIndex(picked)
        elif prev_key is not None:
            for i, entry in enumerate(self._dem_layers):
                if self._dem_entry_key(entry) == prev_key:
                    self.combo_dem.setCurrentIndex(i)
                    break
        self.combo_dem.blockSignals(False)
        self._on_dem_changed()

    def _on_project_layers_changed(self, *args) -> None:
        """Repopulate the DEM combo when project layers are added/removed."""
        self._populate_raster_layers()

    def _on_pick_terrain_dir(self) -> None:
        """"Folder…": pick a local terrain folder in the file browser. It is
        selected at once and listed in both tabs' DEM combo from now on;
        Cancel changes nothing."""
        path = terrain_dirs.ask_terrain_dir(self, self._selected_terrain_dir())
        if path:
            self._populate_raster_layers(select_dir=path)
            terrain_dirs.announce_pick(self._dialog, self)

    def _selected_dem_layer(self) -> Optional[QgsRasterLayer]:
        idx = self.combo_dem.currentIndex()
        if 0 <= idx < len(self._dem_layers):
            entry = self._dem_layers[idx]
            if isinstance(entry, str):
                return None  # local dir, not a layer
            return entry
        return None

    def _selected_terrain_dir(self) -> Optional[str]:
        """Return the selected local terrain dir, or None if a QGIS layer is selected."""
        idx = self.combo_dem.currentIndex()
        if 0 <= idx < len(self._dem_layers):
            entry = self._dem_layers[idx]
            if isinstance(entry, str):
                return entry
        return None

    def _on_dem_changed(self) -> None:
        layer = self._selected_dem_layer()
        terrain_dir = self._selected_terrain_dir()
        if terrain_dir:
            self.lbl_dem_info.setText(f"Local directory: {terrain_dir}")
        elif layer:
            info = get_source_resolution_info(layer)
            if dem_layer_warning(layer):
                self.lbl_dem_info.setText(
                    f"⚠ Not elevation data (looks like imagery) — {info}"
                )
            else:
                self.lbl_dem_info.setText(f"Source resolution: {info}")
        else:
            self.lbl_dem_info.setText("")

    # ==================================================================
    # Mode switching
    # ==================================================================

    def set_mode(self, mode: str) -> None:
        """Update internal mode, column visibility, and ITM visibility.

        LOS mode hides the AZ Rotation column (antenna rotation is
        irrelevant for pure geometric line-of-sight).
        """
        self._current_mode = mode
        # LOS and MIN_ALT are both purely geometric — no assets
        # (freq/power/patterns) and no antenna AZ rotation.
        is_geometric = mode in ("LOS", "MIN_ALT")
        is_min_alt = mode == "MIN_ALT"
        self.sites_table.setColumnHidden(_COL_ASSET, is_geometric)
        self.sites_table.setColumnHidden(_COL_AZ_ROTATION, is_geometric)
        # In MIN_ALT mode the per-altitude table is replaced by an explanatory
        # note (the single run already spans all altitudes).
        self.alt_table.setVisible(not is_min_alt)
        self.btn_add_alt.setVisible(not is_min_alt)
        self.btn_remove_alt.setVisible(not is_min_alt)
        self.lbl_alt_min_alt_note.setVisible(is_min_alt)
        self._update_itm_visibility()

    def _update_itm_visibility(self) -> None:
        """Show ITM parameters only when mode is LOSS and model is ITM."""
        mode = self._dialog.get_mode()
        if mode == "LOSS":
            loss_model = self._dialog.get_loss_model()
            self.itm_group.setVisible(loss_model == "ITM")
        else:
            self.itm_group.setVisible(False)

    # ==================================================================
    # Sites table management
    # ==================================================================

    def _add_site_row_internal(self) -> int:
        """Insert a new row into the sites table and return its index."""
        row = self.sites_table.rowCount()
        self.sites_table.insertRow(row)

        # -- Column 0: Asset (QComboBox dropdown) --
        combo_asset = QComboBox()
        combo_asset.addItem("")  # blank default
        combo_asset.addItems(self._asset_names())
        combo_asset.currentTextChanged.connect(
            lambda text, r=row: self._on_asset_changed(r, text),
        )
        self.sites_table.setCellWidget(row, _COL_ASSET, combo_asset)

        # -- Column 1: Location (lat spinbox + lon spinbox + Pick button) --
        loc_container = QWidget()
        loc_layout = QHBoxLayout(loc_container)
        loc_layout.setContentsMargins(2, 0, 2, 0)
        loc_layout.setSpacing(2)

        spin_lat = QDoubleSpinBox()
        spin_lat.setObjectName("spin_lat")
        spin_lat.setRange(-90.0, 90.0)
        spin_lat.setDecimals(6)
        spin_lat.setValue(0.0)
        spin_lat.setPrefix("Lat ")
        spin_lat.setMinimumWidth(130)
        spin_lat.valueChanged.connect(
            lambda val, r=row: self._on_coord_spinbox_changed(r),
        )
        loc_layout.addWidget(spin_lat, 1)

        spin_lon = QDoubleSpinBox()
        spin_lon.setObjectName("spin_lon")
        spin_lon.setRange(-180.0, 180.0)
        spin_lon.setDecimals(6)
        spin_lon.setValue(0.0)
        spin_lon.setPrefix("Lon ")
        spin_lon.setMinimumWidth(130)
        spin_lon.valueChanged.connect(
            lambda val, r=row: self._on_coord_spinbox_changed(r),
        )
        loc_layout.addWidget(spin_lon, 1)

        pick_btn = QPushButton("Pick")
        pick_btn.setMaximumWidth(48)
        pick_btn.clicked.connect(
            lambda checked, r=row: self._on_pick_location(r),
        )
        loc_layout.addWidget(pick_btn)

        self.sites_table.setCellWidget(row, _COL_LOCATION, loc_container)

        # -- Column 2: Height (m) --
        spin_height = QDoubleSpinBox()
        spin_height.setRange(MIN_AMSL_M, 10000.0)
        spin_height.setValue(30.0)
        spin_height.setDecimals(1)
        spin_height.setSuffix(" m")
        self.sites_table.setCellWidget(row, _COL_HEIGHT, spin_height)

        # -- Column 3: Height Mode --
        combo_mode = QComboBox()
        combo_mode.addItems(["AGL", "AMSL"])
        self.sites_table.setCellWidget(row, _COL_HEIGHT_MODE, combo_mode)

        # The height's lower bound depends on the mode beside it: a hard 1 m
        # floor for AGL, below sea level allowed for AMSL. See gui.height_inputs.
        bind_height_mode(spin_height, combo_mode)

        # -- Column 4: AZ Rotation (deg) --
        spin_az_rot = QDoubleSpinBox()
        spin_az_rot.setRange(0.0, 360.0)
        spin_az_rot.setValue(0.0)
        spin_az_rot.setDecimals(1)
        spin_az_rot.setSuffix("\u00b0")
        self.sites_table.setCellWidget(row, _COL_AZ_ROTATION, spin_az_rot)

        # -- Column 5: AZ Start (deg) --
        spin_az_start = QDoubleSpinBox()
        spin_az_start.setRange(0.0, 360.0)
        spin_az_start.setValue(0.0)
        spin_az_start.setDecimals(1)
        spin_az_start.setSuffix("\u00b0")
        self.sites_table.setCellWidget(row, _COL_AZ_START, spin_az_start)

        # -- Column 6: AZ End (deg) --
        spin_az_end = QDoubleSpinBox()
        spin_az_end.setRange(0.0, 360.0)
        spin_az_end.setValue(360.0)
        spin_az_end.setDecimals(1)
        spin_az_end.setSuffix("\u00b0")
        self.sites_table.setCellWidget(row, _COL_AZ_END, spin_az_end)

        # -- Column 7: Range (km) --
        spin_range = QSpinBox()
        spin_range.setRange(1, 500)
        spin_range.setValue(50)
        spin_range.setSuffix(" km")
        self.sites_table.setCellWidget(row, _COL_RANGE, spin_range)

        return row

    def _on_asset_changed(self, row: int, asset_name: str) -> None:
        """Auto-fill height and height mode from the selected asset."""
        asset = self._get_asset(asset_name)
        if asset is None:
            return

        # Auto-fill height mode *first*: it drives the height spinbox's minimum
        # (1 m for AGL, below sea level for AMSL), so filling the height before
        # the mode would clamp an AMSL asset's negative elevation to the AGL
        # floor and the later mode switch would not bring it back.
        combo_m = self.sites_table.cellWidget(row, _COL_HEIGHT_MODE)
        if isinstance(combo_m, QComboBox):
            mode_text = asset.get("default_height_mode", "AGL")
            idx = combo_m.findText(mode_text)
            if idx >= 0:
                combo_m.setCurrentIndex(idx)

        # Auto-fill height.
        spin_h = self.sites_table.cellWidget(row, _COL_HEIGHT)
        if isinstance(spin_h, QDoubleSpinBox):
            default_h = asset.get("default_height_m", 30.0)
            spin_h.setValue(default_h)

    def _on_coord_spinbox_changed(self, row: int) -> None:
        """Update internal coords dict when spinboxes are edited manually."""
        loc_container = self.sites_table.cellWidget(row, _COL_LOCATION)
        if loc_container is None:
            return
        spin_lat = loc_container.findChild(QDoubleSpinBox, "spin_lat")
        spin_lon = loc_container.findChild(QDoubleSpinBox, "spin_lon")
        if spin_lat is not None and spin_lon is not None:
            self._site_coords[row] = (spin_lat.value(), spin_lon.value())
        self._update_rubber_band()

    def _update_rubber_band(self) -> None:
        """Redraw site position markers on the map canvas."""
        self._sites_rb.reset(QgsWkbTypes.GeometryType.PointGeometry)
        canvas = self.iface.mapCanvas()
        canvas_crs = canvas.mapSettings().destinationCrs()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        xform = QgsCoordinateTransform(wgs84, canvas_crs, QgsProject.instance())

        for _row, (lat, lon) in sorted(self._site_coords.items()):
            if lat == 0.0 and lon == 0.0:
                continue
            pt = xform.transform(QgsPointXY(lon, lat))
            self._sites_rb.addPoint(pt)

    def _on_add_site(self) -> None:
        """Add a new site row with default values."""
        self._add_site_row_internal()

    def _on_remove_site(self) -> None:
        """Remove the currently selected site row."""
        selected = self.sites_table.selectedItems()
        if not selected:
            row = self.sites_table.currentRow()
        else:
            row = selected[0].row()

        if row < 0:
            return

        self.sites_table.removeRow(row)

        # Rebuild _site_coords with shifted indices.
        new_coords: Dict[int, Tuple[float, float]] = {}
        for r, coord in self._site_coords.items():
            if r < row:
                new_coords[r] = coord
            elif r > row:
                new_coords[r - 1] = coord
            # r == row is the removed entry -- skip it.
        self._site_coords = new_coords

        # Rebind pick buttons and spinbox callbacks to updated row indices.
        self._rebind_row_callbacks()

        # Redraw the canvas markers so the removed site's dot disappears.
        self._update_rubber_band()

    def _rebind_row_callbacks(self) -> None:
        """Reconnect row-indexed callbacks after row removal."""
        for row in range(self.sites_table.rowCount()):
            # Rebind asset combo.
            combo_asset = self.sites_table.cellWidget(row, _COL_ASSET)
            if isinstance(combo_asset, QComboBox):
                try:
                    combo_asset.currentTextChanged.disconnect()
                except TypeError:
                    pass
                combo_asset.currentTextChanged.connect(
                    lambda text, r=row: self._on_asset_changed(r, text),
                )

            # Rebind location widget children.
            loc_container = self.sites_table.cellWidget(row, _COL_LOCATION)
            if loc_container is None:
                continue

            # Pick button.
            pick_btn = loc_container.findChild(QPushButton)
            if pick_btn is not None:
                try:
                    pick_btn.clicked.disconnect()
                except TypeError:
                    pass
                pick_btn.clicked.connect(
                    lambda checked, r=row: self._on_pick_location(r),
                )

            # Lat/lon spinboxes.
            spin_lat = loc_container.findChild(QDoubleSpinBox, "spin_lat")
            if spin_lat is not None:
                try:
                    spin_lat.valueChanged.disconnect()
                except TypeError:
                    pass
                spin_lat.valueChanged.connect(
                    lambda val, r=row: self._on_coord_spinbox_changed(r),
                )

            spin_lon = loc_container.findChild(QDoubleSpinBox, "spin_lon")
            if spin_lon is not None:
                try:
                    spin_lon.valueChanged.disconnect()
                except TypeError:
                    pass
                spin_lon.valueChanged.connect(
                    lambda val, r=row: self._on_coord_spinbox_changed(r),
                )

    def _on_pick_location(self, row: int) -> None:
        """Activate map point capture for the given site row."""
        def _callback(lat: float, lon: float) -> None:
            self._site_coords[row] = (lat, lon)
            self._update_location_spinboxes(row, lat, lon)
            self._update_rubber_band()
            notify = getattr(self._dialog, "sites_picked_in", None)
            if callable(notify):
                notify(self)

        # Keep a reference to prevent garbage collection.
        tool = activate_point_capture(self.iface, _callback)
        self._pick_tools.append(tool)

        # Minimize the parent dialog so the user can interact with the map.
        parent_window = self._dialog
        if hasattr(parent_window, "showMinimized"):
            parent_window.showMinimized()

    def clear_sites(self) -> None:
        """Zero every site location.

        Counterpart to ``P2PTab.clear_sites`` — see the note there. The rows
        themselves stay, so the user keeps their heights, ranges and asset
        choices and only re-places the points.
        """
        for row in range(self.sites_table.rowCount()):
            self._site_coords[row] = (0.0, 0.0)
            self._update_location_spinboxes(row, 0.0, 0.0)
        self._update_rubber_band()

    def _update_location_spinboxes(
        self, row: int, lat: float, lon: float,
    ) -> None:
        """Update the lat/lon spinboxes in the sites table for *row*."""
        if row >= self.sites_table.rowCount():
            return
        loc_container = self.sites_table.cellWidget(row, _COL_LOCATION)
        if loc_container is None:
            return

        spin_lat = loc_container.findChild(QDoubleSpinBox, "spin_lat")
        spin_lon = loc_container.findChild(QDoubleSpinBox, "spin_lon")

        # Block signals to avoid recursive updates while setting values.
        if spin_lat is not None:
            spin_lat.blockSignals(True)
            spin_lat.setValue(lat)
            spin_lat.blockSignals(False)
        if spin_lon is not None:
            spin_lon.blockSignals(True)
            spin_lon.setValue(lon)
            spin_lon.blockSignals(False)

        # Restore the parent dialog.
        parent_window = self._dialog
        if hasattr(parent_window, "showNormal"):
            parent_window.showNormal()
        if hasattr(parent_window, "activateWindow"):
            parent_window.activateWindow()

    # ==================================================================
    # Altitudes table management
    # ==================================================================

    def _add_altitude_row(
        self, altitude: float = 1.5, reference: str = "AGL",
    ) -> int:
        """Insert a new altitude row and return its index."""
        row = self.alt_table.rowCount()
        self.alt_table.insertRow(row)

        spin_alt = QDoubleSpinBox()
        spin_alt.setRange(MIN_AMSL_M, 10000.0)
        spin_alt.setValue(altitude)
        spin_alt.setDecimals(1)
        spin_alt.setSuffix(" m")
        self.alt_table.setCellWidget(row, 0, spin_alt)

        combo_ref = QComboBox()
        combo_ref.addItems(["AGL", "AMSL"])
        idx = combo_ref.findText(reference)
        if idx >= 0:
            combo_ref.setCurrentIndex(idx)
        self.alt_table.setCellWidget(row, 1, combo_ref)

        # Bound after the mode is set so the initial minimum matches it: 1 m AGL
        # floor, or below sea level when the row is AMSL. See gui.height_inputs.
        bind_height_mode(spin_alt, combo_ref)

        return row

    def _on_add_altitude(self) -> None:
        """Add a new altitude row with defaults."""
        self._add_altitude_row()

    def _on_remove_altitude(self) -> None:
        """Remove the currently selected altitude row."""
        selected = self.alt_table.selectedItems()
        if not selected:
            row = self.alt_table.currentRow()
        else:
            row = selected[0].row()
        if row >= 0:
            self.alt_table.removeRow(row)

    # ==================================================================
    # Browse directory
    # ==================================================================

    def _on_browse_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select Output Directory", self.line_output_dir.text(),
        )
        if path:
            self.line_output_dir.setText(path)
            save_output_dir(SITE_OUTPUT_DIR_KEY, path)

    # ==================================================================
    # Read tables into structured data
    # ==================================================================

    def _read_sites(self) -> List[Dict]:
        """Read all site rows into a list of dicts."""
        sites: List[Dict] = []
        for row in range(self.sites_table.rowCount()):
            site: Dict = {}

            # Asset name.
            combo_a = self.sites_table.cellWidget(row, _COL_ASSET)
            site["asset_name"] = (
                combo_a.currentText() if isinstance(combo_a, QComboBox) else ""
            )

            # Coordinates.
            site["coords"] = self._site_coords.get(row)

            # Height.
            spin_h = self.sites_table.cellWidget(row, _COL_HEIGHT)
            site["height"] = (
                spin_h.value() if isinstance(spin_h, QDoubleSpinBox) else 30.0
            )

            # Height mode.
            combo_m = self.sites_table.cellWidget(row, _COL_HEIGHT_MODE)
            site["height_mode"] = (
                combo_m.currentText() if isinstance(combo_m, QComboBox) else "AGL"
            )

            # AZ Rotation.
            spin_azr = self.sites_table.cellWidget(row, _COL_AZ_ROTATION)
            site["az_rotation"] = (
                spin_azr.value()
                if isinstance(spin_azr, QDoubleSpinBox)
                else 0.0
            )

            # AZ Start.
            spin_azs = self.sites_table.cellWidget(row, _COL_AZ_START)
            site["az_start"] = (
                spin_azs.value()
                if isinstance(spin_azs, QDoubleSpinBox)
                else 0.0
            )

            # AZ End.
            spin_aze = self.sites_table.cellWidget(row, _COL_AZ_END)
            site["az_end"] = (
                spin_aze.value()
                if isinstance(spin_aze, QDoubleSpinBox)
                else 360.0
            )

            # Range.
            spin_r = self.sites_table.cellWidget(row, _COL_RANGE)
            site["range"] = (
                spin_r.value() if isinstance(spin_r, QSpinBox) else 50
            )

            sites.append(site)
        return sites

    def _read_altitudes(self) -> List[Tuple[float, str]]:
        """Read all altitude rows into a list of (altitude, reference)."""
        altitudes: List[Tuple[float, str]] = []
        for row in range(self.alt_table.rowCount()):
            spin = self.alt_table.cellWidget(row, 0)
            alt = spin.value() if isinstance(spin, QDoubleSpinBox) else 1.5
            combo = self.alt_table.cellWidget(row, 1)
            ref = combo.currentText() if isinstance(combo, QComboBox) else "AGL"
            altitudes.append((alt, ref))
        return altitudes

    # ==================================================================
    # Build CoverageParams for each (site x altitude) combination
    # ==================================================================

    # -- Resolution profile (Fixed / Adaptive / Custom) ---------------------

    def _resolution_mode(self) -> str:
        if self.radio_res_adaptive.isChecked():
            return rp.MODE_ADAPTIVE
        if self.radio_res_custom.isChecked():
            return rp.MODE_CUSTOM
        return rp.MODE_FIXED

    def _profile_range_km(self) -> float:
        """Range the profile label and the Custom editor are drawn for — the
        longest site range (each site's profile is clipped to its own)."""
        ranges = [float(s.get("range") or 0) for s in self._read_sites()]
        return max(ranges + [0.0]) or 50.0

    def _bands_for(self, base_m: int, range_km: float) -> Optional[list]:
        """The profile for one site, or None for a fixed (single-band) run."""
        mode = self._resolution_mode()
        if mode == rp.MODE_ADAPTIVE:
            bands = rp.adaptive_bands(base_m, range_km)
        elif mode == rp.MODE_CUSTOM and self._custom_bands:
            bands = rp.normalise_bands(self._custom_bands, range_km)
        else:
            bands = None
        return bands if bands and len(bands) > 1 else None

    def _band_buildings_for(self, bands: list) -> Tuple[List[bool], List[str]]:
        """Per-band buildings flags for *bands* and the reason for each: the
        "Include buildings" checkbox is the master switch, Adaptive skips
        bands at >= 90 m, Custom uses the editor's per-band choice."""
        mode = self._resolution_mode()
        master = self.chk_buildings.isChecked()
        overrides = self._custom_buildings if mode == rp.MODE_CUSTOM else None
        flags = rp.band_buildings(bands, overrides, master)
        return flags, rp.buildings_notes(bands, flags, mode, master)

    def _refresh_res_profile(self) -> None:
        base = int(self.combo_resolution.currentText())
        mode = self._resolution_mode()
        if mode == rp.MODE_FIXED:
            self.lbl_res_profile.setText("")
            return
        bands = self._bands_for(base, self._profile_range_km())
        if bands is None:
            self.lbl_res_profile.setText(
                f"{base} m over the whole range — already as coarse as the far "
                f"step, or the range ends inside the near field.")
        else:
            text = rp.describe(bands)
            if self.chk_buildings.isChecked():
                flags, _ = self._band_buildings_for(bands)
                with_b = [f"{s} m" for (_, s), f in zip(bands, flags) if f]
                text += (" — buildings on " + ", ".join(with_b) if with_b
                         else " — no band gets buildings")
            self.lbl_res_profile.setText(text)

    def _output_res(self) -> int:
        """The result map's pixel size: the resolution in Fixed mode, the
        user's explicit choice with Adaptive / Custom."""
        base = int(self.combo_resolution.currentText())
        if self._resolution_mode() == rp.MODE_FIXED or not self.combo_output_res.count():
            return base
        return int(self.combo_output_res.currentText())

    def _fill_output_res(self) -> None:
        """Offer the first step and every coarser size; keep a still-valid pick."""
        base = int(self.combo_resolution.currentText())
        keep = self.combo_output_res.currentText()
        self.combo_output_res.blockSignals(True)
        self.combo_output_res.clear()
        self.combo_output_res.addItems([str(r) for r in VALID_RESOLUTIONS if r >= base])
        idx = self.combo_output_res.findText(keep)
        self.combo_output_res.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo_output_res.blockSignals(False)
        self._refresh_output_res()

    def _refresh_output_res(self) -> None:
        fixed = self._resolution_mode() == rp.MODE_FIXED
        base = int(self.combo_resolution.currentText())
        if fixed:
            self.combo_output_res.blockSignals(True)
            self.combo_output_res.setCurrentIndex(0)  # = the resolution
            self.combo_output_res.blockSignals(False)
        self.combo_output_res.setEnabled(not fixed)
        out = self._output_res()
        text = ("One pixel size for the whole map. The steps above set how finely "
                "the rays sample terrain; this sets the map grid. Coarser = much "
                "smaller file, near field still computed at the fine steps (each "
                "pixel shows one point of its cell, not an average).")
        if fixed:
            text = "Fixed: the output pixel is the resolution (" + f"{base} m)."
        with contextlib.suppress(Exception):  # the estimate is informative only
            from ..core import output_estimate as oe
            mode = self._dialog.get_mode()
            model = ("MIN_ALT" if mode == "MIN_ALT"
                     else self._dialog.get_loss_model() if mode == "LOSS" else "LOS")
            size = oe.engine_output_bytes(model, self._profile_range_km(), out)
            text += f" Result at {out} m to {self._profile_range_km():g} km: ~{oe.format_bytes(size)}."
        self.lbl_output_res.setText(text)

    def _on_resolution_changed(self, _index: int) -> None:
        # A custom profile is drawn from one base resolution; another base
        # starts it over (the first band IS the chosen resolution).
        base = int(self.combo_resolution.currentText())
        if self._custom_bands and self._custom_bands[0][1] != base:
            self._custom_bands = None
            self._custom_buildings = {}
            if self.radio_res_custom.isChecked():
                self.radio_res_adaptive.setChecked(True)
        self._refresh_res_profile()
        self._fill_output_res()

    def _on_res_mode_toggled(self, on: bool, mode: str) -> None:
        if on:
            self._res_mode_before_custom = mode  # where a cancelled Custom returns
        self._refresh_res_profile()
        self._refresh_output_res()

    def _on_custom_resolution(self) -> None:
        """Custom clicked: open the profile editor (again, on every click)."""
        base = int(self.combo_resolution.currentText())
        dlg = RangeProfileDialog(base, self._profile_range_km(), self._custom_bands, self,
                                 buildings=self._custom_buildings,
                                 buildings_master=self.chk_buildings.isChecked())
        if dlg.exec() == dlg.DialogCode.Accepted:
            self._custom_bands = dlg.bands()
            self._custom_buildings = dlg.buildings()
            self.radio_res_custom.setChecked(True)
        elif not self._custom_bands:
            # Nothing drawn yet: Custom has no profile to run, go back.
            back = (self.radio_res_adaptive if self._res_mode_before_custom == rp.MODE_ADAPTIVE
                    else self.radio_res_fixed)
            back.setChecked(True)
        self._refresh_res_profile()
        self._refresh_output_res()

    def _build_jobs(self) -> List[Tuple[CoverageParams, str]]:
        """Create a (CoverageParams, display_name) tuple for every combination.

        Reads freq_mhz, erp_watts, az_pattern, and el_pattern from the
        selected asset for each site.  Returns an empty list if validation
        fails (the caller should not proceed).
        """
        mode = self._dialog.get_mode()
        if mode == "LOSS":
            model = self._dialog.get_loss_model()
        elif mode == "MIN_ALT":
            model = "MIN_ALT"
        else:
            model = "LOS"

        resolution = int(self.combo_resolution.currentText())
        backend = self.combo_backend.currentText()

        # ITM parameters (read unconditionally; only used when model == ITM).
        climate_value = self.combo_climate.currentIndex() + 1

        sites = self._read_sites()
        # MIN_ALT sweeps every altitude internally (the result is independent of
        # the receiver height), so one job per site suffices — using the whole
        # altitude table would just recompute identical rasters.
        if model == "MIN_ALT":
            altitudes = [(1.5, "AGL")]
        else:
            altitudes = self._read_altitudes()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        jobs: List[Tuple[CoverageParams, str]] = []

        for site_idx, site in enumerate(sites):
            coords = site.get("coords")
            if coords is None:
                continue  # skip sites without location

            tx_lat, tx_lon = coords

            # Resolve asset data for this site.
            asset_name = site.get("asset_name", "")
            asset = self._get_asset(asset_name)

            # If the asset has a file path, reload it for fresh data.
            if asset and asset.get("_path"):
                with contextlib.suppress(Exception):  # fall back to cached version
                    asset = load_asset(asset["_path"])

            # Extract RF parameters from asset (with sensible fallbacks).
            freq_mhz = 433.0
            erp_watts = 10.0
            az_pattern = None
            el_pattern = None

            if asset:
                freq_mhz = asset.get("frequency_mhz", 433.0)
                erp_watts = asset.get("erp_watts", 10.0)
                if erp_watts <= 0:
                    erp_watts = compute_erp(
                        asset.get("peak_power_watts", 10.0),
                        asset.get("antenna_gain_dbi", 0.0),
                    )
                # Inline patterns are not file paths — aether_core needs
                # .az/.el files. Leave as None for now (omnidirectional).
                az_pattern = None
                el_pattern = None

            for alt_idx, (rx_alt, rx_ref) in enumerate(altitudes):
                output_name = (
                    f"site{site_idx + 1}_alt{alt_idx + 1}_{timestamp}"
                )
                if model == "MIN_ALT":
                    display_name = (
                        f"Site {site_idx + 1} "
                        f"({tx_lat:.4f}, {tx_lon:.4f}) "
                        f"— LOS Floor"
                    )
                else:
                    display_name = (
                        f"Site {site_idx + 1} "
                        f"({tx_lat:.4f}, {tx_lon:.4f}) "
                        f"@ {rx_alt:.1f}m {rx_ref}"
                    )

                params = CoverageParams(
                    tx_lat=tx_lat,
                    tx_lon=tx_lon,
                    tx_height=site.get("height", 30.0),
                    tx_mode=site.get("height_mode", "AGL"),
                    freq_mhz=freq_mhz,
                    erp_watts=erp_watts,
                    az_pattern=az_pattern,
                    el_pattern=el_pattern,
                    az_rotation=site.get("az_rotation", 0.0),
                    rx_height=rx_alt,
                    rx_mode=rx_ref,
                    model=model,
                    resolution_m=resolution,
                    max_range_km=site.get("range", 50),
                    az_start=site.get("az_start", 0.0),
                    az_end=site.get("az_end", 360.0),
                    backend=backend,
                    output_name=output_name,
                    # ITM propagation parameters.
                    eps=self.spin_eps.value(),
                    sgm=self.spin_sgm.value(),
                    ens=self.spin_ens.value(),
                    climate=climate_value,
                    pol=self.combo_pol.currentIndex(),
                    conf=self.spin_conf.value(),
                    rel=self.spin_rel.value(),
                    earth_radius=self.combo_earth_radius.currentText(),
                    add_to_map=True,
                )
                bands = self._bands_for(resolution, float(params.max_range_km))
                if bands:
                    # Adaptive / custom: the same single job, with the profile,
                    # written at the output pixel the user chose.
                    out_res = self._output_res()
                    flags, notes = self._band_buildings_for(bands)
                    params = dataclasses.replace(params, range_profile=list(bands),
                                                 range_profile_buildings=flags,
                                                 range_profile_buildings_notes=notes,
                                                 resolution_m=out_res)
                    display_name = f"{display_name} — {rp.describe(bands)}"
                    if out_res != resolution:
                        display_name += f" · output {out_res} m"
                elif self._resolution_mode() == rp.MODE_CUSTOM:
                    # This site's range ends inside the first custom band: a
                    # fixed run at that band's step, which keeps the band's
                    # buildings choice (one flag, read by the worker).
                    flags, notes = self._band_buildings_for(
                        [(float(params.max_range_km) * 1000.0, resolution)])
                    params = dataclasses.replace(params, range_profile_buildings=flags,
                                                 range_profile_buildings_notes=notes)
                jobs.append((params, display_name))

        return jobs

    # ==================================================================
    # Run analysis
    # ==================================================================

    def _on_run(self) -> None:
        """Validate inputs and launch the analysis worker."""
        # ---- Validate ----
        if self.sites_table.rowCount() == 0:
            QMessageBox.warning(
                self, "No Sites",
                "Please add at least one site before running the analysis.",
            )
            return

        # Check that at least one site has coordinates.
        sites = self._read_sites()
        sites_with_coords = [s for s in sites if s.get("coords") is not None]
        if not sites_with_coords:
            QMessageBox.warning(
                self, "No Site Locations",
                "Please set a location for at least one site.",
            )
            return

        if self.alt_table.rowCount() == 0:
            QMessageBox.warning(
                self, "No Altitudes",
                "Please add at least one receiver altitude.",
            )
            return

        dem_layer = self._selected_dem_layer()
        terrain_dir = self._selected_terrain_dir()
        if dem_layer is None and not terrain_dir:
            QMessageBox.warning(
                self, "No DEM",
                "Please select a DEM layer or configure a local terrain directory.",
            )
            return

        # Guard against picking a basemap/imagery layer (e.g. OpenStreetMap)
        # as the terrain source — elevation data is required.
        if dem_layer is not None:
            warn = dem_layer_warning(dem_layer)
            if warn and QMessageBox.warning(
                self, "Not a DEM?", warn + "\n\nUse it anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return

        output_dir = self.line_output_dir.text().strip()
        if not output_dir:
            QMessageBox.warning(
                self, "No Output Directory",
                "Please specify an output directory.",
            )
            return
        # The folder this run uses is the one the next dialog opens with.
        save_output_dir(SITE_OUTPUT_DIR_KEY, output_dir)

        # ---- The engine and the API key have to be there ----
        # Checked before anything else: terrain preparation falls back to the
        # slow QGIS raster path when aether_converter is missing, so without
        # this the user waits out the whole extract and only then hits the
        # error. Blocking, not advisory — the run cannot succeed.
        engine_warning = setup_warning()
        if engine_warning:
            show_run_error(self, setup_title(engine_warning), engine_warning)
            return

        # Refresh asset cache before building jobs so latest data is used.
        self._refresh_asset_cache()

        jobs = self._build_jobs()
        if not jobs:
            QMessageBox.warning(
                self, "No Jobs",
                "No valid site/altitude combinations could be built. "
                "Ensure all sites have a location set.",
            )
            return

        # ---- Reject antennas below the AGL floor ----
        # Not a "run anyway?" prompt: the job builder raises on these, so
        # offering to continue would only move the failure into the worker
        # thread. AGL only — an AMSL height is an absolute elevation and may
        # legitimately be zero or negative.
        height_errors = [
            f"{name}: {msg}"
            for job_params, name in jobs
            for msg in [antenna_height_error(job_params)]
            if msg is not None
        ]
        if height_errors:
            QMessageBox.critical(
                self, f"Antenna below the {MIN_ANTENNA_AGL_M:.1f} m minimum",
                "\n\n".join(dict.fromkeys(height_errors)),
            )
            return

        # ---- Confirm parameters that leave the model's validated range ----
        # Neither AETHER nor Splat surfaces ITM's own kwx indicator in map mode,
        # so an out-of-range run (0 m receivers, sub-kilometre ranges, high
        # alpine refractivity) otherwise looks exactly like a valid one.
        model_issues = [w for job_params, _name in jobs for w in model_warnings(job_params)]
        if any(w.severity == INVALID for w in model_issues):
            if QMessageBox.warning(
                self, "Outside the propagation model's range",
                "These parameters are outside the range the propagation model is "
                "defined for. The run will complete, but the numbers it produces "
                "are probably invalid.\n\n"
                + format_model_warnings(model_issues)
                + "\n\nRun anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return

        # ---- Disk for the coverage rasters (before any terrain is fetched) ----
        # The raster is written at the chosen resolution over the whole range,
        # also for Adaptive / Custom: 2 m to 500 km is ~370 GB for LOS Floor,
        # which used to fail with "not enough space" deep into the engine run.
        problem, blocking = output_space_problem(
            [(name, params.model, params.max_range_km, params.resolution_m,
              params.az_start, params.az_end) for params, name in jobs],
            output_dir,
        )
        # A large result that fits is a question; one that does not fit is
        # refused below, together with the terrain pool on its drive
        # (_disk_refusal, 2026-10-21 — it used to be "Run anyway?").
        if problem and not blocking and QMessageBox.warning(
            self, "Very large result", problem,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return

        # ---- Confirm an unreasonably large terrain download ----
        # Jobs that only differ by receiver altitude share one terrain cache,
        # so dedupe on the parameters the cache key is built from — otherwise
        # the estimate would multiply by the number of altitudes.
        seen: set = set()
        unique: List[CoverageParams] = []
        union: Optional[dict] = None
        # Price against the tile pool, not the whole set: terrain that is
        # already on disk must not be announced as a download. A file source
        # builds only the region of a tile each site needs (partial pool
        # tiles), for every job of the run at once — the estimate prices the
        # same regions (run_region_union, as the worker passes it).
        plan_source = terrain_dir or (dem_layer.source() if dem_layer else "")
        plan_buildings = buildings_identity(
            osm_buildings=self.chk_buildings.isChecked())
        region_union = run_region_union([p for p, _ in jobs])
        for params, _name in jobs:
            key = (params.tx_lat, params.tx_lon, params.max_range_km,
                   params.resolution_m, params.az_start, params.az_end,
                   tuple(params.range_profile or ()),
                   tuple(params.range_profile_buildings or ()))
            if key in seen:
                continue
            seen.add(key)
            unique.append(params)
            bb = analysis_bbox(params.tx_lat, params.tx_lon,
                               params.max_range_km, params.az_start,
                               params.az_end)
            if union is None:
                union = dict(bb)
            else:
                union["north"] = max(union["north"], bb["north"])
                union["south"] = min(union["south"], bb["south"])
                union["east"] = max(union["east"], bb["east"])
                union["west"] = min(union["west"], bb["west"])

        # A pre-built .abt folder is asked about ONCE on the GUI thread (a
        # memoised listing); its headers, the tile selection and the checks
        # run in the coverage check's worker thread below — the selection is
        # memoised, so the worker's terrain step reuses it.
        prebuilt = bool(terrain_dir) and is_abt_tile_dir(terrain_dir)
        if prebuilt:
            # Used as it is — nothing to build, nothing to price.
            abt_index.expire(terrain_dir)   # this run: one fresh look
            totals = dict(_PREBUILT_TOTALS)
        else:
            totals = _plan_jobs(unique, plan_source, plan_buildings,
                                region_union)

        # A local terrain directory that does not reach across the analysis
        # area is a mistake worth stopping for: it is the only source once
        # selected, so whatever it misses has no terrain (a raster folder:
        # built as 0 m sea level; a pre-built folder: NODATA to the engine).
        abt_subs: List[str] = []
        abt_buildings = None
        # The run's ONE preparation of a raster folder (index check, source
        # infos, FootprintIndex) — made by the coverage check, handed to
        # the worker.
        terrain_prep: Optional[TerrainPrep] = None
        if terrain_dir and union is not None:
            # A raster folder is read through its footprint index, in a
            # worker thread (it used to be opened file by file on the GUI
            # thread: 84 s frozen for 2,500 files). Files that cannot be
            # used are listed in ONE prompt — skip them, or cancel (default).
            raster_folder = not prebuilt
            if raster_folder:
                try:
                    scan = terrain_folder_scan.scan_folder(self, terrain_dir)
                except RuntimeError as exc:
                    QMessageBox.critical(self, "Terrain folder could not be "
                                         "read", str(exc))
                    return
                if scan is None:
                    return
                answer = terrain_folder_scan.resolve_folder_problems(
                    self, terrain_dir, scan["index"],
                    decisions=terrain_index.approved_decisions(
                        terrain_dir, scan["index"]))
                if answer is None:
                    return
                terrain_index.approve(terrain_dir, answer[1], scan["index"])
            # Resolution and buildings matter for a pre-built .abt folder:
            # neither can be changed by the run, so a mismatch has to be
            # raised here or it becomes a silently wrong result. Off the GUI
            # thread too: per-file footprints over the whole folder.
            res_m = jobs[0][0].resolution_m if jobs else None
            try:
                # The same worker thread prepares a raster folder ONCE for
                # the run — source infos in overlap order + one
                # FootprintIndex over the index check just made, priced and
                # coverage-checked from it, then handed to the worker
                # (TerrainPrep: no second index check, source-info build or
                # FootprintIndex there) — or chooses every job's pre-built
                # tiles and reads what their buildings records say (a
                # pre-built folder); one modal progress dialog, the GUI
                # stays live.
                checked = terrain_folder_scan.scan_folder(
                    self, terrain_dir, refresh=False,
                    title="Checking terrain coverage",
                    after=lambda _idx: _terrain_dir_checks(
                        terrain_dir, union, res_m, unique, prebuilt,
                        plan_source, plan_buildings, region_union))
            except RuntimeError:
                checked = {"extra": None}
            if checked is None:
                return
            cov, priced, abt_subs, abt_buildings, terrain_prep = (
                checked["extra"] or (None, None, [], None, None))
            if priced is not None:
                totals = priced
            if cov and QMessageBox.warning(
                self, "Incomplete terrain coverage", cov + "\n\nRun anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return

        # Pre-built .abt folder: the plugin chooses the tile per footprint for
        # the step it needs (the engine never picks). Where the folder has
        # only a COARSER resolution than the ground needs, say so before
        # running — no silent substitution (finer ones are only logged).
        if prebuilt:
            subs = list(abt_subs or [])
            if subs:
                shown = "\n".join(f"  • {x}" for x in subs[:12])
                more = f"\n  … and {len(subs) - 12} more" if len(subs) > 12 else ""
                if QMessageBox.warning(
                    self, "Terrain resolution not available",
                    "The pre-built terrain folder does not have the resolution "
                    "these areas need:\n\n" + shown + more +
                    "\n\nRun anyway with the closest resolution the folder has "
                    "there? (Otherwise build the missing tiles in the Map "
                    "Converter, or change the resolution steps.)",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                ) != QMessageBox.StandardButton.Yes:
                    return
            # Buildings are baked into prepared tiles: say what the chosen
            # tiles carry before the run (the box cannot change it).
            if not self._confirm_prepared_buildings(terrain_dir,
                                                    abt_buildings):
                return

        total_mb = totals["download_mb"]
        cached_mb = totals["cached_mb"]
        raw_mb = totals.get("raw_mb", 0)
        buildings_mb = totals.get("buildings_mb", 0)
        if prebuilt:
            QgsMessageLog.logMessage(
                "terrain plan: pre-built folder — nothing to build",
                TAG, Qgis.MessageLevel.Info)
        else:
            _log_terrain_plan(totals["tiles_cached"], totals["tiles_missing"],
                              total_mb, cached_mb, totals["tiles_partial"],
                              totals["build_deg2"], totals["tile_deg2"],
                              totals["source_files"], totals["source_mb"],
                              raw_mb=raw_mb, buildings_mb=buildings_mb)

        # ---- Disk: coverage rasters + pool tiles still to build + the raw
        # source tiles an XYZ download caches, per drive (2 GB kept free) —
        # refused instead of failing with ENOSPC hours into the run. A
        # pre-built .abt folder adds nothing to the pool.
        refusal = _disk_refusal(
            jobs, 0 if prebuilt else total_mb, output_dir,
            raw_mb=0 if prebuilt else raw_mb,
            buildings_mb=0 if prebuilt else buildings_mb)
        if refusal:
            QgsMessageLog.logMessage(refusal, TAG, Qgis.MessageLevel.Warning)
            QMessageBox.critical(self, "Not enough disk space", refusal)
            return
        warn = None if prebuilt else terrain_size_warning(total_mb, cached_mb)
        if warn is not None:
            msg, strong = warn
            default = (
                QMessageBox.StandardButton.No if strong
                else QMessageBox.StandardButton.Yes
            )
            if QMessageBox.warning(
                self, "Large terrain download", msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, default,
            ) != QMessageBox.StandardButton.Yes:
                return

        # ---- Launch worker ----
        self._launch_worker(jobs, dem_layer, output_dir,
                            terrain_prep=terrain_prep)

    def _confirm_prepared_buildings(self, terrain_dir: str,
                                    state: Any) -> bool:
        """The buildings of a pre-built folder's chosen tiles (*state*: a
        ``terrain_adapter.PreparedBuildings`` from the Map Converter's
        record, or None) against "Include buildings" — a prepared tile keeps
        the buildings it was built with, the box cannot add or remove them.
        Ticked: all → information ("nothing is added", run); none / mixed →
        a warning, Yes runs anyway; unknown → one log line. Unticked and
        some tiles carry buildings → information, once per folder per
        session. False = the user said No."""
        if state is None:
            return True
        yes, no = (QMessageBox.StandardButton.Yes,
                   QMessageBox.StandardButton.No)
        counts = state.counts()
        if self.chk_buildings.isChecked():
            if state.state == terrain_adapter.PREPARED_ALL:
                QMessageBox.information(
                    self, "Buildings already in the terrain",
                    "This prepared terrain already contains buildings; "
                    f"nothing is added.\n\n{terrain_dir}\n({counts})")
                return True
            if state.state == terrain_adapter.PREPARED_NONE:
                return QMessageBox.warning(
                    self, "Prepared terrain without buildings",
                    f"The prepared terrain in:\n{terrain_dir}\n\nhas no "
                    f"buildings in the tiles this analysis uses ({counts})."
                    "\n\n'Include buildings' cannot add them to a prepared "
                    "folder — build it with buildings in the Map Converter "
                    "(Download Buildings…).\n\nRun anyway, without "
                    "buildings?", yes | no, no) == yes
            if state.state == terrain_adapter.PREPARED_MIXED:
                return QMessageBox.warning(
                    self, "Prepared terrain partly without buildings",
                    f"Of the tiles this analysis uses from:\n{terrain_dir}"
                    f"\n\n{counts}.\n\n'Include buildings' cannot add "
                    "buildings to a prepared folder — rebuild it with "
                    "buildings in the Map Converter.\n\nRun anyway?",
                    yes | no, no) == yes
            QgsMessageLog.logMessage(
                f"buildings: whether the prepared terrain in {terrain_dir} "
                f"carries buildings is not recorded ({counts}; no Map "
                f"Converter record) — 'Include buildings' adds nothing to a "
                f"prepared folder", TAG, Qgis.MessageLevel.Warning)
            return True
        key = os.path.normcase(os.path.abspath(terrain_dir))
        if state.with_buildings and key not in _BUILDINGS_NOTICE_FOLDERS:
            _BUILDINGS_NOTICE_FOLDERS.add(key)
            QMessageBox.information(
                self, "Terrain carries buildings",
                f"The prepared terrain in:\n{terrain_dir}\n\ncarries "
                f"buildings ({counts}) although 'Include buildings' is "
                "unticked — they are part of its tiles and stay in the "
                "result.")
        return True

    def _on_buildings_toggled(self, _on: bool) -> None:
        """"Include buildings" changed: the band profile follows; on a
        pre-built ``.abt`` folder a hint says the box cannot change its
        tiles (the status tip keeps saying it)."""
        self._refresh_res_profile()
        terrain_dir = self._selected_terrain_dir()
        try:
            prepared = bool(terrain_dir) and is_abt_tile_dir(terrain_dir)
        except Exception:  # noqa: BLE001 — a hint never fails the toggle
            prepared = False
        tip = (_PREPARED_BUILDINGS_HINT if prepared else "")
        self.chk_buildings.setStatusTip(tip)
        if prepared:
            with contextlib.suppress(Exception):  # no bar (tests, headless)
                self.iface.messageBar().pushMessage(
                    "Waveshed", tip, Qgis.MessageLevel.Info, 8)

    def _launch_worker(
        self, jobs: List[Tuple[CoverageParams, str]],
        dem_layer: QgsRasterLayer, output_dir: str,
        terrain_prep: Optional[TerrainPrep] = None,
    ) -> None:
        """Put the tab in run mode and start the worker for *jobs*
        (*terrain_prep*: the run's preparation of the terrain folder, see
        ``_terrain_dir_checks``)."""
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_stop.setVisible(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.status_label.setVisible(True)
        self.status_label.setText("Starting...")

        terrain_dir = self._selected_terrain_dir() or ""
        self._worker = worker = _SiteAnalysisWorker(
            jobs, dem_layer, output_dir, terrain_dir, self,
            osm_buildings=self.chk_buildings.isChecked(),
            terrain_prep=terrain_prep,
        )
        worker.progress.connect(self.progress_bar.setValue)
        worker.status.connect(self._on_status)
        worker.finished_ok.connect(self._on_finished_ok)
        worker.finished_err.connect(self._on_finished_err)
        # However the run ends (a stopped one emits neither signal above),
        # the UI comes back only once the thread has really ended.
        worker.finished.connect(lambda: self._on_worker_finished(worker))
        worker.start()

    def _on_stop(self) -> None:
        """Stop a running analysis — without blocking the GUI thread.

        cancel() kills the engine process the worker waits on (terrain
        download, ingest or aether_core); the run UI is reset only when the
        thread has ended (``_on_worker_finished``). Resetting it here, while
        the worker was still unwinding, re-enabled Run and let a second run
        overlap the first on the same pool, view dir and temp names.
        """
        if self._worker and self._worker.isRunning():
            self.btn_stop.setEnabled(False)
            self.status_label.setText("Stopping...")
            self._worker.cancel()
        else:
            self._reset_run_ui()

    def _on_worker_finished(self, worker: _SiteAnalysisWorker) -> None:
        """QThread.finished of *worker*: done, failed or stopped — it is over.

        A late signal of an older worker must not reset a newer run's UI.
        """
        if worker is self._worker:
            self._reset_run_ui()

    def _reset_run_ui(self) -> None:
        # Idempotent: finished_ok / finished_err reset first, then the
        # thread's finished signal resets again.
        self.btn_run.setEnabled(True)
        self.btn_stop.setVisible(False)
        self.progress_bar.setVisible(False)
        self.status_label.setVisible(False)

    def _on_status(self, msg: str) -> None:
        self.status_label.setText(msg)
        QgsMessageLog.logMessage(msg, TAG, Qgis.MessageLevel.Info)

    def _on_finished_ok(self, results: list) -> None:
        """Handle successful completion of all jobs.

        *results* is a list of ``(tif_path, model, display_name)`` tuples.
        """
        self.progress_bar.setValue(100)
        self.status_label.setText("Complete.")
        self._reset_run_ui()

        loaded_count = 0
        # Group each run under its own timestamped subgroup so repeated
        # analyses don't pile up identically-named layers at one tree level.
        run_group = GROUP_COVERAGE + (f"Run {datetime.now():%Y-%m-%d %H:%M:%S}",)
        for tif_path, model, display_name in results:
            try:
                layer = load_coverage_result(tif_path, model, display_name)
                add_layer_to_project(layer, run_group)
                loaded_count += 1
            except Exception as exc:
                QgsMessageLog.logMessage(
                    f"Failed to load result '{display_name}': {exc}",
                    TAG,
                    Qgis.MessageLevel.Warning,
                )

        has_min_alt = any(
            str(model).upper() == "MIN_ALT" for _, model, _ in results
        )
        if has_min_alt:
            reply = QMessageBox.question(
                self, "Analysis Complete",
                f"Completed {len(results)} LOS Floor job(s).\n"
                f"{loaded_count} result(s) loaded into QGIS.\n\n"
                "Open the Altitude Explorer to pick a preferred altitude and "
                "see the reachable area live?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                try:
                    from .altitude_explorer import show_altitude_explorer
                    show_altitude_explorer(self.iface)
                except Exception as exc:
                    QgsMessageLog.logMessage(
                        f"Could not open Altitude Explorer: {exc}",
                        TAG, Qgis.MessageLevel.Warning,
                    )
        else:
            QMessageBox.information(
                self, "Analysis Complete",
                f"Completed {len(results)} analysis job(s).\n"
                f"{loaded_count} result(s) loaded into QGIS.",
            )

    def _on_finished_err(self, message: str) -> None:
        self._reset_run_ui()
        show_run_error(self, "Analysis Failed", message)

    # ==================================================================
    # Cancel (called by parent dialog if needed)
    # ==================================================================

    def cancel_worker(self) -> None:
        """Cancel a running worker and clean up map visuals.

        The dialog is closing, so this one waits (bounded) for the run to
        end. Run is still re-enabled only by ``_on_worker_finished`` — never
        while the thread is alive, also when the wait times out.
        """
        if self._worker and self._worker.isRunning():
            self._on_stop()
            self._worker.wait(5000)
        # Remove site markers from the canvas.
        self._sites_rb.reset()
