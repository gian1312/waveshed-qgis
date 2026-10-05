"""Engine notices: first-run prompt, startup update check, run-error buttons.

Everything here only *points* the user at Settings; nothing downloads by
itself. The engine download always goes through the Settings dialog's
"Download Binaries" flow, which asks for EULA consent every time.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from qgis.PyQt.QtCore import QThread, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import QMessageBox, QPushButton
from qgis.core import Qgis, QgsMessageLog

from ..core import binary_manager

#: Callable[[bool], None] — open Settings; True also starts the download flow.
OpenSettings = Callable[[bool], None]


class _UpdateCheckThread(QThread):
    """Run :func:`binary_manager.check_for_engine_update` off the GUI thread."""

    result = pyqtSignal(object)  # EngineUpdateInfo or None

    def run(self) -> None:  # noqa: D401 – Qt override
        try:
            info = binary_manager.check_for_engine_update()
        except Exception as exc:  # noqa: BLE001 — offline is normal, stay quiet
            QgsMessageLog.logMessage(
                f"Engine update check skipped: {exc}",
                binary_manager.TAG, Qgis.MessageLevel.Info,
            )
            info = None
        self.result.emit(info)


class EngineNotices:
    """Once-per-session startup notices, owned by the plugin instance."""

    def __init__(self, iface, open_settings: OpenSettings) -> None:
        self.iface = iface
        self._open_settings = open_settings
        self._thread: Optional[_UpdateCheckThread] = None
        self._started = False

    def start(self) -> None:
        """Schedule the checks after QGIS has finished starting (never blocks)."""
        if self._started:
            return
        self._started = True
        QTimer.singleShot(0, self.run_startup_checks)

    def run_startup_checks(self) -> None:
        binary_dir = binary_manager.discover_binary_dir()
        if binary_dir is None:
            self._push(
                "Waveshed needs the Aether engine. Download it from Settings "
                "(you will be asked to accept its EULA first).",
                "Open Settings", lambda: self._open_settings(False),
            )
            return
        # macOS: make a browser-downloaded copy runnable (chmod + quarantine),
        # off the GUI thread — xattr can take a moment on a slow disk.
        threading.Thread(
            target=binary_manager.prepare_engine_dir, args=(binary_dir,),
            daemon=True,
        ).start()
        if binary_manager.auto_update_check_enabled():
            self._thread = _UpdateCheckThread(self.iface.mainWindow())
            self._thread.result.connect(self.on_update_info)
            self._thread.start()

    def on_update_info(self, info) -> None:
        if not binary_manager.should_notify_update(info):
            return
        binary_manager.mark_update_notified(info.available)
        installed = info.installed or "an unknown version"
        self._push(
            f"Aether engine {info.available} is available (installed: {installed}).",
            "Update", lambda: self._open_settings(True),
        )

    def stop(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            self._thread.wait(2000)
        self._thread = None

    def _push(self, text: str, button_text: str, on_click: Callable[[], None]) -> None:
        bar = self.iface.messageBar()
        widget = bar.createMessage("Waveshed", text)
        button = QPushButton(widget)
        button.setText(button_text)
        button.clicked.connect(lambda *_: on_click())
        widget.layout().addWidget(button)
        bar.pushWidget(widget, Qgis.MessageLevel.Info, 0)


def find_engine_settings_host(widget) -> Optional[object]:
    """The nearest ancestor of *widget* that can show the engine settings."""
    node = widget
    for _ in range(32):
        if node is None:
            return None
        if callable(getattr(node, "show_engine_settings", None)):
            return node
        parent = getattr(node, "parent", None)
        node = parent() if callable(parent) else None
    return None


def show_run_error(parent, title: str, message: str) -> None:
    """``QMessageBox.critical`` that adds "Update engine" / "Open Settings".

    When *message* tells the user to update or install the engine
    (:func:`binary_manager.engine_message_action`), the box gets a button that
    takes them straight there — the update variant also starts the download
    flow, which still asks for EULA consent.
    """
    action = binary_manager.engine_message_action(message)
    host = find_engine_settings_host(parent) if action else None
    if host is None:
        QMessageBox.critical(parent, title, message)
        return
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle(title)
    box.setText(message)
    label = "Update engine" if action == "update" else "Open Settings"
    go = box.addButton(label, QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Close)
    box.exec()
    if box.clickedButton() is go:
        host.show_engine_settings(start_download=(action == "update"))
