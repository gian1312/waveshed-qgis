"""The asset store and how its changes reach the run tabs.

Covers (brief A2): file names never overwrite another asset, the file
follows a renamed asset (write first, delete second), ONE name → asset rule
for the 360° and P2P tabs, the store signature, the Assets tab's
``assets_changed`` / ``asset_renamed`` signals and their wiring in the main
dialog, and the 360° run's refusal of a row whose asset no longer resolves.

Logic tests on the conftest stubs (dict-backed QgsSettings, working
pyqtSignal); the real widgets were driven separately with PyQGIS.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import json
import os
import tempfile
import unittest
from unittest import mock

from qgis.core import QgsSettings

import waveshed.core.asset_manager as am
import waveshed.gui.asset_manager_tab as amt
import waveshed.gui.main_dialog as md
import waveshed.gui.p2p_tab as p2p_mod
from waveshed.gui.asset_manager_tab import AssetManagerTab
from waveshed.gui.main_dialog import AetherMainDialog
from waveshed.gui.p2p_tab import P2PTab
import waveshed.gui.site_analysis_tab as sat
from waveshed.gui.site_analysis_tab import (
    SiteAnalysisTab, UnresolvedAssetError, _COL_ASSET,
)

from test_settings_refresh import _FakeCombo


class _StoreCase(unittest.TestCase):
    """A temp assets folder set as ``waveshed/assets_dir``."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = os.path.join(self._tmp.name, "assets")
        os.makedirs(self.store)
        self._settings = mock.patch.object(
            QgsSettings, "_store", {"waveshed/assets_dir": self.store})
        self._settings.start()
        am._DUPLICATES_LOGGED.clear()

    def tearDown(self):
        self._settings.stop()
        self._tmp.cleanup()

    def write(self, filename, name, **fields):
        path = os.path.join(self.store, filename)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(dict(am.create_default_asset(name), **fields), fh)
        return path

    def name_in(self, filename):
        with open(os.path.join(self.store, filename), encoding="utf-8") as fh:
            return json.load(fh)["name"]

    def files(self):
        return sorted(os.listdir(self.store))


# ---------------------------------------------------------------------------
# core/asset_manager
# ---------------------------------------------------------------------------


class TestNoOverwrite(_StoreCase):

    def test_derived_path_skips_existing_files(self):
        self.write("new_asset.json", "Yagi 14 el")
        self.write("new_asset_2.json", "Other")
        path = am.save_asset(am.create_default_asset("New Asset"))
        self.assertEqual(os.path.basename(path), "new_asset_3.json")
        self.assertEqual(self.name_in("new_asset.json"), "Yagi 14 el")
        self.assertEqual(self.name_in("new_asset_2.json"), "Other")

    def test_explicit_path_is_written_as_given(self):
        path = self.write("x.json", "Old")
        am.save_asset(am.create_default_asset("New"), path=path)
        self.assertEqual(self.name_in("x.json"), "New")
        self.assertEqual(self.files(), ["x.json"])

    def test_failed_write_keeps_the_old_file_and_leaves_no_temp(self):
        path = self.write("keep.json", "Keep")
        with mock.patch.object(am.json, "dump", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                am.save_asset(am.create_default_asset("Lost"), path=path)
        self.assertEqual(self.name_in("keep.json"), "Keep")
        self.assertEqual(self.files(), ["keep.json"])


class TestFileFollowsTheName(_StoreCase):

    def test_rename_moves_the_file_and_removes_the_old_one(self):
        old = self.write("new_asset.json", "New Asset")
        new = am.save_renamed_asset(am.create_default_asset("Yagi 14 el"), old)
        self.assertEqual(os.path.basename(new), "yagi_14_el.json")
        self.assertEqual(self.files(), ["yagi_14_el.json"])

    def test_rename_never_overwrites_another_file(self):
        old = self.write("a.json", "A")
        self.write("yagi.json", "Someone else")
        new = am.save_renamed_asset(am.create_default_asset("Yagi"), old)
        self.assertEqual(os.path.basename(new), "yagi_2.json")
        self.assertEqual(self.name_in("yagi.json"), "Someone else")
        self.assertEqual(self.files(), ["yagi.json", "yagi_2.json"])

    def test_name_mapping_to_its_own_file_saves_in_place(self):
        old = self.write("yagi.json", "yagi")
        new = am.save_renamed_asset(am.create_default_asset("Yagi"), old)
        self.assertEqual(new, os.path.abspath(old))
        self.assertEqual(self.files(), ["yagi.json"])
        self.assertEqual(self.name_in("yagi.json"), "Yagi")

    def test_file_outside_the_store_is_saved_in_place(self):
        outside = os.path.join(self._tmp.name, "elsewhere.json")
        with open(outside, "w", encoding="utf-8") as fh:
            json.dump(am.create_default_asset("Out"), fh)
        new = am.save_renamed_asset(am.create_default_asset("Renamed"), outside)
        self.assertEqual(new, os.path.abspath(outside))
        self.assertEqual(self.files(), [])

    def test_failed_write_keeps_the_old_file(self):
        old = self.write("new_asset.json", "New Asset")
        with mock.patch.object(am.json, "dump", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                am.save_renamed_asset(am.create_default_asset("Yagi"), old)
        self.assertEqual(self.files(), ["new_asset.json"])
        self.assertEqual(self.name_in("new_asset.json"), "New Asset")


class TestOneAssetPerName(_StoreCase):

    def test_first_file_in_path_order_wins_and_is_logged_once(self):
        self.write("b.json", "Dup", frequency_mhz=200.0)
        self.write("a.json", "Dup", frequency_mhz=100.0)
        self.write("c.json", "Solo")
        with mock.patch.object(am, "_log") as log:
            first = am.resolve_assets()
            second = am.resolve_assets()
        self.assertEqual(sorted(first), ["Dup", "Solo"])
        self.assertEqual(os.path.basename(first["Dup"]["_path"]), "a.json")
        self.assertEqual(first["Dup"]["frequency_mhz"], 100.0)
        self.assertEqual(second["Dup"]["_path"], first["Dup"]["_path"])
        self.assertEqual(log.call_count, 1)
        self.assertIn("b.json", log.call_args[0][0])

    def test_both_run_tabs_resolve_a_duplicate_to_the_same_file(self):
        self.write("new_asset_2.json", "N2", frequency_mhz=222.0)
        self.write("dup.json", "N2", frequency_mhz=111.0)
        site = SiteAnalysisTab.__new__(SiteAnalysisTab)
        site._asset_cache = {}
        site._refresh_asset_cache()
        p2p = P2PTab.__new__(P2PTab)
        self.assertEqual(p2p._asset_names(), ["N2"])
        self.assertEqual(site._get_asset("N2")["frequency_mhz"], 111.0)
        p2p.spin_freq = mock.Mock()
        p2p.spin_erp = mock.Mock()
        p2p._on_asset_changed("N2")
        p2p.spin_freq.setValue.assert_called_once_with(111.0)


class TestSignature(_StoreCase):

    def test_changes_with_the_store_and_only_then(self):
        s0 = am.assets_signature()
        self.assertEqual(s0, am.assets_signature())
        path = self.write("a.json", "A")
        s1 = am.assets_signature()
        self.assertNotEqual(s0, s1)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(" ")
        self.assertNotEqual(s1, am.assets_signature())

    def test_other_files_do_not_count(self):
        s0 = am.assets_signature()
        with open(os.path.join(self.store, "notes.txt"), "w") as fh:
            fh.write("x")
        self.assertEqual(s0, am.assets_signature())

    def test_missing_folder_is_not_created(self):
        missing = os.path.join(self._tmp.name, "nope")
        QgsSettings().setValue("waveshed/assets_dir", missing)
        self.assertEqual(am.assets_signature(), (missing, None))
        self.assertFalse(os.path.exists(missing))


# ---------------------------------------------------------------------------
# gui/asset_manager_tab
# ---------------------------------------------------------------------------


class _Item:
    def __init__(self):
        self.text = None

    def setText(self, t):
        self.text = t


def _asset_tab(assets, current=0, editor_name=None):
    tab = AssetManagerTab.__new__(AssetManagerTab)
    tab._assets = assets
    tab._current_index = current
    tab._dirty = True
    tab._block_dirty = False
    tab.asset_list = mock.Mock()
    tab.asset_list.item.return_value = _Item()
    tab._rebuild_list = lambda: None
    tab.lbl_store = mock.Mock()
    base = dict(assets[current]) if assets else am.create_default_asset()
    base.pop("_path", None)
    if editor_name is not None:
        base["name"] = editor_name
    tab._collect_from_editor = lambda: dict(base)
    tab.events = []
    tab.asset_renamed.connect(lambda o, n: tab.events.append(("renamed", o, n)))
    tab.assets_changed.connect(lambda: tab.events.append(("changed",)))
    return tab


class TestAssetsTab(_StoreCase):

    def _loaded(self, filename, name):
        path = self.write(filename, name)
        asset = am.load_asset(path)
        asset["_path"] = path
        return asset

    def test_rename_save_moves_the_file_and_signals_rename_then_change(self):
        tab = _asset_tab([self._loaded("new_asset.json", "New Asset")],
                         editor_name="Yagi 14 el")
        self.assertTrue(tab._on_save())
        self.assertEqual(self.files(), ["yagi_14_el.json"])
        self.assertEqual(tab.events, [("renamed", "New Asset", "Yagi 14 el"),
                                      ("changed",)])
        self.assertFalse(tab._dirty)

    def test_plain_save_signals_change_only(self):
        tab = _asset_tab([self._loaded("a.json", "A")])
        self.assertTrue(tab._on_save())
        self.assertEqual(tab.events, [("changed",)])
        self.assertEqual(self.files(), ["a.json"])

    def test_rename_to_a_taken_name_is_refused_and_nothing_written(self):
        self.write("b.json", "B")
        tab = _asset_tab([self._loaded("a.json", "A")], editor_name="B")
        with mock.patch.object(amt.QMessageBox, "warning") as warn:
            self.assertFalse(tab._on_save())
        self.assertIn('"B"', warn.call_args[0][2])
        self.assertEqual(self.files(), ["a.json", "b.json"])
        self.assertEqual(self.name_in("a.json"), "A")
        self.assertEqual(tab.events, [])
        self.assertTrue(tab._dirty, "refused save dropped the edits")

    def test_unsaved_prompt_save_refused_does_not_go_on(self):
        self.write("b.json", "B")
        tab = _asset_tab([self._loaded("a.json", "A")], editor_name="B")
        with mock.patch.object(amt.QMessageBox, "question",
                               return_value=amt.QMessageBox.StandardButton.Save), \
                mock.patch.object(amt.QMessageBox, "warning"):
            self.assertFalse(tab._check_unsaved_changes())

    def test_new_after_rename_does_not_overwrite(self):
        # The store holds "Yagi 14 el" in new_asset.json (an old rename);
        # the list has no "New Asset" — New must not take that file.
        self.write("new_asset.json", "Yagi 14 el")
        tab = _asset_tab([self._loaded("other.json", "Other")])
        tab._dirty = False
        tab._on_new()
        self.assertEqual(self.name_in("new_asset.json"), "Yagi 14 el")
        self.assertIn("new_asset_2.json", self.files())
        self.assertEqual(self.name_in("new_asset_2.json"), "New Asset")
        self.assertEqual(tab.events, [("changed",)])

    def test_new_is_unique_against_names_on_disk(self):
        self.write("x.json", "New Asset")  # on disk, not in the list
        tab = _asset_tab([])
        tab._dirty = False
        tab._on_new()
        self.assertEqual(tab._assets[-1]["name"], "New Asset 2")

    def test_save_as_starts_in_the_store_with_the_name(self):
        tab = _asset_tab([self._loaded("a.json", "A")], editor_name="Yagi")
        with mock.patch.object(amt.QFileDialog, "getSaveFileName",
                               create=True, return_value=("", "")) as dlg:
            tab._on_save_as()
        self.assertEqual(dlg.call_args[0][2],
                         os.path.join(os.path.abspath(self.store), "yagi.json"))
        self.assertEqual(tab.events, [])

    def test_save_as_into_the_store_refuses_a_taken_name(self):
        tab = _asset_tab([self._loaded("a.json", "A")])  # name stays "A"
        target = os.path.join(self.store, "copy.json")
        with mock.patch.object(amt.QFileDialog, "getSaveFileName", create=True,
                               return_value=(target, "")), \
                mock.patch.object(amt.QMessageBox, "warning") as warn:
            tab._on_save_as()
        self.assertEqual(self.files(), ["a.json"])
        self.assertIn("Name already used", warn.call_args[0][1])

    def test_save_as_outside_warns_and_does_not_signal(self):
        tab = _asset_tab([self._loaded("a.json", "A")], editor_name="Out")
        target = os.path.join(self._tmp.name, "out")  # no extension
        with mock.patch.object(amt.QFileDialog, "getSaveFileName", create=True,
                               return_value=(target, "")), \
                mock.patch.object(amt.QMessageBox, "warning") as warn:
            tab._on_save_as()
        self.assertTrue(os.path.isfile(target + ".json"))
        self.assertIn(os.path.abspath(self.store), warn.call_args[0][2])
        self.assertEqual(tab.events, [])

    def test_change_folder_switches_the_store_and_signals(self):
        other = os.path.join(self._tmp.name, "other")
        os.makedirs(other)
        tab = _asset_tab([self._loaded("a.json", "A")])
        tab._dirty = False
        tab.refresh = lambda: None
        with mock.patch.object(amt.QFileDialog, "getExistingDirectory",
                               create=True, return_value=other):
            tab._on_change_store()
        self.assertEqual(am.get_assets_dir(), os.path.abspath(other))
        self.assertEqual(tab.events, [("changed",)])
        self.assertEqual(self.files(), ["a.json"], "old assets must stay")

    def test_change_folder_cancelled_at_unsaved_prompt_keeps_the_store(self):
        other = os.path.join(self._tmp.name, "other")
        os.makedirs(other)
        tab = _asset_tab([self._loaded("a.json", "A")])
        with mock.patch.object(amt.QFileDialog, "getExistingDirectory",
                               create=True, return_value=other), \
                mock.patch.object(amt.QMessageBox, "question",
                                  return_value=amt.QMessageBox.StandardButton.Cancel):
            tab._on_change_store()
        self.assertEqual(am.get_assets_dir(), self.store)
        self.assertEqual(tab.events, [])


# ---------------------------------------------------------------------------
# Run tabs
# ---------------------------------------------------------------------------


class _Table:
    def __init__(self, combos):
        self.combos = combos

    def rowCount(self):
        return len(self.combos)

    def cellWidget(self, row, col):
        return self.combos[row] if col == _COL_ASSET else None


class TestSiteTabAssets(unittest.TestCase):

    def _tab(self, mode="LOSS", sites=()):
        tab = SiteAnalysisTab.__new__(SiteAnalysisTab)
        tab._asset_cache = {"Kept": {"name": "Kept"}}
        tab._dialog = mock.Mock(get_mode=mock.Mock(return_value=mode))
        tab._read_sites = lambda: list(sites)
        return tab

    def test_rename_relabels_only_rows_showing_the_old_name(self):
        tab = self._tab()
        tab._asset_cache = {"Old": {"name": "Old"}, "Kept": {}}
        a = _FakeCombo(items=["", "Kept", "Old"], current="Old")
        b = _FakeCombo(items=["", "Kept", "Old"], current="Kept")
        tab.sites_table = _Table([a, b])
        tab.rename_asset("Old", "New")
        self.assertEqual((a.current, b.current), ("New", "Kept"))
        self.assertIn("New", tab._asset_cache)
        self.assertFalse(a.blocked or b.blocked)

    def test_unresolved_asset_refused_in_propagation_mode(self):
        sites = [{"asset_name": "Kept", "coords": (47, 8)},
                 {"asset_name": "Gone", "coords": (47, 8)},
                 {"asset_name": "", "coords": (47, 8)},
                 {"asset_name": "Gone too", "coords": None}]
        problem = self._tab()._unresolved_asset_problem("LOSS", sites)
        self.assertIn('Site 2: "Gone"', problem)
        self.assertNotIn("Gone too", problem)
        self.assertNotIn("Site 1", problem)
        self.assertNotIn("Site 3", problem)

    def test_los_modes_ignore_assets(self):
        sites = [{"asset_name": "Gone", "coords": (47, 8)}]
        for mode in ("LOS", "MIN_ALT"):
            self.assertIsNone(self._tab()._unresolved_asset_problem(mode, sites))

    def test_build_jobs_never_falls_back_silently(self):
        tab = self._tab("LOSS", [{"asset_name": "Gone", "coords": (47.0, 8.0)}])
        tab._dialog.get_loss_model.return_value = "ITM"
        tab.combo_resolution = mock.Mock(currentText=lambda: "30")
        tab.combo_backend = mock.Mock(currentText=lambda: "AUTO")
        tab.combo_climate = mock.Mock(currentIndex=lambda: 4)
        tab._read_altitudes = lambda: [(1.5, "AGL")]
        with self.assertRaises(UnresolvedAssetError) as ctx:
            tab._build_jobs()
        self.assertIn('Site 1: "Gone"', str(ctx.exception))

    def test_run_shows_the_refusal_and_launches_nothing(self):
        tab = self._tab("LOSS")
        tab.sites_table = _Table([])
        tab._build_jobs = mock.Mock(
            side_effect=UnresolvedAssetError('Site 1: "Gone"'))
        tab._launch_worker = mock.Mock()
        # Everything _on_run checks before the job build passes.
        tab.sites_table.rowCount = lambda: 1
        tab._read_sites = lambda: [{"asset_name": "Gone", "coords": (47, 8)}]
        tab.alt_table = mock.Mock(rowCount=lambda: 1)
        tab._selected_dem_layer = lambda: None
        tab._selected_terrain_dir = lambda: "/t"
        tab.line_output_dir = mock.Mock(text=lambda: "/out")
        tab._refresh_asset_cache = lambda: None
        with mock.patch.object(sat, "save_output_dir"), \
                mock.patch.object(sat, "setup_warning", return_value=""), \
                mock.patch.object(sat.QMessageBox, "warning") as warn:
            tab._on_run()
        self.assertEqual(warn.call_args[0][1], "Asset not found")
        self.assertIn('"Gone"', warn.call_args[0][2])
        tab._launch_worker.assert_not_called()


class TestP2PTabAssets(unittest.TestCase):

    def test_rename_follows_and_keeps_the_values(self):
        tab = P2PTab.__new__(P2PTab)
        tab.combo_asset = _FakeCombo(items=["", "Old", "X"], current="Old")
        tab.rename_asset("Old", "New")
        self.assertEqual(tab.combo_asset.current, "New")
        self.assertFalse(tab.combo_asset.blocked)

    def test_refresh_assets_lists_each_name_once(self):
        tab = P2PTab.__new__(P2PTab)
        tab.combo_asset = _FakeCombo(items=[""], current="")
        assets = [{"name": "D", "_path": "/s/b.json"},
                  {"name": "D", "_path": "/s/a.json"}]
        with mock.patch.object(p2p_mod, "list_assets", return_value=assets), \
                mock.patch.object(am, "_log"):
            tab.refresh_assets()
        self.assertEqual(tab.combo_asset.items, ["", "D"])


# ---------------------------------------------------------------------------
# main_dialog wiring
# ---------------------------------------------------------------------------


class TestMainDialogWiring(_StoreCase):

    def _dlg(self):
        dlg = AetherMainDialog.__new__(AetherMainDialog)
        dlg.site_tab = mock.Mock(spec=["refresh_assets", "rename_asset"])
        dlg.p2p_tab = mock.Mock(spec=["refresh_assets", "rename_asset"])
        return dlg

    def test_signals_reach_both_run_tabs(self):
        dlg = self._dlg()
        src = AssetManagerTab.__new__(AssetManagerTab)
        src.asset_renamed.connect(dlg._on_asset_renamed)
        src.assets_changed.connect(dlg._on_assets_changed)
        src.asset_renamed.emit("Old", "New")
        src.assets_changed.emit()
        for tab in (dlg.site_tab, dlg.p2p_tab):
            tab.rename_asset.assert_called_once_with("Old", "New")
            tab.refresh_assets.assert_called_once_with()

    def test_tab_activation_rereads_only_when_the_store_changed(self):
        dlg = self._dlg()
        dlg._mark_assets_seen()
        with mock.patch.object(md, "QgsMessageLog"):
            dlg._refresh_assets_if_changed(dlg.site_tab)
            dlg.site_tab.refresh_assets.assert_not_called()
            self.write("a.json", "A")
            dlg._refresh_assets_if_changed(dlg.site_tab)
            dlg._refresh_assets_if_changed(dlg.site_tab)
            dlg.site_tab.refresh_assets.assert_called_once_with()
            # The P2P tab has not seen the change yet.
            dlg._refresh_assets_if_changed(dlg.p2p_tab)
            dlg.p2p_tab.refresh_assets.assert_called_once_with()

    def test_change_signal_marks_the_store_seen(self):
        dlg = self._dlg()
        dlg._mark_assets_seen()
        self.write("a.json", "A")
        dlg._on_assets_changed()
        dlg._refresh_assets_if_changed(dlg.site_tab)
        dlg.site_tab.refresh_assets.assert_called_once_with()  # the signal's

    def test_other_tabs_are_not_refreshed(self):
        dlg = self._dlg()
        other = mock.Mock(spec=["refresh_assets"])
        dlg._refresh_assets_if_changed(other)
        other.refresh_assets.assert_not_called()


if __name__ == "__main__":
    unittest.main()
