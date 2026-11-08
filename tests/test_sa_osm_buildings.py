"""Site Analysis buildings: the strict path (2026-10-23) and prepared folders.

Site Analysis used to fetch OpenFreeMap tiles leniently (8 workers, one
attempt, a 4096-tile cap, tiles for the run's SECTOR only although pool
tiles are whole 0.25°–1° tiles) and, on any failure, logged "continuing
without buildings" and reported success. Pinned here, through
``terrain_adapter.prepare_terrain`` (fake fetcher and converter, and the
REAL 0.2.7 converter against the stand-in OpenFreeMap):

* the building tiles are the cover of the WHOLE pool tiles being built (not
  the sector), fetched strictly into THE store; a failure ENDS the run with
  its cause, nothing is downloaded or pooled, the next run fetches again;
* the download job (and the ingest jobs of a file/rendered source) carry
  the run's view; the converter's own lines decide: a pass without a burn
  report, a decode warning, buildings decoded and none drawn → the run
  fails (pool tiles a download wrote are flagged for rebuild; an ingest
  places nothing); "no buildings in extent" everywhere, or no tile with
  data at all, is a WARNING and a success;
* a prepared ``.abt`` folder: what the Map Converter recorded for the
  SELECTED tiles (``terrain_adapter.prepared_buildings``) — Site Analysis
  dialogs (all / none / mixed / unknown, ticked and unticked, once per
  folder per session), the "Include buildings" hint, the log line of
  prepare_terrain / prepare_terrain_profile, the P2P tab's log line;
* the fetch NEXT TO the download (2026-10-23, aether_converter >= 0.2.10,
  toolkit CONTRACT changelog 45 / §9b): the converter is launched before
  the fetch ends, its job carries ``buildings_pbf_ready`` /
  ``buildings_pbf_wait_s``, a stale marker is removed before the launch,
  the marker is written after the last link; a failed fetch kills the
  waiting converter and removes the pass's pool tiles; Stop is a quiet
  cancel with the same cleanup; an older converter keeps the sequential
  path (fake converter over a real process; and the REAL converter when
  ``AETHER_CONVERTER_0210`` or ``AETHER_BIN_DIR`` provides a >= 0.2.10);
* the fetch NEXT TO the conversion (2026-10-25, aether_converter >= 0.2.12,
  toolkit CONTRACT item 48): the folder route (and a rendered source)
  launches its ingest before the fetch ends, its jobs carry
  ``buildings_pbf_ready`` / ``buildings_pbf_wait_s``, the marker is written
  after the last link; a failed fetch kills the waiting converter (one that
  failed before the launch launches nothing) and pools nothing; Stop is
  quiet; an older converter keeps the sequential path, job for job (fake
  converter over a real process; the REAL converter when
  ``AETHER_CONVERTER_0212`` or ``AETHER_BIN_DIR`` provides a >= 0.2.12:
  byte-identical pool tile, killed while waiting by a failed fetch);
* the LIST of aether_converter >= 0.2.14 (``buildings_pbf_list``, toolkit
  CONTRACT changelog 51), both routes (``test_list_mode_…``): the job
  carries THE store + the list (absolute, under ``<cache>/views``) +
  ``buildings_pbf_wait_s``, never ``buildings_pbf_ready``; no building tile
  is linked, no building view made; the list is written once, atomically,
  naming exactly the tiles with data, after the fetch; a stale list is
  removed first (cannot → the 0.2.12 view); a failed fetch / Stop: no list,
  the converter killed; 0.2.13 keeps the view. The marker classes pin a
  >= 0.2.14 binary to the view; ``RealConverterListsBesideTheConversion``
  (``AETHER_CONVERTER_0214`` or ``AETHER_BIN_DIR`` provides a >= 0.2.14;
  skipped without) pools the bytes of the view mode.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import contextlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

import waveshed.core.terrain_adapter as ta
import waveshed.gui.p2p_tab as p2p
import waveshed.gui.site_analysis_tab as sat
from waveshed.core import abt_index
from waveshed.core import binary_manager as bmod
from waveshed.core import openfreemap as ofm
from waveshed.core import osm_buildings as ob
from waveshed.core.job_builder import CoverageParams

from _ofm_server import OfmServer, building_tile
from _real_gdal import HAVE_GDAL, real_gdal, write_tif
from test_abt_selection import _write_tile as _write_header_tile
from test_mc_osm_buildings import (HOUSE, _AFTER_LINE_S, _HeldTiles,
                                   _WAITING, _ingest_ready_converter,
                                   _list_converter)
from test_osm_buildings import (DECODING, DOWNLOAD_DRAWN, DOWNLOAD_EMPTY,
                                DOWNLOAD_NONE_DRAWN, INGEST_DRAWN,
                                INGEST_NONE_DRAWN, read_building_list)
from test_raw_tile_cache import _XYZ as _RAW_XYZ
from test_raw_tile_cache import _Engine as _RawEngine
from test_raw_tile_cache import _bm as _raw_bm
from test_terrain_adapter import _fake_probe
from test_two_phase_convert import _tiles as _mc_tiles
from test_two_phase_convert import (_find_real_converter, _sleeper,
                                    _terrarium_png, _wait_for, _write_tile)

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: Scratch inside the project's gitignored .tmp (never the container drive).
_SCRATCH = os.path.join(_REPO, ".tmp", "test_sa_osm_buildings")

#: A terrarium service the downloader knows the encoding of.
_MAPZEN = ("type=xyz&url=https%3A//s3.amazonaws.com/elevation-tiles-prod/"
           "terrarium/%7Bz%7D/%7Bx%7D/%7By%7D.png&zmax=15")
_WMS = "contextualWMSLegend=0&crs=EPSG:4326&url=https://wms.example/x"
#: The middle of the 30 m pool tile 47.0-47.5 N, 8.0-8.5 E.
SITE = (47.25, 8.25)
TILE_A = "tile_N47.50E8.00_30m.abt"


def _scratch(test):
    os.makedirs(_SCRATCH, exist_ok=True)
    d = tempfile.mkdtemp(prefix="t_", dir=_SCRATCH)
    test.addCleanup(shutil.rmtree, d, True)
    return d


def _warn(name):
    return (f"[Warn] buildings {name}: PBF decode error: failed to decode "
            f"Protobuf message: unexpected end group tag")


def _sparse(name):
    """About one z14 tile in 64 has data (few store files: the scratch is
    on the project mount)."""
    _z, x, y = (int(v) for v in name[:-len(".pbf")].split("_"))
    return x % 8 == 0 and y % 8 == 0


class _Fetch:
    """``openfreemap.fetch_building_tiles`` stand-in: every tile *data*
    says has data is written into the store; *fail* raises FetchFailed."""

    def __init__(self, data=_sparse, fail=None):
        self.data = data
        self.fail = fail
        self.calls = []

    def __call__(self, tiles, store_dir, template, should_cancel=None,
                 on_start=None, on_progress=None, require_data=True, **_kw):
        tiles = list(tiles)
        self.calls.append(tiles)
        if self.fail:
            raise ofm.FetchFailed(self.fail, missing=3, total=len(tiles),
                                  cause=self.fail)
        os.makedirs(store_dir, exist_ok=True)
        if on_start is not None:
            on_start(0, len(tiles))
        present = []
        for z, x, y in tiles:
            name = ofm.tile_filename(z, x, y)
            if self.data(name):
                with open(os.path.join(store_dir, name), "wb") as fh:
                    fh.write(b"pbf")
                present.append(name)
        if on_progress is not None:
            on_progress(len(tiles), len(tiles), 3 * len(present))
        if not present and require_data:
            raise ofm.FetchFailed("none has data", missing=len(tiles),
                                  total=len(tiles), cause="no tile with data")
        return ofm.FetchResult(len(tiles), 0, len(present),
                               len(tiles) - len(present), 3 * len(present),
                               0.01, tuple(sorted(present)))


class _Download:
    """One ``aether_converter download`` pass (``_run_converter_download_
    once``): each spec's tile written whole into the pool, *lines* printed
    between the progress and the stats."""

    def __init__(self, lines):
        self.lines = list(lines)
        self.jobs = []

    def __call__(self, exe, job_file, on_line=None, should_cancel=None,
                 on_start=None):
        with open(job_file, encoding="utf-8") as fh:
            job = json.load(fh)
        self.jobs.append(job)
        for spec in job["tiles"]:
            _write_tile({"output_path": os.path.join(job["output_dir"],
                                                     spec["filename"]),
                         "size_px": spec["size_px"], "ul_lat": spec["ul_lat"],
                         "ul_lon": spec["ul_lon"]})
        lines = ([f"[Download] z={job['zoom']} tiles=3x3=9 (~1MB) "
                  f"connections=8"] + self.lines
                 + ["[Stats] Tiles: 9/9 OK (100.0% success)"])
        for line in lines:
            if on_line is not None:
                on_line(line)
        return 0, lines


class _SaCase(unittest.TestCase):

    #: What the converter's ``--version`` answers (the probe the download
    #: route gates on): the frozen 0.2.7 — the sequential fetch.
    converter_version = "0.2.7"

    def setUp(self):
        self.cache = _scratch(self)
        self.logged = []
        for patch in (mock.patch.object(ta, "get_cache_dir",
                                        return_value=self.cache),
                      mock.patch.object(ta, "_log",
                                        side_effect=self.logged.append),
                      mock.patch.object(ta, "classify_raster_layer",
                                        return_value="dem"),
                      mock.patch.object(bmod, "probe_converter_version",
                                        side_effect=lambda *_a, **_k:
                                        self.converter_version),
                      mock.patch.object(ofm, "resolve_tile_template_strict",
                                        return_value="http://ofm.test/{z}/{x}"
                                                     "/{y}.pbf")):
            patch.start()
            self.addCleanup(patch.stop)
        self.bm = mock.Mock()
        self.bm.find_binary.return_value = "aether_converter"
        self.pool = ta._pool_dir(_MAPZEN, ta.buildings_identity(
            osm_buildings=True))

    def layer(self, source=_MAPZEN):
        layer = mock.Mock()
        layer.source.return_value = source
        layer.name.return_value = "dem"
        layer.crs.return_value = conftest._FakeCRS("EPSG:4326")
        return layer

    def store_names(self):
        try:
            return set(os.listdir(ta.osm_buildings_store_dir()))
        except OSError:
            return set()

    def text(self):
        return "\n".join(self.logged)


# ---------------------------------------------------------------------------
# The download route (an XYZ source): fetch, job, the converter's lines
# ---------------------------------------------------------------------------

class DownloadRoute(_SaCase):

    def run_(self, lines, fetch=None):
        fetch = fetch or _Fetch()
        download = _Download(lines)
        with mock.patch.object(ofm, "fetch_building_tiles",
                               side_effect=fetch), \
             mock.patch.object(ta, "_run_converter_download_once",
                               side_effect=download), \
             mock.patch.object(ta, "_binary_has_buildings_support",
                               return_value=True):
            try:
                view = ta.prepare_terrain(self.layer(), *SITE, 1.0, 30,
                                          self.bm, osm_buildings=True)
                return view, None, fetch, download
            except RuntimeError as exc:
                return None, str(exc), fetch, download

    def test_the_whole_pool_tile_is_covered_and_the_job_carries_it(self):
        view, err, fetch, download = self.run_([DOWNLOAD_DRAWN])
        self.assertIsNone(err)
        bbox = ta._compute_sector_bbox(*SITE, 1.0, 0.0, 360.0)
        tiles = ta._compute_subtiles(bbox, 30, *SITE, 1.0, 0.0, 360.0)
        self.assertEqual([t["filename"] for t in tiles], [TILE_A])
        whole = ob.BuildingCover([ta._subtile_bbox(tiles[0])]).needed()
        self.assertEqual(fetch.calls, [whole])
        sector = {(14, x, y) for _z, x, y in ofm.tiles_for_bbox(bbox)}
        self.assertTrue(sector < set(whole))
        self.assertGreater(len(whole), 20 * len(sector))
        # The download job carries a view of exactly the fetched tiles.
        (job,) = download.jobs
        pbf = job["buildings_pbf_dir"]
        self.assertTrue(pbf.startswith(os.path.join(self.cache, "views")))
        with_data = sorted(n for n in (ofm.tile_filename(*t) for t in whole)
                           if _sparse(n))
        self.assertTrue(with_data)
        self.assertEqual(sorted(os.listdir(pbf)), with_data)
        text = self.text()
        self.assertIn(f"  buildings (OpenFreeMap z14): {len(whole):,} tile(s) "
                      f"for the 1 pool tile(s) to build — whole tiles, not "
                      f"only this run's area (at most ~", text)
        self.assertIn("  buildings: 12 building(s) decoded, 3,835 px raised",
                      text)
        self.assertNotIn("WARNING", text)
        self.assertTrue(ta._tile_ready(self.pool, TILE_A))
        self.assertTrue(os.path.isdir(view))
        # Pooled with its buildings: the next run is a cache HIT.
        view2, err2, fetch2, download2 = self.run_([DOWNLOAD_DRAWN])
        self.assertIsNone(err2)
        self.assertEqual((fetch2.calls, download2.jobs), ([], []))

    def test_a_fetch_failure_ends_the_run_before_any_download(self):
        cause = ("3 of 812 building tiles missing — tile 14_8555_5755.pbf: "
                 "timed out (after 4 attempts)")
        view, err, fetch, download = self.run_(
            [DOWNLOAD_DRAWN], fetch=_Fetch(fail=cause))
        self.assertIsNone(view)
        self.assertEqual(err.split("\n")[0],
                         f"Buildings download failed (OpenFreeMap z14): "
                         f"{cause}.")
        self.assertIn("untick 'Include buildings'", err)
        self.assertEqual(download.jobs, [])
        self.assertFalse(os.path.exists(os.path.join(self.pool, TILE_A)))
        self.assertNotIn("continuing without buildings", self.text())

    def test_no_tile_with_data_is_a_warning_not_an_error(self):
        view, err, _fetch, download = self.run_(
            [], fetch=_Fetch(data=lambda name: False))
        self.assertIsNone(err)
        (job,) = download.jobs
        self.assertNotIn("buildings_pbf_dir", job)
        self.assertIn("WARNING: buildings: none of the", self.text())
        self.assertTrue(ta._tile_ready(self.pool, TILE_A))

    def test_no_buildings_in_extent_is_a_warning_not_an_error(self):
        view, err, _fetch, _download = self.run_([DOWNLOAD_EMPTY])
        self.assertIsNone(err)
        self.assertIn("WARNING: buildings: the 2 OpenFreeMap tile(s) of these "
                      "pool tiles hold no building", self.text())
        self.assertTrue(ta._tile_ready(self.pool, TILE_A))

    def test_decoded_and_none_drawn_is_an_error_and_flags_the_tile(self):
        view, err, _fetch, _download = self.run_([DOWNLOAD_NONE_DRAWN])
        self.assertIsNone(view)
        self.assertIn("the converter decoded 12 building(s) from the 12 "
                      "OpenFreeMap tile(s) it was given but drew none", err)
        self.assertIn("flagged for rebuild", err)
        self.assertFalse(ta._tile_ready(self.pool, TILE_A))
        # The next run builds it again.
        _view, err2, fetch2, download2 = self.run_([DOWNLOAD_DRAWN])
        self.assertIsNone(err2)
        self.assertEqual(len(download2.jobs), 1)
        self.assertTrue(ta._tile_ready(self.pool, TILE_A))

    def test_a_download_without_a_burn_report_is_an_error(self):
        view, err, _fetch, _download = self.run_([])
        self.assertIsNone(view)
        self.assertIn("does not apply buildings_pbf_dir to downloads", err)
        self.assertFalse(ta._tile_ready(self.pool, TILE_A))

    def test_an_undecodable_tile_ends_the_run_and_leaves_the_cache(self):
        bad = next(n for n in (ofm.tile_filename(*t) for t in ob.BuildingCover(
            [ta._subtile_bbox(ta._compute_subtiles(
                ta._compute_sector_bbox(*SITE, 1.0, 0, 360), 30, *SITE, 1.0,
                0, 360)[0])]).needed()) if _sparse(n))
        view, err, _fetch, _download = self.run_([_warn(bad), DOWNLOAD_DRAWN])
        self.assertIsNone(view)
        self.assertIn(f"the converter could not decode 1 OpenFreeMap tile(s) "
                      f"(first: {bad}: PBF decode error", err)
        self.assertNotIn(bad, self.store_names())
        self.assertFalse(ta._tile_ready(self.pool, TILE_A))


class DiskNeed(_SaCase):
    """No tile cap any more: the count and its bound are priced instead —
    ``terrain_plan``'s ``buildings_mb`` and the refusal's "building tiles"."""

    def test_the_plan_bounds_the_building_tiles_of_the_missing_tiles(self):
        plain = ta.terrain_plan(_MAPZEN, *SITE, 1.0, 30)
        osm = ta.terrain_plan(_MAPZEN, *SITE, 1.0, 30, buildings=ta.
                              buildings_identity(osm_buildings=True))
        self.assertEqual(plain["buildings_mb"], 0)
        whole = ofm.estimate_tile_count({"south": 47.0, "north": 47.5,
                                         "west": 8.0, "east": 8.5})
        self.assertEqual(osm["buildings_mb"],
                         -(-whole * ob.TILE_BYTES_ESTIMATE // (1024 * 1024)))
        self.assertGreater(osm["buildings_mb"], 0)
        # Pooled tiles fetch nothing.
        os.makedirs(self.pool, exist_ok=True)
        t = ta._compute_subtiles(ta._compute_sector_bbox(*SITE, 1.0, 0, 360),
                                 30, *SITE, 1.0, 0, 360)[0]
        _write_tile({"output_path": os.path.join(self.pool, t["filename"]),
                     "size_px": t["size_px"], "ul_lat": t["ul_lat"],
                     "ul_lon": t["ul_lon"]})
        pooled = ta.terrain_plan(_MAPZEN, *SITE, 1.0, 30, buildings=ta.
                                 buildings_identity(osm_buildings=True))
        self.assertEqual(pooled["buildings_mb"], 0)

    def test_the_refusal_counts_them_on_the_cache_drive(self):
        import waveshed.core.output_estimate as oe
        p = mock.Mock(model="LOS", max_range_km=1.0, resolution_m=30,
                      az_start=0.0, az_end=360.0)
        need = oe.engine_output_bytes("LOS", 1.0, 30) + 40 * 1024 * 1024
        fits = need + oe.DISK_RESERVE_BYTES
        self.assertIsNone(sat._disk_refusal(
            [(p, "Site 1")], 0, self.cache, self.cache,
            free_of=lambda _p: fits, buildings_mb=40))
        msg = sat._disk_refusal([(p, "Site 1")], 0, self.cache, self.cache,
                                free_of=lambda _p: fits - 1, buildings_mb=40)
        self.assertIn("building tiles", msg)


# ---------------------------------------------------------------------------
# The fetch next to the download (aether_converter >= 0.2.10)
# ---------------------------------------------------------------------------

#: What 0.2.10 prints for an empty buildings_pbf_dir (probed, 2026-10-23).
DOWNLOAD_ZERO = "[Buildings] 0 pbf tile(s), no buildings in extent"


class _SlowFetch(_Fetch):
    """``_Fetch`` that takes *seconds* first (Stop seen within 20 ms →
    FetchCancelled) and records when it began and ended. *fail*: after the
    wait, raise as the strict fetch does (cause = the tile and its error)."""

    def __init__(self, seconds=1.0, fail=None, **kw):
        super().__init__(**kw)
        self.seconds = seconds
        self.fail_cause = fail
        self.began = self.ended = None
        self.cancelled = False

    def __call__(self, tiles, store_dir, template, should_cancel=None,
                 **kw):
        tiles = list(tiles)
        self.began = time.monotonic()
        try:
            end = self.began + self.seconds
            while time.monotonic() < end:
                if should_cancel is not None and should_cancel():
                    self.cancelled = True
                    raise ofm.FetchCancelled()
                time.sleep(0.02)
            if self.fail_cause:
                self.calls.append(tiles)
                raise ofm.FetchFailed(
                    f"3 of {len(tiles):,} building tiles missing — tile "
                    f"{self.fail_cause}", missing=3, total=len(tiles),
                    cause=self.fail_cause)
            return super().__call__(tiles, store_dir, template,
                                    should_cancel=should_cancel, **kw)
        finally:
            self.ended = time.monotonic()


class _WaitingConverter:
    """``run_converter_streaming`` for ``download`` as aether_converter
    0.2.10 runs a job (CONTRACT §9b), over a REAL process (a sleeper handed
    to on_start: what Stop and a failed fetch kill). The terrain first —
    every spec's tile written whole into output_dir — then, with
    ``buildings_pbf_ready``, the wait for the marker (polled every 20 ms;
    the wait / ready lines; *on_wait(proc)* when it starts), the directory
    read ONCE (:attr:`read`: its tile names), the burn line, the stats.
    Killed while waiting: its exit code, the terrain left behind (a killed
    converter cannot clean up). With ``buildings_pbf_list`` (0.2.14,
    CONTRACT changelog 51) the list is the marker and the tiles it names
    are read from ``buildings_pbf_dir`` (never a listing of it); a listed
    name not there → exit 1 (:attr:`errors`)."""

    def __init__(self, test, burn=DOWNLOAD_DRAWN, on_wait=None):
        self.test = test
        self.burn = burn
        self.on_wait = on_wait
        self.jobs, self.launched, self.read = [], [], []
        self.marker_at_launch, self.killed, self.errors = [], [], []
        self.dir_at_launch = []

    def __call__(self, exe, args, on_line, on_start=None, env=None):
        with open(args[-1], encoding="utf-8") as fh:
            job = json.load(fh)
        self.jobs.append(job)
        self.launched.append(time.monotonic())
        pbf, ready = job.get("buildings_pbf_dir"), job.get(
            "buildings_pbf_ready")
        listed = job.get("buildings_pbf_list")
        if listed and ready:
            self.errors.append("buildings_pbf_ready with buildings_pbf_list")
            return 1
        marker = (listed if listed
                  else os.path.join(pbf, ready) if pbf and ready else None)
        self.marker_at_launch.append(bool(marker)
                                     and os.path.lexists(marker))
        self.dir_at_launch.append(bool(pbf) and os.path.isdir(pbf))
        proc = _sleeper(self.test)
        if on_start is not None:
            on_start(proc)
        for spec in job["tiles"]:
            _write_tile({"output_path": os.path.join(job["output_dir"],
                                                     spec["filename"]),
                         "size_px": spec["size_px"], "ul_lat": spec["ul_lat"],
                         "ul_lon": spec["ul_lon"]})
        on_line(f"[Download] z={job['zoom']} tiles=3x3=9 (~1MB) "
                f"connections=8")
        if marker and not os.path.isfile(marker):
            on_line(f"[Buildings] waiting for the building tiles ({marker})")
            if self.on_wait is not None:
                self.on_wait(proc)
            t0 = time.monotonic()
            while not os.path.isfile(marker):
                if proc.poll() is not None:
                    self.killed.append(proc.returncode)
                    return proc.returncode
                time.sleep(0.02)
            on_line(f"[Buildings] building tiles ready after "
                    f"{time.monotonic() - t0:.1f} s")
        if pbf and listed:
            names = read_building_list(listed)
            gone = [n for n in names
                    if not os.path.isfile(os.path.join(pbf, n))]
            if gone:
                self.errors.append(gone[0])
                on_line(f"Error: Building tile {gone[0]} listed in {listed} "
                        f"is not in {pbf}")
                proc.kill()
                proc.wait()
                return 1
            self.read.append(sorted(names))
        elif pbf:
            self.read.append(sorted(n for n in os.listdir(pbf)
                                    if n.endswith(".pbf")))
        if pbf:
            for line in ([self.burn] if isinstance(self.burn, str)
                         else self.burn):
                on_line(line)
        on_line("[Stats] Tiles: 9/9 OK (100.0% success)")
        proc.kill()
        proc.wait()
        return 0


def _whole_cover_names():
    """The z14 tiles of the pool tile SITE's 1 km run builds, and those of
    them :func:`_sparse` gives data."""
    (tile,) = ta._compute_subtiles(ta._compute_sector_bbox(*SITE, 1.0, 0, 360),
                                   30, *SITE, 1.0, 0, 360)
    names = [ofm.tile_filename(*t)
             for t in ob.BuildingCover([ta._subtile_bbox(tile)]).needed()]
    return names, sorted(n for n in names if _sparse(n))


class BesideTheDownload(_SaCase):
    """The fetch runs WHILE the converter downloads (converter >= 0.2.10)."""

    converter_version = "0.2.10"

    def setUp(self):
        super().setUp()
        self.names, self.with_data = _whole_cover_names()
        self.view = ta.osm_buildings_view_dir(self.names,
                                              marker=ob.READY_MARKER)
        self.marker = os.path.join(self.view, ob.READY_MARKER)

    def run_(self, fetch, conv, should_cancel=None, on_start=None,
             profile=None):
        with mock.patch.object(ofm, "fetch_building_tiles",
                               side_effect=fetch), \
             mock.patch.object(ta, "run_converter_streaming",
                               side_effect=conv), \
             mock.patch.object(ta, "_binary_has_buildings_support",
                               return_value=True):
            try:
                if profile is not None:
                    view = ta.prepare_terrain_profile(
                        self.layer(), *SITE, profile, self.bm,
                        osm_buildings=True, should_cancel=should_cancel,
                        on_start=on_start)
                else:
                    view = ta.prepare_terrain(
                        self.layer(), *SITE, 1.0, 30, self.bm,
                        osm_buildings=True, should_cancel=should_cancel,
                        on_start=on_start)
                return view, None
            except RuntimeError as exc:
                return None, str(exc)
            finally:
                self.assertFalse(
                    any(t.name == "waveshed-buildings" and t.is_alive()
                        for t in threading.enumerate()),
                    "the fetch thread outlived the run")

    def test_the_converter_runs_while_the_tiles_are_fetched(self):
        fetch, conv = _SlowFetch(1.0), _WaitingConverter(self)
        links = []
        real_link = os.link

        def link(src, dst, *a, **kw):
            if dst.endswith(".pbf"):        # not the terrain view's .abt
                links.append((os.path.basename(dst),
                              os.path.lexists(self.marker)))
            return real_link(src, dst, *a, **kw)

        with mock.patch.object(ta.os, "link", side_effect=link):
            view, err = self.run_(fetch, conv)
        self.assertIsNone(err)
        # Launched at once — long before the 1 s fetch ended.
        (launched,) = conv.launched
        self.assertLess(launched, fetch.ended - 0.5)
        (job,) = conv.jobs
        self.assertEqual(job["buildings_pbf_dir"], os.path.abspath(self.view))
        self.assertEqual(job["buildings_pbf_ready"], "_ready")
        self.assertEqual(job["buildings_pbf_wait_s"], 14400)
        self.assertEqual(conv.marker_at_launch, [False])
        # Every tile linked before the marker; the converter read them all.
        self.assertTrue(links)
        self.assertFalse(any(seen for _n, seen in links), links)
        self.assertTrue(os.path.isfile(self.marker))
        self.assertEqual(conv.read, [self.with_data])
        text = self.text()
        self.assertIn("    [Buildings] waiting for the building tiles (", text)
        self.assertRegex(text, r"  buildings fetched next to the terrain "
                               r"download: fetch \d+\.\d s, download \d+\.\d s, "
                               r"converter waited \d+\.\d s")
        self.assertIn("  buildings: 12 building(s) decoded, 3,835 px raised",
                      text)
        self.assertNotIn("WARNING", text)
        self.assertTrue(ta._tile_ready(self.pool, TILE_A))
        self.assertTrue(os.path.isdir(view))
        # Pooled with its buildings: the next run is a cache HIT.
        fetch2, conv2 = _SlowFetch(0.0), _WaitingConverter(self)
        _view, err2 = self.run_(fetch2, conv2)
        self.assertIsNone(err2)
        self.assertEqual((fetch2.calls, conv2.jobs), ([], []))

    def test_a_stale_marker_is_removed_before_the_launch(self):
        os.makedirs(self.view)
        for stale in (ob.READY_MARKER, "14_1_1.pbf"):
            with open(os.path.join(self.view, stale), "w") as fh:
                fh.write("old")
        conv = _WaitingConverter(self)
        _view, err = self.run_(_SlowFetch(0.3), conv)
        self.assertIsNone(err)
        self.assertEqual(conv.marker_at_launch, [False])
        self.assertEqual(conv.read, [self.with_data])      # no stale tile

    def test_an_older_converter_fetches_first_without_the_fields(self):
        for version in ("0.2.9", None):
            self.converter_version = version
            fetch, conv = _SlowFetch(0.3), _WaitingConverter(self)
            _view, err = self.run_(fetch, conv)
            self.assertIsNone(err)
            (job,) = conv.jobs
            self.assertNotIn("buildings_pbf_ready", job)
            self.assertNotIn("buildings_pbf_wait_s", job)
            self.assertGreater(conv.launched[0], fetch.ended)
            # The batch view of exactly the tiles with data, as before.
            self.assertEqual(job["buildings_pbf_dir"],
                             ta.osm_buildings_view_dir(self.with_data))
            self.assertNotIn("fetched next to the terrain download",
                             self.text())
            shutil.rmtree(self.pool)                  # rebuilt next round

    def test_a_failed_fetch_kills_the_converter_and_removes_its_tiles(self):
        cause = "14_8555_5755.pbf: timed out (after 4 attempts)"
        fetch = _SlowFetch(1.0, fail=cause)
        conv = _WaitingConverter(self)
        procs = []
        view, err = self.run_(fetch, conv, on_start=procs.append)
        self.assertIsNone(view)
        self.assertEqual(err.split("\n")[0],
                         f"Buildings download failed (OpenFreeMap z14): 3 of "
                         f"{len(self.names):,} building tiles missing — tile "
                         f"{cause}.")
        self.assertIn("untick 'Include buildings'", err)
        # It was waiting, and was killed — by the fetch, not by Stop.
        self.assertEqual(len(conv.killed), 1)
        self.assertEqual(len(procs), 1)
        self.assertIsNotNone(procs[0].poll())
        # Terrain without its buildings is never pooled; no marker.
        self.assertFalse(os.path.exists(os.path.join(self.pool, TILE_A)))
        self.assertFalse(ta._tile_ready(self.pool, TILE_A))
        self.assertFalse(os.path.lexists(self.marker))
        text = self.text()
        self.assertIn("  ERROR: Buildings download failed (OpenFreeMap z14): "
                      "3 of", text)
        self.assertIn("1 pool tile(s) it wrote removed", text)
        # The next run fetches again and builds the tile.
        _view, err2 = self.run_(_SlowFetch(0.2), _WaitingConverter(self))
        self.assertIsNone(err2)
        self.assertTrue(ta._tile_ready(self.pool, TILE_A))

    def _stop_case(self, kill_on_stop):
        stop = threading.Event()
        procs = []

        def on_wait(proc):
            stop.set()                     # Stop, while the converter waits
            if kill_on_stop:               # the tab's worker kills its proc
                ta.kill_engine_process(procs[-1])

        fetch = _SlowFetch(5.0)
        conv = _WaitingConverter(self, on_wait=on_wait)
        t0 = time.monotonic()
        with self.assertRaises(ta.ConverterCancelled):
            self.run_(fetch, conv, should_cancel=stop.is_set,
                      on_start=procs.append if kill_on_stop else None)
        self.assertLess(time.monotonic() - t0, 4.0, "Stop was not prompt")
        self.assertTrue(fetch.cancelled)
        self.assertEqual(len(conv.killed), 1)
        self.assertFalse(os.path.exists(os.path.join(self.pool, TILE_A)))
        self.assertFalse(os.path.lexists(self.marker))
        self.assertNotIn("ERROR", self.text())

    def test_stop_during_the_wait_is_quiet_and_cleans_up(self):
        self._stop_case(kill_on_stop=True)

    def test_stop_without_an_on_start_hook_still_ends_the_wait(self):
        # A caller with should_cancel only: the fetch, stopped, kills the
        # converter it was holding back (it prints nothing while waiting).
        self._stop_case(kill_on_stop=False)

    def test_an_undecodable_tile_is_still_the_burns_verdict(self):
        bad = self.with_data[0]
        conv = _WaitingConverter(self, burn=[_warn(bad), DOWNLOAD_DRAWN])
        view, err = self.run_(_SlowFetch(0.3), conv)
        self.assertIsNone(view)
        self.assertIn(f"the converter could not decode 1 OpenFreeMap tile(s) "
                      f"(first: {bad}: PBF decode error", err)
        self.assertNotIn(bad, self.store_names())
        self.assertNotIn(bad, os.listdir(self.view))
        self.assertFalse(ta._tile_ready(self.pool, TILE_A))   # flagged

    def test_no_tile_with_data_still_releases_the_download(self):
        conv = _WaitingConverter(self, burn=DOWNLOAD_ZERO)
        _view, err = self.run_(_SlowFetch(0.3, data=lambda _n: False), conv)
        self.assertIsNone(err)
        self.assertEqual(conv.read, [[]])
        self.assertTrue(os.path.isfile(self.marker))
        text = self.text()
        self.assertEqual(text.count("WARNING"), 1, text)
        self.assertIn("WARNING: buildings: none of the", text)
        self.assertTrue(ta._tile_ready(self.pool, TILE_A))

    def test_every_profile_band_with_buildings_runs_beside_its_download(self):
        fetch, conv = _SlowFetch(0.6), _WaitingConverter(self)
        view, err = self.run_(fetch, conv,
                              profile=[(1000.0, 30), (3000.0, 90)])
        self.assertIsNone(err)
        self.assertEqual(len(conv.jobs), 2)
        for job in conv.jobs:
            self.assertEqual(job["buildings_pbf_ready"], "_ready")
        self.assertEqual(conv.marker_at_launch, [False, False])
        self.assertEqual(len(fetch.calls), 2)
        self.assertEqual(self.text().count(
            "buildings fetched next to the terrain download: "), 2)

    # -- aether_converter >= 0.2.14: the list, no view (changelog 51) -------
    def list_mode(self):
        self.converter_version = "0.2.14"
        self.list = ta.osm_buildings_list_path(self.names)
        self.store = ta.osm_buildings_store_dir()

    def views(self):
        views = os.path.join(self.cache, "views")
        return set(os.listdir(views)) if os.path.isdir(views) else set()

    def test_list_mode_the_download_reads_the_store_through_its_list(self):
        self.list_mode()
        fetch, conv = _SlowFetch(1.0), _WaitingConverter(self)
        links, writes = [], []
        real_link, real_replace = os.link, ta.replace_with_retry

        def link(src, dst, *a, **kw):
            if dst.endswith(".pbf"):        # not the terrain view's .abt
                links.append(dst)
            return real_link(src, dst, *a, **kw)

        def replace(src, dst, *a, **kw):
            if dst == self.list:            # atomic: a whole temp renamed
                with open(src, encoding="utf-8") as fh:
                    writes.append((os.path.dirname(src)
                                   == os.path.dirname(dst),
                                   os.path.lexists(dst), fh.read()))
            return real_replace(src, dst, *a, **kw)

        with mock.patch.object(ta.os, "link", side_effect=link), \
             mock.patch.object(ta, "replace_with_retry", side_effect=replace):
            view, err = self.run_(fetch, conv)
        self.assertIsNone(err)
        # Launched at once — long before the 1 s fetch ended.
        (launched,) = conv.launched
        self.assertLess(launched, fetch.ended - 0.5)
        (job,) = conv.jobs
        self.assertEqual(job["buildings_pbf_dir"], os.path.abspath(self.store))
        self.assertEqual(job["buildings_pbf_list"], self.list)
        self.assertTrue(os.path.isabs(self.list))
        self.assertTrue(self.list.startswith(os.path.join(self.cache, "views")
                                             + os.sep), self.list)
        self.assertEqual(job["buildings_pbf_wait_s"], 14400)
        self.assertNotIn("buildings_pbf_ready", job)
        self.assertEqual(conv.marker_at_launch, [False])
        # THE store was there at the launch (a fresh cache: the fetch had
        # written nothing yet).
        self.assertEqual(conv.dir_at_launch, [True])
        self.assertEqual(conv.errors, [])
        # The list written once, atomically, with exactly the tiles with
        # data; the converter read those from the store; nothing linked, no
        # building view (the terrain view and the lists' directory only).
        self.assertEqual(writes, [(True, False, "".join(
            f"{n}\n" for n in self.with_data))])
        self.assertEqual(read_building_list(self.list), self.with_data)
        self.assertEqual(conv.read, [self.with_data])
        self.assertEqual(links, [])
        self.assertEqual(self.views(), {os.path.basename(view),
                                        os.path.basename(
                                            os.path.dirname(self.list))})
        text = self.text()
        self.assertIn(f"  buildings: fetched next to the terrain download "
                      f"(aether_converter >= 0.2.14 waits for {self.list})",
                      self.logged)
        self.assertIn(f"  buildings fused during download (buildings_pbf_dir; "
                      f"the converter waits for {self.list})", self.logged)
        self.assertIn(f"    [Buildings] waiting for the building tiles "
                      f"({self.list})", self.logged)
        self.assertIn(f"  buildings: {len(self.with_data):,} OpenFreeMap "
                      f"tile(s) listed for the converter in {self.list} — the "
                      f"download burns them now", self.logged)
        self.assertRegex(text, r"  buildings fetched next to the terrain "
                               r"download: fetch \d+\.\d s, download \d+\.\d s, "
                               r"converter waited \d+\.\d s")
        self.assertIn("  buildings: 12 building(s) decoded, 3,835 px raised",
                      text)
        self.assertNotIn("_ready", text)
        self.assertNotIn("linked into", text)
        self.assertNotIn("WARNING", text)
        self.assertTrue(ta._tile_ready(self.pool, TILE_A))
        # Pooled with its buildings: the next run is a cache HIT.
        fetch2, conv2 = _SlowFetch(0.0), _WaitingConverter(self)
        _view, err2 = self.run_(fetch2, conv2)
        self.assertIsNone(err2)
        self.assertEqual((fetch2.calls, conv2.jobs), ([], []))

    def test_list_mode_a_stale_list_is_removed_before_the_launch(self):
        self.list_mode()
        os.makedirs(os.path.dirname(self.list))
        with open(self.list, "w", encoding="utf-8") as fh:
            fh.write("14_1_1.pbf\n")      # an earlier run's: not in the store
        conv = _WaitingConverter(self)
        _view, err = self.run_(_SlowFetch(0.3), conv)
        self.assertIsNone(err)
        (job,) = conv.jobs
        self.assertEqual(job["buildings_pbf_list"], self.list)
        self.assertEqual(conv.marker_at_launch, [False])
        self.assertEqual(conv.errors, [])
        self.assertEqual(conv.read, [self.with_data])
        self.assertEqual(read_building_list(self.list), self.with_data)

    def test_list_mode_a_list_that_cannot_be_removed_takes_the_view(self):
        self.list_mode()
        os.makedirs(self.list)            # os.remove refuses a directory
        conv = _WaitingConverter(self)
        _view, err = self.run_(_SlowFetch(0.3), conv)
        self.assertIsNone(err)
        (job,) = conv.jobs
        self.assertEqual(job["buildings_pbf_dir"], os.path.abspath(self.view))
        self.assertEqual(job["buildings_pbf_ready"], "_ready")
        self.assertNotIn("buildings_pbf_list", job)
        self.assertTrue(os.path.isfile(self.marker))
        self.assertEqual(conv.read, [self.with_data])
        self.assertIn(f"  WARNING: buildings: {self.list} (left by an earlier "
                      f"run) could not be removed — the building tiles are "
                      f"linked into a view for the download instead",
                      self.logged)

    def test_list_mode_a_failed_fetch_kills_the_converter_no_list(self):
        self.list_mode()
        cause = "14_8555_5755.pbf: timed out (after 4 attempts)"
        conv = _WaitingConverter(self)
        procs = []
        view, err = self.run_(_SlowFetch(1.0, fail=cause), conv,
                              on_start=procs.append)
        self.assertIsNone(view)
        self.assertEqual(err.split("\n")[0],
                         f"Buildings download failed (OpenFreeMap z14): 3 of "
                         f"{len(self.names):,} building tiles missing — tile "
                         f"{cause}.")
        (job,) = conv.jobs
        self.assertEqual(job["buildings_pbf_list"], self.list)
        # It was waiting, and was killed — by the fetch; no list, no tile.
        self.assertEqual(len(conv.killed), 1)
        self.assertIsNotNone(procs[0].poll())
        self.assertFalse(os.path.lexists(self.list))
        self.assertFalse(os.path.exists(os.path.join(self.pool, TILE_A)))
        self.assertIn("1 pool tile(s) it wrote removed", self.text())

    def test_list_mode_stop_during_the_wait_is_quiet_no_list(self):
        self.list_mode()
        stop = threading.Event()
        procs = []

        def on_wait(proc):
            stop.set()                     # Stop, while the converter waits
            ta.kill_engine_process(procs[-1])

        fetch = _SlowFetch(5.0)
        conv = _WaitingConverter(self, on_wait=on_wait)
        t0 = time.monotonic()
        with self.assertRaises(ta.ConverterCancelled):
            self.run_(fetch, conv, should_cancel=stop.is_set,
                      on_start=procs.append)
        self.assertLess(time.monotonic() - t0, 4.0, "Stop was not prompt")
        self.assertTrue(fetch.cancelled)
        (job,) = conv.jobs
        self.assertEqual(job["buildings_pbf_list"], self.list)
        self.assertEqual(len(conv.killed), 1)
        self.assertFalse(os.path.lexists(self.list))
        self.assertFalse(os.path.exists(os.path.join(self.pool, TILE_A)))
        self.assertNotIn("ERROR", self.text())

    def test_list_mode_no_tile_with_data_lists_none(self):
        self.list_mode()
        conv = _WaitingConverter(self, burn=DOWNLOAD_ZERO)
        _view, err = self.run_(_SlowFetch(0.3, data=lambda _n: False), conv)
        self.assertIsNone(err)
        self.assertEqual(conv.read, [[]])
        self.assertEqual(read_building_list(self.list), [])
        text = self.text()
        self.assertEqual(text.count("WARNING"), 1, text)
        self.assertIn("WARNING: buildings: none of the", text)
        self.assertTrue(ta._tile_ready(self.pool, TILE_A))

    def test_0213_still_takes_the_view_and_its_marker(self):
        # The gate: buildings_pbf_list from 0.2.14 only.
        self.converter_version = "0.2.13"
        conv = _WaitingConverter(self)
        _view, err = self.run_(_SlowFetch(0.3), conv)
        self.assertIsNone(err)
        (job,) = conv.jobs
        self.assertEqual(job["buildings_pbf_ready"], "_ready")
        self.assertEqual(job["buildings_pbf_dir"], os.path.abspath(self.view))
        self.assertNotIn("buildings_pbf_list", job)
        self.assertFalse(os.path.lexists(ta.osm_buildings_list_dir()))


class _BurningEngine(_RawEngine):
    """``test_raw_tile_cache``'s download engine (*mode* ``"gap"``: a retry
    pass follows; ``"degraded"``: a repair pass follows) whose non-repair
    passes also report their burn (a download pass owes one)."""

    def __call__(self, exe, args, on_line, on_start=None, env=None):
        with open(args[-1], encoding="utf-8") as fh:
            job = json.load(fh)
        burn = args[0] == "download" and not job.get("repair_source_tiles")

        def say(line):
            if burn and line.startswith("[Stats] Tiles:"):
                on_line(DOWNLOAD_DRAWN)
            on_line(line)

        return super().__call__(exe, args, say, on_start, env)


class DownloadPassesCarryTheList(unittest.TestCase):
    """``_try_rust_download(pbf_list=)`` (aether_converter >= 0.2.14,
    toolkit CONTRACT changelog 51): EVERY pass — the first, a retry, a
    repair (which ignores it) — carries ``buildings_pbf_list`` (absolute)
    and ``buildings_pbf_wait_s`` next to ``buildings_pbf_dir``, never
    ``buildings_pbf_ready``; ``pbf_ready`` keeps the marker fields; both at
    once is refused before anything runs."""

    def setUp(self):
        self.cache = _scratch(self)

    def download(self, mode, **fields):
        specs = [t for _r, t in _mc_tiles()[0]]
        engine = _BurningEngine(mode=mode)
        with mock.patch.object(ta, "get_cache_dir", return_value=self.cache), \
             mock.patch.object(ta, "run_converter_streaming",
                               side_effect=engine), \
             mock.patch.object(ta, "_abt_has_holes", return_value=False), \
             mock.patch.object(ta, "_abt_has_zero_fill", return_value=False), \
             mock.patch.object(ta, "_binary_has_buildings_support",
                               return_value=True), \
             mock.patch.object(bmod, "probe_converter_version",
                               return_value="0.2.14"):
            store = ta.osm_buildings_store_dir()
            outcome = ta._try_rust_download(
                _RAW_XYZ, ta._pool_dir(_RAW_XYZ), {}, 30, specs, _raw_bm(),
                pbf_dir=store, **fields)
        self.assertTrue(outcome, getattr(outcome, "detail", None))
        return store, [rec["job"] for rec in engine.downloads]

    def test_every_pass_carries_the_list_never_the_marker(self):
        path = os.path.join(self.cache, "views", "1" * 32, "2" * 32 + ".txt")
        for mode in ("gap", "degraded"):
            store, jobs = self.download(mode, pbf_list=path,
                                        pbf_wait_s=14400)
            self.assertEqual(len(jobs), 2, mode)  # + the retry / repair pass
            self.assertEqual(bool(jobs[1].get("repair_source_tiles")),
                             mode == "degraded")
            for job in jobs:
                self.assertEqual(job["buildings_pbf_dir"],
                                 os.path.abspath(store))
                self.assertEqual(job["buildings_pbf_list"], path)
                self.assertEqual(job["buildings_pbf_wait_s"], 14400)
                self.assertNotIn("buildings_pbf_ready", job)

    def test_the_marker_fields_stay_without_a_list(self):
        _store, jobs = self.download("degraded", pbf_ready="_ready",
                                     pbf_wait_s=14400)
        self.assertEqual(len(jobs), 2)
        for job in jobs:
            self.assertEqual((job["buildings_pbf_ready"],
                              job["buildings_pbf_wait_s"]), ("_ready", 14400))
            self.assertNotIn("buildings_pbf_list", job)

    def test_both_are_refused(self):
        with mock.patch.object(ta, "run_converter_streaming") as run, \
                self.assertRaises(ValueError):
            ta._try_rust_download(_RAW_XYZ, self.cache, {}, 30, [], _raw_bm(),
                                  pbf_dir="/s", pbf_ready="_ready",
                                  pbf_list="/s.txt")
        run.assert_not_called()


# ---------------------------------------------------------------------------
# The fetch next to the conversion (aether_converter >= 0.2.12)
# ---------------------------------------------------------------------------

#: The 0.2.11 ingest's report for an empty buildings_pbf_dir (probed,
#: 2026-10-25) — what a view without any tile with data gets.
INGEST_ZERO = "[Info] buildings_pbf_dir: 0 tile(s), no buildings in extent"


def _every_tile(_name):
    return True


class _WaitingIngest:
    """``run_converter_streaming`` for ``ingest`` as aether_converter 0.2.12
    runs a job file whose jobs carry ``buildings_pbf_ready`` (CONTRACT item
    48), over a REAL process (a sleeper handed to on_start: what Stop and a
    failed fetch kill): its terrain work starts at once (nothing to do
    here), then — a marker not there — the wait line ONCE (*on_wait(proc)*),
    the markers polled every 20 ms, the ready line; the view read ONCE
    (:attr:`read`: its tile names), *before* lines, per job its *report*,
    the tile written whole and its ``[Tile]`` line. Jobs without the field
    (an older converter) read the view at the start. Killed while waiting:
    its exit code, nothing written. With ``buildings_pbf_list`` (0.2.14,
    CONTRACT changelog 51) the list is the marker; the tiles it names are
    read from ``buildings_pbf_dir`` (never a listing of it) after the 0.2.14
    decode header; a listed name not there → exit 1, nothing written
    (:attr:`errors`)."""

    def __init__(self, test, report=INGEST_DRAWN, before=(), on_wait=None):
        self.test = test
        self.report = report
        self.before = list(before)
        self.on_wait = on_wait
        self.jobs, self.launched, self.read = [], [], []
        self.marker_at_launch, self.killed, self.errors = [], [], []
        self.dir_at_launch = []

    def __call__(self, exe, args, on_line, on_start=None, env=None):
        with open(args[args.index("--job-file") + 1], encoding="utf-8") as fh:
            jobs = json.load(fh)
        self.jobs.extend(jobs)
        self.launched.append(time.monotonic())
        if any(j.get("buildings_pbf_list") and j.get("buildings_pbf_ready")
               for j in jobs):
            self.errors.append("buildings_pbf_ready with buildings_pbf_list")
            return 1
        views = sorted({j["buildings_pbf_dir"] for j in jobs
                        if j.get("buildings_pbf_dir")
                        and not j.get("buildings_pbf_list")})
        lists = sorted({(j["buildings_pbf_dir"], j["buildings_pbf_list"])
                        for j in jobs if j.get("buildings_pbf_list")})
        markers = sorted({os.path.join(j["buildings_pbf_dir"],
                                       j["buildings_pbf_ready"])
                          for j in jobs if j.get("buildings_pbf_ready")}
                         | {p for _d, p in lists})
        self.marker_at_launch.append(any(os.path.lexists(m)
                                         for m in markers))
        self.dir_at_launch.append(all(
            os.path.isdir(j["buildings_pbf_dir"]) for j in jobs
            if j.get("buildings_pbf_dir")))
        proc = _sleeper(self.test)
        if on_start is not None:
            on_start(proc)
        try:
            on_line(f"[Rust] Batch processing {len(jobs)} tiles...")
            missing = [m for m in markers if not os.path.isfile(m)]
            if missing:
                on_line(f"[Buildings] waiting for the building tiles "
                        f"({missing[0]})")
                if self.on_wait is not None:
                    self.on_wait(proc)
                t0 = time.monotonic()
                while not all(os.path.isfile(m) for m in markers):
                    if proc.poll() is not None:
                        self.killed.append(proc.returncode)
                        return proc.returncode
                    time.sleep(0.02)
                on_line(f"[Buildings] building tiles ready after "
                        f"{time.monotonic() - t0:.1f} s")
            for d in views:
                self.read.append(sorted(n for n in os.listdir(d)
                                        if n.endswith(".pbf")))
            for d, p in lists:
                names = read_building_list(p)
                gone = [n for n in names
                        if not os.path.isfile(os.path.join(d, n))]
                if gone:
                    self.errors.append(gone[0])
                    on_line(f"Error: Building tile {gone[0]} listed in {p} "
                            f"is not in {d}")
                    return 1
                on_line(f'[Rust] Decoding {len(names)} listed building tiles '
                        f'from "{d}" (once for the run)...')
                self.read.append(sorted(names))
            for line in self.before:
                on_line(line)
            for job in jobs:
                if job.get("buildings_pbf_dir"):
                    on_line(self.report)
                _write_tile(job)
                on_line(f"[Tile] {os.path.basename(job['output_path'])} "
                        f"covered=1/1")
            on_line(f"[Rust] Progress: {len(jobs)}/{len(jobs)}")
            return 0
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


def _region_cover_names():
    """The z14 tiles of the REGION of the 30 m pool tile SITE's 1 km folder
    run builds (a file source builds only the part of the tile it needs —
    ``terrain_adapter.tile_regions`` — and covers only that)."""
    bbox = ta._compute_sector_bbox(*SITE, 1.0, 0.0, 360.0)
    (tile,) = ta._compute_subtiles(bbox, 30, *SITE, 1.0, 0.0, 360.0)
    region = ta.tile_regions([tile], bbox, 30)[tile["filename"]]
    return [ofm.tile_filename(*t)
            for t in ob.BuildingCover([region]).needed()]


#: The keys of an ingest job of the sequential path (folder source with
#: OpenFreeMap buildings) — the older converter's job, unchanged.
_SEQUENTIAL_JOB_KEYS = {"output_path", "format", "ul_lat", "ul_lon",
                        "resolution_m", "size_px", "void_fill_m", "sources",
                        "buildings_pbf_dir"}


class BesideTheConversion(_SaCase):
    """The fetch runs WHILE the converter ingests (converter >= 0.2.12):
    the folder route, and a rendered source."""

    converter_version = "0.2.12"

    def setUp(self):
        super().setUp()
        self.dem = os.path.join(self.cache, "dem")
        os.makedirs(self.dem)
        open(os.path.join(self.dem, "covering.tif"), "wb").close()
        covering = {"west": 0.0, "east": 20.0, "south": 40.0, "north": 50.0}
        probe = mock.patch.object(ta.terrain_index, "probe_file",
                                  side_effect=lambda *_a: _fake_probe(
                                      covering))
        probe.start()
        self.addCleanup(probe.stop)
        self.pool = ta._pool_dir(self.dem, ta.buildings_identity(
            osm_buildings=True))
        self.names = _region_cover_names()
        self.view = ta.osm_buildings_view_dir(self.names,
                                              marker=ob.READY_MARKER)
        self.marker = os.path.join(self.view, ob.READY_MARKER)
        self.part = os.path.join(self.pool, TILE_A + ta._PART_SUFFIX)

    def run_(self, fetch, conv, should_cancel=None, on_start=None,
             source=None):
        rendered = source is not None
        with mock.patch.object(ofm, "fetch_building_tiles",
                               side_effect=fetch), \
             mock.patch.object(ta, "run_converter_streaming",
                               side_effect=conv), \
             mock.patch.object(ta, "_export_via_qgis",
                               side_effect=_fake_export):
            try:
                view = ta.prepare_terrain(
                    self.layer(source if rendered else self.dem), *SITE,
                    1.0, 30, self.bm,
                    terrain_dir=None if rendered else self.dem,
                    osm_buildings=True, should_cancel=should_cancel,
                    on_start=on_start)
                return view, None
            except RuntimeError as exc:
                return None, str(exc)
            finally:
                self.assertFalse(
                    any(t.name == "waveshed-buildings" and t.is_alive()
                        for t in threading.enumerate()),
                    "the fetch thread outlived the run")

    def pooled(self, pool=None):
        pool = pool or self.pool
        try:
            return sorted(n for n in os.listdir(pool)
                          if n.endswith((".abt", ".abt.part")))
        except OSError:
            return []

    def scratch_left(self):
        try:
            return [n for n in os.listdir(self.pool)
                    if n.startswith(".incoming-")]
        except OSError:
            return []

    def test_the_converter_runs_while_the_tiles_are_fetched(self):
        fetch = _SlowFetch(1.0, data=_every_tile)
        conv = _WaitingIngest(self)
        links = []
        real_link = os.link

        def link(src, dst, *a, **kw):
            if dst.endswith(".pbf"):        # not the terrain view's .abt
                links.append((os.path.basename(dst), os.path.lexists(
                    os.path.join(os.path.dirname(dst), ob.READY_MARKER))))
            return real_link(src, dst, *a, **kw)

        with mock.patch.object(ta.os, "link", side_effect=link):
            view, err = self.run_(fetch, conv)
        self.assertIsNone(err)
        # The cover is the region's (a file source), fetched once.
        self.assertEqual([[ofm.tile_filename(*t) for t in c]
                          for c in fetch.calls], [self.names])
        # Launched at once — long before the 1 s fetch ended.
        (launched,) = conv.launched
        self.assertLess(launched, fetch.ended - 0.5)
        (job,) = conv.jobs
        self.assertEqual(set(job), _SEQUENTIAL_JOB_KEYS
                         | {"buildings_pbf_ready", "buildings_pbf_wait_s"})
        self.assertEqual(job["buildings_pbf_dir"], os.path.abspath(self.view))
        self.assertEqual(job["buildings_pbf_ready"], "_ready")
        self.assertEqual(job["buildings_pbf_wait_s"], 14400)
        self.assertEqual(conv.marker_at_launch, [False])
        # Every tile linked before the marker; the converter read them all.
        self.assertEqual(sorted(n for n, _seen in links), sorted(self.names))
        self.assertFalse(any(seen for _n, seen in links), links)
        self.assertTrue(os.path.isfile(self.marker))
        self.assertEqual(conv.read, [sorted(self.names)])
        text = self.text()
        self.assertIn(f"  buildings: fetched next to the conversion "
                      f"(aether_converter >= 0.2.12 waits for {self.marker})",
                      self.logged)
        self.assertIn("    [Buildings] waiting for the building tiles (", text)
        self.assertIn(f"  buildings: {len(self.names):,} OpenFreeMap tile(s) "
                      f"linked into {self.view}, _ready written — the "
                      f"conversion burns them now", self.logged)
        timing = [m for m in self.logged
                  if m.startswith("  buildings fetched next to the ")]
        self.assertEqual(len(timing), 1, timing)
        m = re.fullmatch(r"  buildings fetched next to the conversion: fetch "
                         r"(\d+\.\d) s, converter (\d+\.\d) s, converter "
                         r"waited (\d+\.\d) s", timing[0])
        self.assertIsNotNone(m, timing[0])
        self.assertGreaterEqual(float(m.group(1)), 1.0)
        self.assertGreater(float(m.group(3)), 0.3)  # the ready line's figure
        self.assertIn("  buildings: 12 building(s) decoded, 12 building(s) "
                      "drawn, 3,835 px raised", text)
        self.assertNotIn("WARNING", text)
        self.assertEqual(self.pooled(), [TILE_A + ta._PART_SUFFIX])
        self.assertEqual(self.scratch_left(), [])
        self.assertTrue(os.path.isdir(view))
        # Pooled with its buildings: the next run is a cache HIT.
        fetch2, conv2 = _SlowFetch(0.0), _WaitingIngest(self)
        _view, err2 = self.run_(fetch2, conv2)
        self.assertIsNone(err2)
        self.assertEqual((fetch2.calls, conv2.jobs), ([], []))

    def test_a_stale_marker_is_removed_before_the_launch(self):
        os.makedirs(self.view)
        for stale in (ob.READY_MARKER, "14_1_1.pbf"):
            with open(os.path.join(self.view, stale), "w") as fh:
                fh.write("old")
        conv = _WaitingIngest(self)
        _view, err = self.run_(_SlowFetch(0.3, data=_every_tile), conv)
        self.assertIsNone(err)
        self.assertEqual(conv.marker_at_launch, [False])
        self.assertEqual(conv.read, [sorted(self.names)])  # no stale tile

    def test_an_older_converter_fetches_first_as_before(self):
        # The sequential path, job for job: the fetch first, then ONE ingest
        # with the batch view of exactly the tiles with data and no marker
        # field; no line of the new route.
        for version in ("0.2.11", "0.2.10", None):
            self.converter_version = version
            self.logged.clear()
            fetch = _SlowFetch(0.3, data=_every_tile)
            conv = _WaitingIngest(self)
            _view, err = self.run_(fetch, conv)
            self.assertIsNone(err, version)
            self.assertEqual([[ofm.tile_filename(*t) for t in c]
                              for c in fetch.calls], [self.names], version)
            (job,) = conv.jobs
            self.assertEqual(set(job), _SEQUENTIAL_JOB_KEYS, version)
            self.assertEqual(job["buildings_pbf_dir"], os.path.abspath(
                ta.osm_buildings_view_dir(sorted(self.names))))
            self.assertGreater(conv.launched[0], fetch.ended)
            self.assertEqual(conv.read, [sorted(self.names)])
            self.assertFalse(os.path.lexists(self.marker))
            text = self.text()
            self.assertNotIn("fetched next to the conversion", text)
            self.assertNotIn("_ready", text)
            self.assertNotIn("[Buildings] waiting", text)
            self.assertEqual(self.pooled(), [TILE_A + ta._PART_SUFFIX])
            shutil.rmtree(self.pool)                  # rebuilt next round

    def test_a_failed_fetch_kills_the_converter_and_fails_the_run(self):
        cause = "14_8555_5755.pbf: timed out (after 4 attempts)"
        fetch = _SlowFetch(1.0, fail=cause)
        conv = _WaitingIngest(self)
        procs = []
        view, err = self.run_(fetch, conv, on_start=procs.append)
        self.assertIsNone(view)
        self.assertEqual(err.split("\n")[0],
                         f"Buildings download failed (OpenFreeMap z14): 3 of "
                         f"{len(self.names):,} building tiles missing — tile "
                         f"{cause}.")
        self.assertIn("untick 'Include buildings'", err)
        # It was waiting, and was killed — by the fetch, not by Stop.
        self.assertEqual(len(conv.killed), 1)
        self.assertEqual(len(procs), 1)
        self.assertIsNotNone(procs[0].poll())
        # Nothing pooled, no scratch left, no marker.
        self.assertEqual(self.pooled(), [])
        self.assertEqual(self.scratch_left(), [])
        self.assertFalse(os.path.lexists(self.marker))
        text = self.text()
        self.assertIn("  ERROR: Buildings download failed (OpenFreeMap z14): "
                      "3 of", text)
        self.assertNotIn("buildings fetched next to the conversion", text)
        # The next run fetches again and builds the tile.
        _view, err2 = self.run_(_SlowFetch(0.2, data=_every_tile),
                                _WaitingIngest(self))
        self.assertIsNone(err2)
        self.assertEqual(self.pooled(), [TILE_A + ta._PART_SUFFIX])

    def test_a_fetch_failed_before_the_launch_launches_nothing(self):
        cause = "14_8555_5755.pbf: HTTP 503 (after 4 attempts)"
        conv = _WaitingIngest(self)
        real_preflight = ta.preflight_ingest_jobs

        def preflight(jobs):
            # The job build outlasts the fetch: it has failed by now.
            self.assertTrue(_wait_for(lambda: not any(
                t.name == "waveshed-buildings" and t.is_alive()
                for t in threading.enumerate())))
            return real_preflight(jobs)

        with mock.patch.object(ta, "preflight_ingest_jobs",
                               side_effect=preflight):
            view, err = self.run_(_SlowFetch(0.0, fail=cause), conv)
        self.assertIsNone(view)
        self.assertTrue(err.startswith("Buildings download failed "
                                       "(OpenFreeMap z14): 3 of "), err)
        self.assertEqual(conv.jobs, [])
        self.assertEqual(self.pooled(), [])
        self.assertEqual(self.scratch_left(), [])

    def _stop_case(self, kill_on_stop):
        stop = threading.Event()
        procs = []

        def on_wait(proc):
            stop.set()                     # Stop, while the converter waits
            if kill_on_stop:               # the tab's worker kills its proc
                ta.kill_engine_process(procs[-1])

        fetch = _SlowFetch(5.0)
        conv = _WaitingIngest(self, on_wait=on_wait)
        t0 = time.monotonic()
        with self.assertRaises(ta.ConverterCancelled):
            self.run_(fetch, conv, should_cancel=stop.is_set,
                      on_start=procs.append if kill_on_stop else None)
        self.assertLess(time.monotonic() - t0, 4.0, "Stop was not prompt")
        self.assertTrue(fetch.cancelled)
        self.assertEqual(len(conv.killed), 1)
        self.assertEqual(self.pooled(), [])
        self.assertEqual(self.scratch_left(), [])
        self.assertFalse(os.path.lexists(self.marker))
        self.assertNotIn("ERROR", self.text())

    def test_stop_during_the_wait_is_quiet_and_cleans_up(self):
        self._stop_case(kill_on_stop=True)

    def test_stop_without_an_on_start_hook_still_ends_the_wait(self):
        # A caller with should_cancel only: the fetch, stopped, kills the
        # converter it was holding back (it prints nothing while waiting).
        self._stop_case(kill_on_stop=False)

    def test_no_tile_with_data_still_releases_the_conversion(self):
        conv = _WaitingIngest(self, report=INGEST_ZERO)
        _view, err = self.run_(_SlowFetch(0.3, data=lambda _n: False), conv)
        self.assertIsNone(err)
        self.assertEqual(conv.read, [[]])
        self.assertTrue(os.path.isfile(self.marker))
        text = self.text()
        self.assertEqual(text.count("WARNING"), 1, text)
        self.assertIn("WARNING: buildings: none of the", text)
        self.assertEqual(self.pooled(), [TILE_A + ta._PART_SUFFIX])

    def test_an_undecodable_tile_is_still_the_burns_verdict(self):
        bad = self.names[0]
        conv = _WaitingIngest(self, before=[_warn(bad)])
        view, err = self.run_(_SlowFetch(0.3, data=_every_tile), conv)
        self.assertIsNone(view)
        self.assertIn(f"the converter could not decode 1 OpenFreeMap tile(s) "
                      f"(first: {bad}: PBF decode error", err)
        self.assertIn("Nothing was added to the terrain cache", err)
        self.assertNotIn(bad, self.store_names())
        self.assertNotIn(bad, os.listdir(self.view))
        self.assertEqual(self.pooled(), [])
        self.assertEqual(self.scratch_left(), [])
        (job,) = conv.jobs
        self.assertFalse(os.path.exists(job["output_path"]),
                         "a tile was written after the warning")

    def test_a_rendered_source_converts_beside_its_fetch_too(self):
        # The same ingest route: whole pool tiles, the whole tile's cover.
        names, _with_data = _whole_cover_names()
        fetch = _SlowFetch(0.8, data=_every_tile)
        conv = _WaitingIngest(self)
        view, err = self.run_(fetch, conv, source=_WMS)
        self.assertIsNone(err)
        self.assertLess(conv.launched[0], fetch.ended - 0.3)
        (job,) = conv.jobs
        self.assertEqual(job["buildings_pbf_ready"], "_ready")
        self.assertEqual(job["buildings_pbf_dir"], os.path.abspath(
            ta.osm_buildings_view_dir(names, marker=ob.READY_MARKER)))
        self.assertEqual(conv.read, [sorted(names)])
        pool = ta._pool_dir(_WMS, ta.buildings_identity(osm_buildings=True))
        self.assertEqual(self.pooled(pool), [TILE_A])
        self.assertTrue(ta._tile_ready(pool, TILE_A))
        self.assertIn("  buildings fetched next to the conversion: fetch ",
                      self.text())

    # -- aether_converter >= 0.2.14: the list, no view (changelog 51) -------
    def list_mode(self, names=None):
        self.converter_version = "0.2.14"
        self.list = ta.osm_buildings_list_path(names or self.names)
        self.store = ta.osm_buildings_store_dir()

    def views(self):
        views = os.path.join(self.cache, "views")
        return set(os.listdir(views)) if os.path.isdir(views) else set()

    def test_list_mode_the_ingest_reads_the_store_through_its_list(self):
        self.list_mode()
        fetch = _SlowFetch(1.0, data=_every_tile)
        conv = _WaitingIngest(self)
        links, writes = [], []
        real_link, real_replace = os.link, ta.replace_with_retry

        def link(src, dst, *a, **kw):
            if dst.endswith(".pbf"):        # not the terrain view's .abt
                links.append(dst)
            return real_link(src, dst, *a, **kw)

        def replace(src, dst, *a, **kw):
            if dst == self.list:            # atomic: a whole temp renamed
                with open(src, encoding="utf-8") as fh:
                    writes.append((os.path.dirname(src)
                                   == os.path.dirname(dst),
                                   os.path.lexists(dst), fh.read()))
            return real_replace(src, dst, *a, **kw)

        with mock.patch.object(ta.os, "link", side_effect=link), \
             mock.patch.object(ta, "replace_with_retry", side_effect=replace):
            view, err = self.run_(fetch, conv)
        self.assertIsNone(err)
        # The cover is the region's (a file source), fetched once.
        self.assertEqual([[ofm.tile_filename(*t) for t in c]
                          for c in fetch.calls], [self.names])
        (launched,) = conv.launched
        self.assertLess(launched, fetch.ended - 0.5)
        (job,) = conv.jobs
        self.assertEqual(set(job), _SEQUENTIAL_JOB_KEYS
                         | {"buildings_pbf_list", "buildings_pbf_wait_s"})
        self.assertEqual(job["buildings_pbf_dir"], os.path.abspath(self.store))
        self.assertEqual(job["buildings_pbf_list"], self.list)
        self.assertTrue(self.list.startswith(os.path.join(self.cache, "views")
                                             + os.sep), self.list)
        self.assertEqual(job["buildings_pbf_wait_s"], 14400)
        self.assertEqual(conv.marker_at_launch, [False])
        self.assertEqual(conv.dir_at_launch, [True])      # THE store
        self.assertEqual(conv.errors, [])
        self.assertEqual(writes, [(True, False, "".join(
            f"{n}\n" for n in sorted(self.names)))])
        self.assertEqual(conv.read, [sorted(self.names)])
        self.assertEqual(links, [])
        self.assertEqual(self.views(), {os.path.basename(view),
                                        os.path.basename(
                                            os.path.dirname(self.list))})
        text = self.text()
        self.assertIn(f"  buildings: fetched next to the conversion "
                      f"(aether_converter >= 0.2.14 waits for {self.list})",
                      self.logged)
        self.assertIn(f"    [Buildings] waiting for the building tiles "
                      f"({self.list})", self.logged)
        self.assertIn(f'    [Rust] Decoding {len(self.names)} listed building '
                      f'tiles from "{os.path.abspath(self.store)}" (once for '
                      f'the run)...', self.logged)
        self.assertIn(f"  buildings: {len(self.names):,} OpenFreeMap tile(s) "
                      f"listed for the converter in {self.list} — the "
                      f"conversion burns them now", self.logged)
        timing = [m for m in self.logged
                  if m.startswith("  buildings fetched next to the ")]
        self.assertEqual(len(timing), 1, timing)
        m = re.fullmatch(r"  buildings fetched next to the conversion: fetch "
                         r"(\d+\.\d) s, converter (\d+\.\d) s, converter "
                         r"waited (\d+\.\d) s", timing[0])
        self.assertIsNotNone(m, timing[0])
        self.assertGreater(float(m.group(3)), 0.3)  # the ready line's figure
        self.assertIn("  buildings: 12 building(s) decoded, 12 building(s) "
                      "drawn, 3,835 px raised", text)
        self.assertNotIn("_ready", text)
        self.assertNotIn("linked into", text)
        self.assertNotIn("WARNING", text)
        self.assertEqual(self.pooled(), [TILE_A + ta._PART_SUFFIX])
        self.assertEqual(self.scratch_left(), [])
        # Pooled with its buildings: the next run is a cache HIT.
        fetch2, conv2 = _SlowFetch(0.0), _WaitingIngest(self)
        _view, err2 = self.run_(fetch2, conv2)
        self.assertIsNone(err2)
        self.assertEqual((fetch2.calls, conv2.jobs), ([], []))

    def test_list_mode_a_stale_list_is_removed_before_the_launch(self):
        self.list_mode()
        os.makedirs(os.path.dirname(self.list))
        with open(self.list, "w", encoding="utf-8") as fh:
            fh.write("14_1_1.pbf\n")      # an earlier run's: not in the store
        conv = _WaitingIngest(self)
        _view, err = self.run_(_SlowFetch(0.3, data=_every_tile), conv)
        self.assertIsNone(err)
        (job,) = conv.jobs
        self.assertEqual(job["buildings_pbf_list"], self.list)
        self.assertEqual(conv.marker_at_launch, [False])
        self.assertEqual(conv.errors, [])
        self.assertEqual(conv.read, [sorted(self.names)])
        self.assertEqual(read_building_list(self.list), sorted(self.names))

    def test_list_mode_a_list_that_cannot_be_removed_takes_the_view(self):
        self.list_mode()
        os.makedirs(self.list)            # os.remove refuses a directory
        conv = _WaitingIngest(self)
        _view, err = self.run_(_SlowFetch(0.3, data=_every_tile), conv)
        self.assertIsNone(err)
        (job,) = conv.jobs
        self.assertEqual(job["buildings_pbf_dir"], os.path.abspath(self.view))
        self.assertEqual(job["buildings_pbf_ready"], "_ready")
        self.assertNotIn("buildings_pbf_list", job)
        self.assertTrue(os.path.isfile(self.marker))
        self.assertIn(f"  WARNING: buildings: {self.list} (left by an earlier "
                      f"run) could not be removed — the building tiles are "
                      f"linked into a view for the conversion instead",
                      self.logged)

    def test_list_mode_a_failed_fetch_kills_the_converter_no_list(self):
        self.list_mode()
        cause = "14_8555_5755.pbf: timed out (after 4 attempts)"
        conv = _WaitingIngest(self)
        procs = []
        view, err = self.run_(_SlowFetch(1.0, fail=cause), conv,
                              on_start=procs.append)
        self.assertIsNone(view)
        self.assertEqual(err.split("\n")[0],
                         f"Buildings download failed (OpenFreeMap z14): 3 of "
                         f"{len(self.names):,} building tiles missing — tile "
                         f"{cause}.")
        (job,) = conv.jobs
        self.assertEqual(job["buildings_pbf_list"], self.list)
        self.assertEqual(len(conv.killed), 1)
        self.assertIsNotNone(procs[0].poll())
        self.assertFalse(os.path.lexists(self.list))
        self.assertEqual(self.pooled(), [])
        self.assertEqual(self.scratch_left(), [])

    def test_list_mode_a_fetch_failed_before_the_launch_launches_nothing(
            self):
        self.list_mode()
        cause = "14_8555_5755.pbf: HTTP 503 (after 4 attempts)"
        conv = _WaitingIngest(self)
        real_preflight = ta.preflight_ingest_jobs

        def preflight(jobs):
            self.assertTrue(_wait_for(lambda: not any(
                t.name == "waveshed-buildings" and t.is_alive()
                for t in threading.enumerate())))
            return real_preflight(jobs)

        with mock.patch.object(ta, "preflight_ingest_jobs",
                               side_effect=preflight):
            view, err = self.run_(_SlowFetch(0.0, fail=cause), conv)
        self.assertIsNone(view)
        self.assertTrue(err.startswith("Buildings download failed "
                                       "(OpenFreeMap z14): 3 of "), err)
        self.assertEqual(conv.jobs, [])
        self.assertIn(f"  buildings: fetched next to the conversion "
                      f"(aether_converter >= 0.2.14 waits for {self.list})",
                      self.logged)
        self.assertFalse(os.path.lexists(self.list))
        self.assertEqual(self.pooled(), [])

    def test_list_mode_stop_during_the_wait_is_quiet_no_list(self):
        self.list_mode()
        stop = threading.Event()
        procs = []

        def on_wait(proc):
            stop.set()                     # Stop, while the converter waits
            ta.kill_engine_process(procs[-1])

        fetch = _SlowFetch(5.0)
        conv = _WaitingIngest(self, on_wait=on_wait)
        t0 = time.monotonic()
        with self.assertRaises(ta.ConverterCancelled):
            self.run_(fetch, conv, should_cancel=stop.is_set,
                      on_start=procs.append)
        self.assertLess(time.monotonic() - t0, 4.0, "Stop was not prompt")
        self.assertTrue(fetch.cancelled)
        (job,) = conv.jobs
        self.assertEqual(job["buildings_pbf_list"], self.list)
        self.assertEqual(len(conv.killed), 1)
        self.assertFalse(os.path.lexists(self.list))
        self.assertEqual(self.pooled(), [])
        self.assertEqual(self.scratch_left(), [])
        self.assertNotIn("ERROR", self.text())

    def test_list_mode_no_tile_with_data_lists_none(self):
        self.list_mode()
        conv = _WaitingIngest(self, report=INGEST_ZERO)
        _view, err = self.run_(_SlowFetch(0.3, data=lambda _n: False), conv)
        self.assertIsNone(err)
        self.assertEqual(conv.read, [[]])
        self.assertEqual(read_building_list(self.list), [])
        text = self.text()
        self.assertEqual(text.count("WARNING"), 1, text)
        self.assertIn("WARNING: buildings: none of the", text)
        self.assertEqual(self.pooled(), [TILE_A + ta._PART_SUFFIX])

    def test_list_mode_an_undecodable_tile_is_still_the_burns_verdict(self):
        self.list_mode()
        bad = self.names[0]
        conv = _WaitingIngest(self, before=[_warn(bad)])
        view, err = self.run_(_SlowFetch(0.3, data=_every_tile), conv)
        self.assertIsNone(view)
        self.assertIn(f"the converter could not decode 1 OpenFreeMap tile(s) "
                      f"(first: {bad}: PBF decode error", err)
        self.assertNotIn(bad, self.store_names())       # fetched anew next
        self.assertEqual(self.pooled(), [])
        (job,) = conv.jobs
        self.assertEqual(job["buildings_pbf_list"], self.list)
        self.assertFalse(os.path.exists(job["output_path"]),
                         "a tile was written after the warning")

    def test_list_mode_a_rendered_source_converts_beside_its_list_too(self):
        names, _with_data = _whole_cover_names()
        self.list_mode(names)
        fetch = _SlowFetch(0.8, data=_every_tile)
        conv = _WaitingIngest(self)
        view, err = self.run_(fetch, conv, source=_WMS)
        self.assertIsNone(err)
        self.assertLess(conv.launched[0], fetch.ended - 0.3)
        (job,) = conv.jobs
        self.assertEqual(job["buildings_pbf_list"], self.list)
        self.assertEqual(job["buildings_pbf_dir"], os.path.abspath(self.store))
        self.assertNotIn("buildings_pbf_ready", job)
        self.assertEqual(conv.read, [sorted(names)])
        pool = ta._pool_dir(_WMS, ta.buildings_identity(osm_buildings=True))
        self.assertEqual(self.pooled(pool), [TILE_A])

    def test_0213_still_takes_the_view_and_its_marker(self):
        # The gate: buildings_pbf_list from 0.2.14 only.
        self.converter_version = "0.2.13"
        conv = _WaitingIngest(self)
        _view, err = self.run_(_SlowFetch(0.3, data=_every_tile), conv)
        self.assertIsNone(err)
        (job,) = conv.jobs
        self.assertEqual(set(job), _SEQUENTIAL_JOB_KEYS
                         | {"buildings_pbf_ready", "buildings_pbf_wait_s"})
        self.assertEqual(job["buildings_pbf_dir"], os.path.abspath(self.view))
        self.assertFalse(os.path.lexists(ta.osm_buildings_list_dir()))


# ---------------------------------------------------------------------------
# The ingest route (a rendered / file source): the jobs carry the view
# ---------------------------------------------------------------------------

class _Ingest:
    """``run_converter_streaming`` for ``ingest``: *before* lines, then per
    job its *report* (when it carries the field), the tile written whole and
    its ``[Tile]`` line."""

    def __init__(self, report, before=()):
        self.report = report
        self.before = list(before)
        self.jobs = []

    def __call__(self, exe, args, on_line, on_start=None, env=None):
        with open(args[-1], encoding="utf-8") as fh:
            jobs = json.load(fh)
        self.jobs.extend(jobs)
        for line in self.before:
            on_line(line)
        for job in jobs:
            if job.get("buildings_pbf_dir"):
                on_line(self.report)
            _write_tile(job)
            on_line(f"[Tile] {os.path.basename(job['output_path'])} "
                    f"covered=1/1")
        return 0


def _fake_export(dem_layer, dest, bbox, resolution_m):
    with open(dest, "wb") as fh:
        fh.write(b"\0" * 8)


class IngestRoute(_SaCase):

    def run_(self, converter):
        self.pool = ta._pool_dir(_WMS, ta.buildings_identity(
            osm_buildings=True))
        with mock.patch.object(ofm, "fetch_building_tiles",
                               side_effect=_Fetch()), \
             mock.patch.object(ta, "_export_via_qgis",
                               side_effect=_fake_export), \
             mock.patch.object(ta, "run_converter_streaming",
                               side_effect=converter):
            try:
                return ta.prepare_terrain(self.layer(_WMS), *SITE, 1.0, 30,
                                          self.bm, osm_buildings=True), None
            except RuntimeError as exc:
                return None, str(exc)

    def pooled(self):
        try:
            return sorted(n for n in os.listdir(self.pool)
                          if n.endswith(".abt"))
        except OSError:
            return []

    def test_the_jobs_carry_the_view_and_the_burn_is_read(self):
        conv = _Ingest(INGEST_DRAWN, before=[DECODING])
        view, err = self.run_(conv)
        self.assertIsNone(err)
        (job,) = conv.jobs
        self.assertTrue(job["buildings_pbf_dir"].startswith(
            os.path.join(self.cache, "views")))
        self.assertEqual(self.pooled(), [TILE_A])
        self.assertIn("  buildings: 12 building(s) decoded, 12 building(s) "
                      "drawn, 3,835 px raised", self.text())

    def test_none_drawn_places_nothing_in_the_pool(self):
        view, err = self.run_(_Ingest(INGEST_NONE_DRAWN))
        self.assertIsNone(view)
        self.assertIn("but drew none", err)
        self.assertIn("Nothing was added to the terrain cache", err)
        self.assertEqual(self.pooled(), [])
        self.assertFalse(any(n.startswith(".incoming-")
                             for n in os.listdir(self.pool)))

    def test_a_decode_warning_kills_the_ingest_before_its_first_tile(self):
        bad = ofm.tile_filename(14, 8555, 5755)
        conv = _Ingest(INGEST_DRAWN, before=[DECODING, _warn(bad)])
        with mock.patch.object(ofm, "fetch_building_tiles",
                               side_effect=_Fetch()):
            view, err = self.run_(conv)
        self.assertIsNone(view)
        self.assertIn("the converter could not decode 1 OpenFreeMap tile(s)",
                      err)
        self.assertEqual(self.pooled(), [])
        (job,) = conv.jobs
        self.assertFalse(os.path.exists(job["output_path"]),
                         "a tile was written after the warning")


# ---------------------------------------------------------------------------
# The real converter (AETHER_BIN_DIR): terrarium + the stand-in OpenFreeMap
# ---------------------------------------------------------------------------

class RealConverterSiteAnalysis(unittest.TestCase):

    def setUp(self):
        self.exe = _find_real_converter()
        if not self.exe:
            self.skipTest("aether_converter not provided via AETHER_BIN_DIR "
                          "or PATH")
        self.cache = _scratch(self)

    def _run(self, srv, osm=True):
        logged = []
        layer = mock.Mock()
        layer.source.return_value = srv.terrain_uri(zmax=12)
        layer.name.return_value = "terrarium"
        bm = mock.Mock()
        bm.find_binary.return_value = self.exe
        with mock.patch.object(ta, "get_cache_dir", return_value=self.cache), \
             mock.patch.object(ta, "classify_raster_layer",
                               return_value="dem"), \
             mock.patch.object(ta, "_log", side_effect=logged.append), \
             mock.patch.object(ofm, "TILEJSON_URL", srv.tilejson_url):
            view = ta.prepare_terrain(layer, 47.32, 8.42, 1.0, 30, bm,
                                      osm_buildings=osm)
            pool = ta._pool_dir(layer.source.return_value,
                                ta.buildings_identity(osm_buildings=osm))
        return view, pool, logged

    def test_whole_tile_buildings_are_burned_into_the_pool_tile(self):
        import numpy as np
        from waveshed.core import abt
        terrain = (lambda z, x, y: _terrarium_png(z, x, y, 900.0))
        # A house in every 8th z14 tile of the pool tile (the rest answer
        # "no data"), also far from the 1 km site.
        cover = ob.BuildingCover([{"south": 47.0, "north": 47.5,
                                   "west": 8.0, "east": 8.5}])
        houses = {xy: building_tile([HOUSE]) for xy in cover.cover
                  if xy[0] % 8 == 0 and xy[1] % 8 == 0}
        with OfmServer(tiles=houses, default=None, terrain=terrain) as srv:
            _view, pool, logged = self._run(srv)
            asked = srv.requested()
            _view, plain, _plain_log = self._run(srv, osm=False)
            n_bld = len(srv.requests)
            _again, _pool, again_log = self._run(srv)
            n_bld2 = len(srv.requests)
        text = "\n".join(logged)
        # The whole 0.5 deg pool tile's z14 tiles, not the 1 km sector's.
        self.assertEqual(asked, cover.cover)
        m = re.search(r"  buildings: ([\d,]+) building\(s\) decoded, "
                      r"([\d,]+) px raised", text)
        self.assertIsNotNone(m, text[-2000:])
        self.assertEqual(int(m.group(1).replace(",", "")), len(houses))
        self.assertGreater(int(m.group(2).replace(",", "")), 50 * len(houses))
        self.assertNotIn("WARNING", text)
        # The pool tile carries the buildings: raised over the plain tile,
        # never lowered, by about the 40 m roof.
        h1 = abt.read_header(os.path.join(pool, TILE_A))
        h0 = abt.read_header(os.path.join(plain, TILE_A))
        raised = (abt.read_tile(h1).astype(np.int32)
                  - abt.read_tile(h0).astype(np.int32))
        self.assertTrue((raised >= 0).all())
        self.assertGreaterEqual(raised.max(), 2 * 40)
        # Second run: a cache HIT, nothing fetched.
        self.assertEqual(n_bld2, n_bld)
        self.assertTrue(any("cache HIT" in ln for ln in again_log))

    def test_tiles_without_buildings_are_a_warning(self):
        # Tiles with data (a water layer) but no building in them.
        terrain = (lambda z, x, y: _terrarium_png(z, x, y, 900.0))
        cover = ob.BuildingCover([{"south": 47.0, "north": 47.5,
                                   "west": 8.0, "east": 8.5}])
        water = {xy: building_tile() for xy in cover.cover
                 if xy[0] % 8 == 0 and xy[1] % 8 == 0}
        with OfmServer(tiles=water, default=None, terrain=terrain) as srv:
            view, _pool, logged = self._run(srv)
        text = "\n".join(logged)
        self.assertTrue(os.path.isdir(view))
        self.assertIn("hold no building (\"no buildings in extent\")", text)


def _ready_converter():
    """An aether_converter >= 0.2.10 (``buildings_pbf_ready``): the one
    ``AETHER_CONVERTER_0210`` names, else AETHER_BIN_DIR's / PATH's when it
    is one; None."""
    for exe in (os.environ.get("AETHER_CONVERTER_0210"),
                _find_real_converter()):
        if (exe and os.path.isfile(exe)
                and bmod.converter_supports_buildings_ready(exe)):
            return exe
    return None


#: The converter's waiting line as Site Analysis logs it (indented by 4).
_WAITING_LINE = "    " + _WAITING


class RealConverterBesideTheDownload(unittest.TestCase):
    """The REAL converter >= 0.2.10 against the stand-in servers, the
    building tiles held until the converter waits for them (``_HeldTiles``):
    the download runs while they are fetched, waits for the marker and
    pools the bytes the sequential path pools; a failed fetch kills it
    while it waits."""

    def setUp(self):
        self.exe = _ready_converter()
        if not self.exe:
            self.skipTest("no aether_converter >= 0.2.10 (set "
                          "AETHER_CONVERTER_0210)")
        self.root = _scratch(self)
        self.cover = ob.BuildingCover([{"south": 47.0, "north": 47.5,
                                        "west": 8.0, "east": 8.5}])
        self.houses = {xy: building_tile([HOUSE]) for xy in self.cover.cover
                       if xy[0] % 8 == 0 and xy[1] % 8 == 0}

    def _run(self, srv, cache, sequential=False, on_line=None):
        """*on_line*: also called with every logged line."""
        logged = []

        def log(msg):
            logged.append(msg)
            if on_line is not None:
                on_line(msg)

        layer = mock.Mock()
        layer.source.return_value = srv.terrain_uri(zmax=12)
        layer.name.return_value = "terrarium"
        bm = mock.Mock()
        bm.find_binary.return_value = self.exe
        # sequential: the same binary through the < 0.2.10 path; a >= 0.2.14
        # binary is pinned to the view and its marker (the list mode has
        # its own class).
        gate = (mock.patch.object(bmod, "converter_supports_buildings_ready",
                                  return_value=False) if sequential
                else contextlib.nullcontext())
        with mock.patch.object(bmod, "converter_supports_buildings_list",
                               return_value=False), \
             mock.patch.object(ta, "get_cache_dir", return_value=cache), \
             mock.patch.object(ta, "classify_raster_layer",
                               return_value="dem"), \
             mock.patch.object(ta, "_log", side_effect=log), \
             mock.patch.object(ofm, "TILEJSON_URL", srv.tilejson_url), gate:
            try:
                view, err = ta.prepare_terrain(layer, 47.32, 8.42, 1.0, 30, bm,
                                               osm_buildings=True), None
            except RuntimeError as exc:
                view, err = None, str(exc)
            pool = ta._pool_dir(layer.source.return_value,
                                ta.buildings_identity(osm_buildings=True))
        return view, err, pool, logged

    @staticmethod
    def _markers(cache):
        views = os.path.join(cache, "views")
        return [v for v in os.listdir(views)
                if os.path.lexists(os.path.join(views, v, ob.READY_MARKER))]

    def test_the_download_waits_and_pools_the_sequential_bytes(self):
        terrain = (lambda z, x, y: _terrarium_png(z, x, y, 900.0))
        houses = self.houses
        # The house tiles are answered once the download waits for them
        # (+1 s); the sequential run fetches before its converter: not held.
        hold = _HeldTiles(self, _WAITING_LINE, houses, after_s=_AFTER_LINE_S)
        with OfmServer(tiles=houses, default=None, terrain=terrain,
                       delay_fn=hold) as srv, hold.client():
            _v, err, pool, logged = self._run(
                srv, os.path.join(self.root, "beside"), on_line=hold.saw)
            hold.check("\n".join(logged))
            hold.disarm()
            _v0, err0, pool0, logged0 = self._run(
                srv, os.path.join(self.root, "first"), sequential=True)
        self.assertIsNone(err)
        self.assertIsNone(err0)
        text = "\n".join(logged)
        # Launched before the fetch ended; waited for the marker there.
        first = next(i for i, m in enumerate(logged)
                     if m.startswith("    [Download] z="))
        ready = next(i for i, m in enumerate(logged)
                     if m.startswith("  buildings ready: "))
        self.assertLess(first, ready, text[-3000:])
        self.assertIn("    [Buildings] waiting for the building tiles (", text)
        self.assertRegex(text, r"    \[Buildings\] building tiles ready after "
                               r"\d+\.\d s")
        waited = re.search(r"  buildings fetched next to the terrain "
                           r"download: fetch \d+\.\d s, download \d+\.\d s, "
                           r"converter waited (\d+\.\d) s", text)
        self.assertIsNotNone(waited, text[-3000:])
        self.assertGreater(float(waited.group(1)), 0.0)
        m = re.search(r"  buildings: ([\d,]+) building\(s\) decoded", text)
        self.assertEqual(int(m.group(1).replace(",", "")), len(houses))
        self.assertNotIn("fetched next to the terrain download",
                         "\n".join(logged0))
        # The bytes of the sequential path (CONTRACT §9b: the same burn).
        with open(os.path.join(pool, TILE_A), "rb") as a, \
                open(os.path.join(pool0, TILE_A), "rb") as b:
            self.assertEqual(a.read(), b.read())

    def test_a_failed_fetch_kills_the_waiting_converter(self):
        terrain = (lambda z, x, y: _terrarium_png(z, x, y, 900.0))
        bad = sorted(self.cover.cover)[0]
        cache = os.path.join(self.root, "failed")
        hold = _HeldTiles(self, _WAITING_LINE, {bad})  # once it waits
        with OfmServer(tiles=self.houses, default=None, terrain=terrain,
                       fail={bad: 500}, delay_fn=hold) as srv, \
                mock.patch.object(ofm, "TILE_ATTEMPTS", 1), hold.client():
            view, err, pool, logged = self._run(srv, cache, on_line=hold.saw)
        text = "\n".join(logged)
        hold.check(text)
        self.assertIsNone(view)
        self.assertTrue(err.startswith("Buildings download failed "
                                       "(OpenFreeMap z14): "), err)
        self.assertIn(f"{ofm.tile_filename(14, *bad)}: HTTP 500", err)
        self.assertIn("    [Buildings] waiting for the building tiles (", text)
        self.assertIn("1 pool tile(s) it wrote removed", text)
        self.assertFalse(os.path.exists(os.path.join(pool, TILE_A)))
        self.assertEqual(self._markers(cache), [])


#: The real conversion's site (1 km at 30 m: pool tile A, a region of it)
#: and the GeoTIFF folder that covers that region.
_REAL_SITE = (47.32, 8.42)
_REAL_DEM = {"west": 8.37, "east": 8.47, "south": 47.28, "north": 47.36}


class _RealFolderConversion:
    """The real-converter folder fixture (no tests of its own): a real
    GeoTIFF folder covering the region of pool tile A that SITE's 1 km run
    builds, houses on every z14 tile of that region; :meth:`_converter`
    names the binary."""

    def _converter(self):
        raise NotImplementedError

    def setUp(self):
        self.exe = self._converter()
        self.root = _scratch(self)
        self.dem = os.path.join(self.root, "dem")
        os.makedirs(self.dem)
        with real_gdal():
            import numpy as np
            rows, cols = 160, 200
            r, c = np.mgrid[0:rows, 0:cols]
            write_tif(os.path.join(self.dem, "dem.tif"), _REAL_DEM,
                      size=(cols, rows),
                      array=(600.0 + 0.5 * r + 0.25 * c).astype(np.float32))
        bbox = ta._compute_sector_bbox(*_REAL_SITE, 1.0, 0.0, 360.0)
        (tile,) = ta._compute_subtiles(bbox, 30, *_REAL_SITE, 1.0, 0.0,
                                       360.0)
        self.assertEqual(tile["filename"], TILE_A)
        region = ta.tile_regions([tile], bbox, 30)[TILE_A]
        self.cover = ob.BuildingCover([region])
        self.houses = {xy: building_tile([HOUSE]) for xy in self.cover.cover}

    def _run(self, srv, cache, sequential=False, lists=False, on_line=None):
        """*sequential*: the same binary through the < 0.2.12 path; else
        the 0.2.12 view and its marker, or with *lists* the >= 0.2.14
        list. *on_line*: also called with every logged line."""
        logged = []

        def log(msg):
            logged.append(msg)
            if on_line is not None:
                on_line(msg)

        layer = mock.Mock()
        layer.source.return_value = self.dem
        layer.name.return_value = "dem"
        bm = mock.Mock()
        bm.find_binary.return_value = self.exe
        gate = (mock.patch.object(bmod,
                                  "converter_supports_ingest_buildings_ready",
                                  return_value=False) if sequential
                else contextlib.nullcontext())
        views = (contextlib.nullcontext() if lists
                 else mock.patch.object(bmod,
                                        "converter_supports_buildings_list",
                                        return_value=False))
        with real_gdal(), views, \
             mock.patch.object(ta, "get_cache_dir", return_value=cache), \
             mock.patch.object(ta, "classify_raster_layer",
                               return_value="dem"), \
             mock.patch.object(ta, "_log", side_effect=log), \
             mock.patch.object(ofm, "TILEJSON_URL", srv.tilejson_url), gate:
            try:
                view, err = ta.prepare_terrain(
                    layer, *_REAL_SITE, 1.0, 30, bm, terrain_dir=self.dem,
                    osm_buildings=True), None
            except RuntimeError as exc:
                view, err = None, str(exc)
            pool = ta._pool_dir(self.dem,
                                ta.buildings_identity(osm_buildings=True))
        return view, err, pool, logged

    @staticmethod
    def _markers(cache):
        views = os.path.join(cache, "views")
        if not os.path.isdir(views):
            return []
        return [v for v in os.listdir(views)
                if os.path.lexists(os.path.join(views, v, ob.READY_MARKER))]


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class RealConverterBesideTheConversion(_RealFolderConversion,
                                       unittest.TestCase):
    """The REAL converter >= 0.2.12 (CONTRACT item 48) over a real GeoTIFF
    folder and the stand-in OpenFreeMap, the building tiles held until the
    converter waits for them: the ingest runs while they are fetched, waits
    for the marker and pools the bytes the sequential path pools; a failed
    fetch kills it while it waits and pools nothing. (A >= 0.2.14 binary is
    pinned to the view here; :class:`RealConverterListsBesideTheConversion`
    runs its list.)"""

    def _converter(self):
        exe = _ingest_ready_converter()
        if not exe:
            self.skipTest("no aether_converter >= 0.2.12 (set "
                          "AETHER_CONVERTER_0212)")
        return exe

    def test_the_ingest_waits_and_pools_the_sequential_bytes(self):
        houses = self.houses
        # The house tiles are answered once the ingest waits for them
        # (+1 s); the sequential run fetches before its converter: not held.
        hold = _HeldTiles(self, _WAITING_LINE, houses, after_s=_AFTER_LINE_S)
        with OfmServer(tiles=houses, default=None, delay_fn=hold) as srv, \
                hold.client():
            _v, err, pool, logged = self._run(
                srv, os.path.join(self.root, "beside"), on_line=hold.saw)
            hold.check("\n".join(logged))
            hold.disarm()
            _v0, err0, pool0, logged0 = self._run(
                srv, os.path.join(self.root, "first"), sequential=True)
        text, text0 = "\n".join(logged), "\n".join(logged0)
        self.assertIsNone(err, text[-3000:])
        self.assertIsNone(err0, text0[-3000:])
        # The converter was running — and waiting for the marker — before
        # the fetch had its tiles.
        waiting = next((i for i, m in enumerate(logged) if m.startswith(
            "    [Buildings] waiting for the building tiles (")), None)
        ready = next(i for i, m in enumerate(logged)
                     if m.startswith("  buildings ready: "))
        self.assertIsNotNone(waiting, text[-3000:])
        self.assertLess(waiting, ready, text[-3000:])
        self.assertRegex(text, r"    \[Buildings\] building tiles ready after "
                               r"\d+\.\d s")
        timed = re.search(r"  buildings fetched next to the conversion: fetch "
                          r"\d+\.\d s, converter \d+\.\d s, converter waited "
                          r"(\d+\.\d) s", text)
        self.assertIsNotNone(timed, text[-3000:])
        self.assertGreater(float(timed.group(1)), 0.0)
        self.assertNotIn("fetched next to the conversion", text0)
        self.assertNotIn("[Buildings] waiting", text0)
        # The same burn, the same bytes (CONTRACT item 48: the burn is the
        # one an ingest that decodes at its start does).
        burn = [m for m in logged if m.startswith("  buildings: ")
                and " decoded" in m]
        burn0 = [m for m in logged0 if m.startswith("  buildings: ")
                 and " decoded" in m]
        self.assertEqual(burn, burn0)
        self.assertEqual(len(burn), 1, text[-3000:])
        part = TILE_A + ta._PART_SUFFIX
        with open(os.path.join(pool, part), "rb") as a, \
                open(os.path.join(pool0, part), "rb") as b:
            self.assertEqual(a.read(), b.read())
        self.assertFalse([n for n in os.listdir(pool)
                          if n.startswith(".incoming-")])

    def test_a_failed_fetch_kills_the_waiting_converter(self):
        bad = sorted(self.cover.cover)[0]
        cache = os.path.join(self.root, "failed")
        hold = _HeldTiles(self, _WAITING_LINE, {bad})  # once it waits
        with OfmServer(tiles=self.houses, default=None, fail={bad: 500},
                       delay_fn=hold) as srv, \
                mock.patch.object(ofm, "TILE_ATTEMPTS", 1), hold.client():
            view, err, pool, logged = self._run(srv, cache, on_line=hold.saw)
        text = "\n".join(logged)
        hold.check(text)
        self.assertIsNone(view)
        self.assertTrue(err.startswith("Buildings download failed "
                                       "(OpenFreeMap z14): "), err)
        self.assertIn(f"{ofm.tile_filename(14, *bad)}: HTTP 500", err)
        self.assertIn("    [Buildings] waiting for the building tiles (", text)
        listing = os.listdir(pool) if os.path.isdir(pool) else []
        self.assertFalse([n for n in listing if n.endswith((".abt",
                                                            ".abt.part"))])
        self.assertFalse([n for n in listing if n.startswith(".incoming-")])
        self.assertEqual(self._markers(cache), [])


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class RealConverterListsBesideTheConversion(_RealFolderConversion,
                                            unittest.TestCase):
    """The REAL aether_converter >= 0.2.14 (``AETHER_CONVERTER_0214``, else
    AETHER_BIN_DIR's / PATH's; ``buildings_pbf_list``, toolkit CONTRACT
    changelog 51; skipped without one) over the folder of
    :class:`RealConverterBesideTheConversion`:
    the ingest runs while the tiles are fetched, its job carries THE store
    and its list — nothing linked, no building view — it waits for the
    list and pools the bytes the view (0.2.12 marker) mode of the same
    binary pools."""

    def _converter(self):
        return _list_converter(self)

    def test_the_ingest_reads_its_list_and_pools_the_view_bytes(self):
        houses = self.houses
        jobs = {}
        real_run = ta.run_converter_streaming

        def launch(tag):
            def run(exe, args, on_line, **kw):
                if args[0] == "ingest":
                    with open(args[args.index("--job-file") + 1],
                              encoding="utf-8") as fh:
                        jobs[tag] = json.load(fh)
                return real_run(exe, args, on_line, **kw)
            return run

        # The house tiles are answered once the ingest waits for them
        # (+1 s), in both runs (each has its own cache, so fetches anew).
        hold = _HeldTiles(self, _WAITING_LINE, houses, after_s=_AFTER_LINE_S)
        with OfmServer(tiles=houses, default=None, delay_fn=hold) as srv, \
                hold.client():
            cache = os.path.join(self.root, "list")
            with mock.patch.object(ta, "run_converter_streaming",
                                   side_effect=launch("list")):
                view, err, pool, logged = self._run(srv, cache, lists=True,
                                                    on_line=hold.saw)
            hold.check("\n".join(logged))
            hold.rearm()
            cache0 = os.path.join(self.root, "view")
            with mock.patch.object(ta, "run_converter_streaming",
                                   side_effect=launch("view")):
                _v0, err0, pool0, logged0 = self._run(srv, cache0,
                                                      on_line=hold.saw)
            hold.check("\n".join(logged0))
        text, text0 = "\n".join(logged), "\n".join(logged0)
        self.assertIsNone(err, text[-3000:])
        self.assertIsNone(err0, text0[-3000:])
        (job,), (job0,) = jobs["list"], jobs["view"]
        with mock.patch.object(ta, "get_cache_dir", return_value=cache):
            store = ta.osm_buildings_store_dir()
            lists = ta.osm_buildings_list_dir()
        self.assertEqual(job["buildings_pbf_dir"], os.path.abspath(store))
        path = job["buildings_pbf_list"]
        self.assertEqual(os.path.dirname(path), lists)
        self.assertNotIn("buildings_pbf_ready", job)
        self.assertEqual(job0["buildings_pbf_ready"], "_ready")
        self.assertNotIn("buildings_pbf_list", job0)
        names = read_building_list(path)
        self.assertEqual(sorted(names), sorted(
            n for n in os.listdir(job0["buildings_pbf_dir"])
            if n.endswith(".pbf")))
        self.assertEqual(len(names), len(houses))
        # No building view: the terrain view and the lists' directory.
        self.assertEqual(sorted(os.listdir(os.path.join(cache, "views"))),
                         sorted([os.path.basename(view),
                                 os.path.basename(lists)]))
        # Running — and waiting for its list — before the fetch had its
        # tiles; then the listed tiles decoded from the store.
        waiting = logged.index(f"    [Buildings] waiting for the building "
                               f"tiles ({path})")
        ready = next(i for i, m in enumerate(logged)
                     if m.startswith("  buildings ready: "))
        self.assertLess(waiting, ready, text[-3000:])
        self.assertIn(f"    [Rust] Decoding {len(names)} listed building "
                      f"tiles from ", text)
        self.assertIn(f"  buildings: {len(names):,} OpenFreeMap tile(s) "
                      f"listed for the converter in {path} — the conversion "
                      f"burns them now", logged)
        timed = re.search(r"  buildings fetched next to the conversion: fetch "
                          r"\d+\.\d s, converter \d+\.\d s, converter waited "
                          r"(\d+\.\d) s", text)
        self.assertIsNotNone(timed, text[-3000:])
        self.assertGreater(float(timed.group(1)), 0.0)
        # The same burn, the same bytes.
        burn = [m for m in logged if m.startswith("  buildings: ")
                and " decoded" in m]
        burn0 = [m for m in logged0 if m.startswith("  buildings: ")
                 and " decoded" in m]
        self.assertEqual(len(burn), 1, text[-3000:])
        self.assertEqual(burn, burn0)
        part = TILE_A + ta._PART_SUFFIX
        with open(os.path.join(pool, part), "rb") as a, \
                open(os.path.join(pool0, part), "rb") as b:
            self.assertEqual(a.read(), b.read())


# ---------------------------------------------------------------------------
# Prepared .abt folders: what the chosen tiles carry
# ---------------------------------------------------------------------------

def _prepared(folder, records, *corners):
    """30 m prepared tiles at *corners* (south, west) with the Map
    Converter's records (name → buildings record, None = no record)."""
    os.makedirs(folder, exist_ok=True)
    names = []
    for south, west in corners:
        t = ta._tile_params(0.5, south, west, 30)
        _write_header_tile(os.path.join(folder, t["filename"]),
                           float(t["exact_res_m"]), int(t["size_px"]),
                           t["ul_lat"], t["ul_lon"])
        names.append(t["filename"])
    if records is not None:
        ta.write_tiles_manifest(
            folder, {n: f"fp-{n}" for n in names},
            {n: rec for n, rec in zip(names, records) if rec})
    return names


class _PreparedCase(unittest.TestCase):

    def setUp(self):
        self.root = _scratch(self)
        self.dir = os.path.join(self.root, "prepared")
        for patch in (mock.patch.object(ta, "get_cache_dir",
                                        return_value=os.path.join(self.root,
                                                                  "cache")),):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(abt_index.forget)
        sat._BUILDINGS_NOTICE_FOLDERS.clear()
        self.addCleanup(sat._BUILDINGS_NOTICE_FOLDERS.clear)


class SiteAnalysisDialogs(_PreparedCase):
    """``_on_run`` on a prepared folder whose coverage and resolution fit:
    only the buildings dialogs speak."""

    def _run(self, ticked, answers=None, site=SITE):
        jobs = [(CoverageParams(tx_lat=site[0], tx_lon=site[1],
                                resolution_m=30, max_range_km=4, model="LOS",
                                output_name="site1"), "Site 1")]
        tab = sat.SiteAnalysisTab.__new__(sat.SiteAnalysisTab)
        tab.sites_table = mock.MagicMock()
        tab.sites_table.rowCount.return_value = 1
        tab.alt_table = mock.MagicMock()
        tab.alt_table.rowCount.return_value = 1
        tab._read_sites = lambda: [{"coords": site}]
        tab._selected_dem_layer = lambda: None
        tab._selected_terrain_dir = lambda: self.dir
        tab.line_output_dir = mock.Mock(text=mock.Mock(
            return_value=os.path.join(self.root, "out")))
        tab._refresh_asset_cache = lambda: None
        tab._build_jobs = lambda: jobs
        tab.chk_buildings = mock.Mock(isChecked=mock.Mock(return_value=ticked))
        tab._launch_worker = mock.Mock()
        box = mock.Mock()
        box.StandardButton = sat.QMessageBox.StandardButton
        box.warning.side_effect = lambda _p, title, *_a: getattr(
            box.StandardButton, (answers or {}).get(title, "No"))

        def scan_folder(_parent, _folder, after=None, **_kw):
            out = {}
            th = threading.Thread(target=lambda: out.update(extra=after(None)))
            th.start()
            th.join()
            return {"index": None, "stats": None, "extra": out["extra"]}

        logged = []
        with mock.patch.object(sat, "dem_layer_warning", return_value=None), \
             mock.patch.object(sat, "save_output_dir"), \
             mock.patch.object(sat, "binaries_warning", return_value=None), \
             mock.patch.object(sat, "antenna_height_error", return_value=None), \
             mock.patch.object(sat, "model_warnings", return_value=[]), \
             mock.patch.object(sat, "output_space_problem",
                               return_value=(None, False)), \
             mock.patch.object(sat, "_disk_refusal", return_value=None), \
             mock.patch.object(sat, "QMessageBox", box), \
             mock.patch.object(sat.terrain_folder_scan, "scan_folder",
                               side_effect=scan_folder), \
             mock.patch.object(sat.QgsMessageLog, "logMessage",
                               side_effect=lambda m, *a, **k: logged.append(m)):
            tab._on_run()
        return tab, box, logged

    @staticmethod
    def titles(calls):
        return [c.args[1] for c in calls]

    def test_ticked_and_every_tile_has_buildings(self):
        _prepared(self.dir, ["openfreemap:z14"], (47.0, 8.0))
        tab, box, _logged = self._run(ticked=True)
        self.assertEqual(self.titles(box.warning.call_args_list), [])
        self.assertEqual(self.titles(box.information.call_args_list),
                         ["Buildings already in the terrain"])
        text = box.information.call_args.args[2]
        self.assertTrue(text.startswith("This prepared terrain already "
                                        "contains buildings; nothing is "
                                        "added."))
        self.assertIn("1 with buildings (openfreemap:z14) — of 1 tile(s)",
                      text)
        tab._launch_worker.assert_called_once()

    def test_ticked_and_no_tile_has_buildings(self):
        _prepared(self.dir, ["none"], (47.0, 8.0))
        tab, box, _logged = self._run(ticked=True)
        self.assertEqual(self.titles(box.warning.call_args_list),
                         ["Prepared terrain without buildings"])
        text = box.warning.call_args.args[2]
        self.assertIn("'Include buildings' cannot add them to a prepared "
                      "folder — build it with buildings in the Map "
                      "Converter", text)
        tab._launch_worker.assert_not_called()              # "No"
        tab, box, _logged = self._run(
            ticked=True, answers={"Prepared terrain without buildings": "Yes"})
        tab._launch_worker.assert_called_once()             # run anyway

    def test_ticked_and_mixed_lists_the_counts(self):
        _prepared(self.dir, ["openfreemap:z14", "none"], (47.0, 8.0),
                  (47.0, 8.5))
        tab, box, _logged = self._run(ticked=True, site=(47.25, 8.5))
        self.assertEqual(self.titles(box.warning.call_args_list),
                         ["Prepared terrain partly without buildings"])
        self.assertIn("1 with buildings (openfreemap:z14), 1 without — of 2 "
                      "tile(s)", box.warning.call_args.args[2])
        tab._launch_worker.assert_not_called()

    def test_ticked_and_unknown_is_one_log_line(self):
        _prepared(self.dir, None, (47.0, 8.0))           # no manifest
        tab, box, logged = self._run(ticked=True)
        self.assertEqual(box.warning.call_args_list, [])
        self.assertEqual(box.information.call_args_list, [])
        lines = [m for m in logged if "is not recorded" in m]
        self.assertEqual(len(lines), 1, logged)
        self.assertIn("'Include buildings' adds nothing to a prepared "
                      "folder", lines[0])
        tab._launch_worker.assert_called_once()

    def test_unticked_with_buildings_tells_once_per_folder(self):
        _prepared(self.dir, ["openfreemap:z14"], (47.0, 8.0))
        tab, box, _logged = self._run(ticked=False)
        self.assertEqual(self.titles(box.information.call_args_list),
                         ["Terrain carries buildings"])
        self.assertIn("although 'Include buildings' is unticked",
                      box.information.call_args.args[2])
        tab._launch_worker.assert_called_once()
        _tab, box2, _logged = self._run(ticked=False)
        self.assertEqual(box2.information.call_args_list, [])

    def test_unticked_without_buildings_says_nothing(self):
        _prepared(self.dir, ["none"], (47.0, 8.0))
        tab, box, _logged = self._run(ticked=False)
        self.assertEqual(box.information.call_args_list, [])
        self.assertEqual(box.warning.call_args_list, [])
        tab._launch_worker.assert_called_once()


class BuildingsToggleHint(_PreparedCase):

    def _toggle(self, terrain_dir):
        tab = sat.SiteAnalysisTab.__new__(sat.SiteAnalysisTab)
        tab._selected_terrain_dir = lambda: terrain_dir
        tab._refresh_res_profile = mock.Mock()
        tab.chk_buildings = mock.Mock()
        tab.iface = mock.Mock()
        tab._on_buildings_toggled(True)
        tab._refresh_res_profile.assert_called_once()
        return tab

    def test_a_prepared_folder_gets_the_hint(self):
        _prepared(self.dir, None, (47.0, 8.0))
        tab = self._toggle(self.dir)
        tab.chk_buildings.setStatusTip.assert_called_once_with(
            sat._PREPARED_BUILDINGS_HINT)
        push = tab.iface.messageBar.return_value.pushMessage
        push.assert_called_once()
        self.assertEqual(push.call_args.args[1], sat._PREPARED_BUILDINGS_HINT)
        self.assertIn("cannot add or remove them", sat._PREPARED_BUILDINGS_HINT)

    def test_no_hint_for_a_layer_or_a_raster_folder(self):
        for terrain_dir in (None, self.root):
            tab = self._toggle(terrain_dir)
            tab.chk_buildings.setStatusTip.assert_called_once_with("")
            tab.iface.messageBar.return_value.pushMessage.assert_not_called()


class PreparedFolderLogs(_PreparedCase):
    """The worker / Processing route: prepare_terrain and
    prepare_terrain_profile log what the chosen tiles carry."""

    def test_prepare_terrain_and_the_profile_log_it(self):
        _prepared(self.dir, ["openfreemap:z14"], (47.0, 8.0))
        logged = []
        layer = mock.Mock()
        with mock.patch.object(ta, "_log", side_effect=logged.append):
            ta.prepare_terrain(layer, *SITE, 4.0, 30, mock.Mock(),
                               terrain_dir=self.dir, osm_buildings=True)
            ta.prepare_terrain(layer, *SITE, 4.0, 30, mock.Mock(),
                               terrain_dir=self.dir)
            ta.prepare_terrain_profile(layer, *SITE, [(4000.0, 30)],
                                       mock.Mock(), terrain_dir=self.dir,
                                       osm_buildings=True)
        lines = [ln.strip() for ln in logged
                 if ln.strip().startswith(("buildings:", "WARNING: build"))]
        self.assertEqual(lines, [
            "buildings: the prepared terrain already carries buildings (1 "
            "with buildings (openfreemap:z14) — of 1 tile(s)) — nothing is "
            "added",
            "buildings: the prepared terrain carries buildings (1 with "
            "buildings (openfreemap:z14) — of 1 tile(s)) although none were "
            "requested — they are part of its tiles",
            "buildings: the prepared terrain already carries buildings (1 "
            "with buildings (openfreemap:z14) — of 1 tile(s)) — nothing is "
            "added"])

    def test_requested_on_a_folder_without_a_record_warns(self):
        _prepared(self.dir, None, (47.0, 8.0))
        logged = []
        with mock.patch.object(ta, "_log", side_effect=logged.append):
            ta.prepare_terrain(mock.Mock(), *SITE, 4.0, 30, mock.Mock(),
                               terrain_dir=self.dir, osm_buildings=True)
        self.assertTrue(any(ln.strip().startswith(
            "WARNING: buildings were requested, but whether the prepared "
            "terrain carries any is not recorded") for ln in logged), logged)


class P2PLogLine(_PreparedCase):

    def _check(self):
        csv_path = os.path.join(self.root, "link.csv")
        with open(csv_path, "w", encoding="utf-8") as fh:
            fh.write(f"S,TX,{SITE[0]:.6f},{SITE[1] - 0.01:.6f},10.0,AGL\n"
                     f"R,RX,{SITE[0]:.6f},{SITE[1] + 0.01:.6f},10.0,AGL\n")
        tab = p2p.P2PTab.__new__(p2p.P2PTab)
        logged = []
        with mock.patch.object(p2p.QgsMessageLog, "logMessage",
                               side_effect=lambda m, *a, **k:
                               logged.append(m)), \
             mock.patch.object(p2p, "QMessageBox") as box:
            ok = tab._check_terrain_folder(self.dir, csv_path,
                                           mock.Mock(resolution_m=30))
        self.assertTrue(ok)
        box.warning.assert_not_called()
        return [m for m in logged if m.startswith("P2P terrain:")]

    def test_one_line_says_what_the_tiles_carry(self):
        _prepared(self.dir, ["openfreemap:z14"], (47.0, 8.0))
        self.assertEqual(self._check(), [
            f"P2P terrain: the prepared tiles of {self.dir} carry buildings "
            f"— the link is computed over them (1 with buildings "
            f"(openfreemap:z14) — of 1 tile(s))"])

    def test_without_a_record(self):
        _prepared(self.dir, None, (47.0, 8.0))
        (line,) = self._check()
        self.assertIn("have no record of their buildings", line)


if __name__ == "__main__":
    unittest.main()
