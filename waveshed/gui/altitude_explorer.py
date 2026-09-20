"""Altitude Explorer — a dock panel for interactively viewing MIN_ALT results.

A single MIN_ALT run stores, per pixel, the *lowest* altitude (AGL) at which a
location gains line-of-sight to the transmitter.  Coverage at any chosen
altitude *A* is therefore just ``{pixels where required_altitude <= A}`` — a
pure re-classification of the existing raster, with **no recomputation**.

This dock exposes that as a live set of altitudes: pick as many as you like and
every selected MIN_ALT layer repaints as nested bands, each in its own colour,
so "reachable by 100 m" and "needs 200 m" are legible in one picture rather
than one slider position at a time.  It drives one *or several* layers at once
(e.g. multiple transmitter sites), so their reachable areas union on the canvas.

Altitudes can be read two ways.  **AGL** is what the solver emits — height above
the ground directly below, which is what a mast or a terrain-following drone
holds.  **AMSL** is the sea-level altitude a pilot actually flies; getting there
means adding the terrain under each pixel, so the first switch to it builds a
sea-level twin of each layer (``raster_tools.build_amsl_raster``) using the very
terrain the run was computed over.  Twins are cached on disk and re-used, so the
switch is instant after the first time.

The heavy lifting (renderer construction, quantization, raster maths) lives in
``core.result_loader`` / ``core.min_alt`` / ``core.raster_tools``; this module
is only the UI.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from qgis.core import Qgis, QgsMessageLog, QgsProject, QgsRasterLayer
from qgis.gui import QgsColorButton, QgsDockWidget, QgsMapLayerComboBox
from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal
from qgis.PyQt.QtGui import QColor, QIcon, QPixmap
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QRadioButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.layer_utils import (
    altitude_reference,
    derived_from,
    hide_from_dem_picker,
    is_min_alt_layer,
    terrain_dir as layer_terrain_dir,
)
from ..core.min_alt import (
    DEFAULT_RAMP_MAX_M,
    REF_AGL,
    REF_AMSL,
    AltitudeBand,
    band_color,
    next_band_color,
    nice_ceiling,
    reference_phrase,
    reference_suffix,
)
from ..core.result_loader import (
    GROUP_AMSL,
    GROUP_CONTOURS,
    GROUP_MERGE,
    add_layer_to_project,
    build_band_renderer,
    build_min_alt_renderer,
    estimate_altitude_range_m,
    load_amsl_result,
    load_best_alt_result,
    load_best_site_result,
    load_contour_result,
)

TAG = "Waveshed"

_LAYER_ID_ROLE = Qt.ItemDataRole.UserRole

#: More bands than this stop being readable — the legend turns into a wall and
#: neighbouring colours stop being tellable apart on the map.
MAX_BANDS = 8

#: Swatch size for the band list icons.
_SWATCH_PX = 12

#: Share of a result's reachable area that may fall outside the elevation data
#: before the sea-level view says so. A run's own terrain is cut to the analysis
#: sector with a small margin, so a sliver at the edge is normal and reporting
#: it every time would train the user to dismiss the warning unread.
_MAX_QUIET_TERRAIN_GAP = 0.02


def _is_candidate(layer) -> bool:
    """True if *layer* is a MIN_ALT surface the explorer can drive.

    Accepts layers the plugin stamped as MIN_ALT (the common case) and, as a
    convenience, any single-band UInt16 raster — that is what a manually loaded
    ``min_alt`` GeoTIFF looks like before it has been stamped.
    """
    if not isinstance(layer, QgsRasterLayer) or not layer.isValid():
        return False
    if is_min_alt_layer(layer):
        return True
    try:
        provider = layer.dataProvider()
        return (
            provider.bandCount() == 1
            and provider.dataType(1) == _uint16_data_type()
        )
    except Exception:
        return False


def _uint16_data_type():
    """The UInt16 raster data type, wherever this QGIS keeps it.

    ``Qgis.DataType`` only arrived in QGIS 3.30; on the plugin's declared
    minimum of 3.28 the constant lives on ``QgsRaster``.  Reaching for the new
    spelling alone does not raise here — the caller swallows it — it just makes
    the "unstamped UInt16 raster" convenience quietly never match.
    """
    data_type = getattr(Qgis, "DataType", None)
    if data_type is not None and hasattr(data_type, "UInt16"):
        return data_type.UInt16
    if hasattr(Qgis, "UInt16"):
        return Qgis.DataType.UInt16
    from qgis.core import QgsRaster
    return QgsRaster.UInt16


def _is_site_layer(layer) -> bool:
    """True if *layer* belongs in the dock's layer list.

    Sea-level twins are MIN_ALT rasters too, but they are a *view* of a layer
    already in the list — showing both would make the same site appear twice and
    let the user tick a pair that draws on top of itself.
    """
    return _is_candidate(layer) and not derived_from(layer)


class _Band:
    """One altitude the user asked to see, and how to draw it.

    Mutable on purpose: the slider edits the selected band in place while it is
    being dragged, and identity (not list position) is what the editor strip
    tracks, so removing a band cannot make the strip edit a different one.
    """

    __slots__ = ("altitude_m", "color", "visible")

    def __init__(self, altitude_m: float, color: QColor, visible: bool = True):
        self.altitude_m = float(altitude_m)
        self.color = QColor(color)
        self.visible = visible

    def as_altitude_band(self) -> AltitudeBand:
        """The GUI-free view of this band, for the renderer."""
        return AltitudeBand(
            self.altitude_m,
            (self.color.red(), self.color.green(), self.color.blue()),
            self.visible,
        )


def _swatch(color: QColor, visible: bool) -> QIcon:
    """A colour chip for the band list — hollow when the band is hidden."""
    pixmap = QPixmap(_SWATCH_PX, _SWATCH_PX)
    pixmap.fill(color if visible else QColor(0, 0, 0, 0))
    return QIcon(pixmap)


class _TerrainSourceDialog(QDialog):
    """Ask for an elevation source when a layer's own terrain cannot be found.

    Normally the run's ``.abt`` cache is recorded beside its GeoTIFF and used
    without asking (see ``job_builder.read_job_terrain_dir``).  This is the
    fallback for the cases where it is not: a best-site merge, or a result
    copied away from the job config that made it.
    """

    def __init__(self, parent, layer_names: List[str]):
        super().__init__(parent)
        self.setWindowTitle("Elevation data needed")
        layout = QVBoxLayout(self)

        listed = "\n".join(f"  • {name}" for name in layer_names[:5])
        if len(layer_names) > 5:
            listed += f"\n  • …and {len(layer_names) - 5} more"
        intro = QLabel(
            "Sea-level altitudes are the required height above ground plus the "
            "ground itself, so this needs elevation data for:\n"
            f"{listed}\n\n"
            "Waveshed could not find the terrain these results were computed "
            "over. Choose a DEM covering the same area — ideally the one the "
            "analysis used, so the altitudes match."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        layout.addWidget(QLabel("DEM layer in this project:"))
        self.combo = QgsMapLayerComboBox()
        try:  # QGIS 3.42+: Qgis.LayerFilter flags.
            self.combo.setFilters(Qgis.LayerFilter.RasterLayer)
        except (AttributeError, TypeError):
            from qgis.core import QgsMapLayerProxyModel
            self.combo.setFilters(QgsMapLayerProxyModel.Filter.RasterLayer)
        self.combo.setAllowEmptyLayer(True)
        # Our own coverage results are never terrain; offering them invites a
        # nonsense answer that would look plausible on the map.
        self.combo.setExceptedLayerList([
            lyr for lyr in QgsProject.instance().mapLayers().values()
            if hide_from_dem_picker(lyr)
        ])
        layout.addWidget(self.combo)

        layout.addWidget(QLabel("…or a DEM file on disk:"))
        file_row = QHBoxLayout()
        self.edit_path = QLineEdit()
        self.edit_path.setPlaceholderText("GeoTIFF, VRT, DEM, HGT…")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._on_browse)
        file_row.addWidget(self.edit_path, 1)
        file_row.addWidget(browse)
        layout.addLayout(file_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a DEM", self.edit_path.text() or "",
            "Elevation rasters (*.tif *.tiff *.vrt *.dem *.hgt);;All files (*)",
        )
        if path:
            self.edit_path.setText(path)

    def terrain_path(self) -> str:
        """The chosen elevation source as a path GDAL can open, or "".

        A typed path wins over the combo: it is the more deliberate answer, and
        the combo always has *something* selected.
        """
        typed = self.edit_path.text().strip()
        if typed:
            return typed
        layer = self.combo.currentLayer()
        if layer is None:
            return ""
        # Strip any provider suffix ("|band=1"); GDAL wants the bare path.
        source = (layer.source() or "").split("|")[0]
        return source if os.path.exists(source) else ""

    def accept(self) -> None:
        """Refuse to close on a choice that cannot be read.

        Without this, picking an XYZ elevation layer (Mapzen and friends are
        perfectly good terrain, but they are tiles behind a URL, not a file)
        would close the dialog and report "no elevation data chosen" — which
        looks like the dialog ignored the answer.
        """
        if self.terrain_path():
            super().accept()
            return
        layer = self.combo.currentLayer()
        if layer is not None:
            QMessageBox.warning(
                self, "Elevation data",
                f"'{layer.name()}' is not a file on disk, so it cannot be read "
                "directly. Tiled or remote elevation layers need to be saved "
                "as a GeoTIFF first — or pick a DEM file below.",
            )
        else:
            QMessageBox.warning(
                self, "Elevation data",
                "Choose a DEM layer or a DEM file to continue, or cancel to "
                "keep the altitudes above ground.",
            )


class _AmslWorker(QThread):
    """Build sea-level twins off the GUI thread.

    Two heavy steps per layer, both pure GDAL/numpy: mosaic the run's ``.abt``
    terrain tiles into a DEM (once per terrain cache — the result is kept and
    re-used), then add that DEM to the MIN_ALT surface.  QGIS layer objects are
    constructed by the main thread from :attr:`layer_done`.
    """

    #: (one-based index, layer name) — this layer's build is starting.
    started_layer = pyqtSignal(int, str)
    #: (one-based index, 0.0–1.0) — progress within the layer at that index.
    layer_progress = pyqtSignal(int, float)
    #: (out_tif, source layer id, source layer name, terrain source, share of
    #: reachable pixels the terrain did not cover) — this layer is done.
    layer_done = pyqtSignal(str, str, str, str, float)
    #: (layer name, terrain source, error) — failed; the batch carries on.
    layer_failed = pyqtSignal(str, str, str)
    #: (built, skipped) — the batch finished or was cancelled.
    finished_all = pyqtSignal(int, int)

    def __init__(self, inputs, cache_dir, parent=None):
        """*inputs* is ``[(src_tif, out_tif, layer_id, name, terrain), ...]``
        where *terrain* is an ``.abt`` directory or a DEM file."""
        super().__init__(parent)
        self._inputs = list(inputs)
        self._cache_dir = cache_dir
        self._canceled = False

    def cancel(self) -> None:
        self._canceled = True

    def _terrain_raster(self, terrain: str) -> str:
        """Return a GDAL-readable DEM for *terrain*, mosaicking tiles if needed.

        The ``.abt`` directory name is already a hash of (source, area,
        resolution), so it doubles as the cache key for the mosaic — two runs
        over the same terrain build it once.
        """
        from ..core import abt

        if os.path.isfile(terrain):
            return terrain
        if not os.path.isdir(terrain):
            raise RuntimeError(f"Terrain source not found: {terrain}")

        out_tif = os.path.join(
            self._cache_dir,
            f"terrain_{os.path.basename(os.path.normpath(terrain))}.tif",
        )
        if not os.path.isfile(out_tif):
            abt.mosaic_to_geotiff(
                terrain, out_tif, should_cancel=lambda: self._canceled,
            )
        return out_tif

    def _is_cached(self, out: str, src: str, terrain: str) -> bool:
        """True if the twin at *out* can be re-used as-is.

        Both halves matter. A twin older than its source was built from a
        surface that has since been recomputed; a twin built over *different*
        elevation data holds different altitudes, and it is newer than the
        source either way — so the timestamp alone would keep serving the twin
        the user just corrected the DEM to get rid of.
        """
        from ..core import raster_tools

        if not os.path.isfile(out):
            return False
        try:
            if os.path.getmtime(out) < os.path.getmtime(src):
                return False
        except OSError:
            return False
        return raster_tools.amsl_terrain_of(out) == terrain

    def run(self) -> None:
        from ..core import abt, raster_tools

        built = 0
        skipped = 0
        for i, (src, out, layer_id, name, terrain) in enumerate(self._inputs):
            if self._canceled:
                break
            self.started_layer.emit(i + 1, name)
            try:
                if self._is_cached(out, src, terrain):
                    skipped += 1
                    self.layer_done.emit(out, layer_id, name, terrain, 0.0)
                    continue
                dem = self._terrain_raster(terrain)
                result = raster_tools.build_amsl_raster(
                    src, dem, out,
                    should_cancel=lambda: self._canceled,
                    on_progress=lambda frac, at=i + 1: (
                        self.layer_progress.emit(at, frac)
                    ),
                    terrain_tag=terrain,
                )
            except (raster_tools.AmslCanceled, abt.MosaicCanceled):
                break
            except Exception as exc:  # noqa: BLE001 — surfaced to the user
                self.layer_failed.emit(name, terrain, str(exc))
            else:
                built += 1
                self.layer_done.emit(
                    out, layer_id, name, terrain, result.missing_fraction,
                )
        self.finished_all.emit(built, skipped)


class _ContourWorker(QThread):
    """Generate iso-altitude contours off the GUI thread.

    ``gdal.ContourGenerateEx`` is a single, uninterruptible C call that can take
    a while on a large coverage raster; running it on the main thread froze
    QGIS.  This worker runs the (pure GDAL/OGR, no QGIS-object) generation for
    each input sequentially so the UI stays responsive and the user can cancel
    between layers.  The resulting GeoPackages are loaded into QGIS by the main
    thread (QGIS layer construction is main-thread-only).
    """

    #: (one-based index, layer name) — a layer's contour generation is starting.
    started_layer = pyqtSignal(int, str)
    #: (one-based index, 0.0–1.0) — progress tracing the layer at that index.
    layer_progress = pyqtSignal(int, float)
    #: (out_gpkg, display name, levels_m, decimated) — a layer finished; load it
    #: on the main thread.
    layer_done = pyqtSignal(str, str, list, bool)
    #: (layer name, error message) — a layer failed.
    layer_failed = pyqtSignal(str, str)
    #: (layers_made,) — the whole batch finished (or was cancelled).
    finished_all = pyqtSignal(int)

    def __init__(self, inputs, interval_m, out_dir, stamp, parent=None):
        super().__init__(parent)
        self._inputs = list(inputs)
        self._interval_m = interval_m
        self._out_dir = out_dir
        self._stamp = stamp
        self._canceled = False

    def cancel(self) -> None:
        self._canceled = True

    def run(self) -> None:
        # Imported here so the module still imports without GDAL (tests stub it).
        from ..core import raster_tools

        made = 0
        for i, (path, name) in enumerate(self._inputs):
            if self._canceled:
                break
            self.started_layer.emit(i + 1, name)
            out_gpkg = os.path.join(
                self._out_dir, f"contours_{i + 1}_{self._stamp}.gpkg",
            )
            try:
                result = raster_tools.generate_contours(
                    path, out_gpkg, self._interval_m,
                    should_cancel=lambda: self._canceled,
                    on_progress=lambda frac, at=i + 1: (
                        self.layer_progress.emit(at, frac)
                    ),
                )
            except raster_tools.ContourCanceled:
                break  # Cancel now lands mid-raster, not only between layers.
            except Exception as exc:  # noqa: BLE001 — surfaced to the user
                self.layer_failed.emit(name, str(exc))
            else:
                self.layer_done.emit(
                    out_gpkg, name, list(result.levels_m), result.decimated,
                )
                made += 1
        self.finished_all.emit(made)


class _MergeWorker(QThread):
    """Compute the best-site merge off the GUI thread.

    ``raster_tools.merge_best_site`` warps and reduces every input raster — a
    heavy GDAL/numpy pass that froze QGIS when run on the main thread.  This
    worker runs it in the background; ``cancel()`` stops it between inputs.
    """

    #: (labels, out_alt, out_site) — merge succeeded; load the results.
    finished_ok = pyqtSignal(list, str, str)
    #: (error message,) — merge failed.
    failed = pyqtSignal(str)
    #: () — the user cancelled before completion.
    canceled = pyqtSignal()

    def __init__(self, inputs, out_alt, out_site, parent=None):
        super().__init__(parent)
        self._inputs = list(inputs)
        self._out_alt = out_alt
        self._out_site = out_site
        self._canceled = False

    def cancel(self) -> None:
        self._canceled = True

    def run(self) -> None:
        from ..core import raster_tools

        try:
            labels = raster_tools.merge_best_site(
                self._inputs, self._out_alt, self._out_site,
                should_cancel=lambda: self._canceled,
            )
        except raster_tools.MergeCanceled:
            self.canceled.emit()
        except Exception as exc:  # noqa: BLE001 — surfaced to the user
            self.failed.emit(str(exc))
        else:
            self.finished_ok.emit(labels, self._out_alt, self._out_site)


class AltitudeExplorerDock(QgsDockWidget):
    """Dock widget driving MIN_ALT layers from a set of chosen altitudes."""

    def __init__(self, iface, parent: Optional[QWidget] = None) -> None:
        super().__init__("Waveshed Altitude Explorer", parent)
        self.setObjectName("WaveshedAltitudeExplorer")
        self.iface = iface
        self._updating = False  # guard against slider<->spin feedback loops

        # The altitudes on show, per reference. Kept apart because the numbers
        # are not interchangeable: 100 m AGL is a sensible drone height, while
        # 100 m AMSL is underground for most of the Alps.
        self._band_sets: Dict[str, List[_Band]] = {REF_AGL: [], REF_AMSL: []}
        self._reference = REF_AGL
        self._selected_band: Optional[_Band] = None
        #: Tidy (lowest, highest) altitude the sliders span, for the current
        #: reference and selection.
        self._range: Tuple[float, float] = (0.0, DEFAULT_RAMP_MAX_M)

        # Background sea-level (AMSL) twin building state.
        self._amsl_worker: Optional[_AmslWorker] = None
        self._amsl_progress: Optional[QProgressDialog] = None
        self._amsl_errors: List[str] = []
        self._amsl_gaps: List[Tuple[str, float]] = []
        self._amsl_total = 0
        self._amsl_ticking = False
        #: A DEM the user picked when a run's own terrain could not be found;
        #: kept for the session so the question is asked once, not per layer.
        self._fallback_terrain = ""
        #: Terrain sources that failed, so the next attempt falls back instead
        #: of walking into the same error again.
        self._bad_terrain: set = set()

        # Background iso-altitude contour generation state.
        self._contour_worker: Optional[_ContourWorker] = None
        self._contour_progress: Optional[QProgressDialog] = None
        self._contour_errors: List[str] = []
        self._contour_decimated: List[str] = []
        self._contour_made = 0
        self._contour_total = 0
        self._contour_interval_m = 0
        self._contour_canceled = False
        self._contour_ticking = False

        # Background best-site merge state.
        self._merge_worker: Optional[_MergeWorker] = None
        self._merge_progress: Optional[QProgressDialog] = None

        self._build_ui()
        self._wire_signals()
        self.refresh_layers()

        # Keep the layer list in sync with the project.
        proj = QgsProject.instance()
        proj.layersAdded.connect(self._on_project_changed)
        proj.layersRemoved.connect(self._on_project_changed)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 8)

        intro = QLabel(
            "Pick the altitudes you care about — every selected Minimum LOS "
            "Altitude layer shows where it reaches the transmitter at each of "
            "them, one colour per altitude. No recomputation needed."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(intro)

        # ---- Layer selection ----
        layer_group = QGroupBox("Layers")
        lg_layout = QVBoxLayout(layer_group)
        self.layer_list = QListWidget()
        self.layer_list.setMaximumHeight(120)
        lg_layout.addWidget(self.layer_list)
        row = QHBoxLayout()
        self.btn_all = QPushButton("Select all")
        self.btn_none = QPushButton("Select none")
        self.btn_refresh = QPushButton("Refresh")
        row.addWidget(self.btn_all)
        row.addWidget(self.btn_none)
        row.addStretch()
        row.addWidget(self.btn_refresh)
        lg_layout.addLayout(row)
        layout.addWidget(layer_group)

        # ---- Altitude reference ----
        ref_row = QHBoxLayout()
        ref_row.addWidget(QLabel("Measured from:"))
        self.radio_agl = QRadioButton("Ground (AGL)")
        self.radio_amsl = QRadioButton("Sea level (AMSL)")
        self.radio_agl.setChecked(True)
        self.radio_agl.setToolTip(
            "Height above the ground directly below — what a mast or a "
            "terrain-following drone holds."
        )
        self.radio_amsl.setToolTip(
            "Altitude above sea level — what a pilot flies. Waveshed adds the "
            "terrain the analysis was run over to each layer the first time you "
            "switch; after that it is instant."
        )
        self._ref_group = QButtonGroup(self)
        self._ref_group.addButton(self.radio_agl)
        self._ref_group.addButton(self.radio_amsl)
        ref_row.addWidget(self.radio_agl)
        ref_row.addWidget(self.radio_amsl)
        ref_row.addStretch()
        layout.addLayout(ref_row)

        # ---- Altitude bands ----
        alt_group = QGroupBox("Altitudes")
        ag_layout = QVBoxLayout(alt_group)

        self.chk_threshold = QCheckBox("Limit to these altitudes")
        self.chk_threshold.setChecked(True)
        self.chk_threshold.setToolTip(
            "Off: show the whole layer with its continuous colour ramp, "
            "ignoring the altitudes below."
        )
        ag_layout.addWidget(self.chk_threshold)

        self.band_list = QListWidget()
        self.band_list.setMaximumHeight(110)
        self.band_list.setToolTip(
            "Untick an altitude to hide its band without losing it — the bands "
            "above keep their own areas."
        )
        ag_layout.addWidget(self.band_list)

        band_row = QHBoxLayout()
        self.btn_add_band = QPushButton("Add altitude")
        self.btn_remove_band = QPushButton("Remove")
        self.btn_band_color = QgsColorButton()
        self.btn_band_color.setAllowOpacity(False)
        self.btn_band_color.setToolTip("Colour for the selected altitude.")
        self.btn_band_color.setMaximumWidth(48)
        band_row.addWidget(self.btn_add_band)
        band_row.addWidget(self.btn_remove_band)
        band_row.addStretch()
        band_row.addWidget(QLabel("Colour:"))
        band_row.addWidget(self.btn_band_color)
        ag_layout.addLayout(band_row)

        slider_row = QHBoxLayout()
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setMaximum(int(DEFAULT_RAMP_MAX_M))
        self.spin = QSpinBox()
        self.spin.setRange(0, int(DEFAULT_RAMP_MAX_M))
        self.spin.setSuffix(reference_suffix(REF_AGL))
        self.spin.setToolTip("Altitude of the band selected above.")
        slider_row.addWidget(self.slider, 1)
        slider_row.addWidget(self.spin)
        ag_layout.addLayout(slider_row)

        # ---- Display style ----
        style_row = QHBoxLayout()
        style_row.addWidget(QLabel("Colour by:"))
        self.radio_bands = QRadioButton("Altitude band")
        self.radio_shade = QRadioButton("Required altitude")
        self.radio_bands.setChecked(True)
        self.radio_bands.setToolTip(
            "One colour per altitude above — the nested areas each altitude "
            "buys you."
        )
        self.radio_shade.setToolTip(
            "One continuous ramp up to the highest altitude, colouring pixels "
            "by the altitude they actually need (blue = low, red = high)."
        )
        self._style_group = QButtonGroup(self)
        self._style_group.addButton(self.radio_bands)
        self._style_group.addButton(self.radio_shade)
        style_row.addWidget(self.radio_bands)
        style_row.addWidget(self.radio_shade)
        style_row.addStretch()
        ag_layout.addLayout(style_row)

        self.lbl_readout = QLabel("")
        self.lbl_readout.setWordWrap(True)
        self.lbl_readout.setStyleSheet("font-weight: bold;")
        ag_layout.addWidget(self.lbl_readout)
        layout.addWidget(alt_group)

        # ---- Actions ----
        action_row = QHBoxLayout()
        action_row.addStretch()
        self.btn_reset = QPushButton("Reset to full range")
        self.btn_reset.setToolTip(
            "Restore the continuous Minimum LOS Altitude colour ramp on the "
            "selected layers."
        )
        action_row.addWidget(self.btn_reset)
        layout.addLayout(action_row)

        # ---- Tools (Options E and D) ----
        tools_group = QGroupBox("Tools")
        tg_layout = QVBoxLayout(tools_group)
        self.btn_merge = QPushButton("Merge selected → best site")
        self.btn_merge.setToolTip(
            "Across the selected sites, compute the lowest required altitude at "
            "each location and which site provides it. Produces a combined "
            "Minimum LOS Altitude layer (drivable by the slider) plus a best-site map."
        )
        self.btn_contours = QPushButton("Iso-altitude contours…")
        self.btn_contours.setToolTip(
            "Draw labelled contour lines of equal required altitude on each "
            "selected layer (e.g. the 50 m / 100 m 'reach' lines)."
        )
        tg_layout.addWidget(self.btn_merge)
        tg_layout.addWidget(self.btn_contours)
        layout.addWidget(tools_group)

        layout.addStretch()
        self.setWidget(root)

    def _wire_signals(self) -> None:
        self.slider.valueChanged.connect(self._on_slider)
        self.spin.valueChanged.connect(self._on_spin)
        self.chk_threshold.toggled.connect(self._on_threshold_toggled)
        self.radio_shade.toggled.connect(lambda _: self._apply())
        self.radio_amsl.toggled.connect(self._on_reference_toggled)
        self.btn_reset.clicked.connect(self._on_reset)
        self.btn_all.clicked.connect(lambda: self._set_all_checked(True))
        self.btn_none.clicked.connect(lambda: self._set_all_checked(False))
        self.btn_refresh.clicked.connect(self.refresh_layers)
        self.layer_list.itemChanged.connect(self._on_layer_checked)
        # Re-sort only once the drag ends — see _set_selected_altitude.
        self.slider.sliderReleased.connect(self._on_band_edited)
        self.spin.editingFinished.connect(self._on_band_edited)
        self.band_list.currentRowChanged.connect(self._on_band_selected)
        self.band_list.itemChanged.connect(self._on_band_toggled)
        self.btn_add_band.clicked.connect(self._on_add_band)
        self.btn_remove_band.clicked.connect(self._on_remove_band)
        self.btn_band_color.colorChanged.connect(self._on_band_color)
        self.btn_merge.clicked.connect(self._on_merge)
        self.btn_contours.clicked.connect(self._on_contours)

    # ------------------------------------------------------------------
    # Layer list
    # ------------------------------------------------------------------

    def refresh_layers(self) -> None:
        """Rebuild the layer checklist from the current project, preserving
        the checked state of layers that are still present."""
        previously_checked = set(self._checked_layer_ids())
        had_any = self.layer_list.count() > 0

        self.layer_list.blockSignals(True)
        self.layer_list.clear()
        candidates = [
            lyr for lyr in QgsProject.instance().mapLayers().values()
            if _is_site_layer(lyr)
        ]
        for layer in candidates:
            item = QListWidgetItem(layer.name())
            item.setData(_LAYER_ID_ROLE, layer.id())
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            # New layers default to checked (so a fresh run is driven at once);
            # keep prior choices on refresh.
            if not had_any or layer.id() in previously_checked:
                item.setCheckState(Qt.CheckState.Checked)
            else:
                item.setCheckState(Qt.CheckState.Unchecked)
            self.layer_list.addItem(item)
        self.layer_list.blockSignals(False)

        if not candidates:
            self.lbl_readout.setText(
                "No Minimum LOS Altitude layers found. "
                "Run a Minimum LOS Altitude analysis first."
            )
            self._sync_band_editor()  # nothing to edit — grey the strip out
            return

        self._sync_range_to_layers()
        if not self._bands:
            self._seed_bands()
        self._apply()

    def _checked_layers(self) -> List[QgsRasterLayer]:
        """The MIN_ALT source layers the user ticked, above-ground originals."""
        proj = QgsProject.instance()
        out: List[QgsRasterLayer] = []
        for i in range(self.layer_list.count()):
            item = self.layer_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                layer = proj.mapLayer(item.data(_LAYER_ID_ROLE))
                if isinstance(layer, QgsRasterLayer) and layer.isValid():
                    out.append(layer)
        return out

    def _checked_layer_ids(self) -> List[str]:
        return [lyr.id() for lyr in self._checked_layers()]

    def _set_all_checked(self, checked: bool) -> None:
        self.layer_list.blockSignals(True)
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.layer_list.count()):
            self.layer_list.item(i).setCheckState(state)
        self.layer_list.blockSignals(False)
        # Signals were blocked to react once rather than per row — but react the
        # same way, so a bulk tick above sea level still builds what it needs.
        self._on_layer_checked(None)

    # ------------------------------------------------------------------
    # Above-sea-level twins
    # ------------------------------------------------------------------

    def _twin_of(self, layer) -> Optional[QgsRasterLayer]:
        """The sea-level twin of *layer* already in the project, if any."""
        for candidate in QgsProject.instance().mapLayers().values():
            if (isinstance(candidate, QgsRasterLayer) and candidate.isValid()
                    and derived_from(candidate) == layer.id()
                    and altitude_reference(candidate) == REF_AMSL):
                return candidate
        return None

    def _driven_layers(self) -> List[QgsRasterLayer]:
        """The layers the current reference actually paints.

        Above ground that is the ticked layers themselves; above sea level it is
        their twins, and a layer whose twin does not exist yet is simply left
        out — :meth:`_ensure_twins` is what creates them, and it reports what it
        could not do rather than silently drawing the wrong surface.
        """
        if self._reference == REF_AGL:
            return self._checked_layers()
        twins = [self._twin_of(lyr) for lyr in self._checked_layers()]
        return [twin for twin in twins if twin is not None]

    def _set_layer_visible(self, layer, visible: bool) -> None:
        """Tick/untick *layer* in the Layers panel (canvas visibility)."""
        try:
            node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
            if node is not None:
                node.setItemVisibilityChecked(visible)
        except Exception:  # noqa: BLE001 — cosmetic; never worth an error
            pass

    def _sync_twin_visibility(self) -> None:
        """Show whichever surface of each pair the current reference means.

        Both rasters cover the same ground, so leaving a pair on together would
        paint one over the other and make the altitudes on screen mean nothing.
        Flipping the switch back restores what it hid.

        Every pair is swapped, not just the ticked ones: unticking a layer means
        "stop driving it", not "hide it", so its raster stays on the canvas —
        and it has to be the raster the reference says it is.  Layers with no
        twin are left exactly as the user had them.
        """
        amsl = self._reference == REF_AMSL
        for layer in QgsProject.instance().mapLayers().values():
            if not _is_site_layer(layer):
                continue
            twin = self._twin_of(layer)
            if twin is None:
                continue  # nothing to swap with — leave the user's tree alone
            self._set_layer_visible(layer, not amsl)
            self._set_layer_visible(twin, amsl)

    def _twin_path(self, layer) -> Optional[str]:
        """Where *layer*'s sea-level twin lives on disk.

        Beside the source raster when that directory can be written, so the twin
        survives a reboot along with the run that made it; otherwise in the
        tools directory.
        """
        source = (layer.source() or "").split("|")[0]
        if not os.path.isfile(source):
            return None
        stem = os.path.splitext(os.path.basename(source))[0]
        directory = os.path.dirname(source)
        if not os.access(directory, os.W_OK):
            directory = self._tools_output_dir()
        return os.path.join(directory, f"{stem}_amsl.tif")

    def _terrain_for(self, layer) -> str:
        """The elevation source to use for *layer*, or "" if there is none.

        Preference order, each step a fallback for the one before:

        1. the ``.abt`` cache stamped on the layer when it was loaded,
        2. the cache named in the job config beside the result raster — for
           layers that predate the stamp, or were opened from disk rather than
           by the run that made them,
        3. whatever DEM the user picked earlier this session.

        The run's own cache is preferred over any DEM for a reason beyond
        convenience: it is the exact surface the required altitudes were
        measured against, buildings included where the run used them.  A bare
        DEM would put a rooftop-relative height on top of the ground under the
        building.

        A cache that has gone missing, been emptied, or already failed once is
        skipped rather than retried, so the fallback actually gets a turn
        instead of the same error arriving twice.
        """
        from ..core import abt
        from ..core.job_builder import read_job_terrain_dir

        source = (layer.source() or "").split("|")[0]
        for candidate in (layer_terrain_dir(layer),
                          read_job_terrain_dir(source) or ""):
            if (candidate and candidate not in self._bad_terrain
                    and abt.list_tiles(candidate)):
                return candidate
        if self._fallback_terrain and os.path.exists(self._fallback_terrain):
            return self._fallback_terrain
        return ""

    def _ensure_twins(self) -> bool:
        """Make sure every ticked layer has a sea-level twin.

        Returns True when a background build was started (the caller should stop
        and let :meth:`_on_amsl_finished` pick up), False when there was nothing
        to do or nothing could be done.
        """
        if self._amsl_worker is not None:
            # Already building. The running worker's job list is fixed, so a
            # layer ticked right now is not picked up — but its progress dialog
            # is window-modal, so there is no way to tick one, and if that ever
            # changes the readout says how many are still waiting and re-ticking
            # queues them.
            return True

        pending = []
        for layer in self._checked_layers():
            if self._twin_of(layer) is not None:
                continue
            out = self._twin_path(layer)
            if out is None:
                self._amsl_errors.append(
                    f"{layer.name()}: not a file on disk, so no sea-level view "
                    "can be built. Re-run the analysis to a file first."
                )
                continue
            pending.append([
                (layer.source() or "").split("|")[0], out, layer.id(),
                layer.name(), self._terrain_for(layer),
            ])
        if not pending:
            self._report_amsl_errors()
            return False

        # One prompt for the whole batch: being asked for a DEM once per layer
        # would be miserable, and results from one run share one terrain anyway.
        without_terrain = [job for job in pending if not job[4]]
        if without_terrain:
            dialog = _TerrainSourceDialog(
                self, [job[3] for job in without_terrain],
            )
            if dialog.exec() != QDialog.DialogCode.Accepted:
                # Anything already queued belongs to *this* attempt — carrying
                # it forward would attach it to an unrelated one later.
                self._report_amsl_errors()
                return False
            chosen = dialog.terrain_path()
            if not chosen:
                self._report_amsl_errors()
                QMessageBox.information(
                    self, "Sea-level view",
                    "No elevation data chosen, so the sea-level view was not "
                    "built. The altitudes stay above ground.",
                )
                return False
            # Kept for the session: a second site from the same run would
            # otherwise re-ask for the DEM the user just picked.
            self._fallback_terrain = chosen
            self._bad_terrain.discard(chosen)
            for job in without_terrain:
                job[4] = chosen

        self._start_amsl_build(pending)
        return True

    def _start_amsl_build(self, jobs) -> None:
        self._amsl_total = len(jobs)
        progress = QProgressDialog(
            "Building the sea-level view…", "Cancel", 0, 100, self,
        )
        progress.setWindowTitle("Sea-level altitudes")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)
        self._amsl_progress = progress

        worker = _AmslWorker(
            [tuple(job) for job in jobs], self._tools_output_dir(), self,
        )
        self._amsl_worker = worker
        worker.started_layer.connect(self._on_amsl_started)
        worker.layer_progress.connect(self._on_amsl_progress)
        worker.layer_done.connect(self._on_amsl_layer_done)
        worker.layer_failed.connect(self._on_amsl_layer_failed)
        worker.finished_all.connect(self._on_amsl_finished)
        progress.canceled.connect(self._on_amsl_cancel_requested)
        worker.start()

    # -- AMSL worker callbacks (run on the main thread) --------------------

    def _on_amsl_started(self, index: int, name: str) -> None:
        if self._amsl_progress is not None:
            self._amsl_progress.setLabelText(
                f"Adding terrain to {name} ({index}/{self._amsl_total})…"
            )

    def _on_amsl_progress(self, index: int, fraction: float) -> None:
        # Same re-entrancy guard as the contour ticker: a modal progress dialog
        # spins the event loop inside setValue(), so a tick can deliver the next
        # queued one before this one returns.
        if self._amsl_ticking or not self._amsl_total:
            return
        self._amsl_ticking = True
        try:
            progress = self._amsl_progress
            if progress is not None:
                done = (index - 1 + fraction) / self._amsl_total
                progress.setValue(min(100, int(done * 100)))
        finally:
            self._amsl_ticking = False

    def _on_amsl_layer_done(
        self, out_tif: str, source_id: str, source_name: str, terrain: str,
        missing_fraction: float,
    ) -> None:
        # QGIS layer construction is main-thread-only, hence here.
        source = QgsProject.instance().mapLayer(source_id)
        if source is None:
            return  # removed from the project while we were building
        try:
            layer = load_amsl_result(
                out_tif, f"{source_name} (AMSL)", source_id, terrain,
            )
            add_layer_to_project(layer, GROUP_AMSL)
        except Exception as exc:  # noqa: BLE001
            self._amsl_errors.append(f"{source_name}: {exc}")
            return
        if missing_fraction > _MAX_QUIET_TERRAIN_GAP:
            self._amsl_gaps.append((source_name, missing_fraction))

    def _on_amsl_layer_failed(self, name: str, terrain: str, error: str) -> None:
        # Remember the source that failed: the next attempt then falls through
        # to asking for a DEM rather than repeating this.
        if terrain:
            self._bad_terrain.add(terrain)
            if terrain == self._fallback_terrain:
                self._fallback_terrain = ""
        self._amsl_errors.append(f"{name}: {error}")

    def _on_amsl_cancel_requested(self) -> None:
        if self._amsl_worker is not None:
            self._amsl_worker.cancel()

    def _on_amsl_finished(self, built: int, skipped: int) -> None:  # noqa: ARG002
        progress = self._amsl_progress
        self._amsl_progress = None  # cleared first — a late tick must not re-show
        if progress is not None:
            progress.reset()
            progress.close()
        worker = self._amsl_worker
        self._amsl_worker = None
        if worker is not None:
            worker.wait()
            worker.deleteLater()

        self._report_amsl_errors()
        self._report_amsl_gaps()
        if not self._driven_layers():
            # Nothing to drive above sea level — fall back rather than leave the
            # dock showing AMSL numbers over an AGL map.
            self._set_reference(REF_AGL)
            return
        self._finish_reference_change()

    def _finish_reference_change(self) -> None:
        """Bring the whole dock into line with the reference now in force.

        Shared by the immediate switch and the one that had to wait for a
        background build: the band *list* has to be rebuilt too, or it keeps
        showing the previous reference's rows while the canvas is painted from
        this one's — and the slider then edits a band that is not in the list.
        """
        self._sync_range_to_layers()
        if not self._bands:
            self._seed_bands()
        self._rebuild_band_list()
        self._sync_twin_visibility()
        self._apply()

    def _report_amsl_errors(self) -> None:
        errors = self._amsl_errors
        self._amsl_errors = []
        if not errors:
            return
        QgsMessageLog.logMessage(
            "Sea-level view: " + "; ".join(errors), TAG, Qgis.MessageLevel.Warning,
        )
        QMessageBox.warning(
            self, "Sea-level view",
            "Some layers could not be converted to sea-level altitudes:\n\n"
            + "\n".join(errors)
            + "\n\nSwitching to sea level again will ask you for a DEM.",
        )

    def _report_amsl_gaps(self) -> None:
        """Say so when the elevation data did not cover the whole result.

        Blank patches on a sea-level map are easy to read as "no coverage here"
        when they actually mean "no terrain here" — the difference matters, and
        a large gap usually means the DEM does not cover the area.
        """
        gaps = self._amsl_gaps
        self._amsl_gaps = []
        if not gaps:
            return
        listed = "\n".join(
            f"  • {name}: {fraction:.0%} of the reachable area"
            for name, fraction in gaps
        )
        QgsMessageLog.logMessage(
            "Sea-level view has terrain gaps: "
            + "; ".join(f"{n} {f:.0%}" for n, f in gaps),
            TAG, Qgis.MessageLevel.Warning,
        )
        QMessageBox.warning(
            self, "Sea-level view",
            "Elevation data was missing under part of the result, so those "
            "areas are blank rather than guessed:\n\n" + listed
            + "\n\nIf that is more than you expect, the elevation source "
            "probably does not cover the whole analysis area.",
        )

    # ------------------------------------------------------------------
    # Altitude bands
    # ------------------------------------------------------------------

    @property
    def _bands(self) -> List[_Band]:
        """The bands for the reference in force."""
        return self._band_sets[self._reference]

    def _seed_bands(self) -> None:
        """Start a reference off with one altitude in a useful place.

        A third of the way up the layer's own range: high enough to show real
        coverage on the first paint, low enough that dragging up is interesting.
        """
        low, high = self._range
        band = _Band(self._snap(low + (high - low) / 3.0), QColor(*band_color(0)))
        self._bands.append(band)
        self._rebuild_band_list(select=band)

    @staticmethod
    def _snap(value_m: float) -> int:
        """Round an altitude to something a person would say out loud."""
        if value_m >= 1000:
            return int(round(value_m / 50.0) * 50)
        if value_m >= 100:
            return int(round(value_m / 10.0) * 10)
        return int(round(value_m / 5.0) * 5)

    def _rebuild_band_list(self, select: Optional[_Band] = None) -> None:
        """Redraw the band list from :attr:`_bands`, in altitude order.

        *select* is a band rather than a row on purpose: the list re-sorts here,
        so a row index taken before the rebuild would point at whichever band
        happened to land there.  Defaults to keeping the current selection.
        """
        bands = sorted(self._bands, key=lambda b: b.altitude_m)
        self._band_sets[self._reference] = bands
        target = select if select in bands else self._selected_band
        if target not in bands:
            target = bands[0] if bands else None

        suffix = reference_suffix(self._reference)
        self.band_list.blockSignals(True)
        self.band_list.clear()
        for band in bands:
            item = QListWidgetItem(f"{band.altitude_m:g}{suffix}")
            item.setIcon(_swatch(band.color, band.visible))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if band.visible
                else Qt.CheckState.Unchecked
            )
            self.band_list.addItem(item)
        if target is not None:
            self.band_list.setCurrentRow(bands.index(target))
        self.band_list.blockSignals(False)

        self._selected_band = target
        self._sync_band_editor()

    def _sync_band_editor(self) -> None:
        """Point the slider/spin/colour strip at the selected band, and set
        every control's enabled state from one place.

        One place on purpose: this runs after ``_on_threshold_toggled``, so
        anything that one disables and this one does not would come straight
        back on — editing a list the user cannot see.
        """
        band = self._selected_band
        limiting = self.chk_threshold.isChecked()
        enabled = band is not None and limiting
        self.slider.setEnabled(enabled)
        self.spin.setEnabled(enabled)
        self.btn_band_color.setEnabled(enabled)
        self.btn_add_band.setEnabled(limiting and len(self._bands) < MAX_BANDS)
        self.btn_remove_band.setEnabled(limiting and len(self._bands) > 1)
        if band is None:
            return
        self._updating = True
        self.spin.setSuffix(reference_suffix(self._reference))
        self.slider.setValue(int(round(band.altitude_m)))
        self.spin.setValue(int(round(band.altitude_m)))
        self.btn_band_color.setColor(band.color)
        self._updating = False

    def _sync_range_to_layers(self) -> None:
        """Span the sliders over the altitudes the driven layers actually hold.

        Above ground that is 0 to a tidy ceiling; above sea level the floor is
        the valley, so the slider does not spend most of its travel underground.
        """
        layers = self._driven_layers()
        if not layers:
            return
        ranges = [estimate_altitude_range_m(lyr) for lyr in layers]
        low = min(lo for lo, _ in ranges)
        high = nice_ceiling(max(hi for _, hi in ranges))
        if low >= high:
            low = 0.0
        self._range = (low, high)

        self._updating = True
        self.slider.setMinimum(int(low))
        self.slider.setMaximum(int(high))
        self.spin.setRange(int(low), int(high))
        self._updating = False

        # Bands from a previous, smaller layer set would otherwise sit outside
        # the range and quietly draw nothing.
        clamped = False
        for band in self._bands:
            bounded = min(max(band.altitude_m, low), high)
            if bounded != band.altitude_m:
                band.altitude_m = bounded
                clamped = True
        if clamped:
            self._rebuild_band_list()
        else:
            self._sync_band_editor()

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    def _on_project_changed(self, *args) -> None:
        self.refresh_layers()

    def _on_layer_checked(self, _item) -> None:
        if self._amsl_pending():
            return
        self._sync_range_to_layers()
        self._sync_twin_visibility()
        self._apply()

    def _amsl_pending(self) -> bool:
        """True when the caller should stand down and let the reference settle.

        Either a background build is running (its completion re-applies), or
        nothing could be built and we have dropped back to above-ground — in
        which case the fallback has already redrawn everything.
        """
        if self._reference != REF_AMSL:
            return False
        if self._ensure_twins():
            return True
        if self._checked_layers() and not self._driven_layers():
            # The user has already been told why; sitting on AMSL with an empty
            # canvas would just look like the map had gone.
            self._set_reference(REF_AGL)
            return True
        return False

    def _on_reference_toggled(self, amsl: bool) -> None:
        if self._updating:
            return
        self._set_reference(REF_AMSL if amsl else REF_AGL)

    def _set_reference(self, reference: str) -> None:
        """Switch what the altitudes are measured from, building twins if the
        sea-level view needs them."""
        self._reference = reference
        self._updating = True
        self.radio_agl.setChecked(reference == REF_AGL)
        self.radio_amsl.setChecked(reference == REF_AMSL)
        self.spin.setSuffix(reference_suffix(reference))
        self._updating = False

        if self._amsl_pending():
            return  # a build is running, or we fell back to above-ground
        self._finish_reference_change()

    def _on_band_selected(self, row: int) -> None:
        bands = self._bands
        self._selected_band = bands[row] if 0 <= row < len(bands) else None
        self._sync_band_editor()

    def _on_band_toggled(self, item) -> None:
        row = self.band_list.row(item)
        bands = self._bands
        if not (0 <= row < len(bands)):
            return
        bands[row].visible = item.checkState() == Qt.CheckState.Checked
        self.band_list.blockSignals(True)
        item.setIcon(_swatch(bands[row].color, bands[row].visible))
        self.band_list.blockSignals(False)
        self._apply()

    def _on_add_band(self) -> None:
        bands = self._bands
        if len(bands) >= MAX_BANDS:
            return
        altitude = self._next_altitude()
        if altitude is None:
            self.iface.messageBar().pushInfo(
                "Waveshed",
                "Every altitude in this layer's range already has a band.",
            )
            return
        color = next_band_color([
            (b.color.red(), b.color.green(), b.color.blue()) for b in bands
        ])
        band = _Band(altitude, QColor(*color))
        bands.append(band)
        self._rebuild_band_list(select=band)
        self._apply()

    def _next_altitude(self) -> Optional[int]:
        """Where a newly added band goes, or None if the range is full.

        A step above the highest band is what one expects; once the ceiling is
        taken, the widest gap is split instead.  Both can land on an altitude
        already in use, and a duplicate band is invisible — ``band_stops``
        de-duplicates, so it would get a row, a tick box and a colour while
        drawing nothing — so the last resort is the free altitude furthest from
        any existing band.
        """
        low, high = self._range
        used = sorted(int(round(band.altitude_m)) for band in self._bands)
        if not used:
            return self._snap(low + (high - low) / 3.0)
        taken = set(used)

        step = max(self._snap((high - low) / 8.0), 1)
        candidate = int(used[-1] + step)
        if candidate <= high and candidate not in taken:
            return candidate

        edges = [low] + used + [high]
        widest = max(range(len(edges) - 1), key=lambda i: edges[i + 1] - edges[i])
        midpoint = int(self._snap((edges[widest] + edges[widest + 1]) / 2.0))
        if low <= midpoint <= high and midpoint not in taken:
            return midpoint

        free = [v for v in range(int(low), int(high) + 1) if v not in taken]
        if not free:
            return None
        return max(free, key=lambda v: min(abs(v - u) for u in used))

    def _on_remove_band(self) -> None:
        bands = self._bands
        row = self.band_list.currentRow()
        if len(bands) <= 1 or not (0 <= row < len(bands)):
            return
        del bands[row]
        self._selected_band = None
        self._rebuild_band_list(select=bands[min(row, len(bands) - 1)])
        self._apply()

    def _on_band_color(self, color) -> None:
        band = self._selected_band
        if self._updating or band is None or not color.isValid():
            return
        band.color = QColor(color)
        row = self.band_list.currentRow()
        item = self.band_list.item(row)
        if item is not None:
            self.band_list.blockSignals(True)
            item.setIcon(_swatch(band.color, band.visible))
            self.band_list.blockSignals(False)
        self._apply()

    def _on_slider(self, value: int) -> None:
        if self._updating:
            return
        self._updating = True
        self.spin.setValue(value)
        self._updating = False
        self._set_selected_altitude(value)

    def _on_spin(self, value: int) -> None:
        if self._updating:
            return
        self._updating = True
        self.slider.setValue(value)
        self._updating = False
        self._set_selected_altitude(value)

    def _set_selected_altitude(self, value: int) -> None:
        """Move the selected band, live.

        The list text is updated in place rather than rebuilt: a rebuild would
        re-sort mid-drag and hand the slider a different band halfway through.
        Re-sorting waits until the drag ends (:meth:`_on_band_edited`).
        """
        band = self._selected_band
        if band is None:
            return
        band.altitude_m = float(value)
        row = self.band_list.currentRow()
        item = self.band_list.item(row)
        if item is not None:
            self.band_list.blockSignals(True)
            item.setText(f"{band.altitude_m:g}{reference_suffix(self._reference)}")
            self.band_list.blockSignals(False)
        self._apply()

    def _on_band_edited(self) -> None:
        """Put the list back in altitude order once an edit is finished."""
        if self._selected_band is not None:
            self._rebuild_band_list(select=self._selected_band)

    def _on_threshold_toggled(self, on: bool) -> None:
        self.band_list.setEnabled(on)
        self._sync_band_editor()  # owns every other control's enabled state
        self._apply()

    def _on_reset(self) -> None:
        self.chk_threshold.setChecked(False)
        self._apply()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _apply(self) -> None:
        """Re-render every driven layer for the current altitudes and style."""
        layers = self._driven_layers()
        use_bands = self.chk_threshold.isChecked()
        bands = sorted(self._bands, key=lambda b: b.altitude_m)

        # A shared ramp spanning every driven layer keeps colours comparable
        # across sites — one site's "must climb high" red means what the next
        # one's does.
        ranges = [estimate_altitude_range_m(lyr) for lyr in layers]
        min_ramp_m = min((lo for lo, _ in ranges), default=0.0)
        max_ramp_m = max((hi for _, hi in ranges), default=DEFAULT_RAMP_MAX_M)

        for layer in layers:
            if use_bands and bands and self.radio_bands.isChecked():
                renderer = build_band_renderer(
                    layer.dataProvider(), 1,
                    [band.as_altitude_band() for band in bands],
                    reference=self._reference,
                )
            else:
                renderer = build_min_alt_renderer(
                    layer.dataProvider(), 1,
                    threshold_m=bands[-1].altitude_m if (use_bands and bands)
                    else None,
                    shade=self.radio_shade.isChecked() or not use_bands,
                    max_ramp_m=max_ramp_m,
                    min_ramp_m=min_ramp_m,
                    reference=self._reference,
                )
            layer.setRenderer(renderer)
            layer.triggerRepaint()

        # Above sea level a ticked layer is only drawn once its twin exists, so
        # say how many are waiting rather than quietly reporting a lower count.
        undriven = (len(self._checked_layers()) - len(layers)
                    if self._reference == REF_AMSL else 0)
        self.lbl_readout.setText(
            self._readout(len(layers), bands, use_bands, undriven)
        )

    def _readout(self, layer_count: int, bands, use_bands: bool,
                 undriven: int = 0) -> str:
        """One line saying exactly what is on the canvas right now."""
        if not layer_count:
            if undriven:
                return ("No sea-level view for the selected layers yet — "
                        "re-tick them to build it, or switch back to AGL.")
            return "No layers selected."

        plural = "s" if layer_count != 1 else ""
        waiting = (f" {undriven} more await{'s' if undriven == 1 else ''} a "
                   f"sea-level view." if undriven else "")

        if not use_bands or not bands:
            return (f"Showing the full Minimum LOS Altitude ramp on "
                    f"{layer_count} layer{plural}.{waiting}")

        suffix = reference_suffix(self._reference)
        if not self.radio_bands.isChecked():
            # One ramp clipped at the highest altitude: the per-band colours and
            # the hidden/shown ticks play no part, so do not claim they do.
            return (f"Reachable at ≤ {bands[-1].altitude_m:g}{suffix}, coloured "
                    f"by the altitude each point needs, on {layer_count} "
                    f"layer{plural}.{waiting}")

        shown = [band for band in bands if band.visible]
        if not shown:
            return (f"All {len(bands)} altitudes are hidden — tick one to draw "
                    f"it.")
        if len(shown) == 1:
            return (f"Reachable at ≤ {shown[0].altitude_m:g}{suffix} on "
                    f"{layer_count} layer{plural}.{waiting}")
        listed = ", ".join(f"{band.altitude_m:g}" for band in shown)
        return (f"Reachable at {listed}{suffix} — nested bands on "
                f"{layer_count} layer{plural}.{waiting}")

    # ------------------------------------------------------------------
    # Tools: best-site merge (Option E) and iso-altitude contours (Option D)
    # ------------------------------------------------------------------

    @staticmethod
    def _tools_output_dir() -> str:
        out_dir = os.path.join(tempfile.gettempdir(), "aether_tools")
        os.makedirs(out_dir, exist_ok=True)
        return out_dir

    def _file_inputs(self, layers=None) -> Optional[List[tuple]]:
        """Return ``[(path, name), ...]`` for *layers* (the checked ones by
        default), or None (with a message) if any is not a plain file on disk."""
        inputs = []
        for layer in (self._checked_layers() if layers is None else layers):
            src = layer.source().split("|")[0]
            if not os.path.isfile(src):
                QMessageBox.warning(
                    self, "Waveshed",
                    f"Layer '{layer.name()}' is not a file on disk, so it "
                    "cannot be processed. Re-run the analysis to a file first.",
                )
                return None
            inputs.append((src, layer.name()))
        return inputs

    def _on_merge(self) -> None:
        if self._merge_worker is not None:
            QMessageBox.information(
                self, "Best-site merge",
                "A merge is already running — wait for it to finish or cancel "
                "it first.",
            )
            return
        if len(self._checked_layers()) < 2:
            QMessageBox.information(
                self, "Best-site merge",
                "Tick at least two Minimum LOS Altitude layers to merge.",
            )
            return
        # Deliberately the above-ground originals, whatever reference is on
        # show: the lowest of several surfaces and "add the terrain" commute, so
        # merging here and converting after gives the same answer for less work
        # — and keeps every layer in the list an above-ground site.
        inputs = self._file_inputs()
        if not inputs:
            return

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = self._tools_output_dir()
        out_alt = os.path.join(out_dir, f"best_alt_{stamp}.tif")
        out_site = os.path.join(out_dir, f"best_site_{stamp}.tif")

        # merge_best_site warps + reduces every input — run it off the GUI
        # thread so QGIS stays responsive. The pass has no per-pixel progress,
        # so show a busy dialog; Cancel takes effect between inputs.
        progress = QProgressDialog(
            f"Merging {len(inputs)} sites into a best-site map…", "Cancel",
            0, 0, self,
        )
        progress.setWindowTitle("Best-site merge")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)
        self._merge_progress = progress

        worker = _MergeWorker(inputs, out_alt, out_site, self)
        self._merge_worker = worker
        worker.finished_ok.connect(self._on_merge_ok)
        worker.failed.connect(self._on_merge_failed)
        worker.canceled.connect(self._on_merge_canceled)
        progress.canceled.connect(self._on_merge_cancel_requested)
        worker.start()

    # -- Best-site merge callbacks (run on the main thread) ----------------

    def _close_merge(self) -> None:
        progress = self._merge_progress
        self._merge_progress = None  # cleared first — see _on_contours_finished
        if progress is not None:
            progress.reset()
            progress.close()
        worker = self._merge_worker
        self._merge_worker = None
        if worker is not None:
            worker.wait()
            worker.deleteLater()

    def _on_merge_ok(self, labels: list, out_alt: str, out_site: str) -> None:
        self._close_merge()
        try:
            alt_layer = load_best_alt_result(
                out_alt, f"Best required altitude ({len(labels)} sites)",
            )
            add_layer_to_project(alt_layer, GROUP_MERGE)
            site_layer = load_best_site_result(out_site, labels, "Best site")
            add_layer_to_project(site_layer, GROUP_MERGE)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(
                self, "Best-site merge",
                f"Merged, but the result could not be loaded: {exc}",
            )
            return

        self.refresh_layers()
        QMessageBox.information(
            self, "Best-site merge",
            f"Merged {len(labels)} sites.\n\n"
            "• 'Best required altitude' — an above-ground surface, now in the "
            "layer list and drivable by the altitudes above.\n"
            "• 'Best site' — which transmitter serves each location.",
        )

    def _on_merge_failed(self, error: str) -> None:
        self._close_merge()
        QgsMessageLog.logMessage(
            f"Best-site merge failed: {error}", TAG, Qgis.MessageLevel.Critical,
        )
        QMessageBox.critical(self, "Best-site merge failed", error)

    def _on_merge_canceled(self) -> None:
        self._close_merge()
        self.iface.messageBar().pushInfo("Waveshed", "Best-site merge cancelled.")

    def _on_merge_cancel_requested(self) -> None:
        if self._merge_worker is not None:
            self._merge_worker.cancel()

    def _on_contours(self) -> None:
        if self._contour_worker is not None:
            QMessageBox.information(
                self, "Contours",
                "Contour generation is already running — wait for it to finish "
                "or cancel it first.",
            )
            return
        # Contours trace what is on the canvas, so above sea level they follow
        # the twins — "hold this altitude to clear that ridge" lines.
        layers = self._driven_layers()
        if not layers:
            QMessageBox.information(
                self, "Contours",
                "Tick at least one Minimum LOS Altitude layer.",
            )
            return
        inputs = self._file_inputs(layers)
        if inputs is None:
            return  # a layer was not a file; already reported

        interval_m, ok = QInputDialog.getInt(
            self, "Iso-altitude contours",
            f"Contour interval (metres {reference_phrase(self._reference)}):",
            25, 1, 10000, 1,
        )
        if not ok:
            return

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = self._tools_output_dir()

        # Reset per-run batch state.
        self._contour_errors = []
        self._contour_decimated = []
        self._contour_made = 0
        self._contour_total = len(inputs)
        self._contour_interval_m = interval_m
        self._contour_canceled = False

        # The heavy generation runs in a worker thread; this dialog keeps QGIS
        # responsive and lets the user cancel. Scaled 0–100 over the whole batch
        # because the worker now reports progress *within* each raster too.
        progress = QProgressDialog(
            "Generating iso-altitude contours…", "Cancel", 0, 100, self,
        )
        progress.setWindowTitle("Iso-altitude contours")
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)
        self._contour_progress = progress

        worker = _ContourWorker(inputs, interval_m, out_dir, stamp, self)
        self._contour_worker = worker
        worker.started_layer.connect(self._on_contour_started)
        worker.layer_progress.connect(self._on_contour_progress)
        worker.layer_done.connect(self._on_contour_layer_done)
        worker.layer_failed.connect(self._on_contour_layer_failed)
        worker.finished_all.connect(self._on_contours_finished)
        progress.canceled.connect(self._on_contours_cancel_requested)
        worker.start()

    # -- Contour worker callbacks (run on the main thread) -----------------

    def _on_contour_started(self, index: int, name: str) -> None:
        if self._contour_progress is not None:
            self._contour_progress.setLabelText(
                f"Contouring {name} ({index}/{self._contour_total})…"
            )

    def _on_contour_progress(self, index: int, fraction: float) -> None:
        # A modal QProgressDialog spins the event loop inside setValue(), so a
        # tick can deliver the *next* queued tick before this one returns. That
        # was harmless when progress moved once per layer; now that the worker
        # reports every percent, letting it nest would recurse as deep as the
        # backlog. One tick at a time, and re-read the dialog each time — the
        # batch may have finished inside the call that let this one in.
        if self._contour_ticking or not self._contour_total:
            return
        self._contour_ticking = True
        try:
            progress = self._contour_progress
            if progress is not None:
                done = (index - 1 + fraction) / self._contour_total
                progress.setValue(min(100, int(done * 100)))
        finally:
            self._contour_ticking = False

    def _on_contour_layer_done(
        self, out_gpkg: str, name: str, levels_m: list, decimated: bool,
    ) -> None:
        # QGIS layer construction must happen on the main thread — hence here,
        # not in the worker.
        try:
            layer = load_contour_result(
                out_gpkg, f"Contours — {name} ({self._contour_interval_m} m)",
                levels_m=levels_m,
            )
            add_layer_to_project(layer, GROUP_CONTOURS)
            self._contour_made += 1
            if decimated:
                self._contour_decimated.append(name)
        except Exception as exc:  # noqa: BLE001
            self._contour_errors.append(f"{name}: {exc}")

    def _on_contour_layer_failed(self, name: str, error: str) -> None:
        self._contour_errors.append(f"{name}: {error}")

    def _on_contours_cancel_requested(self) -> None:
        self._contour_canceled = True
        if self._contour_worker is not None:
            self._contour_worker.cancel()

    def _on_contours_finished(self, made: int) -> None:  # noqa: ARG002
        # Clear the attribute *before* tearing the dialog down: this can be
        # reached from inside a progress tick's setValue(), and a tick that ran
        # afterwards would otherwise re-show a dialog nothing will close again.
        progress = self._contour_progress
        self._contour_progress = None
        if progress is not None:
            progress.reset()
            progress.close()

        worker = self._contour_worker
        self._contour_worker = None
        if worker is not None:
            worker.wait()
            worker.deleteLater()

        errors = self._contour_errors
        if errors:
            QgsMessageLog.logMessage(
                "Contour errors: " + "; ".join(errors),
                TAG, Qgis.MessageLevel.Warning,
            )
        if self._contour_made:
            self.refresh_layers()
            message = f"Generated contours for {self._contour_made} layer(s)."
            if self._contour_decimated:
                message += (
                    f" {len(self._contour_decimated)} were traced at reduced "
                    "resolution to keep the lines readable."
                )
            self.iface.messageBar().pushSuccess("Waveshed", message)
        elif self._contour_canceled:
            self.iface.messageBar().pushInfo(
                "Waveshed", "Contour generation cancelled.",
            )
        elif errors:
            QMessageBox.critical(
                self, "Contours failed", "\n".join(errors),
            )

    def _shutdown_workers(self) -> None:
        """Stop any in-flight background workers — called on plugin unload/close."""
        for attr in ("_contour_worker", "_merge_worker", "_amsl_worker"):
            worker = getattr(self, attr, None)
            if worker is not None:
                try:
                    worker.cancel()
                    worker.wait(5000)
                except Exception:
                    pass
                setattr(self, attr, None)
        for attr in ("_contour_progress", "_merge_progress", "_amsl_progress"):
            progress = getattr(self, attr, None)
            if progress is not None:
                try:
                    progress.reset()
                    progress.close()
                except Exception:
                    pass
                setattr(self, attr, None)


# ---------------------------------------------------------------------------
# Module-level singleton helpers (shared by the toolbar action and the
# Site Analysis tab's "Open Altitude Explorer" button).
# ---------------------------------------------------------------------------

_INSTANCE: Optional[AltitudeExplorerDock] = None


def show_altitude_explorer(iface) -> "AltitudeExplorerDock":
    """Create the Altitude Explorer dock (or reveal the existing one) and
    return it."""
    global _INSTANCE
    from qgis.PyQt import sip

    if _INSTANCE is not None:
        try:
            if not sip.isdeleted(_INSTANCE):
                _INSTANCE.refresh_layers()
                _INSTANCE.show()
                _INSTANCE.raise_()
                return _INSTANCE
        except Exception:
            pass
        _INSTANCE = None

    _INSTANCE = AltitudeExplorerDock(iface, iface.mainWindow())
    iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, _INSTANCE)
    _INSTANCE.show()
    _INSTANCE.raise_()
    return _INSTANCE


def remove_altitude_explorer(iface) -> None:
    """Remove and destroy the dock (called on plugin unload)."""
    global _INSTANCE
    from qgis.PyQt import sip

    if _INSTANCE is not None:
        try:
            if not sip.isdeleted(_INSTANCE):
                _INSTANCE._shutdown_workers()
                iface.removeDockWidget(_INSTANCE)
                _INSTANCE.deleteLater()
        except Exception:
            pass
        _INSTANCE = None
