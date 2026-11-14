"""Asset management tab widget for emitter definitions.

Embeddable QWidget (not a dialog) that provides an asset list top-bar with
a two-subtab editor below.  Users can create, edit, save, save-as, and
delete asset JSON files through this interface.

Faithful port of the MPT SIGMA emitter management tab (minus probability-of-
detection/radar parameters).  Includes inline antenna pattern tables, a
pattern generator dialog with multibeam support, a polar-plot visualiser,
automatic ERP computation, SPLAT! .az/.el file I/O, and full dirty tracking.
"""

from __future__ import annotations

import math
import os
from typing import Optional

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import Qt, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.asset_manager import (
    compute_erp,
    create_default_asset,
    delete_asset,
    get_assets_dir,
    is_in_store,
    list_assets,
    load_asset,
    save_asset,
    save_renamed_asset,
    unique_asset_path,
)
from .height_inputs import MIN_AMSL_M, bind_height_mode
from .plotting import PolarPlotWidget

#: Elevation tilt sign, said where it is typed (generator "Tilt" and the
#: Generic "Global Tilt"): the lobe is centred at +tilt, + = above the horizon.
TILT_LABEL = "Tilt (+ up):"
GLOBAL_TILT_LABEL = "Global Tilt (+ up):"
TILT_TIP = (
    "Elevation of the beam centre in degrees: positive raises the beam above "
    "the horizon, negative lowers it. A 5° downtilt is entered as −5. "
    "The tilt is baked into the pattern data."
)


def parse_pattern_file(text: str, ext: str) -> dict:
    """Parse a SPLAT!-style ``.az`` / ``.el`` file the way aether_core does.

    Line 1 is ALWAYS the header (``.az``: rotation bearing; ``.el``:
    mechanical tilt and its azimuth) — never a data point. Every line is cut
    at its first ``;`` (comment); a line that does not start with two numbers
    is skipped, as the engine skips it.

    Returns ``{"data": [[angle, gain], ...], "rotation": float, "tilt":
    float, "tilt_azimuth": float, "skipped": int}`` — the header numbers as
    read (0.0 when absent), gains as written (not clamped).
    """
    lines = text.splitlines()
    out = {"data": [], "rotation": 0.0, "tilt": 0.0, "tilt_azimuth": 0.0,
           "skipped": 0}
    if not lines:
        return out
    header = lines[0].split(";", 1)[0].split()

    def _num(i: int) -> float:
        try:
            return float(header[i])
        except (IndexError, ValueError):
            return 0.0

    if ext == "az":
        out["rotation"] = _num(0)
    else:
        out["tilt"], out["tilt_azimuth"] = _num(0), _num(1)
    for line in lines[1:]:
        body = line.split(";", 1)[0].strip()
        if not body:
            continue
        parts = body.split()
        try:
            angle, gain = float(parts[0]), float(parts[1])
        except (IndexError, ValueError):
            out["skipped"] += 1
            continue
        if not (math.isfinite(angle) and math.isfinite(gain)):
            out["skipped"] += 1
            continue
        out["data"].append([angle, gain])
    return out


def pattern_header_note(parsed: dict, ext: str) -> str:
    """What the user must be told about a loaded file's header ("" = nothing)."""
    if ext == "az":
        rot = parsed.get("rotation", 0.0)
        if rot:
            return (
                f"The file's header turns the pattern to {rot:g}°. Waveshed "
                "stores the pattern unrotated and does not apply it: set the "
                f"site's AZ Rotation to {round(rot) % 360}° (360° tab "
                "or P2P) to point the antenna there.")
        return ""
    tilt = parsed.get("tilt", 0.0)
    if tilt:
        return (
            f"The file's header has a mechanical tilt of {tilt:g}° (toward "
            f"bearing {parsed.get('tilt_azimuth', 0.0):g}°). It is NOT "
            "applied: Waveshed patterns carry the tilt in the data. To tilt the "
            "beam, use a pattern whose data is already tilted (Generate: "
            "Tilt, + up).")
    return ""


# ---------------------------------------------------------------------------
# Pattern Generator Dialog
# ---------------------------------------------------------------------------


class _PatternGeneratorDialog(QDialog):
    """Dialog for generating synthetic antenna patterns.

    Supports azimuth pattern types (Omnidirectional, Sectoral, Directional,
    Multibeam) and elevation pattern types (Isotropic, Directional, Multibeam)
    using cosine-lobe / sharpness models ported from MPT SIGMA.
    """

    def __init__(
        self,
        pattern_type: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        """Initialise the dialog.

        Args:
            pattern_type: ``"azimuth"`` or ``"elevation"``.
            parent: Parent widget.
        """
        super().__init__(parent)
        self._pattern_type = pattern_type
        self._result_data: Optional[list[list[float]]] = None
        self.isotropic_elevation_pattern: Optional[list[list[float]]] = None
        self.setWindowTitle(
            "Generate Azimuth Pattern"
            if pattern_type == "azimuth"
            else "Generate Elevation Pattern"
        )
        self.setMinimumWidth(380)
        self._build_ui()
        self._connect_signals()
        # Trigger initial page visibility.
        self._on_type_changed(0)

    # ---- UI ---------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Pattern type selector.
        type_form = QFormLayout()
        self.combo_type = QComboBox()
        if self._pattern_type == "azimuth":
            self.combo_type.addItems([
                "Omnidirectional",
                "Sectoral",
                "Directional",
                "Multibeam",
                "Isotropic (Omni 3D)",
                "Generic (Multi-Beam)",
            ])
        else:
            self.combo_type.addItems([
                "Isotropic",
                "Directional",
                "Multibeam",
                "Generic (Multi-Beam)",
            ])
        type_form.addRow("Pattern Type:", self.combo_type)
        layout.addLayout(type_form)

        # Parameter widgets (stacked so we can show/hide per type).
        self.param_stack = QStackedWidget()
        layout.addWidget(self.param_stack)

        if self._pattern_type == "azimuth":
            self._build_az_pages()
        else:
            self._build_el_pages()

        # Dialog buttons.
        btn_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btn_box.accepted.connect(self._on_accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

    # -- Azimuth pages -----------------------------------------------------

    def _build_az_pages(self) -> None:
        # Page 0: Omnidirectional -- no parameters.
        omni_page = QWidget()
        omni_layout = QVBoxLayout(omni_page)
        omni_layout.addWidget(QLabel("Uniform gain of 1.0 at all azimuths."))
        omni_layout.addStretch()
        self.param_stack.addWidget(omni_page)

        # Page 1: Sectoral.
        sect_page = QWidget()
        sect_form = QFormLayout(sect_page)
        self.spin_sect_bw = QDoubleSpinBox()
        self.spin_sect_bw.setRange(1.0, 360.0)
        self.spin_sect_bw.setDecimals(1)
        self.spin_sect_bw.setValue(90.0)
        self.spin_sect_bw.setSuffix(" deg")
        sect_form.addRow("Beamwidth:", self.spin_sect_bw)
        self.spin_sect_sl = QDoubleSpinBox()
        self.spin_sect_sl.setRange(-60.0, 0.0)
        self.spin_sect_sl.setDecimals(1)
        self.spin_sect_sl.setValue(-20.0)
        self.spin_sect_sl.setSuffix(" dB")
        sect_form.addRow("Sidelobe Level:", self.spin_sect_sl)
        self.param_stack.addWidget(sect_page)

        # Page 2: Directional.
        dir_page = QWidget()
        dir_form = QFormLayout(dir_page)
        self.spin_dir_bw = QDoubleSpinBox()
        self.spin_dir_bw.setRange(1.0, 360.0)
        self.spin_dir_bw.setDecimals(1)
        self.spin_dir_bw.setValue(30.0)
        self.spin_dir_bw.setSuffix(" deg")
        dir_form.addRow("Beamwidth:", self.spin_dir_bw)
        self.spin_dir_fb = QDoubleSpinBox()
        self.spin_dir_fb.setRange(0.0, 60.0)
        self.spin_dir_fb.setDecimals(1)
        self.spin_dir_fb.setValue(25.0)
        self.spin_dir_fb.setSuffix(" dB")
        dir_form.addRow("Front-to-Back Ratio:", self.spin_dir_fb)
        self.spin_dir_sl = QDoubleSpinBox()
        self.spin_dir_sl.setRange(-60.0, 0.0)
        self.spin_dir_sl.setDecimals(1)
        self.spin_dir_sl.setValue(-20.0)
        self.spin_dir_sl.setSuffix(" dB")
        dir_form.addRow("Sidelobe Level:", self.spin_dir_sl)
        self.param_stack.addWidget(dir_page)

        # Page 3: Multibeam.
        mb_page = QWidget()
        mb_form = QFormLayout(mb_page)
        self.spin_az_mb_beams = QSpinBox()
        self.spin_az_mb_beams.setRange(1, 12)
        self.spin_az_mb_beams.setValue(3)
        mb_form.addRow("Number of Beams:", self.spin_az_mb_beams)
        self.spin_az_mb_sep = QDoubleSpinBox()
        self.spin_az_mb_sep.setRange(1.0, 360.0)
        self.spin_az_mb_sep.setDecimals(1)
        self.spin_az_mb_sep.setValue(30.0)
        self.spin_az_mb_sep.setSuffix(" deg")
        mb_form.addRow("Separation:", self.spin_az_mb_sep)
        self.spin_az_mb_bw = QDoubleSpinBox()
        self.spin_az_mb_bw.setRange(1.0, 360.0)
        self.spin_az_mb_bw.setDecimals(1)
        self.spin_az_mb_bw.setValue(10.0)
        self.spin_az_mb_bw.setSuffix(" deg")
        mb_form.addRow("Beamwidth:", self.spin_az_mb_bw)
        self.spin_az_mb_sl = QDoubleSpinBox()
        self.spin_az_mb_sl.setRange(-60.0, 0.0)
        self.spin_az_mb_sl.setDecimals(1)
        self.spin_az_mb_sl.setValue(-20.0)
        self.spin_az_mb_sl.setSuffix(" dB")
        mb_form.addRow("Sidelobe Level:", self.spin_az_mb_sl)
        self.spin_az_mb_heading = QDoubleSpinBox()
        self.spin_az_mb_heading.setRange(0.0, 360.0)
        self.spin_az_mb_heading.setDecimals(1)
        self.spin_az_mb_heading.setValue(0.0)
        self.spin_az_mb_heading.setSuffix(" deg")
        mb_form.addRow("Global Heading:", self.spin_az_mb_heading)
        self.param_stack.addWidget(mb_page)

        # Page 4: Isotropic (Omni 3D) -- no parameters, generates both AZ and EL.
        iso3d_page = QWidget()
        iso3d_layout = QVBoxLayout(iso3d_page)
        iso3d_layout.addWidget(QLabel(
            "Isotropic (Omni 3D): Uniform gain of 1.0 in all directions.\n"
            "This also generates a matching isotropic elevation pattern."
        ))
        iso3d_layout.addStretch()
        self.param_stack.addWidget(iso3d_page)

        # Page 5: Generic (Multi-Beam).
        az_gen_page = QWidget()
        az_gen_layout = QVBoxLayout(az_gen_page)
        az_gen_form = QFormLayout()
        self.spin_az_generic_tilt = QDoubleSpinBox()
        self.spin_az_generic_tilt.setRange(0.0, 360.0)
        self.spin_az_generic_tilt.setDecimals(1)
        self.spin_az_generic_tilt.setValue(0.0)
        self.spin_az_generic_tilt.setSuffix(" deg")
        az_gen_form.addRow("Global Heading:", self.spin_az_generic_tilt)
        self.spin_az_generic_sl = QDoubleSpinBox()
        self.spin_az_generic_sl.setRange(-60.0, 0.0)
        self.spin_az_generic_sl.setDecimals(1)
        self.spin_az_generic_sl.setValue(-30.0)
        self.spin_az_generic_sl.setSuffix(" dB")
        az_gen_form.addRow("Sidelobe Floor:", self.spin_az_generic_sl)
        az_gen_layout.addLayout(az_gen_form)
        az_gen_layout.addWidget(QLabel("Beam Table:"))
        self.az_generic_table = QTableWidget(0, 3)
        self.az_generic_table.setHorizontalHeaderLabels(
            ["Center Angle (deg)", "Beamwidth (deg)", "Peak Gain (dB)"]
        )
        self.az_generic_table.horizontalHeader().setStretchLastSection(True)
        self.az_generic_table.setMinimumHeight(120)
        az_gen_layout.addWidget(self.az_generic_table)
        az_gen_btn_row = QHBoxLayout()
        self.btn_az_generic_add = QPushButton("Add Beam")
        self.btn_az_generic_remove = QPushButton("Remove Beam")
        self.btn_az_generic_add.clicked.connect(self._on_az_generic_add_beam)
        self.btn_az_generic_remove.clicked.connect(self._on_az_generic_remove_beam)
        az_gen_btn_row.addWidget(self.btn_az_generic_add)
        az_gen_btn_row.addWidget(self.btn_az_generic_remove)
        az_gen_btn_row.addStretch()
        az_gen_layout.addLayout(az_gen_btn_row)
        az_gen_layout.addStretch()
        self.param_stack.addWidget(az_gen_page)
        # Add default beam row for azimuth generic.
        self._add_generic_beam_row(self.az_generic_table, 0.0, 30.0, 0.0)

    # -- Elevation pages ---------------------------------------------------

    def _build_el_pages(self) -> None:
        # Page 0: Isotropic -- no parameters.
        iso_page = QWidget()
        iso_layout = QVBoxLayout(iso_page)
        iso_layout.addWidget(QLabel("Uniform gain of 1.0 at all elevations."))
        iso_layout.addStretch()
        self.param_stack.addWidget(iso_page)

        # Page 1: Directional.
        dir_page = QWidget()
        dir_form = QFormLayout(dir_page)
        self.spin_el_bw = QDoubleSpinBox()
        self.spin_el_bw.setRange(1.0, 180.0)
        self.spin_el_bw.setDecimals(1)
        self.spin_el_bw.setValue(10.0)
        self.spin_el_bw.setSuffix(" deg")
        dir_form.addRow("Beamwidth:", self.spin_el_bw)
        self.spin_el_tilt = QDoubleSpinBox()
        self.spin_el_tilt.setRange(-90.0, 90.0)
        self.spin_el_tilt.setDecimals(1)
        self.spin_el_tilt.setValue(0.0)
        self.spin_el_tilt.setSuffix(" deg")
        self.spin_el_tilt.setToolTip(TILT_TIP)
        dir_form.addRow(TILT_LABEL, self.spin_el_tilt)
        dir_form.labelForField(self.spin_el_tilt).setToolTip(TILT_TIP)
        self.spin_el_sl = QDoubleSpinBox()
        self.spin_el_sl.setRange(-60.0, 0.0)
        self.spin_el_sl.setDecimals(1)
        self.spin_el_sl.setValue(-20.0)
        self.spin_el_sl.setSuffix(" dB")
        dir_form.addRow("Sidelobe Level:", self.spin_el_sl)
        self.param_stack.addWidget(dir_page)

        # Page 2: Multibeam.
        mb_page = QWidget()
        mb_form = QFormLayout(mb_page)
        self.spin_el_mb_beams = QSpinBox()
        self.spin_el_mb_beams.setRange(1, 12)
        self.spin_el_mb_beams.setValue(3)
        mb_form.addRow("Number of Beams:", self.spin_el_mb_beams)
        self.spin_el_mb_sep = QDoubleSpinBox()
        self.spin_el_mb_sep.setRange(1.0, 180.0)
        self.spin_el_mb_sep.setDecimals(1)
        self.spin_el_mb_sep.setValue(30.0)
        self.spin_el_mb_sep.setSuffix(" deg")
        mb_form.addRow("Separation:", self.spin_el_mb_sep)
        self.spin_el_mb_bw = QDoubleSpinBox()
        self.spin_el_mb_bw.setRange(1.0, 180.0)
        self.spin_el_mb_bw.setDecimals(1)
        self.spin_el_mb_bw.setValue(10.0)
        self.spin_el_mb_bw.setSuffix(" deg")
        mb_form.addRow("Beamwidth:", self.spin_el_mb_bw)
        self.spin_el_mb_sl = QDoubleSpinBox()
        self.spin_el_mb_sl.setRange(-60.0, 0.0)
        self.spin_el_mb_sl.setDecimals(1)
        self.spin_el_mb_sl.setValue(-20.0)
        self.spin_el_mb_sl.setSuffix(" dB")
        mb_form.addRow("Sidelobe Level:", self.spin_el_mb_sl)
        self.param_stack.addWidget(mb_page)

        # Page 3: Generic (Multi-Beam).
        el_gen_page = QWidget()
        el_gen_layout = QVBoxLayout(el_gen_page)
        el_gen_form = QFormLayout()
        self.spin_el_generic_tilt = QDoubleSpinBox()
        self.spin_el_generic_tilt.setRange(-90.0, 90.0)
        self.spin_el_generic_tilt.setDecimals(1)
        self.spin_el_generic_tilt.setValue(0.0)
        self.spin_el_generic_tilt.setSuffix(" deg")
        self.spin_el_generic_tilt.setToolTip(TILT_TIP)
        el_gen_form.addRow(GLOBAL_TILT_LABEL, self.spin_el_generic_tilt)
        el_gen_form.labelForField(self.spin_el_generic_tilt).setToolTip(TILT_TIP)
        self.spin_el_generic_sl = QDoubleSpinBox()
        self.spin_el_generic_sl.setRange(-60.0, 0.0)
        self.spin_el_generic_sl.setDecimals(1)
        self.spin_el_generic_sl.setValue(-30.0)
        self.spin_el_generic_sl.setSuffix(" dB")
        el_gen_form.addRow("Sidelobe Floor:", self.spin_el_generic_sl)
        el_gen_layout.addLayout(el_gen_form)
        el_gen_layout.addWidget(QLabel("Beam Table:"))
        self.el_generic_table = QTableWidget(0, 3)
        self.el_generic_table.setHorizontalHeaderLabels(
            ["Center Angle (deg)", "Beamwidth (deg)", "Peak Gain (dB)"]
        )
        self.el_generic_table.horizontalHeader().setStretchLastSection(True)
        self.el_generic_table.setMinimumHeight(120)
        el_gen_layout.addWidget(self.el_generic_table)
        el_gen_btn_row = QHBoxLayout()
        self.btn_el_generic_add = QPushButton("Add Beam")
        self.btn_el_generic_remove = QPushButton("Remove Beam")
        self.btn_el_generic_add.clicked.connect(self._on_el_generic_add_beam)
        self.btn_el_generic_remove.clicked.connect(self._on_el_generic_remove_beam)
        el_gen_btn_row.addWidget(self.btn_el_generic_add)
        el_gen_btn_row.addWidget(self.btn_el_generic_remove)
        el_gen_btn_row.addStretch()
        el_gen_layout.addLayout(el_gen_btn_row)
        el_gen_layout.addStretch()
        self.param_stack.addWidget(el_gen_page)
        # Add default beam row for elevation generic.
        self._add_generic_beam_row(self.el_generic_table, 0.0, 10.0, 0.0)

    # ---- Signals ----------------------------------------------------------

    def _connect_signals(self) -> None:
        self.combo_type.currentIndexChanged.connect(self._on_type_changed)

    def _on_type_changed(self, index: int) -> None:
        self.param_stack.setCurrentIndex(index)

    # ---- Core math (ported from MPT SIGMA) --------------------------------

    def _cosine_lobe(
        self,
        target_angle: float,
        center_angle: float,
        beamwidth_deg: float,
        peak_gain: float = 1.0,
    ) -> float:
        """Compute gain via cosine-lobe model (MPT SIGMA algorithm).

        Args:
            target_angle: Angle being evaluated (degrees).
            center_angle: Beam centre angle (degrees).
            beamwidth_deg: Full beamwidth in degrees.
            peak_gain: Maximum gain value.

        Returns:
            Linear gain value.
        """
        if beamwidth_deg <= 0:
            return 0.0
        diff = (target_angle - center_angle + 180) % 360 - 180
        if abs(diff) >= 90:
            return 0.0
        rad_diff = math.radians(diff)
        bw_rad = math.radians(beamwidth_deg)
        cos_bw_half = math.cos(bw_rad / 2.0)
        if abs(cos_bw_half) < 0.0001:
            return 0.0
        n = math.log(0.5) / math.log(cos_bw_half)
        gain = (math.cos(rad_diff) ** n) * peak_gain
        return gain

    def _cosine_tapered_window(
        self,
        target_angle: float,
        center_angle: float,
        width_deg: float,
    ) -> float:
        """Compute a cosine-tapered window function.

        Args:
            target_angle: Angle being evaluated (degrees).
            center_angle: Window centre angle (degrees).
            width_deg: Full width of the window in degrees.

        Returns:
            Taper value in [0, 1].
        """
        diff = (target_angle - center_angle + 180) % 360 - 180
        if abs(diff) <= width_deg / 2.0:
            arg = diff * 90.0 / (width_deg / 2.0)
            return math.cos(math.radians(arg))
        return 0.0

    # ---- Azimuth generators -----------------------------------------------

    def _generate_az_omni(self) -> list[list[float]]:
        return [[float(a), 1.0] for a in range(361)]

    def _generate_az_sectoral(self) -> list[list[float]]:
        beamwidth = self.spin_sect_bw.value()
        sidelobe_db = self.spin_sect_sl.value()
        sidelobe_gain = 10.0 ** (sidelobe_db / 20.0)
        data: list[list[float]] = []
        for a in range(361):
            lobe = self._cosine_lobe(float(a), 0.0, beamwidth)
            gain = max(lobe, sidelobe_gain)
            data.append([float(a), round(gain, 4)])
        return data

    def _generate_az_directional(self) -> list[list[float]]:
        """MPT SIGMA directional azimuth generation."""
        beamwidth = self.spin_dir_bw.value()
        fb_ratio_db = self.spin_dir_fb.value()
        sidelobe_db = self.spin_dir_sl.value()
        backlobe_gain = 10.0 ** (-fb_ratio_db / 20.0)
        sidelobe_gain = 10.0 ** (sidelobe_db / 20.0)
        sharpness = 400.0 / (beamwidth if beamwidth > 0 else 1.0)
        data: list[list[float]] = []
        for angle in range(361):
            angle_rad = math.radians(angle)
            gain = (
                (abs(math.cos(angle_rad / 2.0)) ** sharpness)
                + (backlobe_gain * math.sin(angle_rad / 2.0) ** 2)
            )
            if gain < sidelobe_gain:
                gain = sidelobe_gain
            data.append([float(angle), round(gain, 4)])
        return data

    def _generate_az_multibeam(self) -> list[list[float]]:
        num_beams = self.spin_az_mb_beams.value()
        separation = self.spin_az_mb_sep.value()
        beamwidth = self.spin_az_mb_bw.value()
        sidelobe_db = self.spin_az_mb_sl.value()
        global_heading = self.spin_az_mb_heading.value()
        sidelobe_gain = 10.0 ** (sidelobe_db / 20.0)
        centers = [
            (i - (num_beams - 1) / 2.0) * separation + global_heading
            for i in range(num_beams)
        ]
        data: list[list[float]] = []
        for angle in range(361):
            max_gain = sidelobe_gain
            for center in centers:
                lobe_gain = self._cosine_lobe(float(angle), center, beamwidth)
                if lobe_gain > max_gain:
                    max_gain = lobe_gain
            data.append([float(angle), round(max_gain, 4)])
        return data

    # ---- Elevation generators ---------------------------------------------

    def _generate_el_isotropic(self) -> list[list[float]]:
        return [[float(a), 1.0] for a in range(-90, 91)]

    def _generate_el_directional(self) -> list[list[float]]:
        beamwidth = self.spin_el_bw.value()
        tilt = self.spin_el_tilt.value()
        sidelobe_db = self.spin_el_sl.value()
        sidelobe_gain = 10.0 ** (sidelobe_db / 20.0)
        data: list[list[float]] = []
        for a in range(-90, 91):
            lobe = self._cosine_lobe(float(a), tilt, beamwidth)
            gain = max(lobe, sidelobe_gain)
            data.append([float(a), round(gain, 4)])
        return data

    def _generate_el_multibeam(self) -> list[list[float]]:
        num_beams = self.spin_el_mb_beams.value()
        separation = self.spin_el_mb_sep.value()
        beamwidth = self.spin_el_mb_bw.value()
        sidelobe_db = self.spin_el_mb_sl.value()
        sidelobe_gain = 10.0 ** (sidelobe_db / 20.0)
        centers = [
            (i - (num_beams - 1) / 2.0) * separation
            for i in range(num_beams)
        ]
        data: list[list[float]] = []
        for angle in range(-90, 91):
            max_gain = sidelobe_gain
            for center in centers:
                lobe_gain = self._cosine_lobe(float(angle), center, beamwidth)
                if lobe_gain > max_gain:
                    max_gain = lobe_gain
            data.append([float(angle), round(max_gain, 4)])
        return data

    # ---- Generic (Multi-Beam) helpers --------------------------------------

    def _add_generic_beam_row(
        self,
        table: QTableWidget,
        center: float,
        width: float,
        gain_db: float,
    ) -> None:
        """Add a row to a generic beam table."""
        row = table.rowCount()
        table.insertRow(row)
        table.setItem(row, 0, QTableWidgetItem(f"{center:.1f}"))
        table.setItem(row, 1, QTableWidgetItem(f"{width:.1f}"))
        table.setItem(row, 2, QTableWidgetItem(f"{gain_db:.1f}"))

    def _on_az_generic_add_beam(self) -> None:
        self._add_generic_beam_row(self.az_generic_table, 0.0, 30.0, 0.0)

    def _on_az_generic_remove_beam(self) -> None:
        rows = sorted(
            {idx.row() for idx in self.az_generic_table.selectedIndexes()},
            reverse=True,
        )
        for row in rows:
            self.az_generic_table.removeRow(row)

    def _on_el_generic_add_beam(self) -> None:
        self._add_generic_beam_row(self.el_generic_table, 0.0, 10.0, 0.0)

    def _on_el_generic_remove_beam(self) -> None:
        rows = sorted(
            {idx.row() for idx in self.el_generic_table.selectedIndexes()},
            reverse=True,
        )
        for row in rows:
            self.el_generic_table.removeRow(row)

    # ---- Generic (Multi-Beam) generator ------------------------------------

    def _generate_generic_multibeam(self) -> list[list[float]]:
        """Generate a pattern from a user-defined beam table."""
        if self._pattern_type == "azimuth":
            global_tilt = self.spin_az_generic_tilt.value()
            sidelobe_db = self.spin_az_generic_sl.value()
            table = self.az_generic_table
        else:
            global_tilt = self.spin_el_generic_tilt.value()
            sidelobe_db = self.spin_el_generic_sl.value()
            table = self.el_generic_table

        sidelobe_gain = 10 ** (sidelobe_db / 20.0)

        beams = []
        for row in range(table.rowCount()):
            c = float(table.item(row, 0).text())
            w = float(table.item(row, 1).text())
            g_db = float(table.item(row, 2).text())
            gain_lin = 10 ** (g_db / 20.0)
            beams.append({"c": c, "w": w, "g": gain_lin})

        angle_range = (
            range(0, 361)
            if self._pattern_type == "azimuth"
            else range(-90, 91)
        )

        gains: list[tuple[float, float]] = []
        for angle in angle_range:
            max_gain = sidelobe_gain
            lookup = angle - global_tilt
            for beam in beams:
                lobe = self._cosine_lobe(
                    lookup, beam["c"], beam["w"], beam["g"]
                )
                if lobe > max_gain:
                    max_gain = lobe
            gains.append((float(angle), max_gain))
        # Peak 1, BEFORE the table clamps to 0..1: beams above 0 dB keep
        # their levels relative to each other (job_builder.peak_normalized
        # does the same for assets saved before this). Floored at 0.001
        # (-60 dB, the table's last digit): a gain of 0 is a null to the
        # engine (no signal there), never a quiet sidelobe.
        peak = max((g for _, g in gains), default=1.0)
        scale = peak if peak > 1.0 else 1.0
        return [[a, max(round(g / scale, 4), 0.001)] for a, g in gains]

    # ---- Accept -----------------------------------------------------------

    def _on_accept(self) -> None:
        """Generate the pattern data and accept the dialog."""
        self.isotropic_elevation_pattern = None  # Reset
        if self._pattern_type == "azimuth":
            idx = self.combo_type.currentIndex()
            if idx == 0:
                self._result_data = self._generate_az_omni()
            elif idx == 1:
                self._result_data = self._generate_az_sectoral()
            elif idx == 2:
                self._result_data = self._generate_az_directional()
            elif idx == 3:
                self._result_data = self._generate_az_multibeam()
            elif idx == 4:
                # Isotropic (Omni 3D): flat AZ + auto-generate flat EL
                self._result_data = self._generate_az_omni()
                self.isotropic_elevation_pattern = self._generate_el_isotropic()
            elif idx == 5:
                self._result_data = self._generate_generic_multibeam()
        else:
            idx = self.combo_type.currentIndex()
            if idx == 0:
                self._result_data = self._generate_el_isotropic()
            elif idx == 1:
                self._result_data = self._generate_el_directional()
            elif idx == 2:
                self._result_data = self._generate_el_multibeam()
            elif idx == 3:
                self._result_data = self._generate_generic_multibeam()
        self.accept()

    def get_generated_pattern(self) -> Optional[list[list[float]]]:
        """Return the generated pattern, or *None* if cancelled."""
        return self._result_data


# ---------------------------------------------------------------------------
# Pattern Visualizer Dialog
# ---------------------------------------------------------------------------


class _PatternVisualizerDialog(QDialog):
    """Polar-plot dialog for visualising antenna patterns.

    Draws on :class:`PolarPlotWidget` (``gui/plotting``, MPT SIGMA's widget):
    azimuth with north up and clockwise winding, elevation on the -90..90
    semicircle with the horizon to the right. Right-click saves the image.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Antenna Pattern")
        self.setMinimumSize(550, 550)
        self._layout = QVBoxLayout(self)
        self.plot_widget = PolarPlotWidget()
        self._layout.addWidget(self.plot_widget, 1)
        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.accept)
        self._layout.addWidget(btn_close)

    def plot(
        self,
        pattern_data: list[list[float]],
        pattern_type: str,
        emitter_name: str,
    ) -> None:
        """Render the pattern on the polar chart.

        Args:
            pattern_data: ``[[angle_deg, gain], ...]``
            pattern_type: ``"azimuth"`` or ``"elevation"``
            emitter_name: Used in the window title.
        """
        widget = self.plot_widget
        if not pattern_data:
            widget.set_message("The pattern table is empty -- nothing to plot.")
            return

        angles = [row[0] for row in pattern_data]
        gains = [row[1] for row in pattern_data]
        widget.set_message(None)
        widget.set_r_max(1.0)
        widget.set_r_ticks([0.25, 0.5, 0.75, 1.0])
        if pattern_type == "azimuth":
            self.setWindowTitle(f"Azimuth Pattern: {emitter_name}")
            widget.set_title("Azimuth (Horizontal) Pattern")
            widget.set_theta_zero("N")
            widget.set_theta_direction("cw")
        else:
            self.setWindowTitle(f"Elevation Pattern: {emitter_name}")
            widget.set_title("Elevation (Vertical) Pattern")
            widget.set_theta_zero("E")
            widget.set_theta_direction("ccw")
            widget.set_theta_range(-90, 90)
        widget.set_series(angles, gains, color="#1f77b4", width=2.0)


# ---------------------------------------------------------------------------
# Main tab widget
# ---------------------------------------------------------------------------


def _same_file(a: str, b: str) -> bool:
    """Path equality as the file system sees it (case-blind on Windows)."""
    return (os.path.normcase(os.path.abspath(a))
            == os.path.normcase(os.path.abspath(b)))


class AssetManagerTab(QWidget):
    """Tab widget for managing emitter asset definitions.

    Provides full MPT SIGMA-style emitter management: transmission parameters
    with auto-computed ERP, inline antenna pattern tables with .az/.el I/O,
    pattern generation, height defaults, and ITM statistical parameters.

    Layout:
        Top bar  -- horizontal asset list + action buttons
                    [Reload] [New] [Save] [Save As] [Delete]
        Below    -- QTabWidget with 2 subtabs: Parameters, Antenna Patterns

    Signals:
        assets_changed -- emitted after every change persisted to the store
            (New, Save, Save As into the store, Delete, Reload, folder
            change); the 360° and P2P tabs re-read their asset lists on it.
        asset_renamed(old, new) -- emitted on a Save that changed the name,
            before ``assets_changed``, so rows showing *old* follow the asset.
    """

    assets_changed = pyqtSignal()
    asset_renamed = pyqtSignal(str, str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        self._assets: list[dict] = []
        self._current_index: int = -1
        self._dirty: bool = False
        self._block_dirty: bool = False
        self._pattern_table_lock: bool = False

        self._build_ui()
        self._connect_signals()
        self.refresh()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)

        # -- Top bar: asset list + buttons ---------------------------------
        outer.addWidget(self._build_top_bar())

        # -- Subtab editor area --------------------------------------------
        self.tab_widget = QTabWidget()
        self.tab_widget.addTab(self._build_parameters_tab(), "Parameters")
        self.tab_widget.addTab(self._build_patterns_tab(), "Antenna Patterns")
        outer.addWidget(self.tab_widget, 1)

    # -- Top bar -----------------------------------------------------------

    def _build_top_bar(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        self.asset_list = QListWidget()
        self.asset_list.setFlow(QListWidget.Flow.LeftToRight)
        self.asset_list.setWrapping(False)
        self.asset_list.setMaximumHeight(60)
        self.asset_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.asset_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.asset_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(self.asset_list)

        btn_row = QHBoxLayout()
        self.btn_reload = QPushButton("Reload")
        self.btn_new = QPushButton("New")
        self.btn_save = QPushButton("Save")
        self.btn_save_as = QPushButton("Save As")
        self.btn_delete = QPushButton("Delete")
        for btn in (
            self.btn_reload,
            self.btn_new,
            self.btn_save,
            self.btn_save_as,
            self.btn_delete,
        ):
            btn_row.addWidget(btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        # Where the assets live: the 360° and P2P tabs list only this folder.
        store_row = QHBoxLayout()
        self.lbl_store = QLabel()
        self.lbl_store.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.btn_open_store = QPushButton("Open folder")
        self.btn_open_store.setToolTip(
            "Show the assets folder in the file manager.")
        self.btn_change_store = QPushButton("Change…")
        self.btn_change_store.setToolTip(
            "Use another folder for assets. Assets in the current folder "
            "stay where they are.")
        store_row.addWidget(self.lbl_store, 1)
        store_row.addWidget(self.btn_open_store)
        store_row.addWidget(self.btn_change_store)
        layout.addLayout(store_row)
        self._update_store_label()

        return panel

    _STORE_LABEL_WIDTH_PX = 520

    def _update_store_label(self) -> None:
        """``Stored in: <dir>``, the path elided in the middle, full in the
        tooltip."""
        store = get_assets_dir()
        shown = self.lbl_store.fontMetrics().elidedText(
            store, Qt.TextElideMode.ElideMiddle, self._STORE_LABEL_WIDTH_PX)
        self.lbl_store.setText(f"Stored in: {shown}")
        self.lbl_store.setToolTip(
            f"{store}\n\nThe 360° and P2P tabs list the assets stored "
            "in this folder.")

    # -- Subtab 1: Parameters ----------------------------------------------

    def _build_parameters_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)

        # Name.
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self.edit_name = QLineEdit()
        name_row.addWidget(self.edit_name)
        layout.addLayout(name_row)

        # -- Output group --------------------------------------------------
        output_group = QGroupBox("Output")
        output_form = QFormLayout(output_group)

        self.spin_frequency = QDoubleSpinBox()
        self.spin_frequency.setRange(0.001, 100000.0)
        self.spin_frequency.setDecimals(3)
        self.spin_frequency.setValue(433.0)
        self.spin_frequency.setSuffix(" MHz")
        output_form.addRow("Frequency:", self.spin_frequency)

        # Peak power + computed ERP on the same row.
        power_row = QHBoxLayout()
        self.spin_peak_power = QDoubleSpinBox()
        self.spin_peak_power.setRange(0.001, 1000000.0)
        self.spin_peak_power.setDecimals(3)
        self.spin_peak_power.setValue(10.0)
        self.spin_peak_power.setSuffix(" W")
        power_row.addWidget(self.spin_peak_power)
        self.lbl_erp = QLabel("Computed ERP: -- W")
        self.lbl_erp.setMinimumWidth(160)
        power_row.addWidget(self.lbl_erp)
        power_row.addStretch()
        output_form.addRow("Peak Power:", power_row)

        self.spin_gain = QDoubleSpinBox()
        self.spin_gain.setRange(-100.0, 100.0)
        self.spin_gain.setDecimals(2)
        self.spin_gain.setValue(0.0)
        self.spin_gain.setSuffix(" dBi")
        output_form.addRow("Antenna Gain:", self.spin_gain)

        self.combo_polarization = QComboBox()
        self.combo_polarization.addItems(["Horizontal (0)", "Vertical (1)"])
        output_form.addRow("Polarization:", self.combo_polarization)

        layout.addWidget(output_group)

        # -- Defaults group ------------------------------------------------
        defaults_group = QGroupBox("Defaults")
        defaults_form = QFormLayout(defaults_group)

        self.spin_height = QDoubleSpinBox()
        self.spin_height.setRange(MIN_AMSL_M, 10000.0)
        self.spin_height.setDecimals(1)
        self.spin_height.setValue(30.0)
        self.spin_height.setSuffix(" m")
        defaults_form.addRow("Default Height:", self.spin_height)

        self.combo_height_mode = QComboBox()
        self.combo_height_mode.addItems(["AGL", "AMSL"])
        defaults_form.addRow("Height Mode:", self.combo_height_mode)

        # The default height seeds every site row built from this asset, so it
        # gets the same mode-dependent bound as the site table: a 1 m floor
        # while the mode is AGL, below sea level once it is AMSL.
        bind_height_mode(self.spin_height, self.combo_height_mode)

        layout.addWidget(defaults_group)

        # -- ITM Statistical Parameters group ------------------------------
        itm_group = QGroupBox("ITM Statistical Parameters")
        itm_form = QFormLayout(itm_group)

        self.spin_fraction_situations = QDoubleSpinBox()
        self.spin_fraction_situations.setRange(0.01, 0.99)
        self.spin_fraction_situations.setDecimals(2)
        self.spin_fraction_situations.setSingleStep(0.05)
        self.spin_fraction_situations.setValue(0.50)
        itm_form.addRow("Fraction of Situations:", self.spin_fraction_situations)

        self.spin_fraction_time = QDoubleSpinBox()
        self.spin_fraction_time.setRange(0.01, 0.99)
        self.spin_fraction_time.setDecimals(2)
        self.spin_fraction_time.setSingleStep(0.05)
        self.spin_fraction_time.setValue(0.50)
        itm_form.addRow("Fraction of Time:", self.spin_fraction_time)

        layout.addWidget(itm_group)

        layout.addStretch()
        scroll.setWidget(panel)
        return scroll

    # -- Subtab 2: Antenna Patterns ----------------------------------------

    def _build_patterns_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)

        # Azimuth (Horizontal) pattern.
        az_group = QGroupBox("Azimuth (Horizontal) Pattern")
        az_layout = QVBoxLayout(az_group)

        self.az_table = QTableWidget(0, 2)
        self.az_table.setHorizontalHeaderLabels(["Angle (deg)", "Gain (0-1)"])
        self.az_table.horizontalHeader().setStretchLastSection(True)
        self.az_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.az_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.az_table.setMinimumHeight(160)
        az_layout.addWidget(self.az_table)

        az_btns = QHBoxLayout()
        self.btn_az_add = QPushButton("Add Row")
        self.btn_az_remove = QPushButton("Remove Row")
        self.btn_az_load = QPushButton("Load .az")
        self.btn_az_save = QPushButton("Save .az")
        self.btn_az_generate = QPushButton("Generate")
        self.btn_az_visualize = QPushButton("Visualize")
        self.btn_az_clear = QPushButton("Clear")
        for btn in (
            self.btn_az_add,
            self.btn_az_remove,
            self.btn_az_load,
            self.btn_az_save,
            self.btn_az_generate,
            self.btn_az_visualize,
            self.btn_az_clear,
        ):
            az_btns.addWidget(btn)
        az_layout.addLayout(az_btns)
        layout.addWidget(az_group)

        # Elevation (Vertical) pattern.
        el_group = QGroupBox("Elevation (Vertical) Pattern")
        el_layout = QVBoxLayout(el_group)

        self.el_table = QTableWidget(0, 2)
        self.el_table.setHorizontalHeaderLabels(["Angle (deg)", "Gain (0-1)"])
        self.el_table.horizontalHeader().setStretchLastSection(True)
        self.el_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.el_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.el_table.setMinimumHeight(160)
        el_layout.addWidget(self.el_table)

        el_btns = QHBoxLayout()
        self.btn_el_add = QPushButton("Add Row")
        self.btn_el_remove = QPushButton("Remove Row")
        self.btn_el_load = QPushButton("Load .el")
        self.btn_el_save = QPushButton("Save .el")
        self.btn_el_generate = QPushButton("Generate")
        self.btn_el_visualize = QPushButton("Visualize")
        self.btn_el_clear = QPushButton("Clear")
        for btn in (
            self.btn_el_add,
            self.btn_el_remove,
            self.btn_el_load,
            self.btn_el_save,
            self.btn_el_generate,
            self.btn_el_visualize,
            self.btn_el_clear,
        ):
            el_btns.addWidget(btn)
        el_layout.addLayout(el_btns)
        layout.addWidget(el_group)

        layout.addStretch()
        scroll.setWidget(panel)
        return scroll

    # ------------------------------------------------------------------
    # Signal wiring
    # ------------------------------------------------------------------

    def _connect_signals(self) -> None:
        # -- Top bar -------------------------------------------------------
        self.asset_list.currentRowChanged.connect(self._on_asset_selected)
        self.btn_reload.clicked.connect(self._on_reload)
        self.btn_new.clicked.connect(self._on_new)
        self.btn_save.clicked.connect(self._on_save)
        self.btn_save_as.clicked.connect(self._on_save_as)
        self.btn_delete.clicked.connect(self._on_delete)
        self.btn_open_store.clicked.connect(self._on_open_store)
        self.btn_change_store.clicked.connect(self._on_change_store)

        # -- ERP auto-compute ----------------------------------------------
        self.spin_peak_power.valueChanged.connect(self._update_erp)
        self.spin_gain.valueChanged.connect(self._update_erp)

        # -- Dirty tracking: Parameters subtab -----------------------------
        self.edit_name.textChanged.connect(self._mark_dirty)
        self.spin_frequency.valueChanged.connect(self._mark_dirty)
        self.spin_peak_power.valueChanged.connect(self._mark_dirty)
        self.spin_gain.valueChanged.connect(self._mark_dirty)
        self.combo_polarization.currentIndexChanged.connect(self._mark_dirty)
        self.spin_height.valueChanged.connect(self._mark_dirty)
        self.combo_height_mode.currentIndexChanged.connect(self._mark_dirty)
        self.spin_fraction_situations.valueChanged.connect(self._mark_dirty)
        self.spin_fraction_time.valueChanged.connect(self._mark_dirty)

        # -- Dirty tracking: Pattern tables --------------------------------
        self.az_table.itemChanged.connect(self._on_az_item_changed)
        self.el_table.itemChanged.connect(self._on_el_item_changed)

        # -- Azimuth pattern buttons ---------------------------------------
        self.btn_az_add.clicked.connect(lambda: self._on_add_row(self.az_table))
        self.btn_az_remove.clicked.connect(
            lambda: self._on_remove_rows(self.az_table)
        )
        self.btn_az_load.clicked.connect(
            lambda: self._on_load_pattern("az", self.az_table)
        )
        self.btn_az_save.clicked.connect(
            lambda: self._on_save_pattern("az", self.az_table)
        )
        self.btn_az_generate.clicked.connect(
            lambda: self._on_generate_pattern("azimuth", self.az_table)
        )
        self.btn_az_visualize.clicked.connect(
            lambda: self._on_visualize_pattern("azimuth", self.az_table)
        )
        self.btn_az_clear.clicked.connect(
            lambda: self._on_clear_table(self.az_table)
        )

        # -- Elevation pattern buttons -------------------------------------
        self.btn_el_add.clicked.connect(lambda: self._on_add_row(self.el_table))
        self.btn_el_remove.clicked.connect(
            lambda: self._on_remove_rows(self.el_table)
        )
        self.btn_el_load.clicked.connect(
            lambda: self._on_load_pattern("el", self.el_table)
        )
        self.btn_el_save.clicked.connect(
            lambda: self._on_save_pattern("el", self.el_table)
        )
        self.btn_el_generate.clicked.connect(
            lambda: self._on_generate_pattern("elevation", self.el_table)
        )
        self.btn_el_visualize.clicked.connect(
            lambda: self._on_visualize_pattern("elevation", self.el_table)
        )
        self.btn_el_clear.clicked.connect(
            lambda: self._on_clear_table(self.el_table)
        )

    # ------------------------------------------------------------------
    # ERP auto-computation
    # ------------------------------------------------------------------

    def _update_erp(self) -> None:
        """Recompute and display ERP based on current peak power and gain."""
        erp = compute_erp(self.spin_peak_power.value(), self.spin_gain.value())
        self.lbl_erp.setText(f"Computed ERP: {erp:.2f} W")

    # ------------------------------------------------------------------
    # Pattern table helpers
    # ------------------------------------------------------------------

    def _get_pattern_data(self, table: QTableWidget) -> list[list[float]]:
        """Read a pattern table into ``[[angle, gain], ...]``."""
        data: list[list[float]] = []
        for row in range(table.rowCount()):
            angle_item = table.item(row, 0)
            gain_item = table.item(row, 1)
            if angle_item is None or gain_item is None:
                continue
            try:
                angle = float(angle_item.text())
                gain = float(gain_item.text())
            except ValueError:
                continue
            data.append([angle, gain])
        return data

    def _set_pattern_data(
        self, table: QTableWidget, data: list[list[float]]
    ) -> None:
        """Populate a pattern table from ``[[angle, gain], ...]``."""
        self._pattern_table_lock = True
        table.setRowCount(0)
        for pair in data:
            if len(pair) < 2:
                continue
            row = table.rowCount()
            table.insertRow(row)
            table.setItem(row, 0, QTableWidgetItem(f"{pair[0]:.1f}"))
            table.setItem(
                row, 1, QTableWidgetItem(f"{max(0.0, min(1.0, pair[1])):.3f}")
            )
        self._pattern_table_lock = False

    # -- item-changed handlers (clamp gain + dirty) ------------------------

    def _on_az_item_changed(self, item: QTableWidgetItem) -> None:
        self._clamp_and_dirty(item)

    def _on_el_item_changed(self, item: QTableWidgetItem) -> None:
        self._clamp_and_dirty(item)

    def _clamp_and_dirty(self, item: QTableWidgetItem) -> None:
        """Clamp gain values to [0.0, 1.0] on cell edit and mark dirty."""
        if self._pattern_table_lock:
            return
        if item.column() == 1:
            try:
                val = float(item.text())
            except ValueError:
                return
            clamped = max(0.0, min(1.0, val))
            if clamped != val:
                self._pattern_table_lock = True
                item.setText(f"{clamped:.3f}")
                self._pattern_table_lock = False
        self._mark_dirty()

    # -- Pattern table button slots ----------------------------------------

    def _on_add_row(self, table: QTableWidget) -> None:
        """Add a row with the next 10-degree angle increment and gain 1.0."""
        row_count = table.rowCount()
        if row_count > 0:
            last_item = table.item(row_count - 1, 0)
            try:
                next_angle = float(last_item.text()) + 10.0
            except (ValueError, AttributeError):
                next_angle = row_count * 10.0
        else:
            next_angle = 0.0

        self._pattern_table_lock = True
        table.insertRow(row_count)
        table.setItem(row_count, 0, QTableWidgetItem(f"{next_angle:.1f}"))
        table.setItem(row_count, 1, QTableWidgetItem("1.000"))
        self._pattern_table_lock = False
        self._mark_dirty()

    def _on_remove_rows(self, table: QTableWidget) -> None:
        """Remove all selected rows from the given table."""
        rows = sorted(
            {idx.row() for idx in table.selectedIndexes()}, reverse=True
        )
        for row in rows:
            table.removeRow(row)
        self._mark_dirty()

    def _on_clear_table(self, table: QTableWidget) -> None:
        """Remove all rows from the given table."""
        table.setRowCount(0)
        self._mark_dirty()

    def _on_load_pattern(self, ext: str, table: QTableWidget) -> None:
        """Load a SPLAT! antenna pattern file (.az or .el).

        Read as aether_core reads it (:func:`parse_pattern_file`): line 1 is
        the header, ``;`` starts a comment, gains are clamped to [0, 1]. A
        non-zero header rotation / tilt is not applied — the user is told
        where it belongs instead of losing it silently.
        """
        label = "Azimuth" if ext == "az" else "Elevation"
        caption = f"Load {label} Pattern"
        filt = f"{ext.upper()} Pattern Files (*.{ext});;All Files (*)"
        path, _ = QFileDialog.getOpenFileName(self, caption, "", filt)
        if not path:
            return

        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                parsed = parse_pattern_file(f.read(), ext)
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Load Failed",
                f"Could not read pattern file:\n{exc}",
            )
            return
        if not parsed["data"]:
            QMessageBox.warning(
                self, "Load Failed",
                f"No [angle gain] data lines found after the header line of\n{path}",
            )
            return

        data = [[a, max(0.0, min(1.0, g))] for a, g in parsed["data"]]
        self._set_pattern_data(table, data)
        self._mark_dirty()
        notes = [n for n in (pattern_header_note(parsed, ext),) if n]
        if parsed["skipped"]:
            notes.append(f"{parsed['skipped']} line(s) that are not two numbers "
                         "were skipped (the engine skips them too).")
        if notes:
            QMessageBox.information(
                self, f"{label} pattern loaded", "\n\n".join(notes))

    def _on_save_pattern(self, ext: str, table: QTableWidget) -> None:
        """Save current pattern to a SPLAT!-format file (.az or .el).

        Azimuth files get a ``0.0\\t; Antenna Rotation (Bearing)`` header.
        Elevation files get a ``0.0 0.0\\t; Mechanical Tilt, Tilt Axis``
        header.  Data lines are ``angle<tab>gain`` with 6 decimal places.
        """
        label = "Azimuth" if ext == "az" else "Elevation"
        caption = f"Save {label} Pattern"
        filt = f"{ext.upper()} Pattern Files (*.{ext});;All Files (*)"
        path, _ = QFileDialog.getSaveFileName(self, caption, "", filt)
        if not path:
            return

        data = self._get_pattern_data(table)
        try:
            with open(path, "w", encoding="utf-8") as f:
                if ext == "az":
                    f.write("0.0\t; Antenna Rotation (Bearing)\n")
                else:
                    f.write("0.0 0.0\t; Mechanical Tilt, Tilt Axis\n")
                for angle, gain in data:
                    # Fractional angles stay fractional (0.5° steps).
                    f.write(f"{angle:.6g}\t{gain:.6f}\n")
        except OSError as exc:
            QMessageBox.warning(
                self,
                "Save Failed",
                f"Could not write pattern file:\n{exc}",
            )

    def _on_generate_pattern(
        self, pattern_type: str, table: QTableWidget
    ) -> None:
        """Open the pattern generator dialog and populate the table."""
        dlg = _PatternGeneratorDialog(pattern_type, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            data = dlg.get_generated_pattern()
            if data:
                self._set_pattern_data(table, data)
                self._mark_dirty()
            # Handle Isotropic (Omni 3D): also auto-populate elevation.
            if dlg.isotropic_elevation_pattern is not None:
                self._set_pattern_data(self.el_table, dlg.isotropic_elevation_pattern)
                self._mark_dirty()

    def _on_visualize_pattern(
        self, pattern_type: str, table: QTableWidget
    ) -> None:
        """Open the pattern visualiser dialog for the given table."""
        data = self._get_pattern_data(table)
        name = self.edit_name.text().strip() or "Unnamed"
        dlg = _PatternVisualizerDialog(parent=self)
        dlg.plot(data, pattern_type, name)
        dlg.exec()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh_settings(self) -> None:
        """Re-read what this tab derives from QgsSettings (cheap, idempotent).

        Called by the main dialog when the Settings tab saves — the asset
        store location (``waveshed/assets_dir``) is a setting, so the list
        must follow it. A half-edited asset survives: with unsaved edits the
        reload is SKIPPED entirely (rebuilding the list would reset the
        selection and clobber the form); the Reload button remains the
        explicit way to discard edits. Without edits, the reload preserves
        the current selection by name.
        """
        if self._dirty:
            return
        current = None
        if 0 <= self._current_index < len(self._assets):
            current = self._assets[self._current_index].get("name")
        self.refresh()
        if current:
            for i, asset in enumerate(self._assets):
                if asset.get("name") == current:
                    self.asset_list.setCurrentRow(i)
                    break

    def refresh(self) -> None:
        """Reload the asset list from disk and repopulate the QListWidget."""
        self._assets = list_assets()
        self._rebuild_list()
        self._update_store_label()

    # ------------------------------------------------------------------
    # List helpers
    # ------------------------------------------------------------------

    def _rebuild_list(self) -> None:
        """Repopulate the QListWidget from the in-memory asset list."""
        self.asset_list.blockSignals(True)
        self.asset_list.clear()
        for asset in self._assets:
            item = QListWidgetItem(asset.get("name", "Unnamed"))
            self.asset_list.addItem(item)
        self.asset_list.blockSignals(False)

        if self._assets:
            self.asset_list.setCurrentRow(0)
        else:
            self._current_index = -1
            self._clear_editor()

    # ------------------------------------------------------------------
    # Editor <-> asset dict
    # ------------------------------------------------------------------

    def _load_into_editor(self, asset: dict) -> None:
        """Populate editor widgets from an asset dict without triggering
        the dirty flag."""
        self._block_dirty = True

        self.edit_name.setText(asset.get("name", ""))
        self.spin_frequency.setValue(asset.get("frequency_mhz", 433.0))
        self.spin_peak_power.setValue(asset.get("peak_power_watts", 10.0))
        self.spin_gain.setValue(asset.get("antenna_gain_dbi", 0.0))

        # Polarization: 0 = Horizontal, 1 = Vertical.
        pol = asset.get("polarization", 0)
        self.combo_polarization.setCurrentIndex(min(max(pol, 0), 1))

        # Mode before height: the mode sets the height spinbox's minimum (1 m
        # AGL floor vs. below sea level for AMSL), so filling the height first
        # would clamp an AMSL asset's negative elevation to the AGL floor.
        mode = asset.get("default_height_mode", "AGL")
        idx = self.combo_height_mode.findText(mode)
        self.combo_height_mode.setCurrentIndex(max(idx, 0))
        self.spin_height.setValue(asset.get("default_height_m", 30.0))

        # Antenna patterns.
        az_data = asset.get("azimuth_pattern", {}).get("data", [])
        self._set_pattern_data(self.az_table, az_data)
        el_data = asset.get("elevation_pattern", {}).get("data", [])
        self._set_pattern_data(self.el_table, el_data)

        # ITM statistical parameters.
        self.spin_fraction_situations.setValue(
            asset.get("fraction_situations", 0.50)
        )
        self.spin_fraction_time.setValue(asset.get("fraction_time", 0.50))

        # Update the ERP label.
        self._update_erp()

        self._block_dirty = False
        self._dirty = False

    def _collect_from_editor(self) -> dict:
        """Read the editor widgets into an asset dict."""
        erp = compute_erp(self.spin_peak_power.value(), self.spin_gain.value())

        return {
            "name": self.edit_name.text().strip(),
            "frequency_mhz": self.spin_frequency.value(),
            "peak_power_watts": self.spin_peak_power.value(),
            "antenna_gain_dbi": self.spin_gain.value(),
            "erp_watts": erp,
            "polarization": self.combo_polarization.currentIndex(),
            "default_height_m": self.spin_height.value(),
            "default_height_mode": self.combo_height_mode.currentText(),
            "azimuth_pattern": {"data": self._get_pattern_data(self.az_table)},
            "elevation_pattern": {"data": self._get_pattern_data(self.el_table)},
            "fraction_situations": self.spin_fraction_situations.value(),
            "fraction_time": self.spin_fraction_time.value(),
        }

    def _clear_editor(self) -> None:
        """Reset editor widgets to blank / default values."""
        self._block_dirty = True

        self.edit_name.setText("")
        self.spin_frequency.setValue(433.0)
        self.spin_peak_power.setValue(10.0)
        self.spin_gain.setValue(0.0)
        self.combo_polarization.setCurrentIndex(0)
        self.spin_height.setValue(30.0)
        self.combo_height_mode.setCurrentIndex(0)
        self._set_pattern_data(self.az_table, [])
        self._set_pattern_data(self.el_table, [])
        self.spin_fraction_situations.setValue(0.50)
        self.spin_fraction_time.setValue(0.50)
        self._update_erp()

        self._block_dirty = False
        self._dirty = False

    # ------------------------------------------------------------------
    # Dirty tracking
    # ------------------------------------------------------------------

    def _mark_dirty(self) -> None:
        if not self._block_dirty:
            self._dirty = True

    def _check_unsaved_changes(self) -> bool:
        """If unsaved changes exist, ask the user what to do.

        Returns ``True`` if it is safe to proceed (changes were saved or
        discarded).  Returns ``False`` if the user cancelled.
        """
        if not self._dirty:
            return True

        answer = QMessageBox.question(
            self,
            "Unsaved Changes",
            "The current asset has unsaved changes.\n"
            "Save before continuing?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Save:
            # A refused or failed save keeps the edits — do not go on.
            return self._on_save()
        if answer == QMessageBox.StandardButton.Discard:
            self._dirty = False
            return True
        return False

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_asset_selected(self, row: int) -> None:
        if row < 0 or row >= len(self._assets):
            return

        # If switching away from a dirty asset, ask first.
        if row != self._current_index and not self._check_unsaved_changes():
            self.asset_list.blockSignals(True)
            self.asset_list.setCurrentRow(self._current_index)
            self.asset_list.blockSignals(False)
            return

        self._current_index = row
        self._load_into_editor(self._assets[row])

    def _on_new(self) -> None:
        if not self._check_unsaved_changes():
            return

        asset = create_default_asset()

        # A name no asset has — in this list or in the store on disk — and
        # (save_asset without a path) a file no other asset has.
        existing_names = {a.get("name") for a in self._assets}
        existing_names.update(self._store_names())
        base_name = asset["name"]
        counter = 1
        while asset["name"] in existing_names:
            counter += 1
            asset["name"] = f"{base_name} {counter}"

        # Save immediately so it has a file on disk.
        try:
            path = save_asset(asset)
        except OSError as exc:
            QMessageBox.warning(
                self, "New Asset", f"Could not create the asset file:\n{exc}")
            return
        asset["_path"] = path

        self._assets.append(asset)
        self._rebuild_list()

        # Select the newly created asset.
        new_row = len(self._assets) - 1
        self.asset_list.setCurrentRow(new_row)
        self.assets_changed.emit()

    def _store_names(self) -> set:
        """Names of the assets stored on disk (the store, not this list)."""
        return {a.get("name") for a in list_assets()}

    def _name_taken_in_store(self, name: str, own_path: Optional[str]) -> Optional[str]:
        """Path of another asset in the store already called *name*, or None.

        *own_path* (the file being written) does not count: overwriting it
        leaves one asset of that name.
        """
        for asset in list_assets():
            if asset.get("name") != name:
                continue
            path = asset.get("_path") or ""
            if own_path and _same_file(path, own_path):
                continue
            return path
        return None

    def _refuse_taken_name(self, name: str, other_path: str) -> None:
        QMessageBox.warning(
            self, "Name already used",
            f'Another asset in the assets folder is already called "{name}" '
            f"({os.path.basename(other_path)}).\n\n"
            "The 360° and P2P tabs pick assets by name, so two assets "
            "must not share one. Choose another name.",
        )

    def _on_save(self) -> bool:
        """Save current editor state to the asset's file.

        A changed name moves the file to one derived from the new name
        (store only; ``save_renamed_asset``). Returns True when saved.
        """
        if self._current_index < 0 or self._current_index >= len(self._assets):
            return False

        data = self._collect_from_editor()
        if not data["name"]:
            QMessageBox.warning(self, "Validation", "Asset name cannot be empty.")
            return False

        existing = self._assets[self._current_index]
        path = existing.get("_path")
        old_name = existing.get("name") or ""
        renamed = data["name"] != old_name

        if renamed:
            other = self._name_taken_in_store(data["name"], path)
            if other:
                self._refuse_taken_name(data["name"], other)
                return False

        try:
            if renamed and path:
                saved_path = save_renamed_asset(data, path)
            else:
                saved_path = save_asset(data, path=path)
        except OSError as exc:
            QMessageBox.warning(
                self, "Save Failed",
                f"Could not save the asset:\n{exc}\n\nThe file on disk is "
                "unchanged.",
            )
            return False
        data["_path"] = saved_path

        self._assets[self._current_index] = data
        self._dirty = False

        # Update the list item text in case the name changed.
        item = self.asset_list.item(self._current_index)
        if item:
            item.setText(data["name"])

        if renamed and old_name:
            self.asset_renamed.emit(old_name, data["name"])
        self.assets_changed.emit()
        return True

    def _on_save_as(self) -> None:
        """Save a copy under a new file name (the dialog starts in the store)."""
        if self._current_index < 0:
            return

        data = self._collect_from_editor()
        if not data["name"]:
            QMessageBox.warning(self, "Validation", "Asset name cannot be empty.")
            return

        store = get_assets_dir()
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Asset As",
            unique_asset_path(data["name"], assets_dir=store),
            "Asset JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".json"

        in_store = is_in_store(path, assets_dir=store)
        if in_store:
            other = self._name_taken_in_store(data["name"], path)
            if other:
                self._refuse_taken_name(data["name"], other)
                return

        try:
            saved_path = save_asset(data, path=path)
        except OSError as exc:
            QMessageBox.warning(
                self, "Save Failed", f"Could not save the asset:\n{exc}")
            return
        data["_path"] = saved_path

        # Overwriting a listed file replaces its entry instead of listing it
        # twice.
        self._assets = [
            a for a in self._assets
            if not (a.get("_path") and _same_file(a["_path"], saved_path))
        ]
        self._assets.append(data)
        self._dirty = False  # before the rebuild: it selects a row
        self._rebuild_list()

        new_row = len(self._assets) - 1
        self.asset_list.setCurrentRow(new_row)

        if in_store:
            self.assets_changed.emit()
        else:
            QMessageBox.warning(
                self, "Saved outside the assets folder",
                f"Saved to:\n{saved_path}\n\nThe 360° and P2P tabs list "
                f"only assets stored in:\n{store}\n\nThis copy is listed here "
                "until the next Reload.",
            )

    def _on_delete(self) -> None:
        if self._current_index < 0 or self._current_index >= len(self._assets):
            return

        if not self._check_unsaved_changes():
            return

        asset = self._assets[self._current_index]
        name = asset.get("name", "Unnamed")

        answer = QMessageBox.question(
            self,
            "Delete Asset",
            f'Are you sure you want to delete "{name}"?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        path = asset.get("_path")
        if path:
            try:
                delete_asset(path)
            except OSError:
                QMessageBox.warning(
                    self,
                    "Delete Failed",
                    f"Could not delete file:\n{path}",
                )
                return

        self._dirty = False
        self._assets.pop(self._current_index)
        self._current_index = -1
        self._rebuild_list()
        self.assets_changed.emit()

    def _on_open_store(self) -> None:
        """Show the assets folder in the system file manager."""
        QDesktopServices.openUrl(QUrl.fromLocalFile(get_assets_dir()))

    def _on_change_store(self) -> None:
        """Pick another assets folder; the list and the run tabs follow it.

        Assets in the old folder stay where they are. Unsaved edits are asked
        about first (Save writes to the asset's own file in the old folder).
        """
        current = get_assets_dir()
        chosen = QFileDialog.getExistingDirectory(
            self, "Assets folder", current)
        if not chosen or _same_file(chosen, current):
            return
        if not self._check_unsaved_changes():
            return
        QgsSettings().setValue("waveshed/assets_dir", os.path.abspath(chosen))
        self._dirty = False
        self._current_index = -1
        self.refresh()
        self.assets_changed.emit()

    def _on_reload(self) -> None:
        """Reload all assets from disk, discarding unsaved changes."""
        if self._dirty:
            answer = QMessageBox.question(
                self,
                "Reload Assets",
                "Reloading will discard any unsaved changes.\nContinue?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self._dirty = False
        self._current_index = -1
        self.refresh()
        # Files edited, added or removed outside the plugin reach the run
        # tabs too.
        self.assets_changed.emit()
