"""Unit tests for map_converter_tab — estimation, job building, helpers.

QGIS stubs provided by conftest.py.
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401

import math
import os
import re
import tempfile
import unittest
from unittest import mock

from waveshed.gui.map_converter_tab import (
    _abt_tile_name,
    _estimate_tile_count_and_mb,
    _enumerate_tiles,
    _snap_bbox,
    _split_sublayer,
    _ABT_EXTENT_DEG,
    _LayerEntry,
    _MapConverterWorker,
    _fgb_cache_name,
    _fgb_translate_options,
    _list_terrain_files,
    _detect_xyz_resolution,
)
from waveshed.core.terrain_adapter import _tile_params
import waveshed.gui.map_converter_tab as mct


def _abt_size_px(res_m: int, extent_deg: float) -> int:
    """Tile pixel width, via the one sizing function the plugin now has."""
    return _tile_params(extent_deg, 0.0, 0.0, res_m)["size_px"]


class TestFgbCacheName(unittest.TestCase):
    """The conversion cache lives in a shared, session-spanning temp dir, so
    its file name must pin everything that determines the output."""

    def _write(self, d, name, content="x"):
        p = os.path.join(d, name)
        with open(p, "w") as fh:
            fh.write(content)
        return p

    def test_same_basename_from_different_dirs_do_not_collide(self):
        # Regression: `a/buildings.shp` and `b/buildings.shp` used to map onto
        # one cached .fgb, so the second silently got the first's geometry.
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "a"))
            os.makedirs(os.path.join(d, "b"))
            p1 = self._write(os.path.join(d, "a"), "buildings.shp", "one")
            p2 = self._write(os.path.join(d, "b"), "buildings.shp", "two")
            self.assertNotEqual(_fgb_cache_name(p1), _fgb_cache_name(p2))

    def test_declared_crs_changes_the_name(self):
        # Regression: the cache was consulted before src_crs, so re-declaring a
        # source's CRS reused the conversion made under the old one.
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "b.fgb")
            self.assertNotEqual(
                _fgb_cache_name(p, "EPSG:2056"), _fgb_cache_name(p, "EPSG:32632"))
            self.assertNotEqual(_fgb_cache_name(p, ""), _fgb_cache_name(p, "EPSG:2056"))

    def test_editing_the_source_changes_the_name(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "b.fgb", "short")
            before = _fgb_cache_name(p, "EPSG:2056")
            with open(p, "w") as fh:
                fh.write("a much longer body")
            self.assertNotEqual(_fgb_cache_name(p, "EPSG:2056"), before)

    def test_name_is_stable_for_an_unchanged_source(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "b.fgb")
            self.assertEqual(
                _fgb_cache_name(p, "EPSG:2056"), _fgb_cache_name(p, "EPSG:2056"))

    def test_pre_fix_cache_entries_are_unreachable(self):
        # Conversions cached before -s_srs was honoured were plain
        # "<basename>.fgb". Those must no longer be returned, or the fix would
        # never apply to anyone who already has one.
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "buildings.shp")
            self.assertNotEqual(_fgb_cache_name(p, "EPSG:2056"), "buildings.fgb")

    def test_name_stays_recognisable_and_is_an_fgb(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "buildings.shp")
            name = _fgb_cache_name(p, "EPSG:2056")
            self.assertTrue(name.startswith("buildings_"), name)
            self.assertTrue(name.endswith(".fgb"), name)


class TestFgbTranslateOptions(unittest.TestCase):
    """Buildings must always reach the converter in WGS84."""

    def test_always_targets_wgs84_flatgeobuf(self):
        opts = _fgb_translate_options()
        self.assertIn("-t_srs", opts)
        self.assertEqual(opts[opts.index("-t_srs") + 1], "EPSG:4326")
        self.assertEqual(opts[opts.index("-f") + 1], "FlatGeobuf")

    def test_no_source_crs_leaves_s_srs_off(self):
        # GDAL should use the file's embedded CRS when we don't know better.
        self.assertNotIn("-s_srs", _fgb_translate_options(""))

    def test_known_source_crs_is_declared(self):
        # Regression: src_crs used to be accepted and then ignored, so a file
        # without an embedded CRS was reprojected from the wrong origin.
        opts = _fgb_translate_options("EPSG:2056")
        self.assertIn("-s_srs", opts)
        self.assertEqual(opts[opts.index("-s_srs") + 1], "EPSG:2056")


class TestEstimateTileCountAndMb(unittest.TestCase):

    def test_single_res_small_bbox(self):
        bbox = {"north": 48.0, "south": 47.0, "east": 9.0, "west": 8.0}
        tiles, mb = _estimate_tile_count_and_mb(bbox, [30])
        # 0.5 deg tiles: 2x2 = 4
        self.assertEqual(tiles, 4)
        self.assertGreater(mb, 0)

    def test_multiple_res(self):
        bbox = {"north": 48.0, "south": 47.0, "east": 9.0, "west": 8.0}
        _, mb1 = _estimate_tile_count_and_mb(bbox, [30])
        _, mb2 = _estimate_tile_count_and_mb(bbox, [10, 30])
        self.assertGreater(mb2, mb1)

    def test_90m_1deg_tiles(self):
        bbox = {"north": 48.0, "south": 47.0, "east": 9.0, "west": 8.0}
        tiles, _ = _estimate_tile_count_and_mb(bbox, [90])
        self.assertEqual(tiles, 1)

    def test_250m_2deg_tiles(self):
        bbox = {"north": 48.0, "south": 47.0, "east": 9.0, "west": 8.0}
        tiles, _ = _estimate_tile_count_and_mb(bbox, [250])
        self.assertEqual(tiles, 1)

    def test_2m_01deg_tiles(self):
        bbox = {"north": 47.1, "south": 47.0, "east": 8.1, "west": 8.0}
        tiles, _ = _estimate_tile_count_and_mb(bbox, [2])
        self.assertEqual(tiles, 1)

    def test_empty(self):
        bbox = {"north": 48.0, "south": 47.0, "east": 9.0, "west": 8.0}
        self.assertEqual(_estimate_tile_count_and_mb(bbox, []), (0, 0))


class TestAbtSizePx(unittest.TestCase):

    def test_30m_1deg(self):
        sz = _abt_size_px(30, 1.0)
        self.assertEqual(sz % 4, 0)
        self.assertAlmostEqual(sz, 3704, delta=4)

    def test_always_multiple_of_4(self):
        for res, ext in _ABT_EXTENT_DEG.items():
            sz = _abt_size_px(res, ext)
            self.assertEqual(sz % 4, 0, f"res={res}")
            self.assertGreater(sz, 0)

    def test_tile_file_size_under_500mb(self):
        for res, ext in _ABT_EXTENT_DEG.items():
            sz = _abt_size_px(res, ext)
            stride = (sz * 2 + 255) & ~255
            mb = (44 + stride * sz) / (1024 * 1024)
            self.assertLess(mb, 500, f"res={res}m: {mb:.0f} MB")


class TestAbtExtentMapping(unittest.TestCase):

    def test_all_resolutions_present(self):
        for r in [2, 5, 10, 30, 90, 250]:
            self.assertIn(r, _ABT_EXTENT_DEG)

    def test_monotonic(self):
        self.assertLessEqual(_ABT_EXTENT_DEG[2], _ABT_EXTENT_DEG[5])
        self.assertLessEqual(_ABT_EXTENT_DEG[5], _ABT_EXTENT_DEG[30])
        self.assertLessEqual(_ABT_EXTENT_DEG[30], _ABT_EXTENT_DEG[90])
        self.assertLessEqual(_ABT_EXTENT_DEG[90], _ABT_EXTENT_DEG[250])


class TestSharedTileEnumeration(unittest.TestCase):
    """The estimate and the worker must walk the same grid (A13/A14).

    The estimate walked the raw extent and counted every tile; the worker
    walked the outward-snapped extent and skipped tiles already on disk. So
    the number shown before the run was wrong in both directions, and the
    tab's tile sizing was a second copy of ``_tile_params`` that had already
    lost the ``_ABT_MAX_SIZE_PX`` u16 row-stride guard.
    """

    BBOX = {"north": 47.83, "south": 47.11, "east": 8.77, "west": 8.13}

    def test_sizing_comes_from_the_shared_tile_params(self):
        for res, ext in _ABT_EXTENT_DEG.items():
            for _r, tile in _enumerate_tiles(
                    {"north": 47.0 + ext / 2, "south": 47.0,
                     "east": 8.0 + ext / 2, "west": 8.0}, [res]):
                self.assertEqual(tile["size_px"],
                                 _tile_params(ext, 0.0, 0.0, res)["size_px"])

    def test_estimate_matches_the_worker_enumeration(self):
        for resolutions in ([30], [10, 30], [2, 250], [2, 5, 10, 30, 90, 250]):
            count, _mb = _estimate_tile_count_and_mb(self.BBOX, resolutions)
            worker = _enumerate_tiles(_snap_bbox(self.BBOX, resolutions),
                                      resolutions)
            self.assertEqual(count, len(worker), resolutions)

    def test_estimate_snaps_outward_exactly_as_the_worker_does(self):
        snapped = _snap_bbox(self.BBOX, [10, 30])
        self.assertLessEqual(snapped["south"], self.BBOX["south"])
        self.assertGreaterEqual(snapped["north"], self.BBOX["north"])
        self.assertLessEqual(snapped["west"], self.BBOX["west"])
        self.assertGreaterEqual(snapped["east"], self.BBOX["east"])

    def test_existing_tiles_are_not_priced_again(self):
        # The run skips them unless "Rebuild existing tiles" is on, so the
        # estimate must not bill for them either.
        with tempfile.TemporaryDirectory() as out_dir:
            total, total_mb = _estimate_tile_count_and_mb(self.BBOX, [30])
            self.assertGreater(total, 1)

            res, tile = _enumerate_tiles(_snap_bbox(self.BBOX, [30]), [30])[0]
            open(os.path.join(out_dir, _abt_tile_name(
                res, tile["ul_lat"], tile["ul_lon"])), "w").close()

            left, left_mb = _estimate_tile_count_and_mb(
                self.BBOX, [30], output_dir=out_dir, overwrite=False)
            self.assertEqual(left, total - 1)
            self.assertLess(left_mb, total_mb)

            # …but a rebuild really does redo them.
            again, _mb = _estimate_tile_count_and_mb(
                self.BBOX, [30], output_dir=out_dir, overwrite=True)
            self.assertEqual(again, total)

    def test_worker_uses_the_shared_enumerator(self):
        # Pins the sharing itself: a second grid walk in the worker is exactly
        # how the two drifted apart before.
        entry = _LayerEntry(layer_type="raster", source_path="/a.tif",
                            extent=dict(self.BBOX), target_resolutions=[30])
        with tempfile.TemporaryDirectory() as out_dir:
            worker = _MapConverterWorker([entry], out_dir, [30], ["/a.tif"])
            with mock.patch.object(_MapConverterWorker, "_resolve_raster",
                                   return_value=["/a.tif"]), \
                 mock.patch.object(_MapConverterWorker, "_resolve_buildings",
                                   return_value=None), \
                 mock.patch.object(mct, "find_binary",
                                   side_effect=RuntimeError("no engine")), \
                 mock.patch.object(mct, "_enumerate_tiles",
                                   wraps=mct._enumerate_tiles) as spy:
                worker.run()
        spy.assert_called_once()
        self.assertEqual(spy.call_args[0][0], _snap_bbox(self.BBOX, [30]))


class TestSublayerUris(unittest.TestCase):
    """A GeoPackage sublayer arrives as "<file>|layername=<x>" (B13).

    ``os.path.isfile`` is False on the joined URI and ``os.path.splitext``
    yields ".gpkg|layername=x", so the ->FGB conversion was skipped twice over
    and the raw URI reached the converter, which cannot open it and only warns.
    """

    def test_plain_path_is_unchanged(self):
        self.assertEqual(_split_sublayer("/data/b.fgb"), ("/data/b.fgb", ""))

    def test_layername_is_split_off(self):
        self.assertEqual(_split_sublayer("/data/x.gpkg|layername=buildings"),
                         ("/data/x.gpkg", "buildings"))

    def test_layername_is_found_past_other_options(self):
        self.assertEqual(
            _split_sublayer("/d/x.gpkg|geometrytype=Polygon|layername=b"),
            ("/d/x.gpkg", "b"))

    def test_unnamed_sublayer_yields_the_file_alone(self):
        # layerid= is an index, not a name: convert the whole container rather
        # than the wrong layer.
        self.assertEqual(_split_sublayer("/d/x.gpkg|layerid=0"),
                         ("/d/x.gpkg", ""))

    def test_two_sublayers_of_one_file_are_two_conversions(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.gpkg")
            open(p, "w").close()
            self.assertNotEqual(_fgb_cache_name(p, "", "buildings"),
                                _fgb_cache_name(p, "", "roads"))
            self.assertNotEqual(_fgb_cache_name(p, ""),
                                _fgb_cache_name(p, "", "buildings"))

    def _resolve(self, uri, converted="/tmp/out.fgb"):
        calls = []

        def fake_convert(src, out_dir, src_crs="", layer_name=""):
            calls.append((src, src_crs, layer_name))
            return converted

        with tempfile.TemporaryDirectory() as out_dir:
            worker = _MapConverterWorker([], out_dir, [30], [])
            with mock.patch.object(mct, "_convert_to_fgb",
                                   side_effect=fake_convert):
                result = worker._resolve_buildings(
                    {"resolved_source": uri, "crs_authid": "EPSG:4326"})
        return result, calls

    def test_gpkg_sublayer_is_converted_to_fgb(self):
        with tempfile.TemporaryDirectory() as d:
            gpkg = os.path.join(d, "x.gpkg")
            open(gpkg, "w").close()
            result, calls = self._resolve(f"{gpkg}|layername=buildings")
        self.assertEqual(calls, [(gpkg, "EPSG:4326", "buildings")])
        self.assertEqual(result, os.path.abspath("/tmp/out.fgb"))

    def test_plain_gpkg_extension_is_matched(self):
        # Second latent bug in the same expression: even for a real file,
        # splitext of the joined URI never matched the extension tuple.
        with tempfile.TemporaryDirectory() as d:
            gpkg = os.path.join(d, "x.gpkg")
            open(gpkg, "w").close()
            _result, calls = self._resolve(gpkg)
        self.assertEqual(calls, [(gpkg, "EPSG:4326", "")])

    def test_unconvertible_sublayer_keeps_its_suffix_and_absolute_path(self):
        # abspath() on the whole URI used to prefix the cwd to it.
        with tempfile.TemporaryDirectory() as d:
            fgb = os.path.join(d, "x.fgb")
            open(fgb, "w").close()
            result, calls = self._resolve(f"{fgb}|layername=b", converted=None)
        self.assertEqual(result, f"{fgb}|layername=b")
        self.assertTrue(os.path.isabs(result))


class TestLayerEntry(unittest.TestCase):

    def test_defaults(self):
        e = _LayerEntry()
        self.assertEqual(e.layer_type, "raster")
        self.assertEqual(e.target_resolutions, [30])
        self.assertIsNone(e.extent)

    def test_buildings(self):
        e = _LayerEntry(layer_type="buildings", source_path="/b.fgb")
        self.assertEqual(e.layer_type, "buildings")


class TestWorkerProgress(unittest.TestCase):

    def test_parse_line(self):
        self.assertEqual(
            _MapConverterWorker._parse_progress("[Rust] Progress: 10/47"),
            (10, 47),
        )

    def test_parse_final(self):
        self.assertEqual(
            _MapConverterWorker._parse_progress("[Rust] Progress: 47/47"),
            (47, 47),
        )

    def test_no_match(self):
        self.assertIsNone(
            _MapConverterWorker._parse_progress("[Rust] Batch processing...")
        )


class TestListTerrainFiles(unittest.TestCase):

    def test_finds_terrain(self):
        d = tempfile.mkdtemp()
        for name in ["a.tif", "b.tiff", "c.dem", "d.hgt", "e.txt", "f.png"]:
            open(os.path.join(d, name), "w").close()
        files = _list_terrain_files(d)
        names = [os.path.basename(f) for f in files]
        self.assertIn("a.tif", names)
        self.assertIn("c.dem", names)
        self.assertNotIn("e.txt", names)

    def test_empty(self):
        d = tempfile.mkdtemp()
        self.assertEqual(_list_terrain_files(d), [])


class TestDetectXyzResolution(unittest.TestCase):

    def test_z15(self):
        src = "type=xyz&url=http://example.com/{z}/{x}/{y}.png&zmax=15"
        res = _detect_xyz_resolution(src)
        self.assertIsNotNone(res)
        # z15 at equator: ~4.8m
        self.assertAlmostEqual(res, 4.8, delta=0.5)

    def test_z10(self):
        src = "type=xyz&url=http://example.com/{z}/{x}/{y}.png&zmax=10"
        res = _detect_xyz_resolution(src)
        self.assertIsNotNone(res)
        # z10: ~152m
        self.assertAlmostEqual(res, 152.9, delta=5)

    def test_default_z15(self):
        src = "type=xyz&url=http://example.com/{z}/{x}/{y}.png"
        res = _detect_xyz_resolution(src)
        self.assertIsNotNone(res)


class TestOverpassToGeojson(unittest.TestCase):

    def test_basic_conversion(self):
        from waveshed.gui.map_converter_tab import MapConverterTab
        data = {
            "elements": [
                {"type": "node", "id": 1, "lat": 47.0, "lon": 8.0},
                {"type": "node", "id": 2, "lat": 47.0, "lon": 8.001},
                {"type": "node", "id": 3, "lat": 47.001, "lon": 8.001},
                {"type": "node", "id": 4, "lat": 47.001, "lon": 8.0},
                {
                    "type": "way", "id": 100,
                    "nodes": [1, 2, 3, 4, 1],
                    "tags": {"building": "yes"},
                },
            ]
        }
        geojson = MapConverterTab._overpass_to_geojson(data)
        self.assertEqual(geojson["type"], "FeatureCollection")
        self.assertEqual(len(geojson["features"]), 1)
        self.assertEqual(
            geojson["features"][0]["geometry"]["type"], "Polygon"
        )

    def test_empty(self):
        from waveshed.gui.map_converter_tab import MapConverterTab
        geojson = MapConverterTab._overpass_to_geojson({"elements": []})
        self.assertEqual(len(geojson["features"]), 0)


if __name__ == "__main__":
    unittest.main()



class TestExportPitchFollowsTargetResolution(unittest.TestCase):
    """The QGIS export that feeds `aether_converter ingest` must be written at
    the resolution the user asked for, not at the layer's own resolution.

    ingest.rs point-samples the exported GeoTIFF onto the .abt grid (one source
    pixel per output cell, no averaging in either direction), so any mismatch
    between the export pitch and the .abt pitch is resampled at an arbitrary
    ratio: coarser gives plateaus of repeated cells, finer gives 1-in-N
    aliasing. Both are lattice-locked terrain error, which the LOS horizon
    sweep renders as a checkerboard. The direct XYZ download never shows it
    because `terrain_adapter.tile_zoom` picks its source zoom FROM the target.

    Regression: the export used `_detect_resolution(layer) or 30.0` and the
    target never reached it — the resolution checkboxes are read in `_on_run`
    but only handed to the worker, which is built after the export has run.
    """

    SPAN_DEG = 0.2

    def _run_export(self, target_res_m, target_resolutions=None):
        from qgis import core as qc
        from waveshed.gui import map_converter_tab as mct

        seen = {}

        class _Rect:
            def __init__(self, w, s, e, n):
                self._box = (w, s, e, n)

            def width(self):
                return self._box[2] - self._box[0]

            def height(self):
                return self._box[3] - self._box[1]

        class _Pipe:
            def set(self, provider):
                return True

            def size(self):
                return 1

            def insert(self, idx, obj):
                return True

        class _Projector:
            def setCrs(self, *a):
                pass

        class _Writer:
            def __init__(self, path):
                seen["path"] = path

            def setOutputFormat(self, fmt):
                pass

            def writeRaster(self, pipe, nc, nr, extent, crs, ctx):
                seen["nc"], seen["nr"] = nc, nr
                return 0

        patches = {
            "QgsRectangle": _Rect,
            "QgsRasterPipe": _Pipe,
            "QgsRasterProjector": _Projector,
            "QgsRasterFileWriter": _Writer,
        }
        originals = {k: getattr(qc, k, None) for k in patches}
        for k, v in patches.items():
            setattr(qc, k, v)

        layer = qc.QgsRasterLayer()
        layer.source = lambda: "type=xyz&url=https://x/{z}/{x}/{y}.png&zmax=15"
        layer.dataProvider = lambda: mock.MagicMock()
        layer.crs = lambda: qc.QgsCoordinateReferenceSystem("EPSG:4326")

        entry = _LayerEntry(
            layer_type="raster",
            source_path="type=xyz&url=https://x/{z}/{x}/{y}.png&zmax=15",
            qgis_layer=layer,
            extent={"west": 8.0, "east": 8.0 + self.SPAN_DEG,
                    "south": 47.0, "north": 47.0 + self.SPAN_DEG},
            target_resolutions=target_resolutions or [30],
        )
        try:
            mct._resolve_source_on_main_thread(entry, target_res_m)
        finally:
            for k, v in originals.items():
                if v is None:
                    delattr(qc, k)
                else:
                    setattr(qc, k, v)
        return seen

    def _pitch_m(self, seen):
        self.assertIn("nc", seen, "writeRaster was never reached")
        return self.SPAN_DEG / seen["nc"] * 111_111.0

    def test_export_pitch_matches_the_requested_2m(self):
        self.assertAlmostEqual(self._pitch_m(self._run_export(2.0)), 2.0,
                               delta=0.01)

    def test_export_pitch_matches_the_requested_30m(self):
        self.assertAlmostEqual(self._pitch_m(self._run_export(30.0)), 30.0,
                               delta=0.05)

    def test_falls_back_to_the_entry_target_when_caller_passes_nothing(self):
        seen = self._run_export(None, target_resolutions=[10, 30])
        self.assertAlmostEqual(self._pitch_m(seen), 10.0, delta=0.02)

    def test_export_is_not_sized_from_the_layer(self):
        # The old bug: a zmax=15 XYZ layer exported at ~4.8 m, or at the 30 m
        # `or 30.0` fallback, whatever the user asked for.
        pitch = self._pitch_m(self._run_export(2.0))
        self.assertNotAlmostEqual(pitch, 4.8, delta=0.5)
        self.assertNotAlmostEqual(pitch, 30.0, delta=1.0)
