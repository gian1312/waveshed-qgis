"""Settings dialog for the AETHER QGIS plugin.

Provides configuration for binary paths, API key management, and
runtime defaults (cache directory, VRAM/RAM budgets).
"""

from __future__ import annotations

import os
from pathlib import Path

from qgis.PyQt.QtCore import Qt, QThread, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsSettings

from ..core import api_key, binary_manager


# ---------------------------------------------------------------------------
# Background worker for binary download
# ---------------------------------------------------------------------------

class _ManifestThread(QThread):
    """Fetch the release manifest and select the platform asset off-thread."""

    ready = pyqtSignal(object, object)   # (manifest dict, asset dict)
    error = pyqtSignal(str)              # error message on failure

    def run(self) -> None:  # noqa: D401 – Qt override
        try:
            manifest = binary_manager.fetch_manifest()
            asset = binary_manager.select_asset(manifest)
            self.ready.emit(manifest, asset)
        except Exception as exc:
            self.error.emit(str(exc))


class _DownloadThread(QThread):
    """Run binary_manager.download_engine() off the main thread."""

    progress = pyqtSignal(int)        # percentage 0-100
    finished_ok = pyqtSignal(str)     # target directory on success
    finished_err = pyqtSignal(str)    # error message on failure

    def __init__(
        self,
        manifest: dict,
        asset: dict,
        target_dir: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._manifest = manifest
        self._asset = asset
        self._target_dir = target_dir

    def run(self) -> None:  # noqa: D401 – Qt override
        def _progress_cb(received: int, total: int) -> None:
            if total > 0:
                self.progress.emit(min(100, int(received * 100 / total)))

        try:
            result_dir = binary_manager.download_engine(
                self._manifest,
                self._asset,
                target_dir=self._target_dir,
                progress_cb=_progress_cb,
            )
            self.finished_ok.emit(result_dir)
        except Exception as exc:
            self.finished_err.emit(str(exc))


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------

class SettingsDialog(QDialog):
    """Waveshed plugin settings dialog."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Waveshed Settings")
        self.setMinimumWidth(560)

        self._manifest_thread: _ManifestThread | None = None
        self._download_thread: _DownloadThread | None = None
        self._settings = QgsSettings()

        # --- Main layout -----------------------------------------------------
        layout = QVBoxLayout(self)
        layout.addWidget(self._build_binaries_group())
        layout.addWidget(self._build_api_key_group())
        layout.addWidget(self._build_defaults_group())

        # --- OK / Cancel -----------------------------------------------------
        self._button_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        self._button_box.accepted.connect(self._on_accept)
        self._button_box.rejected.connect(self.reject)
        layout.addWidget(self._button_box)

        # --- Populate from current settings ----------------------------------
        self._load_settings()

    # ------------------------------------------------------------------ build
    def _build_binaries_group(self) -> QGroupBox:
        group = QGroupBox("Binaries")
        vbox = QVBoxLayout(group)

        # Binary directory row
        row = QHBoxLayout()
        row.addWidget(QLabel("Binary directory:"))
        self._binary_dir_edit = QLineEdit()
        self._binary_dir_edit.setPlaceholderText("Path to AETHER binaries")
        row.addWidget(self._binary_dir_edit)
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(self._browse_binary_dir)
        row.addWidget(btn_browse)
        vbox.addLayout(row)

        # Action buttons row
        row2 = QHBoxLayout()
        btn_detect = QPushButton("Auto-detect")
        btn_detect.clicked.connect(self._auto_detect_binaries)
        row2.addWidget(btn_detect)

        self._btn_download = QPushButton("Download Binaries")
        self._btn_download.clicked.connect(self._download_binaries)
        row2.addWidget(self._btn_download)
        row2.addStretch()
        vbox.addLayout(row2)

        # Progress bar (hidden by default)
        self._download_progress = QProgressBar()
        self._download_progress.setRange(0, 100)
        self._download_progress.setVisible(False)
        vbox.addWidget(self._download_progress)

        # Status labels
        self._binary_status_label = QLabel()
        vbox.addWidget(self._binary_status_label)

        self._prereq_label = QLabel()
        self._prereq_label.setWordWrap(True)
        vbox.addWidget(self._prereq_label)

        return group

    def _build_api_key_group(self) -> QGroupBox:
        group = QGroupBox("API Key")
        vbox = QVBoxLayout(group)

        # Key input row
        row = QHBoxLayout()
        row.addWidget(QLabel("API key:"))
        self._api_key_edit = QLineEdit()
        self._api_key_edit.setEchoMode(QLineEdit.Password)
        self._api_key_edit.setPlaceholderText("Paste your Waveshed API key")
        row.addWidget(self._api_key_edit)

        self._btn_show_key = QPushButton("Show")
        self._btn_show_key.setCheckable(True)
        self._btn_show_key.toggled.connect(self._toggle_key_visibility)
        row.addWidget(self._btn_show_key)
        vbox.addLayout(row)

        # Status label
        self._api_status_label = QLabel()
        vbox.addWidget(self._api_status_label)

        # Action buttons
        row2 = QHBoxLayout()
        btn_validate = QPushButton("Save")
        btn_validate.clicked.connect(self._validate_api_key)
        row2.addWidget(btn_validate)

        btn_register = QPushButton("Get API Key")
        btn_register.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(api_key.GET_API_KEY_URL))
        )
        row2.addWidget(btn_register)

        btn_fingerprint = QPushButton("Show machine fingerprint")
        btn_fingerprint.setToolTip(
            "Read this machine's hardware fingerprint, needed to request a "
            "machine-locked license key."
        )
        btn_fingerprint.clicked.connect(self._show_machine_fingerprint)
        row2.addWidget(btn_fingerprint)
        row2.addStretch()
        vbox.addLayout(row2)

        return group

    def _build_defaults_group(self) -> QGroupBox:
        group = QGroupBox("Defaults")
        vbox = QVBoxLayout(group)

        # Local terrain directory (GeoTIFF/DEM files)
        row_terrain = QHBoxLayout()
        row_terrain.addWidget(QLabel("Local terrain dir:"))
        self._terrain_dir_edit = QLineEdit()
        self._terrain_dir_edit.setPlaceholderText("Optional: directory with GeoTIFF/DEM files")
        row_terrain.addWidget(self._terrain_dir_edit)
        btn_terrain = QPushButton("Browse...")
        btn_terrain.clicked.connect(self._browse_terrain_dir)
        row_terrain.addWidget(btn_terrain)
        vbox.addLayout(row_terrain)

        # Terrain cache directory
        row = QHBoxLayout()
        row.addWidget(QLabel("Terrain cache:"))
        self._cache_dir_edit = QLineEdit()
        self._cache_dir_edit.setPlaceholderText("~/.aether/cache/")
        row.addWidget(self._cache_dir_edit)
        btn_browse = QPushButton("Browse...")
        btn_browse.clicked.connect(self._browse_cache_dir)
        row.addWidget(btn_browse)
        vbox.addLayout(row)

        # VRAM budget
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Max VRAM budget (GB):"))
        self._vram_spin = QSpinBox()
        self._vram_spin.setRange(1, 64)
        self._vram_spin.setValue(8)
        row2.addWidget(self._vram_spin)
        row2.addStretch()
        vbox.addLayout(row2)

        # RAM budget
        row3 = QHBoxLayout()
        row3.addWidget(QLabel("Max RAM budget (GB):"))
        self._ram_spin = QSpinBox()
        self._ram_spin.setRange(1, 256)
        self._ram_spin.setValue(16)
        row3.addWidget(self._ram_spin)
        row3.addStretch()
        vbox.addLayout(row3)

        # Download connections
        row4 = QHBoxLayout()
        row4.addWidget(QLabel("Download connections:"))
        self._conn_spin = QSpinBox()
        self._conn_spin.setRange(16, 1024)
        self._conn_spin.setValue(256)
        row4.addWidget(self._conn_spin)
        row4.addStretch()
        vbox.addLayout(row4)

        return group

    # ------------------------------------------------------------- settings IO
    def _load_settings(self) -> None:
        """Populate widgets from QgsSettings."""
        s = self._settings

        # Binaries
        self._binary_dir_edit.setText(s.value("waveshed/binary_dir", ""))
        self._refresh_binary_status()

        # API key
        self._api_key_edit.setText(api_key.get_stored_key())
        self._refresh_api_status()

        # Defaults
        self._terrain_dir_edit.setText(s.value("waveshed/terrain_dir", ""))
        default_cache = os.path.join(str(Path.home()), ".aether", "cache")
        self._cache_dir_edit.setText(s.value("waveshed/cache_dir", default_cache))
        self._vram_spin.setValue(int(s.value("waveshed/max_vram_gb", 8)))
        self._ram_spin.setValue(int(s.value("waveshed/max_ram_gb", 16)))
        self._conn_spin.setValue(int(s.value("waveshed/download_connections", 256)))

    def save_settings(self) -> None:
        """Persist all settings to QgsSettings. Always saves, no validation dialogs."""
        s = self._settings
        s.setValue("waveshed/binary_dir", self._binary_dir_edit.text().strip())
        s.setValue("waveshed/terrain_dir", self._terrain_dir_edit.text().strip())
        s.setValue("waveshed/cache_dir", self._cache_dir_edit.text().strip())
        s.setValue("waveshed/max_vram_gb", self._vram_spin.value())
        s.setValue("waveshed/max_ram_gb", self._ram_spin.value())
        s.setValue("waveshed/download_connections", self._conn_spin.value())
        api_key.store_key(self._api_key_edit.text().strip())

    def _on_accept(self) -> None:
        """Save and close (standalone dialog mode)."""
        self.save_settings()
        if self.windowFlags() & Qt.Dialog:
            self.accept()

    # ------------------------------------------------------ binary helpers
    def _browse_binary_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select Binary Directory", self._binary_dir_edit.text()
        )
        if path:
            self._binary_dir_edit.setText(path)
            self._refresh_binary_status()

    def _auto_detect_binaries(self) -> None:
        detected = binary_manager.discover_binary_dir()
        if detected:
            self._binary_dir_edit.setText(detected)
            self._refresh_binary_status()
        else:
            self._binary_status_label.setText("Binaries: Not found")
            self._binary_status_label.setStyleSheet("color: red;")

    def _download_binaries(self) -> None:
        if self._manifest_thread is not None and self._manifest_thread.isRunning():
            return  # already checking
        if self._download_thread is not None and self._download_thread.isRunning():
            return  # already downloading

        # Step 1: fetch the manifest off-thread. Consent + download follow in
        # _on_manifest_ready once we know the engine version and EULA URL.
        self._btn_download.setEnabled(False)
        self._binary_status_label.setText(
            "Checking waveshed.io for the latest Aether engine..."
        )
        self._binary_status_label.setStyleSheet("color: gray;")

        self._manifest_thread = _ManifestThread(parent=self)
        self._manifest_thread.ready.connect(self._on_manifest_ready)
        self._manifest_thread.error.connect(self._on_download_err)
        self._manifest_thread.start()

    def _on_manifest_ready(self, manifest: dict, asset: dict) -> None:
        version = manifest.get("version", "?")
        eula_url = manifest.get("eula_url", "")

        # Advisory: the engine release may want a newer plugin. Allow proceeding.
        if binary_manager.is_plugin_outdated(manifest.get("min_plugin_version")):
            QMessageBox.warning(
                self, "Plugin update recommended",
                f"This Aether engine release recommends Waveshed plugin "
                f"{manifest.get('min_plugin_version')} or newer. You can "
                f"continue, but updating the plugin is advised for full "
                f"compatibility.",
            )

        # Mandatory consent to the proprietary engine EULA before any download.
        if not self._confirm_engine_download(version, eula_url):
            self._btn_download.setEnabled(True)
            self._refresh_binary_status()
            return

        target = self._binary_dir_edit.text().strip() or binary_manager.DEFAULT_INSTALL_DIR
        self._download_progress.setValue(0)
        self._download_progress.setVisible(True)

        self._download_thread = _DownloadThread(manifest, asset, target, parent=self)
        self._download_thread.progress.connect(self._download_progress.setValue)
        self._download_thread.finished_ok.connect(self._on_download_ok)
        self._download_thread.finished_err.connect(self._on_download_err)
        self._download_thread.start()

    def _confirm_engine_download(self, version: str, eula_url: str) -> bool:
        """Modal consent dialog for the proprietary Aether engine download.

        Nothing about the consent is persisted — it is requested every time.
        """
        dlg = QDialog(self)
        dlg.setWindowTitle("Download Aether engine")
        vbox = QVBoxLayout(dlg)

        msg = QLabel(
            f"Waveshed will download the proprietary <b>Aether engine "
            f"(version {version})</b> from waveshed.io.<br><br>"
            f"The Aether engine is closed-source software, separate from this "
            f"GPL-licensed plugin, and is provided under its own End User "
            f"License Agreement (EULA). By choosing <b>Accept &amp; Download</b> "
            f"you agree to that EULA."
        )
        msg.setWordWrap(True)
        msg.setTextFormat(Qt.RichText)
        vbox.addWidget(msg)

        if eula_url:
            link = QLabel(f'<a href="{eula_url}">Read the Aether engine EULA</a>')
            link.setTextFormat(Qt.RichText)
            link.setOpenExternalLinks(True)
            vbox.addWidget(link)

        buttons = QDialogButtonBox()
        buttons.addButton("Accept && Download", QDialogButtonBox.AcceptRole)
        buttons.addButton("Cancel", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        vbox.addWidget(buttons)

        return dlg.exec_() == QDialog.Accepted

    def _on_download_ok(self, result_dir: str) -> None:
        self._download_progress.setVisible(False)
        self._btn_download.setEnabled(True)
        self._binary_dir_edit.setText(result_dir)
        self._refresh_binary_status()
        QMessageBox.information(
            self, "Download Complete",
            f"Aether engine installed to:\n{result_dir}",
        )

    def _on_download_err(self, error: str) -> None:
        self._download_progress.setVisible(False)
        self._btn_download.setEnabled(True)
        self._refresh_binary_status()
        QMessageBox.critical(self, "Download Failed", error)

    def _refresh_binary_status(self) -> None:
        """Update the binary-status and prerequisites labels."""
        binary_dir = self._binary_dir_edit.text().strip()

        if not binary_dir or not os.path.isdir(binary_dir):
            self._binary_status_label.setText("Binaries: Missing")
            self._binary_status_label.setStyleSheet("color: red;")
            self._prereq_label.setText("")
            return

        found = sum(
            1 for b in binary_manager.REQUIRED_BINARIES
            if binary_manager._dir_has_binary(binary_dir, b)
        )
        total = len(binary_manager.REQUIRED_BINARIES)

        if found == total:
            self._binary_status_label.setText(f"Binaries: Found ({found}/{total})")
            self._binary_status_label.setStyleSheet("color: green;")
        else:
            self._binary_status_label.setText(f"Binaries: Missing ({found}/{total} found)")
            self._binary_status_label.setStyleSheet("color: red;")

        issues = binary_manager.check_prerequisites(binary_dir)
        if issues:
            self._prereq_label.setText("\n".join(issues))
            self._prereq_label.setStyleSheet("color: orange;")
        else:
            self._prereq_label.setText("")

    # ------------------------------------------------------- API key helpers
    def _toggle_key_visibility(self, checked: bool) -> None:
        if checked:
            self._api_key_edit.setEchoMode(QLineEdit.Normal)
            self._btn_show_key.setText("Hide")
        else:
            self._api_key_edit.setEchoMode(QLineEdit.Password)
            self._btn_show_key.setText("Show")

    def _validate_api_key(self) -> None:
        key_text = self._api_key_edit.text().strip()
        is_valid, message, _expiry = api_key.validate_api_key(key_text)

        self._api_status_label.setText(message)
        if is_valid:
            self._api_status_label.setStyleSheet("color: green;")
        else:
            self._api_status_label.setStyleSheet("color: red;")

    def _show_machine_fingerprint(self) -> None:
        """Read and display this machine's Aether engine fingerprint.

        Off the main flow — nothing is persisted. If the engine binaries are
        not installed the user is nudged to download them first; any probe
        failure surfaces as an error dialog rather than crashing QGIS.
        """
        if binary_manager.discover_binary_dir() is None:
            QMessageBox.information(
                self,
                "Aether engine not installed",
                "The Aether engine binaries are not installed yet. Use "
                "'Download Binaries' above (or set the binary directory), "
                "then try again.",
            )
            return

        try:
            fingerprint = binary_manager.read_machine_fingerprint()
        except Exception as exc:  # noqa: BLE001 - must never crash QGIS
            QMessageBox.critical(
                self,
                "Could not read machine fingerprint",
                f"Failed to read this machine's fingerprint:\n\n{exc}",
            )
            return

        # Copyable text field so the user can select and copy the value.
        QInputDialog.getText(
            self,
            "Machine fingerprint",
            "Send this fingerprint to get a machine-locked license key:",
            QLineEdit.Normal,
            fingerprint,
        )

    def _refresh_api_status(self) -> None:
        """Update the API-key status label from the current field value."""
        key_text = self._api_key_edit.text().strip()

        if not key_text:
            self._api_status_label.setText("Not set")
            self._api_status_label.setStyleSheet("color: gray;")
            return

        is_valid, message, expiry = api_key.validate_api_key(key_text)
        self._api_status_label.setText(message)
        if is_valid:
            self._api_status_label.setStyleSheet("color: green;")
        elif expiry is not None:
            # Expired key -- message already says "expired"
            self._api_status_label.setStyleSheet("color: red;")
        else:
            self._api_status_label.setStyleSheet("color: red;")

    # ---------------------------------------------------- defaults helpers
    def _browse_terrain_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select Local Terrain Directory", self._terrain_dir_edit.text()
        )
        if path:
            self._terrain_dir_edit.setText(path)

    def _browse_cache_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select Terrain Cache Directory", self._cache_dir_edit.text()
        )
        if path:
            self._cache_dir_edit.setText(path)
