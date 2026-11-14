"""P2P tab (2026-11-14): batch CSV input, its own ITM group, AZ Rotation
enabled by the asset's pattern, asset → Site A height.

Logic on the conftest stubs: the tab is a bare ``P2PTab.__new__`` with fake
widgets; the job is built by the real ``build_p2p_job`` / worker.
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401

import json
import os
import tempfile
import unittest
from unittest import mock

import waveshed.gui.p2p_tab as p2p_mod
from waveshed.core.job_builder import P2PParams, build_p2p_job
from waveshed.gui.p2p_tab import P2PTab


# 2 sources × 3 receivers around Bern.
BATCH_2x3 = (
    "# sites\n"
    "S,BERN,46.948000,7.447400,30.0,AGL\n"
    "R,THUN,46.758000,7.628000,2.0,AGL\n"
    "S,GURTEN,46.919000,7.440000,900.0,AMSL\n"
    "R,BELP,46.891000,7.498000,10.0,AGL\n"
    "R,MUENSINGEN,46.873000,7.561000,1.5,AGL\n"
)


def _write(d, text, name="batch.csv", raw=None):
    path = os.path.join(d, name)
    with open(path, "wb") as fh:
        fh.write(raw if raw is not None else text.encode("utf-8"))
    return path


class _Combo:
    def __init__(self, current="", items=None):
        self.items = list(items) if items is not None else [current]
        self.current = current
        self.set_calls = []

    def currentText(self):
        return self.current

    def findText(self, text):
        return self.items.index(text) if text in self.items else -1

    def setCurrentIndex(self, idx):
        self.current = self.items[idx]
        self.set_calls.append(("mode", self.current))

    def text(self):
        return self.current


class _Spin:
    def __init__(self, value=0.0, log=None):
        self._v = value
        self.enabled = True
        self.tip = ""
        self.log = log

    def value(self):
        return self._v

    def setValue(self, v):
        self._v = v
        if self.log is not None:
            self.log.append(("height", v))

    def setEnabled(self, on):
        self.enabled = on

    def setToolTip(self, tip):
        self.tip = tip


class _Group:
    def __init__(self, values):
        self._values = values

    def values(self):
        return dict(self._values)


ITM_VALUES = {"eps": 25.0, "sgm": 0.02, "ens": 320.0, "climate": 6, "pol": 1,
              "conf": 0.9, "rel": 0.7, "earth_radius": "ADVANCED"}

PAT = {"name": "Pat", "frequency_mhz": 868.0, "erp_watts": 5.0,
       "default_height_m": 77.0, "default_height_mode": "AGL",
       "azimuth_pattern": {"data": [[0, 1.0], [180, 0.1]]}}
AMSL = {"name": "Hill", "frequency_mhz": 433.0, "erp_watts": 1.0,
        "default_height_m": -12.0, "default_height_mode": "AMSL"}
PLAIN = {"name": "Plain", "frequency_mhz": 433.0, "erp_watts": 10.0}
ASSETS = [dict(PAT, _path="/s/p.json"), dict(AMSL, _path="/s/h.json"),
          dict(PLAIN, _path="/s/o.json")]


def _tab(mode="LOSS", batch=False, asset="", itm=True):
    tab = P2PTab.__new__(P2PTab)
    tab._parent_dialog = mock.Mock(get_mode=mock.Mock(return_value=mode),
                                   get_loss_model=mock.Mock(return_value="ITM"))
    tab.combo_input = _Combo(p2p_mod.INPUT_BATCH if batch else p2p_mod.INPUT_SINGLE)
    tab.combo_asset = _Combo(asset)
    tab.combo_backend = _Combo("AUTO")
    tab.combo_resolution = _Combo("30")
    tab.combo_a_mode = _Combo("AGL", items=["AGL", "AMSL"])
    tab.combo_b_mode = _Combo("AGL", items=["AGL", "AMSL"])
    tab.spin_az_rotation = _Spin(90.0)
    tab.spin_freq = _Spin(450.0)
    tab.spin_erp = _Spin(100.0)
    for name, v in (("spin_a_lat", 46.8), ("spin_a_lon", 7.3), ("spin_a_height", 30.0),
                    ("spin_b_lat", 46.85), ("spin_b_lon", 7.35), ("spin_b_height", 2.0)):
        setattr(tab, name, _Spin(v))
    if itm:
        tab.itm_group = _Group(ITM_VALUES)
    return tab


class _AssetStore(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(p2p_mod, "list_assets", return_value=ASSETS)
        patcher.start()
        self.addCleanup(patcher.stop)


# ---------------------------------------------------------------------------
# 1. Batch CSV
# ---------------------------------------------------------------------------

class ReadBatchLinks(unittest.TestCase):

    def test_two_sources_three_receivers_are_six_links(self):
        with tempfile.TemporaryDirectory() as d:
            entries = p2p_mod.read_batch_links(_write(d, BATCH_2x3))
        self.assertEqual(len(entries), 5)
        self.assertEqual(p2p_mod.batch_link_count(entries), 6)

    def _refused(self, text, raw=None):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError) as ctx:
                p2p_mod.read_batch_links(_write(d, text, raw=raw))
        return str(ctx.exception)

    def test_a_malformed_line_is_refused_with_its_number(self):
        msg = self._refused("S,A,46.9,7.4,30,AGL\nR,B,46.8,7.5,2\n")
        self.assertIn("Line 2", msg)
        self.assertIn("expected 6 fields", msg)

    def test_a_non_numeric_coordinate_names_the_line(self):
        msg = self._refused("S,A,46.9,7.4,30,AGL\nR,B,north,7.5,2,AGL\n")
        self.assertIn("Line 2: lat is not a number", msg)

    def test_bad_type_mode_and_floor(self):
        self.assertIn("Line 1: type", self._refused("X,A,46.9,7.4,30,AGL\n"))
        self.assertIn("Line 1: mode", self._refused("S,A,46.9,7.4,30,MSL\n"))
        self.assertIn("Line 2: altitude",
                      self._refused("S,A,46.9,7.4,30,AGL\nR,B,46.8,7.5,0.2,AGL\n"))

    def test_no_receiver_or_no_source_is_no_link(self):
        self.assertIn("0 receiver (R)", self._refused("S,A,46.9,7.4,30,AGL\n"))
        self.assertIn("0 source (S)", self._refused("R,A,46.9,7.4,30,AGL\n"))

    def test_bom_and_lower_case_s_are_read_as_written(self):
        # Excel's "CSV UTF-8" byte-order mark and a lower-case 's' are
        # accepted: the engine reads a clean copy (write_engine_batch_csv).
        bom = b"\xef\xbb\xbf" + BATCH_2x3.encode("utf-8")
        with tempfile.TemporaryDirectory() as d:
            entries = p2p_mod.read_batch_links(_write(d, "", raw=bom))
            self.assertEqual(p2p_mod.batch_link_count(entries), 6)
            lower = p2p_mod.read_batch_links(_write(
                d, "R,B,46.8,7.5,2,AGL\ns,A,46.9,7.4,30,AGL\n"))
        self.assertEqual([e[0] for e in lower], ["R", "S"])

    def test_the_engine_copy_holds_exactly_the_checked_rows(self):
        from waveshed.core.job_builder import write_engine_batch_csv
        bom = (b"\xef\xbb\xbf# exported from Excel\n"
               b"s,TX A,46.948,7.4474,30,agl\n\nr,RX 1,46.8,7.5,2.5,AMSL\n")
        with tempfile.TemporaryDirectory() as d:
            entries = p2p_mod.read_batch_links(_write(d, "", raw=bom))
            out = write_engine_batch_csv(entries, path=f"{d}/engine.csv")
            with open(out, "rb") as fh:
                raw = fh.read()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(raw.decode("utf-8").splitlines(), [
            "S,TX A,46.948,7.4474,30.0,AGL",
            "R,RX 1,46.8,7.5,2.5,AMSL",
        ])

    def test_an_id_the_engine_cannot_read_back_is_refused(self):
        from waveshed.core.job_builder import write_engine_batch_csv
        with self.assertRaises(ValueError):
            write_engine_batch_csv([("S", "a,b", 46.9, 7.4, 30.0, "AGL")],
                                   path=os.devnull)

    def test_no_path_or_no_file(self):
        with self.assertRaises(ValueError):
            p2p_mod.read_batch_links("")
        with self.assertRaises(ValueError) as ctx:
            p2p_mod.read_batch_links("/nowhere/batch.csv")
        self.assertIn("does not exist", str(ctx.exception))


class BatchParamsAndJob(_AssetStore):

    def test_job_tx_is_the_first_source_and_rx_the_first_receiver(self):
        with tempfile.TemporaryDirectory() as d:
            entries = p2p_mod.read_batch_links(_write(d, BATCH_2x3))
        params = _tab(batch=True)._collect_params(entries)
        job = build_p2p_job(params, "/abt", "/out", batch_file="/u/batch.csv")
        self.assertEqual(job["analysis"]["task_type"], "BATCH_P2P")
        self.assertEqual(job["analysis"]["batch_file"], "/u/batch.csv")
        self.assertEqual((job["tx"]["lat"], job["tx"]["lon"],
                          job["tx"]["height_m"], job["tx"]["mode"]),
                         (46.948, 7.4474, 30.0, "AGL"))
        self.assertEqual((job["rx"]["height_m"], job["rx"]["mode"]), (2.0, "AGL"))

    def test_single_link_still_takes_site_a(self):
        params = _tab()._collect_params()
        self.assertEqual((params.tx_lat, params.tx_lon, params.tx_height),
                         (46.8, 7.3, 30.0))

    def _run(self, tab, csv_path):
        tab._worker = None
        tab._validate_inputs = lambda: None
        tab._selected_dem_layer = lambda: None
        tab._selected_terrain_dir = lambda: ""
        tab.line_output_dir = _Combo("/out")
        tab.line_batch_csv = _Combo(csv_path)
        tab._launch_worker = mock.Mock()
        with mock.patch.object(p2p_mod, "save_output_dir"), \
                mock.patch.object(p2p_mod, "QgsSettings"), \
                mock.patch.object(p2p_mod, "setup_warning", return_value=""), \
                mock.patch.object(p2p_mod, "_write_temp_batch_csv") as temp, \
                mock.patch.object(p2p_mod, "terrain_plan", side_effect=RuntimeError), \
                mock.patch.object(p2p_mod, "terrain_size_warning", return_value=None), \
                mock.patch.object(p2p_mod.QMessageBox, "warning",
                                  return_value=p2p_mod.QMessageBox.StandardButton.No) as warn:
            tab._on_run()
        return tab._launch_worker, temp, warn

    def test_run_hands_the_engine_a_clean_copy_of_the_users_rows(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write(d, BATCH_2x3)
            with open(path, "rb") as fh:
                before = fh.read()
            launch, temp, warn = self._run(_tab(batch=True), path)
            with open(path, "rb") as fh:
                self.assertEqual(fh.read(), before)  # the user's file untouched
            args, kwargs = launch.call_args
            engine_csv = args[3]
            try:
                self.assertNotEqual(engine_csv, path)
                self.assertEqual(p2p_mod.read_batch_links(engine_csv),
                                 p2p_mod.read_batch_links(path))
            finally:
                os.remove(engine_csv)
        temp.assert_not_called()
        warn.assert_not_called()
        self.assertTrue(kwargs["batch"])
        self.assertEqual(args[0].tx_lat, 46.948)

    def test_a_malformed_csv_runs_nothing_and_names_the_line(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write(d, "S,A,46.9,7.4,30,AGL\nR,B,46.8\n")
            launch, _temp, warn = self._run(_tab(batch=True), path)
        launch.assert_not_called()
        self.assertEqual(warn.call_args[0][1], "Batch CSV refused")
        self.assertIn("Line 2", warn.call_args[0][2])

    def test_single_link_writes_the_temp_csv(self):
        launch, temp, _warn = self._run(_tab(), "/ignored.csv")
        temp.assert_called_once()
        self.assertFalse(launch.call_args[1]["batch"])

    def test_worker_job_from_the_users_batch(self):
        """The worker writes the job: batch file = the user CSV, max_range
        from the batch's longest link, job.tx = the first S."""
        with tempfile.TemporaryDirectory() as d:
            path = _write(d, BATCH_2x3)
            entries = p2p_mod.read_batch_links(path)
            params = dataclass_copy(_tab(batch=True)._collect_params(entries))
            params.output_name = "p2p_test"
            out_dir = os.path.join(d, "out")
            worker = p2p_mod._P2PWorker(params, mock.Mock(), out_dir, path)
            with mock.patch("waveshed.core.terrain_adapter.prepare_terrain",
                            return_value=os.path.join(d, "abt")), \
                    mock.patch("waveshed.core.binary_manager.check_engine_for_job"), \
                    mock.patch.object(p2p_mod, "find_binary", return_value="aether_core"), \
                    mock.patch.object(p2p_mod.api_key, "apply_license_env"), \
                    mock.patch.object(p2p_mod.terrain_adapter, "run_converter_streaming",
                                      return_value=0):
                worker.run()
            with open(os.path.join(out_dir, "p2p_test_job.json"), encoding="utf-8") as fh:
                job = json.load(fh)
        self.assertEqual(job["analysis"]["batch_file"], path)
        self.assertEqual(job["tx"]["lat"], 46.948)
        # Bern–Thun 25.2 km is the longest of the six links.
        self.assertGreaterEqual(job["analysis"]["max_range_km"], 26)
        self.assertEqual(job["propagation"]["radio_climate"], 6)


def dataclass_copy(params: P2PParams) -> P2PParams:
    import dataclasses
    return dataclasses.replace(params)


class BatchResults(unittest.TestCase):

    def test_every_link_is_listed(self):
        rows = [{"Source_ID": s, "Target_ID": t, "Signal_dBm": "-80.1",
                 "Path_Loss_dB": "120.5"} for s in ("A", "B") for t in ("X", "Y", "Z")]
        text = p2p_mod.format_batch_results(rows)
        self.assertIn("6 link(s)", text)
        for s in ("A", "B"):
            for t in ("X", "Y", "Z"):
                self.assertIn(f"{s}, {t}, -80.1, 120.5", text)

    def test_display_lists_the_batch_and_skips_the_one_link_report(self):
        tab = P2PTab.__new__(P2PTab)
        tab._last_run_batch = True
        tab._last_output_name = "p2p_x"
        tab.result_area = mock.Mock()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "p2p_x.csv"), "w", encoding="utf-8") as fh:
                fh.write("Source_ID,Target_ID,Signal_dBm,Path_Loss_dB\n"
                         "A,X,-1,2\nA,Y,-3,4\n")
            with open(os.path.join(d, "p2p_report.txt"), "w", encoding="utf-8") as fh:
                fh.write("one link\n")
            tab._display_results(d)
        shown = "\n".join(c[0][0] for c in tab.result_area.append.call_args_list)
        self.assertIn("A, X, -1, 2", shown)
        self.assertIn("A, Y, -3, 4", shown)
        self.assertNotIn("one link", shown)

    def test_view_plots_disabled_with_a_reason_for_a_batch(self):
        tab = _tab(batch=True)
        tab.btn_view_plots = _Spin()
        tab._last_run_batch = False
        tab._update_view_plots()
        self.assertFalse(tab.btn_view_plots.enabled)
        self.assertEqual(tab.btn_view_plots.tip, p2p_mod.VIEW_PLOTS_BATCH_TIP)
        tab.combo_input.current = p2p_mod.INPUT_SINGLE
        tab._update_view_plots()
        self.assertTrue(tab.btn_view_plots.enabled)
        tab._last_run_batch = True       # the plots of a batch run: one link of many
        tab._update_view_plots()
        self.assertFalse(tab.btn_view_plots.enabled)


# ---------------------------------------------------------------------------
# 2. Own ITM group
# ---------------------------------------------------------------------------

class ItmGroup(unittest.TestCase):

    def test_visible_in_propagation_with_itm_only(self):
        tab = _tab()
        self.assertFalse(tab.itm_visible_for(False))
        self.assertTrue(tab.itm_visible_for(True))
        tab._parent_dialog.get_loss_model.return_value = "SIMPLE_LOSS"
        self.assertFalse(tab.itm_visible_for(True))

    def test_set_mode_drives_the_group(self):
        tab = _tab()
        tab._lbl_site_a = mock.Mock()
        tab._lbl_site_b = mock.Mock()
        tab._params_group = mock.Mock()
        tab.itm_group = mock.Mock()
        tab.set_mode("LOSS")
        tab.itm_group.setVisible.assert_called_with(True)
        tab.set_mode("LOS")
        tab.itm_group.setVisible.assert_called_with(False)

    def test_group_values_reach_the_job(self):
        with mock.patch.object(p2p_mod, "list_assets", return_value=[]):
            params = _tab()._collect_params()
        prop = build_p2p_job(params, "/abt", "/out", "/b.csv")["propagation"]
        self.assertEqual(prop, {
            "eps_dielect": 25.0, "sgm_conductivity": 0.02, "eno_ns_surfref": 320.0,
            "radio_climate": 6, "pol": 1, "conf": 0.9, "rel": 0.7,
            "earth_radius_mode": "ADVANCED"})

    def test_values_go_in_los_mode_too_as_the_360_tab_does(self):
        params = _tab(mode="LOS")._collect_params()
        self.assertEqual(params.climate, 6)
        self.assertEqual(params.earth_radius, "ADVANCED")


# ---------------------------------------------------------------------------
# 3. AZ Rotation follows the asset's pattern
# ---------------------------------------------------------------------------

class AzRotation(_AssetStore):

    def test_state_per_asset(self):
        self.assertEqual(p2p_mod.az_rotation_state("Pat", PAT), (True, p2p_mod.AZ_ROTATION_TIP))
        on, tip = p2p_mod.az_rotation_state("", None)
        self.assertFalse(on)
        self.assertIn("No asset selected", tip)
        on, tip = p2p_mod.az_rotation_state("Plain", PLAIN)
        self.assertFalse(on)
        self.assertIn('"Plain" has no antenna pattern', tip)
        on, tip = p2p_mod.az_rotation_state("Gone", None)
        self.assertFalse(on)
        self.assertIn("not in the assets folder", tip)

    def test_elevation_only_pattern_counts(self):
        self.assertTrue(p2p_mod.asset_has_pattern(
            {"elevation_pattern": {"data": [[-90, 0.1], [0, 1.0]]}}))
        self.assertFalse(p2p_mod.asset_has_pattern(
            {"azimuth_pattern": {"data": []}, "elevation_pattern": {}}))

    def test_follows_selection_refresh_and_rename(self):
        tab = _tab(asset="Pat")
        tab._on_asset_changed("Pat")
        self.assertTrue(tab.spin_az_rotation.enabled)
        tab.combo_asset.current = "Plain"
        tab._on_asset_changed("Plain")
        self.assertFalse(tab.spin_az_rotation.enabled)
        tab.combo_asset.current = ""
        tab._on_asset_changed("")
        self.assertFalse(tab.spin_az_rotation.enabled)

        # Refresh: the asset lost its pattern on disk.
        tab = _tab(asset="Pat")
        tab.combo_asset = mock.Mock()
        tab.combo_asset.currentText.return_value = "Pat"
        tab.combo_asset.findText.return_value = 1
        tab._update_az_rotation()
        self.assertTrue(tab.spin_az_rotation.enabled)
        with mock.patch.object(p2p_mod, "list_assets",
                               return_value=[dict(PLAIN, name="Pat", _path="/s/p.json")]):
            tab.refresh_assets()
        self.assertFalse(tab.spin_az_rotation.enabled)

        # Rename: the relabelled entry is looked up under its new name.
        tab = _tab(asset="Plain")
        tab.combo_asset = mock.Mock()
        tab.combo_asset.findText.return_value = 1
        tab.combo_asset.currentText.return_value = "Pat"
        tab.rename_asset("Plain", "Pat")
        tab.combo_asset.setItemText.assert_called_once_with(1, "Pat")
        self.assertTrue(tab.spin_az_rotation.enabled)


# ---------------------------------------------------------------------------
# 4. Asset → Site A height (single link only)
# ---------------------------------------------------------------------------

class AssetFillsSiteA(_AssetStore):

    def test_height_and_mode_mode_first(self):
        tab = _tab()
        log = []
        tab.spin_a_height = _Spin(30.0, log=log)
        tab.combo_a_mode.set_calls = log
        tab._on_asset_changed("Hill")
        self.assertEqual(log, [("mode", "AMSL"), ("height", -12.0)])
        self.assertEqual(tab.spin_freq.value(), 433.0)

    def test_pattern_asset_fills_77_m(self):
        tab = _tab()
        tab._on_asset_changed("Pat")
        self.assertEqual(tab.spin_a_height.value(), 77.0)
        self.assertEqual(tab.combo_a_mode.currentText(), "AGL")
        self.assertEqual(tab.spin_freq.value(), 868.0)

    def test_missing_default_takes_30_m_agl(self):
        tab = _tab()
        tab.spin_a_height.setValue(12.0)
        tab._on_asset_changed("Plain")
        self.assertEqual(tab.spin_a_height.value(), 30.0)

    def test_batch_leaves_site_a_alone(self):
        tab = _tab(batch=True)
        tab._on_asset_changed("Pat")
        self.assertEqual(tab.spin_a_height.value(), 30.0)
        self.assertEqual(tab.spin_freq.value(), 868.0)   # frequency as today


if __name__ == "__main__":
    unittest.main()
