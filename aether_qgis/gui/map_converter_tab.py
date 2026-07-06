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

from qgis.PyQt.QtCore import QThread, pyqtSignal, Qt
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
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
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

from ..core.binary_manager import find_binary
from ..core.layer_utils import classify_raster_layer

TAG = "AETHER"

_SUBPROCESS_FLAGS = (
    subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
)

# .abt tile geographic extent per resolution.  Controls how large each
# .abt file is on disk (pixels = extent_deg / (res_m / 111111)).
# aether_core accepts any size; these keep files under ~60 MB.
_ABT_EXTENT_DEG = {
    2: 0.1,
    5: 0.25,
    10: 0.25,
    30: 0.5,
    90: 1.0,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _abt_size_px(res_m: int, extent_deg: float) -> int:
    """Compute .abt tile pixel width for a given resolution and extent."""
    target_deg = res_m / 111_111.0
    sz = int(round(extent_deg / target_deg))
    return ((sz + 3) // 4) * 4  # BC6H block alignment


def _estimate_tile_count_and_mb(
    bbox: Dict[str, float], resolutions: List[int],
) -> Tuple[int, int]:
    """Return (tile_count, total_mb) for the given bbox and resolutions."""
    total_tiles = 0
    total_bytes = 0
    for res in resolutions:
        ext = _ABT_EXTENT_DEG.get(res, 1.0)
        lat = math.floor(bbox["south"] / ext) * ext
        while lat < bbox["north"]:
            lon = math.floor(bbox["west"] / ext) * ext
            while lon < bbox["east"]:
                sz = _abt_size_px(res, ext)
                bpr = sz * 2
                stride = (bpr + 255) & ~255
                total_bytes += 44 + stride * sz
                total_tiles += 1
                lon = round(lon + ext, 6)
            lat = round(lat + ext, 6)
    return total_tiles, total_bytes // (1024 * 1024)


def _list_terrain_files(folder: str) -> List[str]:
    """Return sorted absolute paths of terrain files in *folder*."""
    exts = (".tif", ".tiff", ".dem", ".hgt")
    return sorted(
        os.path.join(folder, f) for f in os.listdir(folder)
        if f.lower().endswith(exts)
    )


def _detect_resolution(layer) -> Optional[float]:
    """Auto-detect a raster layer's ground resolution in metres."""
    if not isinstance(layer, QgsRasterLayer):
        return None
    provider = layer.dataProvider()
    if provider is None or provider.xSize() <= 0:
        return None
    try:
        ext = provider.extent()
        crs = layer.crs()
        if crs.isGeographic():
            clat = (ext.yMinimum() + ext.yMaximum()) / 2.0
            cos_lat = max(math.cos(math.radians(clat)), 0.01)
            x_m = ext.width() / provider.xSize() * 111_111 * cos_lat
            y_m = ext.height() / provider.ySize() * 111_111
            return round(min(x_m, y_m), 1)
        else:
            return round(min(ext.width() / provider.xSize(),
                             ext.height() / provider.ySize()), 1)
    except Exception:
        return None


def _detect_xyz_resolution(source: str) -> Optional[float]:
    """Detect resolution of an XYZ tile layer from its zmax parameter."""
    try:
        import urllib.parse
        params = dict(urllib.parse.parse_qsl(source))
        z_max = int(params.get("zmax", "15"))
        # At equator: res = 40075000 / (2^z * 256)
        return round(40_075_000 / (2 ** z_max * 256), 1)
    except Exception:
        return None


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


def _detect_folder_crs(folder: str) -> Optional[str]:
    """Detect CRS from the first GeoTIFF in a folder."""
    try:
        from osgeo import gdal
        for fn in os.listdir(folder):
            if fn.lower().endswith((".tif", ".tiff")):
                ds = gdal.Open(os.path.join(folder, fn))
                if ds and ds.GetProjection():
                    crs = QgsCoordinateReferenceSystem(ds.GetProjection())
                    if crs.isValid():
                        return crs.authid()
                    ds = None
                break
    except Exception:
        pass
    return None


def _detect_folder_resolution(folder: str) -> Optional[float]:
    """Detect ground resolution from the first GeoTIFF in a folder."""
    try:
        from osgeo import gdal
        for fn in os.listdir(folder):
            if fn.lower().endswith((".tif", ".tiff")):
                ds = gdal.Open(os.path.join(folder, fn))
                if ds:
                    gt = ds.GetGeoTransform()
                    proj = ds.GetProjection()
                    crs = QgsCoordinateReferenceSystem(proj) if proj else None
                    res = min(abs(gt[1]), abs(gt[5]))
                    if crs and crs.isGeographic():
                        clat = gt[3] - (ds.RasterYSize / 2) * abs(gt[5])
                        res *= 111_111 * max(math.cos(math.radians(clat)), 0.01)
                    return round(res, 1)
                break
    except Exception:
        pass
    return None


def _filter_files_by_extent(
    files: List[str],
    extent: Dict[str, float],
    crs_authid: str,
) -> List[str]:
    """Keep only files whose geographic bounds overlap *extent* (WGS84).

    Reads only the GeoTransform from each file (fast, no pixel data).
    For LV95 files, converts bounds to WGS84 for comparison.
    """
    from osgeo import gdal
    result = []
    s, n, w, e = extent["south"], extent["north"], extent["west"], extent["east"]
    is_lv95 = crs_authid == "EPSG:2056"

    for path in files:
        try:
            ds = gdal.Open(path, gdal.GA_ReadOnly)
            if ds is None:
                continue
            gt = ds.GetGeoTransform()
            xsize, ysize = ds.RasterXSize, ds.RasterYSize
            ds = None

            f_west = gt[0]
            f_north = gt[3]
            f_east = gt[0] + gt[1] * xsize
            f_south = gt[3] + gt[5] * ysize
            if f_south > f_north:
                f_south, f_north = f_north, f_south

            if is_lv95:
                # Quick LV95 → WGS84 approximation (inverse of the fast formula).
                # Accurate enough for bounding-box overlap testing.
                f_south, f_west = _lv95_to_wgs84_approx(f_west, f_south)
                f_north, f_east = _lv95_to_wgs84_approx(f_east, f_north)

            # Overlap test.
            if f_east > w and f_west < e and f_north > s and f_south < n:
                result.append(path)
        except Exception:
            result.append(path)  # Keep on error (safe fallback).
    return result


def _lv95_to_wgs84_approx(easting: float, northing: float) -> Tuple[float, float]:
    """Approximate LV95 → WGS84 conversion (inverse of Swisstopo formula).

    Returns (latitude, longitude).  Accuracy ~1m in Switzerland.
    """
    y_aux = (easting - 2_600_000) / 1_000_000
    x_aux = (northing - 1_200_000) / 1_000_000
    lat = (16.9023892 + 3.238272 * x_aux
           - 0.270978 * y_aux * y_aux
           - 0.002528 * x_aux * x_aux
           - 0.0447 * y_aux * y_aux * x_aux
           - 0.0140 * x_aux * x_aux * x_aux) * 100 / 36
    lon = (2.6779094 + 4.728982 * y_aux
           + 0.791484 * y_aux * x_aux
           + 0.1306 * y_aux * x_aux * x_aux
           - 0.0436 * y_aux * y_aux * y_aux) * 100 / 36
    return lat, lon


def _convert_to_fgb(src_path: str, out_dir: str, src_crs: str = "") -> Optional[str]:
    """Convert a vector file (GDB/SHP/GPKG/GeoJSON) to FlatGeobuf via GDAL.

    Returns path to the output .fgb file, or None on failure.
    """
    try:
        from osgeo import gdal, ogr
        basename = os.path.splitext(os.path.basename(src_path))[0]
        out_path = os.path.join(out_dir, f"{basename}.fgb")
        if os.path.exists(out_path):
            return out_path

        options = [
            "-f", "FlatGeobuf",
            "-t_srs", "EPSG:4326",
            "-nlt", "PROMOTE_TO_MULTI",
            "-lco", "SPATIAL_INDEX=YES",
            "-skipfailures",
        ]
        result = gdal.VectorTranslate(out_path, src_path, options=options)
        if result is None:
            return None
        result = None  # Close dataset
        return out_path
    except Exception as exc:
        QgsMessageLog.logMessage(
            f"Vector conversion failed for {src_path}: {exc}",
            TAG, Qgis.MessageLevel.Warning,
        )
        return None


# ---------------------------------------------------------------------------
# Layer data model
# ---------------------------------------------------------------------------

class _LayerEntry:
    """One layer in the converter stack."""
    __slots__ = (
        "layer_type", "source_path", "qgis_layer", "crs_authid",
        "native_res_m", "target_resolutions", "extent", "priority",
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


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

def _resolve_source_on_main_thread(entry: _LayerEntry) -> str:
    """Extract a file/folder path from a layer entry (must be called on
    the main thread because QgsMapLayer objects are not thread-safe).

    For local files/folders, returns the path directly.
    For remote layers (WMS/XYZ), exports to a temp GeoTIFF.
    """
    if entry.qgis_layer is not None:
        src = entry.qgis_layer.source()
        if os.path.isfile(src) or src.startswith("/vsi"):
            return src
        if os.path.isdir(entry.source_path):
            return entry.source_path
        # Remote layer: the actual export happens in the worker thread
        # using the source path.  We can't export here (would block).
        # For remote layers, pass the source string and let the worker
        # handle export — but QGIS writeRaster needs a QgsRasterLayer
        # which is main-thread-only.  So export NOW on main thread.
        if isinstance(entry.qgis_layer, QgsRasterLayer) and entry.extent:
            try:
                from qgis.core import (
                    QgsRasterFileWriter, QgsRasterPipe,
                    QgsRasterProjector, QgsRectangle,
                )
                wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
                bbox = entry.extent
                extent = QgsRectangle(
                    bbox["west"], bbox["south"], bbox["east"], bbox["north"]
                )
                provider = entry.qgis_layer.dataProvider()
                if provider:
                    pipe = QgsRasterPipe()
                    if pipe.set(provider.clone()):
                        if entry.qgis_layer.crs() != wgs84:
                            proj = QgsRasterProjector()
                            proj.setCrs(
                                entry.qgis_layer.crs(), wgs84,
                                QgsProject.instance().transformContext(),
                            )
                            pipe.insert(pipe.size(), proj)
                        res = _detect_resolution(entry.qgis_layer) or 30.0
                        deg = res / 111_111.0
                        nc = max(1, int(round(extent.width() / deg)))
                        nr = max(1, int(round(extent.height() / deg)))
                        cap = 16384
                        if max(nc, nr) > cap:
                            ratio = max(nc, nr) / cap
                            nc, nr = int(nc / ratio), int(nr / ratio)
                        out = os.path.join(
                            tempfile.gettempdir(),
                            f"aether_exp_{id(entry)}_{os.getpid()}.tif",
                        )
                        w = QgsRasterFileWriter(out)
                        w.setOutputFormat("GTiff")
                        err = w.writeRaster(
                            pipe, nc, nr, extent, wgs84,
                            QgsProject.instance().transformContext(),
                        )
                        if err == 0:  # NoError
                            return out
            except Exception:
                pass
        return entry.source_path
    return entry.source_path


def _kill_proc(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:
        pass


class _MapConverterWorker(QThread):
    """Background worker — does ALL heavy I/O + runs the converter.

    The main thread only passes lightweight config; this worker handles:
    - Resolving layer paths (file discovery, CRS detection)
    - GDAL reprojection of non-WGS84 layers
    - Exporting remote WMS/XYZ layers to temp GeoTIFF
    - Downloading OSM buildings via Overpass API
    - Converting GDB/SHP/GPKG → FGB
    - Building the ingest job JSON
    - Running the converter subprocess
    """

    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    log_line = pyqtSignal(str)
    finished_ok = pyqtSignal(str)
    finished_err = pyqtSignal(str)

    def __init__(self, layers, output_dir, out_res, parent=None):
        super().__init__(parent)
        # Deep-copy the layer config so the main thread can't mutate it.
        self._layers = [
            {
                "layer_type": e.layer_type,
                "source_path": e.source_path,
                "crs_authid": e.crs_authid,
                "native_res_m": e.native_res_m,
                "extent": dict(e.extent) if e.extent else None,
                "priority": e.priority,
                # qgis_layer can't be passed to another thread — resolve
                # the source path on the main thread before constructing.
                "resolved_source": _resolve_source_on_main_thread(e),
            }
            for e in layers
        ]
        self._output_dir = output_dir
        self._out_res = out_res
        self._canceled = False
        self._proc = None

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

    def run(self):
        try:
            # ---- Phase 1: Resolve ALL layers in parallel ----
            self.status.emit("Preparing layers...")
            self._log("Phase 1: Resolving layers (parallel)...")
            self.progress.emit(2)

            raster_entries = [e for e in self._layers
                              if e["layer_type"] != "buildings"]
            building_entries = [e for e in self._layers
                                if e["layer_type"] == "buildings"]

            from concurrent.futures import ThreadPoolExecutor, as_completed

            all_raster_paths = []
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
                        self._log(f"  ERROR resolving raster layer {idx}: {exc}")
                        raster_results[idx] = []
                    done_tasks += 1
                    pct = int(done_tasks * 25 / max(total_tasks, 1))
                    self.progress.emit(pct)
                    self.status.emit(
                        f"Resolved layer {done_tasks}/{total_tasks}")

                # Collect building results.
                for fut in as_completed(building_futures):
                    if self._canceled:
                        return
                    try:
                        bp = fut.result()
                        if bp:
                            buildings_path = bp
                    except Exception as exc:
                        self._log(f"  ERROR resolving buildings: {exc}")
                    done_tasks += 1
                    pct = int(done_tasks * 25 / max(total_tasks, 1))
                    self.progress.emit(pct)

            # Reassemble raster paths in priority order.
            for i in sorted(raster_results.keys()):
                all_raster_paths.extend(raster_results[i])

            if not all_raster_paths:
                self.finished_err.emit("No raster layers resolved to files.")
                return

            base = all_raster_paths[-1]
            overlays = all_raster_paths[:-1]
            self._log(f"  Base: {os.path.basename(base)}")
            self._log(f"  Overlays: {len(overlays)} file(s)")
            if buildings_path:
                self._log(f"  Buildings: {buildings_path}")

            # ---- Phase 2: Build tile job list ----
            if self._canceled:
                return
            self.progress.emit(30)
            self.status.emit("Building tile list...")
            self._log("Phase 2: Building .abt tile jobs...")

            combined = None
            for entry in self._layers:
                if entry["layer_type"] == "buildings" or not entry["extent"]:
                    continue
                if combined is None:
                    combined = dict(entry["extent"])
                else:
                    combined["south"] = min(combined["south"], entry["extent"]["south"])
                    combined["north"] = max(combined["north"], entry["extent"]["north"])
                    combined["west"] = min(combined["west"], entry["extent"]["west"])
                    combined["east"] = max(combined["east"], entry["extent"]["east"])

            if combined is None:
                self.finished_err.emit("No layer has an extent set.")
                return

            # Snap to grid.
            finest = min(_ABT_EXTENT_DEG.get(r, 1.0) for r in self._out_res)
            bbox = {
                "south": math.floor(combined["south"] / finest) * finest,
                "north": math.ceil(combined["north"] / finest) * finest,
                "west": math.floor(combined["west"] / finest) * finest,
                "east": math.ceil(combined["east"] / finest) * finest,
            }

            os.makedirs(self._output_dir, exist_ok=True)
            jobs = []
            for res in sorted(self._out_res):
                ext_deg = _ABT_EXTENT_DEG.get(res, 1.0)
                lat = math.floor(bbox["south"] / ext_deg) * ext_deg
                while lat < bbox["north"]:
                    lon = math.floor(bbox["west"] / ext_deg) * ext_deg
                    while lon < bbox["east"]:
                        ul_lat = round(lat + ext_deg, 6)
                        ul_lon = round(lon, 6)
                        sz = _abt_size_px(res, ext_deg)
                        exact_res = ext_deg / sz * 111_111.0
                        fn = f"Tile_N{ul_lat:.2f}E{ul_lon:.2f}_{res}m_r16sint.abt"
                        out = os.path.join(self._output_dir, fn)
                        if not os.path.exists(out):
                            job = {
                                "output_path": os.path.abspath(out),
                                "format": "r16sint",
                                "ul_lat": ul_lat, "ul_lon": ul_lon,
                                "resolution_m": float(exact_res),
                                "size_px": sz,
                                "base_tif": base,
                                "swiss_tifs": overlays,
                            }
                            if buildings_path:
                                job["buildings_file"] = buildings_path
                            jobs.append(job)
                        lon = round(lon + ext_deg, 6)
                    lat = round(lat + ext_deg, 6)

            if not jobs:
                self.status.emit("All tiles already exist.")
                self.finished_ok.emit(self._output_dir)
                return

            self._log(f"Generated {len(jobs)} tile jobs.")
            self.progress.emit(40)

            # ---- Phase 3: Run converter ----
            if self._canceled:
                return

            try:
                converter_exe = find_binary("aether_converter")
            except RuntimeError as exc:
                self.finished_err.emit(str(exc))
                return

            total = len(jobs)
            self.status.emit(f"Converting {total} .abt tiles...")
            self._log(f"Phase 3: Running converter ({total} tiles)...")

            job_file = os.path.join(
                tempfile.gettempdir(), f"aether_conv_{os.getpid()}.json"
            )
            with open(job_file, "w") as fh:
                json.dump(jobs, fh)

            self._log(f"Launching: {converter_exe} ingest")

            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"

            self._proc = subprocess.Popen(
                [converter_exe, "ingest", "--job-file", job_file],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                text=True, encoding="utf-8", errors="replace",
                creationflags=_SUBPROCESS_FLAGS,
                env=env,
            )

            for line in iter(self._proc.stdout.readline, ""):
                if self._canceled:
                    _kill_proc(self._proc)
                    self._log("Cancelled by user.")
                    return
                line = line.strip()
                if not line:
                    continue
                self._log(line)

                prog = self._parse_progress(line)
                if prog:
                    curr, tot = prog
                    # Map converter progress to 40-100% range.
                    pct = 40 + int(curr * 60 / max(tot, 1))
                    self.progress.emit(pct)
                    self.status.emit(f"Tile {curr}/{tot}")

            self._proc.wait()
            rc = self._proc.returncode
            self._proc = None

            try:
                os.remove(job_file)
            except OSError:
                pass

            if rc != 0:
                msg = f"Converter failed (exit code {rc}). See log."
                self._log(msg)
                self.finished_err.emit(msg)
                return

            self.progress.emit(100)
            self.status.emit("Done.")
            self._log(f"Conversion complete: {total} tiles.")
            self.finished_ok.emit(self._output_dir)

        except Exception as exc:
            self._log(f"ERROR: {exc}")
            self.finished_err.emit(f"Unexpected error: {exc}")

    # -- Heavy I/O methods (run on worker thread) -------------------------

    # CRS that the converter handles natively (no reprojection needed).
    _NATIVE_CRS = {"EPSG:4326", "EPSG:2056"}

    def _needs_reproject(self, crs: str) -> bool:
        """Return True if CRS is not natively handled by the converter."""
        return bool(crs) and crs not in self._NATIVE_CRS

    def _resolve_raster(self, entry: dict) -> List[str]:
        """Resolve a raster layer entry to file paths for the converter.

        - WGS84 (EPSG:4326) and LV95 (EPSG:2056) are passed as-is —
          the converter handles both natively.
        - Other CRS are reprojected to WGS84 via GDAL Warp.
        - Folders are filtered by extent to avoid loading irrelevant files.
        """
        source = entry["resolved_source"]
        crs = entry["crs_authid"]
        extent = entry.get("extent")
        is_folder = os.path.isdir(source)

        if is_folder:
            files = _list_terrain_files(source)
            if not files:
                raise RuntimeError(f"No terrain files in {source}")

            # Filter by extent — only keep files that overlap.
            if extent:
                before = len(files)
                files = _filter_files_by_extent(files, extent, crs)
                self._log(f"  Folder: {source} — {len(files)}/{before} "
                          f"files overlap extent")
            else:
                self._log(f"  Folder: {source} ({len(files)} files, no filter)")

            if self._needs_reproject(crs):
                self._log(f"  Reprojecting {len(files)} files to WGS84...")
                files = [self._ensure_wgs84(f, crs) for f in files]

            return [os.path.abspath(f) for f in files]

        if os.path.isfile(source) or source.startswith("/vsi"):
            self._log(f"  File: {os.path.basename(source)}")
            if self._needs_reproject(crs):
                source = self._ensure_wgs84(source, crs)
            return [os.path.abspath(source)]

        self._log(f"  Resolved: {source}")
        return [os.path.abspath(source)]

    def _resolve_buildings(self, entry: dict) -> Optional[str]:
        """Resolve a buildings entry to an FGB path."""
        path = entry["resolved_source"]

        # Deferred OSM download.
        if path.startswith("[OSM Download"):
            return self._download_osm_buildings(entry)

        # GDB → FGB conversion.
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
            self._log(f"  Converting GDB → FlatGeobuf: {path}")
            out_dir = os.path.join(tempfile.gettempdir(), "aether_fgb")
            os.makedirs(out_dir, exist_ok=True)
            fgb = _convert_to_fgb(path, out_dir)
            if fgb:
                return os.path.abspath(fgb)
            self._log("  GDB conversion failed.")

        # SHP/GPKG/GeoJSON → FGB.
        ext = os.path.splitext(path)[1].lower() if os.path.isfile(path) else ""
        if ext in (".shp", ".gpkg", ".geojson"):
            self._log(f"  Converting {ext} → FlatGeobuf...")
            out_dir = os.path.join(tempfile.gettempdir(), "aether_fgb")
            os.makedirs(out_dir, exist_ok=True)
            fgb = _convert_to_fgb(path, out_dir)
            if fgb:
                return os.path.abspath(fgb)

        return os.path.abspath(path)

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
                headers={"User-Agent": "AETHER-QGIS-Plugin/1.0"},
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                raw = resp.read()

            osm_data = json.loads(raw)
            geojson = MapConverterTab._overpass_to_geojson(osm_data)

            out_dir = os.path.join(tempfile.gettempdir(), "aether_fgb")
            os.makedirs(out_dir, exist_ok=True)
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

    def _ensure_wgs84(self, path: str, crs: str) -> str:
        if not crs or crs == "EPSG:4326":
            return path
        try:
            from osgeo import gdal
            out = os.path.join(
                tempfile.gettempdir(),
                f"aether_wgs84_{os.path.basename(path)}"
            )
            if os.path.exists(out):
                return out
            self._log(f"  Reprojecting {os.path.basename(path)} → WGS84")
            r = gdal.Warp(out, path, dstSRS="EPSG:4326", format="GTiff",
                          resampleAlg=gdal.GRA_Bilinear)
            if r:
                r.FlushCache()
                r = None
                return out
        except Exception as exc:
            self._log(f"  Reproject failed: {exc}")
        return path


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
        self._rb = QgsRubberBand(self._canvas, QgsWkbTypes.PolygonGeometry)
        self._rb.setColor(QColor(255, 120, 0, 100))
        self._rb.setWidth(2)
        self._rb.setLineStyle(Qt.DashLine)

        # We subclass QgsMapToolEmitPoint inline by monkey-patching
        # canvasMoveEvent and canvasReleaseEvent.
        self._tool = QgsMapToolEmitPoint(self._canvas)
        self._tool.setCursor(Qt.CrossCursor)
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
        if event.button() == Qt.RightButton:
            self._cancel()
            return
        if event.button() != Qt.LeftButton:
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
            # Keep the rubber band visible (don't reset it).
            self._draw_rect(self._first_point_canvas, point)
            self._restore()
            self._callback(s, n, w, e)

    def _on_move(self, event):
        """Update rubber band as mouse moves after first click."""
        if self._first_point_canvas is None:
            return
        current = self._tool.toMapCoordinates(event.pos())
        self._draw_rect(self._first_point_canvas, current)

    def _on_key(self, event):
        if event.key() == Qt.Key_Escape:
            self._cancel()

    # -- Rubber band -------------------------------------------------------

    def _draw_rect(self, p1, p2):
        """Draw a rectangle from two opposite corners (canvas CRS)."""
        self._rb.reset(QgsWkbTypes.PolygonGeometry)
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


class MapConverterTab(QWidget):
    """Map Converter tab — fuse layers into .abt terrain tiles."""

    def __init__(self, parent_dialog, parent=None):
        super().__init__(parent)
        self._dialog = parent_dialog
        self.iface = parent_dialog.iface
        self._worker = None
        self._layers: List[_LayerEntry] = []
        self._rect_tool = None
        self._drawing_for_layer: Optional[int] = None

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
        try:
            # QGIS 3.42+: use Qgis.LayerFilter flags.
            self._combo_add.setFilters(
                Qgis.LayerFilter.RasterLayer | Qgis.LayerFilter.VectorLayer
            )
        except (AttributeError, TypeError):
            # Older QGIS: use QgsMapLayerProxyModel flags.
            self._combo_add.setFilters(
                QgsMapLayerProxyModel.RasterLayer | QgsMapLayerProxyModel.VectorLayer
            )
        self._combo_add.setAllowEmptyLayer(True)
        self._combo_add.setShowCrs(True)
        self._combo_add.setMinimumWidth(200)
        add_row.addWidget(self._combo_add, 3)

        self._btn_add_layer = QPushButton("Add Layer")
        self._btn_add_layer.setToolTip("Add QGIS layer (DEM or buildings).")
        add_row.addWidget(self._btn_add_layer)

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
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.horizontalHeader().setSectionResizeMode(
            _COL_SOURCE, QHeaderView.Stretch
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
            "Draw a rectangle on the map to set this layer's extent."
        )
        self._btn_layer_extent = QPushButton("Extent from Layer")
        self._btn_layer_extent.setToolTip(
            "Set extent from the selected layer's own geographic bounds."
        )
        btn_row.addWidget(self._btn_up)
        btn_row.addWidget(self._btn_down)
        btn_row.addWidget(self._btn_draw_extent)
        btn_row.addWidget(self._btn_layer_extent)
        btn_row.addStretch()
        btn_row.addWidget(self._btn_remove)
        gl.addLayout(btn_row)

        root.addWidget(grp)

        # ---- Output Resolutions ----
        res_grp = QGroupBox("Output .abt Resolutions")
        res_lay = QHBoxLayout(res_grp)
        res_lay.addWidget(QLabel("Generate tiles at:"))
        self._res_checks: Dict[int, QCheckBox] = {}
        for res in [2, 5, 10, 30, 90]:
            ext = _ABT_EXTENT_DEG.get(res, 1.0)
            sz = _abt_size_px(res, ext)
            cb = QCheckBox(f"{res}m")
            cb.setToolTip(
                f"{res}m resolution, {ext}\u00b0 geographic extent per tile, "
                f"{sz}\u00d7{sz} px"
            )
            if res == 30:
                cb.setChecked(True)
            cb.stateChanged.connect(self._update_estimate)
            self._res_checks[res] = cb
            res_lay.addWidget(cb)
        res_lay.addStretch()
        root.addWidget(res_grp)

        # ---- Output ----
        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("Output:"))
        self._edit_output = QLineEdit()
        self._edit_output.setPlaceholderText("~/.aether/cache")
        self._edit_output.setToolTip("Directory for .abt output files.")
        out_row.addWidget(self._edit_output, 1)
        self._btn_browse = QPushButton("Browse...")
        out_row.addWidget(self._btn_browse)
        root.addLayout(out_row)

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

    # ------------------------------------------------------------------
    # Add layers
    # ------------------------------------------------------------------

    def _on_add_layer(self):
        layer = self._combo_add.currentLayer()
        if layer is None:
            QMessageBox.warning(self, "No Layer", "Select a layer first.")
            return

        ltype = _detect_layer_type(layer)
        if ltype == "imagery":
            r = QMessageBox.warning(
                self, "Possible Imagery",
                f"'{layer.name()}' looks like imagery (RGB), not a DEM.\n"
                f"Elevation data needs single-band Float32/Int16.\n\nAdd anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if r != QMessageBox.Yes:
                return

        is_buildings = isinstance(layer, QgsVectorLayer)

        # Detect resolution.
        native_res = None
        if not is_buildings:
            source = layer.source()
            if "type=xyz" in source:
                native_res = _detect_xyz_resolution(source)
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
        self._layers.append(entry)
        self._rebuild_table()
        self._update_estimate()
        self._log_msg(f"Added layer: {layer.name()} "
                      f"(native ~{native_res or '?'}m)")

    def _on_add_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Select DEM/GeoTIFF Folder", "",
        )
        if not folder:
            return
        tifs = _list_terrain_files(folder)
        if not tifs:
            QMessageBox.warning(self, "No Terrain Files",
                                f"No GeoTIFF/DEM/HGT files in:\n{folder}")
            return

        crs_id = _detect_folder_crs(folder)
        if not crs_id:
            crs_id = self._ask_crs(f"CRS unknown for files in:\n{folder}")
            if not crs_id:
                return

        native_res = _detect_folder_resolution(folder)

        self._layers.append(_LayerEntry(
            layer_type="raster", source_path=folder,
            crs_authid=crs_id, native_res_m=native_res,
            target_resolutions=[30],
            priority=len(self._layers) + 1,
        ))
        self._rebuild_table()
        self._update_estimate()
        self._log_msg(f"Added folder: {folder} ({len(tifs)} files, "
                      f"~{native_res or '?'}m)")

    def _on_add_buildings(self):
        # Show a dialog with two clearly labeled buttons.
        msg = QMessageBox(self)
        msg.setWindowTitle("Add Buildings")
        msg.setText("How do you want to add building footprints?")
        msg.setInformativeText(
            "File: single vector file (FGB, SHP, GPKG, GeoJSON)\n"
            "Folder: directory of .fgb parts, or an ESRI .gdb"
        )
        btn_file = msg.addButton("Select File...", QMessageBox.AcceptRole)
        btn_folder = msg.addButton("Select Folder...", QMessageBox.AcceptRole)
        msg.addButton("Cancel", QMessageBox.RejectRole)
        msg.exec_()

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
        common = ["EPSG:4326", "EPSG:2056", "EPSG:32632", "EPSG:32633",
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

    def _on_remove(self):
        row = self._table.currentRow()
        if row < 0:
            return
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
        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
        if tooltip:
            item.setToolTip(tooltip)
        self._table.setItem(row, col, item)

    # ------------------------------------------------------------------
    # Per-layer extent
    # ------------------------------------------------------------------

    def _on_draw_extent(self):
        row = self._table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Select Layer",
                                    "Select a layer row first.")
            return
        self._drawing_for_layer = row
        parent = self._dialog
        if hasattr(parent, "showMinimized"):
            parent.showMinimized()
        self._rect_tool = _RectangleDrawTool(self.iface, self._on_extent_drawn)

    def _on_extent_drawn(self, s, n, w, e):
        row = self._drawing_for_layer
        if row is not None and 0 <= row < len(self._layers):
            self._layers[row].extent = {
                "south": s, "north": n, "west": w, "east": e
            }
            self._rebuild_table()
            self._update_estimate()
            self._log_msg(
                f"Extent set for layer {row + 1}: "
                f"S={s:.4f} N={n:.4f} W={w:.4f} E={e:.4f}"
            )
        self._drawing_for_layer = None
        parent = self._dialog
        if hasattr(parent, "showNormal"):
            parent.showNormal()
            parent.raise_()
            parent.activateWindow()

    def _choose_project_layer_for_extent(self, preferred=None):
        """Prompt for a loaded project layer to source an extent from.

        *preferred*, when given, is preselected in the list (e.g. the row's
        own layer) — but the user can still pick any other loaded layer.
        Returns the chosen QgsRasterLayer/QgsVectorLayer, or None if cancelled
        or no suitable layer is loaded.
        """
        from qgis.PyQt.QtWidgets import QInputDialog

        layers = [
            lyr for lyr in QgsProject.instance().mapLayers().values()
            if isinstance(lyr, (QgsRasterLayer, QgsVectorLayer))
        ]
        if not layers:
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
        name, ok = QInputDialog.getItem(
            self, "Extent from Layer",
            "Use the geographic extent of which layer?",
            names, preselect, False,
        )
        if not ok or not name:
            return None
        for lyr in layers:
            if lyr.name() == name:
                return lyr
        return None

    def _on_set_layer_extent(self):
        row = self._table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Select Layer",
                                    "Select a layer row first.")
            return
        entry = self._layers[row]
        # Always let the user choose which layer's extent to use, preselecting
        # this row's own layer when it has one. This makes the button useful
        # for every row type — a building/folder row can borrow the DEM's
        # extent, and any row can be re-bounded to a different layer.
        layer = self._choose_project_layer_for_extent(preferred=entry.qgis_layer)
        if layer is None:
            return

        ext = layer.extent()
        crs = layer.crs()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        if crs.isValid() and crs != wgs84:
            xform = QgsCoordinateTransform(crs, wgs84, QgsProject.instance())
            ext = xform.transformBoundingBox(ext)

        entry.extent = {
            "south": ext.yMinimum(), "north": ext.yMaximum(),
            "west": ext.xMinimum(), "east": ext.xMaximum(),
        }
        self._rebuild_table()
        self._update_estimate()
        self._log_msg(f"Extent set from layer: {layer.name()}")

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
        current = ",".join(str(r) for r in sorted(entry.target_resolutions))
        text, ok = QInputDialog.getText(
            self, "Target Resolutions",
            f"Enter comma-separated resolutions in metres.\n"
            f"Available: 2, 5, 10, 30, 90\n"
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

        valid = {2, 5, 10, 30, 90}
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
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if r != QMessageBox.Yes:
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

        total_tiles, total_mb = _estimate_tile_count_and_mb(combined, out_res)

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
            self._edit_output.text() or os.path.expanduser("~/.aether/cache"),
        )
        if folder:
            self._edit_output.setText(folder)

    def _get_output_dir(self) -> str:
        text = self._edit_output.text().strip()
        return os.path.expanduser(text) if text else os.path.expanduser(
            "~/.aether/cache"
        )

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
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if r != QMessageBox.Yes:
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
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                    )
                    if r != QMessageBox.Yes:
                        return

        output_dir = self._get_output_dir()

        self._log.clear()
        self._log_msg("Starting conversion...")

        self._set_running(True)
        self._worker = _MapConverterWorker(
            self._layers, output_dir, out_res, self
        )
        self._worker.progress.connect(self._progress.setValue)
        self._worker.status.connect(self._lbl_status.setText)
        self._worker.log_line.connect(self._log_msg)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.finished_err.connect(self._on_error)
        self._worker.start()

    # ------------------------------------------------------------------
    # Worker callbacks
    # ------------------------------------------------------------------

    def _on_done(self, path):
        self._set_running(False)
        QMessageBox.information(self, "Done", f"Tiles written to:\n{path}")

    def _on_error(self, msg):
        self._set_running(False)
        QMessageBox.critical(self, "Failed", msg)

    def _on_cancel(self):
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._lbl_status.setText("Cancelling...")

    def _set_running(self, running):
        self._btn_run.setEnabled(not running)
        self._btn_cancel.setEnabled(running)
        if running:
            self._progress.setValue(0)

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
            import struct
            import numpy as np
            from osgeo import gdal, osr

            # Read all headers to find bounding box and resolution.
            tiles = []
            for path in abt_files:
                hdr = self._read_abt_header(path)
                if hdr:
                    tiles.append(hdr)

            if not tiles:
                QMessageBox.warning(self, "No Valid Tiles",
                                    "Could not read any .abt headers.")
                return

            # Find the finest resolution and compute mosaic extent.
            ref_res = min(t["pixel_res"] for t in tiles)
            bb_s = min(t["ul_lat"] - t["size"] * t["pixel_res"] for t in tiles)
            bb_n = max(t["ul_lat"] for t in tiles)
            bb_w = min(t["ul_lon"] for t in tiles)
            bb_e = max(t["ul_lon"] + t["size"] * t["pixel_res"] for t in tiles)

            canvas_w = int(math.ceil((bb_e - bb_w) / ref_res))
            canvas_h = int(math.ceil((bb_n - bb_s) / ref_res))

            # Limit canvas size to avoid OOM.
            max_dim = 16384
            if max(canvas_w, canvas_h) > max_dim:
                scale = max(canvas_w, canvas_h) / max_dim
                ref_res *= scale
                canvas_w = int(math.ceil((bb_e - bb_w) / ref_res))
                canvas_h = int(math.ceil((bb_n - bb_s) / ref_res))
                self._log_msg(
                    f"Downsampled mosaic to {canvas_w}x{canvas_h} "
                    f"(res={ref_res * 111111:.1f}m)"
                )

            self._log_msg(
                f"Mosaic: {canvas_w}x{canvas_h} px, "
                f"res={ref_res * 111111:.1f}m, "
                f"bbox S={bb_s:.4f} N={bb_n:.4f} W={bb_w:.4f} E={bb_e:.4f}"
            )

            mosaic = np.full((canvas_h, canvas_w), -9999.0, dtype=np.float32)

            for i, tile in enumerate(tiles):
                raw = self._read_abt_data(tile)
                if raw is None:
                    continue
                grid = raw.astype(np.float32) * 0.5  # i16 → metres

                col_off = int(round((tile["ul_lon"] - bb_w) / ref_res))
                row_off = int(round((bb_n - tile["ul_lat"]) / ref_res))

                tile_deg = tile["size"] * tile["pixel_res"]
                dst_w = int(round(tile_deg / ref_res))
                dst_h = dst_w

                # Resample if tile resolution differs from mosaic.
                if dst_w != tile["size"] or dst_h != tile["size"]:
                    ri = np.linspace(0, tile["size"] - 1, dst_h).astype(int)
                    ci = np.linspace(0, tile["size"] - 1, dst_w).astype(int)
                    grid = grid[ri, :][:, ci]

                # Clip to canvas bounds.
                sr0, sr1 = 0, dst_h
                sc0, sc1 = 0, dst_w
                dr0, dr1 = row_off, row_off + dst_h
                dc0, dc1 = col_off, col_off + dst_w

                if dr0 < 0: sr0 -= dr0; dr0 = 0
                if dc0 < 0: sc0 -= dc0; dc0 = 0
                if dr1 > canvas_h: sr1 -= (dr1 - canvas_h); dr1 = canvas_h
                if dc1 > canvas_w: sc1 -= (dc1 - canvas_w); dc1 = canvas_w

                if dr1 > dr0 and dc1 > dc0:
                    src = grid[sr0:sr1, sc0:sc1]
                    valid = src > -5000
                    mosaic[dr0:dr1, dc0:dc1][valid] = src[valid]

            # Write to temp GeoTIFF.
            out_tif = os.path.join(
                tempfile.gettempdir(),
                f"aether_inspect_{os.path.basename(folder)}.tif",
            )
            driver = gdal.GetDriverByName("GTiff")
            ds = driver.Create(out_tif, canvas_w, canvas_h, 1, gdal.GDT_Float32)
            ds.SetGeoTransform([bb_w, ref_res, 0, bb_n, 0, -ref_res])
            srs = osr.SpatialReference()
            srs.ImportFromEPSG(4326)
            ds.SetProjection(srs.ExportToWkt())
            band = ds.GetRasterBand(1)
            band.SetNoDataValue(-9999.0)
            band.WriteArray(mosaic)
            ds.FlushCache()
            ds = None

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

    @staticmethod
    def _read_abt_header(path: str) -> Optional[Dict[str, Any]]:
        """Read the 44-byte .abt header."""
        import struct
        try:
            with open(path, "rb") as f:
                hdr = f.read(44)
            if len(hdr) < 44:
                return None
            magic, ver, size, ul_lat, ul_lon, sc_y, sc_x, base, stride = \
                struct.unpack("<4sHHddddhH", hdr)
            if magic != b"AETH":
                return None
            pixel_res = sc_x if sc_x < 0.005 else sc_x / size
            return {
                "path": path, "size": size, "ul_lat": ul_lat,
                "ul_lon": ul_lon, "pixel_res": pixel_res, "stride": stride,
            }
        except Exception:
            return None

    @staticmethod
    def _read_abt_data(info: Dict[str, Any]):
        """Read .abt pixel data, handling row-stride padding."""
        import numpy as np
        try:
            size = info["size"]
            stride = info["stride"]
            bpr = size * 2
            with open(info["path"], "rb") as f:
                f.seek(44)
                grid = np.zeros((size, size), dtype=np.int16)
                for i in range(size):
                    grid[i, :] = np.frombuffer(f.read(bpr), dtype=np.int16)
                    pad = stride - bpr
                    if pad > 0:
                        f.seek(pad, os.SEEK_CUR)
            return grid
        except Exception:
            return None

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
