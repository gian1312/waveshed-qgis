"""Main AETHER analysis dialog — single entry point combining all analysis modes.

Top-level mode selector (LOS vs Propagation Loss) with tabs for
Site Analysis, P2P Link, Assets, and Settings.
"""

from __future__ import annotations

from typing import Optional

from qgis.PyQt.QtCore import Qt
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

from .site_analysis_tab import SiteAnalysisTab
from .p2p_tab import P2PTab
from .asset_manager_tab import AssetManagerTab
from .map_converter_tab import MapConverterTab
from .settings_dialog import SettingsDialog


class AetherMainDialog(QDialog):
    """Combined analysis dialog with mode selector and tabbed interface."""

    def __init__(self, iface, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent or iface.mainWindow())
        self.iface = iface

        self.setWindowTitle("AETHER RF Analysis")
        self.setMinimumSize(640, 580)

        # Non-modal: clean up on close and float as a proper window
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowFlags(self.windowFlags() | Qt.Window)

        self._build_ui()
        self._connect_signals()

    # ------------------------------------------------------------------
    # Public interface used by child tabs
    # ------------------------------------------------------------------

    def get_mode(self) -> str:
        """Return 'LOS' or 'LOSS' based on the top radio buttons."""
        return "LOS" if self.radio_los.isChecked() else "LOSS"

    def get_loss_model(self) -> str:
        """Return the loss sub-model ('SIMPLE_LOSS' or 'ITM').

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
        self.radio_los.setChecked(True)

        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self.radio_los, 0)
        self._mode_group.addButton(self.radio_loss, 1)

        mode_row.addWidget(self.radio_los)
        mode_row.addWidget(self.radio_loss)

        # Loss sub-model (only visible when Loss is selected)
        mode_row.addSpacing(16)
        self.lbl_loss_model = QLabel("Model:")
        mode_row.addWidget(self.lbl_loss_model)

        self.combo_loss_model = QComboBox()
        self.combo_loss_model.addItems(["SIMPLE_LOSS", "ITM"])
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
        self._settings_widget.setWindowFlags(Qt.Widget)
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

    def _on_mode_changed(self) -> None:
        is_loss = self.radio_loss.isChecked()
        self.lbl_loss_model.setVisible(is_loss)
        self.combo_loss_model.setVisible(is_loss)

        mode = self.get_mode()
        self.site_tab.set_mode(mode)
        self.p2p_tab.set_mode(mode)

    def _on_save_settings(self) -> None:
        self._settings_widget.save_settings()
        from qgis.PyQt.QtWidgets import QMessageBox
        QMessageBox.information(self, "Settings", "Settings saved.")

    def _on_loss_model_changed(self) -> None:
        # Propagate to tabs that care (site analysis ITM section)
        self.site_tab.set_mode(self.get_mode())

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        # Save settings when closing
        self._settings_widget.save_settings()
        # Cancel any running workers
        self.site_tab.cancel_worker()
        self.p2p_tab.cancel_worker()
        self.converter_tab.cancel_worker()
        super().closeEvent(event)

    def reject(self) -> None:
        self._settings_widget.save_settings()
        self.site_tab.cancel_worker()
        self.p2p_tab.cancel_worker()
        self.converter_tab.cancel_worker()
        super().reject()
