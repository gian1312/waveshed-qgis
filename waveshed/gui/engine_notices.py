"""Engine notices: first-run prompt, startup update check, run-error buttons.

Everything here only *points* the user at Settings; nothing downloads by
itself. The engine download always goes through the Settings dialog's
"Download Binaries" flow, which asks for EULA consent every time.
"""

from __future__ import annotations

import threading
from typing import Callable, List, Optional, Tuple

from qgis.PyQt.QtCore import QThread, QTimer, pyqtSignal
from qgis.PyQt.QtWidgets import QCheckBox, QMessageBox, QPushButton
from qgis.core import Qgis, QgsMessageLog, QgsSettings

from ..core import api_key, binary_manager

#: Callable[[bool], None] — open Settings; True also starts the download flow.
OpenSettings = Callable[[bool], None]

#: The first-run message bar: what is missing, and the one click that fixes it.
FIRST_RUN_TEXT = (
    "Waveshed needs the free Aether engine before it can compute anything. "
    "Click \"Download engine\": you accept its licence (EULA), then it "
    "downloads and installs by itself (Settings \u2192 Download Binaries)."
)

#: The startup bar when the engine is there but no usable API key is.
KEY_TEXT = (
    "Waveshed needs an API key to run analyses. Click \"Enter API key\" to "
    "paste yours, or get one with \"Get API Key\" there."
)

#: Added to :data:`FIRST_RUN_TEXT` when the key is missing too.
KEY_TEXT_ALSO = "It also needs an API key (\"Enter API key\")."


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

    def __init__(self, iface, open_settings: OpenSettings,
                 open_key_settings: Optional[Callable[[], None]] = None) -> None:
        self.iface = iface
        self._open_settings = open_settings
        self._open_key_settings = open_key_settings
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
        no_key = api_key.key_problem() is not None
        if binary_dir is None:
            buttons = [("Download engine", lambda: self._open_settings(True))]
            text = FIRST_RUN_TEXT
            if no_key:
                text += " " + KEY_TEXT_ALSO
                buttons.append((api_key.ENTER_KEY_BUTTON, self._open_key))
            self._push(text, buttons)
            return
        if no_key:
            self._push(KEY_TEXT, [(api_key.ENTER_KEY_BUTTON, self._open_key)])
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
            [("Update", lambda: self._open_settings(True))],
        )

    def stop(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            self._thread.wait(2000)
        self._thread = None

    def _open_key(self) -> None:
        if self._open_key_settings is not None:
            self._open_key_settings()
        else:
            self._open_settings(False)

    def _push(self, text: str,
              buttons: List[Tuple[str, Callable[[], None]]]) -> None:
        bar = self.iface.messageBar()
        widget = bar.createMessage("Waveshed", text)
        for button_text, on_click in buttons:
            button = QPushButton(widget)
            button.setText(button_text)
            button.clicked.connect(lambda *_, f=on_click: f())
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


#: Button label per message action (``binary_manager.message_actions``).
_ACTION_LABELS = {
    "update": "Update engine",
    "install": "Download engine",
    "key": "Enter API key",
}


def _go(host, action: str) -> None:
    """Take the user where *action* is fixed: the download flow (which still
    fetches the signed manifest and asks for EULA consent) or the API key."""
    if action == "key":
        show_key = getattr(host, "show_key_settings", None)
        if callable(show_key):
            show_key()
            return
        host.show_engine_settings(start_download=False)
        return
    host.show_engine_settings(start_download=True)


def show_run_error(parent, title: str, message: str) -> None:
    """The run-error box: what the user needs first, the technical rest behind
    "Show Details…".

    *message* is split with :func:`binary_manager.split_details`. Every fix
    its summary names (:func:`binary_manager.message_actions`: install /
    update the engine, enter an API key) gets a button that goes straight
    there.
    """
    summary, details = binary_manager.split_details(message)
    actions = binary_manager.message_actions(message)
    host = find_engine_settings_host(parent) if actions else None
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle(title)
    box.setText(summary)
    if details:
        box.setDetailedText(details)
    buttons = {}
    if host is not None:
        for action in actions:
            buttons[action] = box.addButton(
                _ACTION_LABELS[action], QMessageBox.ButtonRole.AcceptRole)
    box.addButton(QMessageBox.StandardButton.Close)
    box.exec()
    clicked = box.clickedButton()
    for action, button in buttons.items():
        if clicked is button:
            _go(host, action)
            return


def setup_title(message: str) -> str:
    """The error box title for a :func:`binary_manager.setup_warning`."""
    actions = binary_manager.message_actions(message)
    if actions == ["key"]:
        return "API key needed"
    if "key" in actions:
        return "Waveshed is not set up yet"
    return "Aether engine not found"


#: QgsSettings: True once the user ticked "Don't show this again" in the
#: setup dialog. Cleared as soon as the setup is complete, so a LATER problem
#: (a key that expires, an engine folder that moves) is shown again. Only the
#: dialog on opening is silenced — the Run check and its buttons stay.
SETUP_DIALOG_OFF_KEY = "waveshed/setup_dialog_off"


def show_setup_dialog(parent) -> bool:
    """When the plugin window opens: tell the user what is not set up yet
    (engine, API key) with a button to each fix. True when it was shown.

    A dialog, not only the message bar — the bar is easy to miss. "Don't
    show this again" silences it until the setup is complete once
    (:data:`SETUP_DIALOG_OFF_KEY`).
    """
    message = binary_manager.setup_warning()
    settings = QgsSettings()
    if not message:
        settings.remove(SETUP_DIALOG_OFF_KEY)
        return False
    if str(settings.value(SETUP_DIALOG_OFF_KEY, False)).lower() in ("true", "1"):
        return False
    summary, details = binary_manager.split_details(message)
    summary = summary.replace("Then run again.", "").rstrip()
    actions = binary_manager.message_actions(message)
    host = find_engine_settings_host(parent)
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle("Set up Waveshed")
    box.setText(summary)
    if details:
        box.setDetailedText(details)
    buttons = {}
    if host is not None:
        for action in actions:
            buttons[action] = box.addButton(
                _ACTION_LABELS[action], QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
    off = QCheckBox("Don't show this again (a run still says what is missing)")
    box.setCheckBox(off)
    box.exec()
    if off.isChecked():
        settings.setValue(SETUP_DIALOG_OFF_KEY, True)
    clicked = box.clickedButton()
    for action, button in buttons.items():
        if clicked is button:
            _go(host, action)
            break
    return True
