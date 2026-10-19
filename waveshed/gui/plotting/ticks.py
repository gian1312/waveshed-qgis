"""Nice-number tick math for the QPainter plotting package.

PURE MODULE: imports nothing but :mod:`math` so it stays unit-testable even
when the test harness mocks all of Qt. Do not add any Qt (or numpy) imports
here — :mod:`tests.unit.test_plotting_ticks` loads this file in isolation to
prove that property.
"""

import math
from typing import List

__all__ = ["nice_ticks", "tick_format"]


def _decimals_for_step(step: float) -> int:
    """Number of decimal places needed to render a value at ``step`` resolution.

    ``step`` of 20 -> 0 decimals, 0.5 -> 1, 0.05 -> 2. A tiny nudge on the
    ``log10`` result keeps values that are exact powers of ten (whose float
    log can land at ``x.9999999``) on the correct decade.
    """
    if step <= 0 or not math.isfinite(step):
        return 0
    exp = math.floor(math.log10(abs(step)) + 1e-9)
    return max(0, -exp)


def nice_ticks(vmin: float, vmax: float, target: int = 6) -> List[float]:
    """Return "nice" round tick values covering ``[vmin, vmax]``.

    Produces roughly ``target`` ticks using the standard 1/2/5x10^n
    progression, so labels fall on human-friendly numbers.

    Args:
        vmin: Lower bound of the data range.
        vmax: Upper bound of the data range.
        target: Approximate number of ticks desired.

    Returns:
        Ascending list of tick values, cleaned of floating-point dust. A flat
        range (``vmin == vmax``) is padded so it still yields ticks; a reversed
        range is swapped; a non-finite range yields ``[]``.
    """
    vmin = float(vmin)
    vmax = float(vmax)
    if not (math.isfinite(vmin) and math.isfinite(vmax)):
        return []

    # 1. Normalise the input range.
    if vmax < vmin:
        vmin, vmax = vmax, vmin
    if vmin == vmax:
        if vmin == 0.0:
            vmin, vmax = -0.5, 0.5
        else:
            pad = abs(vmin) * 0.05
            vmin, vmax = vmin - pad, vmax + pad

    # 2-3. Choose a 1/2/5 step near the requested tick density.
    raw = (vmax - vmin) / max(1, int(target))
    mag = 10.0 ** math.floor(math.log10(raw))
    norm = raw / mag
    if norm < 1.5:
        step = 1.0 * mag
    elif norm < 3.0:
        step = 2.0 * mag
    elif norm < 7.0:
        step = 5.0 * mag
    else:
        step = 10.0 * mag

    # 4-5. Emit multiples of step across the range, rounded to step's decade.
    decimals = _decimals_for_step(step)
    first = math.ceil(vmin / step) * step
    limit = vmax + step * 1e-9
    ticks: List[float] = []
    value = first
    guard = 0
    while value <= limit and guard < 100000:
        rounded = round(value, decimals)
        if rounded == 0.0:  # collapse -0.0 to 0.0 for clean labels
            rounded = 0.0
        ticks.append(rounded)
        value += step
        guard += 1
    return ticks


def tick_format(value: float, step: float) -> str:
    """Format ``value`` with just enough decimals for a tick spacing of ``step``.

    Trailing zeros are stripped and whole numbers render without a decimal
    point, so a step of 0.5 renders ``5``/``5.5`` rather than ``5.0``/``5.50``.
    """
    value = float(value)
    if value == 0.0:  # collapse -0.0
        value = 0.0
    decimals = _decimals_for_step(float(step))
    if decimals == 0:
        return str(int(round(value)))
    text = f"{value:.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0", "-0.0"):
        text = "0"
    return text
