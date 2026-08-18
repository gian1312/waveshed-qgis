"""Settings propagation to open tabs (TODO: "Settings dialog changes don't
reach open tabs").

Pattern follows test_settings_dialog.py: objects are built via ``__new__``
(bypassing the Qt-heavy ``__init__``) and driven with small fake widgets.
The conftest stubs provide QgsSettings (a dict-backed store) and a working
pyqtSignal descriptor, so signal emission and settings reads are real; the
widget layer is faked per test.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import os
import tempfile
import unittest
from unittest import mock

from qgis.core import QgsSettings
from qgis.PyQt import QtWidgets

from waveshed.gui.settings_dialog import SettingsDialog
from waveshed.gui.main_dialog import AetherMainDialog
from waveshed.gui.site_analysis_tab import SiteAnalysisTab, _COL_ASSET
from waveshed.gui.p2p_tab import P2PTab
from waveshed.gui.asset_manager_tab import AssetManagerTab
from waveshed.gui.map_converter_tab import MapConverterTab
import waveshed.gui.p2p_tab as p2p_mod
import waveshed.core.terrain_adapter as ta


def _fresh_store():
    """Patch the stub QgsSettings store with an isolated dict."""
    return mock.patch.object(QgsSettings, "_store", {})


class _FakeLineEdit:
    def __init__(self, text=""):
        self._text = text
        self.placeholder = None

    def text(self):
        return self._text

    def setText(self, t):
        self._text = t

    def setPlaceholderText(self, t):
        self.placeholder = t


class _FakeSpin:
    def __init__(self, value):
        self._value = value

    def value(self):
        return self._value


class _FakeLabel:
    def __init__(self):
        self.text_value = None
        self.style = None

    def setText(self, t):
        self.text_value = t

    def setStyleSheet(self, s):
        self.style = s


class _FakeCombo(QtWidgets.QComboBox):
    """QComboBox stand-in that IS a (stub) QComboBox for isinstance checks."""

    def __init__(self, items=None, current=""):
        self.items = list(items or [])
        self.current = current
        self.blocked = False

    def currentText(self):
        return self.current

    def blockSignals(self, v):
        prev, self.blocked = self.blocked, v
        return prev

    def clear(self):
        self.items = []
        self.current = ""

    def addItem(self, item):
        self.items.append(item)

    def addItems(self, items):
        self.items.extend(items)

    def findText(self, text):
        return self.items.index(text) if text in self.items else -1

    def setCurrentIndex(self, idx):
        self.current = self.items[idx] if 0 <= idx < len(self.items) else ""


def _settings_dialog(binary_dir="", cache_dir="/c", terrain_dir=""):
    dlg = SettingsDialog.__new__(SettingsDialog)
    dlg._settings = QgsSettings()
    dlg._binary_dir_edit = _FakeLineEdit(binary_dir)
    dlg._terrain_dir_edit = _FakeLineEdit(terrain_dir)
    dlg._cache_dir_edit = _FakeLineEdit(cache_dir)
    dlg._vram_spin = _FakeSpin(8)
    dlg._ram_spin = _FakeSpin(16)
    dlg._conn_spin = _FakeSpin(256)
    dlg._api_key_edit = _FakeLineEdit("")
    dlg._binary_status_label = _FakeLabel()
    dlg._prereq_label = _FakeLabel()
    return dlg


class TestSettingsChangedSignal(unittest.TestCase):
    """save_settings emits settings_changed — but only on an actual change."""

    def test_signal_emitted_when_a_value_changes(self):
        with _fresh_store():
            dlg = _settings_dialog(cache_dir="/new/cache")
            fired = []
            dlg.settings_changed.connect(lambda: fired.append(1))
            dlg.save_settings()
        self.assertEqual(fired, [1])

    def test_saving_identical_values_does_not_emit(self):
        with _fresh_store():
            dlg = _settings_dialog(cache_dir="/new/cache")
            fired = []
            dlg.settings_changed.connect(lambda: fired.append(1))
            dlg.save_settings()   # first save changes the store
            dlg.save_settings()   # identical values — no change
        self.assertEqual(fired, [1])

    def test_invalid_binary_dir_surfaces_in_the_refreshed_status(self):
        # Fail-loudly: saving a bad binary path must say "Missing" NOW, not
        # keep an old discovery result.
        with _fresh_store():
            dlg = _settings_dialog(binary_dir="/no/such/binary/dir")
            dlg.save_settings()
        self.assertIn("Missing", dlg._binary_status_label.text_value)
        self.assertIn("red", dlg._binary_status_label.style)

    def test_valid_binary_dir_reports_found(self):
        with _fresh_store(), tempfile.TemporaryDirectory() as d:
            from waveshed.core import binary_manager as bm
            for name in bm.REQUIRED_BINARIES:
                p = os.path.join(d, name + bm._EXE_SUFFIX)
                with open(p, "w") as fh:
                    fh.write("x")
                os.chmod(p, 0o755)
            dlg = _settings_dialog(binary_dir=d)
            with mock.patch.object(bm, "check_prerequisites",
                                   return_value=[]):
                dlg.save_settings()
            self.assertIn("Found", dlg._binary_status_label.text_value)


class TestMainDialogFanOut(unittest.TestCase):
    """The main dialog fans settings_changed out to every tab's refresh."""

    def test_all_tabs_are_refreshed(self):
        dlg = AetherMainDialog.__new__(AetherMainDialog)
        tabs = [mock.Mock(spec=["refresh_settings"]) for _ in range(4)]
        (dlg.site_tab, dlg.p2p_tab, dlg.asset_tab, dlg.converter_tab) = tabs
        dlg._on_settings_changed()
        for tab in tabs:
            tab.refresh_settings.assert_called_once_with()

    def test_a_tab_without_the_hook_is_skipped(self):
        dlg = AetherMainDialog.__new__(AetherMainDialog)
        dlg.site_tab = object()          # no refresh_settings
        dlg.p2p_tab = mock.Mock(spec=["refresh_settings"])
        dlg.asset_tab = object()
        dlg.converter_tab = mock.Mock(spec=["refresh_settings"])
        dlg._on_settings_changed()       # must not raise
        dlg.p2p_tab.refresh_settings.assert_called_once_with()

    def test_signal_to_fanout_wiring_shape(self):
        # The connection itself: a settings dialog's emission reaches
        # _on_settings_changed once connected (as _connect_signals does).
        with _fresh_store():
            sd = _settings_dialog(cache_dir="/x")
            dlg = AetherMainDialog.__new__(AetherMainDialog)
            tab = mock.Mock(spec=["refresh_settings"])
            dlg.site_tab = dlg.p2p_tab = dlg.asset_tab = dlg.converter_tab = tab
            sd.settings_changed.connect(dlg._on_settings_changed)
            sd.save_settings()
        self.assertEqual(tab.refresh_settings.call_count, 4)


class TestSiteTabRefresh(unittest.TestCase):

    def _tab(self):
        tab = SiteAnalysisTab.__new__(SiteAnalysisTab)
        tab._asset_cache = {}
        return tab

    def test_refresh_settings_rereads_all_three_sources(self):
        tab = self._tab()
        calls = []
        tab._populate_raster_layers = lambda: calls.append("dem")
        tab._refresh_asset_cache = lambda: calls.append("assets")
        tab._refresh_asset_combos = lambda: calls.append("combos")
        tab.refresh_settings()
        self.assertEqual(calls, ["dem", "assets", "combos"])

    def test_asset_combos_repopulate_and_keep_the_row_choice(self):
        tab = self._tab()
        tab._asset_cache = {"NewAsset": {}, "Kept": {}}
        combo = _FakeCombo(items=["", "Kept", "Gone"], current="Kept")

        class _Table:
            def rowCount(self):
                return 1

            def cellWidget(self, row, col):
                return combo if col == _COL_ASSET else None

        tab.sites_table = _Table()
        tab._refresh_asset_combos()
        self.assertEqual(combo.items, ["", "Kept", "NewAsset"])
        self.assertEqual(combo.current, "Kept", "user's row choice was lost")
        self.assertFalse(combo.blocked, "signals left blocked")

    def test_vanished_asset_falls_back_to_blank_visibly(self):
        tab = self._tab()
        tab._asset_cache = {"Other": {}}
        combo = _FakeCombo(items=["", "Gone"], current="Gone")

        class _Table:
            def rowCount(self):
                return 1

            def cellWidget(self, row, col):
                return combo if col == _COL_ASSET else None

        tab.sites_table = _Table()
        tab._refresh_asset_combos()
        self.assertEqual(combo.current, "")


class TestP2PTabRefresh(unittest.TestCase):

    def test_refresh_settings_repopulates_assets_keeping_choice(self):
        tab = P2PTab.__new__(P2PTab)
        tab.combo_asset = _FakeCombo(items=["", "Kept", "Old"],
                                     current="Kept")
        fake_assets = [{"name": "Kept"}, {"name": "Fresh"}]
        with mock.patch.object(p2p_mod, "list_assets",
                               return_value=fake_assets):
            tab.refresh_settings()
        self.assertIn("Fresh", tab.combo_asset.items)
        self.assertNotIn("Old", tab.combo_asset.items)
        self.assertEqual(tab.combo_asset.current, "Kept")


class TestAssetTabRefresh(unittest.TestCase):

    def _tab(self, dirty=False, assets=None, current=0):
        tab = AssetManagerTab.__new__(AssetManagerTab)
        tab._dirty = dirty
        tab._assets = assets or []
        tab._current_index = current
        tab.refreshed = []
        tab.refresh = lambda: tab.refreshed.append(1)
        tab.asset_list = mock.Mock()
        return tab

    def test_clean_editor_reloads_and_reselects_by_name(self):
        assets = [{"name": "A"}, {"name": "B"}]
        tab = self._tab(assets=assets, current=1)   # "B" selected

        def fake_refresh():
            tab.refreshed.append(1)
            tab._assets = [{"name": "B"}, {"name": "C"}]  # store changed

        tab.refresh = fake_refresh
        tab.refresh_settings()
        self.assertEqual(tab.refreshed, [1])
        tab.asset_list.setCurrentRow.assert_called_once_with(0)  # "B" now row 0

    def test_dirty_editor_survives_untouched(self):
        # A half-filled form must never be clobbered by a settings save.
        tab = self._tab(dirty=True, assets=[{"name": "A"}])
        tab.refresh_settings()
        self.assertEqual(tab.refreshed, [], "reload clobbered a dirty editor")


class TestMapConverterTabRefresh(unittest.TestCase):

    def _tab(self, typed=""):
        tab = MapConverterTab.__new__(MapConverterTab)
        tab._edit_output = _FakeLineEdit(typed)
        tab.estimates = []
        tab._update_estimate = lambda: tab.estimates.append(1)
        return tab

    def test_default_output_dir_follows_the_cache_setting(self):
        with _fresh_store():
            QgsSettings().setValue("waveshed/cache_dir", "/relocated/cache")
            tab = self._tab()
            tab.refresh_settings()
            self.assertEqual(tab._edit_output.placeholder, "/relocated/cache")
            self.assertEqual(tab._get_output_dir(), "/relocated/cache")
            self.assertEqual(tab.estimates, [1])

    def test_user_typed_output_path_survives_refresh(self):
        with _fresh_store():
            QgsSettings().setValue("waveshed/cache_dir", "/relocated/cache")
            tab = self._tab(typed="/my/output")
            tab.refresh_settings()
            self.assertEqual(tab._get_output_dir(), "/my/output")
            self.assertEqual(tab._edit_output.text(), "/my/output")
