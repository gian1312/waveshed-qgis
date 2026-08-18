"""Unit tests for tools/abt_diag.py's .abt reader.

abt_diag is the diagnostic you reach for when chasing stripe/patch gaps, so it
being wrong about tile geometry is expensive: it reported every gap's lat/lon
from its own header parsing, which

  * recomputed the row stride from the width instead of reading the header
    field, and
  * skipped the legacy ``scale_x`` correction ``core.abt.read_header`` applies
    (older converter builds wrote the whole tile SPAN in scale_x, not the
    per-pixel step),

so on a legacy tile every reported coordinate was off by a factor of ``size``.
It now reads headers through ``waveshed.core.abt``.
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401

import os
import struct
import sys
import tempfile
import unittest

try:
    import numpy as np
    _HAVE_NUMPY = True
except Exception:  # noqa: BLE001 — a wheel for another platform raises too.
    _HAVE_NUMPY = False

_TOOLS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)


def _write_abt(path, size, scale_x, scale_y=None, stride=None,
               ul_lat=47.0, ul_lon=8.0, fill=None):
    """Write a complete .abt with an arbitrary header stride and scale."""
    stride = stride if stride is not None else (size * 2 + 255) & ~255
    scale_y = scale_x if scale_y is None else scale_y
    header = (b"AETH" + struct.pack("<HH", 1, size)
              + struct.pack("<dddd", ul_lat, ul_lon, scale_y, scale_x)
              + struct.pack("<hH", 0, stride))
    with open(path, "wb") as handle:
        handle.write(header)
        for row in range(size):
            value = row if fill is None else fill
            handle.write(struct.pack(f"<{size}h", *([value] * size)))
            handle.write(b"\xaa" * (stride - size * 2))


@unittest.skipUnless(_HAVE_NUMPY, "numpy required")
class TestAbtDiagUsesThePluginReader(unittest.TestCase):

    def setUp(self):
        import abt_diag
        self.abt_diag = abt_diag

    def test_legacy_scale_x_is_corrected_to_a_per_pixel_step(self):
        # A legacy tile stores the tile SPAN (0.1 deg) in scale_x. Read as a
        # per-pixel step, every gap coordinate came out `size` times too far
        # east — the exact class of bug this script exists to find.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "legacy.abt")
            _write_abt(path, size=8, scale_x=0.1)
            _elev, _lat, _lon, res_y, res_x = self.abt_diag.load_abt(path)
        self.assertAlmostEqual(res_x, 0.1 / 8)
        self.assertAlmostEqual(res_y, 0.1 / 8)

    def test_modern_per_pixel_scale_is_left_alone(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "modern.abt")
            _write_abt(path, size=8, scale_x=0.0001)
            _elev, _lat, _lon, _res_y, res_x = self.abt_diag.load_abt(path)
        self.assertAlmostEqual(res_x, 0.0001)

    def test_row_stride_comes_from_the_header_not_a_recomputation(self):
        # A tile whose stride is not the 256-aligned value the old code
        # assumed was decoded at the wrong pitch: rows sheared into each other
        # and every "gap" it then reported was an artefact of the reader.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "odd_stride.abt")
            _write_abt(path, size=8, scale_x=0.0001, stride=300)
            elev, _lat, _lon, _ry, _rx = self.abt_diag.load_abt(path)
        self.assertEqual(elev.shape, (8, 8))
        for row in range(8):
            self.assertTrue((elev[row] == row).all(), elev[row])

    def test_a_non_abt_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "not.abt")
            with open(path, "wb") as handle:
                handle.write(b"NOPE" + b"\0" * 100)
            self.assertIsNone(self.abt_diag.load_abt(path))

    def test_a_truncated_body_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "short.abt")
            _write_abt(path, size=8, scale_x=0.0001)
            with open(path, "rb") as handle:
                head = handle.read(44 + 100)
            with open(path, "wb") as handle:
                handle.write(head)
            self.assertIsNone(self.abt_diag.load_abt(path))

    def test_inferred_zoom_uses_the_corrected_resolution(self):
        # first_abt_params feeds infer_zoom, so the legacy correction decides
        # which slippy tile a gap gets blamed on.
        with tempfile.TemporaryDirectory() as d:
            _write_abt(os.path.join(d, "legacy.abt"), size=1024, scale_x=0.25)
            scale_x, centre_lat = self.abt_diag.first_abt_params(d)
        self.assertAlmostEqual(scale_x, 0.25 / 1024)
        # ~27 m/px at this latitude -> z12. Read uncorrected, the same tile
        # looked like 27 km/px and the script blamed z2 for every gap.
        self.assertEqual(self.abt_diag.infer_zoom(scale_x, centre_lat), 12)
        self.assertEqual(self.abt_diag.infer_zoom(0.25, centre_lat), 2)


if __name__ == "__main__":
    unittest.main()
