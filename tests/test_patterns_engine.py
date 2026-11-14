"""Antenna patterns stored in assets reach the engine (brief D2, point 5).

Pins: the job's ``tx.az_pattern_data`` / ``tx.el_pattern_data`` (floats,
unchanged, peak 1) and ``tx.az_rotation`` (whole degrees, only with a
pattern); Propagation mode only (LOS / LOS Floor send neither); every job a
360° row produces (fixed, adaptive, multi-site, altitudes); the P2P tab
(blank asset, unresolved asset, the CPU question); the engine gate
(``PATTERN_DATA_MIN_ENGINE``); the Assets tab's ``.az`` / ``.el`` import /
export and the Generic-beam peak normalisation.

Logic tests on the conftest stubs. The real widgets and the real engine were
driven separately (PyQGIS 3.22 offscreen, aether_core 0.4.10).
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import json
import os
import tempfile
import unittest
from unittest import mock

from waveshed.core import binary_manager as bm
from waveshed.core import range_profile as rp
from waveshed.core.job_builder import (
    CoverageParams,
    P2PParams,
    asset_patterns,
    build_coverage_job,
    build_p2p_job,
    has_pattern_data,
    peak_normalized,
    whole_degrees,
)
import waveshed.gui.asset_manager_tab as amt
import waveshed.gui.p2p_tab as p2p_mod
from waveshed.gui.p2p_tab import P2PAssetError, P2PTab
from waveshed.gui.site_analysis_tab import SiteAnalysisTab

AZ = [[float(a), 1.0 if a == 0 else 0.1] for a in range(0, 361, 10)]
EL = [[float(a), 1.0 if a == -5 else 0.2] for a in range(-90, 91, 5)]
DIRECTIONAL = {"name": "Dir", "frequency_mhz": 450.0, "erp_watts": 100.0,
               "azimuth_pattern": {"data": AZ},
               "elevation_pattern": {"data": EL}}
OMNI = {"name": "Omni", "frequency_mhz": 450.0, "erp_watts": 100.0,
        "azimuth_pattern": {"data": []}, "elevation_pattern": {"data": []}}


class BuildTx(unittest.TestCase):

    def _tx(self, **kw):
        kw.setdefault("model", "ITM")
        return build_coverage_job(CoverageParams(**kw), "/abt", "/out")["tx"]

    def test_inline_data_is_sent_unchanged_as_floats(self):
        tx = self._tx(az_pattern_data=[[0, 1], [180, 0.5]],
                      el_pattern_data=[[-90, 0.1], [0, 1], [90, 0.1]],
                      az_rotation=90.0)
        self.assertEqual(tx["az_pattern_data"], [[0.0, 1.0], [180.0, 0.5]])
        self.assertEqual(tx["el_pattern_data"], [[-90.0, 0.1], [0.0, 1.0], [90.0, 0.1]])
        self.assertTrue(all(isinstance(v, float)
                            for p in tx["az_pattern_data"] for v in p))
        self.assertEqual(tx["az_rotation"], 90.0)
        json.dumps(tx, allow_nan=False)

    def test_no_pattern_no_rotation(self):
        tx = self._tx(az_rotation=90.0)
        for key in ("az_pattern_data", "el_pattern_data", "az_rotation"):
            self.assertNotIn(key, tx)

    def test_empty_data_is_not_sent(self):
        tx = self._tx(az_pattern_data=[], el_pattern_data=[[0, 1]], az_rotation=0.0)
        self.assertNotIn("az_pattern_data", tx)
        self.assertEqual(tx["el_pattern_data"], [[0.0, 1.0]])
        self.assertEqual(tx["az_rotation"], 0.0)

    def test_geometric_models_send_no_pattern_and_no_rotation(self):
        for model in ("LOS", "MIN_ALT"):
            tx = self._tx(model=model, az_pattern_data=AZ, el_pattern_data=EL,
                          az_rotation=45.0)
            for key in ("az_pattern_data", "el_pattern_data", "az_rotation"):
                self.assertNotIn(key, tx, model)

    def test_rotation_whole_degrees(self):
        self.assertEqual(whole_degrees(89.6), 90.0)
        self.assertEqual(whole_degrees(359.7), 0.0)
        self.assertEqual(self._tx(az_pattern_data=AZ, az_rotation=12.4)["az_rotation"], 12.0)

    def test_peak_above_one_is_divided_by_its_peak(self):
        pts, peak = peak_normalized([[0.0, 2.0], [90.0, 1.0], [180.0, 0.5]])
        self.assertEqual(peak, 2.0)
        self.assertEqual(pts, [[0.0, 1.0], [90.0, 0.5], [180.0, 0.25]])
        tx = self._tx(az_pattern_data=[[0, 2.0], [90, 1.0]],
                      el_pattern_data=[[0, 0.5], [10, 0.8]])
        self.assertEqual(tx["az_pattern_data"], [[0.0, 1.0], [90.0, 0.5]])
        # Each axis on its own: a peak below 1 is left alone.
        self.assertEqual(tx["el_pattern_data"], [[0.0, 0.5], [10.0, 0.8]])

    def test_p2p_builder_carries_it_too(self):
        job = build_p2p_job(P2PParams(model="SIMPLE_LOSS", az_pattern_data=AZ,
                                      az_rotation=270.0), "/abt", "/out", "/b.csv")
        self.assertEqual(len(job["tx"]["az_pattern_data"]), len(AZ))
        self.assertEqual(job["tx"]["az_rotation"], 270.0)
        job = build_p2p_job(P2PParams(model="LOS", az_pattern_data=AZ,
                                      az_rotation=270.0), "/abt", "/out", "/b.csv")
        self.assertNotIn("az_pattern_data", job["tx"])
        self.assertNotIn("az_rotation", job["tx"])


class AssetPatterns(unittest.TestCase):

    def test_empty_is_none(self):
        self.assertEqual(asset_patterns(OMNI), (None, None))
        self.assertEqual(asset_patterns(None), (None, None))

    def test_directional_unchanged(self):
        az, el = asset_patterns(DIRECTIONAL)
        self.assertEqual(az, AZ)
        self.assertEqual(el, EL)

    def test_above_one_is_normalised_and_logged(self):
        asset = {"name": "Hot", "azimuth_pattern": {"data": [[0, 4.0], [180, 1.0]]},
                 "elevation_pattern": {"data": []}}
        with mock.patch("waveshed.core.job_builder._log_info") as log:
            az, el = asset_patterns(asset)
        self.assertEqual(az, [[0.0, 1.0], [180.0, 0.25]])
        self.assertIsNone(el)
        self.assertEqual(log.call_count, 1)
        self.assertIn('"Hot"', log.call_args[0][0])
        self.assertIn("+12.04 dB", log.call_args[0][0])

    def test_malformed_names_the_asset(self):
        asset = {"name": "Bad", "azimuth_pattern": {"data": [[0, "x"]]}}
        with self.assertRaises(ValueError) as ctx:
            asset_patterns(asset)
        self.assertIn('Asset "Bad": azimuth', str(ctx.exception))


class EngineGate(unittest.TestCase):

    def _check(self, version, pattern):
        with mock.patch.object(bm, "read_engine_version", return_value=version), \
                mock.patch.object(bm, "find_binary", return_value="/x/aether_core"):
            return bm.check_engine_for_job(30, pattern_data=pattern)

    def test_old_or_unknown_engine_refuses_a_pattern(self):
        for version in (None, "0.4.2"):
            with self.assertRaises(bm.EngineTooOldError) as ctx:
                self._check(version, True)
            msg = str(ctx.exception)
            self.assertIn("antenna pattern", msg)
            self.assertIn(f"{bm.PATTERN_DATA_MIN_ENGINE} or newer", msg)
            self.assertIn("Nothing was downloaded", msg)

    def test_new_engine_or_no_pattern_passes(self):
        self.assertEqual(self._check("0.4.3", True), "0.4.3")
        self.assertIn("unknown", self._check(None, False))

    def test_constant(self):
        self.assertEqual(bm.PATTERN_DATA_MIN_ENGINE, "0.4.3")


def _site_tab(mode="LOSS", res_mode=rp.MODE_FIXED, sites=None, assets=None):
    tab = SiteAnalysisTab.__new__(SiteAnalysisTab)
    tab._dialog = mock.Mock(get_mode=mock.Mock(return_value=mode),
                            get_loss_model=mock.Mock(return_value="ITM"))
    tab.combo_resolution = mock.Mock(currentText=mock.Mock(return_value="5"))
    tab.combo_output_res = mock.Mock(currentText=mock.Mock(return_value="5"),
                                     count=mock.Mock(return_value=6))
    tab.combo_backend = mock.Mock(currentText=mock.Mock(return_value="AUTO"))
    tab.combo_climate = mock.Mock(currentIndex=mock.Mock(return_value=4))
    tab.combo_pol = mock.Mock(currentIndex=mock.Mock(return_value=0))
    tab.combo_earth_radius = mock.Mock(currentText=mock.Mock(return_value="FOUR_THIRDS"))
    for name, value in (("spin_eps", 15.0), ("spin_sgm", 0.005), ("spin_ens", 301.0),
                        ("spin_conf", 0.5), ("spin_rel", 0.5)):
        setattr(tab, name, mock.Mock(value=mock.Mock(return_value=value)))
    for name, on in (("radio_res_fixed", res_mode == rp.MODE_FIXED),
                     ("radio_res_adaptive", res_mode == rp.MODE_ADAPTIVE),
                     ("radio_res_custom", res_mode == rp.MODE_CUSTOM)):
        setattr(tab, name, mock.Mock(isChecked=mock.Mock(return_value=on)))
    tab._custom_bands = None
    tab._custom_buildings = {}
    tab.chk_buildings = mock.Mock(isChecked=mock.Mock(return_value=False))
    tab._asset_cache = dict(assets or {"Dir": DIRECTIONAL, "Omni": OMNI})
    tab._read_sites = lambda: list(sites or [
        {"asset_name": "Dir", "coords": (46.8, 7.3), "range": 50, "height": 30.0,
         "az_rotation": 90.0},
    ])
    tab._read_altitudes = lambda: [(2.0, "AGL"), (10.0, "AGL")]
    return tab


class SiteJobs(unittest.TestCase):

    def _txs(self, tab):
        return [build_coverage_job(p, "/abt", "/out")["tx"] for p, _ in tab._build_jobs()]

    def test_every_fixed_job_carries_the_pattern_and_rotation(self):
        txs = self._txs(_site_tab())
        self.assertEqual(len(txs), 2)
        for tx in txs:
            self.assertEqual(tx["az_pattern_data"], AZ)
            self.assertEqual(tx["el_pattern_data"], EL)
            self.assertEqual(tx["az_rotation"], 90.0)

    def test_adaptive_job_carries_it(self):
        jobs = _site_tab(res_mode=rp.MODE_ADAPTIVE)._build_jobs()
        for params, _ in jobs:
            self.assertTrue(params.range_profile)
            self.assertTrue(has_pattern_data(params))

    def test_multi_site_each_row_its_own(self):
        sites = [
            {"asset_name": "Dir", "coords": (46.8, 7.3), "range": 20, "az_rotation": 90.0},
            {"asset_name": "Omni", "coords": (46.9, 7.4), "range": 20, "az_rotation": 45.0},
            {"asset_name": "", "coords": (47.0, 7.5), "range": 20, "az_rotation": 10.0},
            {"asset_name": "Dir", "coords": (47.1, 7.6), "range": 20, "az_rotation": 180.4},
        ]
        txs = self._txs(_site_tab(sites=sites))
        self.assertEqual(len(txs), 8)
        dir1, omni, blank, dir2 = txs[0], txs[2], txs[4], txs[6]
        self.assertEqual(dir1["az_rotation"], 90.0)
        for tx in (omni, blank):
            for key in ("az_pattern_data", "el_pattern_data", "az_rotation"):
                self.assertNotIn(key, tx)
        self.assertEqual(dir2["az_rotation"], 180.0)
        self.assertEqual(dir2["az_pattern_data"], AZ)

    def test_los_and_los_floor_send_nothing(self):
        for mode in ("LOS", "MIN_ALT"):
            for tx in self._txs(_site_tab(mode=mode)):
                for key in ("az_pattern_data", "el_pattern_data", "az_rotation"):
                    self.assertNotIn(key, tx, mode)

    def test_worker_gate_asks_for_patterns(self):
        params = [p for p, _ in _site_tab()._build_jobs()]
        self.assertTrue(all(has_pattern_data(p) for p in params))
        params = [p for p, _ in _site_tab(mode="LOS")._build_jobs()]
        self.assertFalse(any(has_pattern_data(p) for p in params))


class _Combo:
    def __init__(self, text):
        self._text = text

    def currentText(self):
        return self._text


class _Spin:
    def __init__(self, value):
        self._v = value

    def value(self):
        return self._v


def _p2p_tab(mode="LOSS", asset="Dir", backend="AUTO", rotation=90.0):
    tab = P2PTab.__new__(P2PTab)
    tab._parent_dialog = mock.Mock(get_mode=mock.Mock(return_value=mode),
                                   get_loss_model=mock.Mock(return_value="ITM"))
    tab.combo_asset = _Combo(asset)
    tab.combo_backend = _Combo(backend)
    tab.combo_resolution = _Combo("30")
    tab.combo_earth_radius = _Combo("FOUR_THIRDS")
    tab.combo_a_mode = tab.combo_b_mode = _Combo("AGL")
    tab.spin_az_rotation = _Spin(rotation)
    tab.spin_freq = _Spin(450.0)
    tab.spin_erp = _Spin(100.0)
    for name, v in (("spin_a_lat", 46.8), ("spin_a_lon", 7.3), ("spin_a_height", 30.0),
                    ("spin_b_lat", 46.85), ("spin_b_lon", 7.35), ("spin_b_height", 2.0)):
        setattr(tab, name, _Spin(v))
    return tab


class P2PParamsFromTheTab(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch.object(p2p_mod, "list_assets",
                                    return_value=[dict(DIRECTIONAL, _path="/s/d.json"),
                                                  dict(OMNI, _path="/s/o.json")])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_selected_asset_pattern_and_rotation(self):
        params = _p2p_tab()._collect_params()
        tx = build_p2p_job(params, "/abt", "/out", "/b.csv")["tx"]
        self.assertEqual(tx["az_pattern_data"], AZ)
        self.assertEqual(tx["el_pattern_data"], EL)
        self.assertEqual(tx["az_rotation"], 90.0)

    def test_blank_or_omni_asset_no_pattern_no_rotation(self):
        for name in ("", "Omni"):
            tx = build_p2p_job(_p2p_tab(asset=name)._collect_params(),
                               "/abt", "/out", "/b.csv")["tx"]
            for key in ("az_pattern_data", "el_pattern_data", "az_rotation"):
                self.assertNotIn(key, tx, name)

    def test_los_mode_no_pattern(self):
        params = _p2p_tab(mode="LOS")._collect_params()
        self.assertIsNone(params.az_pattern_data)
        self.assertIsNone(params.az_rotation)

    def test_unresolved_asset_is_refused(self):
        with self.assertRaises(P2PAssetError) as ctx:
            _p2p_tab(asset="Gone")._collect_params()
        self.assertEqual(ctx.exception.title, "Asset not found")
        self.assertIn('"Gone"', str(ctx.exception))

    def _run(self, backend, answer):
        tab = _p2p_tab(backend=backend)
        tab._worker = None
        tab._validate_inputs = lambda: None
        tab._selected_dem_layer = lambda: None
        tab._selected_terrain_dir = lambda: ""
        tab.line_output_dir = _Combo("/out")
        tab.line_output_dir.text = lambda: "/out"
        tab._launch_worker = mock.Mock()
        with mock.patch.object(p2p_mod, "save_output_dir"), \
                mock.patch.object(p2p_mod, "setup_warning", return_value=""), \
                mock.patch.object(p2p_mod, "_write_temp_batch_csv", return_value="/b.csv"), \
                mock.patch.object(p2p_mod, "terrain_plan", side_effect=RuntimeError), \
                mock.patch.object(p2p_mod, "terrain_size_warning", return_value=None), \
                mock.patch.object(p2p_mod.QMessageBox, "question",
                                  return_value=answer) as ask:
            tab._on_run()
        return ask, tab._launch_worker

    def test_cpu_backend_asks_and_no_stops(self):
        No = p2p_mod.QMessageBox.StandardButton.No
        ask, launch = self._run("CPU", No)
        self.assertEqual(ask.call_count, 1)
        self.assertIn("CPU backend ignores antenna patterns in P2P", ask.call_args[0][2])
        self.assertIn("Run without the pattern?", ask.call_args[0][2])
        self.assertEqual(ask.call_args[0][4], No)  # default No
        launch.assert_not_called()

    def test_cpu_backend_yes_runs_without_the_pattern(self):
        ask, launch = self._run("CPU", p2p_mod.QMessageBox.StandardButton.Yes)
        params = launch.call_args[0][0]
        self.assertIsNone(params.az_pattern_data)
        self.assertIsNone(params.el_pattern_data)
        self.assertIsNone(params.az_rotation)

    def test_auto_and_gpu_do_not_ask(self):
        for backend in ("AUTO", "GPU"):
            ask, launch = self._run(backend, p2p_mod.QMessageBox.StandardButton.No)
            ask.assert_not_called()
            self.assertTrue(has_pattern_data(launch.call_args[0][0]))


class PatternFiles(unittest.TestCase):

    def test_bare_header_is_never_a_data_point(self):
        parsed = amt.parse_pattern_file("0.0 0.0\n-10 0.5\n0 1.0\n10 0.5\n", "el")
        self.assertEqual(parsed["data"], [[-10.0, 0.5], [0.0, 1.0], [10.0, 0.5]])
        self.assertEqual(amt.pattern_header_note(parsed, "el"), "")

    def test_comments_are_stripped_not_whole_lines(self):
        text = "45\t; Antenna Rotation\n0 1.0 ; boresight\n# note\n180 0.1\n"
        parsed = amt.parse_pattern_file(text, "az")
        self.assertEqual(parsed["data"], [[0.0, 1.0], [180.0, 0.1]])
        self.assertEqual(parsed["rotation"], 45.0)
        self.assertEqual(parsed["skipped"], 1)
        note = amt.pattern_header_note(parsed, "az")
        self.assertIn("45°", note)
        self.assertIn("AZ Rotation", note)

    def test_el_header_tilt_is_reported(self):
        parsed = amt.parse_pattern_file("5.0 90.0 ; tilt\n0 1\n", "el")
        self.assertEqual((parsed["tilt"], parsed["tilt_azimuth"]), (5.0, 90.0))
        self.assertIn("NOT applied", amt.pattern_header_note(parsed, "el"))

    def test_export_keeps_fractional_angles(self):
        tab = amt.AssetManagerTab.__new__(amt.AssetManagerTab)
        tab._get_pattern_data = lambda table: [[0.5, 1.0], [359.5, 0.25], [-90.0, 0.1]]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.el")
            with mock.patch.object(amt.QFileDialog, "getSaveFileName", create=True,
                                   return_value=(path, "")):
                tab._on_save_pattern("el", None)
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        self.assertEqual(lines[0].split()[:2], ["0.0", "0.0"])
        self.assertEqual(lines[1:], ["0.5\t1.000000", "359.5\t0.250000", "-90\t0.100000"])
        # Round trip through the importer.
        parsed = amt.parse_pattern_file("\n".join(lines), "el")
        self.assertEqual(parsed["data"], [[0.5, 1.0], [359.5, 0.25], [-90.0, 0.1]])


class _Item:
    def __init__(self, text):
        self._t = text

    def text(self):
        return self._t


class _BeamTable:
    def __init__(self, rows):
        self._rows = rows

    def rowCount(self):
        return len(self._rows)

    def item(self, row, col):
        return _Item(str(self._rows[row][col]))


class GenericBeams(unittest.TestCase):

    def _generate(self, beams, floor_db=-30.0):
        dlg = amt._PatternGeneratorDialog.__new__(amt._PatternGeneratorDialog)
        dlg._pattern_type = "azimuth"
        dlg.spin_az_generic_tilt = _Spin(0.0)
        dlg.spin_az_generic_sl = _Spin(floor_db)
        dlg.az_generic_table = _BeamTable(beams)
        return dict((a, g) for a, g in dlg._generate_generic_multibeam())

    def test_beams_above_0_db_keep_their_relative_levels(self):
        data = self._generate([(0.0, 30.0, 6.0), (180.0, 30.0, 0.0)])
        self.assertEqual(max(data.values()), 1.0)
        self.assertAlmostEqual(data[0.0], 1.0)
        # 0 dB beam is 6 dB below the 6 dB beam: 10**(-6/20) = 0.5012.
        self.assertAlmostEqual(data[180.0], round(10 ** (-6 / 20), 4), places=4)

    def test_beams_at_or_below_0_db_unchanged(self):
        data = self._generate([(90.0, 30.0, -6.0)])
        self.assertAlmostEqual(data[90.0], round(10 ** (-6 / 20), 4), places=4)


if __name__ == "__main__":
    unittest.main()
