"""The ITM parameter group, one per analysis tab (360° and P2P).

Both tabs build their own instance (owner, 2026-11-14: P2P "gets its own"),
so a P2P link can be computed for other ground or climate than the 360° run
beside it. The widgets, ranges, defaults and labels live here once; the
defaults are the engine's (``job_builder.CoverageParams`` / ``P2PParams``).

**Earth Radius Mode** is NOT in the group: the earth's curvature matters for
LOS and LOS Floor as much as for ITM (owner, 2026-11-14), so each tab shows it
in its Analysis section in every mode, from :data:`EARTH_RADIUS_MODES` and
:data:`EARTH_RADIUS_TIP`.
What this engine version does with it (READ, AETHER 0.5.1): only the CPU ITM
paths read ``propagation.earth_radius_mode`` (engines/cpu_coverage.rs,
engines/p2p.rs ``run_p2p_cpu``); LOS, LOS Floor and every GPU path compute
with a fixed k = 4/3 (shader.wgsl), and there is no "real earth" (k = 1)
mode yet. The tooltip says so.
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
    "Earth-curvature model (horizon and diffraction geometry):\n"
    "FOUR_THIRDS: standard 4/3-earth refraction (normal, recommended).\n"
    "ADVANCED: a k-factor per path from the surface refractivity and both "
    "endpoint heights.\n"
    "This engine version applies it to ITM on the CPU backend only; LOS, "
    "LOS Floor and the GPU backend still compute with the 4/3 earth."
)


#: The Earth Radius Mode items both tabs offer (each tab builds its own combo
#: from them, in its Analysis section, every mode).
EARTH_RADIUS_MODES = ("FOUR_THIRDS", "ADVANCED")


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
        self.spin_eps.setDecimals(2)
        self.spin_eps.setValue(15.0)
        layout.addRow("Ground Permittivity:", self.spin_eps)

        # Ground Conductivity
        self.spin_sgm = QDoubleSpinBox()
        self.spin_sgm.setRange(0.0001, 10.0)
        # Decimals BEFORE the value: a spin box rounds to its current
        # decimals (2 by default), and 0.005 set first came out as 0.01 —
        # every ITM job sent wet-ground conductivity instead of the default.
        self.spin_sgm.setDecimals(4)
        self.spin_sgm.setValue(0.005)
        layout.addRow("Ground Conductivity:", self.spin_sgm)

        # Surface Refractivity
        self.spin_ens = QDoubleSpinBox()
        self.spin_ens.setRange(200.0, 450.0)
        self.spin_ens.setDecimals(1)
        self.spin_ens.setValue(301.0)
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
        self.spin_conf.setDecimals(2)
        self.spin_conf.setValue(0.50)
        self.spin_conf.setSingleStep(0.05)
        layout.addRow("Confidence:", self.spin_conf)

        # Reliability
        self.spin_rel = QDoubleSpinBox()
        self.spin_rel.setRange(0.01, 0.99)
        self.spin_rel.setDecimals(2)
        self.spin_rel.setValue(0.50)
        self.spin_rel.setSingleStep(0.05)
        layout.addRow("Reliability:", self.spin_rel)

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
        }
