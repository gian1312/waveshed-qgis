"""Three loops that did one disk round trip at a time, now parallel or
throttled, with the same answers:

* ``preflight_ingest_jobs`` — 30,000 ``os.path.exists`` in a row cost 42 s
  before Phase 2 of the owner's Map Converter run (busy HDD). Threads now;
  each distinct path still checked once; the first missing path named.
* ``link_pbf_view`` — 21,000 hard links in a row cost 12 s per batch;
  threads pay on the container's network mount (2,000 links 12.6 s → 4.4 s)
  but not on the owner's NTFS HDD (bench 2026-11-07, 20,000 links: one
  thread 2,120/s, sixteen 1,540/s), so Windows links one after the other
  (``link_threads``, ``WAVESHED_LINK_THREADS`` overrides for an A/B run).
* ``_run_converter_download_once`` — the converter prints a progress line
  per percent; the panel gets every 10 %, the parsers every line.

QGIS stubs provided by conftest.py.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import os
import tempfile
import threading
import unittest
from unittest import mock

from waveshed.core import terrain_adapter as ta


def _touch(path: str, text: str = "") -> None:
    with open(path, "w") as fh:
        fh.write(text)


class TestPreflightParallel(unittest.TestCase):
    N = 300  # well beyond the inline threshold

    def _setup(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        paths = [os.path.join(d.name, f"f{i:04d}.tif") for i in range(self.N)]
        for p in paths:
            _touch(p)
        # Four jobs referencing the same files: 1,200 references, 300 paths.
        jobs = [{"output_path": os.path.join(d.name, "out", f"t{i}.abt"),
                 "sources": [{"path": p} for p in paths]} for i in range(4)]
        return d.name, paths, jobs

    def test_each_distinct_path_checked_once_in_parallel(self):
        self.assertGreaterEqual(self.N, ta._PREFLIGHT_PARALLEL_MIN)
        _d, paths, jobs = self._setup()
        seen, lock = [], threading.Lock()
        real = os.path.exists

        def counting(p):
            with lock:
                seen.append(p)
            return real(p)

        with mock.patch.object(ta.os.path, "exists", side_effect=counting):
            ta.preflight_ingest_jobs(jobs)
        wanted = set(paths)
        checks = [p for p in seen if p in wanted]
        self.assertEqual(len(checks), self.N, "once per distinct path")
        self.assertEqual(set(checks), wanted)

    def test_first_missing_path_is_named(self):
        _d, paths, jobs = self._setup()
        os.remove(paths[150])
        os.remove(paths[250])
        with self.assertRaises(RuntimeError) as cm:
            ta.preflight_ingest_jobs(jobs)
        self.assertIn("vanished before the converter ran", str(cm.exception))
        self.assertIn(os.path.basename(paths[150]), str(cm.exception))
        self.assertNotIn(os.path.basename(paths[250]), str(cm.exception),
                         "the FIRST missing path, as before")

    def test_small_batches_stay_inline(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        paths = [os.path.join(d.name, f"g{i}.tif") for i in range(5)]
        for p in paths:
            _touch(p)
        jobs = [{"output_path": os.path.join(d.name, "o.abt"),
                 "sources": paths}]
        ta.preflight_ingest_jobs(jobs)          # no raise
        os.remove(paths[2])
        with mock.patch("concurrent.futures.ThreadPoolExecutor") as pool:
            with self.assertRaises(RuntimeError) as cm:
                ta.preflight_ingest_jobs(jobs)
            pool.assert_not_called()
        self.assertIn("g2.tif", str(cm.exception))


class TestLinkPbfViewParallel(unittest.TestCase):
    N = 200

    def _store(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        store = os.path.join(d.name, "store")
        view = os.path.join(d.name, "view")
        os.makedirs(store)
        names = [f"{i:05d}.pbf" for i in range(self.N)]
        for n in names:
            _touch(os.path.join(store, n), n)
        return store, view, names

    def test_every_name_is_linked(self):
        self.assertGreaterEqual(self.N, ta._LINK_PARALLEL_MIN)
        store, view, names = self._store()
        self.assertEqual(ta.link_pbf_view(store, view, names), self.N)
        self.assertEqual(sorted(os.listdir(view)), names)
        for n in names[:5] + names[-5:]:
            with open(os.path.join(view, n)) as fh:
                self.assertEqual(fh.read(), n)
        # A second call over the same view links nothing and removes a
        # stranger, as before.
        _touch(os.path.join(view, "stranger.pbf"))
        with mock.patch.object(ta.os, "link",
                               side_effect=AssertionError("relinked")):
            self.assertEqual(ta.link_pbf_view(store, view, names), self.N)
        self.assertNotIn("stranger.pbf", os.listdir(view))

    def test_a_missing_store_tile_is_named(self):
        store, view, names = self._store()
        os.remove(os.path.join(store, names[77]))
        with self.assertRaises(RuntimeError) as cm:
            ta.link_pbf_view(store, view, names)
        self.assertIn(names[77], str(cm.exception))

    def test_copy_where_the_file_system_has_no_links(self):
        store, view, names = self._store()
        with mock.patch.object(ta.os, "link", side_effect=OSError("no links")):
            self.assertEqual(ta.link_pbf_view(store, view, names), self.N)
        self.assertEqual(sorted(os.listdir(view)), names)
        with open(os.path.join(view, names[123])) as fh:
            self.assertEqual(fh.read(), names[123])

    def test_windows_links_one_after_the_other(self):
        # Owner's bench 2026-11-07 (NTFS HDD, 20,000 links): one thread
        # 2,120/s, sixteen 1,540/s — the directory inserts are serialised,
        # threads only contend. Elsewhere the 16 threads stay; the env knob
        # overrides either for an A/B run.
        with mock.patch.dict(os.environ, {ta.LINK_THREADS_ENV: ""}):
            with mock.patch.object(ta, "_is_windows", return_value=True):
                self.assertEqual(ta.link_threads(), 1)
            with mock.patch.object(ta, "_is_windows", return_value=False):
                self.assertEqual(ta.link_threads(), 16)
        for raw, want_win, want_posix in (("4", 4, 4), ("0", 1, 16),
                                          ("x", 1, 16), (" 8 ", 8, 8)):
            with mock.patch.dict(os.environ, {ta.LINK_THREADS_ENV: raw}):
                with mock.patch.object(ta, "_is_windows", return_value=True):
                    self.assertEqual(ta.link_threads(), want_win, raw)
                with mock.patch.object(ta, "_is_windows", return_value=False):
                    self.assertEqual(ta.link_threads(), want_posix, raw)
        # One thread: a big batch links inline, no pool, same result.
        store, view, names = self._store()
        self.assertGreaterEqual(self.N, ta._LINK_PARALLEL_MIN)
        with mock.patch.object(ta, "link_threads", return_value=1), \
                mock.patch("concurrent.futures.ThreadPoolExecutor") as pool:
            self.assertEqual(ta.link_pbf_view(store, view, names), self.N)
            pool.assert_not_called()
        self.assertEqual(sorted(os.listdir(view)), names)


class TestDownloadPanelStep(unittest.TestCase):
    HEADER = "[Download] z=15 tiles=638x35=22330 (~4361MB) connections=256"
    STALL = ("[Download] STALL tile z=15/x=16886/y=11440: first byte after "
             "0.3s, longest body gap 10.1s")
    STATS = "[Stats] Tiles: 22330/22330 OK (100.0% success)"

    def _progress(self, pct):
        return (f"[Download] {pct}% ({pct * 223}/22330) — 40.0 MB/s, 0 errors, "
                f"255 in-flight, window 256")

    def test_panel_every_ten_percent_parsers_every_line(self):
        emitted = [self.HEADER] + [self._progress(p) for p in range(1, 101)] \
            + ["[Tile] tile_N47.00E8.00_2m.abt voids=0", self.STALL, self.STATS]
        logged, seen = [], []

        def fake_stream(exe, args, on_line, on_start=None, env=None):
            for line in emitted:
                on_line(line)
            return 0

        with mock.patch.object(ta, "run_converter_streaming", fake_stream), \
                mock.patch.object(ta, "_log", side_effect=logged.append):
            rc, lines = ta._run_converter_download_once(
                "conv", "job.json", on_line=seen.append)
        self.assertEqual(rc, 0)
        self.assertEqual(lines, emitted, "the pass's lines, all of them")
        self.assertEqual(seen, emitted, "the progress parser sees every line")
        panel = [l.strip() for l in logged]
        progress = [l for l in panel if ta._parse_download_progress(l)]
        self.assertEqual([ta._parse_download_progress(l)[0] for l in progress],
                         list(range(10, 101, 10)))
        self.assertEqual([l for l in panel if not ta._parse_download_progress(l)],
                         [self.HEADER, self.STALL, self.STATS],
                         "the per-tile voids line is read, never shown")

    def test_a_panel_tag_prefixes_the_panel_copy_only(self):
        # Two Map Converter lanes log into one panel: a lane sets its tag
        # for its thread and the panel copy carries it; the raw lines and
        # the parser's copy never do. Another thread sees no tag.
        emitted = [self.HEADER, self._progress(10), self.STATS]
        logged, seen, other = [], [], []

        def fake_stream(exe, args, on_line, on_start=None, env=None):
            for line in emitted:
                on_line(line)
            t = threading.Thread(target=lambda: other.append(ta.panel_tag()))
            t.start()
            t.join()
            return 0

        ta.set_panel_tag("[z14] ")
        try:
            with mock.patch.object(ta, "run_converter_streaming",
                                   fake_stream), \
                    mock.patch.object(ta, "_log", side_effect=logged.append):
                _rc, lines = ta._run_converter_download_once(
                    "conv", "job.json", on_line=seen.append)
        finally:
            ta.set_panel_tag(None)
        self.assertEqual([l.strip() for l in logged],
                         [f"[z14] {l}" for l in emitted])
        self.assertEqual(lines, emitted)
        self.assertEqual(seen, emitted)
        self.assertEqual(other, [""], "the tag is this thread's only")
        self.assertEqual(ta.panel_tag(), "")

    def test_the_last_line_of_a_short_pass_is_shown(self):
        # A repair pass of two tiles prints 50 % and 100 %: both reach the
        # panel (50 is a step, 100 is the end).
        emitted = ["[Download] 50% (1/2) — 0.0 MB/s, 0 errors, 1 in-flight, "
                   "window 256",
                   "[Download] 100% (2/2) — 0.1 MB/s, 0 errors, 0 in-flight, "
                   "window 256"]
        logged = []

        def fake_stream(exe, args, on_line, on_start=None, env=None):
            for line in emitted:
                on_line(line)
            return 0

        with mock.patch.object(ta, "run_converter_streaming", fake_stream), \
                mock.patch.object(ta, "_log", side_effect=logged.append):
            ta._run_converter_download_once("conv", "job.json")
        self.assertEqual([l.strip() for l in logged], emitted)


if __name__ == "__main__":
    unittest.main()
