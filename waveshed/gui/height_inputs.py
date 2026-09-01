"""Mode-dependent minimums for antenna-height spinboxes.

Every height field in the plugin is paired with an AGL/AMSL combo, and the two
modes want opposite bounds:

* **AGL** is a height above the ground, so it has a hard floor —
  :data:`~..core.job_builder.MIN_ANTENNA_AGL_M`. Below it ITM is undefined and
  the line-of-sight decision falls under the terrain data's own rounding, so
  ``job_builder`` rejects the job outright. Stopping the value at the spinbox
  means the user meets the limit while typing instead of at run time.

* **AMSL** is an absolute elevation, which is legitimately zero or negative —
  the Dead Sea shore is -430 m and Schiphol is -4 m. Every AMSL-capable spinbox
  in the plugin used to start at 0.0, so those sites could not be entered at
  all. :data:`MIN_AMSL_M` opens the range far enough to cover the lowest dry
  land on Earth.

The single helper here keeps the two numbers, and the wiring that swaps between
them, from drifting across the three tabs that need it.
"""

from __future__ import annotations

from typing import Any

from ..core.job_builder import MIN_ANTENNA_AGL_M, MIN_ANTENNA_AMSL_M

#: Lowest AMSL elevation any height spinbox accepts — the SAME constant
#: ``height_floor_error`` gates the spinbox-less paths (batch CSVs, the
#: Processing algorithms) with, so the dialog and the gate can never drift.
#: A local -500.0 here drifted exactly that way: the gate had no AMSL floor
#: at all, and -501 m sailed through every non-GUI path.
MIN_AMSL_M = MIN_ANTENNA_AMSL_M


def bind_height_mode(spin: Any, combo: Any) -> None:
    """Make *spin*'s minimum follow the AGL/AMSL selection in *combo*.

    Applies the correct minimum immediately and re-applies it whenever the mode
    changes. Qt clamps the current value into the new range on ``setMinimum``,
    so switching AMSL -> AGL lifts a below-floor value to the floor rather than
    leaving an out-of-range number in the box.

    Args:
        spin: A ``QDoubleSpinBox`` holding a height in metres.
        combo: The ``QComboBox`` holding ``"AGL"`` / ``"AMSL"`` for that height.
    """

    def _apply(*_args: Any) -> None:
        text = combo.currentText() if hasattr(combo, "currentText") else "AGL"
        is_agl = (text or "AGL").strip().upper() != "AMSL"
        spin.setMinimum(MIN_ANTENNA_AGL_M if is_agl else MIN_AMSL_M)

    combo.currentTextChanged.connect(_apply)
    _apply()
