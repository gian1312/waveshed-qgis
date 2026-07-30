"""Unit tests for the .abt terrain tile reader (``core.abt``).

The tiles are written here rather than fixtured so the header layout under test
is the one this package documents — a stale binary fixture would keep passing
after the format moved.

QGIS stubs provided by conftest.py.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import os
import struct
import tempfile
import unittest

from waveshed.core import abt

try:
    import numpy as np
    _HAVE_NUMPY = True
except Exception:  # noqa: BLE001 — numpy ships with QGIS, so this should never
    # fire. Catch broadly anyway: a wheel built for another platform raises
    # AttributeError rather than ImportError, and an uncaught one here aborts
    # collection for the whole suite, not just this file.
    _HAVE_NUMPY = False


def _aligned_stride(size: int) -> int:
    """The 256-byte-aligned row stride aether_converter writes."""
    return (size * 2 + 255) & ~255


def _write_tile(path, size, ul_lat, ul_lon, pixel_res, values=None,
                stride=None, magic=b"AETH", scale_x=None):
    """Write a synthetic .abt tile; *values* is a size x size int16 iterable."""
    stride = _aligned_stride(size) if stride is None else stride
    scale_x = pixel_res if scale_x is None else scale_x
    header = struct.pack(
        "<4sHHddddhH", magic, 1, size, ul_lat, ul_lon, pixel_res, scale_x,
        0, stride,
    )
    with open(path, "wb") as handle:
        handle.write(header)
        for row in range(size):
            for col in range(size):
                value = 0 if values is None else int(values[row][col])
                handle.write(struct.pack("<h", value))
            handle.write(b"\x00" * (stride - size * 2))
    return path


class TestHeader(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _path(self, name="tile.abt"):
        return os.path.join(self.tmp.name, name)

    def test_reads_the_documented_layout(self):
        path = _write_tile(self._path(), 4, 47.5, 8.0, 0.001)
        header = abt.read_header(path)
        self.assertIsNotNone(header)
        self.assertEqual(header.size, 4)
        self.assertAlmostEqual(header.ul_lat, 47.5)
        self.assertAlmostEqual(header.ul_lon, 8.0)
        self.assertAlmostEqual(header.pixel_res, 0.001)
        self.assertEqual(header.stride, _aligned_stride(4))

    def test_whole_tile_span_in_scale_x_is_divided_out(self):
        # Older converter builds wrote the tile's span, not the pixel step.
        path = _write_tile(self._path(), 4, 47.5, 8.0, 0.001, scale_x=0.01)
        header = abt.read_header(path)
        self.assertAlmostEqual(header.pixel_res, 0.01 / 4)

    def test_rejects_foreign_and_truncated_files(self):
        bad_magic = _write_tile(self._path("x.abt"), 4, 0, 0, 0.001,
                                magic=b"NOPE")
        self.assertIsNone(abt.read_header(bad_magic))

        short = self._path("short.abt")
        with open(short, "wb") as handle:
            handle.write(b"AETH")
        self.assertIsNone(abt.read_header(short))

        self.assertIsNone(abt.read_header(self._path("missing.abt")))

    def test_list_tiles_is_flat_and_sorted(self):
        _write_tile(self._path("b.abt"), 2, 0, 0, 0.001)
        _write_tile(self._path("a.abt"), 2, 0, 0, 0.001)
        with open(self._path("notes.txt"), "w") as handle:
            handle.write("not a tile")
        nested = os.path.join(self.tmp.name, "sub")
        os.makedirs(nested)
        _write_tile(os.path.join(nested, "c.abt"), 2, 0, 0, 0.001)

        found = [os.path.basename(p) for p in abt.list_tiles(self.tmp.name)]
        self.assertEqual(found, ["a.abt", "b.abt"])

    def test_missing_directory_lists_nothing(self):
        self.assertEqual(abt.list_tiles(self._path("nope")), [])


@unittest.skipUnless(_HAVE_NUMPY, "numpy not available")
class TestTileData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_reads_past_row_padding(self):
        values = [[1, 2], [3, 4]]
        path = _write_tile(
            os.path.join(self.tmp.name, "t.abt"), 2, 1.0, 0.0, 0.5, values,
        )
        header = abt.read_header(path)
        self.assertGreater(header.stride, header.size * 2)  # padding present
        grid = abt.read_tile(header)
        self.assertEqual(grid.tolist(), values)

    def test_truncated_body_reads_as_no_tile(self):
        path = os.path.join(self.tmp.name, "t.abt")
        _write_tile(path, 2, 1.0, 0.0, 0.5, [[1, 2], [3, 4]])
        header = abt.read_header(path)
        with open(path, "rb") as handle:
            head = handle.read(abt.HEADER_SIZE + 2)
        with open(path, "wb") as handle:
            handle.write(head)
        self.assertIsNone(abt.read_tile(header))


class TestMosaicGrid(unittest.TestCase):
    @staticmethod
    def _header(ul_lat, ul_lon, size=100, pixel_res=0.01):
        return abt.TileHeader("t.abt", size, ul_lat, ul_lon, pixel_res, size * 2)

    def test_no_tiles_no_grid(self):
        self.assertIsNone(abt.mosaic_grid([]))

    def test_spans_every_tile_at_the_finest_resolution(self):
        # Two tiles side by side, the right one at twice the detail.
        grid = abt.mosaic_grid([
            self._header(1.0, 0.0, size=100, pixel_res=0.01),   # 0.0 .. 1.0 lon
            self._header(1.0, 1.0, size=200, pixel_res=0.005),  # 1.0 .. 2.0 lon
        ])
        self.assertAlmostEqual(grid.west, 0.0)
        self.assertAlmostEqual(grid.north, 1.0)
        self.assertAlmostEqual(grid.resolution, 0.005)
        self.assertEqual(grid.width, 400)   # 2 degrees at 0.005
        self.assertEqual(grid.height, 200)  # 1 degree at 0.005
        self.assertFalse(grid.decimated)

    def test_coarsens_to_the_pixel_budget(self):
        grid = abt.mosaic_grid([self._header(1.0, 0.0, 1000, 0.001)], max_dim=100)
        self.assertTrue(grid.decimated)
        self.assertLessEqual(max(grid.width, grid.height), 100)
        # Still covers the same ground.
        self.assertAlmostEqual(grid.west, 0.0)
        self.assertAlmostEqual(grid.width * grid.resolution, 1.0, places=6)

    def test_no_phantom_edge_from_float_noise(self):
        # (1.0 - 0.998) / 0.001 evaluates to 2.0000000000000018, and a bare
        # ceil() of that would add a row of no-data along the south edge.
        grid = abt.mosaic_grid([self._header(1.0, 0.0, size=2, pixel_res=0.001)])
        self.assertEqual((grid.width, grid.height), (2, 2))

    def test_geo_transform_is_north_up(self):
        grid = abt.mosaic_grid([self._header(47.0, 8.0, 100, 0.01)])
        west, x_res, x_skew, north, y_skew, y_res = grid.geo_transform
        self.assertAlmostEqual(west, 8.0)
        self.assertAlmostEqual(north, 47.0)
        self.assertAlmostEqual(x_res, 0.01)
        self.assertAlmostEqual(y_res, -0.01)
        self.assertEqual((x_skew, y_skew), (0.0, 0.0))


@unittest.skipUnless(_HAVE_NUMPY, "numpy not available")
class TestPasteTile(unittest.TestCase):
    def _canvas(self, shape=(4, 4)):
        return np.full(shape, abt.MOSAIC_NODATA_M, dtype=np.float32)

    def test_pastes_at_the_offset(self):
        canvas = self._canvas()
        abt.paste_tile(canvas, np.full((2, 2), 5.0, dtype=np.float32), 1, 2)
        self.assertEqual(canvas[1, 2], 5.0)
        self.assertEqual(canvas[2, 3], 5.0)
        self.assertEqual(canvas[0, 0], abt.MOSAIC_NODATA_M)

    def test_clips_on_every_side(self):
        canvas = self._canvas((2, 2))
        abt.paste_tile(canvas, np.full((4, 4), 7.0, dtype=np.float32), -1, -1)
        self.assertEqual(canvas.tolist(), [[7.0, 7.0], [7.0, 7.0]])

        canvas = self._canvas((2, 2))
        abt.paste_tile(canvas, np.full((4, 4), 7.0, dtype=np.float32), 1, 1)
        self.assertEqual(canvas[1, 1], 7.0)
        self.assertEqual(canvas[0, 0], abt.MOSAIC_NODATA_M)

    def test_a_tile_entirely_outside_is_a_no_op(self):
        canvas = self._canvas((2, 2))
        abt.paste_tile(canvas, np.ones((2, 2), dtype=np.float32), 5, 5)
        abt.paste_tile(canvas, np.ones((2, 2), dtype=np.float32), -9, 0)
        self.assertEqual(canvas.tolist(),
                         [[abt.MOSAIC_NODATA_M] * 2] * 2)

    def test_void_pixels_do_not_erase_a_neighbour(self):
        canvas = self._canvas((1, 2))
        abt.paste_tile(canvas, np.array([[3.0, 4.0]], dtype=np.float32), 0, 0)
        void = np.array([[abt.MOSAIC_NODATA_M, 9.0]], dtype=np.float32)
        abt.paste_tile(canvas, void, 0, 0)
        self.assertEqual(canvas.tolist(), [[3.0, 9.0]])


@unittest.skipUnless(_HAVE_NUMPY, "numpy not available")
class TestBuildMosaic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    # 2 px at 0.001 degrees: one tile spans 0.002 degrees of longitude.
    RES = 0.001
    SPAN = 0.002

    def test_two_tiles_land_side_by_side_in_metres(self):
        # Counts are half-metres: 20 -> 10 m, 40 -> 20 m.
        left = _write_tile(
            os.path.join(self.tmp.name, "l.abt"), 2, 1.0, 0.0, self.RES,
            [[20, 20], [20, 20]],
        )
        right = _write_tile(
            os.path.join(self.tmp.name, "r.abt"), 2, 1.0, self.SPAN, self.RES,
            [[40, 40], [40, 40]],
        )
        headers = abt.read_headers([left, right])
        grid = abt.mosaic_grid(headers)
        canvas = abt.build_mosaic(headers, grid)

        self.assertEqual(canvas.shape, (2, 4))
        self.assertEqual(canvas[0, :2].tolist(), [10.0, 10.0])
        self.assertEqual(canvas[0, 2:].tolist(), [20.0, 20.0])

    def test_cancel_stops_the_assembly(self):
        # Mosaicking a whole terrain cache is the slow first step of the
        # sea-level view; without a way out, closing QGIS mid-build leaves a
        # thread running past the dock that owns it.
        tile = _write_tile(
            os.path.join(self.tmp.name, "a.abt"), 2, 1.0, 0.0, self.RES,
            [[20, 20], [20, 20]],
        )
        headers = abt.read_headers([tile])
        with self.assertRaises(abt.MosaicCanceled):
            abt.build_mosaic(headers, abt.mosaic_grid(headers),
                             should_cancel=lambda: True)

    def test_uncovered_ground_stays_nodata(self):
        # Two tiles with one tile's worth of gap between them: the gap has no
        # data and must not read as sea level.
        near = _write_tile(
            os.path.join(self.tmp.name, "a.abt"), 2, 1.0, 0.0, self.RES,
            [[20, 20], [20, 20]],
        )
        far = _write_tile(
            os.path.join(self.tmp.name, "b.abt"), 2, 1.0, self.SPAN * 2,
            self.RES, [[20, 20], [20, 20]],
        )
        headers = abt.read_headers([near, far])
        canvas = abt.build_mosaic(headers, abt.mosaic_grid(headers))
        self.assertEqual(canvas.shape, (2, 6))
        self.assertEqual(canvas[0, 2:4].tolist(),
                         [abt.MOSAIC_NODATA_M, abt.MOSAIC_NODATA_M])


if __name__ == "__main__":
    unittest.main()
