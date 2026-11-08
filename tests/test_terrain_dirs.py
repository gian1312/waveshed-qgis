"""The DEM pickers' "Folder…" button and the remembered terrain folders.

Site Analysis (360°) and P2P list, ahead of the project's raster layers, the
Settings → Local terrain dir and then the folders picked with "Folder…"
(``waveshed/gui/terrain_dirs.py``, one recent list for both tabs). These tests
drive the real module and the real tab methods (``_populate_raster_layers``,
``_on_pick_terrain_dir``, ``_selected_terrain_dir``) against the dict-backed
QgsSettings stub from conftest.py, real folders in a temporary directory, a
combo with QComboBox's index rules and a mocked ``QFileDialog``.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import os
import tempfile
import unittest
from unittest import mock

from qgis.core import QgsRasterLayer, QgsSettings

from waveshed import help as help_pkg
import waveshed.gui.p2p_tab as p2p_mod
import waveshed.gui.site_analysis_tab as sa_mod
import waveshed.gui.terrain_dirs as td
from waveshed.gui.main_dialog import AetherMainDialog
from waveshed.gui.p2p_tab import P2PTab
from waveshed.gui.site_analysis_tab import SiteAnalysisTab

RECENT_KEY = "waveshed/terrain_dirs_recent"
SETTINGS_KEY = "waveshed/terrain_dir"
HOME = os.path.expanduser("~")


def _label(path):
    """The combo text of a folder entry (unchanged: "Local: <path>")."""
    return "Local: " + path


class _Combo:
    """A DEM combo with QComboBox's index rules: clear() leaves -1, the first
    item added becomes current, an index out of range is -1."""

    def __init__(self):
        self.items = []
        self.index = -1
        self.blocked = False

    def blockSignals(self, on):
        previous, self.blocked = self.blocked, on
        return previous

    def clear(self):
        self.items = []
        self.index = -1

    def addItem(self, text):
        self.items.append(text)
        if self.index < 0:
            self.index = 0

    def currentIndex(self):
        return self.index

    def setCurrentIndex(self, index):
        self.index = index if 0 <= index < len(self.items) else -1

    def currentText(self):
        return self.items[self.index] if self.index >= 0 else ""

    def select(self, text):
        self.setCurrentIndex(self.items.index(text))


class _Label:
    def __init__(self):
        self.text = ""

    def setText(self, text):
        self.text = text


class _Layer(QgsRasterLayer):
    """A project raster layer (the stub class, so isinstance holds)."""

    def __init__(self, layer_id, name):
        self._id = layer_id
        self._name = name

    def id(self):
        return self._id

    def name(self):
        return self._name


class _Project:
    """QgsProject.instance(): mapLayers() reads the live list."""

    def __init__(self, layers):
        self.layers = layers

    def mapLayers(self):
        return {layer.id(): layer for layer in self.layers}


class _Button:
    """A QPushButton that keeps its text and tooltip and has a real
    ``clicked`` signal."""

    def __init__(self, text="", *args, **kwargs):
        self.text = text
        self.tooltip = None
        self.clicked = conftest._BoundSignal()

    def setToolTip(self, tip):
        self.tooltip = tip

    def __getattr__(self, name):  # setEnabled, toggled, ...
        if name.startswith("_"):
            raise AttributeError(name)
        return mock.MagicMock()


class _FolderCase(unittest.TestCase):
    """A fresh settings store and a temporary directory for real folders."""

    def setUp(self):
        self.store = {}
        self._patch(mock.patch.object(QgsSettings, "_store", self.store))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

    def _patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def folder(self, name):
        path = os.path.join(self.root, name)
        os.makedirs(path, exist_ok=True)
        return path


# ---------------------------------------------------------------------------
# The module: the recent list, the combo's folders, the folder browser
# ---------------------------------------------------------------------------

class TestRecentList(_FolderCase):

    def test_keys_and_cap(self):
        self.assertEqual(td.RECENT_TERRAIN_DIRS_KEY, RECENT_KEY)
        self.assertEqual(td.SETTINGS_TERRAIN_DIR_KEY, SETTINGS_KEY)
        self.assertEqual(td.MAX_RECENT, 8)

    def test_a_pick_goes_first(self):
        a, b = self.folder("a"), self.folder("b")
        td.remember_terrain_dir(a)
        td.remember_terrain_dir(b)
        self.assertEqual(self.store[RECENT_KEY], [b, a])
        self.assertEqual(td.recent_terrain_dirs(), [b, a])

    def test_picking_a_folder_again_moves_it_up_without_a_duplicate(self):
        a, b = self.folder("a"), self.folder("b")
        for path in (a, b, a):
            td.remember_terrain_dir(path)
        self.assertEqual(self.store[RECENT_KEY], [a, b])

    def test_one_folder_spelled_differently_is_one_entry(self):
        a, b = self.folder("a"), self.folder("b")
        td.remember_terrain_dir(a)
        td.remember_terrain_dir(b)
        respelled = a + os.sep          # e.g. a trailing separator
        td.remember_terrain_dir(respelled)
        self.assertEqual(self.store[RECENT_KEY], [respelled, b])
        dotted = os.path.join(self.root, ".", "b")
        td.remember_terrain_dir(dotted)
        self.assertEqual(self.store[RECENT_KEY], [dotted, respelled])

    def test_at_most_eight_newest_first(self):
        folders = [self.folder(f"f{i}") for i in range(10)]
        for path in folders:
            td.remember_terrain_dir(path)
        expected = folders[::-1][:8]
        self.assertEqual(self.store[RECENT_KEY], expected)
        self.assertEqual(td.recent_terrain_dirs(), expected)

    def test_folders_that_no_longer_exist_are_dropped(self):
        a, gone = self.folder("a"), self.folder("gone")
        self.store[RECENT_KEY] = [gone, a]
        os.rmdir(gone)
        self.assertEqual(td.recent_terrain_dirs(), [a])
        self.assertEqual(self.store[RECENT_KEY], [a], "the store kept it")

    def test_a_pick_drops_vanished_folders_before_the_cap(self):
        gone = [self.folder(f"gone{i}") for i in range(7)]
        kept = self.folder("kept")
        self.store[RECENT_KEY] = gone + [kept]
        for path in gone:
            os.rmdir(path)
        new = self.folder("new")
        td.remember_terrain_dir(new)
        self.assertEqual(self.store[RECENT_KEY], [new, kept])

    def test_a_clean_list_is_not_rewritten(self):
        a = self.folder("a")
        stored = [a]
        self.store[RECENT_KEY] = stored
        self.assertEqual(td.recent_terrain_dirs(), [a])
        self.assertIs(self.store[RECENT_KEY], stored)

    def test_what_qsettings_hands_back_for_one_item_or_none(self):
        # An empty list reads back as None (QGIS 3.22, INI); some PyQt
        # builds hand a one-item list back as a plain string.
        a = self.folder("a")
        self.store[RECENT_KEY] = a
        self.assertEqual(td.recent_terrain_dirs(), [a])
        self.store[RECENT_KEY] = None
        self.assertEqual(td.recent_terrain_dirs(), [])
        self.store[RECENT_KEY] = ["", "  "]
        self.assertEqual(td.recent_terrain_dirs(), [])

    def test_a_blank_pick_stores_nothing(self):
        td.remember_terrain_dir("")
        td.remember_terrain_dir("   ")
        self.assertNotIn(RECENT_KEY, self.store)


class TestComboFolders(_FolderCase):

    def test_settings_folder_first_then_the_recent_picks(self):
        settings, a, b = self.folder("s"), self.folder("a"), self.folder("b")
        self.store[SETTINGS_KEY] = settings
        self.store[RECENT_KEY] = [b, a]
        self.assertEqual(td.combo_folders(), [settings, b, a])

    def test_no_settings_folder(self):
        a = self.folder("a")
        self.store[RECENT_KEY] = [a]
        self.assertEqual(td.combo_folders(), [a])
        self.store[SETTINGS_KEY] = "  "
        self.assertEqual(td.combo_folders(), [a])

    def test_a_missing_settings_folder_is_not_listed(self):
        a = self.folder("a")
        self.store[SETTINGS_KEY] = os.path.join(self.root, "missing")
        self.store[RECENT_KEY] = [a]
        self.assertEqual(td.combo_folders(), [a])

    def test_the_settings_folder_is_not_listed_twice(self):
        settings, a = self.folder("s"), self.folder("a")
        self.store[SETTINGS_KEY] = settings
        self.store[RECENT_KEY] = [a, settings + os.sep]
        self.assertEqual(td.combo_folders(), [settings, a])
        # ... and stays remembered, for when Settings names another folder.
        self.assertEqual(self.store[RECENT_KEY], [a, settings + os.sep])

    def test_entries_and_labels(self):
        a = self.folder("a")
        self.store[RECENT_KEY] = [a]
        combo, entries = _Combo(), []
        td.add_folder_entries(combo, entries)
        self.assertEqual(combo.items, [_label(a)])
        self.assertEqual(entries, [a])
        self.assertEqual(td.entry_label(a), _label(a))

    def test_folder_index_matches_the_folder_not_the_spelling(self):
        a, b = self.folder("a"), self.folder("b")
        layer = _Layer("id", "a layer")
        entries = [a, layer, b]
        self.assertEqual(td.folder_index(entries, b + os.sep), 2)
        self.assertEqual(td.folder_index(entries, a), 0)
        self.assertEqual(td.folder_index(entries, self.folder("c")), -1)
        self.assertEqual(td.folder_index(entries, ""), -1)
        self.assertEqual(td.folder_index(entries, None), -1)


class TestFolderBrowser(_FolderCase):

    def setUp(self):
        super().setUp()
        self.browser = self._patch(mock.patch.object(td, "QFileDialog"))

    def _ask(self, answer, selected=None):
        self.browser.getExistingDirectory.return_value = answer
        parent = object()
        result = td.ask_terrain_dir(parent, selected)
        args = self.browser.getExistingDirectory.call_args[0]
        self.assertIs(args[0], parent)
        return result, args[2]

    def test_starts_in_the_selected_folder(self):
        selected, last = self.folder("selected"), self.folder("last")
        self.store[RECENT_KEY] = [last]
        _, start = self._ask("", selected)
        self.assertEqual(start, selected)

    def test_else_in_the_last_folder_picked(self):
        last, older = self.folder("last"), self.folder("older")
        self.store[RECENT_KEY] = [last, older]
        self.assertEqual(self._ask("", None)[1], last)
        self.assertEqual(self._ask("", "")[1], last)
        gone = os.path.join(self.root, "gone")
        self.assertEqual(self._ask("", gone)[1], last)

    def test_else_at_home(self):
        self.assertEqual(self._ask("", None)[1], HOME)
        # A Settings folder is not "the last one picked".
        self.store[SETTINGS_KEY] = self.folder("s")
        self.assertEqual(self._ask("", None)[1], HOME)

    def test_a_pick_is_returned_and_remembered_first(self):
        older, new = self.folder("older"), self.folder("new")
        self.store[RECENT_KEY] = [older]
        result, _ = self._ask(new)
        self.assertEqual(result, new)
        self.assertEqual(self.store[RECENT_KEY], [new, older])

    def test_cancel_returns_nothing_and_stores_nothing(self):
        older = self.folder("older")
        self.store[RECENT_KEY] = [older]
        result, _ = self._ask("")
        self.assertEqual(result, "")
        self.assertEqual(self.store[RECENT_KEY], [older])

    def test_announce_reaches_the_dialog_hook_only_when_there_is_one(self):
        dialog = mock.Mock(spec=["terrain_dir_picked_in"])
        tab = object()
        td.announce_pick(dialog, tab)
        dialog.terrain_dir_picked_in.assert_called_once_with(tab)
        td.announce_pick(object(), tab)   # no hook: nothing happens
        td.announce_pick(None, tab)


# ---------------------------------------------------------------------------
# Both tabs: the DEM combo and its "Folder…" button
# ---------------------------------------------------------------------------

class _PickerCase:
    """The DEM picker contract, shared by both tabs."""

    module = None

    def setUp(self):
        super().setUp()
        self.layers = [_Layer("dem1", "swiss DEM"), _Layer("dem2", "srtm")]
        project = _Project(self.layers)
        self._patch(mock.patch.object(
            self.module, "QgsProject", mock.Mock(instance=lambda: project)))
        self.browser = self._patch(mock.patch.object(td, "QFileDialog"))
        self.dialog = mock.Mock(spec=["terrain_dir_picked_in"])

    def new_tab(self):
        raise NotImplementedError

    def pick(self, tab, answer):
        self.browser.getExistingDirectory.reset_mock()
        self.browser.getExistingDirectory.return_value = answer
        tab._on_pick_terrain_dir()
        return self.browser.getExistingDirectory.call_args[0][2]

    def assertNoFolderSelected(self, tab):
        self.assertFalse(tab._selected_terrain_dir())

    # -- listing --------------------------------------------------------

    def test_settings_folder_then_recent_folders_then_layers(self):
        settings, a, b = self.folder("s"), self.folder("a"), self.folder("b")
        self.store[SETTINGS_KEY] = settings
        self.store[RECENT_KEY] = [b, a]
        tab = self.new_tab()
        self.assertEqual(tab.combo_dem.items, [
            _label(settings), _label(b), _label(a), "swiss DEM", "srtm"])
        self.assertEqual(tab._dem_layers[:3], [settings, b, a])
        self.assertEqual(tab._dem_layers[3:], self.layers)
        self.assertEqual(tab._selected_terrain_dir(), settings)
        self.assertFalse(tab.combo_dem.blocked, "signals left blocked")

    def test_without_folders_only_layers(self):
        tab = self.new_tab()
        self.assertEqual(tab.combo_dem.items, ["swiss DEM", "srtm"])
        self.assertNoFolderSelected(tab)

    def test_the_settings_folder_is_listed_once(self):
        settings, a = self.folder("s"), self.folder("a")
        self.store[SETTINGS_KEY] = settings
        self.store[RECENT_KEY] = [settings + os.sep, a, settings]
        tab = self.new_tab()
        self.assertEqual(tab.combo_dem.items, [
            _label(settings), _label(a), "swiss DEM", "srtm"])

    def test_a_vanished_recent_folder_is_not_listed_and_forgotten(self):
        a, gone = self.folder("a"), self.folder("gone")
        self.store[RECENT_KEY] = [gone, a]
        os.rmdir(gone)
        tab = self.new_tab()
        self.assertEqual(tab.combo_dem.items, [_label(a), "swiss DEM", "srtm"])
        self.assertEqual(self.store[RECENT_KEY], [a])

    # -- picking --------------------------------------------------------

    def test_a_picked_folder_is_selected_and_remembered(self):
        tab = self.new_tab()
        self.assertNoFolderSelected(tab)          # a layer is selected
        target = self.folder("picked")
        self.pick(tab, target)
        self.assertEqual(tab._selected_terrain_dir(), target)
        self.assertIsNone(tab._selected_dem_layer())
        self.assertEqual(tab.combo_dem.items,
                         [_label(target), "swiss DEM", "srtm"])
        self.assertEqual(tab.combo_dem.currentText(), _label(target))
        self.assertEqual(self.store[RECENT_KEY], [target])
        self.assertFalse(tab.combo_dem.blocked)
        self.dialog.terrain_dir_picked_in.assert_called_once_with(tab)

    def test_with_a_layer_selected_the_browser_opens_in_the_last_pick(self):
        last = self.folder("last")
        self.store[RECENT_KEY] = [last]
        tab = self.new_tab()
        tab.combo_dem.select("srtm")
        self.assertNoFolderSelected(tab)
        new = self.folder("new")
        self.assertEqual(self.pick(tab, new), last)
        self.assertEqual(tab._selected_terrain_dir(), new)
        self.assertEqual(tab.combo_dem.items[:2], [_label(new), _label(last)])
        self.assertEqual(self.store[RECENT_KEY], [new, last])

    def test_the_browser_opens_in_the_selected_folder(self):
        a, b = self.folder("a"), self.folder("b")
        self.store[RECENT_KEY] = [a, b]
        tab = self.new_tab()
        tab.combo_dem.select(_label(b))
        self.assertEqual(self.pick(tab, ""), b)

    def test_the_browser_opens_at_home_without_any_folder(self):
        tab = self.new_tab()
        self.assertEqual(self.pick(tab, ""), HOME)

    def test_cancel_changes_nothing(self):
        a = self.folder("a")
        self.store[RECENT_KEY] = [a]
        tab = self.new_tab()
        tab.combo_dem.select("srtm")
        before = (list(tab.combo_dem.items), tab.combo_dem.currentIndex(),
                  list(tab._dem_layers))
        self.pick(tab, "")
        self.assertEqual((list(tab.combo_dem.items),
                          tab.combo_dem.currentIndex(),
                          list(tab._dem_layers)), before)
        self.assertEqual(self.store[RECENT_KEY], [a])
        self.dialog.terrain_dir_picked_in.assert_not_called()

    def test_picking_the_settings_folder_selects_its_entry(self):
        settings = self.folder("s")
        self.store[SETTINGS_KEY] = settings
        tab = self.new_tab()
        tab.combo_dem.select("srtm")
        self.pick(tab, settings + os.sep)          # the same folder
        self.assertEqual(tab.combo_dem.items,
                         [_label(settings), "swiss DEM", "srtm"])
        self.assertEqual(tab.combo_dem.currentIndex(), 0)
        self.assertEqual(tab._selected_terrain_dir(), settings)

    # -- refills --------------------------------------------------------

    def test_a_picked_folder_survives_a_refill(self):
        tab = self.new_tab()
        target = self.folder("picked")
        self.pick(tab, target)
        self.layers.append(_Layer("dem3", "new layer"))  # project changed
        tab._on_project_layers_changed()
        self.assertIn("new layer", tab.combo_dem.items)
        self.assertEqual(tab._selected_terrain_dir(), target)

    def test_a_selected_layer_survives_a_refill_that_adds_a_folder(self):
        tab = self.new_tab()
        tab.combo_dem.select("srtm")
        # A folder picked in the other tab lands in the shared list.
        td.remember_terrain_dir(self.folder("other"))
        tab._populate_raster_layers()
        self.assertEqual(tab.combo_dem.items[0], _label(self.folder("other")))
        self.assertEqual(tab.combo_dem.currentText(), "srtm")
        self.assertNoFolderSelected(tab)

    def test_a_selected_recent_folder_survives_a_refill(self):
        a, b = self.folder("a"), self.folder("b")
        self.store[RECENT_KEY] = [a, b]
        tab = self.new_tab()
        tab.combo_dem.select(_label(b))
        td.remember_terrain_dir(self.folder("c"))   # b moves down one
        tab._populate_raster_layers()
        self.assertEqual(tab.combo_dem.items[:3],
                         [_label(self.folder("c")), _label(a), _label(b)])
        self.assertEqual(tab._selected_terrain_dir(), b)


class TestSiteAnalysisPicker(_PickerCase, _FolderCase):
    module = sa_mod

    def setUp(self):
        super().setUp()
        self._patch(mock.patch.object(
            sa_mod, "get_source_resolution_info", lambda layer: "10 m"))
        self._patch(mock.patch.object(
            sa_mod, "dem_layer_warning", lambda layer: None))

    def new_tab(self):
        tab = SiteAnalysisTab.__new__(SiteAnalysisTab)
        tab.combo_dem = _Combo()
        tab.lbl_dem_info = _Label()
        tab._dialog = self.dialog
        tab._populate_raster_layers()
        return tab

    def test_the_info_line_follows_a_pick(self):
        # _on_dem_changed runs for the folder selected.
        tab = self.new_tab()
        self.assertEqual(tab.lbl_dem_info.text, "Source resolution: 10 m")
        target = self.folder("picked")
        self.pick(tab, target)
        self.assertEqual(tab.lbl_dem_info.text, f"Local directory: {target}")

    def test_the_key_of_a_folder_entry_is_its_path(self):
        a = self.folder("a")
        self.store[RECENT_KEY] = [a]
        tab = self.new_tab()
        self.assertEqual(tab._current_dem_key(), a)


class TestP2PPicker(_PickerCase, _FolderCase):
    module = p2p_mod

    def new_tab(self):
        tab = P2PTab.__new__(P2PTab)
        tab.combo_dem = _Combo()
        tab._parent_dialog = self.dialog
        tab._populate_raster_layers()
        return tab


# ---------------------------------------------------------------------------
# The two tabs together, the button, the help
# ---------------------------------------------------------------------------

class TestSharedBetweenTabs(_FolderCase):

    def setUp(self):
        super().setUp()
        self.layers = [_Layer("dem1", "swiss DEM")]
        project = _Project(self.layers)
        for module in (sa_mod, p2p_mod):
            self._patch(mock.patch.object(
                module, "QgsProject", mock.Mock(instance=lambda: project)))
        self._patch(mock.patch.object(
            sa_mod, "get_source_resolution_info", lambda layer: "10 m"))
        self._patch(mock.patch.object(
            sa_mod, "dem_layer_warning", lambda layer: None))
        self.browser = self._patch(mock.patch.object(td, "QFileDialog"))
        self.dlg = AetherMainDialog.__new__(AetherMainDialog)
        self.dlg.site_tab = SiteAnalysisTab.__new__(SiteAnalysisTab)
        self.dlg.site_tab.combo_dem = _Combo()
        self.dlg.site_tab.lbl_dem_info = _Label()
        self.dlg.site_tab._dialog = self.dlg
        self.dlg.p2p_tab = P2PTab.__new__(P2PTab)
        self.dlg.p2p_tab.combo_dem = _Combo()
        self.dlg.p2p_tab._parent_dialog = self.dlg
        for tab in (self.dlg.site_tab, self.dlg.p2p_tab):
            tab._populate_raster_layers()

    def _pick(self, tab, answer):
        self.browser.getExistingDirectory.return_value = answer
        tab._on_pick_terrain_dir()

    def test_a_pick_in_the_360_tab_is_listed_in_p2p_at_once(self):
        site, p2p = self.dlg.site_tab, self.dlg.p2p_tab
        target = self.folder("picked")
        self._pick(site, target)
        self.assertEqual(site._selected_terrain_dir(), target)
        self.assertEqual(p2p.combo_dem.items, [_label(target), "swiss DEM"])
        self.assertEqual(p2p.combo_dem.currentText(), "swiss DEM",
                         "the other tab's selection changed")

    def test_a_pick_in_p2p_is_listed_in_the_360_tab_at_once(self):
        site, p2p = self.dlg.site_tab, self.dlg.p2p_tab
        target = self.folder("picked")
        self._pick(p2p, target)
        self.assertEqual(p2p._selected_terrain_dir(), target)
        self.assertEqual(site.combo_dem.items, [_label(target), "swiss DEM"])
        self.assertIsNone(site._selected_terrain_dir())

    def test_a_reopened_dialog_lists_the_picks(self):
        first, second = self.folder("first"), self.folder("second")
        self._pick(self.dlg.site_tab, first)
        self._pick(self.dlg.p2p_tab, second)
        reopened = P2PTab.__new__(P2PTab)
        reopened.combo_dem = _Combo()
        reopened._parent_dialog = None
        reopened._populate_raster_layers()
        self.assertEqual(reopened.combo_dem.items,
                         [_label(second), _label(first), "swiss DEM"])

    def test_the_dialog_refills_only_the_other_tab(self):
        dlg = AetherMainDialog.__new__(AetherMainDialog)
        dlg.site_tab = mock.Mock(spec=["_populate_raster_layers"])
        dlg.p2p_tab = mock.Mock(spec=["_populate_raster_layers"])
        dlg.terrain_dir_picked_in(dlg.site_tab)
        dlg.p2p_tab._populate_raster_layers.assert_called_once_with()
        dlg.site_tab._populate_raster_layers.assert_not_called()
        dlg.terrain_dir_picked_in(dlg.p2p_tab)
        dlg.site_tab._populate_raster_layers.assert_called_once_with()


def _built(module, tab_cls, builder, noop=()):
    """Run *tab_cls.builder* on a bare instance with every Qt widget class
    of *module* mocked, QPushButton recording (see test_output_dir_persist).
    Returns the tab and the mocks by class name."""
    names = [n for n in dir(module) if n.startswith("Q")
             and isinstance(getattr(module, n), type)
             and n not in ("QgsSettings", "QThread", "QPushButton")]
    mocks = {n: mock.MagicMock() for n in names}
    patches = [mock.patch.object(module, n, m) for n, m in mocks.items()]
    patches.append(mock.patch.object(module, "QPushButton", _Button))
    tab = tab_cls.__new__(tab_cls)
    for name in noop:
        setattr(tab, name, lambda *a, **kw: None)
    for patcher in patches:
        patcher.start()
    try:
        with mock.patch.object(QgsSettings, "_store", {}):
            getattr(tab, builder)()
    finally:
        for patcher in patches:
            patcher.stop()
    return tab, mocks


def _wired(tab_cls):
    """A bare tab whose widgets are MagicMocks, except the Folder… button,
    after ``_connect_signals``; returns (tab, calls of the pick slot)."""

    class Bare(tab_cls):
        def __getattr__(self, name):
            if name.startswith("_"):
                raise AttributeError(name)
            value = mock.MagicMock()
            setattr(self, name, value)
            return value

    tab = Bare.__new__(Bare)
    tab.btn_dem_folder = _Button(td.FOLDER_BUTTON_TEXT)
    calls = []
    tab._on_pick_terrain_dir = lambda *a: calls.append(a)
    tab._connect_signals()
    return tab, calls


class TestTheButton(unittest.TestCase):

    def test_both_tabs_build_it_beside_the_combo(self):
        for module, cls, builder, noop in (
                (sa_mod, SiteAnalysisTab, "_build_analysis_section",
                 ("_fill_output_res",)),
                (p2p_mod, P2PTab, "_build_analysis_group", ())):
            with self.subTest(cls.__name__):
                tab, mocks = _built(module, cls, builder, noop)
                self.assertIsInstance(tab.btn_dem_folder, _Button)
                self.assertEqual(tab.btn_dem_folder.text, "Folder…")
                self.assertEqual(tab.btn_dem_folder.tooltip,
                                 td.FOLDER_BUTTON_TOOLTIP)
                # In the "DEM Layer:" row, right after the combo.
                row = mocks["QHBoxLayout"].return_value
                added = [c.args[0] for c in row.addWidget.call_args_list]
                at = added.index(tab.combo_dem)
                self.assertIs(added[at + 1], tab.btn_dem_folder)
                form = mocks["QFormLayout"].return_value
                self.assertIn(mock.call("DEM Layer:", row),
                              form.addRow.call_args_list)

    def test_clicking_it_runs_the_pick(self):
        for cls in (SiteAnalysisTab, P2PTab):
            with self.subTest(cls.__name__):
                tab, calls = _wired(cls)
                self.assertEqual(calls, [])
                tab.btn_dem_folder.clicked.emit()
                self.assertEqual(calls, [()])

    def test_the_help_describes_it_where_the_dem_picker_is(self):
        for anchor in ("terrain", "site-analysis", "p2p", "settings"):
            with self.subTest(anchor):
                topic = help_pkg.find_topic(anchor)
                self.assertIn(td.FOLDER_BUTTON_TEXT, topic.body)
        text = help_pkg._plain_text(help_pkg.build_html())
        self.assertIn(f"the last {td.MAX_RECENT} picked", text)


if __name__ == "__main__":
    unittest.main()
