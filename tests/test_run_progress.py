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
        clock.t = 29.0                       # 24 s without a finished tile
        self.assertGreater(m.eta_s(), eta_live * 4)
        self.assertIn("no tile finished for 24 s", m.label())
        self.assertIn("ETA ", m.label())
        self.assertNotIn(rp.ETA_UNKNOWN, m.label())
        m.set_done("a", 60)
        self.assertNotIn("no tile finished", m.label())

    def test_thirty_seconds_without_progress_blank_the_eta(self):
        # The owner saw "days": minutes of silence (a repair pass being
        # prepared, a building decode) stretched the rate without bound.
        # From ETA_BLANK_S on, no ETA is extrapolated at all.
        clock = _Clock()
        m = rp.RunProgress(clock)
        m.set_work("a", 1000)
        for i in range(1, 6):
            clock.t = float(i)
            m.set_done("a", i * 10)
        self.assertEqual(m.ETA_BLANK_S, 30.0)
        clock.t = 5.0 + 29.9
        self.assertIsNotNone(m.eta_s())
        self.assertFalse(m.eta_stalled())
        clock.t = 5.0 + 30.0                 # 30 s: no slot reported
        self.assertIsNone(m.eta_s())
        self.assertTrue(m.eta_stalled())
        label = m.label()
        self.assertTrue(label.endswith(" · ETA —"), label)
        self.assertIn("no tile finished for 30 s", label)
        self.assertEqual(label.count("ETA"), 1)
        clock.t = 5.0 + 3 * 3600.0           # hours later: still no guess
        self.assertTrue(m.label().endswith("ETA —"), m.label())
        self.assertNotIn(" h ", m.label())
        # A slot reports again: the ETA is measured again at once.
        m.set_done("a", 60)
        self.assertFalse(m.eta_stalled())
        self.assertNotIn(rp.ETA_UNKNOWN, m.label())
        # Concurrent activities: once, at the end of the label.
        m.set_activity("a", "Converting band 2/4", {"done": 1, "total": 9})
        clock.t += 31.0
        self.assertTrue(m.label().endswith(" · ETA —"), m.label())
        self.assertEqual(m.label().count("ETA"), 1)
        # Stopped (done / cancelled): no ETA, not even a blank one.
        m.stop("Cancelled.")
        self.assertEqual(m.label(), "Cancelled.")

    def test_no_eta_dash_before_anything_was_measured(self):
        clock = _Clock()
        m = rp.RunProgress(clock)
        m.set_work("a", 100)
        m.set_phase("Preparing layers")
        clock.t = 120.0                      # nothing reported yet at all
        self.assertFalse(m.eta_stalled())
        self.assertEqual(m.label(), "Preparing layers")

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


class TestDegradedRepairPass(unittest.TestCase):
    """The real ``_try_rust_download`` against a mocked converter: pass 1
    reports two degraded source tiles and no gaps; a converter that knows
    ``repair_source_tiles`` gets ONE repair pass over exactly those tiles,
    an older one gets the whole-tile re-run — neither at halved connections.
    The job JSON is read as the converter would read it."""

    def _run(self, converter_version, sink=None):
        tiles = mct._enumerate_tiles(
            mct._snap_bbox({"south": 47.1, "north": 47.25, "west": 8.1,
                            "east": 8.35}, [2]), [2])
        subtiles = [t for _r, t in tiles]
        (zoom, specs), = ta._download_specs_by_zoom(_XYZ, subtiles).items()
        s0 = specs[0]
        n = 2.0 ** zoom
        import math
        lat = s0["ul_lat"] - 0.001
        x0 = int((s0["ul_lon"] + 0.001 + 180.0) / 360.0 * n)
        y0 = int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi)
                 / 2 * n)
        bad = [(zoom, x0, y0), (zoom, x0 + 1, y0)]
        hit = ta._specs_hit_by_degraded(
            specs, [ta.DegradedTile(z, x, y, "", None) for z, x, y in bad])
        total1 = ta._source_tile_count(specs, zoom)
        jobs = []

        def fake_once(exe, job_file, on_line=None, **_kw):
            import json
            with open(job_file) as fh:
                job = json.load(fh)
            jobs.append(job)
            if len(jobs) == 1:
                total = total1
                out = [f"[Download] z={zoom} tiles=1x{total}={total} (~1MB) "
                       f"connections=256"]
                out += _converter_pass_lines(total)
                out += [f"[Degraded] z={z} x={x} y={y} reason=timeout "
                        f"filled_from=z{zoom - 1}" for z, x, y in bad]
                out.append(f"[Stats] DEGRADED: 2 of {total} source tile(s) "
                           f"filled from coarser zooms after a failed fetch")
                out.append(f"[Stats] Tiles: {total - 2}/{total} OK")
            else:
                total = (len(job["repair_source_tiles"])
                         if "repair_source_tiles" in job
                         else ta._source_tile_count(job["tiles"], zoom))
                out = [f"[Download] z={zoom} tiles=1x{total}={total} (~1MB) "
                       f"connections=256"]
                out += _converter_pass_lines(total)
                out.append(f"[Stats] Tiles: {total}/{total} OK")
            for line in out:
                if on_line:
                    on_line(line)
            return 0, out

        fracs = []

        class Cb:
            def __call__(self, frac, label):
                fracs.append((frac, label))

        cb = Cb()
        if sink is not None:
            cb.download_event = sink
        bm = mock.Mock()
        bm.find_binary.return_value = "aether_converter"
        from waveshed.core import binary_manager
        with tempfile.TemporaryDirectory() as pool:
            for t in subtiles:
                _write_abt(os.path.join(pool, t["filename"]))
            with mock.patch.object(ta, "_run_converter_download_once",
                                   side_effect=fake_once), \
                 mock.patch.object(ta, "_abt_has_holes", return_value=False), \
                 mock.patch.object(ta, "_abt_has_zero_fill",
                                   return_value=False), \
                 mock.patch.object(ta, "_get_download_max_passes",
                                   return_value=4), \
                 mock.patch.object(ta, "_get_download_connections",
                                   return_value=256), \
                 mock.patch.object(binary_manager, "probe_converter_version",
                                   return_value=converter_version):
                ok = ta._try_rust_download(_XYZ, pool, {}, 2, subtiles, bm,
                                           progress_cb=cb)
        return ok, jobs, fracs, bad, hit, specs

    def test_repair_job_shape(self):
        ok, jobs, _f, bad, hit, specs = self._run("0.2.6")
        self.assertTrue(ok)
        self.assertEqual(len(jobs), 2)
        self.assertNotIn("repair_source_tiles", jobs[0])
        self.assertEqual(jobs[1]["repair_source_tiles"],
                         [list(t) for t in sorted(bad)])
        self.assertEqual({t["filename"] for t in jobs[1]["tiles"]}, hit)
        self.assertLess(len(hit), len(specs))
        self.assertEqual(jobs[1]["tiles"],
                         [s for s in specs if s["filename"] in hit])
        self.assertEqual([j["max_connections"] for j in jobs], [256, 256])
        self.assertEqual(jobs[1]["zoom"], jobs[0]["zoom"])

    def test_old_converter_gets_whole_tiles_without_the_field(self):
        ok, jobs, _f, _bad, hit, _specs = self._run("0.2.5")
        self.assertTrue(ok)
        self.assertEqual(len(jobs), 2)
        self.assertNotIn("repair_source_tiles", jobs[1])
        self.assertEqual({t["filename"] for t in jobs[1]["tiles"]}, hit)
        self.assertEqual([j["max_connections"] for j in jobs], [256, 256])

    def test_unknown_converter_version_is_the_old_one(self):
        _ok, jobs, _f, _b, _h, _s = self._run(None)
        self.assertNotIn("repair_source_tiles", jobs[1])

    def test_progress_monotonic_and_advancing_through_the_repair_pass(self):
        events = []
        ok, jobs, fracs, bad, _hit, _specs = self._run("0.2.6", events.append)
        self.assertTrue(ok)
        values = [f for f, _l in fracs]
        self.assertEqual(values, sorted(values), "the bar went backwards")
        self.assertEqual(values[-1], 1.0)
        first_repair = next(i for i, (_f, label) in enumerate(fracs)
                            if "repair pass 2/4" in label)
        self.assertLess(max(values[:first_repair]), 1.0)
        repair = [f for f, label in fracs
                  if "repair pass 2/4" in label and "source tiles" in label
                  and not label.endswith(" 0/2 source tiles")]
        self.assertEqual(len(repair), 2)            # 1/2 and 2/2
        self.assertTrue(repair[1] > repair[0] > values[first_repair - 1])
        # The repair pass ADDS exactly its source tiles to the total.
        totals = [e["total_units"] for e in events]
        before = max(e["total_units"] for e in events if e["pass"] == 1)
        rep = [e for e in events if e.get("repair")]
        self.assertTrue(rep)
        self.assertTrue(all(e["pass"] == 2 and e["pass_total"] == 2
                            for e in rep), rep)
        self.assertEqual(max(totals), before + len(bad))
        self.assertEqual(events[-1]["done_units"], events[-1]["total_units"])

    def test_the_run_label_says_repair_pass(self):
        model = rp.RunProgress()
        key = ("download", 0, 2)
        model.set_work(key, 1)
        sink = mct._DownloadProgressSink(model, key, 2, 1, 1)
        labels, fracs = [], []

        def tap(info):
            sink.download_event(info)
            labels.append(model.label())
            fracs.append(model.fraction())
        self._run("0.2.6", tap)
        self.assertTrue(any("(repair pass 2/4)" in lb and "/2 source tiles" in lb
                            for lb in labels), labels[-5:])
        self.assertEqual(fracs, sorted(fracs))
        self.assertEqual(model.fraction(), 1.0)


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
        # One slot per (entry, zoom): 90 m needs z11 here, 30 m z12.
        self.assertEqual(model.work(("download", 0, 11)), 100)
        self.assertEqual(model.work(("download", 0, 12)), 100)
        self.assertLess(model.fraction(), 1.0)
        self.assertIn("Downloading terrain z12 (30 m) [2/2]", model.label())
        model.finish(("convert",))
        self.assertEqual(model.fraction(), 1.0)

    def test_pool_hit_finishes_the_slot(self):
        model = rp.RunProgress()
        expected = mct._enumerate_tiles(
            mct._snap_bbox({"south": 47.1, "north": 47.4, "west": 8.1,
                            "east": 8.4}, [30]), [30])
        model.set_work(("download", 0, 12), 500)      # 30 m: z12 here
        with mock.patch.object(ta, "ensure_pool_tiles",
                               return_value={}):
            mct._download_xyz_entries([{"uri": "u", "extent": None}],
                                      expected, progress_model=model)
        self.assertEqual(model.fraction(), 1.0)


class TestConcurrentSlotsCreepApart(unittest.TestCase):
    """Phase 2 and a download report into their own slots in turn (the Map
    Converter runs them at the same time): each slot creeps towards its own
    next report at its own rate; nothing is reset or counted twice."""

    def test_two_slots_in_turn(self):
        clock = _Clock()
        m = rp.WorkProgress(clock)
        m.set_work("dl", 1000)
        m.set_work("cv", 1000)
        shown = []
        for i in range(1, 11):          # dl: 20 u/s, cv: 5 u/s, interleaved
            clock.t = float(i)
            m.set_done("dl", i * 20, ceiling=(i + 1) * 20)
            shown.append(m.shown())
            clock.t = i + 0.5
            m.set_done("cv", i * 5, ceiling=(i + 1) * 5)
            shown.append(m.shown())
        clock.t = 10.9                   # both creep towards their ceilings
        shown.append(m.shown())
        self.assertGreater(m.fraction(), (200 + 50) / 2000 - 1e-9)
        self.assertEqual(shown, sorted(shown))
        clock.t = 60.0                   # ...never past them
        capped = m.shown()
        self.assertAlmostEqual(capped, (220 + 55) / 2000, places=6)
        # The confirming reports: nothing shown twice.
        m.set_done("dl", 220, ceiling=240)
        m.set_done("cv", 55, ceiling=60)
        self.assertAlmostEqual(m.fraction(), (220 + 55) / 2000, places=6)
        self.assertGreaterEqual(m.shown(), capped)

    def test_each_slot_creeps_at_its_own_rate(self):
        clock = _Clock()
        m = rp.WorkProgress(clock)
        m.set_work("fast", 10_000)
        m.set_work("slow", 10_000)
        for i in range(1, 6):
            clock.t = float(i)
            m.set_done("fast", i * 100, ceiling=i * 100 + 1000)
            m.set_done("slow", i * 1, ceiling=i * 1 + 1)   # 1 unit of room
        base = m.fraction()
        clock.t = 6.0                    # one second of creep each
        gained = (m.shown() - base) * 20_000 / (1 - base)
        # fast: ~100 units at its own rate, slow: its 1 unit of room — the
        # slow slot reporting last must not stop the fast one creeping.
        self.assertGreater(gained, 90)
        self.assertLess(gained, 115)


class TestActivitiesLabel(unittest.TestCase):

    def test_concurrent_activities_are_named_together(self):
        clock = _Clock()
        m = rp.RunProgress(clock)
        m.set_work("cv", 100)
        m.set_work("dl", 100)
        m.set_phase("Converting tiles from local files")
        m.set_activity("cv", "Converting tiles from local files",
                       {"done": 0, "total": 531, "unit": "tiles"})
        m.set_activity("dl", "Downloading terrain z15 (2, 5 m)",
                       {"done": 12345, "total": 193978,
                        "unit": "source tiles", "mbps": 25.1})
        self.assertEqual(
            m.label(),
            "Converting tiles from local files: 0/531 tiles; "
            "Downloading terrain z15 (2, 5 m): 12,345/193,978 source "
            "tiles, 25.1 MB/s")
        m.set_activity("cv", "Converting tiles from local files",
                       {"done": 120})
        self.assertTrue(m.label().startswith(
            "Converting tiles from local files: 120/531 tiles; "
            "Downloading terrain z15"))          # start order kept
        self.assertEqual(m.activities(),
                         ["Converting tiles from local files",
                          "Downloading terrain z15 (2, 5 m)"])
        m.end_activity("cv")
        self.assertTrue(m.label().startswith("Downloading terrain z15"))
        m.end_activity("dl")
        self.assertEqual(m.label(), "Converting tiles from local files")
        m.set_activity("dl", "Downloading terrain z12 (30 m)")
        m.stop("Cancelled.")
        self.assertEqual(m.label(), "Cancelled.")
        self.assertEqual(m.activities(), [])

    def test_eta_and_stall_note_once_at_the_end(self):
        clock = _Clock()
        m = rp.RunProgress(clock)
        m.set_work("a", 100)
        m.set_work("b", 100)
        m.set_activity("a", "A", {"done": 1, "total": 2})
        m.set_activity("b", "B")
        for i in range(1, 5):
            clock.t = float(i)
            m.set_done("a", i * 10)
        label = m.label()
        self.assertTrue(label.startswith("A: 1/2 tiles; B · ETA"), label)
        self.assertEqual(label.count("ETA"), 1)
        clock.t = 40.0
        self.assertIn(" · no tile finished for 36 s", m.label())

    def test_a_step_counter_is_named_first(self):
        m = rp.RunProgress(_Clock())
        m.set_work("b", 10)
        m.set_activity("b", "Converting band 2/4",
                       {"step": "decoding building tiles", "done": 12000,
                        "total": 20808})
        self.assertEqual(m.label(), "Converting band 2/4: decoding building "
                                    "tiles 12,000/20,808")
        m.set_phase("Converting band 2/4", {"step": "decoding building "
                                            "tiles", "done": 1000,
                                            "total": 2500})
        m.end_activity("b")
        self.assertEqual(m.label(), "Converting band 2/4: decoding building "
                                    "tiles 1,000/2,500")

    def test_a_step_without_a_counter_is_named(self):
        # A batch whose converter waits for its building tiles
        # (aether_converter >= 0.2.12): the step alone, no counter.
        m = rp.RunProgress(_Clock())
        m.set_work("b", 10)
        m.set_activity("b", "Converting band 2/4",
                       {"step": "waiting for building tiles"}, reset=True)
        self.assertEqual(m.label(),
                         "Converting band 2/4: waiting for building tiles")
        m.set_activity("b", "Converting band 2/4",
                       {"done": 3, "total": 9, "unit": "tiles"}, reset=True)
        self.assertEqual(m.label(), "Converting band 2/4: 3/9 tiles")
        m.end_activity("b")
        m.set_phase("Converting tiles from local files",
                    {"step": "waiting for building tiles"})
        self.assertEqual(m.label(), "Converting tiles from local files: "
                                    "waiting for building tiles")


# ---------------------------------------------------------------------------
# The building-tile decode of an ingest batch (toolkit 0.2.9)
# ---------------------------------------------------------------------------

#: The lines aether_converter 0.2.9 prints while it decodes a batch's
#: buildings_pbf_dir (every 1,000 tiles, then the end), before its first
#: `Progress:` line — as the toolkit change specifies them.
_DECODE_DIR = '[Rust] Decoding building tiles in "/c/views/0f3a" (once for ' \
              'the run)...'


def _decode(n, m):
    return f"[Rust] Decoding building tiles: {n}/{m}"


def _decoded(m, b, s):
    return f"[Rust] Building tiles decoded: {m} tile(s), {b} building(s) " \
           f"in {s:.1f} s"


_INFO = ("[Info] buildings_pbf_dir: 3 tile(s), 3 building(s) (3 after "
         "dedup), 3 drawn, 30 pixel(s) raised")


class TestBuildingDecodeParsers(unittest.TestCase):

    def test_decode_progress_line(self):
        self.assertEqual(rp.parse_building_decode(_decode(12000, 20808)),
                         (12000, 20808))
        self.assertEqual(rp.parse_building_decode(
            "[Rust] Decoding building tiles: 12,000/20,808"), (12000, 20808))
        # The directory line (0.2.7 / 0.2.8 too) carries no counter.
        self.assertIsNone(rp.parse_building_decode(_DECODE_DIR))
        self.assertIsNone(rp.parse_building_decode("[Rust] Progress: 10/30"))

    def test_decode_end_line(self):
        self.assertEqual(rp.parse_buildings_decoded(
            _decoded(20808, 1234567, 83.4)), (20808, 1234567, 83.4))
        self.assertEqual(rp.parse_buildings_decoded(
            "[Rust] Building tiles decoded: 2,500 tile(s), 7,800 "
            "building(s) in 5.0 s"), (2500, 7800, 5.0))
        self.assertIsNone(rp.parse_buildings_decoded(_decode(1000, 2500)))
        self.assertIsNone(rp.parse_buildings_decoded(_INFO))
        # Neither line is an ingest progress line.
        for line in (_decode(1000, 2500), _decoded(2500, 7, 1.0)):
            self.assertIsNone(rp.parse_ingest_progress(line), line)

    def test_next_decode_report(self):
        self.assertEqual(rp.BUILDING_DECODE_STEP, 1000)
        self.assertEqual(rp.next_decode_report(0, 20808), 1000)
        self.assertEqual(rp.next_decode_report(12000, 20808), 13000)
        self.assertEqual(rp.next_decode_report(20000, 20808), 20808)
        self.assertEqual(rp.next_decode_report(500, 800), 800)


class TestBuildingDecodeThroughTheWorker(unittest.TestCase):
    """The new lines fed through a fake converter into the shipped
    ``_MapConverterWorker._run_ingest``: the label names the decode, the
    batch's slot moves through its decode share (and creeps between the
    lines), the ETA is blanked after 30 s without a report."""

    SLOT = ("convert", 7)
    PHASE = "Converting band 2/4"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.clock = _Clock()
        self.model = rp.RunProgress(self.clock)

    def tearDown(self):
        self._tmp.cleanup()

    def jobs(self, n=30, pbf=True):
        pbf_dir = os.path.join(self.root, "pbf")
        os.makedirs(pbf_dir, exist_ok=True)
        jobs = []
        for i in range(n):
            job = {"output_path": os.path.join(self.root, "out",
                                               f"t{i}.abt"),
                   "size_px": 256}
            if pbf:
                job["buildings_pbf_dir"] = pbf_dir
            jobs.append(job)
        return jobs

    def run_ingest(self, script, jobs):
        """*script*: ``(t, line)`` fed in order (a callable *line* is a
        probe, run at time *t* between two lines). Returns the snapshots
        ``(line, fraction, shown, label)`` taken after each line and the
        status texts."""
        worker = mct._MapConverterWorker([], os.path.join(self.root, "out"),
                                         [30], [],
                                         progress_model=self.model)
        status, seen = [], []
        worker.status.connect(status.append)
        model = self.model

        def fake(exe, args, on_line, on_start=None, env=None):
            for t, line in script:
                self.clock.t = t
                if callable(line):
                    line()
                    continue
                on_line(line)
                seen.append((line, model.fraction(), model.shown(),
                             model.label()))
            return 0

        with mock.patch.object(ta, "run_converter_streaming",
                               side_effect=fake):
            worker._run_ingest("conv", jobs, self.SLOT, self.PHASE,
                               activity=self.SLOT, batch=7)
        return seen, status

    def ingest_lines(self, t0, n, pbf=True):
        out = []
        for k in range(1, n + 1):
            if pbf:
                out.append((t0 + k * 0.1, _INFO))
            if k % 10 == 0 or k == n:
                out.append((t0 + k * 0.1, f"[Rust] Progress: {k}/{n}"))
        return out

    def test_the_decode_is_the_first_share_of_the_batch(self):
        probes = {}

        def probe(name):
            return lambda: probes.__setitem__(
                name, (self.model.fraction(), self.model.shown()))

        script = [(1.0, _DECODE_DIR), (2.0, _decode(1000, 2500)),
                  (4.0, _decode(2000, 2500)), (4.5, probe("creep")),
                  (6.0, _decoded(2500, 7800, 5.0))]
        script += self.ingest_lines(7.0, 30)
        seen, status = self.run_ingest(script, self.jobs())
        by_line = {line: (f, label) for line, f, _s, label in seen}
        share = rp.BUILDING_DECODE_SHARE
        f, label = by_line[_decode(1000, 2500)]
        self.assertAlmostEqual(f, share * 1000 / 2500, places=9)
        self.assertEqual(label.split(" · ")[0], "Converting band 2/4: "
                         "decoding building tiles 1,000/2,500")
        f, label = by_line[_decode(2000, 2500)]
        self.assertAlmostEqual(f, share * 2000 / 2500, places=9)
        self.assertTrue(label.startswith("Converting band 2/4: decoding "
                                         "building tiles 2,000/2,500"), label)
        # Between two decode lines the bar creeps — never past the next one.
        f, shown = probes["creep"]
        self.assertGreater(shown, f)
        self.assertLessEqual(shown, share + 1e-9)
        f, label = by_line[_decoded(2500, 7800, 5.0)]
        self.assertAlmostEqual(f, share, places=9)
        self.assertTrue(label.startswith("Converting band 2/4: 0/30 tiles"),
                        label)
        f, label = by_line["[Rust] Progress: 10/30"]
        self.assertAlmostEqual(f, share + (1 - share) / 3, places=9)
        self.assertTrue(label.startswith("Converting band 2/4: 10/30 tiles"),
                        label)
        self.assertNotIn("decoding", label)
        fracs = [f for _l, f, _s, _lb in seen]
        self.assertEqual(fracs, sorted(fracs))
        self.assertEqual(self.model.fraction(), 1.0)
        self.assertIn("Decoding building tiles 1,000/2,500", status)
        self.assertIn("Decoding building tiles 2,000/2,500", status)
        self.assertIn("Converting tile 30/30", status)

    def test_a_long_decode_blanks_the_eta(self):
        script = [(2.0, _decode(1000, 20808)), (4.0, _decode(2000, 20808)),
                  (6.0, _decode(3000, 20808))]
        stalled = {}

        def at(name):
            return lambda: stalled.__setitem__(name, self.model.label())

        script += [(20.0, at("quiet 14 s")), (36.0, at("quiet 30 s")),
                   (4000.0, at("quiet 66 min")),
                   (4001.0, _decode(4000, 20808)),
                   (4001.5, at("reported again"))]
        script += [(4002.0, _decoded(20808, 99, 4000.0))]
        script += self.ingest_lines(4003.0, 30)
        self.run_ingest(script, self.jobs())
        self.assertIn("ETA ", stalled["quiet 14 s"])
        self.assertNotIn(rp.ETA_UNKNOWN, stalled["quiet 14 s"])
        for name in ("quiet 30 s", "quiet 66 min"):
            self.assertTrue(stalled[name].endswith(" · ETA —"),
                            stalled[name])
            self.assertTrue(stalled[name].startswith(
                "Converting band 2/4: decoding building tiles 3,000/20,808"),
                stalled[name])
        self.assertNotIn(rp.ETA_UNKNOWN, stalled["reported again"])

    def test_a_converter_without_decode_lines_jumps_the_share(self):
        # 0.2.7 / 0.2.8: only the directory line, then the tiles.
        script = [(1.0, _DECODE_DIR)] + self.ingest_lines(2.0, 30)
        seen, status = self.run_ingest(script, self.jobs())
        labels = [label for _l, _f, _s, label in seen]
        self.assertFalse([lb for lb in labels if "decoding" in lb], labels)
        f = next(f for line, f, _s, _lb in seen
                 if line == "[Rust] Progress: 10/30")
        share = rp.BUILDING_DECODE_SHARE
        self.assertAlmostEqual(f, share + (1 - share) / 3, places=9)
        self.assertEqual(self.model.fraction(), 1.0)
        self.assertFalse([s for s in status if s.startswith("Decoding")])

    def test_a_batch_without_buildings_has_no_decode_share(self):
        seen, _status = self.run_ingest(self.ingest_lines(1.0, 30, pbf=False),
                                        self.jobs(pbf=False))
        f = next(f for line, f, _s, _lb in seen
                 if line == "[Rust] Progress: 10/30")
        self.assertAlmostEqual(f, 1 / 3, places=9)


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
