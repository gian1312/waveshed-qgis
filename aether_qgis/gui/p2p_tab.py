"""P2P Link Analysis tab widget -- embedded in the main dialog.

QWidget providing single-link P2P analysis.  Supports LOS and LOSS
modes with dynamic label and field visibility.  Generates a temporary
2-line batch CSV from the two endpoints, then runs the P2P pipeline
(terrain_adapter -> job_builder -> aether_core) in a background QThread.

P2P mode in aether_core produces output files directly (no aether_export):
  - {output_name}.csv        -- Source_ID, Target_ID, Signal_dBm, Path_Loss_dB
  - p2p_report.txt           -- human-readable summary
  - terrain_profile.gp       -- terrain elevations vs distance
  - height_profile.gp        -- terrain with earth bulge
  - path_profile.gp          -- cumulative path loss at each distance
"""

from __future__ import annotations

import csv
import math
import os
import platform
import re
import subprocess
import tempfile
from datetime import datetime
from typing import List, Optional, Tuple

from qgis.PyQt.QtCore import QThread, pyqtSignal, Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTabWidget,
    QTextEdit,
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

from ..core.binary_manager import find_binary
from ..core.job_builder import P2PParams, build_p2p_job, write_job_file
from ..core.layer_utils import dem_layer_warning, hide_from_dem_picker
from .map_tools import activate_point_capture

TAG = "AETHER"

_SUBPROCESS_FLAGS = (
    subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
)


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


# ---------------------------------------------------------------------------
# GP file parser
# ---------------------------------------------------------------------------

def _parse_gp_file(filepath: str) -> Tuple[List[float], List[float]]:
    """Parse a .gp (gnuplot data) file into x and y value lists.

    Lines starting with ``#`` or blank lines are skipped.  Each data line
    is expected to have at least two whitespace-separated float columns.

    Returns (x_values, y_values).  Both lists are empty when the file
    does not exist or contains no valid data.
    """
    x_val: List[float] = []
    y_val: List[float] = []
    if not os.path.exists(filepath):
        return x_val, y_val
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            parts = stripped.split()
            if len(parts) >= 2:
                try:
                    x_val.append(float(parts[0]))
                    y_val.append(float(parts[1]))
                except ValueError:
                    continue
    return x_val, y_val


# ---------------------------------------------------------------------------
# Batch CSV helpers
# ---------------------------------------------------------------------------

def _parse_batch_csv(path: str) -> List[Tuple[str, str, float, float, float, str]]:
    """Parse an AETHER batch CSV into a list of (type, id, lat, lon, alt, mode) tuples.

    Raises ValueError on malformed rows.
    """
    entries: List[Tuple[str, str, float, float, float, str]] = []
    with open(path, "r", newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        for line_no, row in enumerate(reader, start=1):
            if not row or row[0].startswith("#"):
                continue
            if len(row) < 6:
                raise ValueError(
                    f"Line {line_no}: expected 6 fields, got {len(row)}"
                )
            row_type = row[0].strip().upper()
            if row_type not in ("S", "R"):
                raise ValueError(
                    f"Line {line_no}: type must be 'S' or 'R', got '{row[0].strip()}'"
                )
            mode = row[5].strip().upper()
            if mode not in ("AGL", "AMSL"):
                raise ValueError(
                    f"Line {line_no}: mode must be AGL or AMSL, got '{row[5].strip()}'"
                )
            entries.append((
                row_type,
                row[1].strip(),
                float(row[2].strip()),
                float(row[3].strip()),
                float(row[4].strip()),
                mode,
            ))
    return entries


def _compute_bbox_for_entries(
    entries: List[Tuple[str, str, float, float, float, str]],
) -> Tuple[float, float, float, float]:
    """Return (min_lat, max_lat, min_lon, max_lon) enclosing all CSV entries."""
    lats = [e[2] for e in entries]
    lons = [e[3] for e in entries]
    return min(lats), max(lats), min(lons), max(lons)


def _write_temp_batch_csv(
    tx_lat: float,
    tx_lon: float,
    tx_height: float,
    tx_mode: str,
    rx_lat: float,
    rx_lon: float,
    rx_height: float,
    rx_mode: str,
) -> str:
    """Write a temporary 2-line batch CSV for single-link analysis.

    Returns the path to the written file.
    """
    path = os.path.join(
        tempfile.gettempdir(),
        f"aether_p2p_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
    )
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["S", "TX", f"{tx_lat:.6f}", f"{tx_lon:.6f}",
                         f"{tx_height:.1f}", tx_mode])
        writer.writerow(["R", "RX", f"{rx_lat:.6f}", f"{rx_lon:.6f}",
                         f"{rx_height:.1f}", rx_mode])
    return path


# ---------------------------------------------------------------------------
# P2P Result Viewer Dialog (matplotlib plots)
# ---------------------------------------------------------------------------

class _P2PResultViewer(QDialog):
    """Dialog showing P2P result plots with matplotlib.

    Tab 1 -- Combined Link: dual-axis with terrain profile and signal
    strength overlay.

    Tab 2 -- Curved Earth Profile: terrain on earth bulge, LOS line,
    and first Fresnel zone.
    """

    def __init__(
        self,
        output_dir: str,
        output_name: str,
        freq_mhz: float,
        tx_height: float,
        rx_height: float,
        tx_dbm: float,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("P2P Link Analysis Plots")
        self.setMinimumSize(800, 550)
        self.setAttribute(Qt.WA_DeleteOnClose)

        self._output_dir = output_dir
        self._output_name = output_name
        self._freq_mhz = freq_mhz
        self._tx_height = tx_height
        self._rx_height = rx_height
        self._tx_dbm = tx_dbm

        # Deferred matplotlib import with Qt5Agg backend.
        import matplotlib
        matplotlib.use("Qt5Agg")
        from matplotlib.backends.backend_qt5agg import (
            FigureCanvasQTAgg,
            NavigationToolbar2QT,
        )
        from matplotlib.figure import Figure

        self._FigureCanvasQTAgg = FigureCanvasQTAgg
        self._NavigationToolbar2QT = NavigationToolbar2QT
        self._Figure = Figure

        self._build_ui()

    # ---- UI ---------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        tabs = QTabWidget()

        # Tab 1: Combined Link
        combined_widget = self._build_combined_tab()
        tabs.addTab(combined_widget, "Terrain Profile")

        # Tab 2: Curved Earth Profile
        curved_widget = self._build_curved_earth_tab()
        tabs.addTab(curved_widget, "Curved Earth Profile")

        layout.addWidget(tabs)

    # ---- Tab 1: Combined Link ---------------------------------------------

    def _build_combined_tab(self) -> QWidget:
        """Dual-axis plot: terrain profile (left) and signal strength (right)."""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        fig = self._Figure(figsize=(8, 4), dpi=100)
        canvas = self._FigureCanvasQTAgg(fig)
        toolbar = self._NavigationToolbar2QT(canvas, widget)
        layout.addWidget(toolbar)
        layout.addWidget(canvas)

        terrain_file = os.path.join(self._output_dir, "terrain_profile.gp")
        path_file = os.path.join(self._output_dir, "path_profile.gp")

        dist_terrain, elev = _parse_gp_file(terrain_file)
        dist_path, path_loss = _parse_gp_file(path_file)

        ax1 = fig.add_subplot(111)

        if dist_terrain and elev:
            ax1.fill_between(dist_terrain, elev, alpha=0.4, color="green",
                             label="Terrain")
            ax1.plot(dist_terrain, elev, color="darkgreen", linewidth=0.8)
            ax1.set_xlabel("Distance (km)")
            ax1.set_ylabel("Elevation (m)", color="darkgreen")
            ax1.tick_params(axis="y", labelcolor="darkgreen")
        else:
            ax1.set_xlabel("Distance (km)")
            ax1.set_ylabel("Elevation (m)")
            ax1.text(0.5, 0.5, "No terrain profile data",
                     transform=ax1.transAxes, ha="center", va="center",
                     fontsize=12, color="gray")

        if dist_path and path_loss:
            # Compute signal: tx_dbm - path_loss
            signal = [self._tx_dbm - pl for pl in path_loss]
            ax2 = ax1.twinx()
            ax2.plot(dist_path, signal, color="red", linewidth=1.5,
                     label="Signal (dBm)")
            ax2.set_ylabel("Signal Strength (dBm)", color="red")
            ax2.tick_params(axis="y", labelcolor="red")

            # Combined legend
            lines1, labels1 = ax1.get_legend_handles_labels()
            lines2, labels2 = ax2.get_legend_handles_labels()
            ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper right",
                       fontsize=8)
        elif dist_terrain and elev:
            ax1.legend(loc="upper right", fontsize=8)

        ax1.set_title("Combined Link Profile")
        fig.tight_layout()
        canvas.draw()

        return widget

    # ---- Tab 2: Curved Earth Profile --------------------------------------

    def _build_curved_earth_tab(self) -> QWidget:
        """Terrain on curved earth with LOS line and Fresnel zone."""
        widget = QWidget()
        layout = QVBoxLayout(widget)

        fig = self._Figure(figsize=(8, 4), dpi=100)
        canvas = self._FigureCanvasQTAgg(fig)
        toolbar = self._NavigationToolbar2QT(canvas, widget)
        layout.addWidget(toolbar)
        layout.addWidget(canvas)

        terrain_file = os.path.join(self._output_dir, "terrain_profile.gp")
        dist_km, elev = _parse_gp_file(terrain_file)

        ax = fig.add_subplot(111)

        if not dist_km or not elev:
            ax.text(0.5, 0.5, "No terrain profile data",
                    transform=ax.transAxes, ha="center", va="center",
                    fontsize=12, color="gray")
            ax.set_title("Curved Earth Profile")
            canvas.draw()
            return widget

        total_dist_km = dist_km[-1] if dist_km else 1.0
        r_eff = 6371000.0 * (4.0 / 3.0)  # effective earth radius (m)

        # Compute earth bulge for each sample point
        h_bulge = []
        for d in dist_km:
            d1_m = d * 1000.0
            d2_m = (total_dist_km - d) * 1000.0
            bulge = (d1_m * d2_m) / (2.0 * r_eff)
            h_bulge.append(bulge)

        # Terrain on curved earth
        terrain_curved = [e + b for e, b in zip(elev, h_bulge)]
        ax.fill_between(dist_km, terrain_curved, alpha=0.35, color="saddlebrown",
                        label="Terrain")
        ax.plot(dist_km, terrain_curved, color="saddlebrown", linewidth=0.8)

        # Antenna tip heights (at endpoints the bulge is 0)
        tx_tip = elev[0] + self._tx_height + h_bulge[0]
        rx_tip = elev[-1] + self._rx_height + h_bulge[-1]

        # LOS line
        ax.plot([dist_km[0], dist_km[-1]], [tx_tip, rx_tip],
                color="blue", linewidth=1.2, linestyle="--", label="LOS")

        # First Fresnel zone (F1)
        if self._freq_mhz > 0 and total_dist_km > 0:
            fresnel_upper = []
            fresnel_lower = []
            for d in dist_km:
                d1_km = d
                d2_km = total_dist_km - d
                if d1_km <= 0 or d2_km <= 0:
                    # At endpoints the Fresnel radius is 0
                    frac = d / total_dist_km if total_dist_km > 0 else 0
                    los_h = tx_tip + frac * (rx_tip - tx_tip)
                    fresnel_upper.append(los_h)
                    fresnel_lower.append(los_h)
                    continue
                f1 = 17.32 * math.sqrt(
                    d1_km * d2_km / (self._freq_mhz * total_dist_km)
                )
                frac = d / total_dist_km
                los_h = tx_tip + frac * (rx_tip - tx_tip)
                fresnel_upper.append(los_h + f1)
                fresnel_lower.append(los_h - f1)

            ax.fill_between(dist_km, fresnel_lower, fresnel_upper,
                            alpha=0.15, color="orange", label="Fresnel Zone (F1)")

        # Site markers
        ax.plot(dist_km[0], tx_tip, "rv", markersize=8, label="TX")
        ax.plot(dist_km[-1], rx_tip, "b^", markersize=8, label="RX")

        ax.set_xlabel("Distance (km)")
        ax.set_ylabel("Height (m)")
        ax.set_title("Curved Earth Profile (4/3 earth radius)")
        ax.legend(loc="upper right", fontsize=7)
        fig.tight_layout()
        canvas.draw()

        return widget


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------

class _P2PWorker(QThread):
    """Run the P2P pipeline off the main thread.

    Pipeline: terrain_adapter -> job_builder -> aether_core.
    P2P mode does NOT use aether_export -- aether_core writes output
    files directly.

    The ``finished_ok`` signal emits the output directory path (not a
    single file) so the caller can locate the CSV and .gp files.
    """

    progress = pyqtSignal(int)           # 0-100
    status = pyqtSignal(str)             # human-readable step description
    finished_ok = pyqtSignal(str)        # output_dir
    finished_err = pyqtSignal(str)       # error message

    def __init__(
        self,
        params: P2PParams,
        dem_layer: QgsRasterLayer,
        output_dir: str,
        batch_file: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.params = params
        self.dem_layer = dem_layer
        self.output_dir = output_dir
        self.batch_file = batch_file
        self._canceled = False
        self._proc: Optional[subprocess.Popen] = None

    def cancel(self) -> None:
        self._canceled = True
        if self._proc is not None:
            _kill_proc(self._proc)

    def _parse_p2p_progress(self, line: str) -> Optional[int]:
        """Extract percentage from P2P progress lines."""
        m = re.search(r"(\d+)/(\d+)", line)
        if m:
            current, total = int(m.group(1)), int(m.group(2))
            if total > 0:
                return int(current * 100 / total)
        return None

    def run(self) -> None:  # noqa: C901 -- sequential pipeline steps
        try:
            params = self.params
            output_dir = self.output_dir
            os.makedirs(output_dir, exist_ok=True)

            self.progress.emit(0)

            # ---- 1. Compute terrain bbox from batch CSV ----
            self.status.emit("Computing terrain extents...")
            self.progress.emit(5)

            entries = _parse_batch_csv(self.batch_file)
            min_lat, max_lat, min_lon, max_lon = _compute_bbox_for_entries(entries)

            # Centroid as reference for terrain prep.
            centre_lat = (min_lat + max_lat) / 2.0
            centre_lon = (min_lon + max_lon) / 2.0

            # Range that covers the full extent (with margin).
            lat_span_km = (max_lat - min_lat) * 111.0
            cos_lat = math.cos(math.radians(centre_lat))
            if cos_lat < 1e-6:
                cos_lat = 1e-6
            lon_span_km = (max_lon - min_lon) * 111.0 * cos_lat
            half_diag_km = math.sqrt(lat_span_km**2 + lon_span_km**2) / 2.0
            max_range_km = max(half_diag_km + 5.0, 10.0)

            if self._canceled:
                return

            # ---- 2. Prepare terrain ----
            self.status.emit("Preparing terrain tiles...")
            self.progress.emit(10)

            from ..core import binary_manager as bm
            from ..core.terrain_adapter import prepare_terrain

            abt_dir = prepare_terrain(
                dem_layer=self.dem_layer,
                tx_lat=centre_lat,
                tx_lon=centre_lon,
                max_range_km=max_range_km,
                resolution_m=params.resolution_m,
                binary_manager=bm,
            )

            if self._canceled:
                return

            # ---- 3. Build job config ----
            self.status.emit("Building job configuration...")
            self.progress.emit(20)

            params.max_range_km = int(math.ceil(max_range_km))

            job_config = build_p2p_job(
                params, abt_dir, output_dir, batch_file=self.batch_file,
            )
            job_file = write_job_file(job_config, output_dir, params.output_name)

            if self._canceled:
                return

            # ---- 4. Run aether_core ----
            self.status.emit("Running aether_core...")
            self.progress.emit(25)

            core_exe = find_binary("aether_core")
            env = os.environ.copy()
            env["RUST_LOG"] = "info"

            proc = subprocess.Popen(
                [core_exe, "--config", job_file],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                creationflags=_SUBPROCESS_FLAGS,
            )
            self._proc = proc

            output_lines: list[str] = []
            for line in iter(proc.stdout.readline, ""):
                if self._canceled:
                    _kill_proc(proc)
                    return
                output_lines.append(line)
                # Surface aether_core's own stdout log in the QGIS Log
                # Messages panel under the AETHER tag.
                s = line.rstrip()
                if s:
                    QgsMessageLog.logMessage(s, TAG, Qgis.MessageLevel.Info)
                pct = self._parse_p2p_progress(line)
                if pct is not None:
                    self.progress.emit(25 + int(pct * 0.70))

            proc.wait()
            self._proc = None
            if proc.returncode != 0:
                tail = "".join(output_lines[-20:])
                raise RuntimeError(
                    f"aether_core exited with code {proc.returncode}:\n{tail}"
                )

            if self._canceled:
                return

            # ---- 5. Verify output files ----
            # P2P produces output directly -- no aether_export step.
            self.status.emit("Verifying output files...")
            self.progress.emit(95)

            csv_path = os.path.join(output_dir, params.output_name + ".csv")
            if not os.path.isfile(csv_path):
                raise RuntimeError(
                    f"Expected P2P result CSV not found: {csv_path}\n"
                    "aether_core may have failed to produce output."
                )

            # Log which supplementary files are present.
            for gp_name in ("terrain_profile.gp", "height_profile.gp", "path_profile.gp"):
                gp_path = os.path.join(output_dir, gp_name)
                if os.path.isfile(gp_path):
                    self.status.emit(f"  Found {gp_name}")

            report_path = os.path.join(output_dir, "p2p_report.txt")
            if os.path.isfile(report_path):
                self.status.emit("  Found p2p_report.txt")

            self.progress.emit(100)
            self.finished_ok.emit(output_dir)

        except Exception as exc:
            self.finished_err.emit(str(exc))


# ---------------------------------------------------------------------------
# P2P Tab Widget
# ---------------------------------------------------------------------------

class P2PTab(QWidget):
    """P2P Link Analysis tab, embedded in the main dialog.

    The parent dialog must provide:
      - ``self.iface``            -- QgisInterface
      - ``self.get_mode()``       -- returns ``"LOS"`` or ``"LOSS"``
      - ``self.get_loss_model()`` -- returns ``"SIMPLE_LOSS"`` or ``"ITM"``
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._parent_dialog = parent
        self.iface = parent.iface
        self._worker: Optional[_P2PWorker] = None
        self._pick_tool = None  # reference kept to prevent GC
        self._last_output_dir: Optional[str] = None
        self._last_output_name: Optional[str] = None

        # Rubber bands for map visualisation
        canvas = self.iface.mapCanvas()
        self._site_a_rb = QgsRubberBand(canvas, QgsWkbTypes.PointGeometry)
        self._site_a_rb.setColor(QColor(255, 0, 0, 200))  # Red
        self._site_a_rb.setIconSize(12)

        self._site_b_rb = QgsRubberBand(canvas, QgsWkbTypes.PointGeometry)
        self._site_b_rb.setColor(QColor(0, 0, 255, 200))  # Blue
        self._site_b_rb.setIconSize(12)

        self._link_line_rb = QgsRubberBand(canvas, QgsWkbTypes.LineGeometry)
        self._link_line_rb.setColor(QColor(255, 255, 0, 255))  # Yellow
        self._link_line_rb.setWidth(3)

        self._build_ui()
        self._connect_signals()
        self._populate_raster_layers()
        # Keep the DEM combo in sync as project layers are added/removed.
        _proj = QgsProject.instance()
        _proj.layersAdded.connect(self._on_project_layers_changed)
        _proj.layersRemoved.connect(self._on_project_layers_changed)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_mode(self, mode: str) -> None:
        """Switch between LOS and LOSS display modes.

        Parameters
        ----------
        mode:
            ``"LOS"`` hides frequency/ERP and relabels sites as
            "Site A" / "Site B".
            ``"LOSS"`` shows frequency/ERP and labels sites as
            "Transmitter" / "Receiver".
        """
        is_loss = mode.upper() != "LOS"

        if is_loss:
            self._lbl_site_a.setText("Transmitter:")
            self._lbl_site_b.setText("Receiver:")
        else:
            self._lbl_site_a.setText("Site A:")
            self._lbl_site_b.setText("Site B:")

        self._params_group.setVisible(is_loss)

    def refresh_layers(self) -> None:
        """Re-populate the DEM layer combo from the current project."""
        self._populate_raster_layers()

    def cancel_worker(self) -> None:
        """Cancel any running worker thread.  Called by the parent dialog.

        Also resets all rubber bands from the map canvas.
        """
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(5000)

        self._reset_rubber_bands()

    # ------------------------------------------------------------------
    # Rubber band management
    # ------------------------------------------------------------------

    def _has_coords_a(self) -> bool:
        """Return True if Site A has non-zero coordinates."""
        return not (self.spin_a_lat.value() == 0.0 and self.spin_a_lon.value() == 0.0)

    def _has_coords_b(self) -> bool:
        """Return True if Site B has non-zero coordinates."""
        return not (self.spin_b_lat.value() == 0.0 and self.spin_b_lon.value() == 0.0)

    def _update_visuals(self) -> None:
        """Update rubber bands on the map canvas from current spinbox values."""
        canvas = self.iface.mapCanvas()
        canvas_crs = canvas.mapSettings().destinationCrs()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        xform = QgsCoordinateTransform(wgs84, canvas_crs, QgsProject.instance())

        lat_a = self.spin_a_lat.value()
        lon_a = self.spin_a_lon.value()
        lat_b = self.spin_b_lat.value()
        lon_b = self.spin_b_lon.value()

        has_a = self._has_coords_a()
        has_b = self._has_coords_b()

        # Site A marker
        if has_a:
            pt_a = xform.transform(QgsPointXY(lon_a, lat_a))
            self._site_a_rb.setToGeometry(QgsGeometry.fromPointXY(pt_a), None)
            self._site_a_rb.show()
        else:
            self._site_a_rb.reset(QgsWkbTypes.PointGeometry)

        # Site B marker
        if has_b:
            pt_b = xform.transform(QgsPointXY(lon_b, lat_b))
            self._site_b_rb.setToGeometry(QgsGeometry.fromPointXY(pt_b), None)
            self._site_b_rb.show()
        else:
            self._site_b_rb.reset(QgsWkbTypes.PointGeometry)

        # Link line between the two sites
        if has_a and has_b:
            pt_a = xform.transform(QgsPointXY(lon_a, lat_a))
            pt_b = xform.transform(QgsPointXY(lon_b, lat_b))
            self._link_line_rb.setToGeometry(
                QgsGeometry.fromPolylineXY([pt_a, pt_b]), None
            )
            self._link_line_rb.show()
        else:
            self._link_line_rb.reset(QgsWkbTypes.LineGeometry)

    def _reset_rubber_bands(self) -> None:
        """Clear all rubber bands from the map canvas."""
        self._site_a_rb.reset(QgsWkbTypes.PointGeometry)
        self._site_b_rb.reset(QgsWkbTypes.PointGeometry)
        self._link_line_rb.reset(QgsWkbTypes.LineGeometry)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        main_layout = QVBoxLayout(self)

        # ---- Site A / Transmitter ----
        self._lbl_site_a = QLabel("Site A:")
        self._lbl_site_a.setStyleSheet("font-weight: bold;")
        main_layout.addWidget(self._lbl_site_a)

        site_a_form = QFormLayout()

        # Location row
        a_loc_layout = QHBoxLayout()
        self.spin_a_lat = QDoubleSpinBox()
        self.spin_a_lat.setRange(-90.0, 90.0)
        self.spin_a_lat.setDecimals(6)
        self.spin_a_lat.setPrefix("Lat ")
        self.spin_a_lat.setMinimumWidth(130)
        a_loc_layout.addWidget(self.spin_a_lat)

        self.spin_a_lon = QDoubleSpinBox()
        self.spin_a_lon.setRange(-180.0, 180.0)
        self.spin_a_lon.setDecimals(6)
        self.spin_a_lon.setPrefix("Lon ")
        self.spin_a_lon.setMinimumWidth(130)
        a_loc_layout.addWidget(self.spin_a_lon)

        self.btn_pick_a = QPushButton("Pick from Map")
        a_loc_layout.addWidget(self.btn_pick_a)
        site_a_form.addRow("Location:", a_loc_layout)

        # Height row
        a_height_layout = QHBoxLayout()
        self.spin_a_height = QDoubleSpinBox()
        self.spin_a_height.setRange(0.0, 10000.0)
        self.spin_a_height.setValue(30.0)
        self.spin_a_height.setSuffix(" m")
        a_height_layout.addWidget(self.spin_a_height)

        a_height_layout.addWidget(QLabel("Mode:"))
        self.combo_a_mode = QComboBox()
        self.combo_a_mode.addItems(["AGL", "AMSL"])
        a_height_layout.addWidget(self.combo_a_mode)
        a_height_layout.addStretch()
        site_a_form.addRow("Height:", a_height_layout)

        main_layout.addLayout(site_a_form)

        # ---- Site B / Receiver ----
        self._lbl_site_b = QLabel("Site B:")
        self._lbl_site_b.setStyleSheet("font-weight: bold;")
        main_layout.addWidget(self._lbl_site_b)

        site_b_form = QFormLayout()

        # Location row
        b_loc_layout = QHBoxLayout()
        self.spin_b_lat = QDoubleSpinBox()
        self.spin_b_lat.setRange(-90.0, 90.0)
        self.spin_b_lat.setDecimals(6)
        self.spin_b_lat.setPrefix("Lat ")
        self.spin_b_lat.setMinimumWidth(130)
        b_loc_layout.addWidget(self.spin_b_lat)

        self.spin_b_lon = QDoubleSpinBox()
        self.spin_b_lon.setRange(-180.0, 180.0)
        self.spin_b_lon.setDecimals(6)
        self.spin_b_lon.setPrefix("Lon ")
        self.spin_b_lon.setMinimumWidth(130)
        b_loc_layout.addWidget(self.spin_b_lon)

        self.btn_pick_b = QPushButton("Pick from Map")
        b_loc_layout.addWidget(self.btn_pick_b)
        site_b_form.addRow("Location:", b_loc_layout)

        # Height row
        b_height_layout = QHBoxLayout()
        self.spin_b_height = QDoubleSpinBox()
        self.spin_b_height.setRange(0.0, 10000.0)
        self.spin_b_height.setValue(1.5)
        self.spin_b_height.setSuffix(" m")
        b_height_layout.addWidget(self.spin_b_height)

        b_height_layout.addWidget(QLabel("Mode:"))
        self.combo_b_mode = QComboBox()
        self.combo_b_mode.addItems(["AGL", "AMSL"])
        b_height_layout.addWidget(self.combo_b_mode)
        b_height_layout.addStretch()
        site_b_form.addRow("Height:", b_height_layout)

        main_layout.addLayout(site_b_form)

        # ---- Parameters group (frequency / ERP, visible in LOSS mode only) ----
        main_layout.addWidget(self._build_params_group())

        # ---- Analysis settings (DEM, resolution, backend, output dir) ----
        main_layout.addWidget(self._build_analysis_group())

        # ---- Results area ----
        results_group = QGroupBox("Results")
        results_layout = QVBoxLayout(results_group)
        self.result_area = QTextEdit()
        self.result_area.setReadOnly(True)
        results_layout.addWidget(self.result_area)
        main_layout.addWidget(results_group)

        # ---- Progress bar + Run / View Plots buttons ----
        bottom_layout = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        bottom_layout.addWidget(self.progress_bar, stretch=1)

        self.btn_view_plots = QPushButton("View Plots")
        self.btn_view_plots.setVisible(False)
        bottom_layout.addWidget(self.btn_view_plots)

        self.btn_stop = QPushButton("Stop")
        self.btn_stop.setVisible(False)
        bottom_layout.addWidget(self.btn_stop)
        self.btn_run = QPushButton("Run Analysis")
        bottom_layout.addWidget(self.btn_run)
        main_layout.addLayout(bottom_layout)

    # -- Parameters group (frequency / ERP, visible in LOSS mode only) -----

    def _build_params_group(self) -> QGroupBox:
        self._params_group = QGroupBox("Parameters")
        layout = QFormLayout(self._params_group)

        self.spin_freq = QDoubleSpinBox()
        self.spin_freq.setRange(1.0, 3000.0)
        self.spin_freq.setValue(433.0)
        self.spin_freq.setSuffix(" MHz")
        layout.addRow("Frequency (MHz):", self.spin_freq)

        self.spin_erp = QDoubleSpinBox()
        self.spin_erp.setRange(0.001, 1000000.0)
        self.spin_erp.setValue(10.0)
        self.spin_erp.setDecimals(3)
        self.spin_erp.setSuffix(" W")
        layout.addRow("ERP (Watts):", self.spin_erp)

        # Hidden by default (LOS mode).
        self._params_group.setVisible(False)

        return self._params_group

    # -- Analysis group (DEM, resolution, backend, output dir) -------------

    def _build_analysis_group(self) -> QGroupBox:
        group = QGroupBox("Analysis")
        layout = QFormLayout(group)

        # DEM Layer + Resolution on one row
        dem_row = QHBoxLayout()
        self.combo_dem = QComboBox()
        dem_row.addWidget(self.combo_dem, stretch=1)
        dem_row.addWidget(QLabel("Resolution:"))
        self.combo_resolution = QComboBox()
        self.combo_resolution.addItems(["2", "5", "10", "30"])
        self.combo_resolution.setCurrentIndex(2)  # default "10"
        dem_row.addWidget(self.combo_resolution)
        layout.addRow("DEM Layer:", dem_row)

        # Backend + Output Dir on one row
        backend_row = QHBoxLayout()
        self.combo_backend = QComboBox()
        self.combo_backend.addItems(["AUTO", "GPU", "CPU"])
        backend_row.addWidget(self.combo_backend)
        backend_row.addWidget(QLabel("Output Dir:"))
        self.line_output_dir = QLineEdit()
        self.line_output_dir.setText(
            os.path.join(tempfile.gettempdir(), "aether_output")
        )
        backend_row.addWidget(self.line_output_dir, stretch=1)
        self.btn_browse_dir = QPushButton("Browse")
        backend_row.addWidget(self.btn_browse_dir)
        layout.addRow("Backend:", backend_row)

        # Earth Radius Mode — applies to LOS and ITM alike (horizon geometry).
        self.combo_earth_radius = QComboBox()
        self.combo_earth_radius.addItems(["FOUR_THIRDS", "ADVANCED"])
        self.combo_earth_radius.setToolTip(
            "Earth-curvature model for horizon/diffraction geometry.\n"
            "FOUR_THIRDS: standard 4/3-earth refraction (normal, recommended).\n"
            "ADVANCED: derive effective radius from surface refractivity."
        )
        layout.addRow("Earth Radius Mode:", self.combo_earth_radius)

        return group

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        self.btn_run.clicked.connect(self._on_run)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_view_plots.clicked.connect(self._on_view_plots)

        # Map pick buttons
        self.btn_pick_a.clicked.connect(lambda: self._on_pick_from_map("a"))
        self.btn_pick_b.clicked.connect(lambda: self._on_pick_from_map("b"))

        # Browse button
        self.btn_browse_dir.clicked.connect(self._on_browse_dir)

        # Update rubber bands when coordinates change
        self.spin_a_lat.valueChanged.connect(self._update_visuals)
        self.spin_a_lon.valueChanged.connect(self._update_visuals)
        self.spin_b_lat.valueChanged.connect(self._update_visuals)
        self.spin_b_lon.valueChanged.connect(self._update_visuals)

    # ------------------------------------------------------------------
    # Populate raster layers
    # ------------------------------------------------------------------

    def _populate_raster_layers(self) -> None:
        """Fill the DEM combo with raster layers from the current project.

        Preserves the current selection across refills so a live update (a
        layer added or removed) does not reset the user's choice.
        """
        idx = self.combo_dem.currentIndex()
        prev_id = None
        if getattr(self, "_dem_layers", None) and 0 <= idx < len(self._dem_layers):
            try:
                prev_id = self._dem_layers[idx].id()
            except Exception:
                prev_id = None

        self.combo_dem.blockSignals(True)
        self.combo_dem.clear()
        self._dem_layers: list[QgsRasterLayer] = []
        for layer in QgsProject.instance().mapLayers().values():
            # Skip our own result rasters (coverage/P2P); keep terrain we made.
            if isinstance(layer, QgsRasterLayer) and not hide_from_dem_picker(layer):
                self.combo_dem.addItem(layer.name())
                self._dem_layers.append(layer)

        if prev_id is not None:
            for i, layer in enumerate(self._dem_layers):
                try:
                    same = layer.id() == prev_id
                except Exception:
                    same = False
                if same:
                    self.combo_dem.setCurrentIndex(i)
                    break
        self.combo_dem.blockSignals(False)

    def _on_project_layers_changed(self, *args) -> None:
        """Repopulate the DEM combo when project layers are added/removed."""
        self._populate_raster_layers()

    def _selected_dem_layer(self) -> Optional[QgsRasterLayer]:
        """Return the currently selected DEM layer, or None."""
        idx = self.combo_dem.currentIndex()
        if 0 <= idx < len(self._dem_layers):
            return self._dem_layers[idx]
        return None

    # ------------------------------------------------------------------
    # Slot: Pick from Map
    # ------------------------------------------------------------------

    def _on_pick_from_map(self, target: str) -> None:
        """Activate map point capture for site A or site B.

        Parameters
        ----------
        target:
            ``"a"`` for Site A / Transmitter,
            ``"b"`` for Site B / Receiver.
        """
        if target == "a":
            callback = self._on_point_picked_a
        else:
            callback = self._on_point_picked_b

        # activate_point_capture returns the tool; we must keep a reference.
        self._pick_tool = activate_point_capture(self.iface, callback)

        # Minimize the parent dialog so the user can see the map.
        parent = self._parent_dialog
        if hasattr(parent, "showMinimized"):
            parent.showMinimized()

    def _on_point_picked_a(self, lat: float, lon: float) -> None:
        self.spin_a_lat.setValue(lat)
        self.spin_a_lon.setValue(lon)
        self._restore_after_pick()

    def _on_point_picked_b(self, lat: float, lon: float) -> None:
        self.spin_b_lat.setValue(lat)
        self.spin_b_lon.setValue(lon)
        self._restore_after_pick()

    def _restore_after_pick(self) -> None:
        """Restore the parent dialog after a map pick."""
        parent = self._parent_dialog
        if hasattr(parent, "showNormal"):
            parent.showNormal()
        if hasattr(parent, "activateWindow"):
            parent.activateWindow()

    # ------------------------------------------------------------------
    # Slot: Browse output directory
    # ------------------------------------------------------------------

    def _on_browse_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select Output Directory", self.line_output_dir.text()
        )
        if path:
            self.line_output_dir.setText(path)

    # ------------------------------------------------------------------
    # Slot: View Plots
    # ------------------------------------------------------------------

    def _on_view_plots(self) -> None:
        """Open the P2P result viewer dialog with matplotlib plots."""
        if not self._last_output_dir or not self._last_output_name:
            QMessageBox.information(
                self, "No Results", "Run an analysis first to generate plots."
            )
            return

        parent = self._parent_dialog
        analysis_mode = parent.get_mode() if hasattr(parent, "get_mode") else "LOS"

        if analysis_mode == "LOS":
            freq_mhz = 0.0
            tx_dbm = 0.0
        else:
            freq_mhz = self.spin_freq.value()
            erp_watts = self.spin_erp.value()
            # Convert ERP (Watts) to dBm: P_dBm = 10 * log10(P_mW)
            if erp_watts > 0:
                tx_dbm = 10.0 * math.log10(erp_watts * 1000.0)
            else:
                tx_dbm = 0.0

        try:
            viewer = _P2PResultViewer(
                output_dir=self._last_output_dir,
                output_name=self._last_output_name,
                freq_mhz=freq_mhz,
                tx_height=self.spin_a_height.value(),
                rx_height=self.spin_b_height.value(),
                tx_dbm=tx_dbm,
                parent=self,
            )
            viewer.show()
        except ImportError:
            QMessageBox.warning(
                self, "Missing Dependency",
                "matplotlib is required for P2P plots but is not installed.\n"
                "Install it with: pip install matplotlib"
            )
        except Exception as exc:
            QMessageBox.critical(
                self, "Plot Error", f"Failed to open plot viewer:\n{exc}"
            )

    # ------------------------------------------------------------------
    # Collect parameters from widgets
    # ------------------------------------------------------------------

    def _collect_params(self) -> P2PParams:
        """Read all widget values into a P2PParams dataclass."""
        parent = self._parent_dialog
        analysis_mode = parent.get_mode() if hasattr(parent, "get_mode") else "LOS"

        if analysis_mode == "LOS":
            model = "LOS"
            freq = 0.0
            erp = 0.0
        else:
            model = parent.get_loss_model() if hasattr(parent, "get_loss_model") else "SIMPLE_LOSS"
            freq = self.spin_freq.value()
            erp = self.spin_erp.value()

        return P2PParams(
            # Site A / Transmitter
            tx_lat=self.spin_a_lat.value(),
            tx_lon=self.spin_a_lon.value(),
            tx_height=self.spin_a_height.value(),
            tx_mode=self.combo_a_mode.currentText(),
            freq_mhz=freq,
            erp_watts=erp,
            # Site B / Receiver
            rx_height=self.spin_b_height.value(),
            rx_mode=self.combo_b_mode.currentText(),
            # Analysis
            model=model,
            resolution_m=int(self.combo_resolution.currentText()),
            backend=self.combo_backend.currentText(),
            earth_radius=self.combo_earth_radius.currentText(),
            # Output
            output_name=f"p2p_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate_inputs(self) -> Optional[str]:
        """Validate user inputs before running analysis.

        Returns an error message string, or None if everything is valid.
        """
        if self._selected_dem_layer() is None:
            return "Please select a DEM raster layer."

        output_dir = self.line_output_dir.text().strip()
        if not output_dir:
            return "Please specify an output directory."

        a_lat = self.spin_a_lat.value()
        a_lon = self.spin_a_lon.value()
        b_lat = self.spin_b_lat.value()
        b_lon = self.spin_b_lon.value()

        if a_lat == 0.0 and a_lon == 0.0:
            return "Please set the Site A / Transmitter location."
        if b_lat == 0.0 and b_lon == 0.0:
            return "Please set the Site B / Receiver location."

        return None

    # ------------------------------------------------------------------
    # Slot: Run Analysis
    # ------------------------------------------------------------------

    def _on_run(self) -> None:
        # Guard against double-click while already running.
        if self._worker is not None and self._worker.isRunning():
            return

        error = self._validate_inputs()
        if error:
            QMessageBox.warning(self, "Validation Error", error)
            return

        dem_layer = self._selected_dem_layer()

        # Guard against a basemap/imagery layer (e.g. OpenStreetMap) being
        # used as the terrain source — elevation data is required.
        warn = dem_layer_warning(dem_layer)
        if warn and QMessageBox.warning(
            self, "Not a DEM?", warn + "\n\nUse it anyway?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return

        output_dir = self.line_output_dir.text().strip()

        # Generate a temp 2-line batch CSV from the two endpoints.
        batch_file = _write_temp_batch_csv(
            tx_lat=self.spin_a_lat.value(),
            tx_lon=self.spin_a_lon.value(),
            tx_height=self.spin_a_height.value(),
            tx_mode=self.combo_a_mode.currentText(),
            rx_lat=self.spin_b_lat.value(),
            rx_lon=self.spin_b_lon.value(),
            rx_height=self.spin_b_height.value(),
            rx_mode=self.combo_b_mode.currentText(),
        )

        params = self._collect_params()

        # Switch to running state.
        self.btn_run.setEnabled(False)
        self.btn_stop.setVisible(True)
        self.btn_view_plots.setVisible(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.result_area.clear()

        # Store output name for later use by the plot viewer.
        self._last_output_name = params.output_name
        self._last_output_dir = None  # reset until success

        self._worker = _P2PWorker(
            params, dem_layer, output_dir, batch_file, self,
        )
        self._worker.progress.connect(self.progress_bar.setValue)
        self._worker.status.connect(self._on_status)
        self._worker.finished_ok.connect(self._on_finished_ok)
        self._worker.finished_err.connect(self._on_finished_err)
        self._worker.start()

    def _on_stop(self) -> None:
        if self._worker and self._worker.isRunning():
            self.result_area.append("Stopping...")
            self._worker.cancel()
            self._worker.wait(5000)
        self._reset_run_ui()

    def _reset_run_ui(self) -> None:
        self.btn_run.setEnabled(True)
        self.btn_stop.setVisible(False)
        self.progress_bar.setVisible(False)

    def _on_status(self, msg: str) -> None:
        self.result_area.append(msg)
        QgsMessageLog.logMessage(msg, TAG, Qgis.MessageLevel.Info)

    def _on_finished_ok(self, output_dir: str) -> None:
        self.progress_bar.setValue(100)
        self._reset_run_ui()

        self._last_output_dir = output_dir
        self._display_results(output_dir)

        # Show the View Plots button if any .gp files exist.
        has_gp = any(
            os.path.isfile(os.path.join(output_dir, name))
            for name in ("terrain_profile.gp", "height_profile.gp", "path_profile.gp")
        )
        self.btn_view_plots.setVisible(has_gp)

    def _display_results(self, output_dir: str) -> None:
        """Parse the result CSV and report file, show key metrics."""
        output_name = self._last_output_name or "p2p"

        # --- Show CSV results ---
        csv_path = os.path.join(output_dir, output_name + ".csv")
        try:
            with open(csv_path, "r", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                rows = list(reader)

            if not rows:
                self.result_area.append("\nNo results found in output file.")
            else:
                lines = ["", "P2P Link Analysis Results", "=" * 35]
                for row in rows:
                    for key, value in row.items():
                        lines.append(f"  {key}: {value}")
                    lines.append("")
                self.result_area.append("\n".join(lines))
        except FileNotFoundError:
            self.result_area.append(f"\nResult CSV not found: {csv_path}")
        except Exception as exc:
            self.result_area.append(f"\nError reading CSV results: {exc}")

        # --- Show report file if present ---
        report_path = os.path.join(output_dir, "p2p_report.txt")
        if os.path.isfile(report_path):
            try:
                with open(report_path, "r", encoding="utf-8", errors="replace") as fh:
                    report_text = fh.read()
                if report_text.strip():
                    self.result_area.append("\n--- P2P Report ---")
                    self.result_area.append(report_text)
            except Exception as exc:
                self.result_area.append(f"\nError reading report: {exc}")

    def _on_finished_err(self, message: str) -> None:
        self._reset_run_ui()

        self.result_area.append(f"\nAnalysis failed: {message}")
        QMessageBox.critical(self, "Analysis Failed", message)
