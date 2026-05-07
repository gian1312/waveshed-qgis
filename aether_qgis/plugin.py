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

        # Register processing provider
        from .provider import AetherProvider
        self.provider = AetherProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def unload(self):
        if self._main_dialog is not None and self._main_dialog.isVisible():
            self._main_dialog.close()
        self._main_dialog = None
        for action in self.actions:
            self.iface.removePluginMenu(self.menu_name, action)
        if self.toolbar:
            del self.toolbar
        if self.provider:
            QgsApplication.processingRegistry().removeProvider(self.provider)
        self.actions.clear()

    def _open_main_dialog(self):
        if self._main_dialog is not None and self._main_dialog.isVisible():
            self._main_dialog.raise_()
            self._main_dialog.activateWindow()
            return
        from .gui.main_dialog import AetherMainDialog
        self._main_dialog = AetherMainDialog(self.iface)
        self._main_dialog.show()
