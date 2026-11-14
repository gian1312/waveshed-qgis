"""The ITM parameter group, one per analysis tab (360° and P2P).

Both tabs build their own instance (owner, 2026-11-14: P2P "gets its own"),
so a P2P link can be computed for other ground or climate than the 360° run
beside it. The widgets, ranges, defaults and labels live here once; the
defaults are the engine's (``job_builder.CoverageParams`` / ``P2PParams``).

**Earth Radius Mode** sits here too: aether_core reads
``propagation.earth_radius_mode`` only in its CPU ITM paths
(engines/cpu_coverage.rs, engines/p2p.rs ``run_p2p_cpu``). The GPU backend
and every LOS / LOS Floor path use the fixed 4/3 earth, so the control is
offered with the other ITM parameters — Propagation mode only — and says so.
"""

from __future__ import annotations

from typing import Dict, Optional

from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QWidget,
)

#: Radio climates as aether_core numbers them (config.rs validates 1–7).
RADIO_CLIMATE_DEFAULT = 5

EARTH_RADIUS_TIP = (
    "Earth-curvature model used by ITM on the CPU backend (Backend CPU, or "
    "AUTO without a usable GPU):\n"
    "FOUR_THIRDS: standard 4/3-earth refraction (normal, recommended).\n"
    "ADVANCED: a k-factor per path from the surface refractivity and both "
    "endpoint heights.\n"
    "The GPU backend, LOS and LOS Floor always use the 4/3 earth."
)


class ItmParamsGroup(QGroupBox):
    """"ITM Parameters": the ``propagation.*`` fields of one tab's jobs.

    Attributes keep the names the tabs used before (``spin_eps``,
    ``combo_climate``, …) so callers and tests address the same widgets.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("ITM Parameters", parent)
        self._build()

    def _build(self) -> None:
        layout = QFormLayout(self)

        # Ground Permittivity
        self.spin_eps = QDoubleSpinBox()
        self.spin_eps.setRange(0.0, 100.0)
        self.spin_eps.setValue(15.0)
        self.spin_eps.setDecimals(2)
        layout.addRow("Ground Permittivity:", self.spin_eps)

        # Ground Conductivity
        self.spin_sgm = QDoubleSpinBox()
        self.spin_sgm.setRange(0.0001, 10.0)
        self.spin_sgm.setValue(0.005)
        self.spin_sgm.setDecimals(4)
        layout.addRow("Ground Conductivity:", self.spin_sgm)

        # Surface Refractivity
        self.spin_ens = QDoubleSpinBox()
        self.spin_ens.setRange(200.0, 450.0)
        self.spin_ens.setValue(301.0)
        self.spin_ens.setDecimals(1)
        layout.addRow("Surface Refractivity:", self.spin_ens)

        # Radio Climate — index + 1 is the ITM number (1–7).
        self.combo_climate = QComboBox()
        self.combo_climate.addItems([
            "Equatorial (1)",
            "Continental Subtropical (2)",
            "Maritime Subtropical (3)",
            "Desert (4)",
            "Continental Temperate (5)",
            "Maritime Temperate, Over Land (6)",
            "Maritime Temperate, Over Sea (7)",
        ])
        self.combo_climate.setCurrentIndex(RADIO_CLIMATE_DEFAULT - 1)
        layout.addRow("Radio Climate:", self.combo_climate)

        # Polarization
        self.combo_pol = QComboBox()
        self.combo_pol.addItems(["Horizontal (0)", "Vertical (1)"])
        layout.addRow("Polarization:", self.combo_pol)

        # Confidence
        self.spin_conf = QDoubleSpinBox()
        self.spin_conf.setRange(0.01, 0.99)
        self.spin_conf.setValue(0.50)
        self.spin_conf.setDecimals(2)
        self.spin_conf.setSingleStep(0.05)
        layout.addRow("Confidence:", self.spin_conf)

        # Reliability
        self.spin_rel = QDoubleSpinBox()
        self.spin_rel.setRange(0.01, 0.99)
        self.spin_rel.setValue(0.50)
        self.spin_rel.setDecimals(2)
        self.spin_rel.setSingleStep(0.05)
        layout.addRow("Reliability:", self.spin_rel)

        # Earth Radius Mode — CPU ITM only (see the module note).
        self.combo_earth_radius = QComboBox()
        self.combo_earth_radius.addItems(["FOUR_THIRDS", "ADVANCED"])
        self.combo_earth_radius.setToolTip(EARTH_RADIUS_TIP)
        layout.addRow("Earth Radius Mode:", self.combo_earth_radius)

    def values(self) -> Dict[str, object]:
        """The group as ``CoverageParams`` / ``P2PParams`` keyword arguments."""
        return {
            "eps": self.spin_eps.value(),
            "sgm": self.spin_sgm.value(),
            "ens": self.spin_ens.value(),
            "climate": self.combo_climate.currentIndex() + 1,
            "pol": self.combo_pol.currentIndex(),
            "conf": self.spin_conf.value(),
            "rel": self.spin_rel.value(),
            "earth_radius": self.combo_earth_radius.currentText(),
        }
