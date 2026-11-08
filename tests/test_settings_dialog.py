"""Handler-orchestration tests for waveshed.gui.settings_dialog.

The dialog is constructed via ``__new__`` (bypassing the Qt-heavy ``__init__``)
and individual handlers are driven with small fake widgets. This covers these
behaviours in isolation:

* the "Save" button (``_validate_api_key``) persists a *valid* key, so a user
  who never clicks the dialog's OK button is still covered; and
* "Show machine fingerprint" (``_show_machine_fingerprint``) runs off the UI
  thread and toggles the button disabled -> enabled around the read; and
* "Clear Terrain Cache" (``_clear_cache``) keeps the folder index unless the
  confirmation's checkbox is ticked.

The QGIS/PyQt stubs from conftest make the module importable; the dialog's
layout widgets are never exercised. Under the stubs ``QThread.start()`` is a
no-op, so the worker's ``run()`` never fires — the completion slots are driven
directly to verify the button is always restored.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import os
import tempfile
import unittest
from unittest import mock

import waveshed.core.terrain_adapter as ta
import waveshed.gui.settings_dialog as sd
from waveshed.core import api_key as ak
from waveshed.core import binary_manager as bm
from waveshed.gui.settings_dialog import SettingsDialog

from test_api_key import _b58encode, _v3_raw


class _FakeLineEdit:
    def __init__(self, text: str = "") -> None:
        self._text = text

    def text(self) -> str:
        return self._text


class _FakeLabel:
    def __init__(self) -> None:
        self.text_value: str | None = None
        self.style: str | None = None

    def setText(self, t: str) -> None:
        self.text_value = t

    def setStyleSheet(self, s: str) -> None:
        self.style = s


class _FakeButton:
    def __init__(self) -> None:
        self.enabled = True
        self.text_value = "Show machine fingerprint"

    def setEnabled(self, v: bool) -> None:
        self.enabled = v

    def setText(self, t: str) -> None:
        self.text_value = t


def _new_dialog() -> SettingsDialog:
    """Uninitialized dialog — bypasses the Qt-heavy ``__init__``."""
    return SettingsDialog.__new__(SettingsDialog)


class TestSaveButtonPersists(unittest.TestCase):
    """The 'Save' button (``_validate_api_key``) must persist the key itself."""

    def setUp(self):
        ak.store_key("")

    def tearDown(self):
        ak.store_key("")

    def test_valid_key_is_persisted_normalized(self):
        key = _b58encode(_v3_raw())
        wrapped = f"{key[:40]} {key[40:]}"   # internal space (copy artefact)
        dlg = _new_dialog()
        dlg._api_key_edit = _FakeLineEdit(wrapped)
        dlg._api_status_label = _FakeLabel()

        dlg._validate_api_key()

        # Persisted on its own (no OK click), whitespace-free, reported saved.
        self.assertEqual(ak.get_stored_key(), key)
        self.assertNotIn(" ", ak.get_stored_key())
        self.assertIn("saved", (dlg._api_status_label.text_value or "").lower())

    def test_invalid_key_is_not_persisted(self):
        dlg = _new_dialog()
        dlg._api_key_edit = _FakeLineEdit("has_0_and_O_invalid")
        dlg._api_status_label = _FakeLabel()
        with mock.patch.object(ak, "store_key") as m_store:
            dlg._validate_api_key()
            m_store.assert_not_called()
        self.assertIn("Base58", dlg._api_status_label.text_value or "")


class TestFingerprintButtonToggle(unittest.TestCase):
    """Fingerprint read runs off-thread; the button toggles disabled->enabled."""

    def _dialog_with_button(self) -> SettingsDialog:
        dlg = _new_dialog()
        dlg._fingerprint_thread = None
        dlg._btn_fingerprint = _FakeButton()
        return dlg

    def test_start_disables_button_and_shows_reading(self):
        dlg = self._dialog_with_button()
        with mock.patch.object(bm, "discover_binary_dir", return_value="/bin"):
            dlg._show_machine_fingerprint()
        self.assertFalse(dlg._btn_fingerprint.enabled)
        self.assertEqual(dlg._btn_fingerprint.text_value, "Reading…")
        # A worker thread was created and kept referenced (alive during run).
        self.assertIsNotNone(dlg._fingerprint_thread)

    def test_success_slot_restores_button(self):
        dlg = self._dialog_with_button()
        dlg._btn_fingerprint.setEnabled(False)
        dlg._btn_fingerprint.setText("Reading…")
        dlg._on_fingerprint_ok("ab" * 32)
        self.assertTrue(dlg._btn_fingerprint.enabled)
        self.assertEqual(dlg._btn_fingerprint.text_value, "Show machine fingerprint")

    def test_error_slot_restores_button(self):
        dlg = self._dialog_with_button()
        dlg._btn_fingerprint.setEnabled(False)
        dlg._btn_fingerprint.setText("Reading…")
        dlg._on_fingerprint_err("engine too old for --fingerprint")
        self.assertTrue(dlg._btn_fingerprint.enabled)
        self.assertEqual(dlg._btn_fingerprint.text_value, "Show machine fingerprint")

    def test_not_installed_shows_info_without_touching_button(self):
        dlg = self._dialog_with_button()
        with mock.patch.object(bm, "discover_binary_dir", return_value=None):
            dlg._show_machine_fingerprint()
        self.assertTrue(dlg._btn_fingerprint.enabled)
        self.assertEqual(dlg._btn_fingerprint.text_value, "Show machine fingerprint")
        self.assertIsNone(dlg._fingerprint_thread)


# ---------------------------------------------------------------------------
# Settings → Clear Terrain Cache: the folder index is kept unless ticked
# ---------------------------------------------------------------------------

class _FakeCheckBox:
    def __init__(self, text):
        self.text = text
        self.checked = None

    def setChecked(self, value):
        self.checked = value

    def isChecked(self):
        return self.checked


class _FakeBox:
    """QMessageBox stand-in: the confirmation box (instance) and the result
    messages (static). ``click`` / ``tick`` say what the user does."""

    Icon = sd.QMessageBox.Icon
    StandardButton = sd.QMessageBox.StandardButton
    click = "yes"
    tick = False
    boxes: list = []
    results: list = []

    def __init__(self, parent=None):
        self.buttons = {}
        self.check = None
        self.initially_checked = None
        self.default = self.escape = self.clicked = None
        _FakeBox.boxes.append(self)

    def setIcon(self, icon):
        self.icon = icon

    def setWindowTitle(self, title):
        self.title = title

    def setText(self, text):
        self.text = text

    def setInformativeText(self, text):
        self.info = text

    def setCheckBox(self, box):
        self.check = box
        self.initially_checked = box.isChecked()

    def addButton(self, which):
        self.buttons[which] = button = object()
        return button

    def setDefaultButton(self, button):
        self.default = button

    def setEscapeButton(self, button):
        self.escape = button

    def exec(self):
        if _FakeBox.tick:
            self.check.setChecked(True)
        self.clicked = self.buttons[self.StandardButton.Yes if _FakeBox.click == "yes"
                                    else self.StandardButton.No]
        return 0

    def clickedButton(self):
        return self.clicked

    @staticmethod
    def information(parent, title, text):
        _FakeBox.results.append(("information", title, text))

    @staticmethod
    def warning(parent, title, text):
        _FakeBox.results.append(("warning", title, text))


class TestClearTerrainCache(unittest.TestCase):
    """The confirmation says the folder index is kept; the checkbox (off by
    default) deletes ``<cache>/index/*.json`` through ``clear_index``."""

    MD5 = ("0123456789abcdef0123456789abcdef", "fedcba9876543210fedcba9876543210")

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ws_clear_")
        self.cache = self._tmp.name
        _FakeBox.boxes, _FakeBox.results = [], []
        _FakeBox.click, _FakeBox.tick = "yes", False
        self._patches = [mock.patch.object(ta, "get_cache_dir", return_value=self.cache),
                         mock.patch.object(ta, "_log"),
                         mock.patch.object(sd, "QMessageBox", _FakeBox),
                         mock.patch.object(sd, "QCheckBox", _FakeCheckBox)]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    def put(self, *parts, size=10):
        path = os.path.join(self.cache, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"x" * size)
        return path

    def fill(self, tiles=True):
        tile = (self.put("pool", "a" * 32, "tile_N47.00E8.00_30m.abt", size=2_000_000)
                if tiles else None)
        idx = [self.put("index", m + ".json") for m in self.MD5]
        return tile, idx

    def test_the_confirmation_text_and_checkbox(self):
        self.assertEqual(sd.CLEAR_INDEX_LABEL,
                         "Also clear the folder index (the next scan reads every file again)")
        text, info = sd.clear_cache_texts(3, 120.4, 2, "/c")
        self.assertEqual(text, "Delete 3 cached terrain set(s), freeing 120 MB?")
        self.assertEqual(info, "/c\n\nTerrain will be re-downloaded or rebuilt on the next "
                               "run.\n\nThe folder index (per-folder file metadata, 2 index "
                               "file(s)) is kept: the next scan of an unchanged folder only "
                               "checks file sizes and dates.")
        self.fill()
        _FakeBox.click = "no"
        _new_dialog()._clear_cache()
        box = _FakeBox.boxes[0]
        self.assertEqual(box.title, "Clear Terrain Cache")
        self.assertEqual(box.text, "Delete 1 cached terrain set(s), freeing 2 MB?")
        self.assertIn("The folder index (per-folder file metadata, 2 index file(s)) is kept",
                      box.info)
        self.assertEqual(box.check.text, sd.CLEAR_INDEX_LABEL)
        self.assertIs(box.initially_checked, False, "unticked by default")
        self.assertIs(box.default, box.buttons[_FakeBox.StandardButton.No])
        self.assertIs(box.escape, box.buttons[_FakeBox.StandardButton.No])

    def test_yes_unticked_keeps_the_index(self):
        tile, idx = self.fill()
        with mock.patch.object(ta, "clear_index", wraps=ta.clear_index) as clear_index:
            _new_dialog()._clear_cache()
        clear_index.assert_not_called()
        self.assertFalse(os.path.exists(tile))
        self.assertTrue(all(os.path.exists(p) for p in idx))
        self.assertEqual(_FakeBox.results, [
            ("information", "Clear Terrain Cache",
             "Removed 1 cached terrain set(s), freeing 2 MB.")])

    def test_yes_ticked_clears_the_index_too(self):
        tile, idx = self.fill()
        _FakeBox.tick = True
        _new_dialog()._clear_cache()
        self.assertFalse(os.path.exists(tile))
        self.assertFalse(any(os.path.exists(p) for p in idx))
        self.assertEqual(_FakeBox.results, [
            ("information", "Clear Terrain Cache",
             "Removed 1 cached terrain set(s), freeing 2 MB, and 2 index file(s).")])

    def test_no_deletes_nothing(self):
        tile, idx = self.fill()
        _FakeBox.click, _FakeBox.tick = "no", True
        _new_dialog()._clear_cache()
        self.assertTrue(all(os.path.exists(p) for p in [tile] + idx))
        self.assertEqual(_FakeBox.results, [])

    def test_only_an_index_left_can_still_be_cleared(self):
        _tile, idx = self.fill(tiles=False)
        _FakeBox.tick = True
        _new_dialog()._clear_cache()
        self.assertEqual(_FakeBox.boxes[0].text, "No cached terrain tiles to delete.")
        self.assertFalse(any(os.path.exists(p) for p in idx))
        self.assertEqual(_FakeBox.results, [
            ("information", "Clear Terrain Cache", "Removed 2 index file(s).")])

    def test_nothing_there_asks_nothing(self):
        _new_dialog()._clear_cache()
        self.assertEqual(_FakeBox.boxes, [])
        self.assertEqual(_FakeBox.results, [
            ("information", "Clear Terrain Cache",
             f"No cached terrain found in\n{self.cache}")])

    def test_an_index_file_that_cannot_be_removed_is_reported(self):
        _tile, idx = self.fill()
        _FakeBox.tick = True
        real_remove = os.remove

        def remove(path, *a, **kw):
            if path == idx[0]:
                raise PermissionError(13, "in use", path)
            return real_remove(path, *a, **kw)
        with mock.patch.object(ta.os, "remove", side_effect=remove):
            _new_dialog()._clear_cache()
        kind, _title, text = _FakeBox.results[0]
        self.assertEqual(kind, "warning")
        self.assertTrue(text.startswith("Removed 1 cached terrain set(s), freeing 2 MB, "
                                        "and 1 of 2 index file(s).\nThe rest could not be "
                                        "deleted"), text)


if __name__ == "__main__":
    unittest.main()
