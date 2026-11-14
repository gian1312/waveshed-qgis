"""The ITM parameter group, one per analysis tab (360° and P2P).

Both tabs build their own instance (owner, 2026-11-14: P2P "gets its own"),
so a P2P link can be computed for other ground or climate than the 360° run
beside it. The widgets, ranges, defaults and labels live here once; the
defaults are the engine's (``job_builder.CoverageParams`` / ``P2PParams``).

**Earth Radius Mode** is NOT in the group: the earth's curvature matters for
LOS and LOS Floor as much as for ITM (owner, 2026-11-14), so each tab shows it
in its Analysis section in every mode (:func:`setup_earth_radius_combo`), with
what the engine cannot apply greyed out (:func:`apply_earth_radius_availability`).
What this engine version does with it (READ, AETHER 0.5.1): only the CPU ITM
paths read ``propagation.earth_radius_mode`` (engines/cpu_coverage.rs,
engines/p2p.rs ``run_p2p_cpu``); LOS, LOS Floor and every GPU path compute
with a fixed k = 4/3 (shader.wgsl), and there is no "real earth" (k = 1)
mode yet. The tooltip says so.
"""

from __future__ import annotations

from typing import Dict, Optional

from qgis.PyQt.QtCore import Qt
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
    "Real (k = 1): the geometric earth, no refraction.\n"
    "4/3 earth: standard refraction (normal, recommended).\n"
    "Advanced (per path): a k-factor per path from the surface refractivity "
    "and both endpoint heights.\n"
    "Greyed-out choices are what this engine version cannot apply here yet."
)

#: ``(label, engine value)`` of every Earth Radius Mode the tabs show
#: (owner, 2026-11-14: "Real vs 4/3 vs advanced"; the ones the engine cannot
#: apply in the current mode / backend are greyed out, see
#: :func:`earth_radius_unavailable`).
EARTH_RADIUS_CHOICES = (
    ("Real (k = 1)", "REAL"),
    ("4/3 earth", "FOUR_THIRDS"),
    ("Advanced (per path)", "ADVANCED"),
)
EARTH_RADIUS_DEFAULT = "FOUR_THIRDS"
_EARTH_RADIUS_BY_LABEL = {label: value for label, value in EARTH_RADIUS_CHOICES}

_NO_REAL = ("Needs an engine update (planned): no aether_core version computes "
            "with the real earth radius (k = 1) yet.")
_ADVANCED_GEOMETRIC = ("LOS and LOS Floor are computed with the 4/3 earth "
                       "whatever is chosen (engine update planned).")
_ADVANCED_GPU = ("Applied by ITM on the CPU backend only: set Backend to CPU. "
                 "The GPU (also AUTO with a usable GPU) uses the 4/3 earth.")


def earth_radius_unavailable(mode: str, loss_model: str, backend: str) -> Dict[str, str]:
    """``{engine value: why it is greyed out}`` for this tab's context.

    What aether_core 0.5.1 applies (READ: config.rs, cpu_coverage.rs,
    engines/p2p.rs, shader.wgsl): ``earth_radius_mode`` is read only by ITM on
    the CPU backend, where ADVANCED is per path; every LOS / LOS Floor path and
    every GPU path uses a fixed k = 4/3; no path knows k = 1. 4/3 is always
    available. *mode*: "LOS" / "LOSS" / "MIN_ALT"; *backend*: "AUTO" / "GPU" /
    "CPU" (AUTO usually means the GPU).
    """
    out = {"REAL": _NO_REAL}
    if mode != "LOSS":
        out["ADVANCED"] = _ADVANCED_GEOMETRIC
    elif str(loss_model).upper() != "ITM" or str(backend).upper() != "CPU":
        out["ADVANCED"] = _ADVANCED_GPU
    return out


def setup_earth_radius_combo(combo) -> None:
    """Fill a tab's Earth Radius Mode combo: labels + engine values, 4/3."""
    for label, value in EARTH_RADIUS_CHOICES:
        combo.addItem(label, value)
    combo.setToolTip(EARTH_RADIUS_TIP)
    combo.setCurrentIndex(
        [v for _, v in EARTH_RADIUS_CHOICES].index(EARTH_RADIUS_DEFAULT))
    # The user's own pick, kept while the context greys it out.
    combo.setProperty("waveshed_wanted", EARTH_RADIUS_DEFAULT)
    combo.activated.connect(
        lambda _i, c=combo: c.setProperty("waveshed_wanted", earth_radius_value(c)))


def earth_radius_value(combo) -> str:
    """The engine value of the combo's current choice."""
    data = getattr(combo, "currentData", None)
    if callable(data):
        value = data()
        if isinstance(value, str) and value:
            return value
    text = combo.currentText()
    return _EARTH_RADIUS_BY_LABEL.get(text, text)


def apply_earth_radius_availability(combo, mode: str, loss_model: str,
                                    backend: str) -> None:
    """Grey out what the engine cannot apply here (tooltip says why) and keep
    the selection on an available choice: the user's own pick when it is
    available again, else 4/3. Silent on a combo without an item model (the
    unit-test stand-ins)."""
    model = getattr(combo, "model", None)
    if not callable(model):
        return
    unavailable = earth_radius_unavailable(mode, loss_model, backend)
    items = model()
    for index, (_label, value) in enumerate(EARTH_RADIUS_CHOICES):
        item = items.item(index) if hasattr(items, "item") else None
        if item is None:
            continue
        why = unavailable.get(value, "")
        item.setEnabled(not why)
        combo.setItemData(index, why or EARTH_RADIUS_TIP,
                          Qt.ItemDataRole.ToolTipRole)
    wanted = combo.property("waveshed_wanted") or EARTH_RADIUS_DEFAULT
    target = wanted if wanted not in unavailable else EARTH_RADIUS_DEFAULT
    index = combo.findData(target)
    if index >= 0 and index != combo.currentIndex():
        combo.setCurrentIndex(index)


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
