"""The Map Converter's Auto pick of the output resolutions (owner, 2026-11-07:
"improve the auto pick to a reasonable range, especially with more than one
coverage layer").

The only automatic choice used to be 30 m ticked when the tab opened. The
owner's stack — the swisstopo 2 m folder and the Mapzen 4.8 m XYZ service
over the same extent — was ticked 2/5/10/30/90 by hand, and the 616 tiles of
5 m and 10 m (~25 GB) were built for nothing: with a 2 m tile everywhere the
analysis never takes a 5 m one. Pinned here:

* ``auto_output_resolutions(entries)``: the finest native resolution of the
  TERRAIN rows (building rows never count), snapped up to a tile resolution
  (finer only beyond 10 % — the run's own upsampling rule), plus the 30 m and
  90 m rings of the Site Analysis adaptive profile coarser than it; no
  terrain row with a known resolution → [30]. ONE fine resolution for the
  whole stack;
* the tab: 30 m until the first row arrives; the rule applied on every stack
  change (row added / removed, extent set) until a box is ticked by hand; the
  Auto button right after the boxes applies it again and re-arms it; one log
  line when applied; the estimate refreshed through the off-thread path.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import os
import tempfile
import unittest
from unittest import mock

import waveshed.gui.map_converter_tab as mct
from waveshed.core import range_profile, terrain_index
from waveshed.gui.map_converter_tab import _LayerEntry, auto_output_resolutions
from test_two_phase_convert import _Text

MAPZEN = ("type=xyz&url=https://s3.amazonaws.com/elevation-tiles-prod/"
          "terrarium/%7Bz%7D/%7Bx%7D/%7By%7D.png&zmax=15")
EXT = {"south": 45.7648, "north": 48.1226, "west": 4.8094, "east": 11.5693}

OWNER_LINE = ("  output resolutions: auto 2 m, 30 m, 90 m (finest source "
              "2.0 m; the analysis profile's 30 m and 90 m rings)")


def _row(native, layer_type="raster", source="/dem/a.tif"):
    return _LayerEntry(layer_type=layer_type, source_path=source,
                       native_res_m=native)


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------

class TheRule(unittest.TestCase):

    def test_the_owner_stack_gives_one_fine_resolution(self):
        stack = [_row(2.0, source="/data/swissalti3d"),
                 _row(4.8, source=MAPZEN)]
        self.assertEqual(auto_output_resolutions(stack), [2, 30, 90])
        self.assertEqual(auto_output_resolutions(stack[::-1]), [2, 30, 90])
        self.assertEqual(mct._auto_resolutions_line(stack), OWNER_LINE)

    def test_snapped_up_to_a_tile_resolution(self):
        for native, expected in [
                (2.0, [2, 30, 90]), (4.8, [5, 30, 90]), (9, [10, 30, 90]),
                (25, [30, 90]), (30, [30, 90]), (85, [90]), (90, [90]),
                (250, [250]), (300, [250]), (1000, [250]),
                (0.5, [2, 30, 90]), (1.0, [2, 30, 90])]:
            with self.subTest(native=native):
                self.assertEqual(auto_output_resolutions([_row(native)]),
                                 expected)

    def test_a_source_up_to_ten_percent_coarser_counts_as_that_resolution(
            self):
        # Arc-second data at the equator, float noise, the 10 % edge: the
        # run's upsampling rule (_upsampling_warnings) says no detail is
        # lost there, and the next tile resolution up would throw it away.
        for native, expected in [
                (2.1, [2, 30, 90]), (2.2, [2, 30, 90]), (2.3, [5, 30, 90]),
                (5.5, [5, 30, 90]), (5.6, [10, 30, 90]),
                (30.9, [30, 90]),          # 1″ (Copernicus, SRTM) at 0°
                (33.3, [30, 90]), (33.4, [90]),
                (92.6, [90]),              # 3″ at 0°
                (100.0, [90]), (100.1, [250])]:
            with self.subTest(native=native):
                self.assertEqual(auto_output_resolutions([_row(native)]),
                                 expected)

    def test_the_pick_never_upsamples_and_the_next_finer_would(self):
        keys = sorted(mct._ABT_EXTENT_DEG)
        for native in (0.3, 0.5, 1.9, 2.0, 2.2, 3.0, 4.4, 4.8, 5.5, 5.6, 7.0,
                       9.0, 10.0, 11.1, 12.0, 20.0, 25.0, 30.0, 30.9, 33.3,
                       34.0, 50.0, 85.0, 90.0, 92.6, 100.0, 101.0, 150.0,
                       250.0, 277.0):
            row = _row(native)
            fine = auto_output_resolutions([row])[0]
            with self.subTest(native=native, fine=fine):
                self.assertEqual(mct._upsampling_warnings([row], [fine]), [])
                if fine != keys[0]:
                    finer = keys[keys.index(fine) - 1]
                    self.assertTrue(mct._upsampling_warnings([row], [finer]))

    def test_the_rings_are_the_adaptive_profile(self):
        self.assertEqual(mct._AUTO_RINGS_M, (30, 90))
        self.assertEqual(range_profile.ADAPTIVE_FAR_STEP_M, 90)
        for base in (2, 5, 10):
            steps = {s for _u, s in range_profile.adaptive_bands(base, 50.0)}
            with self.subTest(base=base):
                self.assertTrue(set(mct._AUTO_RINGS_M) <= steps, steps)

    def test_one_fine_resolution_whatever_the_stack(self):
        stack = [_row(25.0), _row(4.8, source=MAPZEN), _row(9.0),
                 _row(2.0, source="/data/folder")]
        self.assertEqual(auto_output_resolutions(stack), [2, 30, 90])

    def test_without_a_known_terrain_resolution_30_m(self):
        osm = _row(None, "buildings", mct._OSM_PLACEHOLDER)
        fgb = _row(0.5, "buildings", "/b.fgb")      # never counts
        self.assertEqual(auto_output_resolutions([]), [30])
        self.assertEqual(auto_output_resolutions([osm]), [30])
        self.assertEqual(auto_output_resolutions([fgb]), [30])
        self.assertEqual(auto_output_resolutions([_row(None)]), [30])
        self.assertEqual(auto_output_resolutions([_row(0.0)]), [30])
        self.assertEqual(auto_output_resolutions([fgb, _row(25.0)]),
                         [30, 90])
        self.assertEqual(mct._auto_resolutions_line([osm]),
                         "  output resolutions: auto 30 m (no terrain row "
                         "with a known resolution)")

    def test_the_line_names_the_rings_added(self):
        self.assertEqual(
            mct._auto_resolutions_line([_row(25.0)]),
            "  output resolutions: auto 30 m, 90 m (finest source 25.0 m; "
            "the analysis profile's 90 m ring)")
        self.assertEqual(
            mct._auto_resolutions_line([_row(85.0)]),
            "  output resolutions: auto 90 m (finest source 85.0 m)")
        self.assertEqual(
            mct._auto_resolutions_line([_row(0.5)]),
            "  output resolutions: auto 2 m, 30 m, 90 m (finest source "
            "0.5 m; the analysis profile's 30 m and 90 m rings)")

    def test_the_tooltip_states_the_rule_and_the_result(self):
        tip = mct._auto_resolutions_tip([_row(2.0), _row(4.8)])
        for needle in ("finest native resolution of the terrain rows",
                       "building rows do not count", "10 %",
                       "30 m and 90 m rings", "ONE fine resolution",
                       "No terrain row with a known resolution: 30 m",
                       "This stack: 2 m, 30 m, 90 m (finest source 2.0 m; "
                       "the analysis profile's 30 m and 90 m rings).",
                       "until you tick a box by hand"):
            self.assertIn(needle, tip)


# ---------------------------------------------------------------------------
# The tab (stub widgets)
# ---------------------------------------------------------------------------

class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)

    def emit(self, *args):
        for fn in list(self.slots):
            fn(*args)


class _Check:
    """A QCheckBox as Qt behaves: ``setChecked`` emits ``stateChanged`` /
    ``toggled`` only when the state changes; ``click`` is the user's."""

    def __init__(self, checked=False, text=""):
        self._on = bool(checked)
        self.text = text
        self.stateChanged = _Signal()
        self.toggled = _Signal()
        self.setToolTip = mock.Mock()

    def isChecked(self):
        return self._on

    def setChecked(self, on):
        on = bool(on)
        if on != self._on:
            self._on = on
            self.stateChanged.emit(2 if on else 0)
            self.toggled.emit(on)

    def click(self):
        self.setChecked(not self._on)


class _Heavy:
    """The two estimates, recorded (resolutions asked) instead of run."""

    def __init__(self):
        self.asked = []

    def count(self, bbox, res, output_dir="", overwrite=True, entries=None):
        self.asked.append(list(res))
        return 1, 1

    @staticmethod
    def need(*_a, **_kw):
        return None

    def patch(self):
        return mock.patch.multiple(mct, _estimate_tile_count_and_mb=self.count,
                                   _estimate_disk_need=self.need)


def _tab(out_dir):
    tab = mct.MapConverterTab.__new__(mct.MapConverterTab)
    tab._worker = None
    tab._layers = []
    tab._est_cache = {}
    tab._res_checks = {r: _Check(r == 30, f"{r}m")
                       for r in sorted(mct._ABT_EXTENT_DEG)}
    tab._btn_auto_res = mock.Mock()
    tab._table = mock.Mock()
    tab._log = mock.Mock()
    tab._lbl_estimate = _Text()
    tab._chk_overwrite = mock.Mock(isChecked=mock.Mock(return_value=False))
    tab._chk_keep_pool = mock.Mock(isChecked=mock.Mock(return_value=True))
    tab._get_output_dir = lambda: out_dir
    tab._rebuild_table = lambda: None
    tab._reselect = lambda _rows: None
    tab._selected_rows = lambda: []
    tab._res_user_touched = False            # as __init__
    tab._res_applying = False
    tab._connect_res_checks()
    return tab


def _ticked(tab):
    return tab._get_output_resolutions()


def _auto_lines(tab):
    return [c.args[0] for c in tab._log.appendPlainText.call_args_list
            if c.args[0].startswith("  output resolutions:")]


def _tip(tab):
    return tab._btn_auto_res.setToolTip.call_args[0][0]


class _TabCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ws_autores_")
        self.out = os.path.join(self._tmp.name, "out")
        self.folder = os.path.join(self._tmp.name, "swissalti3d")
        os.makedirs(self.folder)
        patcher = mock.patch.object(mct, "QTimer",
                                    side_effect=lambda *_a: mock.MagicMock())
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)
        self.tab = _tab(self.out)

    def add_folder(self, px_m=2.0):
        """Add Folder… for a scanned folder whose finest file is *px_m*."""
        entries = {}
        for k, (w, s) in enumerate(((6.0, 46.0), (6.0137, 46.0))):
            entries[os.path.join(self.folder, f"t{k}.tif")] = {
                "readable": True, "crs_wkt": "LV95", "dtype": "Float32",
                "crs_authid": "EPSG:2056", "px_m": px_m,
                "wgs84_bounds": {"west": w, "east": w + 0.0137,
                                 "south": s, "north": s + 0.009}}
        index = mock.Mock(entries=entries)
        scan = mct.terrain_folder_scan
        with mock.patch.object(mct, "QFileDialog") as dialog, \
                mock.patch.object(scan, "scan_folder",
                                  return_value={"index": index}), \
                mock.patch.object(scan, "resolve_folder_problems",
                                  return_value=({}, {})), \
                mock.patch.object(scan, "suggested_crs",
                                  return_value="EPSG:2056"):
            dialog.getExistingDirectory.return_value = self.folder
            self.tab._on_add_folder()

    def add_layer(self, source, native=None, name="layer"):
        layer = mock.Mock()
        layer.source.return_value = source
        layer.name.return_value = name
        with mock.patch.object(mct, "_detect_resolution",
                               return_value=native):
            self.assertTrue(self.tab._add_qgis_layer(layer))
        return layer


class AutoFollowsTheStack(_TabCase):

    def test_the_owner_stack(self):
        heavy = _Heavy()
        self.assertEqual(_ticked(self.tab), [30])     # until the first row
        with heavy.patch():
            self.add_folder(2.0)
            self.assertEqual(_ticked(self.tab), [2, 30, 90])
            self.assertEqual(_auto_lines(self.tab), [OWNER_LINE])
            # The estimate of the new boxes, through the off-thread path.
            self.assertEqual(self.tab._lbl_estimate.text,
                             mct._ESTIMATING_TEXT)
            self.tab.flush_estimate()
            self.assertEqual(heavy.asked, [[2, 30, 90]])
            self.add_layer(MAPZEN, name="Mapzen")          # 4.8 m
            self.assertEqual(self.tab._layers[-1].native_res_m, 4.8)
            self.assertEqual(_ticked(self.tab), [2, 30, 90])
            self.assertEqual(len(_auto_lines(self.tab)), 1, "nothing changed")
            self.tab._set_extent_rows(dict(EXT), [1], "drawn")
            self.assertEqual(_ticked(self.tab), [2, 30, 90])
            self.tab.flush_estimate()
        self.assertEqual(heavy.asked, [[2, 30, 90], [2, 30, 90]])
        self.assertFalse(self.tab._res_user_touched)
        self.assertIn("This stack: 2 m, 30 m, 90 m (finest source 2.0 m",
                      _tip(self.tab))
        logged = [c.args[0] for c in
                  self.tab._log.appendPlainText.call_args_list]
        # The auto line follows the row's own line.
        self.assertTrue(logged[logged.index(OWNER_LINE) - 1].startswith(
            "Added folder: "))

    def test_a_manual_toggle_stops_it_and_auto_rearms_it(self):
        heavy = _Heavy()
        with heavy.patch():
            self.add_layer(MAPZEN, name="Mapzen")
            self.assertEqual(_ticked(self.tab), [5, 30, 90])
            self.tab._set_extent_rows(dict(EXT), [0], "drawn")
            self.tab._res_checks[10].click()              # by hand
            self.assertTrue(self.tab._res_user_touched)
            self.assertEqual(self.tab._lbl_estimate.text,
                             mct._ESTIMATING_TEXT)
            self.tab.flush_estimate()
            self.assertEqual(heavy.asked[-1], [5, 10, 30, 90])
            lines = len(_auto_lines(self.tab))
            # Rows added / an extent set no longer move the boxes ...
            self.add_folder(2.0)
            self.add_layer("/data/lidar.tif", native=0.5, name="lidar")
            self.tab._set_extent_rows(dict(EXT), [2], "drawn")
            self.assertEqual(_ticked(self.tab), [5, 10, 30, 90])
            self.assertEqual(len(_auto_lines(self.tab)), lines)
            # ... but the tooltip names what Auto would tick.
            self.assertIn("This stack: 2 m, 30 m, 90 m (finest source 0.5 m",
                          _tip(self.tab))
            self.tab.flush_estimate()
            self.assertEqual(heavy.asked[-1], [5, 10, 30, 90])
            # Auto applies the rule and re-arms it.
            self.tab._on_auto_resolutions()
            self.assertFalse(self.tab._res_user_touched)
            self.assertEqual(_ticked(self.tab), [2, 30, 90])
            self.assertEqual(
                _auto_lines(self.tab)[-1],
                "  output resolutions: auto 2 m, 30 m, 90 m (finest source "
                "0.5 m; the analysis profile's 30 m and 90 m rings)")
            self.assertEqual(self.tab._lbl_estimate.text,
                             mct._ESTIMATING_TEXT)
            self.tab.flush_estimate()
            self.assertEqual(heavy.asked[-1], [2, 30, 90])
            # Re-armed: removing the fine rows follows the stack again.
            self.tab._selected_rows = lambda: [1, 2]
            self.tab._on_remove()
            self.assertEqual([e.native_res_m for e in self.tab._layers],
                             [4.8])
            self.assertEqual(_ticked(self.tab), [5, 30, 90])
            self.assertEqual(
                _auto_lines(self.tab)[-1],
                "  output resolutions: auto 5 m, 30 m, 90 m (finest source "
                "4.8 m; the analysis profile's 30 m and 90 m rings)")
            self.tab.flush_estimate()
            self.assertEqual(heavy.asked[-1], [5, 30, 90])

    def test_auto_logs_once_even_when_nothing_changes(self):
        self.add_folder(2.0)
        before = len(_auto_lines(self.tab))
        generation = self.tab._est_generation
        self.tab._on_auto_resolutions()
        self.assertEqual(len(_auto_lines(self.tab)), before + 1)
        self.assertEqual(_auto_lines(self.tab)[-1], OWNER_LINE)
        self.assertEqual(self.tab._est_generation, generation + 1,
                         "one estimate request")

    def test_the_rule_own_ticks_are_not_the_user_s(self):
        self.add_layer(MAPZEN, name="Mapzen")
        generation = self.tab._est_generation
        self.add_folder(2.0)          # 5 m off, 2 m on: two box changes
        self.assertFalse(self.tab._res_user_touched)
        self.assertEqual(self.tab._est_generation, generation + 1,
                         "the stack change requests ONE estimate")
        self.assertIsNot(self.tab._res_applying, True)

    def test_building_rows_change_nothing(self):
        self.tab._init_estimate()
        generation = self.tab._est_generation
        self.tab._on_download_buildings()
        self.assertEqual(_ticked(self.tab), [30])
        self.assertEqual(_auto_lines(self.tab), [])
        # The row is a stack change all the same: the label is refreshed
        # (a buildings row changes the disk need; here none to price yet).
        self.assertEqual(self.tab._est_generation, generation + 1)
        self.assertEqual(self.tab._lbl_estimate.text,
                         "Add raster layers and set extents to see estimate.")
        self.add_layer("/data/dem25.tif", native=25.0)
        self.assertEqual(_ticked(self.tab), [30, 90])
        self.assertEqual(
            _auto_lines(self.tab),
            ["  output resolutions: auto 30 m, 90 m (finest source 25.0 m; "
             "the analysis profile's 90 m ring)"])


class TheBuiltTab(unittest.TestCase):
    """``__init__`` + ``_build_ui`` under stub widgets: 30 m ticked, Auto right
    after the boxes, every box wired to the toggle handler."""

    def test_construction(self):
        made = {}

        def factory(name):
            def make(*args, **_kw):
                widget = mock.MagicMock(name=name)
                made.setdefault(name, []).append((args, widget))
                return widget
            return make

        checks = []

        def make_check(text=""):
            cb = _Check(False, text)
            checks.append(cb)
            return cb

        names = ("QVBoxLayout", "QHBoxLayout", "QGroupBox", "QLabel",
                 "QPushButton", "QTableWidget", "QComboBox", "QLineEdit",
                 "QProgressBar", "QPlainTextEdit", "QgsMapLayerComboBox")
        patches = [mock.patch.object(mct, n, side_effect=factory(n))
                   for n in names]
        patches += [mock.patch.object(mct, "QCheckBox",
                                      side_effect=make_check),
                    mock.patch.object(mct, "Qt", mock.MagicMock()),
                    mock.patch.object(mct, "QTimer",
                                      side_effect=lambda *_a:
                                      mock.MagicMock())]
        for p in patches:
            p.start()
        try:
            tab = mct.MapConverterTab(mock.Mock())
        finally:
            for p in patches:
                p.stop()
        boxes = {cb.text: cb for cb in checks}
        self.assertEqual([t for t, cb in boxes.items()
                          if cb.isChecked() and t.endswith("m")
                          and t[:-1].isdigit()], ["30m"])
        self.assertFalse(tab._res_user_touched)
        auto = [w for args, w in made["QPushButton"] if args == ("Auto",)]
        self.assertEqual(len(auto), 1)
        self.assertIs(tab._btn_auto_res, auto[0])
        self.assertIn("This stack: 30 m (no terrain row with a known "
                      "resolution)", auto[0].setToolTip.call_args[0][0])
        # Right after the resolution boxes, before the two other boxes.
        row = [w for _a, w in made["QHBoxLayout"]
               if any(c.args and c.args[0] is boxes["2m"]
                      for c in w.addWidget.call_args_list)][0]
        added = [c.args[0] for c in row.addWidget.call_args_list]
        res_boxes = [boxes[f"{r}m"] for r in sorted(mct._ABT_EXTENT_DEG)]
        start = added.index(res_boxes[0])
        self.assertEqual(added[start:start + len(res_boxes) + 3],
                         res_boxes + [auto[0],
                                      boxes["Rebuild existing tiles"],
                                      boxes["Keep pool tiles"]])
        auto[0].clicked.connect.assert_called_once_with(
            tab._on_auto_resolutions)
        for cb in res_boxes:
            self.assertEqual(cb.stateChanged.slots, [tab._on_res_toggled])
        self.assertEqual(boxes["Rebuild existing tiles"].toggled.slots,
                         [tab._update_estimate])
        timer = tab._est_timer
        timer.setSingleShot.assert_called_once_with(True)
        timer.setInterval.assert_called_once_with(150)
        timer.timeout.connect.assert_called_once_with(tab._on_estimate_timer)
        # A box ticked by hand, then a row: the boxes stay.
        boxes["5m"].click()
        self.assertTrue(tab._res_user_touched)
        tab._layers.append(_row(2.0, source="/data/folder"))
        tab._rebuild_table = lambda: None
        tab._on_stack_changed()
        self.assertEqual(tab._get_output_resolutions(), [5, 30])


class TheHelpSaysSo(unittest.TestCase):

    def test_map_converter_topic(self):
        from waveshed import help as help_pkg
        body = " ".join(help_pkg.find_topic("map-converter").body.split())
        for needle in ("<b>Auto</b>", "30 m and 90 m rings",
                       "one fine resolution for the whole stack",
                       "until you tick one by hand",
                       "<i>Estimating tiles and disk space…</i>"):
            self.assertIn(needle, body)


if __name__ == "__main__":
    unittest.main()
