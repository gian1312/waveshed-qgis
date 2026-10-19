"""The run's one progress bar: parsers, the remaining-work model, the
download aggregator across retry passes, and the Map Converter extent
helpers (copy / union / whole folder)."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

import waveshed.core.terrain_adapter as ta
import waveshed.gui.map_converter_tab as mct
from waveshed.core import run_progress as rp
from waveshed.core import terrain_index


# ---------------------------------------------------------------------------
# Converter output, exactly as the toolkit prints it
# ---------------------------------------------------------------------------

def _converter_pass_lines(total: int, mbps: float = 8.5, errors_at=None):
    """The ``[Download]`` lines one converter pass prints for *total* tiles.

    Mirrors download.rs: a line whenever ``done*100/total`` crosses a
    multiple of 10, and at the end; the counters ride along.
    """
    errors_at = errors_at or {}
    lines, errs = [], 0
    for done in range(1, total + 1):
        errs = max(errs, errors_at.get(done, errs))
        pct, prev = done * 100 // total, (done - 1) * 100 // total
        if pct // 10 > prev // 10 or done == total:
            lines.append(f"[Download] {pct}% ({done}/{total}) — {mbps:.1f} "
                         f"MB/s, {errs} errors, {max(0, total - done)} "
                         f"in-flight")
    return lines


class TestParsers(unittest.TestCase):

    def test_download_line_all_fields(self):
        p = rp.parse_download_line(
            "[Download] 40% (1200/3000) — 12.3 MB/s, 7 errors, 64 in-flight")
        self.assertEqual(p, rp.DownloadLine(40, 1200, 3000, 12.3, 7, 64))

    def test_download_line_short_form_still_counts(self):
        p = rp.parse_download_line("[Download] 5% (1/20)")
        self.assertEqual((p.pct, p.done, p.total), (5, 1, 20))
        self.assertIsNone(p.mbps)

    def test_non_progress_lines(self):
        for line in ("[Download] first timeout: z=15/x=1/y=2",
                     "[Download] SLOW tile z=15/x=1/y=2: 6.1s",
                     "[Stats] Tiles: 500/500 OK (100.0% success)",
                     "[Download] z=12 tiles=6x5=30 (~5MB) connections=256"):
            self.assertIsNone(rp.parse_download_line(line), line)

    def test_header(self):
        self.assertEqual(rp.parse_download_header(
            "[Download] z=12 tiles=6x5=30 (~5MB) connections=256"), (12, 30))
        self.assertIsNone(rp.parse_download_header("[Download] 5% (1/20)"))

    def test_ingest(self):
        self.assertEqual(rp.parse_ingest_progress("[Rust] Progress: 30/62"),
                         (30, 62))

    def test_next_report_matches_the_converter_print_rule(self):
        for total in (1, 3, 4, 7, 10, 19, 30, 101, 997):
            printed = [int(line.split("(")[1].split("/")[0])
                       for line in _converter_pass_lines(total)]
            done = 0
            for want in printed:
                self.assertEqual(rp.next_download_report(done, total), want,
                                 (done, total))
                done = want

    def test_next_ingest_report(self):
        self.assertEqual(rp.next_ingest_report(0, 62), 10)
        self.assertEqual(rp.next_ingest_report(60, 62), 62)

    def test_eta_format(self):
        self.assertEqual(rp.format_eta(None), "")
        self.assertEqual(rp.format_eta(44.6), "ETA 45 s")
        self.assertEqual(rp.format_eta(200), "ETA 3 min 20 s")
        self.assertEqual(rp.format_eta(3900), "ETA 1 h 05 min")


# ---------------------------------------------------------------------------
# WorkProgress
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class TestWorkProgress(unittest.TestCase):

    def test_weighted_by_work_not_by_phase(self):
        m = rp.WorkProgress()
        m.set_work("download", 300)
        m.set_work("convert", 100)
        m.set_done("download", 150)
        self.assertAlmostEqual(m.fraction(), 150 / 400)
        m.finish("download")
        self.assertAlmostEqual(m.fraction(), 300 / 400)
        m.finish("convert")
        self.assertEqual(m.fraction(), 1.0)

    def test_added_work_slows_but_never_reverses_or_freezes(self):
        m = rp.WorkProgress()
        m.set_work("pass1", 100)
        m.set_work("tail", 1)
        m.set_done("pass1", 100)
        top = m.fraction()
        self.assertLess(top, 1.0)
        m.set_work("pass2", 50)              # a retry pass starts
        self.assertEqual(m.fraction(), top)  # never backwards
        last = top
        for done in range(5, 51, 5):
            f = m.set_done("pass2", done)
            self.assertGreater(f, last)      # never frozen
            last = f
        self.assertLess(last, 1.0)
        self.assertEqual(m.finish("tail"), 1.0)

    def test_lower_or_repeated_done_is_ignored(self):
        m = rp.WorkProgress()
        m.set_work("a", 10)
        m.set_done("a", 6)
        f = m.fraction()
        m.set_done("a", 2)
        m.set_done("a", 6)
        self.assertEqual(m.fraction(), f)

    def test_estimate_too_low_grows_with_the_report(self):
        m = rp.WorkProgress()
        m.set_work("a", 10)
        m.set_work("b", 10)
        m.set_done("a", 15)
        self.assertEqual(m.work("a"), 15)
        self.assertLess(m.fraction(), 1.0)

    def test_creep_is_capped_by_the_next_certain_report(self):
        clock = _Clock()
        m = rp.WorkProgress(clock)
        m.set_work("a", 1000)
        for i in range(1, 6):                # 10 units/s, measured
            clock.t = float(i)
            m.set_done("a", i * 10, ceiling=(i + 1) * 10)
        base = m.fraction()
        clock.t = 5.5
        mid = m.shown()
        self.assertGreater(mid, base)        # creeping between lines
        clock.t = 60.0                       # converter silent for a minute
        capped = m.shown()
        self.assertAlmostEqual(capped, 60 / 1000, places=6)
        clock.t = 61.0
        self.assertEqual(m.shown(), capped)  # stops at the ceiling
        m.set_done("a", 60, ceiling=70)      # the confirming line
        self.assertGreaterEqual(m.shown(), capped)
        self.assertAlmostEqual(m.fraction(), 60 / 1000, places=6)

    def test_eta_from_smoothed_rate(self):
        clock = _Clock()
        m = rp.WorkProgress(clock)
        m.set_work("a", 100)
        self.assertIsNone(m.eta_s())
        for i in range(1, 6):
            clock.t = float(i)
            m.set_done("a", i * 10)
        self.assertAlmostEqual(m.eta_s(), 5.0, places=3)

    def test_a_stall_stretches_the_eta_and_is_named(self):
        clock = _Clock()
        m = rp.RunProgress(clock)
        m.set_work("a", 100)
        for i in range(1, 6):
            clock.t = float(i)
            m.set_done("a", i * 10)
        eta_live = m.eta_s()
        clock.t = 35.0                       # 30 s without a finished tile
        self.assertGreater(m.eta_s(), eta_live * 5)
        self.assertIn("no tile finished for 30 s", m.label())
        m.set_done("a", 60)
        self.assertNotIn("no tile finished", m.label())

    def test_label(self):
        clock = _Clock()
        m = rp.RunProgress(clock)
        m.set_work("d", 100)
        m.set_phase("Downloading terrain 30 m z12",
                    {"pass": 2, "passes": 3, "done": 40, "total": 120,
                     "unit": "source tiles", "mbps": 4.25, "errors": 3})
        for i in range(1, 5):
            clock.t = float(i)
            m.set_done("d", i * 10)
        pct, label = m.snapshot()
        self.assertEqual(pct, 40)
        self.assertIn("(pass 2/3)", label)
        self.assertIn("40/120 source tiles", label)
        self.assertIn("4.2 MB/s", label)
        self.assertIn("3 errors", label)
        self.assertIn("ETA", label)
        m.stop("Done.")
        self.assertEqual(m.label(), "Done.")

    def test_percent_is_100_only_when_complete(self):
        m = rp.RunProgress()
        m.set_work("a", 1000)
        m.set_done("a", 999)
        self.assertEqual(m.percent(), 99)
        m.finish_all()
        self.assertEqual(m.percent(), 100)


# ---------------------------------------------------------------------------
# The downloader's own progress across a gap-repair retry pass
# ---------------------------------------------------------------------------

_XYZ = ("type=xyz&url=https%3A//example.com/%7Bz%7D/%7Bx%7D/%7By%7D.png"
        "&zmax=15")


def _write_abt(path, size=64):
    import struct
    stride = ((size * 2 + 255) // 256) * 256
    hdr = (b"AETH" + struct.pack("<HH", 1, size)
           + struct.pack("<dddd", 47.0, 8.0, 1.0 / size, 1.0 / size)
           + struct.pack("<H", stride))
    with open(path, "wb") as fh:
        fh.write(hdr + b"\0" * (size * stride))


class TestRetryPassesKeepTheBarMoving(unittest.TestCase):
    """Feed the real ``_try_rust_download`` two converter passes: the first
    leaves a gap, the plugin retries the gapped output tile."""

    def _run(self, sink=None):
        tiles = mct._enumerate_tiles(
            mct._snap_bbox({"south": 47.1, "north": 47.25, "west": 8.1,
                            "east": 8.35}, [2]), [2])
        subtiles = [t for _r, t in tiles]
        gapped = subtiles[0]["filename"]
        zoom_specs = ta._download_specs_by_zoom(_XYZ, subtiles)
        (zoom, specs), = zoom_specs.items()
        total1 = ta._source_tile_count(specs, zoom)
        retry_specs = [s for s in specs if s["filename"] == gapped]
        total2 = ta._source_tile_count(retry_specs, zoom)
        passes = []

        def fake_once(exe, job_file, on_line=None, **_kw):
            n = len(passes) + 1
            passes.append(n)
            total = total1 if n == 1 else total2
            out = [f"[Download] z={zoom} tiles=1x{total}={total} (~1MB) "
                   f"connections=256"]
            out += _converter_pass_lines(total, errors_at={total // 2: 4})
            ok = total - 4 if n == 1 else total
            out.append(f"[Stats] Tiles: {ok}/{total} OK")
            for line in out:
                if on_line:
                    on_line(line)
            return 0, out

        def has_holes(path):
            # The first pass leaves exactly one gapped output tile.
            return len(passes) == 1 and os.path.basename(path) == gapped

        fracs = []

        class Cb:
            def __call__(self, frac, label):
                fracs.append((frac, label))

        cb = Cb()
        if sink is not None:
            cb.download_event = sink
        bm = mock.Mock()
        bm.find_binary.return_value = "aether_converter"
        with tempfile.TemporaryDirectory() as pool:
            for t in subtiles:
                _write_abt(os.path.join(pool, t["filename"]))
            with mock.patch.object(ta, "_run_converter_download_once",
                                   side_effect=fake_once), \
                 mock.patch.object(ta, "_abt_has_holes",
                                   side_effect=has_holes), \
                 mock.patch.object(ta, "_abt_has_zero_fill",
                                   return_value=False), \
                 mock.patch.object(ta, "_get_download_max_passes",
                                   return_value=3), \
                 mock.patch.object(ta, "_get_download_connections",
                                   return_value=256):
                ok = ta._try_rust_download(_XYZ, pool, {}, 2, subtiles, bm,
                                           progress_cb=cb)
        return ok, passes, fracs, total1, total2

    def test_monotonic_advancing_and_complete(self):
        ok, passes, fracs, total1, total2 = self._run()
        self.assertTrue(ok)
        self.assertEqual(passes, [1, 2], "the retry pass did not run")
        values = [f for f, _l in fracs]
        self.assertEqual(values, sorted(values), "the bar went backwards")
        self.assertEqual(values[-1], 1.0)
        # Pass 1 reported 100 %, yet the bar had room left for the retry.
        first_retry = next(i for i, (_f, label) in enumerate(fracs)
                           if "pass 2/3" in label)
        self.assertLess(max(values[:first_retry]), 1.0)
        # Every progress line of the retry pass moves the bar.
        retry = [f for f, label in fracs
                 if "pass 2/3" in label and "source tiles" in label
                 and not label.endswith(f" 0/{total2} source tiles")]
        self.assertGreaterEqual(len(retry), 2)
        self.assertTrue(all(b > a for a, b in zip(retry, retry[1:])), retry)

    def test_events_carry_counts_passes_and_rates(self):
        events = []
        ok, _passes, _fracs, total1, total2 = self._run(events.append)
        self.assertTrue(ok)
        self.assertEqual({e["pass"] for e in events}, {1, 2})
        done = [e["done_units"] for e in events]
        self.assertEqual(done, sorted(done))
        # The retry's tiles are ADDED to the total when it starts.
        totals = [e["total_units"] for e in events]
        self.assertGreaterEqual(max(totals), total1 + total2)
        self.assertEqual(events[-1]["done_units"], events[-1]["total_units"])
        prog = [e for e in events if e.get("event") == "progress"]
        self.assertTrue(all(e["mbps"] == 8.5 for e in prog))
        self.assertIn(4, {e["errors"] for e in prog})
        for e in events:
            self.assertGreaterEqual(e["ceiling_units"], e["done_units"])

    def test_estimate_equals_the_downloaders_first_total(self):
        events = []
        self._run(events.append)
        tiles = mct._enumerate_tiles(
            mct._snap_bbox({"south": 47.1, "north": 47.25, "west": 8.1,
                            "east": 8.35}, [2]), [2])
        est = ta.estimate_pool_download_units(_XYZ, [t for _r, t in tiles],
                                              pool_check=False)
        self.assertEqual(est, events[0]["total_units"])


class TestMapConverterDownloadDrivesTheRunModel(unittest.TestCase):

    def test_sink_events_reach_the_model_slot(self):
        model = rp.RunProgress()
        expected = mct._enumerate_tiles(
            mct._snap_bbox({"south": 47.1, "north": 47.4, "west": 8.1,
                            "east": 8.4}, [30, 90]), [30, 90])
        entry = {"kind": "xyz", "uri": "u", "extent": None}
        model.set_work(("convert",), 50)
        seen = []

        def fake_ensure(uri, specs, res, progress_cb=None, **_kw):
            total = 100
            progress_cb.download_event({
                "event": "pass_start", "done_units": 0, "total_units": total,
                "ceiling_units": 0, "pass": 1, "max_passes": 3, "zoom": 12,
                "pass_done": 0, "pass_total": total})
            for d in (10, 50, 100):
                progress_cb.download_event({
                    "event": "progress", "done_units": d,
                    "total_units": total, "ceiling_units": d, "pass": 1,
                    "max_passes": 3, "zoom": 12, "pass_done": d,
                    "pass_total": total, "mbps": 3.0, "errors": 0})
                seen.append(model.fraction())
            return {t["filename"]: "/p" for t in specs}

        with mock.patch.object(ta, "ensure_pool_tiles",
                               side_effect=fake_ensure):
            mct._download_xyz_entries([entry], expected,
                                      progress_model=model)
        self.assertEqual(seen, sorted(seen))
        self.assertGreater(seen[0], 0.0)
        # Both resolutions downloaded, convert still outstanding.
        self.assertEqual(model.work(("download", 0, 30)), 100)
        self.assertEqual(model.work(("download", 0, 90)), 100)
        self.assertLess(model.fraction(), 1.0)
        self.assertIn("Downloading terrain 90 m [2/2] z12", model.label())
        model.finish(("convert",))
        self.assertEqual(model.fraction(), 1.0)

    def test_pool_hit_finishes_the_slot(self):
        model = rp.RunProgress()
        expected = mct._enumerate_tiles(
            mct._snap_bbox({"south": 47.1, "north": 47.4, "west": 8.1,
                            "east": 8.4}, [30]), [30])
        model.set_work(("download", 0, 30), 500)
        with mock.patch.object(ta, "ensure_pool_tiles",
                               return_value={}):
            mct._download_xyz_entries([{"uri": "u", "extent": None}],
                                      expected, progress_model=model)
        self.assertEqual(model.fraction(), 1.0)


# ---------------------------------------------------------------------------
# Extent helpers
# ---------------------------------------------------------------------------

A = {"south": 47.0, "north": 47.5, "west": 8.0, "east": 8.5}
B = {"south": 46.5, "north": 47.2, "west": 8.3, "east": 9.0}


class TestExtentHelpers(unittest.TestCase):

    def test_union(self):
        self.assertIsNone(mct._bounds_union([]))
        self.assertIsNone(mct._bounds_union([None, {}]))
        self.assertEqual(mct._bounds_union([A, None, B]),
                         {"south": 46.5, "north": 47.5, "west": 8.0,
                          "east": 9.0})

    def test_union_extent_skips_buildings_rows(self):
        rows = [mct._LayerEntry(extent=dict(A)),
                mct._LayerEntry(layer_type="buildings", extent=dict(B))]
        self.assertEqual(mct._union_extent(rows), A)
        self.assertIsNone(mct._union_extent([mct._LayerEntry()]))

    def test_apply_copies_not_shares(self):
        rows = [mct._LayerEntry(extent=dict(A)), mct._LayerEntry(),
                mct._LayerEntry(layer_type="buildings")]
        changed = mct._apply_extent(rows, rows[0].extent, [1, 2, 7, 1])
        self.assertEqual(changed, [1, 2])
        self.assertEqual(rows[1].extent, A)
        self.assertEqual(rows[2].extent, A)
        rows[1].extent["north"] = 50.0
        self.assertEqual(rows[0].extent["north"], 47.5)
        self.assertEqual(rows[2].extent["north"], 47.5)
        self.assertEqual(mct._apply_extent(rows, None, [0]), [])

    def test_folder_extent_counts_only_files_the_run_uses(self):
        def ent(b, **kw):
            e = {"readable": True, "crs_authid": "EPSG:4326",
                 "wgs84_bounds": b, "bands": 1, "dtype": "Float32"}
            e.update(kw)
            return e

        far = {"south": 10.0, "north": 11.0, "west": 10.0, "east": 11.0}
        index = mock.Mock()
        index.entries = {"/f/a.tif": ent(A), "/f/b.tif": ent(B),
                         "/f/skip.tif": ent(far), "/f/bad.tif": ent(far)}
        problems = {"/f/skip.tif": ("unusable", "x"),
                    "/f/bad.tif": ("unusable", "y")}
        with mock.patch.object(terrain_index, "problem_of",
                               side_effect=lambda e: next(
                                   (problems[p] for p, v in
                                    index.entries.items()
                                    if v is e and p in problems), None)):
            ext = mct._folder_extent(
                index, decisions={"/f/skip.tif": terrain_index.DECISION_SKIP})
        self.assertEqual(ext, mct._bounds_union([A, B]))

    def test_folder_extent_uses_the_assigned_crs(self):
        e_nocrs = {"readable": True}
        index = mock.Mock()
        index.entries = {"/f/a.tif": {"wgs84_bounds": A},
                         "/f/n.tif": e_nocrs}
        with mock.patch.object(
                terrain_index, "problem_of",
                side_effect=lambda e: ((terrain_index.KIND_NOCRS, "no CRS")
                                       if e is e_nocrs else None)), \
             mock.patch.object(terrain_index, "with_crs",
                               return_value={"wgs84_bounds": B}) as wc:
            ext = mct._folder_extent(index,
                                     crs_overrides={"/f/n.tif": "EPSG:2056"})
            wc.assert_called_once_with(e_nocrs, "EPSG:2056")
            self.assertEqual(ext, mct._bounds_union([A, B]))
            # Without the user's CRS the file does not count.
            self.assertEqual(mct._folder_extent(index), A)


class TestRunWorkEstimate(unittest.TestCase):

    def test_only_phases_with_work_get_weight(self):
        model = rp.RunProgress()
        local = mct._LayerEntry(source_path=tempfile.gettempdir(),
                                extent=dict(A))
        jobs = mct._enumerate_tiles(mct._snap_bbox(A, [30]), [30])
        mct._estimate_run_work(model, [local], jobs)
        keys = set(model._total)
        self.assertEqual(keys, {("convert",)})
        self.assertAlmostEqual(
            model.work(("convert",)),
            sum(rp.tile_blocks(t["size_px"]) for _r, t in jobs)
            * rp.CONVERT_UNITS_PER_BLOCK)


if __name__ == "__main__":
    unittest.main()
