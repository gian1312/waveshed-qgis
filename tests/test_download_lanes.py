"""Map Converter: two download lanes per pool (2026-10-25).

The owner's run (2026-10-24): the z15 passes were network-bound at line
rate, the coarser passes (z14/z12/z11, derived by the toolkit from the
cached z15 tiles) read and wrote the disk with the network idle — about
150 s of a 750 s download lane. Per pool now:

* a FETCH lane runs each band's finest call, north to south (the hole unit
  Phase 2 leaves is pushed to its front, as before);
* a DERIVE lane runs a band's coarser calls once that band's finest call
  AND the next band's finest call have ended (one band behind, 2026-11-05:
  the source tiles straddling the band edge then derive from both sides in
  every run instead of racing the next band's z15), in the same order —
  beside the band after next's finest call; the last band and the hole
  unit are not held, and the lane takes the first unit it may run;
* ``_unit_done`` (the band's Phase-3 batch) fires once, after BOTH lanes;
* ``WAVESHED_SERIAL_ZOOMS=1`` restores the one-lane order call for call;
* Stop kills both lanes' processes; a derive-lane failure halts the run and
  is its error, once;
* the progress model shows both running passes (two activities; two calls
  of one slot kept apart by the ledger);
* one timing line per band with coarser passes.

Driven with the shipped worker and fakes for the engine, the pool download
and the free space (``tests/test_mc_pipeline``).
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import os
import re
import tempfile
import threading
import time
import unittest
from unittest import mock

import waveshed.core.terrain_adapter as ta
from waveshed.core import run_progress as rp
import waveshed.gui.map_converter_tab as mct
from waveshed.gui.map_converter_tab import _MapConverterWorker

from test_mc_pipeline import _BandRun, _Case, _files_a
from test_two_phase_convert import _XYZ, _Converter, _sleeper, _wait_for
from test_timing_lane import _BATCH_RE

#: 2 m + 30 m over 47-48 N, 8.0-8.1 E: two 0.5 deg bands, each one z15 call
#: (five 2 m tiles) and one z12 call (one 30 m tile).
_TWO = {"south": 47.0, "north": 48.0, "west": 8.0, "east": 8.1}
#: The same over 47-48.5 N: three bands (north, middle, south).
_THREE = {"south": 47.0, "north": 48.5, "west": 8.0, "east": 8.1}
_ZOOM = {2: 15, 30: 12}

_UNIT_RE = re.compile(
    r"^  timing: (?P<what>band \d+/\d+|Phase-2 holes): z(?P<zoom>\d+) "
    r"landed at \+(?P<fine>[\d.]+) s, coarser passes landed at "
    r"\+(?P<coarse>[\d.]+) s, ran beside (?P<other>.+?) z(?P<oz>\d+): "
    r"(?P<verdict>yes|no)(?P<note> \(none was downloading\))?"
    r"(?P<held>; held (?P<held_s>[\d.]+) s until (?P<gate>.+) z\d+ landed)?$")


def _lanes(serial):
    """The A/B switch for one test: set, or absent (two lanes)."""
    env = dict(os.environ)
    env.pop(mct.SERIAL_ZOOMS_ENV, None)
    if serial:
        env[mct.SERIAL_ZOOMS_ENV] = "1"
    return mock.patch.dict(os.environ, env, clear=True)


def _band_of(name):
    """``"N"`` / ``"M"`` / ``"S"``: the band (48.0-48.5, 47.5-48.0,
    47.0-47.5) of an output tile by its name."""
    lat = float(name.split("_")[1][1:6])
    res = int(name.rsplit("_", 1)[1][:-5])
    centre = lat - (0.1 if res == 2 else 0.5) / 2
    return "N" if centre > 48.0 else "M" if centre > 47.5 else "S"


class _LaneRun(_BandRun):
    """A :class:`_BandRun` that records every download call — zoom, band,
    tiles, thread, start and end — and runs a test's hook for a
    ``(band, zoom)`` call (or ``"holes"`` for a call that holds a tile
    Phase 2 left with a hole, see *holes*) before it returns."""

    def __init__(self, test, out_dir, cache_dir, hooks=None, holes=(),
                 **kw):
        kw.setdefault("resolutions", (2, 30))
        super().__init__(test, out_dir, cache_dir, **kw)
        self.hooks = dict(hooks or {})
        self.holes = set(holes)
        self.calls = []
        self.lock = threading.Lock()

    def pool(self, uri, specs, res, **kw):
        names = sorted(t["filename"] for t in specs)
        zooms = {_ZOOM[int(n.rsplit("_", 1)[1][:-5])] for n in names}
        bands = {_band_of(n) for n in names}
        assert len(zooms) == 1, names
        band = ("holes" if set(names) <= self.holes else
                bands.pop() if len(bands) == 1 else tuple(sorted(bands)))
        info = {"zoom": zooms.pop(), "band": band, "names": names,
                "thread": threading.current_thread().name,
                "tag": ta.panel_tag(),
                "start": time.perf_counter(), "end": None}
        with self.lock:
            self.calls.append(info)
        try:
            hook = self.hooks.get((band, info["zoom"]))
            if hook is not None:
                hook(info, kw)
            return super().pool(uri, specs, res, **kw)
        finally:
            info["end"] = time.perf_counter()

    def order(self, lane):
        """``[(band, zoom)]`` of the calls a lane ran, in start order."""
        return [(c["band"], c["zoom"]) for c in sorted(
            self.calls, key=lambda c: c["start"])
            if c["thread"].startswith(lane)]

    def call(self, band, zoom):
        found = [c for c in self.calls
                 if c["band"] == band and c["zoom"] == zoom]
        assert len(found) == 1, (band, zoom, self.calls)
        return found[0]


def _event(info_or_kw, zoom, done, total):
    """A download event for *kw*'s sink: *done* / *total* source tiles."""
    info_or_kw["progress_cb"].download_event({
        "event": "progress", "done_units": done, "total_units": total,
        "ceiling_units": done, "pass": 1, "max_passes": 3, "zoom": zoom,
        "pass_done": done, "pass_total": total, "mbps": 4.0, "errors": 0})


class _LaneCase(_Case):

    def lane_run(self, serial=False, extent=_TWO, entries=None, **kw):
        entries = entries or [{"kind": "xyz", "uri": _XYZ,
                               "extent": dict(extent)}]
        r = _LaneRun(self, self.out, self.cache, entries=entries,
                     extent=extent, **kw)
        with _lanes(serial), mock.patch.object(tempfile, "tempdir",
                                               self.temp):
            r.run()
        return r

    def start_bg(self, r, serial=False):
        """Run *r* in a thread; returns it (join it yourself). A test that
        fails before its Stop still stops and joins it (no run outlives
        its test)."""
        def go():
            with _lanes(serial), mock.patch.object(tempfile, "tempdir",
                                                   self.temp):
                r.run()
        runner = threading.Thread(target=go)
        runner.start()

        def stop():
            if runner.is_alive() and getattr(r, "worker", None) is not None:
                r.worker.cancel()
            runner.join(30)
        self.addCleanup(stop)
        return runner

    def _hole_run(self, serial, hooks=None):
        """Phase 2 (the south band, from a file) leaves two holes — a 2 m
        and the 30 m tile; band 1's z15 holds until the hole unit is
        queued on the pool's fetch lane. *hooks*: more call hooks."""
        holes = {"tile_N47.20E8.00_2m.abt", "tile_N47.50E8.00_30m.abt"}
        holder = {}

        def first_fetch(info, kw):
            branch = holder["r"].worker._branches[0]
            self.assertTrue(_wait_for(lambda: any(
                key == ("holes",) for key, _c in list(branch._units))),
                "the hole unit was never queued")

        r = _LaneRun(self, self.out, self.cache, extent=_THREE, holes=holes,
                     hooks={("N", 15): first_fetch, **(hooks or {})},
                     entries=[{"kind": "files", "infos": _files_a()},
                              {"kind": "xyz", "uri": _XYZ,
                               "extent": dict(_THREE)}],
                     converter=_Converter(
                         coverage={n: (1, 2) for n in holes}))
        holder["r"] = r
        with _lanes(serial), mock.patch.object(tempfile, "tempdir",
                                               self.temp):
            r.run()
        self.assertEqual(r.errs, [])
        self.assertEqual(r.oks, [self.out])
        self.assertEqual(len(self.outputs()), 18)
        self.assertEqual(self.leftovers(), [])
        return r


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

class TwoLanes(_LaneCase):

    def test_a_band_derives_only_after_its_finest_call_ended(self):
        # Every z15 call takes a while: a lane that started a band's z12
        # with (or before) its z15 would be caught here.
        hooks = {(b, 15): lambda _i, _kw: time.sleep(0.1) for b in "NMS"}
        r = self.lane_run(extent=_THREE, hooks=hooks)
        self.assertEqual(r.errs, [])
        self.assertEqual(r.oks, [self.out])
        for band in "NMS":
            self.assertGreaterEqual(r.call(band, 12)["start"],
                                    r.call(band, 15)["end"], band)
        # One band behind: a band's coarser pass waits for the NEXT band's
        # finest call as well; the last band's runs at once.
        self.assertGreaterEqual(r.call("N", 12)["start"],
                                r.call("M", 15)["end"])
        self.assertGreaterEqual(r.call("M", 12)["start"],
                                r.call("S", 15)["end"])
        # The finest calls on the fetch lane, the coarser ones on the
        # derive lane, each north to south.
        self.assertEqual(r.order("waveshed-download-"),
                         [("N", 15), ("M", 15), ("S", 15)])
        self.assertEqual(r.order("waveshed-derive-"),
                         [("N", 12), ("M", 12), ("S", 12)])
        self.assertIn(mct._lanes_line(False), r.logs)
        self.assertEqual(sum(1 for m in r.logs if "download lanes:" in m), 1)
        # Once per run, before the lanes: QGIS's own resident memory
        # (process_memory.memory_line; owner 2026-11-07).
        mem = [m for m in r.logs if m.startswith("  QGIS process:")]
        self.assertEqual(len(mem), 1, r.logs[:6])
        self.assertLess(r.logs.index(mem[0]),
                        r.logs.index(mct._lanes_line(False)))
        # Every band was converted, one batch per band.
        self.assertEqual(len(r.converter.calls), 3)
        self.assertEqual(len(self.outputs()), 18)
        self.assertEqual(self.leftovers(), [])

    def test_the_band_after_next_fetches_while_a_band_derives(self):
        # Band 1's z12 (released by band 2's z15 landing) holds until band
        # 3's z15 has started: one lane would never get there (band 3
        # would wait for band 1's z12), and a lane that held band 1's z12
        # for band 3's z15 as well would deadlock here.
        s15_started = threading.Event()
        seen = {}

        def n12(info, kw):
            seen["s15_during_n12"] = s15_started.wait(10)

        hooks = {("N", 12): n12,
                 ("S", 15): lambda info, kw: s15_started.set()}
        r = self.lane_run(extent=_THREE, hooks=hooks)
        self.assertEqual(r.errs, [])
        self.assertTrue(seen["s15_during_n12"],
                        "band 3's z15 waited for band 1's coarser pass")
        self.assertGreaterEqual(r.call("N", 12)["start"],
                                r.call("M", 15)["end"])
        self.assertLess(r.call("S", 15)["start"], r.call("N", 12)["end"])
        self.assertEqual(r.oks, [self.out])

    def test_each_lanes_panel_lines_carry_its_zoom(self):
        # Two lanes log into one panel: while a call runs, its lane's
        # thread carries the call's zoom as the panel tag ("[z15] ",
        # "[z12] "); after the call the tag is gone from that thread.
        r = self.lane_run(extent=_TWO)
        self.assertEqual(r.errs, [])
        self.assertEqual(len(r.calls), 4)
        self.assertEqual({(c["zoom"], c["tag"]) for c in r.calls},
                         {(15, "[z15] "), (12, "[z12] ")})
        self.assertEqual(ta.panel_tag(), "")

    def test_one_lane_sets_no_tag(self):
        # The A/B switch: one lane, the lines as before.
        r = self.lane_run(serial=True, extent=_TWO)
        self.assertEqual(r.errs, [])
        self.assertEqual(len(r.calls), 4)
        self.assertEqual({c["tag"] for c in r.calls}, {""})

    def test_a_held_band_is_released_by_the_next_bands_finest_only(self):
        # Band 2's z15 is slow; band 1's z12 is queued long before it
        # lands and must not start until it has — however long the derive
        # lane idles.
        hooks = {("M", 15): lambda _i, _kw: time.sleep(0.3)}
        r = self.lane_run(extent=_THREE, hooks=hooks)
        self.assertEqual(r.errs, [])
        n12, m15 = r.call("N", 12), r.call("M", 15)
        self.assertGreaterEqual(n12["start"], m15["end"])
        self.assertGreaterEqual(m15["end"] - m15["start"], 0.3)
        # Its timing line says so: held until band 2/3's z15 landed.
        lines = [_UNIT_RE.match(m) for m in r.logs
                 if m.startswith("  timing: band 1/3:")]
        self.assertEqual(len(lines), 1, r.logs)
        self.assertIsNotNone(lines[0])
        self.assertEqual(lines[0]["gate"], "band 2/3's")
        self.assertGreaterEqual(float(lines[0]["held_s"]), 0.2)
        # The last band is never held: no suffix.
        last = [_UNIT_RE.match(m) for m in r.logs
                if m.startswith("  timing: band 3/3:")]
        self.assertEqual(len(last), 1)
        self.assertIsNone(last[0]["held"])

    def test_unit_done_fires_once_after_both_lanes(self):
        orig = _MapConverterWorker._unit_done
        seen = []
        holder = {}

        def spy(worker, pool, key):
            run = holder["r"]
            with run.lock:
                ended = {(c["band"], c["zoom"]) for c in run.calls
                         if c["end"] is not None}
            seen.append((key, ended, time.perf_counter()))
            return orig(worker, pool, key)

        # Band 1's z12 is slow: band 2's z15 lands long before it.
        hooks = {("M", 12): lambda _i, _kw: time.sleep(0.3)}
        r = _LaneRun(self, self.out, self.cache, hooks=hooks, extent=_TWO,
                     entries=[{"kind": "xyz", "uri": _XYZ,
                               "extent": dict(_TWO)}])
        holder["r"] = r
        with _lanes(False), mock.patch.object(tempfile, "tempdir",
                                              self.temp), \
                mock.patch.object(_MapConverterWorker, "_unit_done", spy):
            r.run()
        self.assertEqual(r.errs, [])
        self.assertEqual([key for key, _e, _t in seen],
                         [("band", 95), ("band", 94)])
        for (key, ended, t), band in zip(seen, "MS"):
            self.assertLessEqual({(band, 15), (band, 12)}, ended, key)
            self.assertGreaterEqual(t, r.call(band, 12)["end"], key)
        # Each batch holds its band's tiles of BOTH zooms.
        self.assertEqual(
            [sorted({_band_of(os.path.basename(j["output_path"]))
                     for j in c["jobs"]}) for c in r.converter.calls],
            [["M"], ["S"]])
        self.assertEqual([len(c["jobs"]) for c in r.converter.calls], [6, 6])
        self.assertEqual(r.oks, [self.out])

    def test_the_hole_unit_goes_first_on_the_fetch_lane(self):
        # Band 2's z15 takes a while, so band 1's z12 is still held when
        # the hole unit's z12 becomes runnable (instant fake calls would
        # let the whole fetch lane finish before the derive lane looks).
        r = self._hole_run(serial=False,
                           hooks={("M", 15): lambda _i, _kw: time.sleep(0.3)})
        self.assertEqual(r.order("waveshed-download-"),
                         [("N", 15), ("holes", 15), ("M", 15)])
        # The derive lane takes the first unit it may run: the hole unit
        # is never held, band 1's z12 waits for band 2's z15 (the hole
        # unit's z15 in between does not release it).
        self.assertEqual(r.order("waveshed-derive-"),
                         [("holes", 12), ("N", 12), ("M", 12)])
        self.assertGreaterEqual(r.call("holes", 12)["start"],
                                r.call("holes", 15)["end"])
        self.assertGreaterEqual(r.call("N", 12)["start"],
                                r.call("M", 15)["end"])
        # The hole tiles' own batch came after both of their calls.
        self.assertIn("Phase 3 (Phase-2 holes): running the converter "
                      "(2 tiles)...", r.logs)


class SerialZooms(_LaneCase):
    """``WAVESHED_SERIAL_ZOOMS=1``: one lane, the order before the lanes."""

    def test_the_order_before_the_lanes_call_for_call(self):
        r = self._hole_run(serial=True)
        # The hole unit after the unit downloading when Phase 2 ended (its
        # z15 and z12), then band 2 — one thread.
        self.assertEqual([(c["band"], c["zoom"]) for c in r.calls],
                         [("N", 15), ("N", 12), ("holes", 15),
                          ("holes", 12), ("M", 15), ("M", 12)])
        self.assertEqual({c["thread"] for c in r.calls},
                         {"waveshed-download-0"})
        self.assertIn(mct._lanes_line(True), r.logs)
        self.assertNotIn(mct._lanes_line(False), r.logs)

    def test_bands_in_sequence_each_finest_first(self):
        r = self.lane_run(serial=True, extent=_THREE)
        self.assertEqual(r.errs, [])
        self.assertEqual([(c["band"], c["zoom"]) for c in r.calls],
                         [("N", 15), ("N", 12), ("M", 15), ("M", 12),
                          ("S", 15), ("S", 12)])
        for a, b in zip(r.calls, r.calls[1:]):
            self.assertGreaterEqual(b["start"], a["end"])

    def test_the_switch_is_read_once_per_run(self):
        reads = []
        real = mct._serial_zooms_requested

        def counted():
            reads.append(1)
            return real()

        with mock.patch.object(mct, "_serial_zooms_requested", counted):
            r = self.lane_run(serial=True, extent=_THREE)
        self.assertEqual(r.errs, [])
        self.assertEqual(len(reads), 1)
        for value, serial in (("1", True), ("true", True), ("ON", True),
                              ("0", False), ("", False), ("no", False)):
            with mock.patch.dict(os.environ,
                                 {mct.SERIAL_ZOOMS_ENV: value}):
                self.assertEqual(mct._serial_zooms_requested(), serial,
                                 value)
        with _lanes(False):
            self.assertFalse(mct._serial_zooms_requested())


# ---------------------------------------------------------------------------
# Stop and failure
# ---------------------------------------------------------------------------

class LaneStopAndFailure(_LaneCase):

    def test_stop_ends_both_lanes(self):
        # Band 3's z15 (fetch lane) and band 1's z12 (derive lane, released
        # by band 2's z15) are live processes, registered like the
        # converter's: ONE Stop kills both.
        procs = {}

        def live(name):
            def hook(info, kw):
                proc = procs[name] = _sleeper(self)
                kw["on_start"](proc)
                proc.wait(30)
                if kw["should_cancel"]():
                    raise ta.ConverterCancelled()
                raise RuntimeError("Terrain download failed: killed")
            return hook

        r = _LaneRun(self, self.out, self.cache, extent=_THREE,
                     hooks={("S", 15): live("fetch"),
                            ("N", 12): live("derive")},
                     entries=[{"kind": "xyz", "uri": _XYZ,
                               "extent": dict(_THREE)}])
        runner = self.start_bg(r)
        self.assertTrue(_wait_for(lambda: len(procs) == 2, timeout=20),
                        "the two lanes never ran at once")
        self.assertEqual({p.poll() for p in procs.values()}, {None})
        r.worker.cancel()
        runner.join(30)
        self.assertFalse(runner.is_alive(), "the worker did not end")
        for name, proc in procs.items():
            self.assertIsNotNone(proc.poll(), f"the {name} lane outlived Stop")
        branch = r.worker._branches[0]
        self.assertFalse(branch.alive())
        self.assertTrue(branch.ended)
        self.assertTrue(branch.cancelled)
        self.assertIsNone(branch.error)
        self.assertEqual(r.errs, [])
        self.assertEqual(r.oks, [])
        self.assertEqual(r.status[-1], "Cancelled.")
        self.assertEqual(self.outputs(), set())
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(r.worker.progress_model.activities(), [])

    def test_a_derive_lane_failure_halts_the_run_once(self):
        procs = {}

        def fetch(info, kw):
            # Killed by the halt, this download reports a FAILURE (not a
            # cancel): still not the run's error — the derive lane's is,
            # once.
            proc = procs["fetch"] = _sleeper(self)
            kw["on_start"](proc)
            proc.wait(30)
            raise RuntimeError("Terrain download failed: fetch killed")

        def derive(info, kw):
            _wait_for(lambda: "fetch" in procs, timeout=20)
            raise RuntimeError("Terrain download failed: the z12 pass broke")

        r = _LaneRun(self, self.out, self.cache, extent=_THREE,
                     hooks={("S", 15): fetch, ("N", 12): derive},
                     entries=[{"kind": "xyz", "uri": _XYZ,
                               "extent": dict(_THREE)}])
        t0 = time.monotonic()
        with _lanes(False), mock.patch.object(tempfile, "tempdir",
                                              self.temp):
            r.run()
        self.assertLess(time.monotonic() - t0, 20, "the fetch lane lived on")
        self.assertEqual(r.errs, ["Terrain download failed: the z12 pass "
                                  "broke"])
        self.assertIsNotNone(procs["fetch"].poll(), "the fetch lane lives")
        branch = r.worker._branches[0]
        self.assertEqual(branch.error, r.errs[0])
        self.assertTrue(branch.ended)
        self.assertNotIn("Cancelled.", r.status)
        self.assertEqual(r.oks, [])
        # Band 1's batch never formed (its unit failed), band 2's coarser
        # pass never ran (the lane ended), band 3 was killed.
        self.assertEqual(r.converter.calls, [])
        self.assertEqual(self.outputs(), set())
        self.assertEqual(self.leftovers(), [])

    def test_a_fetch_lane_failure_stops_the_derive_lane(self):
        procs = {}

        def derive(info, kw):
            proc = procs["derive"] = _sleeper(self)
            kw["on_start"](proc)
            proc.wait(30)
            if kw["should_cancel"]():
                raise ta.ConverterCancelled()
            raise RuntimeError("Terrain download failed: derive killed")

        def fetch(info, kw):
            _wait_for(lambda: "derive" in procs, timeout=20)
            raise RuntimeError("Terrain download failed: z15 broke")

        r = self.lane_run(extent=_THREE,
                          hooks={("S", 15): fetch, ("N", 12): derive})
        self.assertEqual(r.errs, ["Terrain download failed: z15 broke"])
        self.assertIsNotNone(procs["derive"].poll(), "the derive lane lives")
        self.assertEqual(r.oks, [])


# ---------------------------------------------------------------------------
# Progress: both running passes
# ---------------------------------------------------------------------------

class LaneProgress(_LaneCase):

    def test_the_label_names_both_running_passes(self):
        s15_reported, m12_seen = threading.Event(), threading.Event()
        seen = {}
        holder = {}

        def s15(info, kw):
            _event(kw, 15, 40, 100)
            s15_reported.set()
            m12_seen.wait(10)

        def m12(info, kw):
            _event(kw, 12, 3, 10)
            seen["both"] = s15_reported.wait(10)
            model = holder["r"].worker.progress_model
            seen["label"] = model.label()
            seen["acts"] = model.activities()
            m12_seen.set()

        r = _LaneRun(self, self.out, self.cache, extent=_THREE,
                     hooks={("S", 15): s15, ("N", 12): m12},
                     entries=[{"kind": "xyz", "uri": _XYZ,
                               "extent": dict(_THREE)}])
        holder["r"] = r
        with _lanes(False), mock.patch.object(tempfile, "tempdir",
                                              self.temp):
            r.run()
        self.assertEqual(r.errs, [])
        self.assertTrue(seen["both"])
        self.assertIn("Downloading terrain z15 (2 m) band 3/3: 40/100 "
                      "source tiles", seen["label"])
        self.assertIn("Downloading terrain z12 (30 m) band 1/3: 3/10 source "
                      "tiles", seen["label"])
        self.assertEqual(len(seen["acts"]), 2, seen["acts"])
        model = r.worker.progress_model
        self.assertEqual(model.activities(), [])
        for key in model.keys():
            self.assertEqual(model.done(key), model.work(key), key)
        self.assertEqual(model.percent(), 100)

    def test_two_calls_of_one_slot_at_once_are_kept_apart(self):
        # A zoom that is one band's finest and the band before's coarser:
        # two calls of ONE banded slot run at the same time.
        model = rp.RunProgress()
        ledger = mct._SlotLedger(model)
        key = ("download", 0, 13)
        for band, units in ((96, 100.0), (95, 50.0), (94, 30.0)):
            ledger.add(key, band, units)
        model.set_work(key, ledger.total(key))
        model.set_work(("convert",), 100.0)
        fracs = []
        a = ledger.begin(key, 96)                 # derive lane: band 1
        b = ledger.begin(key, 95)                 # fetch lane: band 2
        self.assertEqual((a.est, b.est), (100.0, 50.0))
        ledger.report(a, 10, total=120, ceiling=20)
        fracs.append(model.fraction())
        ledger.report(b, 5, total=60, ceiling=8)
        fracs.append(model.fraction())
        # Both counted, neither twice: done 10 + 5, work 120 + 60 + 30.
        self.assertEqual(model.done(key), 15.0)
        self.assertEqual(model.work(key), 210.0)
        ledger.report(a, 60, total=120)
        ledger.report(b, 30, total=60, ceiling=40)
        fracs.append(model.fraction())
        self.assertEqual(model.done(key), 90.0)
        ledger.end(a, 120.0)                      # band 1's call is over
        fracs.append(model.fraction())
        self.assertEqual(model.done(key), 150.0)  # 120 + b's 30
        self.assertEqual(model.work(key), 210.0)
        ledger.report(a, 999, total=999)          # ended: ignored
        self.assertEqual(model.work(key), 210.0)
        ledger.end(b, 60.0)
        c = ledger.begin(key, 94)
        ledger.end(c, 30.0)
        fracs.append(model.fraction())
        self.assertEqual(model.done(key), 210.0)
        self.assertEqual(model.work(key), 210.0)
        self.assertEqual(fracs, sorted(fracs))
        model.finish(("convert",))
        self.assertEqual(model.fraction(), 1.0)

    def test_one_call_at_a_time_reports_as_before(self):
        # The banded slot of the one-lane order: the same numbers the
        # ledger gave before calls were kept apart (base + call + rest).
        model = rp.RunProgress()
        ledger = mct._SlotLedger(model)
        key = ("download", 0, 12)
        ledger.add(key, 95, 100.0)
        ledger.add(key, 94, 100.0)
        model.set_work(key, ledger.total(key))
        first = ledger.begin(key, 95)
        sink = mct._DownloadProgressSink(model, key, 30, 1, 1, call=first)
        sink.download_event({"event": "progress", "done_units": 40,
                             "total_units": 120, "ceiling_units": 60,
                             "zoom": 12})
        self.assertEqual((model.done(key), model.work(key)), (40.0, 220.0))
        ledger.end(first, sink.units())
        self.assertEqual((model.done(key), model.work(key)), (120.0, 220.0))
        second = ledger.begin(key, 94)
        sink = mct._DownloadProgressSink(model, key, 30, 1, 1, call=second)
        sink(0.5, "half")                         # no events: its estimate
        self.assertEqual(model.done(key), 170.0)
        ledger.end(second, sink.units())
        self.assertEqual((model.done(key), model.work(key)), (220.0, 220.0))


# ---------------------------------------------------------------------------
# The timing line
# ---------------------------------------------------------------------------

class LaneTiming(_LaneCase):

    def _lines(self, r):
        return [(i, _UNIT_RE.match(m)) for i, m in enumerate(r.logs)
                if m.startswith("  timing: band") and "landed at" in m]

    def test_one_line_per_band_naming_the_overlap(self):
        # Band 1's z12 (released by band 2's z15 landing) and band 3's z15
        # wait for each other to start: they surely ran side by side.
        n12_started, s15_started = threading.Event(), threading.Event()

        def n12(info, kw):
            n12_started.set()
            s15_started.wait(10)

        def s15(info, kw):
            s15_started.set()
            n12_started.wait(10)

        # Band 2's z15 takes a while: band 1's z12 is surely held for it.
        r = self.lane_run(extent=_THREE,
                          hooks={("N", 12): n12, ("S", 15): s15,
                                 ("M", 15): lambda _i, _kw: time.sleep(0.3)})
        self.assertEqual(r.errs, [])
        lines = self._lines(r)
        self.assertEqual(len(lines), 3, r.logs)
        for _i, m in lines:
            self.assertIsNotNone(m)
        (i1, b1), (i2, b2), (i3, b3) = lines
        # Band 1: held for band 2's z15, then beside band 3's.
        self.assertEqual((b1["what"], b1["zoom"], b1["other"], b1["oz"],
                          b1["verdict"], b1["note"], b1["gate"]),
                         ("band 1/3", "15", "band 3/3's", "15", "yes", None,
                          "band 2/3's"))
        # Band 2: band 3's z15 had landed when its coarser pass ran (held
        # for it, or found landed — the fake's calls are quick).
        self.assertEqual((b2["what"], b2["verdict"], b2["note"]),
                         ("band 2/3", "no", " (none was downloading)"))
        self.assertIn(b2["gate"], (None, "band 3/3's"))
        # Band 3, the last: never held, nothing beside it.
        self.assertEqual((b3["what"], b3["verdict"], b3["note"], b3["held"]),
                         ("band 3/3", "no", " (none was downloading)", None))
        self.assertEqual(r.logs[i3],
                         f"  timing: band 3/3: z15 landed at +{b3['fine']} "
                         f"s, coarser passes landed at +{b3['coarse']} s, "
                         f"ran beside another band's z15: no (none was "
                         f"downloading)")
        for m in (b1, b2, b3):
            self.assertLessEqual(float(m["fine"]), float(m["coarse"]))
        # The z15 passes landed north to south (one decimal).
        self.assertLessEqual(float(b1["fine"]), float(b2["fine"]))
        self.assertLessEqual(float(b2["fine"]), float(b3["fine"]))
        # Each band's line comes before its batch's (which is unchanged).
        batch = [i for i, m in enumerate(r.logs) if _BATCH_RE.match(m)]
        self.assertEqual(len(batch), 3, r.logs)
        self.assertLess(i1, batch[0])
        self.assertLess(i2, batch[1])
        self.assertLess(i3, batch[2])

    def test_one_lane_says_no(self):
        r = self.lane_run(serial=True)
        self.assertEqual(r.errs, [])
        lines = self._lines(r)
        self.assertEqual(len(lines), 2, r.logs)
        (_i1, b1), (_i2, b2) = lines
        self.assertEqual((b1["what"], b1["other"], b1["verdict"],
                          b1["note"]), ("band 1/2", "band 2/2's", "no", None))
        self.assertEqual((b2["what"], b2["verdict"], b2["note"]),
                         ("band 2/2", "no", " (none was downloading)"))

    def test_a_band_with_one_zoom_has_no_line(self):
        # 30 m only: one call per band, as before — no unit line.
        r = self.lane_run(resolutions=(30,), extent=_TWO)
        self.assertEqual(r.errs, [])
        self.assertEqual(self._lines(r), [])
        self.assertEqual(r.order("waveshed-derive-"), [])


if __name__ == "__main__":
    unittest.main()
