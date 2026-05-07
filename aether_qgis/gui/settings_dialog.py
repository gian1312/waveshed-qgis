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

class _DownloadThread(QThread):
    """Run binary_manager.download_binaries() off the main thread."""

    progress = pyqtSignal(int)        # percentage 0-100
    finished_ok = pyqtSignal(str)     # target directory on success
    finished_err = pyqtSignal(str)    # error message on failure

    def __init__(self, target_dir: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._target_dir = target_dir

    def run(self) -> None:  # noqa: D401 – Qt override
        def _progress_cb(block_num: int, block_size: int, total_size: int) -> None:
            if total_size > 0:
                pct = min(100, int(block_num * block_size * 100 / total_size))
                self.progress.emit(pct)

        try:
            result_dir = binary_manager.download_binaries(
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
    """AETHER plugin settings dialog."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AETHER Settings")
        self.setMinimumWidth(560)

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
        self._api_key_edit.setPlaceholderText("Paste your AETHER API key")
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
        btn_validate = QPushButton("Validate")
        btn_validate.clicked.connect(self._validate_api_key)
        row2.addWidget(btn_validate)

        btn_register = QPushButton("Get API Key")
        btn_register.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl("https://aether-rf.com/register"))
        )
        row2.addWidget(btn_register)
        row2.addStretch()
        vbox.addLayout(row2)

        return group

    def _build_defaults_group(self) -> QGroupBox:
        group = QGroupBox("Defaults")
        vbox = QVBoxLayout(group)

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

        return group

    # ------------------------------------------------------------- settings IO
    def _load_settings(self) -> None:
        """Populate widgets from QgsSettings."""
        s = self._settings

        # Binaries
        self._binary_dir_edit.setText(s.value("aether/binary_dir", ""))
        self._refresh_binary_status()

        # API key
        self._api_key_edit.setText(api_key.get_stored_key())
        self._refresh_api_status()

        # Defaults
        default_cache = os.path.join(str(Path.home()), ".aether", "cache")
        self._cache_dir_edit.setText(s.value("aether/cache_dir", default_cache))
        self._vram_spin.setValue(int(s.value("aether/max_vram_gb", 8)))
        self._ram_spin.setValue(int(s.value("aether/max_ram_gb", 16)))

    def _on_accept(self) -> None:
        """Validate inputs and persist all settings."""
        binary_dir = self._binary_dir_edit.text().strip()

        # Validate binary directory if a path was provided
        if binary_dir:
            if not os.path.isdir(binary_dir):
                QMessageBox.warning(
                    self,
                    "Invalid Binary Directory",
                    f"The binary directory does not exist:\n{binary_dir}",
                )
                return

            found = sum(
                1 for b in binary_manager.REQUIRED_BINARIES
                if binary_manager._dir_has_binary(binary_dir, b)
            )
            if found < len(binary_manager.REQUIRED_BINARIES):
                answer = QMessageBox.question(
                    self,
                    "Missing Binaries",
                    f"Only {found}/{len(binary_manager.REQUIRED_BINARIES)} "
                    f"binaries found in:\n{binary_dir}\n\nSave anyway?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    return

        # Persist
        s = self._settings
        s.setValue("aether/binary_dir", binary_dir)
        s.setValue("aether/cache_dir", self._cache_dir_edit.text().strip())
        s.setValue("aether/max_vram_gb", self._vram_spin.value())
        s.setValue("aether/max_ram_gb", self._ram_spin.value())

        api_key.store_key(self._api_key_edit.text().strip())

        # Only call accept() when running as a standalone dialog
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
        if self._download_thread is not None and self._download_thread.isRunning():
            return  # already in progress

        target = self._binary_dir_edit.text().strip() or binary_manager.DEFAULT_INSTALL_DIR
        self._download_progress.setValue(0)
        self._download_progress.setVisible(True)
        self._btn_download.setEnabled(False)

        self._download_thread = _DownloadThread(target, parent=self)
        self._download_thread.progress.connect(self._download_progress.setValue)
        self._download_thread.finished_ok.connect(self._on_download_ok)
        self._download_thread.finished_err.connect(self._on_download_err)
        self._download_thread.start()

    def _on_download_ok(self, result_dir: str) -> None:
        self._download_progress.setVisible(False)
        self._btn_download.setEnabled(True)
        self._binary_dir_edit.setText(result_dir)
        self._refresh_binary_status()
        QMessageBox.information(
            self, "Download Complete",
            f"Binaries installed to:\n{result_dir}",
        )

    def _on_download_err(self, error: str) -> None:
        self._download_progress.setVisible(False)
        self._btn_download.setEnabled(True)
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
    def _browse_cache_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select Terrain Cache Directory", self._cache_dir_edit.text()
        )
        if path:
            self._cache_dir_edit.setText(path)
