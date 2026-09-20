"""Unit tests for map_converter_tab — estimation, job building, helpers.

QGIS stubs provided by conftest.py.
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401

import json
import math
import os
import re
import tempfile
import unittest
from unittest import mock

from waveshed.gui.map_converter_tab import (
    _abt_tile_name,
    _classify_acquisition,
    _estimate_tile_count_and_mb,
    _enumerate_tiles,
    _snap_bbox,
    _split_sublayer,
    _ABT_EXTENT_DEG,
    _LayerEntry,
    _MapConverterWorker,
    _fgb_cache_name,
    _fgb_translate_options,
    phase_progress,
)
import waveshed.core.buildings_source as buildings_source
import waveshed.core.terrain_adapter as ta
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
            with mock.patch.object(
                    _MapConverterWorker, "_resolve_raster",
                    return_value={"kind": "files", "infos": []}), \
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
            # Patched where the conversion now LIVES (core.buildings_source),
            # which is also what the Site Analysis path calls — one copy, one
            # patch point.
            with mock.patch.object(buildings_source, "_convert_to_fgb",
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
    """The tab uses terrain_adapter.list_terrain_files — the ONE scanner
    (recursive + sorted). Behaviour change vs the old flat tab copy:
    subfolders are now included, same as Site Analysis."""

    def test_finds_terrain_recursively(self):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "sub"))
        for name in ["a.tif", "b.tiff", "c.dem", "d.hgt", "e.txt", "f.png",
                     os.path.join("sub", "nested.tif")]:
            open(os.path.join(d, name), "w").close()
        files = ta.list_terrain_files(d)
        names = [os.path.basename(f) for f in files]
        self.assertIn("a.tif", names)
        self.assertIn("c.dem", names)
        self.assertIn("nested.tif", names)   # subfolders included now
        self.assertNotIn("e.txt", names)
        self.assertEqual(files, sorted(files))

    def test_empty(self):
        d = tempfile.mkdtemp()
        self.assertEqual(ta.list_terrain_files(d), [])


class TestXyzNativeResolution(unittest.TestCase):
    """terrain_adapter.xyz_native_resolution_m is the ONE XYZ-resolution
    helper (resolve_zmax-based: validated, service-capped)."""

    def test_z15(self):
        src = "type=xyz&url=http://example.com/{z}/{x}/{y}.png&zmax=15"
        res = ta.xyz_native_resolution_m(src)
        self.assertIsNotNone(res)
        # z15 at equator: ~4.8m
        self.assertAlmostEqual(res, 4.8, delta=0.5)

    def test_z10(self):
        src = "type=xyz&url=http://example.com/{z}/{x}/{y}.png&zmax=10"
        res = ta.xyz_native_resolution_m(src)
        self.assertIsNotNone(res)
        # z10: ~152m
        self.assertAlmostEqual(res, 152.9, delta=5)

    def test_default_z15(self):
        src = "type=xyz&url=http://example.com/{z}/{x}/{y}.png"
        res = ta.xyz_native_resolution_m(src)
        self.assertIsNotNone(res)

    def test_known_service_cap_beats_a_lying_zmax(self):
        # The old tab copy trusted zmax=18 (QGIS's default) and reported a
        # fictitious 0.6 m for a z15 service.
        src = ("type=xyz&url=https%3A//s3.amazonaws.com/elevation-tiles-prod/"
               "terrarium/%7Bz%7D/%7Bx%7D/%7By%7D.png&zmax=18")
        self.assertAlmostEqual(ta.xyz_native_resolution_m(src), 4.8,
                               delta=0.5)

    def test_non_xyz_is_none(self):
        self.assertIsNone(ta.xyz_native_resolution_m("/data/dem.tif"))


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



class TestUnifiedTileNaming(unittest.TestCase):
    """One naming scheme for both terrain paths (spec: unified naming)."""

    def test_wrapper_matches_tile_params_filename(self):
        for res in sorted(_ABT_EXTENT_DEG):
            ext = _ABT_EXTENT_DEG[res]
            tile = _tile_params(ext, 47.0, 8.0, res)
            self.assertEqual(
                _abt_tile_name(res, tile["ul_lat"], tile["ul_lon"]),
                tile["filename"])

    def test_the_old_map_converter_spelling_is_gone(self):
        name = _abt_tile_name(30, 47.5, 8.0)
        self.assertEqual(name, "tile_N47.50E8.00_30m.abt")
        self.assertNotIn("r16sint", name)
        self.assertFalse(name.startswith("Tile_"))

    def test_worker_skips_existing_tiles_by_the_new_name_only(self):
        bbox = {"north": 47.4, "south": 47.1, "east": 8.4, "west": 8.1}
        expected = _enumerate_tiles(_snap_bbox(bbox, [30]), [30])
        info = {"path": "/a.tif", "crs": None, "crs_authid": "EPSG:4326",
                "native_bounds": {"west": 0, "east": 20, "south": 40,
                                  "north": 50},
                "wgs84_bounds": {"west": 0, "east": 20, "south": 40,
                                 "north": 50},
                "halo_deg": 0.001}
        with tempfile.TemporaryDirectory() as out_dir:
            # An old-name tile must NOT suppress the new-name output; a
            # new-name tile must.
            res, tile = expected[0]
            open(os.path.join(out_dir,
                              f"Tile_N{tile['ul_lat']:.2f}E"
                              f"{tile['ul_lon']:.2f}_{res}m_r16sint.abt"),
                 "w").close()
            jobs, _temps = mct._build_tile_jobs(
                expected, out_dir, False,
                [{"kind": "files", "infos": [info]}], None, out_dir)
            self.assertEqual(len(jobs), len(expected))

            open(os.path.join(out_dir, tile["filename"]), "w").close()
            jobs, _temps = mct._build_tile_jobs(
                expected, out_dir, False,
                [{"kind": "files", "infos": [info]}], None, out_dir)
            self.assertEqual(len(jobs), len(expected) - 1)

    def test_deleted_helpers_are_gone(self):
        for name in ("_lv95_to_wgs84_approx", "_filter_files_by_extent"):
            self.assertFalse(hasattr(mct, name), name)
        for name in ("_ensure_wgs84", "_needs_reproject", "_NATIVE_CRS"):
            self.assertFalse(hasattr(_MapConverterWorker, name), name)


class TestBuildTileJobsSources(unittest.TestCase):
    """Per-tile jobs carry sources[] in UI stack order; no void_fill_m."""

    BBOX = {"north": 47.4, "south": 47.1, "east": 8.4, "west": 8.1}

    @staticmethod
    def _info(path, bounds, crs="EPSG:4326"):
        return {"path": path, "crs": None, "crs_authid": crs,
                "native_bounds": dict(bounds), "wgs84_bounds": dict(bounds),
                "halo_deg": 0.001}

    def _jobs(self, infos, buildings=None, overwrite=True):
        expected = _enumerate_tiles(_snap_bbox(self.BBOX, [30]), [30])
        with tempfile.TemporaryDirectory() as out_dir:
            jobs, temps = mct._build_tile_jobs(
                expected, out_dir, overwrite,
                [{"kind": "files", "infos": infos}], buildings, out_dir)
        return expected, jobs, temps

    def test_stack_order_is_priority_order(self):
        covering = {"west": 0.0, "east": 20.0, "south": 40.0, "north": 50.0}
        infos = [self._info("/top/overlay.tif", covering, crs="EPSG:32632"),
                 self._info("/bottom/base.tif", covering)]
        _expected, jobs, _temps = self._jobs(infos)
        self.assertTrue(jobs)
        for job in jobs:
            self.assertEqual(
                [os.path.basename(s["path"]) for s in job["sources"]],
                ["overlay.tif", "base.tif"])
            self.assertEqual(job["sources"][0]["crs"], "EPSG:32632")
            self.assertEqual(job["sources"][1]["crs"], "EPSG:4326")

    def test_no_void_fill_and_no_legacy_fields(self):
        # Map Converter tiles keep VOID for no-data — current behaviour.
        infos = [self._info("/a.tif", {"west": 0, "east": 20,
                                       "south": 40, "north": 50})]
        _expected, jobs, _temps = self._jobs(infos)
        for job in jobs:
            self.assertNotIn("void_fill_m", job)
            self.assertNotIn("base_tif", job)
            self.assertNotIn("swiss_tifs", job)

    def test_per_tile_bounds_filter(self):
        covering = {"west": 0.0, "east": 20.0, "south": 40.0, "north": 50.0}
        elsewhere = {"west": 100.0, "east": 101.0,
                     "south": 10.0, "north": 11.0}
        infos = [self._info("/near.tif", covering),
                 self._info("/far.tif", elsewhere)]
        _expected, jobs, _temps = self._jobs(infos)
        for job in jobs:
            names = [os.path.basename(s["path"]) for s in job["sources"]]
            self.assertEqual(names, ["near.tif"])

    def test_buildings_file_rides_along(self):
        infos = [self._info("/a.tif", {"west": 0, "east": 20,
                                       "south": 40, "north": 50})]
        _expected, jobs, _temps = self._jobs(infos, buildings="/b/x.fgb")
        for job in jobs:
            self.assertEqual(job["buildings_file"], "/b/x.fgb")

    def test_output_names_and_geometry_come_from_tile_params(self):
        infos = [self._info("/a.tif", {"west": 0, "east": 20,
                                       "south": 40, "north": 50})]
        expected, jobs, _temps = self._jobs(infos)
        self.assertEqual(len(jobs), len(expected))
        for (res, tile), job in zip(expected, jobs):
            self.assertEqual(os.path.basename(job["output_path"]),
                             tile["filename"])
            self.assertEqual(job["ul_lat"], tile["ul_lat"])
            self.assertEqual(job["ul_lon"], tile["ul_lon"])
            self.assertEqual(job["size_px"], tile["size_px"])
            self.assertEqual(job["resolution_m"], float(tile["exact_res_m"]))
            self.assertEqual(job["format"], "r16sint")


class TestPlanCrossCheck(unittest.TestCase):
    """The worker must abort unless the engine enumerates the same grid."""

    BBOX = {"north": 48.0, "south": 47.0, "east": 9.0, "west": 8.0}

    def _expected(self, resolutions=(30,)):
        return _enumerate_tiles(_snap_bbox(self.BBOX, list(resolutions)),
                                list(resolutions))

    @staticmethod
    def _proc(rc=0, stdout="", stderr=""):
        p = mock.Mock()
        p.returncode = rc
        p.stdout = stdout
        p.stderr = stderr
        return p

    def _plan_doc(self, expected):
        return {
            "schema": "aether-plan/1",
            "tile_count": len(expected),
            "total_bytes": 0,
            "tiles": [{"resolution_m": res, "filename": t["filename"],
                       "ul_lat": t["ul_lat"], "ul_lon": t["ul_lon"],
                       "size_px": t["size_px"],
                       "exact_res_m": t["exact_res_m"], "est_bytes": 1}
                      for res, t in expected],
        }

    def test_matching_plan_passes(self):
        expected = self._expected()
        out = json.dumps(self._plan_doc(expected))
        with mock.patch.object(mct.subprocess, "run",
                               return_value=self._proc(stdout=out)) as run:
            mct._plan_cross_check("/bin/conv", self.BBOX, [30], expected)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "/bin/conv")
        self.assertEqual(cmd[1], "plan")
        self.assertIn("--resolutions", cmd)
        self.assertEqual(cmd[cmd.index("--resolutions") + 1], "30")

    def test_count_mismatch_aborts_with_both_counts(self):
        expected = self._expected()
        doc = self._plan_doc(expected)
        doc["tiles"] = doc["tiles"][:-1]
        doc["tile_count"] = len(doc["tiles"])
        with mock.patch.object(mct.subprocess, "run",
                               return_value=self._proc(
                                   stdout=json.dumps(doc))):
            with self.assertRaises(RuntimeError) as caught:
                mct._plan_cross_check("/bin/conv", self.BBOX,
                                      [30], expected)
        msg = str(caught.exception)
        self.assertIn(str(len(expected)), msg)
        self.assertIn(str(len(expected) - 1), msg)
        self.assertIn("aborted", msg)

    def test_filename_mismatch_aborts_even_with_matching_count(self):
        expected = self._expected()
        doc = self._plan_doc(expected)
        doc["tiles"][0]["filename"] = "tile_N99.00E99.00_30m.abt"
        with mock.patch.object(mct.subprocess, "run",
                               return_value=self._proc(
                                   stdout=json.dumps(doc))):
            with self.assertRaises(RuntimeError) as caught:
                mct._plan_cross_check("/bin/conv", self.BBOX,
                                      [30], expected)
        self.assertIn("tile_N99.00E99.00_30m.abt", str(caught.exception))

    def test_missing_subcommand_names_the_stale_engine(self):
        stderr = "error: unrecognized subcommand 'plan'\n\nUsage: ..."
        with mock.patch.object(mct.subprocess, "run",
                               return_value=self._proc(rc=2, stderr=stderr)):
            with self.assertRaises(RuntimeError) as caught:
                mct._plan_cross_check("/bin/conv", self.BBOX,
                                      [30], self._expected())
        self.assertIn(
            "engine binaries predate this plugin version — update them",
            str(caught.exception))

    def test_other_failures_surface_the_engine_error(self):
        with mock.patch.object(mct.subprocess, "run",
                               return_value=self._proc(
                                   rc=1, stderr="Error: bbox too large")):
            with self.assertRaises(RuntimeError) as caught:
                mct._plan_cross_check("/bin/conv", self.BBOX,
                                      [30], self._expected())
        msg = str(caught.exception)
        self.assertIn("bbox too large", msg)
        self.assertNotIn("predate", msg)

    def test_unparseable_output_aborts(self):
        with mock.patch.object(mct.subprocess, "run",
                               return_value=self._proc(stdout="not json")):
            with self.assertRaises(RuntimeError):
                mct._plan_cross_check("/bin/conv", self.BBOX,
                                      [30], self._expected())

    def test_wrong_schema_aborts(self):
        with mock.patch.object(mct.subprocess, "run",
                               return_value=self._proc(
                                   stdout='{"schema": "aether-plan/2"}')):
            with self.assertRaises(RuntimeError) as caught:
                mct._plan_cross_check("/bin/conv", self.BBOX,
                                      [30], self._expected())
        self.assertIn("aether-plan/1", str(caught.exception))


def _find_real_converter():
    """The aether_converter binary AETHER_BIN_DIR/PATH provides, or None."""
    import shutil
    bin_dir = os.environ.get("AETHER_BIN_DIR", "")
    if bin_dir:
        for name in ("aether_converter", "aether_converter.exe"):
            p = os.path.join(bin_dir, name)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return shutil.which("aether_converter")


class TestPlanIntegrationRealBinary(unittest.TestCase):
    """Optional: the ONLY test allowed to skip — real binary, real plan."""

    def test_real_plan_matches_the_python_enumeration(self):
        exe = _find_real_converter()
        if not exe:
            self.skipTest("aether_converter not provided via "
                          "AETHER_BIN_DIR or PATH")
        bbox = _snap_bbox({"north": 47.6, "south": 47.1,
                           "east": 8.6, "west": 8.1}, [30, 90])
        expected = _enumerate_tiles(bbox, [30, 90])
        # Must not raise: same tile count, same filenames.
        mct._plan_cross_check(exe, bbox, [30, 90], expected)


class TestPlanCrossCheckUsesTheRawBbox(unittest.TestCase):
    """Snapping is NOT idempotent on the 0.1-degree float grid.

    floor/ceil of an already-snapped edge moves it one grid step for 516 of
    3601 grid values (e.g. ceil(47.400000000000006/0.1) = 475), so handing
    the engine a pre-snapped bbox makes its own snap widen the box and the
    cross-check falsely abort. Repro from review: S=24.7 N=25.0 W=-70.3
    E=-69.8 at 2 m — plugin 15 tiles, double-snapped plan 18.
    """

    RAW = {"south": 24.7, "north": 25.0, "west": -70.3, "east": -69.8}

    def test_snapping_is_not_idempotent_on_the_float_grid(self):
        # The premise: an already-snapped edge can move a grid step when
        # snapped again. ceil(47.32/0.1)*0.1 = 47.400000000000006, and
        # ceil of THAT /0.1 is 475 — one step further north.
        once = math.ceil(47.32 / 0.1) * 0.1
        twice = math.ceil(once / 0.1) * 0.1
        self.assertEqual(once, 47.400000000000006)
        self.assertNotEqual(once, twice)

    def test_request_carries_the_raw_unsnapped_bbox(self):
        expected = _enumerate_tiles(_snap_bbox(self.RAW, [2]), [2])
        doc = {"schema": "aether-plan/1", "tile_count": len(expected),
               "total_bytes": 0,
               "tiles": [{"filename": t["filename"]} for _r, t in expected]}
        proc = mock.Mock(returncode=0, stdout=json.dumps(doc), stderr="")
        with mock.patch.object(mct.subprocess, "run",
                               return_value=proc) as run:
            mct._plan_cross_check("/bin/conv", self.RAW, [2], expected)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--south") + 1], repr(24.7))
        self.assertEqual(cmd[cmd.index("--north") + 1], repr(25.0))
        self.assertEqual(cmd[cmd.index("--west") + 1], repr(-70.3))
        self.assertEqual(cmd[cmd.index("--east") + 1], repr(-69.8))

    def test_worker_hands_the_raw_combined_bbox_to_the_check(self):
        entry = _LayerEntry(layer_type="raster", source_path="/a.tif",
                            extent=dict(self.RAW), target_resolutions=[2])
        info = {"path": "/a.tif", "crs": None, "crs_authid": "EPSG:4326",
                "native_bounds": {"west": -71, "east": -69,
                                  "south": 24, "north": 26},
                "wgs84_bounds": {"west": -71, "east": -69,
                                 "south": 24, "north": 26},
                "halo_deg": 0.001}
        seen = {}

        def spy_check(exe, bbox, resolutions, expected):
            seen["bbox"] = dict(bbox)
            seen["expected"] = expected
            raise RuntimeError("stop before any job is built")

        with tempfile.TemporaryDirectory() as out_dir:
            worker = _MapConverterWorker([entry], out_dir, [2], ["/a.tif"])
            with mock.patch.object(
                    _MapConverterWorker, "_resolve_raster",
                    return_value={"kind": "files", "infos": [info]}), \
                 mock.patch.object(_MapConverterWorker, "_resolve_buildings",
                                   return_value=None), \
                 mock.patch.object(mct, "find_binary",
                                   return_value="/bin/conv"), \
                 mock.patch.object(mct, "_plan_cross_check",
                                   side_effect=spy_check):
                worker.run()

        # RAW combined bbox — never the snapped one.
        self.assertEqual(seen["bbox"], self.RAW)
        self.assertNotEqual(seen["bbox"], _snap_bbox(self.RAW, [2]))
        # While the Python enumeration itself still runs over ITS snap.
        self.assertEqual(
            [t["filename"] for _r, t in seen["expected"]],
            [t["filename"] for _r, t in
             _enumerate_tiles(_snap_bbox(self.RAW, [2]), [2])])


class TestPhaseProgress(unittest.TestCase):
    """One bar, phase-weighted: resolve 0-20, download 20-70, convert 70-100."""

    def test_phase_spans(self):
        self.assertEqual(phase_progress("resolve", 0.0), 0)
        self.assertEqual(phase_progress("resolve", 1.0), 20)
        self.assertEqual(phase_progress("download", 0.0), 20)
        self.assertEqual(phase_progress("download", 0.5), 45)
        self.assertEqual(phase_progress("download", 1.0), 70)
        self.assertEqual(phase_progress("convert", 0.0), 70)
        self.assertEqual(phase_progress("convert", 1.0), 100)

    def test_fraction_is_clamped(self):
        self.assertEqual(phase_progress("download", -0.5), 20)
        self.assertEqual(phase_progress("download", 1.5), 70)


class TestAcquisitionRouter(unittest.TestCase):
    """One router: xyz → toolkit download, file → direct, rest → QGIS render."""

    def test_xyz_uri_routes_to_the_downloader(self):
        self.assertEqual(_classify_acquisition(
            "type=xyz&url=https%3A//x/%7Bz%7D.png&zmax=15"), "xyz")

    def test_local_file_and_folder_route_direct(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "dem.tif")
            open(f, "w").close()
            self.assertEqual(_classify_acquisition(f), "file")
            self.assertEqual(_classify_acquisition(d), "file")
        self.assertEqual(
            _classify_acquisition("/vsicurl/https://host/dem.tif"), "file")

    def test_everything_else_is_a_rendered_server(self):
        for src in (
            "contextualWMSLegend=0&crs=EPSG:4326&url=https://wms.example/x",
            "crs=EPSG:3857&format=image/png&layers=dem&styles&"
            "tileMatrixSet=GoogleMapsCompatible&url=https://wmts.example",
            "https://services.arcgis.com/x/ImageServer",
        ):
            self.assertEqual(_classify_acquisition(src), "rendered", src)

    def test_entry_kind_covers_buildings_and_folders(self):
        with tempfile.TemporaryDirectory() as d:
            folder = _LayerEntry(layer_type="raster", source_path=d)
            self.assertEqual(mct._entry_kind(folder), "file")
        b = _LayerEntry(layer_type="buildings", source_path="/b.fgb")
        self.assertEqual(mct._entry_kind(b), "buildings")


class TestRenderedPerTile(unittest.TestCase):
    """WMS/WMTS render per TILE through the one renderer (_export_via_qgis)."""

    BBOX = {"north": 47.4, "south": 47.1, "east": 8.4, "west": 8.1}

    def _entry(self, extent=None, native=None):
        layer = mock.Mock()
        layer.name.return_value = "wms-dem"
        layer.source.return_value = "url=https://wms.example/x"
        return _LayerEntry(layer_type="raster",
                           source_path="url=https://wms.example/x",
                           qgis_layer=layer,
                           native_res_m=native,
                           extent=extent or dict(self.BBOX),
                           target_resolutions=[30])

    def _jobs(self, resolutions=(30,)):
        return _enumerate_tiles(_snap_bbox(self.BBOX, list(resolutions)),
                                list(resolutions))

    def test_renders_one_tif_per_overlapping_tile(self):
        calls = []

        def fake_export(layer, dest, bbox, res_m):
            calls.append((dest, dict(bbox), res_m))
            open(dest, "w").close()

        jobs = self._jobs()
        with mock.patch.object(ta, "_export_via_qgis",
                               side_effect=fake_export):
            ticks = []
            tiles = mct._render_tiles_via_qgis(self._entry(), jobs,
                                               on_tile=ticks.append)
        self.assertEqual(len(calls), len(jobs))
        self.assertEqual(sorted(tiles), sorted(t["filename"] for _r, t in jobs))
        self.assertEqual(sorted(ticks), sorted(tiles))
        for tf in tiles.values():
            os.remove(tf)

    def test_tiles_outside_the_entry_extent_are_not_rendered(self):
        # Entry extent covers a sliver — only the overlapping tile renders.
        # 2 m tiles (0.1 deg grid) so the run spans several tiles.
        jobs = self._jobs(resolutions=(2,))
        sliver = {"north": 47.15, "south": 47.11, "east": 8.15, "west": 8.11}
        calls = []
        with mock.patch.object(ta, "_export_via_qgis",
                               side_effect=lambda l, d, b, r: calls.append(d)):
            tiles = mct._render_tiles_via_qgis(self._entry(extent=sliver),
                                               jobs)
        self.assertLess(len(tiles), len(jobs))
        self.assertEqual(len(calls), len(tiles))

    def test_renders_at_the_finer_of_native_and_finest_output(self):
        jobs = self._jobs()
        seen = []
        with mock.patch.object(ta, "_export_via_qgis",
                               side_effect=lambda l, d, b, r: seen.append(r)):
            mct._render_tiles_via_qgis(self._entry(native=10.0), jobs)
        self.assertTrue(all(r == 10.0 for r in seen), seen)   # native finer
        seen.clear()
        with mock.patch.object(ta, "_export_via_qgis",
                               side_effect=lambda l, d, b, r: seen.append(r)):
            mct._render_tiles_via_qgis(self._entry(native=90.0), jobs)
        self.assertTrue(all(r == 30.0 for r in seen), seen)   # output finer
        seen.clear()
        with mock.patch.object(ta, "_export_via_qgis",
                               side_effect=lambda l, d, b, r: seen.append(r)):
            mct._render_tiles_via_qgis(self._entry(native=None), jobs)
        self.assertTrue(all(r == 30.0 for r in seen), seen)   # no native

    def test_render_failure_is_a_hard_error_naming_the_layer(self):
        # Regression: the old code swallowed every exception and silently
        # passed the raw source string onward.
        with mock.patch.object(ta, "_export_via_qgis",
                               side_effect=ValueError("server said 403")):
            with self.assertRaises(RuntimeError) as caught:
                mct._render_tiles_via_qgis(self._entry(), self._jobs())
        msg = str(caught.exception)
        self.assertIn("wms-dem", msg)
        self.assertIn("server said 403", msg)

    def test_unloaded_rendered_layer_is_a_hard_error(self):
        entry = _LayerEntry(layer_type="raster",
                            source_path="url=https://wms.example/x",
                            qgis_layer=None, extent=dict(self.BBOX))
        with self.assertRaises(RuntimeError) as caught:
            mct._render_tiles_via_qgis(entry, self._jobs())
        self.assertIn("wms.example", str(caught.exception))


class TestDownloadXyzEntries(unittest.TestCase):
    """MC xyz entries fill their pool via the shared downloader, per res."""

    BBOX = {"north": 47.4, "south": 47.1, "east": 8.4, "west": 8.1}

    def test_downloads_per_resolution_and_fills_the_pool(self):
        expected = _enumerate_tiles(_snap_bbox(self.BBOX, [30, 90]), [30, 90])
        entry = {"kind": "xyz", "uri": "type=xyz&url=u", "extent": self.BBOX}
        calls = []

        def fake_ensure(uri, specs, res, binary_manager=None,
                        progress_cb=None, **_kw):
            calls.append((uri, [t["filename"] for t in specs], res))
            if progress_cb:
                progress_cb(1.0, "done")
            return {t["filename"]: f"/pool/{t['filename']}" for t in specs}

        fracs = []
        with mock.patch.object(ta, "ensure_pool_tiles",
                               side_effect=fake_ensure):
            mct._download_xyz_entries(
                [entry], expected,
                progress_cb=lambda f, label: fracs.append(f))

        self.assertEqual([c[2] for c in calls], [30, 90])
        self.assertEqual(calls[0][0], "type=xyz&url=u")
        # Pool filled per resolution with the tiles of THAT resolution.
        self.assertEqual(sorted(entry["pool"]), [30, 90])
        for res, _t in expected:
            names = [t["filename"] for r, t in expected if r == res]
            self.assertEqual(sorted(entry["pool"][res]), sorted(names))
        # Aggregate fraction reaches 1.0 and never decreases.
        self.assertEqual(fracs[-1], 1.0)
        self.assertEqual(fracs, sorted(fracs))

    def test_entry_extent_limits_the_download(self):
        # 2 m tiles (0.1 deg grid) so the run spans several tiles.
        expected = _enumerate_tiles(_snap_bbox(self.BBOX, [2]), [2])
        sliver = {"north": 47.15, "south": 47.11, "east": 8.15, "west": 8.11}
        entry = {"kind": "xyz", "uri": "u", "extent": sliver}
        calls = []

        def fake_ensure(uri, specs, res, binary_manager=None,
                        progress_cb=None, **_kw):
            calls.append(len(specs))
            return {t["filename"]: f"/pool/{t['filename']}" for t in specs}

        with mock.patch.object(ta, "ensure_pool_tiles",
                               side_effect=fake_ensure):
            mct._download_xyz_entries([entry], expected)
        self.assertTrue(calls)
        self.assertLess(calls[0], len(expected))

    def test_download_failure_propagates(self):
        expected = _enumerate_tiles(_snap_bbox(self.BBOX, [30]), [30])
        entry = {"kind": "xyz", "uri": "u", "extent": self.BBOX}
        with mock.patch.object(ta, "ensure_pool_tiles",
                               side_effect=RuntimeError("service down")):
            with self.assertRaises(RuntimeError):
                mct._download_xyz_entries([entry], expected)


class TestAbtAndRenderedSourcesInTileJobs(unittest.TestCase):
    """Pool .abt and rendered tifs land in sources[] in stack order."""

    BBOX = {"north": 47.4, "south": 47.1, "east": 8.4, "west": 8.1}

    def test_tile_jobs_carry_pool_abt_without_crs(self):
        expected = _enumerate_tiles(_snap_bbox(self.BBOX, [30]), [30])
        pool = {t["filename"]: f"/pool/{t['filename']}" for _r, t in expected}
        fn0 = expected[0][1]["filename"]
        covering = {"west": 0.0, "east": 20.0, "south": 40.0, "north": 50.0}
        entries = [
            {"kind": "xyz", "pool": {30: pool}},
            {"kind": "rendered", "tiles": {fn0: "/tmp/render0.tif"}},
            {"kind": "files", "infos": [
                {"path": "/base.tif", "crs": None, "crs_authid": "EPSG:4326",
                 "native_bounds": covering, "wgs84_bounds": covering,
                 "halo_deg": 0.001}]},
        ]
        with tempfile.TemporaryDirectory() as out_dir:
            jobs, _temps = mct._build_tile_jobs(
                expected, out_dir, True, entries, None, out_dir)

        self.assertEqual(len(jobs), len(expected))
        for (res, tile), job in zip(expected, jobs):
            fn = tile["filename"]
            srcs = job["sources"]
            # Stack order: xyz pool first, rendered (first tile only), files.
            self.assertEqual(srcs[0]["path"], os.path.abspath(pool[fn]))
            # .abt sources are self-describing — crs/nodata would be a
            # converter hard error by contract (A1).
            self.assertNotIn("crs", srcs[0])
            self.assertNotIn("nodata", srcs[0])
            if fn == fn0:
                self.assertEqual(srcs[1]["path"],
                                 os.path.abspath("/tmp/render0.tif"))
                self.assertEqual(srcs[1]["crs"], "EPSG:4326")
                self.assertEqual(os.path.basename(srcs[2]["path"]),
                                 "base.tif")
            else:
                self.assertEqual(os.path.basename(srcs[1]["path"]),
                                 "base.tif")

    def test_missing_pool_tile_contributes_nothing(self):
        expected = _enumerate_tiles(_snap_bbox(self.BBOX, [30]), [30])
        entries = [{"kind": "xyz", "pool": {30: {}}}]
        with tempfile.TemporaryDirectory() as out_dir:
            jobs, _temps = mct._build_tile_jobs(
                expected, out_dir, True, entries, None, out_dir)
        for job in jobs:
            self.assertEqual(job["sources"], [])


def _worker_signals(worker):
    """Attach recorders to a worker's finished/status signals."""
    rec = {"ok": [], "err": [], "status": []}
    worker.finished_ok.connect(rec["ok"].append)
    worker.finished_err.connect(rec["err"].append)
    worker.status.connect(rec["status"].append)
    return rec


class TestWorkerDownloadCancel(unittest.TestCase):
    """Cancel during the download phase ends cleanly (review fix 1)."""

    RAW = {"south": 47.1, "north": 47.4, "west": 8.1, "east": 8.4}

    def _worker(self, out_dir, overwrite=True):
        entry = _LayerEntry(layer_type="raster",
                            source_path="type=xyz&url=u&zmax=15",
                            extent=dict(self.RAW), target_resolutions=[2])
        resolved = {"kind": "xyz", "uri": "type=xyz&url=u&zmax=15",
                    "extent": dict(self.RAW)}
        return _MapConverterWorker([entry], out_dir, [2], [resolved],
                                   overwrite=overwrite)

    def test_cancel_mid_download_is_clean_not_an_error(self):
        # cancel() kills the download subprocess; ensure_pool_tiles then
        # reports a failed download — which must NOT become an error dialog.
        with tempfile.TemporaryDirectory() as out_dir:
            worker = self._worker(out_dir)
            rec = _worker_signals(worker)
            sentinel = mock.Mock()

            def fake_ensure(uri, specs, res, binary_manager=None,
                            progress_cb=None, should_cancel=None,
                            on_start=None):
                # The worker must expose the running process for cancel()...
                on_start(sentinel)
                self.assertIs(worker._proc, sentinel)
                self.assertFalse(should_cancel())
                # ...and cancel() must both flip the flag and kill it.
                worker.cancel()
                self.assertTrue(should_cancel())
                sentinel.terminate.assert_called()
                raise RuntimeError("Terrain download failed: killed")

            with mock.patch.object(mct, "find_binary",
                                   return_value="/bin/conv"), \
                 mock.patch.object(mct, "_plan_cross_check"), \
                 mock.patch.object(ta, "ensure_pool_tiles",
                                   side_effect=fake_ensure):
                worker.run()

        self.assertEqual(rec["err"], [], "cancel surfaced as an error dialog")
        self.assertEqual(rec["ok"], [])
        self.assertIn("Cancelled.", rec["status"])
        self.assertIsNone(worker._proc)

    def test_converter_cancelled_from_the_download_is_clean(self):
        with tempfile.TemporaryDirectory() as out_dir:
            worker = self._worker(out_dir)
            rec = _worker_signals(worker)
            with mock.patch.object(mct, "find_binary",
                                   return_value="/bin/conv"), \
                 mock.patch.object(mct, "_plan_cross_check"), \
                 mock.patch.object(ta, "ensure_pool_tiles",
                                   side_effect=ta.ConverterCancelled):
                worker.run()
        self.assertEqual(rec["err"], [])
        self.assertIn("Cancelled.", rec["status"])

    def test_download_gets_only_the_pending_tiles(self):
        # Review fix 3: with overwrite off, tiles already on disk are
        # skipped by render and job building — the downloader must see the
        # same filtered list, not the full enumeration.
        with tempfile.TemporaryDirectory() as out_dir:
            expected = _enumerate_tiles(_snap_bbox(self.RAW, [2]), [2])
            existing = expected[0][1]["filename"]
            open(os.path.join(out_dir, existing), "w").close()

            worker = self._worker(out_dir, overwrite=False)
            rec = _worker_signals(worker)
            seen = {}

            def spy_download(entries, exp, progress_cb=None,
                             should_cancel=None, on_start=None):
                seen["expected"] = list(exp)
                raise ta.ConverterCancelled()   # stop the run cleanly here

            with mock.patch.object(mct, "find_binary",
                                   return_value="/bin/conv"), \
                 mock.patch.object(mct, "_plan_cross_check"), \
                 mock.patch.object(mct, "_download_xyz_entries",
                                   side_effect=spy_download):
                worker.run()

            names = [t["filename"] for _r, t in seen["expected"]]
            self.assertNotIn(existing, names)
            self.assertEqual(len(names), len(expected) - 1)
        self.assertEqual(rec["err"], [])


class TestImageryXyzIsRefused(unittest.TestCase):
    """An RGB basemap must never reach the elevation downloader (fix 5)."""

    def _entry(self):
        layer = mock.Mock()
        layer.name.return_value = "osm-basemap"
        layer.source.return_value = "type=xyz&url=https%3A//tile.example/x"
        return _LayerEntry(layer_type="raster",
                           source_path=layer.source(),
                           qgis_layer=layer,
                           extent={"south": 47.0, "north": 47.1,
                                   "west": 8.0, "east": 8.1})

    def test_imagery_verdict_is_a_hard_error_naming_the_layer(self):
        with mock.patch.object(mct, "classify_raster_layer",
                               return_value="imagery"):
            with self.assertRaises(RuntimeError) as caught:
                mct._resolve_source_on_main_thread(self._entry(), [])
        msg = str(caught.exception)
        self.assertIn("osm-basemap", msg)
        self.assertIn("imagery, not elevation", msg)

    def test_elevation_verdict_routes_to_the_downloader(self):
        with mock.patch.object(mct, "classify_raster_layer",
                               return_value="dem"):
            resolved = mct._resolve_source_on_main_thread(self._entry(), [])
        self.assertEqual(resolved["kind"], "xyz")


class TestImageryRenderedIsRefused(unittest.TestCase):
    """A WMS/WMTS/ArcGIS picture must never be rendered into terrain.

    Torture §4 (4.1, 4.2, 4.4b, 4.5, 4.6): the imagery guard used to live
    only on the xyz branch, so a rendered service fell through and its
    basemap bytes were ingested as metres.
    """

    def _entry(self):
        layer = mock.Mock()
        layer.name.return_value = "osm-wms"
        layer.source.return_value = (
            "contextualWMSLegend=0&crs=EPSG:3857&format=image/png"
            "&layers=OSM-WMS&url=https://ows.example/service")
        return _LayerEntry(layer_type="raster",
                           source_path=layer.source(),
                           qgis_layer=layer,
                           extent={"south": 47.0, "north": 47.1,
                                   "west": 8.0, "east": 8.1})

    def test_imagery_verdict_is_a_hard_error_before_any_render(self):
        with mock.patch.object(mct, "classify_raster_layer",
                               return_value="imagery"), \
             mock.patch.object(mct, "_render_tiles_via_qgis") as render:
            with self.assertRaises(RuntimeError) as caught:
                mct._resolve_source_on_main_thread(self._entry(), [])
        msg = str(caught.exception)
        self.assertIn("osm-wms", msg)
        self.assertIn("imagery, not elevation", msg)
        render.assert_not_called()

    def test_elevation_verdict_still_renders(self):
        # Row 4.3: a WCS coverage classifies as elevation and must keep
        # flowing through the rendered branch untouched.
        with mock.patch.object(mct, "classify_raster_layer",
                               return_value="dem"), \
             mock.patch.object(mct, "_render_tiles_via_qgis",
                               return_value={"t.abt": "/tmp/t.tif"}):
            resolved = mct._resolve_source_on_main_thread(self._entry(), [])
        self.assertEqual(resolved["kind"], "rendered")


class TestBuildingsFailureIsHard(unittest.TestCase):
    """A buildings entry that cannot be prepared aborts the run (fix 6)."""

    def test_worker_aborts_instead_of_running_without_buildings(self):
        raster = _LayerEntry(layer_type="raster", source_path="/a.tif",
                             extent={"south": 47.0, "north": 47.1,
                                     "west": 8.0, "east": 8.1},
                             target_resolutions=[30])
        buildings = _LayerEntry(layer_type="buildings",
                                source_path="/b/broken.gdb")
        with tempfile.TemporaryDirectory() as out_dir:
            worker = _MapConverterWorker(
                [raster, buildings], out_dir, [30],
                [{"kind": "files", "infos": []}, "/b/broken.gdb"])
            rec = _worker_signals(worker)
            with mock.patch.object(
                    _MapConverterWorker, "_resolve_raster",
                    return_value={"kind": "files", "infos": []}), \
                 mock.patch.object(
                    _MapConverterWorker, "_resolve_buildings",
                    side_effect=RuntimeError("GDB has no usable layer")):
                worker.run()
        self.assertEqual(len(rec["err"]), 1)
        self.assertIn("Buildings could not be prepared", rec["err"][0])
        self.assertIn("GDB has no usable layer", rec["err"][0])
        self.assertEqual(rec["ok"], [])


class TestCleanCancelDuringConvert(unittest.TestCase):
    """Cancel mid-convert must not surface 'Converter failed (exit -15)'."""

    RAW = {"south": 47.0, "north": 47.1, "west": 8.0, "east": 8.1}

    def _run_with_worker_hook(self, fake_runner):
        """Run a worker to phase 3 with *fake_runner* as the engine.

        The runner may read ``fake_runner.worker`` (set before run) to flip
        the cancel flag mid-"run", simulating cancel() winning the race.
        """
        with tempfile.TemporaryDirectory() as out_dir:
            # A source that really exists: the worker's launch preflight
            # refuses to start the converter on a path that is not there
            # (that is the whole point of naming it instead of letting the
            # engine die with a pathless "os error 3").
            src = os.path.join(out_dir, "a.tif")
            open(src, "w").close()
            entry = _LayerEntry(layer_type="raster", source_path=src,
                                extent=dict(self.RAW),
                                target_resolutions=[30])
            info = {"path": src, "crs": None, "crs_authid": "EPSG:4326",
                    "native_bounds": {"west": 0, "east": 20, "south": 40,
                                      "north": 50},
                    "wgs84_bounds": {"west": 0, "east": 20, "south": 40,
                                     "north": 50},
                    "halo_deg": 0.001}
            worker = _MapConverterWorker([entry], out_dir, [30], [src])
            fake_runner.worker = worker
            rec = _worker_signals(worker)
            with mock.patch.object(
                    _MapConverterWorker, "_resolve_raster",
                    return_value={"kind": "files", "infos": [info]}), \
                 mock.patch.object(_MapConverterWorker, "_resolve_buildings",
                                   return_value=None), \
                 mock.patch.object(mct, "find_binary",
                                   return_value="/bin/conv"), \
                 mock.patch.object(mct, "_plan_cross_check"), \
                 mock.patch.object(ta, "run_converter_streaming",
                                   side_effect=fake_runner):
                worker.run()
        return worker, rec

    def _run(self, fake_runner):
        return self._run_with_worker_hook(fake_runner)

    def test_kill_winning_the_race_is_not_an_error(self):
        # cancel() kills the process before the per-line check fires: the
        # runner returns the kill signal (-15) as the exit code. That must
        # end as a clean cancel, not "Converter failed (exit code -15)".
        def fake_runner(exe, args, on_line, on_start=None, env=None):
            fake_runner.worker._canceled = True
            return -15

        worker, rec = self._run_with_worker_hook(fake_runner)
        self.assertEqual(rec["err"], [],
                         "kill-race cancel surfaced as an error")
        self.assertEqual(rec["ok"], [])
        self.assertIn("Cancelled.", rec["status"])

    def test_converter_cancelled_exception_is_clean(self):
        def fake_runner(exe, args, on_line, on_start=None, env=None):
            raise ta.ConverterCancelled()

        _worker, rec = self._run(fake_runner)
        self.assertEqual(rec["err"], [])
        self.assertEqual(rec["ok"], [])
        self.assertIn("Cancelled.", rec["status"])

    def test_a_real_failure_still_errors(self):
        def fake_runner(exe, args, on_line, on_start=None, env=None):
            return 1

        _worker, rec = self._run(fake_runner)
        self.assertEqual(len(rec["err"]), 1)
        self.assertIn("exit code 1", rec["err"][0])
