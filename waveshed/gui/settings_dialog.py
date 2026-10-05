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
    QCheckBox,
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
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)
from qgis.core import Qgis, QgsMessageLog, QgsSettings

from ..core import api_key, binary_manager, eula
from ..core.attribution import attribution_lines


# ---------------------------------------------------------------------------
# Background worker for binary download
# ---------------------------------------------------------------------------

class _ManifestThread(QThread):
    """Fetch the (signature-checked) release manifest and select the platform
    asset off-thread. ``fetch_eula=False`` skips the EULA text (update check)."""

    ready = pyqtSignal(object, object, str)  # (manifest, asset, EULA text or "")
    error = pyqtSignal(str)                   # error message on failure

    def __init__(self, parent: QWidget | None = None, fetch_eula: bool = True) -> None:
        super().__init__(parent)
        self._fetch_eula = fetch_eula

    def run(self) -> None:  # noqa: D401 – Qt override
        try:
            manifest = binary_manager.fetch_manifest()
            asset = binary_manager.select_asset(manifest)
        except Exception as exc:
            self.error.emit(str(exc))
            return
        if not self._fetch_eula:
            self.ready.emit(manifest, asset, "")
            return
        # The canonical EULA text is shown in the consent dialog. A failed
        # fetch is not fatal: the dialog falls back to the key points + link.
        try:
            eula_text = eula.fetch_eula_text(manifest.get("eula_url") or eula.EULA_URL)
        except Exception as exc:  # noqa: BLE001 – fall back to the link
            QgsMessageLog.logMessage(
                f"Could not load the engine EULA text: {exc}",
                binary_manager.TAG, Qgis.MessageLevel.Warning,
            )
            eula_text = ""
        self.ready.emit(manifest, asset, eula_text)


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


class _FingerprintThread(QThread):
    """Read this machine's Aether fingerprint off the main thread.

    ``binary_manager.read_machine_fingerprint()`` spawns ``aether_core``
    (and, on Windows, ``reg query``) and can block for up to ~10s, so it must
    not run on the UI thread or the dialog freezes.
    """

    finished_ok = pyqtSignal(str)     # 64-char hex fingerprint on success
    finished_err = pyqtSignal(str)    # error message on failure

    def run(self) -> None:  # noqa: D401 – Qt override
        try:
            fingerprint = binary_manager.read_machine_fingerprint()
            self.finished_ok.emit(fingerprint)
        except Exception as exc:  # noqa: BLE001 – surface any failure to the UI
            self.finished_err.emit(str(exc))


# ---------------------------------------------------------------------------
# Dialog
# ---------------------------------------------------------------------------

#: Every QgsSettings key this dialog writes — the change-detection snapshot
#: reads exactly these, so a save that alters none of them emits no signal.
_WATCHED_KEYS = (
    "waveshed/binary_dir",
    "waveshed/terrain_dir",
    "waveshed/cache_dir",
    "waveshed/max_vram_gb",
    "waveshed/max_ram_gb",
    "waveshed/download_connections",
    binary_manager.UPDATE_CHECK_KEY,
)


class SettingsDialog(QDialog):
    """Waveshed plugin settings dialog."""

    #: Emitted after ``save_settings`` when at least one stored value actually
    #: changed. The main dialog fans this out to every open tab's
    #: ``refresh_settings()`` so dropdowns/paths/status derived from settings
    #: never go stale while the plugin dialog stays open.
    settings_changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Waveshed Settings")
        self.setMinimumWidth(560)

        self._manifest_thread: _ManifestThread | None = None
        self._update_thread: _ManifestThread | None = None
        #: Engine version published in the last manifest seen, for the label.
        self._available_version: str | None = None
        self._download_thread: _DownloadThread | None = None
        self._fingerprint_thread: _FingerprintThread | None = None
        self._settings = QgsSettings()

        # --- Main layout -----------------------------------------------------
        layout = QVBoxLayout(self)
        layout.addWidget(self._build_binaries_group())
        layout.addWidget(self._build_api_key_group())
        layout.addWidget(self._build_defaults_group())
        layout.addWidget(self._build_attribution_group())

        # --- OK / Cancel -----------------------------------------------------
        self._button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
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

        self._btn_check_update = QPushButton("Check for updates")
        self._btn_check_update.setToolTip(
            "Compare the installed Aether engine with the latest release "
            "(nothing is downloaded)."
        )
        self._btn_check_update.clicked.connect(self._check_for_updates)
        row2.addWidget(self._btn_check_update)
        row2.addStretch()
        vbox.addLayout(row2)

        # Installed vs. published engine version
        self._engine_version_label = QLabel()
        vbox.addWidget(self._engine_version_label)

        self._update_check_box = QCheckBox(
            "Check for engine updates when QGIS starts"
        )
        self._update_check_box.setToolTip(
            "Once per QGIS session, compare the installed engine with the "
            "latest release and show a notice when a newer one exists. "
            "Nothing is downloaded without your confirmation."
        )
        vbox.addWidget(self._update_check_box)

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
        self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
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

        self._btn_fingerprint = QPushButton("Show machine fingerprint")
        self._btn_fingerprint.setToolTip(
            "Read this machine's hardware fingerprint, needed to request a "
            "machine-locked license key."
        )
        self._btn_fingerprint.clicked.connect(self._show_machine_fingerprint)
        row2.addWidget(self._btn_fingerprint)
        row2.addStretch()
        vbox.addLayout(row2)

        return group

    def _build_attribution_group(self) -> QGroupBox:
        """Credits for the data the plugin downloads on the user's behalf.

        Several sources are ODbL, which obliges us to name them somewhere the
        user can actually see — the plugin's own GPL notice does not cover
        third-party data.
        """
        group = QGroupBox("Data sources")
        vbox = QVBoxLayout(group)

        intro = QLabel(
            "Terrain and building data are downloaded from third parties and "
            "remain under their own licences:"
        )
        intro.setWordWrap(True)
        vbox.addWidget(intro)

        for line in attribution_lines():
            lbl = QLabel("• " + line)
            lbl.setWordWrap(True)
            lbl.setStyleSheet("color: gray; font-size: 11px;")
            vbox.addWidget(lbl)

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
        btn_clear_cache = QPushButton("Clear...")
        btn_clear_cache.setToolTip(
            "Delete every cached .abt terrain tile.\n"
            "The cache never evicts anything on its own, so this is how you "
            "reclaim the space — and how you pick up refreshed OpenStreetMap "
            "building data, which is otherwise cached indefinitely."
        )
        btn_clear_cache.clicked.connect(self._clear_cache)
        row.addWidget(btn_clear_cache)
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
        self._update_check_box.setChecked(binary_manager.auto_update_check_enabled())
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

    def _snapshot(self) -> dict:
        """Current stored values of everything this dialog writes.

        String-normalized so a type round-trip through QgsSettings (int in,
        str out) cannot fake a change.
        """
        snap = {k: str(self._settings.value(k, "")) for k in _WATCHED_KEYS}
        snap["api_key"] = api_key.get_stored_key()
        return snap

    def save_settings(self) -> None:
        """Persist all settings to QgsSettings. Always saves, no validation dialogs.

        Emits :attr:`settings_changed` when a stored value actually changed,
        and re-checks the binary status so an invalid directory is called out
        immediately (fail loudly — never silently keep an old discovery
        result).
        """
        before = self._snapshot()
        s = self._settings
        s.setValue("waveshed/binary_dir", self._binary_dir_edit.text().strip())
        s.setValue("waveshed/terrain_dir", self._terrain_dir_edit.text().strip())
        s.setValue("waveshed/cache_dir", self._cache_dir_edit.text().strip())
        s.setValue("waveshed/max_vram_gb", self._vram_spin.value())
        s.setValue("waveshed/max_ram_gb", self._ram_spin.value())
        s.setValue("waveshed/download_connections", self._conn_spin.value())
        s.setValue(binary_manager.UPDATE_CHECK_KEY, bool(self._update_check_box.isChecked()))
        api_key.store_key(self._api_key_edit.text().strip())

        # The saved binary dir is what runs use from now on — re-validate it
        # NOW so "Binaries: Missing" appears the moment a bad path is saved.
        self._refresh_binary_status()

        if self._snapshot() != before:
            self.settings_changed.emit()

    def _on_accept(self) -> None:
        """Save and close (standalone dialog mode)."""
        self.save_settings()
        if self.windowFlags() & Qt.WindowType.Dialog:
            self.accept()

    # ------------------------------------------------------ binary helpers
    def _browse_binary_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Select Binary Directory", self._binary_dir_edit.text()
        )
        if path:
            # macOS: a browser-downloaded copy is quarantined — fix it now.
            binary_manager.prepare_engine_dir(path)
            self._binary_dir_edit.setText(path)
            self._refresh_binary_status()

    def _auto_detect_binaries(self) -> None:
        detected = binary_manager.discover_binary_dir()
        if detected:
            binary_manager.prepare_engine_dir(detected)
            self._binary_dir_edit.setText(detected)
            self._refresh_binary_status()
        else:
            self._binary_status_label.setText("Binaries: Not found")
            self._binary_status_label.setStyleSheet("color: red;")

    def start_engine_download(self) -> None:
        """Start the download flow (manifest -> EULA consent -> download).

        Public entry point for the startup update notice and the "Update
        engine" button on run errors.
        """
        self._download_binaries()

    def _check_for_updates(self) -> None:
        """Fetch the signed manifest and show the published engine version."""
        if self._update_thread is not None and self._update_thread.isRunning():
            return
        self._btn_check_update.setEnabled(False)
        self._engine_version_label.setText("Checking waveshed.io for engine updates...")
        self._engine_version_label.setStyleSheet("color: gray;")
        self._update_thread = _ManifestThread(parent=self, fetch_eula=False)
        self._update_thread.ready.connect(self._on_update_info)
        self._update_thread.error.connect(self._on_update_err)
        self._update_thread.start()

    def _on_update_info(self, manifest: dict, _asset: dict, _eula: str = "") -> None:
        self._btn_check_update.setEnabled(True)
        self._available_version = str(manifest.get("version", "")) or None
        self._refresh_engine_version()

    def _on_update_err(self, error: str) -> None:
        self._btn_check_update.setEnabled(True)
        self._refresh_engine_version()
        QMessageBox.warning(self, "Update check failed", error)

    def _refresh_engine_version(self) -> None:
        """Show "installed X / available Y" for the configured engine dir."""
        binary_dir = self._binary_dir_edit.text().strip()
        installed = None
        if binary_dir and os.path.isdir(binary_dir) and binary_manager._dir_has_binary(
                binary_dir, "aether_core"):
            installed = binary_manager.probe_engine_version(binary_dir)
        available = self._available_version
        text = (f"Installed engine: {installed or 'unknown'}"
                f"  /  Latest release: {available or 'not checked'}")
        style = "color: gray;"
        if available and (installed is None
                          or binary_manager.compare_versions(available, installed) > 0):
            text += "  \u2014 update available (Download Binaries)"
            style = "color: #b35900;"
        self._engine_version_label.setText(text)
        self._engine_version_label.setStyleSheet(style)

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

    def _on_manifest_ready(self, manifest: dict, asset: dict, eula_text: str = "") -> None:
        version = manifest.get("version", "?")
        self._available_version = str(manifest.get("version", "")) or None
        eula_url = manifest.get("eula_url", "") or eula.EULA_URL

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
        if not self._confirm_engine_download(version, eula_url, eula_text):
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

    def _confirm_engine_download(
        self, version: str, eula_url: str, eula_text: str = ""
    ) -> bool:
        """Modal consent dialog for the proprietary Aether engine download.

        Shows a highlighted key-points summary (``core.eula.KEY_POINTS``) and
        below it the canonical EULA text fetched from *eula_url*, which
        governs. Consent is requested every time; the accepted EULA version is
        recorded in settings.
        """
        text_version = eula.parse_eula_version(eula_text) if eula_text else None

        dlg = QDialog(self)
        dlg.setWindowTitle("Download Aether engine")
        dlg.resize(680, 640)
        vbox = QVBoxLayout(dlg)

        msg = QLabel(
            f"Waveshed will download the proprietary <b>Aether engine "
            f"(version {version})</b> from waveshed.io.<br><br>"
            f"The Aether engine is closed-source software, separate from this "
            f"GPL-licensed plugin, and is provided under its own End User "
            f"License Agreement (EULA). By choosing <b>Accept &amp; Download</b> "
            f"you agree to that EULA"
            + (f" (version {text_version})." if text_version else ".")
        )
        msg.setWordWrap(True)
        msg.setTextFormat(Qt.TextFormat.RichText)
        vbox.addWidget(msg)

        points = QLabel(eula.key_points_html())
        points.setWordWrap(True)
        points.setTextFormat(Qt.TextFormat.RichText)
        points.setStyleSheet(
            "QLabel { background-color: #fff4ce; color: #1f1f1f; "
            "border: 1px solid #d9a400; border-radius: 4px; padding: 8px; }"
        )
        vbox.addWidget(points)

        if text_version and text_version != eula.SUMMARY_EULA_VERSION:
            note = QLabel(
                f"Note: this summary was written for EULA version "
                f"{eula.SUMMARY_EULA_VERSION}; the full text below is version "
                f"{text_version} and governs."
            )
            note.setWordWrap(True)
            note.setStyleSheet("color: #b35900;")
            vbox.addWidget(note)

        if eula_text:
            full = QTextBrowser()
            full.setOpenExternalLinks(True)
            if hasattr(full, "setMarkdown"):
                full.setMarkdown(eula_text)
            else:
                full.setPlainText(eula_text)
            full.setMinimumHeight(260)
            vbox.addWidget(full, 1)
        else:
            missing = QLabel(
                "The full EULA text could not be loaded here. Please read it "
                "using the link below before accepting — the full text governs."
            )
            missing.setWordWrap(True)
            missing.setStyleSheet("color: #b35900;")
            vbox.addWidget(missing)

        if eula_url:
            link = QLabel(f'<a href="{eula_url}">Open the Aether engine EULA in your browser</a>')
            link.setTextFormat(Qt.TextFormat.RichText)
            link.setOpenExternalLinks(True)
            vbox.addWidget(link)

        buttons = QDialogButtonBox()
        buttons.addButton("Accept && Download", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        vbox.addWidget(buttons)

        accepted = dlg.exec() == QDialog.DialogCode.Accepted
        if accepted:
            eula.record_acceptance(
                text_version
                or f"{eula.SUMMARY_EULA_VERSION} (summary shown; full text not loaded)"
            )
        return accepted

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

        if hasattr(self, "_engine_version_label"):
            self._refresh_engine_version()

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
            self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Normal)
            self._btn_show_key.setText("Hide")
        else:
            self._api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
            self._btn_show_key.setText("Show")

    def _validate_api_key(self) -> None:
        """Validate the entered key and, when valid, persist it immediately.

        This handler is wired to the "Save" button, so a successful validation
        must write the key to settings. Otherwise a user who clicks "Save" but
        never clicks the dialog's OK button would lose the key and hit
        "API key required" at run time. ``store_key`` normalizes the value
        (removing any whitespace picked up from a wrapped terminal copy) before
        writing, so the stored key is always clean.
        """
        key_text = self._api_key_edit.text().strip()
        is_valid, message, _expiry = api_key.validate_api_key(key_text)

        if is_valid:
            api_key.store_key(key_text)
            self._api_status_label.setText("✓ Key saved")
            self._api_status_label.setStyleSheet("color: green;")
        else:
            self._api_status_label.setText(message)
            self._api_status_label.setStyleSheet("color: red;")

    def _show_machine_fingerprint(self) -> None:
        """Read and display this machine's Aether engine fingerprint.

        The probe (``aether_core --fingerprint``; on Windows it also spawns
        ``reg query``) can block for up to ~10s, so it runs on a background
        thread to keep the dialog responsive instead of freezing it. While the
        read is in flight the button is disabled and shows a "Reading…" state;
        it is always restored by the completion slots. Off the main flow —
        nothing is persisted. If the engine binaries are not installed the user
        is nudged to download them first; any probe failure surfaces as an
        error dialog rather than crashing QGIS.
        """
        if self._fingerprint_thread is not None and self._fingerprint_thread.isRunning():
            return  # a read is already in progress

        if binary_manager.discover_binary_dir() is None:
            QMessageBox.information(
                self,
                "Aether engine not installed",
                "The Aether engine binaries are not installed yet. Use "
                "'Download Binaries' above (or set the binary directory), "
                "then try again.",
            )
            return

        # Immediate, visible feedback: disable and relabel the button so the
        # user sees the read is under way while the dialog stays responsive.
        self._btn_fingerprint.setEnabled(False)
        self._btn_fingerprint.setText("Reading…")

        self._fingerprint_thread = _FingerprintThread(parent=self)
        self._fingerprint_thread.finished_ok.connect(self._on_fingerprint_ok)
        self._fingerprint_thread.finished_err.connect(self._on_fingerprint_err)
        self._fingerprint_thread.start()

    def _restore_fingerprint_button(self) -> None:
        """Re-enable and relabel the fingerprint button.

        Always invoked from the completion slots so the button never gets stuck
        in the disabled "Reading…" state.
        """
        self._btn_fingerprint.setEnabled(True)
        self._btn_fingerprint.setText("Show machine fingerprint")

    def _on_fingerprint_ok(self, fingerprint: str) -> None:
        self._restore_fingerprint_button()
        # Copyable text field so the user can select and copy the value.
        QInputDialog.getText(
            self,
            "Machine fingerprint",
            "Send this fingerprint to get a machine-locked license key:",
            QLineEdit.EchoMode.Normal,
            fingerprint,
        )

    def _on_fingerprint_err(self, error: str) -> None:
        # Preserves read_machine_fingerprint's clear messages (incl. the
        # "engine too old" path) — they arrive here as the error string.
        self._restore_fingerprint_button()
        QMessageBox.critical(
            self,
            "Could not read machine fingerprint",
            f"Failed to read this machine's fingerprint:\n\n{error}",
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

    def _clear_cache(self) -> None:
        """Delete every terrain cache entry, after confirming the size."""
        from ..core import terrain_adapter

        # Read against the saved cache path, not whatever is in the edit box:
        # an unsaved path change would otherwise clear a directory the user is
        # only considering.
        entries = terrain_adapter.cache_entries()
        if not entries:
            QMessageBox.information(
                self, "Clear Terrain Cache",
                f"No cached terrain found in\n{terrain_adapter.get_cache_dir()}",
            )
            return

        size_mb = terrain_adapter.cache_size_bytes() / 1e6
        answer = QMessageBox.question(
            self, "Clear Terrain Cache",
            f"Delete {len(entries)} cached terrain set(s), freeing "
            f"{size_mb:,.0f} MB?\n\n{terrain_adapter.get_cache_dir()}\n\n"
            "Terrain will be re-downloaded or rebuilt on the next run.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        removed, freed = terrain_adapter.clear_cache()
        if removed < len(entries):
            QMessageBox.warning(
                self, "Clear Terrain Cache",
                f"Removed {removed} of {len(entries)} cached terrain set(s), "
                f"freeing {freed / 1e6:,.0f} MB.\nThe rest could not be "
                "deleted — they may be open in QGIS or another program.",
            )
        else:
            QMessageBox.information(
                self, "Clear Terrain Cache",
                f"Removed {removed} cached terrain set(s), freeing "
                f"{freed / 1e6:,.0f} MB.",
            )
