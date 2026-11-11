"""Site Analysis / P2P "Output Dir" survives the dialog being rebuilt.

The main dialog is deleted on close and a new one is built on every open, so
the field used to jump back to ``<temp>/aether_output`` each time. These tests
drive the real tab code paths (the builder that creates the field, Browse, and
Run) against the dict-backed QgsSettings stub from conftest.py.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import os
import tempfile
import unittest
from unittest import mock

from qgis.core import QgsSettings

import waveshed.gui.output_dir as od
import waveshed.gui.p2p_tab as p2p_mod
import waveshed.gui.site_analysis_tab as sa_mod
from waveshed.gui.p2p_tab import P2PTab
from waveshed.gui.site_analysis_tab import SiteAnalysisTab

_TEMP_DEFAULT = os.path.join(tempfile.gettempdir(), "aether_output")


class _LineEdit:
    """A QLineEdit with a working editingFinished signal."""

    editingFinished = conftest._MockSignalDescriptor()

    def __init__(self, *a, **kw):
        self._text = ""

    def text(self):
        return self._text

    def setText(self, t):
        self._text = t

    def __getattr__(self, name):  # setToolTip, setPlaceholderText, ...
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **kw: None


def _fresh_store():
    return mock.patch.object(QgsSettings, "_store", {})


def _build_with_fake_widgets(module, tab_cls, builder: str, noop=()):
    """Run *tab_cls.builder* on a bare instance with every Qt widget mocked
    except QLineEdit, which is the fake above. Returns the tab."""
    names = [n for n in dir(module) if n.startswith("Q") and n != "QLineEdit"
             and isinstance(getattr(module, n), type)
             and n not in ("QgsSettings", "QThread")]
    patches = [mock.patch.object(module, n, mock.MagicMock()) for n in names]
    patches.append(mock.patch.object(module, "QLineEdit", _LineEdit))
    tab = tab_cls.__new__(tab_cls)
    for name in noop:  # helpers that read mocked combos back as numbers
        setattr(tab, name, lambda *a, **kw: None)
    for p in patches:
        p.start()
    try:
        getattr(tab, builder)()
    finally:
        for p in patches:
            p.stop()
    return tab


class TestHelper(unittest.TestCase):
    def test_unset_falls_back_to_temp(self):
        with _fresh_store():
            self.assertEqual(od.load_output_dir(od.SITE_OUTPUT_DIR_KEY), _TEMP_DEFAULT)

    def test_empty_or_blank_falls_back_to_temp(self):
        with _fresh_store():
            QgsSettings().setValue(od.P2P_OUTPUT_DIR_KEY, "   ")
            self.assertEqual(od.load_output_dir(od.P2P_OUTPUT_DIR_KEY), _TEMP_DEFAULT)

    def test_saved_value_round_trips(self):
        with _fresh_store():
            od.save_output_dir(od.SITE_OUTPUT_DIR_KEY, "  /data/out  ")
            self.assertEqual(QgsSettings._store[od.SITE_OUTPUT_DIR_KEY], "/data/out")
            self.assertEqual(od.load_output_dir(od.SITE_OUTPUT_DIR_KEY), "/data/out")

    def test_blank_save_keeps_previous(self):
        with _fresh_store():
            od.save_output_dir(od.SITE_OUTPUT_DIR_KEY, "/data/out")
            od.save_output_dir(od.SITE_OUTPUT_DIR_KEY, "")
            self.assertEqual(od.load_output_dir(od.SITE_OUTPUT_DIR_KEY), "/data/out")

    def test_keys_are_waveshed_scoped_and_distinct(self):
        self.assertEqual(od.SITE_OUTPUT_DIR_KEY, "waveshed/site_output_dir")
        self.assertEqual(od.P2P_OUTPUT_DIR_KEY, "waveshed/p2p_output_dir")


class _TabCase:
    """Shared tests; subclasses set module/cls/builder/key."""

    module = None
    cls = None
    builder = ""
    noop = ()
    key = ""
    other_key = ""

    def _build(self):
        return _build_with_fake_widgets(
            self.module, self.cls, self.builder, self.noop)

    def test_fresh_install_shows_temp_default(self):
        with _fresh_store():
            tab = self._build()
            self.assertEqual(tab.line_output_dir.text(), _TEMP_DEFAULT)

    def test_new_dialog_opens_with_saved_folder(self):
        with _fresh_store():
            QgsSettings().setValue(self.key, "/remembered/out")
            tab = self._build()
            self.assertEqual(tab.line_output_dir.text(), "/remembered/out")

    def test_typed_path_persists_on_editing_finished(self):
        with _fresh_store():
            tab = self._build()
            tab.line_output_dir.setText("/typed/out")
            tab.line_output_dir.editingFinished.emit()
            self.assertEqual(QgsSettings._store.get(self.key), "/typed/out")
            # ... and a rebuilt dialog (close + reopen) shows it.
            self.assertEqual(self._build().line_output_dir.text(), "/typed/out")
            # The other tab's folder is not touched.
            self.assertNotIn(self.other_key, QgsSettings._store)

    def test_browse_selection_persists(self):
        with _fresh_store():
            tab = self._build()
            dlg = mock.MagicMock()
            dlg.getExistingDirectory.return_value = "/browsed/out"
            with mock.patch.object(self.module, "QFileDialog", dlg):
                tab._on_browse_dir()
            self.assertEqual(tab.line_output_dir.text(), "/browsed/out")
            self.assertEqual(QgsSettings._store.get(self.key), "/browsed/out")
            self.assertEqual(self._build().line_output_dir.text(), "/browsed/out")

    def test_browse_cancel_saves_nothing(self):
        with _fresh_store():
            QgsSettings().setValue(self.key, "/kept")
            tab = self._build()
            dlg = mock.MagicMock()
            dlg.getExistingDirectory.return_value = ""
            with mock.patch.object(self.module, "QFileDialog", dlg):
                tab._on_browse_dir()
            self.assertEqual(QgsSettings._store.get(self.key), "/kept")


class TestSiteAnalysisTab(_TabCase, unittest.TestCase):
    module = sa_mod
    cls = SiteAnalysisTab
    builder = "_build_analysis_section"
    noop = ("_fill_output_res",)
    key = "waveshed/site_output_dir"
    other_key = "waveshed/p2p_output_dir"

    def test_run_saves_the_folder_it_uses(self):
        """A typed path without editingFinished (e.g. Run clicked straight
        from the field) is still remembered once a run starts with it."""
        with _fresh_store():
            tab = self._build()
            tab.line_output_dir.setText("/run/out")
            tab.sites_table = mock.MagicMock()
            tab.sites_table.rowCount.return_value = 1
            tab.alt_table = mock.MagicMock()
            tab.alt_table.rowCount.return_value = 1
            tab._read_sites = lambda: [{"coords": (46.9, 7.4)}]
            tab._selected_dem_layer = lambda: None
            tab._selected_terrain_dir = lambda: "/terrain"
            err = mock.MagicMock()
            # Stop the run at the engine check that follows the save.
            with mock.patch.object(sa_mod, "setup_warning", lambda: "missing"), \
                 mock.patch.object(sa_mod, "show_run_error", err):
                tab._on_run()
            err.assert_called_once()
            self.assertEqual(QgsSettings._store.get(self.key), "/run/out")


class TestP2PTab(_TabCase, unittest.TestCase):
    module = p2p_mod
    cls = P2PTab
    builder = "_build_analysis_group"
    key = "waveshed/p2p_output_dir"
    other_key = "waveshed/site_output_dir"

    def test_run_saves_the_folder_it_uses(self):
        with _fresh_store():
            tab = self._build()
            tab.line_output_dir.setText("/run/p2p")
            tab._worker = None
            tab._validate_inputs = lambda: None
            err = mock.MagicMock()
            with mock.patch.object(p2p_mod, "setup_warning", lambda: "missing"), \
                 mock.patch.object(p2p_mod, "show_run_error", err):
                tab._on_run()
            err.assert_called_once()
            self.assertEqual(QgsSettings._store.get(self.key), "/run/p2p")

    def test_failed_validation_saves_nothing(self):
        with _fresh_store():
            tab = self._build()
            tab.line_output_dir.setText("/never/used")
            tab._worker = None
            tab._validate_inputs = lambda: "Please set the Site A location."
            tab._on_run()
            self.assertNotIn(self.key, QgsSettings._store)


if __name__ == "__main__":
    unittest.main()
