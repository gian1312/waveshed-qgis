"""Main plugin class — toolbar, menu, lifecycle."""

import os
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction
from qgis.core import QgsApplication


class AetherPlugin:
    """QGIS Plugin implementation for AETHER RF propagation engine."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu_name = "&AETHER"
        self.toolbar = None
        self.provider = None
        self._main_dialog = None

    def initGui(self):
        self.toolbar = self.iface.addToolBar("AETHER")
        self.toolbar.setObjectName("AetherToolbar")

        icon_path = os.path.join(self.plugin_dir, "resources", "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        # Single menu entry
        self.action_main = QAction(icon, "AETHER Analysis", self.iface.mainWindow())
        self.action_main.triggered.connect(self._open_main_dialog)
        self.iface.addPluginToMenu(self.menu_name, self.action_main)
        self.toolbar.addAction(self.action_main)
        self.actions.append(self.action_main)

        # Altitude Explorer dock toggle — drives Min-Altitude result layers.
        self.action_explorer = QAction(
            icon, "Altitude Explorer", self.iface.mainWindow(),
        )
        self.action_explorer.triggered.connect(self._open_altitude_explorer)
        self.iface.addPluginToMenu(self.menu_name, self.action_explorer)
        self.toolbar.addAction(self.action_explorer)
        self.actions.append(self.action_explorer)

        # Register processing provider
        from .provider import AetherProvider
        self.provider = AetherProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def _is_dialog_alive(self) -> bool:
        """Check if the dialog reference is still valid (not deleted by Qt)."""
        if self._main_dialog is None:
            return False
        try:
            # sip.isdeleted is the canonical check, but may not be available.
            from qgis.PyQt import sip
            return not sip.isdeleted(self._main_dialog)
        except (ImportError, AttributeError):
            pass
        try:
            # Fallback: any attribute access on a deleted object raises.
            self._main_dialog.isVisible()
            return True
        except RuntimeError:
            return False

    def _open_altitude_explorer(self):
        from .gui.altitude_explorer import show_altitude_explorer
        show_altitude_explorer(self.iface)

    def unload(self):
        if self._is_dialog_alive():
            try:
                self._main_dialog.close()
            except RuntimeError:
                pass
        self._main_dialog = None
        try:
            from .gui.altitude_explorer import remove_altitude_explorer
            remove_altitude_explorer(self.iface)
        except Exception:
            pass
        for action in self.actions:
            self.iface.removePluginMenu(self.menu_name, action)
        if self.toolbar:
            del self.toolbar
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider)
        self.actions.clear()

    def _open_main_dialog(self):
        if self._is_dialog_alive() and self._main_dialog.isVisible():
            self._main_dialog.raise_()
            self._main_dialog.activateWindow()
            return
        from .gui.main_dialog import AetherMainDialog
        self._main_dialog = AetherMainDialog(self.iface)
        # Clear our reference when Qt deletes the dialog (WA_DeleteOnClose).
        self._main_dialog.destroyed.connect(self._on_dialog_destroyed)
        self._main_dialog.show()

    def _on_dialog_destroyed(self):
        self._main_dialog = None
