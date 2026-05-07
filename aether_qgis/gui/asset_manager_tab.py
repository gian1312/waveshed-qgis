"""Asset management tab widget for emitter/receiver definitions.

Embeddable QWidget (not a dialog) that provides a list of saved assets on
the left with a full editor form on the right.  Users can create, edit, save,
save-as, and delete asset JSON files through this interface.

Provides MPT SIGMA-style emitter management with inline antenna pattern
tables, automatic ERP computation, and SPLAT! .az/.el file I/O.
"""

from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.asset_manager import (
    compute_erp,
    create_default_asset,
    delete_asset,
    list_assets,
    save_asset,
)


# ---------------------------------------------------------------------------
# Antenna pattern table helper
# ---------------------------------------------------------------------------


class _PatternTableWidget(QWidget):
    """Reusable widget wrapping a QTableWidget for antenna pattern editing.

    Two columns: Angle (degrees), Gain (0.0 - 1.0).  Provides buttons for
    add/remove rows, load/save SPLAT!-format files, and clear.
    """

    def __init__(
        self,
        label: str,
        file_ext: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._file_ext = file_ext  # "az" or "el"
        self._label = label
        self._block_clamp = False
        self._build_ui()
        self._connect_signals()

    # ---- UI ----------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Angle (deg)", "Gain (0-1)"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setMinimumHeight(120)
        layout.addWidget(self.table)

        btn_row = QHBoxLayout()
        self.btn_add = QPushButton("Add Row")
        self.btn_remove = QPushButton("Remove Row")
        self.btn_load = QPushButton(f"Load .{self._file_ext}")
        self.btn_save = QPushButton(f"Save .{self._file_ext}")
        self.btn_clear = QPushButton("Clear")
        for btn in (
            self.btn_add,
            self.btn_remove,
            self.btn_load,
            self.btn_save,
            self.btn_clear,
        ):
            btn_row.addWidget(btn)
        layout.addLayout(btn_row)

    def _connect_signals(self) -> None:
        self.btn_add.clicked.connect(self._on_add_row)
        self.btn_remove.clicked.connect(self._on_remove_rows)
        self.btn_load.clicked.connect(self._on_load_file)
        self.btn_save.clicked.connect(self._on_save_file)
        self.btn_clear.clicked.connect(self._on_clear)
        self.table.itemChanged.connect(self._on_item_changed)

    # ---- Data access -------------------------------------------------------

    def get_pattern_data(self) -> list[list[float]]:
        """Return the pattern as a list of [angle, gain] pairs."""
        data: list[list[float]] = []
        for row in range(self.table.rowCount()):
            angle_item = self.table.item(row, 0)
            gain_item = self.table.item(row, 1)
            if angle_item is None or gain_item is None:
                continue
            try:
                angle = float(angle_item.text())
                gain = float(gain_item.text())
            except ValueError:
                continue
            data.append([angle, gain])
        return data

    def set_pattern_data(self, data: list[list[float]]) -> None:
        """Populate the table from a list of [angle, gain] pairs."""
        self._block_clamp = True
        self.table.setRowCount(0)
        for pair in data:
            if len(pair) < 2:
                continue
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(f"{pair[0]:.1f}"))
            self.table.setItem(row, 1, QTableWidgetItem(f"{pair[1]:.3f}"))
        self._block_clamp = False

    # ---- Slots -------------------------------------------------------------

    def _on_add_row(self) -> None:
        """Add a row with default angle = next 10-degree increment, gain = 1.0."""
        row_count = self.table.rowCount()
        if row_count > 0:
            last_angle_item = self.table.item(row_count - 1, 0)
            try:
                next_angle = float(last_angle_item.text()) + 10.0
            except (ValueError, AttributeError):
                next_angle = row_count * 10.0
        else:
            next_angle = 0.0

        self._block_clamp = True
        self.table.insertRow(row_count)
        self.table.setItem(row_count, 0, QTableWidgetItem(f"{next_angle:.1f}"))
        self.table.setItem(row_count, 1, QTableWidgetItem("1.000"))
        self._block_clamp = False

    def _on_remove_rows(self) -> None:
        """Remove all selected rows."""
        rows = sorted(
            {idx.row() for idx in self.table.selectedIndexes()}, reverse=True
        )
        for row in rows:
            self.table.removeRow(row)

    def _on_clear(self) -> None:
        """Remove all rows from the table."""
        self.table.setRowCount(0)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        """Clamp gain values to [0.0, 1.0] when edited."""
        if self._block_clamp:
            return
        if item.column() != 1:
            return
        try:
            val = float(item.text())
        except ValueError:
            return
        clamped = max(0.0, min(1.0, val))
        if clamped != val:
            self._block_clamp = True
            item.setText(f"{clamped:.3f}")
            self._block_clamp = False

    def _on_load_file(self) -> None:
        """Load a SPLAT! antenna pattern file (.az or .el)."""
        ext = self._file_ext
        caption = f"Load {self._label} Pattern"
        filt = f"{ext.upper()} Pattern Files (*.{ext});;All Files (*)"
        path, _ = QFileDialog.getOpenFileName(self, caption, "", filt)
        if not path:
            return

        data: list[list[float]] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    angle = float(parts[0])
                    gain = float(parts[1])
                    gain = max(0.0, min(1.0, gain))
                    data.append([angle, gain])
        except (OSError, ValueError) as exc:
            QMessageBox.warning(
                self,
                "Load Failed",
                f"Could not read pattern file:\n{exc}",
            )
            return

        self.set_pattern_data(data)

    def _on_save_file(self) -> None:
        """Save the current pattern to a SPLAT!-format file."""
        ext = self._file_ext
        caption = f"Save {self._label} Pattern"
        filt = f"{ext.upper()} Pattern Files (*.{ext});;All Files (*)"
        path, _ = QFileDialog.getSaveFileName(self, caption, "", filt)
        if not path:
            return

        data = self.get_pattern_data()
        try:
            with open(path, "w", encoding="utf-8") as f:
                for angle, gain in data:
                    f.write(f"{angle:.0f}\t{gain:.3f}\n")
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Save Failed",
                f"Could not write pattern file:\n{exc}",
            )


# ---------------------------------------------------------------------------
# Tab widget
# ---------------------------------------------------------------------------


class AssetManagerTab(QWidget):
    """Tab widget for managing emitter/receiver asset definitions.

    Provides full MPT SIGMA-style emitter management: transmission parameters
    with auto-computed ERP, inline antenna pattern tables with .az/.el I/O,
    height defaults, and ITM statistical parameters.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self._assets: list[dict] = []
        self._current_index: int = -1
        self._dirty: bool = False
        self._block_dirty: bool = False

        self._build_ui()
        self._connect_signals()
        self.refresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer_layout = QVBoxLayout(self)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_list_panel())
        splitter.addWidget(self._build_editor_panel())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        outer_layout.addWidget(splitter)

    # -- Left panel: asset list + toolbar -----------------------------------

    def _build_list_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(QLabel("Assets"))

        self.asset_list = QListWidget()
        layout.addWidget(self.asset_list)

        btn_layout = QHBoxLayout()
        self.btn_new = QPushButton("New")
        self.btn_save = QPushButton("Save")
        self.btn_save_as = QPushButton("Save As")
        self.btn_delete = QPushButton("Delete")
        self.btn_reload = QPushButton("Reload")
        for btn in (
            self.btn_new,
            self.btn_save,
            self.btn_save_as,
            self.btn_delete,
            self.btn_reload,
        ):
            btn_layout.addWidget(btn)
        layout.addLayout(btn_layout)

        return panel

    # -- Right panel: full editor form -------------------------------------

    def _build_editor_panel(self) -> QWidget:
        # Wrap the editor in a scroll area so it works at small sizes.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)

        # -- Name ----------------------------------------------------------
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self.edit_name = QLineEdit()
        name_row.addWidget(self.edit_name)
        layout.addLayout(name_row)

        # -- Transmission group --------------------------------------------
        tx_group = QGroupBox("Transmission")
        tx_form = QFormLayout(tx_group)

        self.spin_frequency = QDoubleSpinBox()
        self.spin_frequency.setRange(0.001, 100000.0)
        self.spin_frequency.setDecimals(3)
        self.spin_frequency.setValue(433.0)
        self.spin_frequency.setSuffix(" MHz")
        tx_form.addRow("Frequency:", self.spin_frequency)

        # Peak power + computed ERP on the same row.
        power_row = QHBoxLayout()
        self.spin_peak_power = QDoubleSpinBox()
        self.spin_peak_power.setRange(0.001, 1000000.0)
        self.spin_peak_power.setDecimals(3)
        self.spin_peak_power.setValue(10.0)
        self.spin_peak_power.setSuffix(" W")
        power_row.addWidget(self.spin_peak_power)
        power_row.addWidget(QLabel("   Computed ERP:"))
        self.lbl_erp = QLabel("-- W")
        self.lbl_erp.setMinimumWidth(80)
        power_row.addWidget(self.lbl_erp)
        power_row.addStretch()
        tx_form.addRow("Peak Power:", power_row)

        self.spin_gain = QDoubleSpinBox()
        self.spin_gain.setRange(-100.0, 100.0)
        self.spin_gain.setDecimals(2)
        self.spin_gain.setValue(0.0)
        self.spin_gain.setSuffix(" dBi")
        tx_form.addRow("Antenna Gain:", self.spin_gain)

        self.combo_polarization = QComboBox()
        self.combo_polarization.addItems(["Horizontal", "Vertical"])
        tx_form.addRow("Polarization:", self.combo_polarization)

        layout.addWidget(tx_group)

        # -- Defaults group ------------------------------------------------
        defaults_group = QGroupBox("Defaults")
        defaults_form = QFormLayout(defaults_group)

        self.spin_height = QDoubleSpinBox()
        self.spin_height.setRange(0.0, 10000.0)
        self.spin_height.setDecimals(1)
        self.spin_height.setValue(30.0)
        self.spin_height.setSuffix(" m")
        defaults_form.addRow("Default Height:", self.spin_height)

        self.combo_height_mode = QComboBox()
        self.combo_height_mode.addItems(["AGL", "AMSL"])
        defaults_form.addRow("Height Mode:", self.combo_height_mode)

        layout.addWidget(defaults_group)

        # -- Antenna Patterns group ----------------------------------------
        patterns_group = QGroupBox("Antenna Patterns")
        patterns_layout = QVBoxLayout(patterns_group)

        # Azimuth pattern
        az_box = QGroupBox("Azimuth Pattern")
        az_inner = QVBoxLayout(az_box)
        self.az_pattern = _PatternTableWidget("Azimuth", "az")
        az_inner.addWidget(self.az_pattern)
        patterns_layout.addWidget(az_box)

        # Elevation pattern
        el_box = QGroupBox("Elevation Pattern")
        el_inner = QVBoxLayout(el_box)
        self.el_pattern = _PatternTableWidget("Elevation", "el")
        el_inner.addWidget(self.el_pattern)
        patterns_layout.addWidget(el_box)

        layout.addWidget(patterns_group)

        # -- ITM Parameters group ------------------------------------------
        itm_group = QGroupBox("ITM Parameters")
        itm_form = QFormLayout(itm_group)

        self.spin_fraction_situations = QDoubleSpinBox()
        self.spin_fraction_situations.setRange(0.0, 1.0)
        self.spin_fraction_situations.setDecimals(3)
        self.spin_fraction_situations.setSingleStep(0.05)
        self.spin_fraction_situations.setValue(0.5)
        itm_form.addRow("Situation Fraction:", self.spin_fraction_situations)

        self.spin_fraction_time = QDoubleSpinBox()
        self.spin_fraction_time.setRange(0.0, 1.0)
        self.spin_fraction_time.setDecimals(3)
        self.spin_fraction_time.setSingleStep(0.05)
        self.spin_fraction_time.setValue(0.5)
        itm_form.addRow("Time Fraction:", self.spin_fraction_time)

        layout.addWidget(itm_group)

        layout.addStretch()

        scroll.setWidget(panel)
        return scroll

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        # List / toolbar buttons
        self.asset_list.currentRowChanged.connect(self._on_asset_selected)
        self.btn_new.clicked.connect(self._on_new)
        self.btn_save.clicked.connect(self._on_save)
        self.btn_save_as.clicked.connect(self._on_save_as)
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_reload.clicked.connect(self._on_reload)

        # ERP auto-compute when power or gain changes.
        self.spin_peak_power.valueChanged.connect(self._update_erp)
        self.spin_gain.valueChanged.connect(self._update_erp)

        # Dirty tracking on every editor widget.
        self.edit_name.textChanged.connect(self._mark_dirty)
        self.spin_frequency.valueChanged.connect(self._mark_dirty)
        self.spin_peak_power.valueChanged.connect(self._mark_dirty)
        self.spin_gain.valueChanged.connect(self._mark_dirty)
        self.spin_height.valueChanged.connect(self._mark_dirty)
        self.combo_height_mode.currentIndexChanged.connect(self._mark_dirty)
        self.combo_polarization.currentIndexChanged.connect(self._mark_dirty)
        self.spin_fraction_situations.valueChanged.connect(self._mark_dirty)
        self.spin_fraction_time.valueChanged.connect(self._mark_dirty)

        # Pattern table changes also mark dirty.
        self.az_pattern.table.itemChanged.connect(self._mark_dirty)
        self.el_pattern.table.itemChanged.connect(self._mark_dirty)
        self.az_pattern.btn_add.clicked.connect(self._mark_dirty)
        self.az_pattern.btn_remove.clicked.connect(self._mark_dirty)
        self.az_pattern.btn_load.clicked.connect(self._mark_dirty)
        self.az_pattern.btn_clear.clicked.connect(self._mark_dirty)
        self.el_pattern.btn_add.clicked.connect(self._mark_dirty)
        self.el_pattern.btn_remove.clicked.connect(self._mark_dirty)
        self.el_pattern.btn_load.clicked.connect(self._mark_dirty)
        self.el_pattern.btn_clear.clicked.connect(self._mark_dirty)

    # ------------------------------------------------------------------
    # ERP auto-computation
    # ------------------------------------------------------------------

    def _update_erp(self) -> None:
        """Recompute and display the ERP based on current peak power and gain."""
        erp = compute_erp(self.spin_peak_power.value(), self.spin_gain.value())
        self.lbl_erp.setText(f"{erp:.3f} W")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Reload the asset list from disk and repopulate the QListWidget."""
        self._assets = list_assets()
        self._rebuild_list()

    # ------------------------------------------------------------------
    # List helpers
    # ------------------------------------------------------------------

    def _rebuild_list(self) -> None:
        """Repopulate the QListWidget from the in-memory asset list."""
        self.asset_list.blockSignals(True)
        self.asset_list.clear()
        for asset in self._assets:
            item = QListWidgetItem(asset.get("name", "Unnamed"))
            self.asset_list.addItem(item)
        self.asset_list.blockSignals(False)

        if self._assets:
            self.asset_list.setCurrentRow(0)
        else:
            self._current_index = -1
            self._clear_editor()

    # ------------------------------------------------------------------
    # Editor <-> asset dict
    # ------------------------------------------------------------------

    def _load_into_editor(self, asset: dict) -> None:
        """Populate the editor widgets from an asset dict without triggering
        the dirty flag."""
        self._block_dirty = True

        self.edit_name.setText(asset.get("name", ""))
        self.spin_frequency.setValue(asset.get("frequency_mhz", 433.0))
        self.spin_peak_power.setValue(asset.get("peak_power_watts", 10.0))
        self.spin_gain.setValue(asset.get("antenna_gain_dbi", 0.0))

        # Polarization: 0 = Horizontal, 1 = Vertical.
        pol = asset.get("polarization", 0)
        self.combo_polarization.setCurrentIndex(min(max(pol, 0), 1))

        self.spin_height.setValue(asset.get("default_height_m", 30.0))
        mode = asset.get("default_height_mode", "AGL")
        idx = self.combo_height_mode.findText(mode)
        self.combo_height_mode.setCurrentIndex(max(idx, 0))

        # Antenna patterns.
        az_data = asset.get("azimuth_pattern", {}).get("data", [])
        self.az_pattern.set_pattern_data(az_data)
        el_data = asset.get("elevation_pattern", {}).get("data", [])
        self.el_pattern.set_pattern_data(el_data)

        # ITM statistical parameters.
        self.spin_fraction_situations.setValue(
            asset.get("fraction_situations", 0.5)
        )
        self.spin_fraction_time.setValue(asset.get("fraction_time", 0.5))

        # Update the ERP label.
        self._update_erp()

        self._block_dirty = False
        self._dirty = False

    def _collect_from_editor(self) -> dict:
        """Read the editor widgets into an asset dict."""
        erp = compute_erp(self.spin_peak_power.value(), self.spin_gain.value())

        return {
            "name": self.edit_name.text().strip(),
            "frequency_mhz": self.spin_frequency.value(),
            "peak_power_watts": self.spin_peak_power.value(),
            "antenna_gain_dbi": self.spin_gain.value(),
            "erp_watts": erp,
            "default_height_m": self.spin_height.value(),
            "default_height_mode": self.combo_height_mode.currentText(),
            "polarization": self.combo_polarization.currentIndex(),
            "azimuth_pattern": {"data": self.az_pattern.get_pattern_data()},
            "elevation_pattern": {"data": self.el_pattern.get_pattern_data()},
            "fraction_situations": self.spin_fraction_situations.value(),
            "fraction_time": self.spin_fraction_time.value(),
        }

    def _clear_editor(self) -> None:
        """Reset editor widgets to blank / default values."""
        self._block_dirty = True

        self.edit_name.setText("")
        self.spin_frequency.setValue(433.0)
        self.spin_peak_power.setValue(10.0)
        self.spin_gain.setValue(0.0)
        self.combo_polarization.setCurrentIndex(0)
        self.spin_height.setValue(30.0)
        self.combo_height_mode.setCurrentIndex(0)
        self.az_pattern.set_pattern_data([])
        self.el_pattern.set_pattern_data([])
        self.spin_fraction_situations.setValue(0.5)
        self.spin_fraction_time.setValue(0.5)
        self._update_erp()

        self._block_dirty = False
        self._dirty = False

    # ------------------------------------------------------------------
    # Dirty tracking
    # ------------------------------------------------------------------

    def _mark_dirty(self) -> None:
        if not self._block_dirty:
            self._dirty = True

    def _check_unsaved_changes(self) -> bool:
        """If the editor has unsaved changes, ask the user what to do.

        Returns True if it is safe to proceed (changes were saved or
        discarded).  Returns False if the user cancelled the action.
        """
        if not self._dirty:
            return True

        answer = QMessageBox.question(
            self,
            "Unsaved Changes",
            "The current asset has unsaved changes.\n"
            "Do you want to discard them?",
            QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer == QMessageBox.Discard:
            self._dirty = False
            return True
        return False

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_asset_selected(self, row: int) -> None:
        if row < 0 or row >= len(self._assets):
            return

        # If switching away from a dirty asset, ask first.
        if row != self._current_index and not self._check_unsaved_changes():
            # Revert the list selection without re-entering this slot.
            self.asset_list.blockSignals(True)
            self.asset_list.setCurrentRow(self._current_index)
            self.asset_list.blockSignals(False)
            return

        self._current_index = row
        self._load_into_editor(self._assets[row])

    def _on_new(self) -> None:
        if not self._check_unsaved_changes():
            return

        asset = create_default_asset()

        # Ensure uniqueness: append a number if "New Asset" already exists.
        existing_names = {a.get("name") for a in self._assets}
        base_name = asset["name"]
        counter = 1
        while asset["name"] in existing_names:
            counter += 1
            asset["name"] = f"{base_name} {counter}"

        # Save immediately so it has a file on disk.
        path = save_asset(asset)
        asset["_path"] = path

        self._assets.append(asset)
        self._rebuild_list()

        # Select the newly created asset (last item).
        new_row = len(self._assets) - 1
        self.asset_list.setCurrentRow(new_row)

    def _on_save(self) -> None:
        """Save current editor state to the existing asset file."""
        if self._current_index < 0 or self._current_index >= len(self._assets):
            return

        data = self._collect_from_editor()
        if not data["name"]:
            QMessageBox.warning(self, "Validation", "Asset name cannot be empty.")
            return

        existing = self._assets[self._current_index]
        path = existing.get("_path")

        saved_path = save_asset(data, path=path)
        data["_path"] = saved_path

        self._assets[self._current_index] = data
        self._dirty = False

        # Update the list item text in case the name changed.
        item = self.asset_list.item(self._current_index)
        if item:
            item.setText(data["name"])

    def _on_save_as(self) -> None:
        """Save a copy of the current editor state under a new file name."""
        if self._current_index < 0:
            return

        data = self._collect_from_editor()
        if not data["name"]:
            QMessageBox.warning(self, "Validation", "Asset name cannot be empty.")
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Asset As",
            "",
            "Asset JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return

        saved_path = save_asset(data, path=path)
        data["_path"] = saved_path

        # Append as a new entry in the list.
        self._assets.append(data)
        self._rebuild_list()

        new_row = len(self._assets) - 1
        self.asset_list.setCurrentRow(new_row)
        self._dirty = False

    def _on_delete(self) -> None:
        if self._current_index < 0 or self._current_index >= len(self._assets):
            return

        asset = self._assets[self._current_index]
        name = asset.get("name", "Unnamed")

        answer = QMessageBox.question(
            self,
            "Delete Asset",
            f'Are you sure you want to delete "{name}"?',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        path = asset.get("_path")
        if path:
            try:
                delete_asset(path)
            except OSError:
                QMessageBox.warning(
                    self,
                    "Delete Failed",
                    f"Could not delete file:\n{path}",
                )
                return

        self._dirty = False
        self._assets.pop(self._current_index)
        self._current_index = -1
        self._rebuild_list()

    def _on_reload(self) -> None:
        """Reload all assets from disk, discarding unsaved changes."""
        if self._dirty:
            answer = QMessageBox.question(
                self,
                "Reload Assets",
                "Reloading will discard any unsaved changes.\nContinue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        self._dirty = False
        self._current_index = -1
        self.refresh()
