"""Site Analysis tab widget -- multi-site, multi-altitude coverage analysis.

Embeddable QWidget designed for use as a tab inside the main AETHER dialog.
Supports both LOS (pure geometric line-of-sight) and LOSS (signal-strength)
modes, with per-site parameters and multiple receiver altitudes.

The parent dialog must provide:
- ``self.iface``           QgisInterface
- ``self.get_mode()``      returns "LOS" or "LOSS"
- ``self.get_loss_model()`` returns "SIMPLE_LOSS" or "ITM"
"""

from __future__ import annotations

import os
import platform
import re
import subprocess
import tempfile
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from qgis.PyQt.QtCore import QThread, pyqtSignal, Qt
from qgis.PyQt.QtWidgets import (
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
from ..core.binary_manager import find_binary
from ..core import api_key

# Hide console windows on Windows.
_SUBPROCESS_FLAGS = (
    subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
)
from ..core.job_builder import (
    INVALID,
    VALID_RESOLUTIONS,
    CoverageParams,
    build_coverage_job,
    format_model_warnings,
    model_warnings,
    write_job_file,
)
from ..core.layer_utils import dem_layer_warning, hide_from_dem_picker
from ..core.result_loader import (
    GROUP_COVERAGE,
    add_layer_to_project,
    load_coverage_result,
)
from ..core.binary_manager import binaries_warning
from ..core.terrain_adapter import (
    analysis_bbox,
    buildings_identity,
    get_source_resolution_info,
    prepare_terrain,
    reset_terrain_warnings,
    terrain_coverage_warning,
    terrain_plan,
    terrain_size_warning,
)
from .map_tools import activate_point_capture

TAG = "Waveshed"


def _log_terrain_plan(cached: int, missing: int, download_mb: int,
                      cached_mb: int) -> None:
    """Record what the run will reuse vs build, before it starts.

    Without this the log jumps straight to per-tile work and there is no way
    to tell a cache that is working from one that is silently rebuilding.
    """
    QgsMessageLog.logMessage(
        f"terrain plan: {cached + missing} tile(s) needed — {cached} pooled "
        f"(~{cached_mb} MB reused), {missing} to build (~{download_mb} MB)",
        TAG, Qgis.MessageLevel.Info,
    )

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

# Altitude table columns.
_ALT_COLUMNS = ["Altitude (m)", "Reference"]


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

def _kill_proc(proc: subprocess.Popen) -> None:
    """Kill a subprocess and all its children (Windows-safe)."""
    if proc.poll() is not None:
        return
    if platform.system() == "Windows":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
            creationflags=_SUBPROCESS_FLAGS,
        )
    else:
        proc.terminate()


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
    ) -> None:
        super().__init__(parent)
        self.jobs = jobs
        self.dem_layer = dem_layer
        self.output_dir = output_dir
        self.terrain_dir = terrain_dir
        self.osm_buildings = osm_buildings
        self._canceled = False
        self._proc: Optional[subprocess.Popen] = None

    def cancel(self) -> None:
        self._canceled = True
        if self._proc is not None:
            _kill_proc(self._proc)

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
                    osm_buildings=self.osm_buildings,
                )

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

                # `with` closes the stdout pipe (and waits) on exit so we don't
                # leak a file handle (the ResourceWarning).
                with subprocess.Popen(
                    [core_exe, "--config", job_file],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,  # merge stderr into stdout
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                    creationflags=_SUBPROCESS_FLAGS,
                ) as proc:
                    self._proc = proc  # expose for kill

                    stderr_lines: list[str] = []
                    for line in iter(proc.stdout.readline, ""):
                        if self._canceled:
                            _kill_proc(proc)
                            return
                        stderr_lines.append(line)
                        # Surface aether_core's own log (it writes its [Core]
                        # tile-grid / bounds diagnostics to stdout) in the QGIS
                        # Log Messages panel under the AETHER tag.
                        s = line.rstrip()
                        if s:
                            QgsMessageLog.logMessage(s, TAG, Qgis.MessageLevel.Info)
                        pct = self._parse_wedge_progress(line)
                        if pct is not None:
                            self.progress.emit(_job_progress(20 + int(pct * 0.6)))

                self._proc = None
                if proc.returncode != 0:
                    tail = "".join(stderr_lines[-20:])
                    raise RuntimeError(
                        f"aether_core exited with code {proc.returncode}"
                        f" for {display_name}:\n{tail}"
                    )

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

                result = subprocess.run(
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
                if result.returncode != 0:
                    raise RuntimeError(
                        f"aether_export failed for {display_name}"
                        f" (exit {result.returncode}):\n{result.stderr}"
                    )

                results.append((tif_path, params.model, display_name))
                self.progress.emit(_job_progress(100))

            self.progress.emit(100)
            self.finished_ok.emit(results)

        except Exception as exc:
            self.finished_err.emit(str(exc))


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
        self._sites_rb = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.PointGeometry)
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
    # Asset helpers
    # ==================================================================

    def _refresh_asset_cache(self) -> None:
        """Reload assets from disk and rebuild the name-keyed cache."""
        self._asset_cache.clear()
        for asset in list_assets():
            name = asset.get("name", "")
            if name:
                self._asset_cache[name] = asset

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
        self.sites_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.sites_table.setSelectionMode(QTableWidget.SingleSelection)
        self.sites_table.verticalHeader().setVisible(False)
        self.sites_table.verticalHeader().setDefaultSectionSize(36)
        self._setup_sites_columns()
        layout.addWidget(self.sites_table)

        return container

    def _setup_sites_columns(self) -> None:
        """Configure sites table columns (same for all modes)."""
        self.sites_table.setColumnCount(len(_SITE_COLUMNS))
        self.sites_table.setHorizontalHeaderLabels(_SITE_COLUMNS)
        # Location column gets extra space; others resize to content.
        self.sites_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents,
        )
        self.sites_table.horizontalHeader().setSectionResizeMode(
            _COL_LOCATION, QHeaderView.Stretch,
        )

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
            QHeaderView.Stretch,
        )
        self.alt_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.alt_table.setSelectionMode(QTableWidget.SingleSelection)
        self.alt_table.verticalHeader().setVisible(False)
        layout.addWidget(self.alt_table)

        # Shown instead of the table in MIN_ALT mode, where a single run already
        # covers every altitude so per-altitude rows would only duplicate work.
        self.lbl_alt_min_alt_note = QLabel(
            "Minimum LOS Altitude mode computes the lowest line-of-sight "
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

        # DEM Layer
        self.combo_dem = QComboBox()
        self.combo_dem.currentIndexChanged.connect(self._on_dem_changed)
        layout.addRow("DEM Layer:", self.combo_dem)

        # Source info (read-only)
        self.lbl_dem_info = QLabel("")
        self.lbl_dem_info.setStyleSheet("color: gray; font-size: 11px;")
        layout.addRow("", self.lbl_dem_info)

        # Resolution
        self.combo_resolution = QComboBox()
        self.combo_resolution.addItems([str(r) for r in VALID_RESOLUTIONS])
        self.combo_resolution.setCurrentIndex(2)  # default 10
        layout.addRow("Resolution (m):", self.combo_resolution)

        # Buildings — fetched from OpenFreeMap and burned into the terrain
        # surface, so they obstruct exactly like terrain does.
        self.chk_buildings = QCheckBox("Include buildings (OpenFreeMap)")
        self.chk_buildings.setToolTip(
            "Download OpenStreetMap building footprints and treat them as part "
            "of the terrain.\n\n"
            "Buildings are only published at zoom 14, so this adds a sizeable "
            "download and is limited to smaller analysis areas.\n\n"
            + source_credit("OpenFreeMap")
        )
        layout.addRow("Buildings:", self.chk_buildings)

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
        self.line_output_dir.setText(
            os.path.join(tempfile.gettempdir(), "aether_output"),
        )
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
        self.btn_run.clicked.connect(self._on_run)
        self.btn_stop.clicked.connect(self._on_stop)

    # ==================================================================
    # Populate raster layers
    # ==================================================================

    _LOCAL_DIR_PREFIX = "Local: "

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

    def _populate_raster_layers(self) -> None:
        """Fill the DEM combo with raster layers + local terrain dir from settings.

        Preserves the current selection across refills so a live update (a
        layer added or removed in the project) does not reset the user's
        choice.
        """
        prev_key = self._current_dem_key()

        self.combo_dem.blockSignals(True)
        self.combo_dem.clear()
        self._dem_layers: list = []  # QgsRasterLayer or str (path)

        # Add local terrain directory from settings (if configured).
        from qgis.core import QgsSettings as _QS
        terrain_dir = _QS().value("waveshed/terrain_dir", "").strip()
        if terrain_dir and os.path.isdir(terrain_dir):
            self.combo_dem.addItem(f"{self._LOCAL_DIR_PREFIX}{terrain_dir}")
            self._dem_layers.append(terrain_dir)

        for layer in QgsProject.instance().mapLayers().values():
            # Skip our own result rasters (coverage/P2P) — they are never valid
            # DEM inputs and only clutter the picker. Terrain we generated is
            # kept (it IS elevation data).
            if isinstance(layer, QgsRasterLayer) and not hide_from_dem_picker(layer):
                self.combo_dem.addItem(layer.name())
                self._dem_layers.append(layer)

        # Restore the previous selection if it still exists.
        if prev_key is not None:
            for i, entry in enumerate(self._dem_layers):
                if self._dem_entry_key(entry) == prev_key:
                    self.combo_dem.setCurrentIndex(i)
                    break
        self.combo_dem.blockSignals(False)
        self._on_dem_changed()

    def _on_project_layers_changed(self, *args) -> None:
        """Repopulate the DEM combo when project layers are added/removed."""
        self._populate_raster_layers()

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
        spin_height.setRange(0.0, 10000.0)
        spin_height.setValue(30.0)
        spin_height.setDecimals(1)
        spin_height.setSuffix(" m")
        self.sites_table.setCellWidget(row, _COL_HEIGHT, spin_height)

        # -- Column 3: Height Mode --
        combo_mode = QComboBox()
        combo_mode.addItems(["AGL", "AMSL"])
        self.sites_table.setCellWidget(row, _COL_HEIGHT_MODE, combo_mode)

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

        # Auto-fill height.
        spin_h = self.sites_table.cellWidget(row, _COL_HEIGHT)
        if isinstance(spin_h, QDoubleSpinBox):
            default_h = asset.get("default_height_m", 30.0)
            spin_h.setValue(default_h)

        # Auto-fill height mode.
        combo_m = self.sites_table.cellWidget(row, _COL_HEIGHT_MODE)
        if isinstance(combo_m, QComboBox):
            mode_text = asset.get("default_height_mode", "AGL")
            idx = combo_m.findText(mode_text)
            if idx >= 0:
                combo_m.setCurrentIndex(idx)

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
        self._sites_rb.reset(QgsWkbTypes.PointGeometry)
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

        # Keep a reference to prevent garbage collection.
        tool = activate_point_capture(self.iface, _callback)
        self._pick_tools.append(tool)

        # Minimize the parent dialog so the user can interact with the map.
        parent_window = self._dialog
        if hasattr(parent_window, "showMinimized"):
            parent_window.showMinimized()

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
        spin_alt.setRange(0.0, 10000.0)
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
                try:
                    asset = load_asset(asset["_path"])
                except Exception:
                    pass  # fall back to cached version

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
                        f"— Minimum LOS Altitude"
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
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return

        output_dir = self.line_output_dir.text().strip()
        if not output_dir:
            QMessageBox.warning(
                self, "No Output Directory",
                "Please specify an output directory.",
            )
            return

        # ---- The engine has to be there ----
        # Checked before anything else: terrain preparation falls back to the
        # slow QGIS raster path when aether_converter is missing, so without
        # this the user waits out the whole extract and only then hits the
        # error. Blocking, not advisory — the run cannot succeed.
        engine_warning = binaries_warning()
        if engine_warning:
            QMessageBox.critical(self, "Aether engine not found", engine_warning)
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
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return

        # ---- Confirm an unreasonably large terrain download ----
        # Jobs that only differ by receiver altitude share one terrain cache,
        # so dedupe on the parameters the cache key is built from — otherwise
        # the estimate would multiply by the number of altitudes.
        seen: set = set()
        total_mb = 0
        cached_mb = 0
        tiles_cached = 0
        tiles_missing = 0
        union: Optional[dict] = None
        # Price against the tile pool, not the whole set: terrain that is
        # already on disk must not be announced as a download.
        plan_source = terrain_dir or (dem_layer.source() if dem_layer else "")
        plan_buildings = buildings_identity(
            osm_buildings=self.chk_buildings.isChecked())
        for params, _name in jobs:
            key = (params.tx_lat, params.tx_lon, params.max_range_km,
                   params.resolution_m, params.az_start, params.az_end)
            if key in seen:
                continue
            seen.add(key)
            plan = terrain_plan(
                plan_source, params.tx_lat, params.tx_lon,
                params.max_range_km, params.resolution_m,
                params.az_start, params.az_end, plan_buildings,
            )
            total_mb += plan["download_mb"]
            cached_mb += plan["total_mb"] - plan["download_mb"]
            tiles_cached += plan["tiles_cached"]
            tiles_missing += plan["tiles_missing"]
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

        # A local terrain directory that does not reach across the analysis
        # area is a mistake worth stopping for: it is the only source once
        # selected, so whatever it misses is computed over 0 m sea level.
        if terrain_dir and union is not None:
            cov = terrain_coverage_warning(terrain_dir, union)
            if cov and QMessageBox.warning(
                self, "Incomplete terrain coverage", cov + "\n\nRun anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return

        _log_terrain_plan(tiles_cached, tiles_missing, total_mb, cached_mb)
        warn = terrain_size_warning(total_mb, cached_mb)
        if warn is not None:
            msg, strong = warn
            default = QMessageBox.No if strong else QMessageBox.Yes
            if QMessageBox.warning(
                self, "Large terrain download", msg,
                QMessageBox.Yes | QMessageBox.No, default,
            ) != QMessageBox.Yes:
                return

        # ---- Launch worker ----
        self.btn_run.setEnabled(False)
        self.btn_stop.setVisible(True)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.status_label.setVisible(True)
        self.status_label.setText("Starting...")

        terrain_dir = self._selected_terrain_dir() or ""
        self._worker = _SiteAnalysisWorker(
            jobs, dem_layer, output_dir, terrain_dir, self,
            osm_buildings=self.chk_buildings.isChecked(),
        )
        self._worker.progress.connect(self.progress_bar.setValue)
        self._worker.status.connect(self._on_status)
        self._worker.finished_ok.connect(self._on_finished_ok)
        self._worker.finished_err.connect(self._on_finished_err)
        self._worker.start()

    def _on_stop(self) -> None:
        """Stop a running analysis."""
        if self._worker and self._worker.isRunning():
            self.status_label.setText("Stopping...")
            self._worker.cancel()
            self._worker.wait(5000)
        self._reset_run_ui()

    def _reset_run_ui(self) -> None:
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
                f"Completed {len(results)} Minimum LOS Altitude job(s).\n"
                f"{loaded_count} result(s) loaded into QGIS.\n\n"
                "Open the Altitude Explorer to pick a preferred altitude and "
                "see the reachable area live?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes,
            )
            if reply == QMessageBox.Yes:
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
        QMessageBox.critical(self, "Analysis Failed", message)

    # ==================================================================
    # Cancel (called by parent dialog if needed)
    # ==================================================================

    def cancel_worker(self) -> None:
        """Cancel a running worker and clean up map visuals."""
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(5000)
        # Remove site markers from the canvas.
        self._sites_rb.reset()
