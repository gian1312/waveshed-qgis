"""Altitude Explorer — a dock panel for interactively viewing MIN_ALT results.

A single MIN_ALT run stores, per pixel, the *lowest* altitude (AGL) at which a
location gains line-of-sight to the transmitter.  Coverage at any chosen
altitude *A* is therefore just ``{pixels where required_altitude <= A}`` — a
pure re-classification of the existing raster, with **no recomputation**.

This dock exposes that as a live altitude slider: drag it and every selected
MIN_ALT layer instantly repaints to show the area reachable at that altitude.
It drives one *or several* layers at once (e.g. multiple transmitter sites),
so their reachable areas union visually on the canvas.

The heavy lifting (renderer construction, quantization) lives in
``core.result_loader`` / ``core.min_alt``; this module is only the UI.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from typing import List, Optional

from qgis.core import Qgis, QgsMessageLog, QgsProject, QgsRasterLayer
from qgis.gui import QgsDockWidget
from qgis.PyQt.QtCore import Qt, QThread, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
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

from ..core.layer_utils import is_min_alt_layer
from ..core.min_alt import DEFAULT_RAMP_MAX_M, nice_ceiling
from ..core.result_loader import (
    GROUP_CONTOURS,
    GROUP_MERGE,
    add_layer_to_project,
    build_min_alt_renderer,
    estimate_max_altitude_m,
    load_best_alt_result,
    load_best_site_result,
    load_contour_result,
)

TAG = "Waveshed"

_LAYER_ID_ROLE = Qt.UserRole


def _is_candidate(layer) -> bool:
    """True if *layer* is drivable by the explorer.

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
            and provider.dataType(1) == Qgis.DataType.UInt16
        )
    except Exception:
        return False


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
    #: (out_gpkg, display name) — a layer finished; load it on the main thread.
    layer_done = pyqtSignal(str, str)
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
                raster_tools.generate_contours(path, out_gpkg, self._interval_m)
                self.layer_done.emit(out_gpkg, name)
                made += 1
            except Exception as exc:  # noqa: BLE001 — surfaced to the user
                self.layer_failed.emit(name, str(exc))
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
    """Dock widget with a live altitude threshold slider for MIN_ALT layers."""

    def __init__(self, iface, parent: Optional[QWidget] = None) -> None:
        super().__init__("Waveshed Altitude Explorer", parent)
        self.setObjectName("WaveshedAltitudeExplorer")
        self.iface = iface
        self._updating = False  # guard against slider<->spin feedback loops

        # Background iso-altitude contour generation state.
        self._contour_worker: Optional[_ContourWorker] = None
        self._contour_progress: Optional[QProgressDialog] = None
        self._contour_errors: List[str] = []
        self._contour_made = 0
        self._contour_total = 0
        self._contour_interval_m = 0
        self._contour_canceled = False

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
            "Pick your preferred altitude — every selected Minimum LOS Altitude layer "
            "shows where it can reach the transmitter at that height. "
            "No recomputation needed."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(intro)

        # ---- Layer selection ----
        layer_group = QGroupBox("Layers")
        lg_layout = QVBoxLayout(layer_group)
        self.layer_list = QListWidget()
        self.layer_list.setMaximumHeight(140)
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

        # ---- Altitude control ----
        alt_group = QGroupBox("Preferred altitude")
        ag_layout = QVBoxLayout(alt_group)

        self.chk_threshold = QCheckBox("Limit to altitude (show reachable area)")
        self.chk_threshold.setChecked(True)
        ag_layout.addWidget(self.chk_threshold)

        slider_row = QHBoxLayout()
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setMaximum(int(DEFAULT_RAMP_MAX_M))
        self.slider.setValue(int(DEFAULT_RAMP_MAX_M) // 3)
        self.spin = QSpinBox()
        self.spin.setRange(0, int(DEFAULT_RAMP_MAX_M))
        self.spin.setSuffix(" m AGL")
        self.spin.setValue(self.slider.value())
        slider_row.addWidget(self.slider, 1)
        slider_row.addWidget(self.spin)
        ag_layout.addLayout(slider_row)

        self.lbl_readout = QLabel("")
        self.lbl_readout.setStyleSheet("font-weight: bold;")
        ag_layout.addWidget(self.lbl_readout)

        # ---- Display style ----
        style_row = QHBoxLayout()
        style_row.addWidget(QLabel("Colour:"))
        self.radio_shade = QRadioButton("By required altitude")
        self.radio_flat = QRadioButton("Flat mask")
        self.radio_shade.setChecked(True)
        self.radio_shade.setToolTip(
            "Colour reachable pixels by the altitude they actually need "
            "(blue = low / easy, red = must climb high)."
        )
        self.radio_flat.setToolTip(
            "Draw all reachable pixels in one colour — a clean coverage mask."
        )
        self._style_group = QButtonGroup(self)
        self._style_group.addButton(self.radio_shade)
        self._style_group.addButton(self.radio_flat)
        style_row.addWidget(self.radio_shade)
        style_row.addWidget(self.radio_flat)
        style_row.addStretch()
        ag_layout.addLayout(style_row)
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
        self.btn_reset.clicked.connect(self._on_reset)
        self.btn_all.clicked.connect(lambda: self._set_all_checked(True))
        self.btn_none.clicked.connect(lambda: self._set_all_checked(False))
        self.btn_refresh.clicked.connect(self.refresh_layers)
        self.layer_list.itemChanged.connect(self._on_layer_checked)
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
            if _is_candidate(lyr)
        ]
        for layer in candidates:
            item = QListWidgetItem(layer.name())
            item.setData(_LAYER_ID_ROLE, layer.id())
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            # New layers default to checked (so a fresh run is driven at once);
            # keep prior choices on refresh.
            if not had_any or layer.id() in previously_checked:
                item.setCheckState(Qt.Checked)
            else:
                item.setCheckState(Qt.Unchecked)
            self.layer_list.addItem(item)
        self.layer_list.blockSignals(False)

        if not candidates:
            self.lbl_readout.setText(
                "No Minimum LOS Altitude layers found. "
                "Run a Minimum LOS Altitude analysis first."
            )
        else:
            self._sync_range_to_layers()
            self._apply()

    def _checked_layers(self) -> List[QgsRasterLayer]:
        proj = QgsProject.instance()
        out: List[QgsRasterLayer] = []
        for i in range(self.layer_list.count()):
            item = self.layer_list.item(i)
            if item.checkState() == Qt.Checked:
                layer = proj.mapLayer(item.data(_LAYER_ID_ROLE))
                if isinstance(layer, QgsRasterLayer) and layer.isValid():
                    out.append(layer)
        return out

    def _checked_layer_ids(self) -> List[str]:
        return [lyr.id() for lyr in self._checked_layers()]

    def _set_all_checked(self, checked: bool) -> None:
        self.layer_list.blockSignals(True)
        state = Qt.Checked if checked else Qt.Unchecked
        for i in range(self.layer_list.count()):
            self.layer_list.item(i).setCheckState(state)
        self.layer_list.blockSignals(False)
        self._apply()

    def _sync_range_to_layers(self) -> None:
        """Set the slider/spin maximum to a tidy ceiling over all selected
        layers' required altitudes, so the full range is reachable."""
        layers = self._checked_layers()
        if not layers:
            return
        max_m = max(estimate_max_altitude_m(lyr) for lyr in layers)
        max_m = nice_ceiling(max_m)
        self._updating = True
        self.slider.setMaximum(int(max_m))
        self.spin.setMaximum(int(max_m))
        if self.slider.value() > max_m:
            self.slider.setValue(int(max_m))
            self.spin.setValue(int(max_m))
        self._updating = False

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    def _on_project_changed(self, *args) -> None:
        self.refresh_layers()

    def _on_layer_checked(self, _item) -> None:
        self._sync_range_to_layers()
        self._apply()

    def _on_slider(self, value: int) -> None:
        if self._updating:
            return
        self._updating = True
        self.spin.setValue(value)
        self._updating = False
        self._apply()

    def _on_spin(self, value: int) -> None:
        if self._updating:
            return
        self._updating = True
        self.slider.setValue(value)
        self._updating = False
        self._apply()

    def _on_threshold_toggled(self, on: bool) -> None:
        self.slider.setEnabled(on)
        self.spin.setEnabled(on)
        self._apply()

    def _on_reset(self) -> None:
        self.chk_threshold.setChecked(False)
        self._apply()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _apply(self) -> None:
        """Re-render every selected layer for the current altitude/style."""
        layers = self._checked_layers()
        use_threshold = self.chk_threshold.isChecked()
        threshold_m = float(self.spin.value()) if use_threshold else None
        shade = self.radio_shade.isChecked()

        # A shared ramp ceiling keeps colours comparable across sites.
        max_ramp_m = max(
            (estimate_max_altitude_m(lyr) for lyr in layers),
            default=DEFAULT_RAMP_MAX_M,
        )

        for layer in layers:
            renderer = build_min_alt_renderer(
                layer.dataProvider(), 1,
                threshold_m=threshold_m,
                shade=shade,
                max_ramp_m=max_ramp_m,
            )
            layer.setRenderer(renderer)
            layer.triggerRepaint()

        if use_threshold:
            n = len(layers)
            self.lbl_readout.setText(
                f"Showing area reachable at ≤ {self.spin.value()} m AGL "
                f"on {n} layer{'s' if n != 1 else ''}."
            )
        elif layers:
            self.lbl_readout.setText(
                "Showing full Minimum LOS Altitude ramp (all reachable pixels)."
            )

    # ------------------------------------------------------------------
    # Tools: best-site merge (Option E) and iso-altitude contours (Option D)
    # ------------------------------------------------------------------

    @staticmethod
    def _tools_output_dir() -> str:
        out_dir = os.path.join(tempfile.gettempdir(), "aether_tools")
        os.makedirs(out_dir, exist_ok=True)
        return out_dir

    def _file_inputs(self) -> Optional[List[tuple]]:
        """Return ``[(path, name), ...]`` for the checked layers, or None (with
        a message) if any is not a plain file on disk."""
        inputs = []
        for layer in self._checked_layers():
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
        progress.setWindowModality(Qt.WindowModal)
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
        if self._merge_progress is not None:
            self._merge_progress.reset()
            self._merge_progress.close()
            self._merge_progress = None
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
            "• 'Best required altitude' — drive it with the slider above.\n"
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
        if not self._checked_layers():
            QMessageBox.information(
                self, "Contours",
                "Tick at least one Minimum LOS Altitude layer.",
            )
            return
        inputs = self._file_inputs()
        if inputs is None:
            return  # a layer was not a file; already reported

        interval_m, ok = QInputDialog.getInt(
            self, "Iso-altitude contours",
            "Contour interval (metres of required altitude):",
            25, 1, 10000, 1,
        )
        if not ok:
            return

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = self._tools_output_dir()

        # Reset per-run batch state.
        self._contour_errors = []
        self._contour_made = 0
        self._contour_total = len(inputs)
        self._contour_interval_m = interval_m
        self._contour_canceled = False

        # The heavy generation runs in a worker thread; this dialog keeps QGIS
        # responsive and lets the user cancel (effective at the next layer).
        progress = QProgressDialog(
            "Generating iso-altitude contours…", "Cancel", 0, len(inputs), self,
        )
        progress.setWindowTitle("Iso-altitude contours")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        progress.setValue(0)
        self._contour_progress = progress

        worker = _ContourWorker(inputs, interval_m, out_dir, stamp, self)
        self._contour_worker = worker
        worker.started_layer.connect(self._on_contour_started)
        worker.layer_done.connect(self._on_contour_layer_done)
        worker.layer_failed.connect(self._on_contour_layer_failed)
        worker.finished_all.connect(self._on_contours_finished)
        progress.canceled.connect(self._on_contours_cancel_requested)
        worker.start()

    # -- Contour worker callbacks (run on the main thread) -----------------

    def _on_contour_started(self, index: int, name: str) -> None:
        if self._contour_progress is not None:
            self._contour_progress.setValue(index - 1)
            self._contour_progress.setLabelText(
                f"Contouring {name} ({index}/{self._contour_total})…"
            )

    def _on_contour_layer_done(self, out_gpkg: str, name: str) -> None:
        # QGIS layer construction must happen on the main thread — hence here,
        # not in the worker.
        try:
            layer = load_contour_result(
                out_gpkg, f"Contours — {name} ({self._contour_interval_m} m)",
            )
            add_layer_to_project(layer, GROUP_CONTOURS)
            self._contour_made += 1
        except Exception as exc:  # noqa: BLE001
            self._contour_errors.append(f"{name}: {exc}")

    def _on_contour_layer_failed(self, name: str, error: str) -> None:
        self._contour_errors.append(f"{name}: {error}")

    def _on_contours_cancel_requested(self) -> None:
        self._contour_canceled = True
        if self._contour_worker is not None:
            self._contour_worker.cancel()

    def _on_contours_finished(self, made: int) -> None:  # noqa: ARG002
        if self._contour_progress is not None:
            self._contour_progress.reset()
            self._contour_progress.close()
            self._contour_progress = None

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
            self.iface.messageBar().pushSuccess(
                "Waveshed",
                f"Generated contours for {self._contour_made} layer(s).",
            )
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
        for attr in ("_contour_worker", "_merge_worker"):
            worker = getattr(self, attr, None)
            if worker is not None:
                try:
                    worker.cancel()
                    worker.wait(5000)
                except Exception:
                    pass
                setattr(self, attr, None)
        for attr in ("_contour_progress", "_merge_progress"):
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
    iface.addDockWidget(Qt.RightDockWidgetArea, _INSTANCE)
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
