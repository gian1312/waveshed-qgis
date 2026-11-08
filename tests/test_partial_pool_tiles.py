"""Partial pool tiles — build only the part of a pool tile the site needs.

A file source (folder / file layer) builds a tile for the job's region R =
tile ∩ (sector bbox grown by max(2 output px, 500 m)) only: the converter job
keeps the whole tile's geometry and lists only the files R admits, the tile is
pooled as ``<name>.part`` + ``<name>.part.json`` and reached ONLY through
``terrain_adapter.resolve_pool_tile`` (views link it under the canonical
name). Pinned here: the region rule, every hit-rule transition, that a
``.part`` is never a tile to any older path, the view key, the multi-job
union, the bytes inside R (synthetic LV95 folder, real converter when one is
provided, else a converter model fed the job's own ``sources[]``), the view
freshness rule, rebuild flags and range-profile bands.
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import json
import os
import shutil
import struct
import tempfile
import time
import unittest
from unittest import mock

import numpy as np

import waveshed.core.terrain_adapter as ta
from _real_gdal import GDAL, HAVE_GDAL, OSR, real_gdal, write_tif
from waveshed.core import abt
from waveshed.core import terrain_index as ti

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: Scratch inside the project's gitignored .tmp (never the container drive).
_SCRATCH = os.path.join(_REPO, ".tmp", "test_partial_pool_tiles")

_XYZ = ("type=xyz&url=https%3A//example.com/%7Bz%7D/%7Bx%7D/%7By%7D.png"
        "&zmax=15")

#: Site A / site B: 15 km at 90 m, both inside the 1 deg tile N47 E7.
SITE_A = (46.75, 7.35)
SITE_B = (46.30, 7.75)
RANGE_KM = 15.0
RES = 90
TILE = "tile_N47.00E7.00_90m.abt"


def _scratch_dir(test: unittest.TestCase) -> str:
    os.makedirs(_SCRATCH, exist_ok=True)
    d = tempfile.mkdtemp(prefix="t_", dir=_SCRATCH)
    test.addCleanup(shutil.rmtree, d, True)
    return d


def _tiles(site, rng=RANGE_KM, res=RES, az=(0.0, 360.0)):
    bbox = ta._compute_sector_bbox(site[0], site[1], rng, *az)
    return bbox, ta._compute_subtiles(bbox, res, site[0], site[1], rng, *az)


def _spec(site=SITE_A):
    bbox, tiles = _tiles(site)
    (t,) = [t for t in tiles if t["filename"] == TILE]
    return bbox, t


def _region(site=SITE_A, output_res=None):
    bbox, t = _spec(site)
    return ta.tile_regions([t], bbox, RES, output_res)[TILE]


def _header(spec):
    size = int(spec["size_px"])
    stride = (size * 2 + 255) & ~255
    px = spec["exact_res_m"] / 111_111.0
    return (b"AETH" + struct.pack("<HH", 1, size)
            + struct.pack("<dddd", spec["ul_lat"], spec["ul_lon"], px, px)
            + struct.pack("<hH", 0, stride)), size, stride


def _sparse_abt(path, spec):
    """A whole-length .abt of *spec*'s geometry (sparse, no pixels)."""
    hdr, size, stride = _header(spec)
    with open(path, "wb") as fh:
        fh.write(hdr)
        fh.truncate(abt.HEADER_SIZE + size * stride)


def _write_part(pool, spec, regions, fps=None):
    """A bound ``.part`` pair for *spec* holding *regions*."""
    os.makedirs(pool, exist_ok=True)
    _sparse_abt(os.path.join(pool, spec["filename"] + ta._PART_SUFFIX), spec)
    fps = fps or ["fp"] * len(regions)
    assert ta._write_part_meta(pool, spec, [{"bbox": r, "fp": f}
                                            for r, f in zip(regions, fps)])


def _halves(spec):
    tb = ta._subtile_bbox(spec)
    mid = spec["ul_lon"] + (spec["size_px"] // 2) * spec["exact_res_m"] / 111_111.0
    return dict(tb, east=mid), dict(tb, west=mid)


# ---------------------------------------------------------------------------
# The region rule
# ---------------------------------------------------------------------------

class RegionRule(unittest.TestCase):

    def test_region_is_the_grown_sector_bbox_on_the_tile_grid(self):
        bbox, t = _spec()
        r = _region()
        tb = ta._subtile_bbox(t)
        res_deg = t["exact_res_m"] / 111_111.0
        self.assertIsNotNone(r)
        for k in ("west", "east"):
            n = (r[k] - t["ul_lon"]) / res_deg
            self.assertAlmostEqual(n, round(n), places=6)
        for k in ("north", "south"):
            n = (t["ul_lat"] - r[k]) / res_deg
            self.assertAlmostEqual(n, round(n), places=6)
        # Inside the tile, and at least 500 m beyond the sector bbox.
        self.assertTrue(tb["west"] <= r["west"] < r["east"] <= tb["east"])
        self.assertTrue(tb["south"] <= r["south"] < r["north"] <= tb["north"])
        m_lat = 500.0 / 111_111.0
        self.assertLessEqual(r["north"], tb["north"])
        self.assertGreaterEqual(r["north"], bbox["north"] + m_lat - 1e-12)
        self.assertLessEqual(r["south"], bbox["south"] - m_lat + 1e-12)
        # ~0.3 x 0.4 deg of a 1 deg tile.
        self.assertLess(ta._bbox_area(r), 0.2 * ta._bbox_area(tb))

    def test_the_margin_is_two_output_pixels_when_that_is_more(self):
        small, big = _region(output_res=30), _region(output_res=1000)
        self.assertGreaterEqual(big["north"] - small["north"],
                                (2000.0 - 500.0) / 111_111.0 - 1e-3)

    def test_a_job_spanning_the_tile_needs_it_whole(self):
        bbox, tiles = _tiles((46.5, 7.5), rng=80.0)
        regions = ta.tile_regions(tiles, bbox, RES)
        self.assertIsNone(regions[TILE])
        self.assertTrue(any(r is not None for r in regions.values()))

    def test_sources_without_files_are_always_whole(self):
        bbox, tiles = _tiles(SITE_A)
        self.assertEqual(set(ta.tile_regions(tiles, bbox, RES,
                                             partial=False).values()), {None})

    def test_run_union_holds_every_jobs_region(self):
        from waveshed.core.job_builder import CoverageParams
        jobs = [CoverageParams(tx_lat=s[0], tx_lon=s[1], resolution_m=RES,
                               max_range_km=RANGE_KM) for s in (SITE_A, SITE_B)]
        # Two altitudes of site A add nothing.
        jobs.append(CoverageParams(tx_lat=SITE_A[0], tx_lon=SITE_A[1],
                                   resolution_m=RES, max_range_km=RANGE_KM,
                                   rx_height=10.0))
        union = ta.run_region_union(jobs)
        self.assertEqual(union[TILE], ta._merge_regions([_region(SITE_A),
                                                         _region(SITE_B)]))
        self.assertEqual(len(union[TILE]), 2)


# ---------------------------------------------------------------------------
# THE hit rule — every transition (real files, sparse tiles)
# ---------------------------------------------------------------------------

class HitRule(unittest.TestCase):

    def setUp(self):
        self.pool = os.path.join(_scratch_dir(self), "pool")
        os.makedirs(self.pool)
        _bbox, self.spec = _spec()
        self.ra, self.rb = _region(SITE_A), _region(SITE_B)
        self.fps = {}

    def fp_of(self, box):
        return self.fps.get(tuple(sorted(box.items())), "fp")

    def resolve(self, region, **kw):
        kw.setdefault("fp_of", self.fp_of)
        return ta.resolve_pool_tile(self.pool, self.spec, region, **kw)

    def test_empty_pool_builds_the_region(self):
        v = self.resolve(self.ra)
        self.assertEqual((v.hit, v.partial, v.regions, v.reason),
                         (False, True, [self.ra], "missing"))

    def test_a_part_holding_the_region_is_a_hit(self):
        _write_part(self.pool, self.spec, [self.ra])
        v = self.resolve(self.ra)
        self.assertTrue(v.hit and v.partial)
        self.assertTrue(v.path.endswith(TILE + ".part"))
        inner = dict(self.ra, east=self.ra["west"]
                     + (self.ra["east"] - self.ra["west"]) / 2)
        self.assertTrue(self.resolve(inner).hit)

    def test_a_region_outside_rebuilds_the_union(self):
        _write_part(self.pool, self.spec, [self.ra])
        v = self.resolve(self.rb)
        self.assertEqual((v.hit, v.reason), (False, "outside"))
        self.assertEqual(v.regions, ta._merge_regions([self.ra, self.rb]))
        # The run's union rides along (a region inside another adds none).
        far = dict(self.rb, north=self.rb["south"] + 0.01)
        v2 = self.resolve(dict(self.rb), extra_regions=[far, self.ra])
        self.assertEqual(v2.regions, ta._merge_regions([self.ra, self.rb]))

    def test_a_union_covering_the_tile_is_built_whole(self):
        west, east = _halves(self.spec)
        _write_part(self.pool, self.spec, [west])
        v = self.resolve(east)
        self.assertEqual((v.hit, v.partial, v.regions), (False, False, None))
        # Same through the run's union alone.
        os.remove(os.path.join(self.pool, TILE + ".part.json"))
        v = self.resolve(self.ra, extra_regions=[west, east])
        self.assertEqual((v.partial, v.regions), (False, None))

    def test_a_stale_region_rebuilds_every_region(self):
        _write_part(self.pool, self.spec, [self.ra, self.rb], ["fp", "old"])
        v = self.resolve(self.ra)
        self.assertEqual((v.hit, v.reason), (False, "stale"))
        self.assertEqual(v.regions, ta._merge_regions([self.ra, self.rb]))
        # Without fingerprints (the GUI estimate) it is a hit.
        self.assertTrue(self.resolve(self.ra, fp_of=None).hit)

    def test_a_rebuild_flag_blocks_the_part(self):
        _write_part(self.pool, self.spec, [self.ra])
        ta._set_rebuild_flags(self.pool, [TILE], {TILE})
        v = self.resolve(self.ra)
        self.assertEqual((v.hit, v.reason, v.regions),
                         (False, "flagged", [self.ra]))
        ta._set_rebuild_flags(self.pool, [TILE])
        self.assertTrue(self.resolve(self.ra).hit)

    def test_an_unbound_sidecar_is_no_part(self):
        _write_part(self.pool, self.spec, [self.ra])
        part = os.path.join(self.pool, TILE + ".part")
        st = os.stat(part)
        os.utime(part, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        v = self.resolve(self.rb)
        self.assertFalse(v.hit)
        self.assertEqual(v.regions, [self.rb], "old regions must be forgotten")
        self.assertFalse(self.resolve(self.ra).hit)

    def test_a_short_part_is_no_part(self):
        _write_part(self.pool, self.spec, [self.ra])
        part = os.path.join(self.pool, TILE + ".part")
        with open(part, "r+b") as fh:
            fh.truncate(4096)
        ta._write_part_meta(self.pool, self.spec, [{"bbox": self.ra,
                                                    "fp": "fp"}])
        self.assertFalse(self.resolve(self.ra).hit)

    def test_complete_tile_rules_first(self):
        whole = os.path.join(self.pool, TILE)
        _sparse_abt(whole, self.spec)
        with open(whole + ".src", "w") as fh:
            fh.write("fp")
        v = self.resolve(self.ra)
        self.assertEqual((v.hit, v.partial, v.path), (True, False, whole))
        # Stale whole tile: a current .part holding the region serves.
        with open(whole + ".src", "w") as fh:
            fh.write("old")
        self.assertEqual(self.resolve(self.ra).reason, "stale")
        _write_part(self.pool, self.spec, [self.ra])
        v = self.resolve(self.ra)
        self.assertTrue(v.hit and v.partial)
        # A whole-tile job never takes the .part.
        v = self.resolve(None)
        self.assertEqual((v.hit, v.regions, v.reason), (False, None, "stale"))

    def test_link_takes_the_newer_of_whole_and_part(self):
        whole = os.path.join(self.pool, TILE)
        _sparse_abt(whole, self.spec)
        _write_part(self.pool, self.spec, [self.ra])
        part = os.path.join(self.pool, TILE + ".part")
        st = os.stat(part)
        os.utime(whole, ns=(st.st_atime_ns, st.st_mtime_ns - 10**9))
        self.assertEqual(self.resolve(self.ra, for_link=True).path, part)
        os.utime(whole, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        self.assertEqual(self.resolve(self.ra, for_link=True).path, whole)
        # Flags and fingerprints do not matter at link time.
        ta._set_rebuild_flags(self.pool, [TILE], {TILE})
        os.remove(whole)
        self.assertEqual(self.resolve(self.ra, for_link=True,
                                      fp_of=lambda b: "x").path, part)
        self.assertIsNone(self.resolve(self.rb, for_link=True).path)


# ---------------------------------------------------------------------------
# A .part is never a tile to anything that predates it
# ---------------------------------------------------------------------------

class PartIsNeverATile(unittest.TestCase):

    def setUp(self):
        self.root = _scratch_dir(self)
        self.pool = os.path.join(self.root, "pool")
        _bbox, self.spec = _spec()
        _write_part(self.pool, self.spec, [_region()])
        self.listing = ta._pool_listing(self.pool)
        self.assertIsNotNone(ta._read_part_meta(self.pool, self.spec,
                                                self.listing),
                             "the fixture must be a valid, bound .part")

    def test_no_existing_path_serves_it_as_complete(self):
        pool, spec = self.pool, self.spec
        self.assertFalse(ta._tile_ready(pool, TILE))
        self.assertFalse(ta._listed_tile_ready(self.listing, spec, pool))
        self.assertFalse(ta._listed_tile_whole(self.listing, spec, pool))
        self.assertFalse(ta._tile_is_whole(os.path.join(pool, TILE)))
        self.assertEqual(ta.list_abt_tiles(pool), [])
        self.assertEqual(abt.list_tiles(pool), [])
        self.assertFalse(ta.is_abt_tile_dir(pool))
        self.assertEqual(ta.select_abt_tiles(pool, *SITE_A,
                                             [(15000.0, RES)]).paths, [])
        # The engine's read_dir takes the .abt extension only.
        self.assertEqual(os.path.splitext(TILE + ".part")[1], ".part")
        # A view built the old way (no resolver) does not get it.
        view = os.path.join(self.root, "view")
        self.assertEqual(ta._sync_view(pool, view, [TILE]), [TILE])
        self.assertEqual(os.listdir(view), [])
        # A whole-tile job does not take it; a region it lacks does not.
        self.assertFalse(ta.resolve_pool_tile(pool, spec, None).hit)
        self.assertFalse(ta.resolve_pool_tile(pool, spec,
                                              _region(SITE_B)).hit)
        self.assertIsNone(ta.resolve_pool_tile(pool, spec, None,
                                               for_link=True).path)

    def test_xyz_pool_paths_count_it_as_missing(self):
        with mock.patch.object(ta, "get_cache_dir", return_value=self.root):
            pool = ta._pool_dir(_XYZ)
            empty = ta.estimate_pool_download_units(_XYZ, [self.spec])
            self.assertGreater(empty, 0)
            _write_part(pool, self.spec, [_region()])
            self.assertEqual(ta.estimate_pool_download_units(
                _XYZ, [self.spec]), empty)
            plan = ta.terrain_plan(_XYZ, *SITE_A, RANGE_KM, RES)
            self.assertEqual(plan["tiles_cached"], 0)
            self.assertEqual(plan["tiles_partial"], 0, "XYZ is never partial")
            asked = []

            def fake_download(uri, pool_dir, _bbox, res, todo, *_a, **_kw):
                asked.extend(t["filename"] for t in todo)
                for t in todo:
                    _sparse_abt(os.path.join(pool_dir, t["filename"]), t)
                return ta.DownloadOutcome(True)

            with mock.patch.object(ta, "_try_rust_download",
                                   side_effect=fake_download):
                ta.ensure_pool_tiles(_XYZ, [self.spec], RES,
                                     binary_manager=mock.Mock())
        self.assertEqual(asked, [TILE])


# ---------------------------------------------------------------------------
# The view key carries the regions
# ---------------------------------------------------------------------------

class ViewKeyPerRegion(unittest.TestCase):

    def test_key_differs_per_region_and_not_for_whole_runs(self):
        tiles = [{"filename": TILE}]
        ra, rb = _region(SITE_A), _region(SITE_B)
        plain = ta._cache_key("src", tiles, RES)
        a = ta._cache_key("src", tiles, RES, None, {TILE: ra})
        b = ta._cache_key("src", tiles, RES, None, {TILE: rb})
        self.assertEqual(len({plain, a, b}), 3)
        self.assertEqual(ta._cache_key("src", tiles, RES, None, {TILE: None}),
                         plain)
        self.assertEqual(a, ta._cache_key("src", tiles, RES, None,
                                          {TILE: dict(ra)}))


# ---------------------------------------------------------------------------
# End to end over a synthetic LV95 folder
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# A rename Windows refuses (the target held open by another process)
# ---------------------------------------------------------------------------

def _refused(*paths):
    """PermissionError as Windows raises it for a file another process holds
    open (Defender's on-close scan, GDAL, a viewer)."""
    return PermissionError(13, "The process cannot access the file because "
                           "it is being used by another process", *paths)


class ReplaceWithRetry(unittest.TestCase):

    def run_with(self, outcomes, attempts=5):
        """``replace_with_retry`` over an ``os.replace`` that raises the
        exceptions of *outcomes* in turn (None = succeeds)."""
        calls, sleeps = [], []

        def fake(src, dst):
            calls.append((src, dst))
            exc = outcomes[min(len(calls), len(outcomes)) - 1]
            if exc is not None:
                raise exc
        with mock.patch.object(ta.os, "replace", side_effect=fake), \
             mock.patch.object(ta.time, "sleep", side_effect=sleeps.append):
            try:
                ta.replace_with_retry("a", "b", attempts=attempts)
                raised = None
            except OSError as exc:
                raised = exc
        return calls, sleeps, raised

    def test_a_refusal_is_retried_until_the_rename_works(self):
        calls, sleeps, raised = self.run_with([_refused(), _refused(), None])
        self.assertIsNone(raised)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [0.1, 0.1])

    def test_a_refusal_that_persists_is_raised_after_the_last_attempt(self):
        calls, sleeps, raised = self.run_with([_refused()])
        self.assertIsInstance(raised, PermissionError)
        self.assertEqual((len(calls), sleeps), (5, [0.1] * 4))
        calls, _s, raised = self.run_with([_refused()], attempts=2)
        self.assertEqual(len(calls), 2)

    def test_what_counts_as_a_refusal(self):
        import errno
        sharing = OSError(errno.EINVAL, "sharing violation")
        sharing.winerror = 32                       # as Windows reports it
        denied = OSError(errno.EINVAL, "access denied")
        denied.winerror = 5
        for exc in (sharing, denied, OSError(errno.EPERM, "not permitted"),
                    OSError(errno.EACCES, "denied")):
            with self.subTest(exc=exc):
                calls, _s, raised = self.run_with([exc, None])
                self.assertIsNone(raised)
                self.assertEqual(len(calls), 2)
        # Anything else is raised at once: no retry, no wait.
        for exc in (FileNotFoundError(errno.ENOENT, "gone"),
                    OSError(errno.EXDEV, "cross-device link"),
                    OSError(errno.ENOSPC, "disk full")):
            with self.subTest(exc=exc):
                calls, sleeps, raised = self.run_with([exc, None])
                self.assertIs(raised, exc)
                self.assertEqual((len(calls), sleeps), (1, []))

    def test_a_working_rename_is_one_call(self):
        """The POSIX path (renames over open files work): one os.replace,
        no wait — as before the helper."""
        d = _scratch_dir(self)
        src, dst = os.path.join(d, "new"), os.path.join(d, "old")
        for path, text in ((src, "new"), (dst, "old")):
            with open(path, "w") as fh:
                fh.write(text)
        real, calls = os.replace, []
        with mock.patch.object(ta.os, "replace",
                               side_effect=lambda a, b: calls.append(b)
                               or real(a, b)), \
             mock.patch.object(ta.time, "sleep",
                               side_effect=AssertionError("waited")):
            ta.replace_with_retry(src, dst)
        self.assertEqual(calls, [dst])
        with open(dst) as fh:
            self.assertEqual(fh.read(), "new")
        self.assertFalse(os.path.exists(src))


class RefusedRenameKeepsThePool(unittest.TestCase):
    """``_place_built_tiles``: a rename into the pool that Windows still
    refuses after the retries leaves the pool as it was — the previous tile
    (or bound ``.part`` pair) and its ``.src`` stay, only the new file goes,
    and the tile is returned as not built (the caller flags it)."""

    def setUp(self):
        self.root = _scratch_dir(self)
        self.pool = os.path.join(self.root, "pool")
        self.incoming = os.path.join(self.pool, ".incoming-1")
        os.makedirs(self.incoming)
        _bbox, self.spec = _spec()
        self.logs = []
        for p in (mock.patch.object(ta, "_log", side_effect=self.logs.append),
                  mock.patch.object(ta.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)

    def _abt(self, path, mark):
        """A whole .abt of the spec carrying *mark* (to tell copies apart)."""
        _sparse_abt(path, self.spec)
        with open(path, "r+b") as fh:
            fh.seek(abt.HEADER_SIZE + 16)
            fh.write(mark)

    def _mark(self, path):
        with open(path, "rb") as fh:
            fh.seek(abt.HEADER_SIZE + 16)
            return fh.read(3)

    def place(self, regions=None, refusals=None):
        """Place the incoming build; *refusals*: how often the rename onto
        the pool tile is refused (None = every time)."""
        self._abt(os.path.join(self.incoming, TILE), b"NEW")
        verdict = ta.PoolTile(False, None, regions is not None, regions,
                              "stale")
        real, tries = os.replace, []

        def fake(src, dst):
            if os.path.basename(dst) in (TILE, TILE + ta._PART_SUFFIX):
                tries.append(dst)
                if refusals is None or len(tries) <= refusals:
                    raise _refused(src, dst)
            return real(src, dst)
        with mock.patch.object(ta.os, "replace", side_effect=fake):
            failed = ta._place_built_tiles(
                self.pool, self.incoming, [self.spec], {TILE: verdict},
                set(), (lambda t, r: "fp-new") if regions else None)
        return failed, tries

    def test_a_transient_refusal_still_places_the_tile(self):
        canonical = os.path.join(self.pool, TILE)
        self._abt(canonical, b"OLD")
        failed, tries = self.place(refusals=2)
        self.assertEqual(failed, set())
        self.assertEqual(len(tries), 3)
        self.assertEqual(self._mark(canonical), b"NEW")
        self.assertEqual(self.logs, [])

    def test_a_persistent_refusal_keeps_the_previous_tile(self):
        canonical = os.path.join(self.pool, TILE)
        self._abt(canonical, b"OLD")
        with open(canonical + ta._SOURCE_FP_SUFFIX, "w") as fh:
            fh.write("fp-old")
        failed, tries = self.place()
        self.assertEqual(failed, {TILE})
        self.assertEqual(len(tries), 5)
        self.assertEqual(self._mark(canonical), b"OLD")
        self.assertEqual(ta._read_source_fp(self.pool, TILE), "fp-old")
        self.assertFalse(os.path.exists(os.path.join(self.incoming, TILE)))
        self.assertEqual(len(self.logs), 1)
        self.assertIn(f"could not move {TILE} into the pool", self.logs[0])
        self.assertIn("the previous pool copy is kept, flagged for rebuild",
                      self.logs[0])

    def test_a_refused_part_keeps_the_previous_part_bound(self):
        ra = _region()
        part, meta = ta._part_paths(self.pool, TILE)
        self._abt(part, b"OLD")
        self.assertTrue(ta._write_part_meta(self.pool, self.spec,
                                            [{"bbox": ra, "fp": "fp-old"}]))
        with open(meta, "rb") as fh:
            old_meta = fh.read()
        failed, _tries = self.place(regions=[ra, _halves(self.spec)[1]])
        self.assertEqual(failed, {TILE})
        self.assertEqual(self._mark(part), b"OLD")
        with open(meta, "rb") as fh:
            self.assertEqual(fh.read(), old_meta, "the sidecar was not restored")
        bound = ta._read_part_meta(self.pool, self.spec,
                                   ta._pool_listing(self.pool))
        self.assertEqual(bound["regions"], [{"bbox": ra, "fp": "fp-old"}])
        self.assertFalse(os.path.exists(os.path.join(self.pool, TILE)))

    def test_with_nothing_before_it_is_the_hole_it_always_was(self):
        failed, _tries = self.place()
        self.assertEqual(failed, {TILE})
        self.assertEqual([n for n in os.listdir(self.pool)
                          if not n.startswith(".incoming")], [])
        self.assertTrue(self.logs[0].endswith(") — flagged for rebuild"),
                        self.logs)

    def test_a_part_placed_after_a_refusal_gets_its_own_sidecar(self):
        ra = _region()
        part, _meta = ta._part_paths(self.pool, TILE)
        self._abt(part, b"OLD")
        ta._write_part_meta(self.pool, self.spec, [{"bbox": ra, "fp": "old"}])
        failed, _tries = self.place(regions=[ra], refusals=1)
        self.assertEqual(failed, set())
        self.assertEqual(self._mark(part), b"NEW")
        bound = ta._read_part_meta(self.pool, self.spec,
                                   ta._pool_listing(self.pool))
        self.assertEqual(bound["regions"], [{"bbox": ra, "fp": "fp-new"}])


def _find_real_converter():
    bin_dir = os.environ.get("AETHER_BIN_DIR", "")
    if bin_dir:
        for name in ("aether_converter", "aether_converter.exe"):
            p = os.path.join(bin_dir, name)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return shutil.which("aether_converter")


#: The engine runner itself, for the real-converter case.
_REAL_RUNNER = ta.run_converter_streaming

#: (ul_lat, ul_lon, size, res_deg) -> LV95 (E, N) of every cell centre.
_CENTRES: dict = {}


def _lv95_centres(ul_lat, ul_lon, size, res_deg):
    key = (ul_lat, ul_lon, size, res_deg)
    if key not in _CENTRES:
        a = OSR.SpatialReference()
        a.ImportFromEPSG(4326)
        b = OSR.SpatialReference()
        b.ImportFromEPSG(2056)
        for s in (a, b):
            s.SetAxisMappingStrategy(OSR.OAMS_TRADITIONAL_GIS_ORDER)
        ct = OSR.CoordinateTransformation(a, b)
        # Transformed on every 4th cell, bilinear in between: millimetres
        # off on a smooth projection — the model only needs ONE fixed
        # answer per tile geometry (both builds of a tile share it), and the
        # plugin's file admission has a halo of whole source pixels.
        lat_idx = np.unique(np.r_[np.arange(0, size, 4), size - 1])
        idx = np.arange(size)
        lon, lat = np.meshgrid(ul_lon + (lat_idx + 0.5) * res_deg,
                               ul_lat - (lat_idx + 0.5) * res_deg)
        out = np.asarray(ct.TransformPoints(
            np.column_stack([lon.ravel(), lat.ravel()])))
        n = len(lat_idx)

        def dense(v):
            v = v.reshape(n, n)
            rows = np.array([np.interp(idx, lat_idx, v[i]) for i in range(n)])
            return np.array([np.interp(idx, lat_idx, rows[:, j])
                             for j in range(size)]).T
        east, north = dense(out[:, 0]), dense(out[:, 1])
        # Per row / column ranges: an exact superset filter for a source's
        # cell window (only speed — the containment test stays per cell).
        _CENTRES[key] = (east, north,
                         north.min(axis=1), north.max(axis=1),
                         east.min(axis=0), east.max(axis=0))
    return _CENTRES[key]


def _write_fgb(path, lat, lon, height=12.0):
    """A minimal valid FlatGeobuf at *path*: one 20 m square building at
    *lat*/*lon* (WGS84) with a ``height`` attribute. The real converter
    refuses a buildings file it cannot read rather than write a
    building-less tile, so a test that requests buildings needs a real one;
    the model ignores it. Written with the real OGR (callers run under
    :func:`real_gdal`)."""
    from osgeo import ogr, osr       # the real modules inside real_gdal()
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ds = ogr.GetDriverByName("FlatGeobuf").CreateDataSource(path)
    layer = ds.CreateLayer("buildings", srs, ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("height", ogr.OFTReal))
    d = 0.0002
    feat = ogr.Feature(layer.GetLayerDefn())
    feat.SetGeometry(ogr.CreateGeometryFromWkt(
        f"POLYGON(({lon} {lat},{lon + d} {lat},{lon + d} {lat + d},"
        f"{lon} {lat + d},{lon} {lat}))"))
    feat.SetField("height", height)
    layer.CreateFeature(feat)
    feat = layer = ds = None         # flush and close
    return path


def _model_ingest(job):
    """A converter model fed ONLY the job: each cell takes the first listed
    source (priority order) whose pixels contain the cell centre, nearest
    sample; no source = void_fill_m. Writes the .abt at output_path."""
    size = int(job["size_px"])
    res_deg = job["resolution_m"] / 111_111.0
    (east, north, n_lo, n_hi, e_lo, e_hi) = _lv95_centres(
        job["ul_lat"], job["ul_lon"], size, res_deg)
    out = np.full((size, size), np.nan)
    for src in job["sources"]:
        assert src.get("crs") == "EPSG:2056", src
        ds = GDAL.Open(src["path"])
        gt = ds.GetGeoTransform()
        w, n = gt[0], gt[3]
        e, s = w + gt[1] * ds.RasterXSize, n + gt[5] * ds.RasterYSize
        rows = np.nonzero((n_hi >= s) & (n_lo <= n))[0]
        cols = np.nonzero((e_hi >= w) & (e_lo <= e))[0]
        if not len(rows) or not len(cols):
            continue
        win = (slice(rows[0], rows[-1] + 1), slice(cols[0], cols[-1] + 1))
        arr = ds.ReadAsArray().astype(float)
        col = np.floor((east[win] - gt[0]) / gt[1])
        row = np.floor((north[win] - gt[3]) / gt[5])
        sub = out[win]
        hit = ((col >= 0) & (col < ds.RasterXSize) & (row >= 0)
               & (row < ds.RasterYSize) & np.isnan(sub))
        sub[hit] = arr[row[hit].astype(int), col[hit].astype(int)]
        ds = None
    fill = float(job.get("void_fill_m", -5000.0))
    counts = np.round(np.where(np.isnan(out), fill, out) * 2).astype("<i2")
    stride = (size * 2 + 255) & ~255
    body = np.zeros((size, stride), dtype=np.uint8)
    body[:, :size * 2] = counts.view(np.uint8).reshape(size, size * 2)
    with open(job["output_path"], "wb") as fh:
        fh.write(b"AETH" + struct.pack("<HH", 1, size)
                 + struct.pack("<dddd", job["ul_lat"], job["ul_lon"],
                               res_deg, res_deg)
                 + struct.pack("<hH", 0, stride))
        fh.write(body.tobytes())


def _abt_samples(path):
    hdr = abt.read_header(path)
    raw = np.fromfile(path, dtype=np.uint8, offset=abt.HEADER_SIZE,
                      count=hdr.size * hdr.stride)
    return raw.reshape(hdr.size, hdr.stride)[:, :hdr.size * 2].view("<i2")


def _window(spec, region):
    res_deg = spec["exact_res_m"] / 111_111.0
    c0 = round((region["west"] - spec["ul_lon"]) / res_deg)
    c1 = round((region["east"] - spec["ul_lon"]) / res_deg)
    r0 = round((spec["ul_lat"] - region["north"]) / res_deg)
    r1 = round((spec["ul_lat"] - region["south"]) / res_deg)
    return slice(r0, r1), slice(c0, c1)


@unittest.skipUnless(HAVE_GDAL, "needs the real GDAL")
class PartialBuildsEndToEnd(unittest.TestCase):
    """Real folder index, real job building, real pool / view files; the
    conversion itself is the real converter when AETHER_BIN_DIR / PATH
    provide one, else :func:`_model_ingest` behind run_converter_streaming."""

    #: 50 km LV95 files at 500 m over the whole 1 deg tile, plus two 50 m
    #: files: one inside site A's region, one far outside every region.
    GRID_E = range(2_550_000, 2_650_000, 50_000)
    GRID_N = range(1_050_000, 1_250_000, 50_000)
    FINE_IN = (2_588_000, 2_598_000, 1_172_000, 1_182_000)
    FINE_OUT = (2_630_000, 2_640_000, 1_100_000, 1_110_000)

    def setUp(self):
        self.root = _scratch_dir(self)
        self.dem = os.path.join(self.root, "dem")
        os.makedirs(self.dem)
        self.cache = os.path.join(self.root, "cache")
        p = mock.patch.object(ta, "get_cache_dir", side_effect=lambda: self.cache)
        p.start()
        self.addCleanup(p.stop)
        self._gdal = real_gdal()
        self._gdal.__enter__()
        self.addCleanup(self._gdal.__exit__, None, None, None)
        ti.forget_indexes()
        ti.clear_approvals()
        self.addCleanup(ti.forget_indexes)
        self.addCleanup(ti.clear_approvals)
        k = 0
        for e in self.GRID_E:
            for n in self.GRID_N:
                self._tif(f"g_{e}_{n}.tif", (e, e + 50_000, n, n + 50_000),
                          400.0 + 10 * k, 100)
                k += 1
        self._tif("fine_in.tif", self.FINE_IN, 2000.0, 200)
        self._tif("fine_out.tif", self.FINE_OUT, 3000.0, 200)
        self.real = _find_real_converter()
        self.runs = []                      # jobs per converter launch

    def _tif(self, name, box, base, px, value_shift=0.0):
        w, e, s, n = box
        rows, cols = np.mgrid[0:px, 0:px]
        write_tif(os.path.join(self.dem, name),
                  {"west": w, "east": e, "south": s, "north": n}, epsg=2056,
                  size=(px, px),
                  array=(base + value_shift + 0.5 * rows + 0.25 * cols
                         ).astype(np.float32))

    def _runner(self, exe, args, on_line, on_start=None, env=None):
        """Every ingest launch is recorded; the real converter runs when one
        is provided, else the model."""
        job_file = args[args.index("--job-file") + 1]
        with open(job_file) as fh:
            jobs = json.load(fh)
        self.runs.append(jobs)
        if self.real:
            return _REAL_RUNNER(exe, args, on_line, on_start=on_start, env=env)
        for job in jobs:
            _model_ingest(job)
        on_line(f"Progress: {len(jobs)}/{len(jobs)}")
        return 0

    def _prepare(self, site=SITE_A, rng=RANGE_KM, whole=False, **kw):
        bm = mock.Mock()
        bm.find_binary.return_value = self.real or "aether_converter"
        patches = [mock.patch.object(ta, "run_converter_streaming",
                                     side_effect=self._runner)]
        if whole:
            patches.append(mock.patch.object(ta, "_tile_region",
                                             return_value=None))
        logged = []
        patches.append(mock.patch.object(ta, "_log", side_effect=logged.append))
        for p in patches:
            p.start()
        try:
            ta.reset_terrain_warnings()
            view = ta.prepare_terrain(None, site[0], site[1], rng, RES, bm,
                                      terrain_dir=self.dem, **kw)
        finally:
            for p in reversed(patches):
                p.stop()
        self.log = "\n".join(logged)
        jobs = None
        job_file = os.path.join(view, "batch_job.json")
        if os.path.exists(job_file) and "cache MISS" in self.log:
            with open(job_file) as fh:
                jobs = {os.path.basename(j["output_path"]): j
                        for j in json.load(fh)}
        return view, jobs

    def _pool(self):
        return ta._pool_dir(self.dem, None)

    def _infos(self):
        infos = ta.folder_source_infos(self.dem)
        return infos, ta.FootprintIndex(infos)

    def test_bytes_inside_the_region_equal_a_whole_build(self):
        _bbox, spec = _spec()
        ra = _region()
        view_p, jobs_p = self._prepare()
        pool = self._pool()
        part = os.path.join(pool, TILE + ".part")
        tile_p = os.path.join(self.root, "partial.abt")
        shutil.copyfile(part, tile_p)
        self.assertIn("1 partial: region", self.log)
        self.assertIn("deg²", self.log)
        # Pool: a .part pair, never the canonical name.
        self.assertFalse(os.path.exists(os.path.join(pool, TILE)))
        self.assertFalse(ta._tile_ready(pool, TILE))
        with open(part + ".json") as fh:
            meta = json.load(fh)
        infos, fidx = self._infos()
        self.assertEqual([r["bbox"] for r in meta["regions"]], [ra])
        self.assertEqual(meta["regions"][0]["fp"],
                         ta.tile_source_fingerprint(infos, ra,
                                                    spec["exact_res_m"],
                                                    index=fidx))
        self.assertEqual(meta["abt_size"], os.path.getsize(part))
        self.assertEqual(meta["extent"], ta._subtile_bbox(spec))
        # The view holds it under the canonical name.
        self.assertTrue(os.path.samefile(os.path.join(view_p, TILE), part))
        self.assertEqual([n for n in os.listdir(view_p) if n.endswith(".abt")],
                         [TILE])

        # The same site, built whole (fresh cache).
        self.cache = os.path.join(self.root, "cache_whole")
        ti.forget_indexes()
        _view_w, jobs_w = self._prepare(whole=True)
        tile_w = os.path.join(self._pool(), TILE)
        self.assertTrue(ta._tile_ready(self._pool(), TILE))

        jp, jw = jobs_p[TILE], jobs_w[TILE]
        for k in ("ul_lat", "ul_lon", "size_px", "resolution_m",
                  "void_fill_m", "format"):
            self.assertEqual(jp[k], jw[k], k)
        src_p = [s["path"] for s in jp["sources"]]
        src_w = [s["path"] for s in jw["sources"]]
        self.assertLess(len(src_p), len(src_w))
        self.assertEqual(src_p, [s for s in src_w if s in set(src_p)],
                         "priority order must be the whole build's")
        want = [os.path.abspath(i["path"]) for _i, i, _h in
                ta._tile_contributors(infos, ra, spec["exact_res_m"],
                                      index=fidx)]
        self.assertEqual(src_p, want, "the region's contributors, exactly")
        self.assertIn(os.path.join(self.dem, "fine_in.tif"), src_p)
        self.assertNotIn(os.path.join(self.dem, "fine_out.tif"), src_p)
        self.assertIn(os.path.join(self.dem, "fine_out.tif"), src_w)

        a, b = _abt_samples(tile_p), _abt_samples(tile_w)
        rows, cols = _window(spec, ra)
        self.assertTrue(np.array_equal(a[rows, cols], b[rows, cols]),
                        "bytes inside R differ from the whole build")
        self.assertFalse(np.array_equal(a, b), "the .part is not partial")
        # Outside R the partial tile is the void fill (0 m) where none of
        # its sources reach: the far fine file is not in it.
        fr, fc = _window(spec, {"west": 7.86, "east": 7.9,
                                "north": 46.13, "south": 46.08})
        self.assertTrue((a[fr, fc] == 0).all())
        self.assertTrue((b[fr, fc] >= 6000).all())

    def test_transitions_hit_union_promote(self):
        _bbox, spec = _spec()
        ra, rb = _region(SITE_A), _region(SITE_B)
        pool = self._pool()
        view_a, _ = self._prepare(SITE_A)
        self.assertEqual(len(self.runs), 1)
        first = os.stat(os.path.join(pool, TILE + ".part")).st_ino

        again, _ = self._prepare(SITE_A)                    # hit
        self.assertEqual((again, len(self.runs)), (view_a, 1))
        self.assertIn("cache HIT", self.log)
        self.assertIn("(1 partial)", self.log)

        view_b, jobs = self._prepare(SITE_B)                # outside: union
        self.assertNotEqual(view_b, view_a)
        self.assertEqual(len(self.runs), 2)
        infos, fidx = self._infos()
        union = ta._merge_regions([ra, rb])
        want = [os.path.abspath(i["path"]) for _i, i, _h in
                ta._region_contributors(infos, union, spec["exact_res_m"],
                                        index=fidx)]
        self.assertEqual([s["path"] for s in jobs[TILE]["sources"]], want)
        with open(os.path.join(pool, TILE + ".part.json")) as fh:
            self.assertEqual([r["bbox"] for r in json.load(fh)["regions"]],
                             union)
        part = os.path.join(pool, TILE + ".part")
        self.assertNotEqual(os.stat(part).st_ino, first,
                            "the .part must be replaced, not rewritten")
        # A's old view still holds the old inode until A re-syncs.
        self.assertEqual(os.stat(os.path.join(view_a, TILE)).st_ino, first)
        self.assertEqual(self._prepare(SITE_A)[0], view_a)   # hit, relinked
        self.assertEqual(len(self.runs), 2)
        self.assertTrue(os.path.samefile(os.path.join(view_a, TILE), part))

        # A job whose union covers the tile builds it whole: canonical name
        # + .src, the .part pair gone; A's view follows on its next sync.
        tb = ta._subtile_bbox(spec)
        self._prepare(SITE_A, region_union={TILE: [tb]},
                      rng=RANGE_KM + 1.0)
        self.assertEqual(len(self.runs), 3)
        whole = os.path.join(pool, TILE)
        self.assertTrue(ta._tile_ready(pool, TILE))
        self.assertFalse(os.path.exists(part))
        self.assertFalse(os.path.exists(part + ".json"))
        with open(whole + ".src") as fh:
            self.assertEqual(fh.read(), ta.tile_source_fingerprint(
                infos, tb, spec["exact_res_m"], index=fidx))
        self.assertEqual(self._prepare(SITE_A)[0], view_a)
        self.assertEqual(len(self.runs), 3, "a complete tile is a hit")
        self.assertTrue(os.path.samefile(os.path.join(view_a, TILE), whole))

    def test_a_changed_file_rebuilds_only_the_regions_it_feeds(self):
        pool = self._pool()
        self._prepare(SITE_A)
        # fine_out feeds the whole tile, not site A's region.
        path = os.path.join(self.dem, "fine_out.tif")
        self._tif("fine_out.tif", self.FINE_OUT, 3000.0, 200, value_shift=5)
        os.utime(path, ns=(time.time_ns(), time.time_ns() + 10**9))
        self._prepare(SITE_A)
        self.assertEqual(len(self.runs), 1, "a file outside R made it stale")
        path = os.path.join(self.dem, "fine_in.tif")
        self._tif("fine_in.tif", self.FINE_IN, 2000.0, 200, value_shift=5)
        os.utime(path, ns=(time.time_ns(), time.time_ns() + 2 * 10**9))
        self._prepare(SITE_A)
        self.assertEqual(len(self.runs), 2)
        self.assertIn("stale", self.log)
        self.assertTrue(os.path.exists(os.path.join(pool, TILE + ".part")))

    def test_rebuild_flags_are_honoured(self):
        pool = self._pool()
        view, _ = self._prepare(SITE_A)
        ta._set_rebuild_flags(pool, [TILE], {TILE})
        self._prepare(SITE_A)
        self.assertEqual(len(self.runs), 2, "a flagged .part was reused")
        self.assertFalse(os.path.exists(os.path.join(pool, TILE + ".rebuild")))
        # Buildings requested but not applied: flagged, still served.
        with mock.patch.object(ta, "_buildings_complaint",
                               return_value="no such file"), \
             mock.patch("waveshed.core.buildings_source."
                        "resolve_buildings_source",
                        side_effect=lambda p, **_kw: p), \
             mock.patch("waveshed.core.buildings_source.is_converter_readable",
                        return_value=True):
            fgb = _write_fgb(os.path.join(self.root, "b.fgb"), *SITE_A)
            view_b, _ = self._prepare(SITE_A, buildings_file=fgb)
        bpool = ta._pool_dir(self.dem, ta.buildings_identity(fgb))
        self.assertTrue(os.path.exists(os.path.join(bpool, TILE + ".rebuild")))
        self.assertTrue(os.path.samefile(os.path.join(view_b, TILE),
                                         os.path.join(bpool, TILE + ".part")))

    def test_one_run_builds_a_shared_tile_once_for_all_its_jobs(self):
        from waveshed.core.job_builder import CoverageParams
        union = ta.run_region_union([
            CoverageParams(tx_lat=s[0], tx_lon=s[1], resolution_m=RES,
                           max_range_km=RANGE_KM) for s in (SITE_A, SITE_B)])
        view_a, _ = self._prepare(SITE_A, region_union=union)
        view_b, _ = self._prepare(SITE_B, region_union=union)
        self.assertEqual(len(self.runs), 1, "the shared tile was built twice")
        self.assertIn("cache HIT", self.log)
        self.assertNotEqual(view_a, view_b)
        part = os.path.join(self._pool(), TILE + ".part")
        for v in (view_a, view_b):
            self.assertTrue(os.path.samefile(os.path.join(v, TILE), part))

    def test_sync_view_never_keeps_a_link_whose_source_is_gone(self):
        _bbox, spec = _spec()
        ra = _region()
        view, _ = self._prepare(SITE_A)
        pool = self._pool()
        part = os.path.join(pool, TILE + ".part")
        tiles = {TILE: (spec, ra)}
        # Rebuilt by temp + rename (newer file): relinked.
        tmp = part + ".new"
        shutil.copyfile(part, tmp)
        os.replace(tmp, part)
        later = os.stat(part).st_mtime_ns + 10**9
        os.utime(part, ns=(later, later))
        ta._write_part_meta(pool, spec, [{"bbox": ra, "fp": "fp"}])
        self.assertFalse(os.path.samefile(os.path.join(view, TILE), part))
        self.assertEqual(ta._sync_view(pool, view, [TILE], tiles=tiles), [])
        self.assertTrue(os.path.samefile(os.path.join(view, TILE), part))
        # Source gone: the view copy is dropped, the name reported — never
        # treated as fresh (the canonical path keeps its old rule).
        os.remove(part)
        self.assertEqual(ta._sync_view(pool, view, [TILE], tiles=tiles),
                         [TILE])
        self.assertFalse(os.path.exists(os.path.join(view, TILE)))

    def test_profile_bands_link_their_parts_through_the_resolver(self):
        bands = [(8000.0, 90), (20000.0, 250)]
        bm = mock.Mock()
        bm.find_binary.return_value = self.real or "aether_converter"
        patches = [mock.patch.object(ta, "run_converter_streaming",
                                     side_effect=self._runner)]
        for p in patches:
            p.start()
        try:
            ta.reset_terrain_warnings()
            view = ta.prepare_terrain_profile(None, *SITE_A, bands, bm,
                                              terrain_dir=self.dem,
                                              output_res_m=90)
            pool = self._pool()
            names = sorted(n for n in os.listdir(view) if n.endswith(".abt"))
            self.assertEqual(names, ["tile_N47.00E7.00_90m.abt",
                                     "tile_N48.00E6.00_250m.abt"])
            for n in names:
                self.assertTrue(os.path.samefile(
                    os.path.join(view, n), os.path.join(pool, n + ".part")), n)
                self.assertFalse(os.path.exists(os.path.join(pool, n)))
            # The view key carries the regions: another site, another view;
            # the same site again, the same view and no build.
            n_runs = len(self.runs)
            again = ta.prepare_terrain_profile(None, *SITE_A, bands, bm,
                                               terrain_dir=self.dem,
                                               output_res_m=90)
            self.assertEqual((again, len(self.runs)), (view, n_runs))
            other = ta.prepare_terrain_profile(None, 46.7, 7.3, bands, bm,
                                               terrain_dir=self.dem,
                                               output_res_m=90)
            self.assertNotEqual(other, view)
        finally:
            for p in reversed(patches):
                p.stop()

    def test_a_refused_rename_keeps_the_previous_part_and_flags_it(self):
        """Windows refuses the rename over a ``.part`` another process holds
        open: after the retries the run keeps the pool's previous copy
        (bytes, sidecar binding), links it, and flags the tile — the next
        run rebuilds it."""
        import hashlib
        _bbox, spec = _spec()
        ra = _region()
        pool = self._pool()
        part = os.path.join(pool, TILE + ".part")
        view, _ = self._prepare(SITE_A)

        def digest():
            with open(part, "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()
        old = digest()
        with open(part + ".json", "rb") as fh:
            old_meta = fh.read()
        path = os.path.join(self.dem, "fine_in.tif")     # feeds A's region
        self._tif("fine_in.tif", self.FINE_IN, 2000.0, 200, value_shift=5)
        os.utime(path, ns=(time.time_ns(), time.time_ns() + 10**9))
        real_replace = os.replace
        refused = []

        def held_open(src, dst):
            if os.path.basename(dst) == TILE + ".part":
                refused.append(dst)
                raise PermissionError(13, "The process cannot access the file "
                                      "because it is being used by another "
                                      "process", dst)
            return real_replace(src, dst)
        with mock.patch.object(ta.os, "replace", side_effect=held_open), \
             mock.patch.object(ta.time, "sleep"):
            view2, _ = self._prepare(SITE_A)
        self.assertEqual(len(self.runs), 2, "the stale tile was built")
        self.assertEqual(len(refused), 5, "five attempts")
        self.assertEqual(digest(), old, "the previous .part was destroyed")
        with open(part + ".json", "rb") as fh:
            self.assertEqual(fh.read(), old_meta)
        meta = ta._read_part_meta(pool, spec, ta._pool_listing(pool))
        self.assertEqual([r["bbox"] for r in meta["regions"]], [ra],
                         "the previous .part is no longer bound")
        self.assertTrue(os.path.exists(os.path.join(pool, TILE + ".rebuild")))
        self.assertIn(f"could not move {TILE} into the pool", self.log)
        self.assertIn("the previous pool copy is kept, flagged for rebuild",
                      self.log)
        self.assertEqual([n for n in os.listdir(pool)
                          if n.startswith(".incoming-")], [])
        self.assertTrue(os.path.samefile(os.path.join(view2, TILE), part),
                        "the run did not get the previous copy")
        # The rename works again: rebuilt (the flag), replaced, flag gone.
        self._prepare(SITE_A)
        self.assertEqual(len(self.runs), 3)
        self.assertNotEqual(digest(), old)
        self.assertFalse(os.path.exists(os.path.join(pool, TILE + ".rebuild")))
        self.assertIsNotNone(ta._read_part_meta(pool, spec,
                                                ta._pool_listing(pool)))

    def test_plan_prices_the_region_not_the_tile(self):
        infos, fidx = self._infos()
        src = (infos, False, fidx)
        before = ta.terrain_plan(self.dem, *SITE_A, RANGE_KM, RES,
                                 source_infos=src)
        guess = ta.terrain_plan(self.dem, *SITE_A, RANGE_KM, RES)
        self.assertEqual((before["tiles_missing"], before["tiles_partial"]),
                         (1, 1))
        self.assertEqual(guess["tiles_partial"], 1)
        self.assertIsNone(guess["source_files"])
        self.assertAlmostEqual(before["build_deg2"],
                               ta._bbox_area(_region()))
        self.assertAlmostEqual(before["tile_deg2"], 1.0, places=3)
        _bbox, spec = _spec()
        n_region = len(ta._tile_contributors(infos, _region(),
                                             spec["exact_res_m"], index=fidx))
        n_tile = len(ta._tile_contributors(infos, ta._subtile_bbox(spec),
                                           spec["exact_res_m"], index=fidx))
        self.assertEqual(before["source_files"], n_region)
        self.assertLess(n_region, n_tile)
        self._prepare(SITE_A)
        after = ta.terrain_plan(self.dem, *SITE_A, RANGE_KM, RES,
                                source_infos=src)
        self.assertEqual((after["tiles_cached"], after["tiles_missing"]), (1, 0))
        self.assertEqual(ta.terrain_plan(self.dem, *SITE_B, RANGE_KM, RES,
                                         source_infos=src)["tiles_missing"], 1)
        prof = ta.terrain_plan_profile(self.dem, *SITE_A,
                                       [(8000.0, 90), (20000.0, 250)],
                                       source_infos=src, output_res_m=90)
        self.assertEqual(prof["tiles_partial"], 1)    # the 250 m tile
        self.assertEqual(prof["tiles_cached"], 1)     # A's 90 m .part


# ---------------------------------------------------------------------------
# The Site Analysis worker computes the run's union before the first job
# ---------------------------------------------------------------------------

class WorkerPassesTheRunUnion(unittest.TestCase):

    def test_union_of_all_jobs_reaches_the_first_prepare(self):
        from waveshed.core import binary_manager as bm
        from waveshed.core import range_profile as rp
        from waveshed.core.job_builder import CoverageParams
        import waveshed.gui.site_analysis_tab as sat
        jobs = [(CoverageParams(tx_lat=SITE_A[0], tx_lon=SITE_A[1],
                                resolution_m=RES, max_range_km=RANGE_KM,
                                output_name="a"), "A"),
                (CoverageParams(tx_lat=SITE_B[0], tx_lon=SITE_B[1],
                                resolution_m=RES, max_range_km=RANGE_KM,
                                output_name="b",
                                range_profile=rp.adaptive_bands(30, 15)),
                 "B")]
        seen = {}

        def fake_prepare(**kw):
            seen["fixed"] = kw
            raise ta.ConverterCancelled()

        with tempfile.TemporaryDirectory(dir=_scratch_dir(self)) as d:
            worker = sat._SiteAnalysisWorker(jobs, mock.Mock(),
                                             os.path.join(d, "out"))
            with mock.patch.object(sat, "prepare_terrain",
                                   side_effect=fake_prepare), \
                 mock.patch.object(sat, "run_region_union",
                                   wraps=ta.run_region_union) as union, \
                 mock.patch.object(bm, "check_engine_for_job",
                                   return_value="0.4.10"):
                worker.run()
        union.assert_called_once()
        self.assertEqual(list(union.call_args.args[0]), [p for p, _ in jobs])
        got = seen["fixed"]["region_union"]
        self.assertEqual(got, ta.run_region_union([p for p, _ in jobs]))
        self.assertIn(_region(SITE_A), got[TILE])
        # The profile job's 90 m band is in it too (site B's ground).
        self.assertTrue(any(r["south"] < 46.5 for r in got[TILE]))

    def test_profile_job_gets_union_and_output_pixel(self):
        from waveshed.core import binary_manager as bm
        from waveshed.core import range_profile as rp
        from waveshed.core.job_builder import CoverageParams
        import waveshed.gui.site_analysis_tab as sat
        params = CoverageParams(tx_lat=SITE_A[0], tx_lon=SITE_A[1],
                                resolution_m=RES, max_range_km=RANGE_KM,
                                range_profile=rp.adaptive_bands(30, 15))
        seen = {}

        def fake_profile(**kw):
            seen.update(kw)
            raise ta.ConverterCancelled()

        with tempfile.TemporaryDirectory(dir=_scratch_dir(self)) as d:
            worker = sat._SiteAnalysisWorker([(params, "A")], mock.Mock(),
                                             os.path.join(d, "out"))
            with mock.patch.object(sat, "prepare_terrain_profile",
                                   side_effect=fake_profile), \
                 mock.patch.object(bm, "check_engine_for_job",
                                   return_value="0.4.10"):
                worker.run()
        self.assertEqual(seen["output_res_m"], RES)
        self.assertEqual(seen["region_union"], ta.run_region_union([params]))

    def test_profile_hands_the_union_to_every_band(self):
        from waveshed.core import range_profile as rp
        bands = rp.adaptive_bands(30, 15)
        calls = []
        union = {"x": []}
        infos = [{"path": "a.tif", "halo_deg": 0.001,
                  "wgs84_bounds": {"west": 7, "east": 8, "south": 46,
                                   "north": 47}}]
        with tempfile.TemporaryDirectory(dir=_scratch_dir(self)) as cache, \
             mock.patch.object(ta, "prepare_terrain",
                               side_effect=lambda *a, **kw: calls.append(kw)
                               or "v"), \
             mock.patch.object(ta, "get_cache_dir", return_value=cache), \
             mock.patch.object(ta, "_read_source_once",
                               return_value=(infos, False, None)), \
             mock.patch.object(ta, "_sync_view", return_value=[]) as sync, \
             mock.patch.object(ta, "_record_view_dir"):
            ta.prepare_terrain_profile(None, *SITE_A, bands, None,
                                       terrain_dir="/folder",
                                       output_res_m=RES, region_union=union)
        self.assertTrue(calls)
        for kw in calls:
            self.assertIs(kw["region_union"], union)
            self.assertEqual(kw["output_res_m"], RES)
        # Linked through the resolver: every band tile with its region.
        linked = {}
        for call in sync.call_args_list:
            linked.update(call.kwargs["tiles"])
        self.assertTrue(linked)
        self.assertTrue(any(r is not None for _t, r in linked.values()))


class SiteAnalysisEstimate(unittest.TestCase):
    """The Site Analysis pre-run estimate prices what the worker builds."""

    def test_estimate_prices_the_runs_regions_and_logs_them(self):
        from waveshed.core.job_builder import CoverageParams
        import waveshed.gui.site_analysis_tab as sat
        root = _scratch_dir(self)
        folder = os.path.join(root, "dem")
        os.makedirs(folder)
        jobs = [CoverageParams(tx_lat=s[0], tx_lon=s[1], resolution_m=RES,
                               max_range_km=RANGE_KM) for s in (SITE_A, SITE_B)]
        union = ta.run_region_union(jobs)
        with mock.patch.object(ta, "get_cache_dir", return_value=root):
            totals = sat._plan_jobs(jobs, folder, None, union)
            # Site A's region pooled: A is cached; B still builds the union.
            _bbox, spec = _spec()
            _write_part(ta._pool_dir(folder), spec, [_region(SITE_A)])
            later = sat._plan_jobs(jobs, folder, None, union)
        self.assertEqual((totals["tiles_missing"], totals["tiles_partial"]),
                         (2, 2))
        self.assertAlmostEqual(totals["build_deg2"], 2 * sum(
            ta._bbox_area(r) for r in union[TILE]))
        self.assertIsNone(totals["source_files"])
        self.assertEqual((later["tiles_cached"], later["tiles_missing"]),
                         (1, 1))
        with mock.patch.object(sat.QgsMessageLog, "logMessage") as log:
            sat._log_terrain_plan(0, 2, 6, 0, 2, totals["build_deg2"],
                                  totals["tile_deg2"], 3, 12)
        msg = log.call_args.args[0]
        self.assertIn("2 of them partial", msg)
        self.assertIn("deg²", msg)
        self.assertIn("3 source file(s)", msg)


if __name__ == "__main__":
    unittest.main()
