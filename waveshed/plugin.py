"""Main plugin class — toolbar, menu, lifecycle."""

import os
from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsApplication

try:  # Qt6 (QGIS 4) moved QAction from QtWidgets to QtGui.
    from qgis.PyQt.QtGui import QAction
except ImportError:  # Qt5 (QGIS 3.x)
    from qgis.PyQt.QtWidgets import QAction


class AetherPlugin:
    """QGIS plugin implementation for Waveshed — GUI for the Aether RF propagation engine."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu_name = "&Waveshed"
        self.toolbar = None
        self.provider = None
        self._main_dialog = None
        self._engine_notices = None

    def initGui(self):
        self.toolbar = self.iface.addToolBar("Waveshed")
        self.toolbar.setObjectName("WaveshedToolbar")

        icon_path = os.path.join(self.plugin_dir, "resources", "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()

        # Single menu entry
        self.action_main = QAction(icon, "Waveshed Analysis", self.iface.mainWindow())
        self.action_main.triggered.connect(self._open_main_dialog)
        self.iface.addPluginToMenu(self.menu_name, self.action_main)
        self.toolbar.addAction(self.action_main)
        self.actions.append(self.action_main)

        # Altitude Explorer dock toggle — drives LOS Floor result layers.
        # Own icon (main icon + altitude bars) so the two toolbar buttons differ.
        explorer_icon_path = os.path.join(self.plugin_dir, "resources", "icon_explorer.png")
        explorer_icon = QIcon(explorer_icon_path) if os.path.exists(explorer_icon_path) else icon
        self.action_explorer = QAction(
            explorer_icon, "Altitude Explorer", self.iface.mainWindow(),
        )
        self.action_explorer.triggered.connect(self._open_altitude_explorer)
        self.iface.addPluginToMenu(self.menu_name, self.action_explorer)
        self.toolbar.addAction(self.action_explorer)
        self.actions.append(self.action_explorer)

        # Built-in documentation — opens the main dialog on its Help tab.
        self.action_help = QAction(icon, "Waveshed Help", self.iface.mainWindow())
        # Wrapped: QAction.triggered hands the slot a `checked` bool, which
        # would otherwise land in the anchor argument.
        self.action_help.triggered.connect(lambda: self._open_help())
        self.iface.addPluginToMenu(self.menu_name, self.action_help)
        self.actions.append(self.action_help)

        # Register processing provider
        from .provider import AetherProvider
        self.provider = AetherProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

        # First-run prompt / update notice — deferred, never blocks start-up,
        # never downloads anything by itself.
        from .gui.engine_notices import EngineNotices
        self._engine_notices = EngineNotices(self.iface, self.open_engine_settings)
        self._engine_notices.start()

    def open_engine_settings(self, start_download: bool = False) -> None:
        """Open the main dialog on its Settings tab (optionally starting the download)."""
        self._open_main_dialog()
        show = getattr(self._main_dialog, "show_engine_settings", None)
        if callable(show):
            show(start_download=start_download)

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

    def _open_help(self, anchor: str = "") -> None:
        """Open the main dialog and show its Help tab.

        Reuses the dialog when one is already open (``_open_main_dialog``
        raises it), so the menu entry never spawns a second window.
        """
        self._open_main_dialog()
        dialog = self._main_dialog
        show_help = getattr(dialog, "show_help", None)
        if callable(show_help):
            show_help(anchor)

    def _open_altitude_explorer(self):
        from .gui.altitude_explorer import show_altitude_explorer
        show_altitude_explorer(self.iface)

    def unload(self):
        if self._engine_notices is not None:
            self._engine_notices.stop()
            self._engine_notices = None
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
