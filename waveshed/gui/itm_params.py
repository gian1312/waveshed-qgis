"""The ITM parameter group, one per analysis tab (360° and P2P).

Both tabs build their own instance (owner, 2026-11-14: P2P "gets its own"),
so a P2P link can be computed for other ground or climate than the 360° run
beside it. The widgets, ranges, defaults and labels live here once; the
defaults are the engine's (``job_builder.CoverageParams`` / ``P2PParams``).

**Earth Radius Mode** is NOT in the group: the earth's curvature matters for
LOS and LOS Floor as much as for ITM (owner, 2026-11-14), so each tab shows it
in its Analysis section in every mode (:func:`setup_earth_radius_combo`):
Real (k = 1) or 4/3 earth (owner, 2026-11-15). The default depends on the
mode family: LOS and LOS Floor start on Real, Propagation (SIMPLE_LOSS, ITM)
on 4/3; the user's pick is kept per family for the session
(:func:`apply_earth_radius_availability`). aether_core
``binary_manager.EARTH_REAL_MIN_ENGINE`` (0.5.2) and newer apply REAL in every
mode, backend and task; an older engine runs it as 4/3 without a word, so
there Real is greyed out and the selection is 4/3. ADVANCED (Doerry, CPU ITM
only) stays in the code behind :data:`EARTH_RADIUS_ADVANCED_ENABLED`.
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

from ..core.job_builder import EARTH_REAL, earth_radius_default

#: Radio climates as aether_core numbers them (config.rs validates 1–7).
RADIO_CLIMATE_DEFAULT = 5

EARTH_RADIUS_TIP = (
    "Earth-curvature model (horizon and diffraction geometry):\n"
    "Real (k = 1): the geometric earth, no refraction.\n"
    "4/3 earth: standard refraction.\n"
    "LOS and LOS Floor start on Real, Propagation on 4/3; your pick is kept "
    "for each."
)

#: The "Advanced (per path)" choice (Doerry k per path, CPU ITM only) is off
#: (owner, 2026-11-15: Real and 4/3 only for now): with ``False`` the combo
#: does not offer it and no job carries ADVANCED.
EARTH_RADIUS_ADVANCED_ENABLED = False

_ADVANCED_CHOICE = ("Advanced (per path)", "ADVANCED")
_ADVANCED_TIP = ("\nAdvanced (per path): a k-factor per path from the surface "
                 "refractivity and both endpoint heights.")

#: ``(label, engine value)`` of the Earth Radius Modes the tabs show (owner,
#: 2026-11-15: "select between the two"); see :func:`earth_radius_choices`.
EARTH_RADIUS_CHOICES = (
    ("Real (k = 1)", "REAL"),
    ("4/3 earth", "FOUR_THIRDS"),
)
#: The engine's own default, and the fallback when Real cannot be applied.
EARTH_RADIUS_DEFAULT = "FOUR_THIRDS"
_EARTH_RADIUS_BY_LABEL = {label: value for label, value
                          in EARTH_RADIUS_CHOICES + (_ADVANCED_CHOICE,)}

_ADVANCED_GEOMETRIC = ("LOS and LOS Floor are computed with the 4/3 earth "
                       "whatever is chosen.")
_ADVANCED_GPU = ("Applied by ITM on the CPU backend only: set Backend to CPU. "
                 "The GPU (also AUTO with a usable GPU) uses the 4/3 earth.")


def earth_radius_choices() -> tuple:
    """The ``(label, value)`` pairs the combo offers (ADVANCED only with
    :data:`EARTH_RADIUS_ADVANCED_ENABLED`)."""
    if EARTH_RADIUS_ADVANCED_ENABLED:
        return EARTH_RADIUS_CHOICES + (_ADVANCED_CHOICE,)
    return EARTH_RADIUS_CHOICES


def earth_radius_family(mode: str) -> str:
    """``"LOS"`` for LOS and LOS Floor (``"LOS"``, ``"MIN_ALT"``), ``"RF"``
    for Propagation (``"LOSS"``: SIMPLE_LOSS and ITM)."""
    return "LOS" if earth_radius_default(mode) == EARTH_REAL else "RF"


def _no_real_tip(version: Optional[str]) -> str:
    from ..core.binary_manager import EARTH_REAL_MIN_ENGINE
    return (f"Needs aether_core {EARTH_REAL_MIN_ENGINE} or newer (installed: "
            f"{version or 'unknown'}): update the engine in Settings → "
            "Download binaries.")


def earth_radius_unavailable(mode: str, loss_model: str, backend: str,
                             engine_version: Optional[str] = None) -> Dict[str, str]:
    """``{engine value: why it is greyed out}`` for this tab's context.

    REAL needs aether_core ``EARTH_REAL_MIN_ENGINE`` (*engine_version*;
    ``None`` = unknown = the oldest build): older engines run it as 4/3
    without a word. ADVANCED (when enabled) is read by ITM on the CPU backend
    only. 4/3 is always available. *mode*: "LOS" / "LOSS" / "MIN_ALT";
    *backend*: "AUTO" / "GPU" / "CPU" (AUTO usually means the GPU).
    """
    from ..core.binary_manager import engine_supports_real_earth
    out = {}
    if not engine_supports_real_earth(engine_version):
        out["REAL"] = _no_real_tip(engine_version)
    if mode != "LOSS":
        out["ADVANCED"] = _ADVANCED_GEOMETRIC
    elif str(loss_model).upper() != "ITM" or str(backend).upper() != "CPU":
        out["ADVANCED"] = _ADVANCED_GPU
    return out


def _wanted_key(family: str) -> str:
    return f"waveshed_wanted_{family}"


def setup_earth_radius_combo(combo) -> None:
    """Fill a tab's Earth Radius Mode combo: labels + engine values; the
    selection follows the tab's mode (:func:`apply_earth_radius_availability`),
    LOS until the tab sets one."""
    for label, value in earth_radius_choices():
        combo.addItem(label, value)
    combo.setToolTip(EARTH_RADIUS_TIP + (
        _ADVANCED_TIP if EARTH_RADIUS_ADVANCED_ENABLED else ""))
    combo.setProperty("waveshed_family", "LOS")
    combo.setCurrentIndex(
        [v for _, v in earth_radius_choices()].index(EARTH_RADIUS_DEFAULT))
    # The user's own pick, per mode family, kept while another family is
    # shown or the engine greys it out.
    combo.activated.connect(
        lambda _i, c=combo: c.setProperty(
            _wanted_key(c.property("waveshed_family") or "LOS"),
            earth_radius_value(c)))


def earth_radius_value(combo) -> str:
    """The engine value of the combo's current choice (never ADVANCED while
    :data:`EARTH_RADIUS_ADVANCED_ENABLED` is off: then the default of the
    combo's mode family)."""
    value = ""
    data = getattr(combo, "currentData", None)
    if callable(data):
        value = data()
    if not (isinstance(value, str) and value):
        text = combo.currentText()
        value = _EARTH_RADIUS_BY_LABEL.get(text, text)
    if value == "ADVANCED" and not EARTH_RADIUS_ADVANCED_ENABLED:
        family = getattr(combo, "property", None)
        family = family("waveshed_family") if callable(family) else None
        return "FOUR_THIRDS" if family == "RF" else "REAL"
    return value


def apply_earth_radius_availability(combo, mode: str, loss_model: str,
                                    backend: str,
                                    engine_version: Optional[str] = None) -> None:
    """Grey out what the engine cannot apply here (tooltip says why) and set
    the selection for *mode*'s family: the user's own pick there when it is
    available, else the family's default (:func:`earth_radius_default`), else
    4/3. Silent on a combo without an item model (the unit-test stand-ins)."""
    model = getattr(combo, "model", None)
    if not callable(model):
        return
    unavailable = earth_radius_unavailable(mode, loss_model, backend,
                                           engine_version)
    items = model()
    for index in range(combo.count()):
        item = items.item(index) if hasattr(items, "item") else None
        if item is None:
            continue
        why = unavailable.get(combo.itemData(index), "")
        item.setEnabled(not why)
        combo.setItemData(index, why or EARTH_RADIUS_TIP,
                          Qt.ItemDataRole.ToolTipRole)
    family = earth_radius_family(mode)
    combo.setProperty("waveshed_family", family)
    target = EARTH_RADIUS_DEFAULT
    for value in (combo.property(_wanted_key(family)),
                  earth_radius_default(mode)):
        if value and value not in unavailable and combo.findData(value) >= 0:
            target = value
            break
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
