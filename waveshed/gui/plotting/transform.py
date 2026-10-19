"""Linear data<->pixel coordinate mapping for the plotting package.

PURE MODULE: no Qt (or numpy) imports, so it stays unit-testable under the
Qt-mocked test harness. :mod:`tests.unit.test_plotting_transform` loads this
file in isolation to prove that property.
"""

__all__ = ["LinearScale"]


class LinearScale:
    """Affine map between a data interval and a pixel interval.

    For the Y axis the caller passes ``pmin=bottom_pixel`` and
    ``pmax=top_pixel`` (i.e. inverted, because screen Y grows downward), so
    :meth:`px` naturally produces screen-down coordinates.
    """

    def __init__(self, dmin: float, dmax: float, pmin: float, pmax: float) -> None:
        """Build a scale from data range ``[dmin, dmax]`` to pixels ``[pmin, pmax]``.

        A degenerate data domain (``dmin == dmax``) is widened to a tiny span so
        :meth:`px` never divides by zero; values then map to the pixel midpoint.
        """
        dmin = float(dmin)
        dmax = float(dmax)
        if dmin == dmax:
            eps = abs(dmin) * 1e-9 if dmin != 0.0 else 1e-9
            if eps == 0.0:
                eps = 1e-9
            dmin -= eps
            dmax += eps
        self.dmin = dmin
        self.dmax = dmax
        self.pmin = float(pmin)
        self.pmax = float(pmax)

    def px(self, v: float) -> float:
        """Map a data value ``v`` to its pixel position."""
        frac = (float(v) - self.dmin) / (self.dmax - self.dmin)
        return self.pmin + frac * (self.pmax - self.pmin)

    def data(self, p: float) -> float:
        """Map a pixel position ``p`` back to a data value (inverse of :meth:`px`)."""
        if self.pmax == self.pmin:
            return self.dmin
        frac = (float(p) - self.pmin) / (self.pmax - self.pmin)
        return self.dmin + frac * (self.dmax - self.dmin)
