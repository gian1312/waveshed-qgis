"""Main AETHER analysis dialog — single entry point combining all analysis modes.

Top-level mode selector (LOS vs Propagation Loss) with tabs for
Site Analysis, P2P Link, Assets, and Settings.
"""

from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtCore import QByteArray, Qt
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QRadioButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsSettings

from .site_analysis_tab import SiteAnalysisTab
from .p2p_tab import P2PTab
from .asset_manager_tab import AssetManagerTab
from .map_converter_tab import MapConverterTab
from .settings_dialog import SettingsDialog


#: QgsSettings key holding the saved window geometry.
_GEOMETRY_KEY = "waveshed/main_dialog_geometry"

#: Opening size on a first run. The minimum is a floor for small screens, not
#: a sensible working size — five dense tabs at 640x580 make every one of them
#: scroll — and Qt opens a dialog at its minimum when nothing else is set.
_DEFAULT_SIZE = (980, 780)

_MIN_ALT_TOOLTIP = (
    "Minimum-LOS-altitude map: for every location, the lowest altitude "
    "(AGL) at which it first gains line-of-sight to the transmitter.\n"
    "One run answers coverage at *any* altitude — explore it live with "
    "the Altitude Explorer."
)


class AetherMainDialog(QDialog):
    """Combined analysis dialog with mode selector and tabbed interface."""

    def __init__(self, iface, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent or iface.mainWindow())
        self.iface = iface

        self.setWindowTitle("Waveshed")
        # Wide enough for the sites table in Propagation Loss mode, which shows
        # two more columns than LOS (Asset, AZ Rotation) and totals ~785 px of
        # fixed columns before Location gets any. At the old 640 the table was
        # narrower than its own contents in the mode that needs it most.
        self.setMinimumSize(900, 600)

        # Non-modal: clean up on close and float as a proper window
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.Window)

        self._build_ui()
        self._connect_signals()
        self._restore_geometry()

    # ------------------------------------------------------------------
    # Window geometry
    # ------------------------------------------------------------------

    def _restore_geometry(self) -> None:
        """Reopen at the size and place the user last left the window."""
        # QgsSettings hands back whatever type it stored, and only a QByteArray
        # restores; ask for one explicitly rather than trusting the round-trip.
        try:
            saved = QgsSettings().value(_GEOMETRY_KEY, None, type=QByteArray)
        except (TypeError, ValueError):
            saved = None
        if saved and self.restoreGeometry(saved):
            return
        self.resize(*_DEFAULT_SIZE)

    def _save_geometry(self) -> None:
        QgsSettings().setValue(_GEOMETRY_KEY, self.saveGeometry())

    # ------------------------------------------------------------------
    # Public interface used by child tabs
    # ------------------------------------------------------------------

    def get_mode(self) -> str:
        """Return 'LOS', 'LOSS', or 'MIN_ALT' based on the top radio buttons."""
        if self.radio_min_alt.isChecked():
            return "MIN_ALT"
        return "LOS" if self.radio_los.isChecked() else "LOSS"

    def get_loss_model(self) -> str:
        """Return the loss sub-model ('ITM').

        Only meaningful when get_mode() == 'LOSS'.
        """
        return self.combo_loss_model.currentText()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # ---- Mode selector row ----
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Analysis Mode:"))

        self.radio_los = QRadioButton("Line of Sight (LOS)")
        self.radio_loss = QRadioButton("Propagation Loss")
        self.radio_min_alt = QRadioButton("Minimum LOS Altitude")
        self.radio_min_alt.setToolTip(_MIN_ALT_TOOLTIP)
        self.radio_los.setChecked(True)

        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self.radio_los, 0)
        self._mode_group.addButton(self.radio_loss, 1)
        self._mode_group.addButton(self.radio_min_alt, 2)

        mode_row.addWidget(self.radio_los)
        mode_row.addWidget(self.radio_loss)
        mode_row.addWidget(self.radio_min_alt)

        # Loss sub-model (only visible when Loss is selected)
        mode_row.addSpacing(16)
        self.lbl_loss_model = QLabel("Model:")
        mode_row.addWidget(self.lbl_loss_model)

        self.combo_loss_model = QComboBox()
        # ITM only. SIMPLE_LOSS (free-space path loss) was withdrawn as a user
        # choice: it ignores terrain entirely, so on a terrain-analysis tool it
        # reads as a modelling option when it is really a lower bound. Results
        # computed with it still load — see core/result_loader.
        self.combo_loss_model.addItems(["ITM"])
        mode_row.addWidget(self.combo_loss_model)

        self.lbl_loss_model.setVisible(False)
        self.combo_loss_model.setVisible(False)

        mode_row.addStretch()
        layout.addLayout(mode_row)

        # ---- Tabs ----
        self.tabs = QTabWidget()

        self.site_tab = SiteAnalysisTab(self)
        self.p2p_tab = P2PTab(self)
        self.asset_tab = AssetManagerTab()
        self.converter_tab = MapConverterTab(self)
        self.settings_tab = self._build_settings_tab()

        self.tabs.addTab(self.site_tab, "360\u00B0")
        self.tabs.addTab(self.p2p_tab, "P2P Link")
        self.tabs.addTab(self.asset_tab, "Assets")
        self.tabs.addTab(self.converter_tab, "Map Converter")
        self.tabs.addTab(self.settings_tab, "Settings")

        layout.addWidget(self.tabs)

    def _build_settings_tab(self) -> QWidget:
        """Wrap the existing SettingsDialog content as an embeddable widget."""
        # Re-use SettingsDialog but embed it. Create a simple wrapper.
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)

        self._settings_widget = SettingsDialog(parent=wrapper)
        # Replace OK/Cancel with a single "Save Settings" button.
        self._settings_widget._button_box.setVisible(False)
        self._settings_widget.setWindowFlags(Qt.WindowType.Widget)
        layout.addWidget(self._settings_widget)

        from qgis.PyQt.QtWidgets import QPushButton, QHBoxLayout as _HL
        btn_row = _HL()
        btn_row.addStretch()
        btn_save = QPushButton("Save Settings")
        btn_save.clicked.connect(self._on_save_settings)
        btn_row.addWidget(btn_save)
        layout.addLayout(btn_row)

        return wrapper

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        self._mode_group.buttonClicked.connect(self._on_mode_changed)
        self.combo_loss_model.currentIndexChanged.connect(self._on_loss_model_changed)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        # Saved settings must reach the OPEN tabs — they read QgsSettings at
        # construction, and without this fan-out every settings-derived
        # dropdown/path/status stayed stale until the dialog was reopened.
        self._settings_widget.settings_changed.connect(
            self._on_settings_changed)
        # Apply the rule to whichever tab opens first, rather than waiting for
        # the user to switch tabs once.
        self._on_tab_changed(self.tabs.currentIndex())

    def sites_picked_in(self, tab) -> None:
        """One tab placed a site — clear the other tab's sites.

        The 360 and P2P tabs are two analyses of the same map, and each keeps
        its own coordinates. Leaving both populated means a run can quietly use
        points the user last placed in the tab they are not looking at.
        """
        other = self.p2p_tab if tab is self.site_tab else self.site_tab
        clear = getattr(other, "clear_sites", None)
        if callable(clear):
            clear()

    def _on_tab_changed(self, index: int) -> None:
        """Keep the mode selector consistent with the active tab.

        MIN_ALT is a coverage-only output — there is no per-link minimum
        altitude — so the radio is disabled while the P2P tab is in front. It
        was previously possible to select MIN_ALT and then have the P2P tab
        disabled underneath, which reads as the tab being broken.
        """
        on_p2p = index == self.tabs.indexOf(self.p2p_tab)
        self.radio_min_alt.setEnabled(not on_p2p)
        self.radio_min_alt.setToolTip(
            "Not available for point-to-point links — a single link has no "
            "minimum-altitude surface. Switch to the 360° tab to use it."
            if on_p2p else _MIN_ALT_TOOLTIP
        )
        if on_p2p and self.radio_min_alt.isChecked():
            self.radio_los.setChecked(True)
            self._on_mode_changed()

    def _on_mode_changed(self) -> None:
        is_loss = self.radio_loss.isChecked()
        self.lbl_loss_model.setVisible(is_loss)
        self.combo_loss_model.setVisible(is_loss)

        mode = self.get_mode()
        self.site_tab.set_mode(mode)
        # MIN_ALT is a coverage-only (SINGLE) output — there is no per-link
        # minimum-altitude, so the P2P tab is disabled and the link tab falls
        # back to plain geometric LOS labelling.
        is_min_alt = mode == "MIN_ALT"
        p2p_index = self.tabs.indexOf(self.p2p_tab)
        if p2p_index != -1:
            self.tabs.setTabEnabled(p2p_index, not is_min_alt)
            if is_min_alt and self.tabs.currentIndex() == p2p_index:
                self.tabs.setCurrentWidget(self.site_tab)
        self.p2p_tab.set_mode("LOS" if is_min_alt else mode)

    def _on_save_settings(self) -> None:
        self._settings_widget.save_settings()
        from qgis.PyQt.QtWidgets import QMessageBox
        QMessageBox.information(self, "Settings", "Settings saved.")

    def _on_settings_changed(self) -> None:
        """Fan saved settings out to every open tab.

        Each tab's ``refresh_settings()`` is cheap and idempotent: it
        re-reads only what that tab derives from QgsSettings and never
        touches user-entered form state.
        """
        for tab in (self.site_tab, self.p2p_tab, self.asset_tab,
                    self.converter_tab):
            refresh = getattr(tab, "refresh_settings", None)
            if callable(refresh):
                refresh()

    def _on_loss_model_changed(self) -> None:
        # Propagate to tabs that care (site analysis ITM section)
        self.site_tab.set_mode(self.get_mode())

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        # WA_DeleteOnClose means there is no later chance to read the size, and
        # Esc closes through reject() without ever reaching here — so both
        # exits have to save. Geometry first: a throw in the teardown below
        # must not be what loses it.
        self._save_geometry()
        # Save settings when closing
        self._settings_widget.save_settings()
        # Cancel any running workers
        self.site_tab.cancel_worker()
        self.p2p_tab.cancel_worker()
        self.converter_tab.cancel_worker()
        super().closeEvent(event)

    def reject(self) -> None:
        self._save_geometry()
        self._settings_widget.save_settings()
        self.site_tab.cancel_worker()
        self.p2p_tab.cancel_worker()
        self.converter_tab.cancel_worker()
        super().reject()
