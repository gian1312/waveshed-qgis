"""Handler-orchestration tests for waveshed.gui.settings_dialog.

The dialog is constructed via ``__new__`` (bypassing the Qt-heavy ``__init__``)
and individual handlers are driven with small fake widgets. This covers two
behaviours in isolation:

* the "Save" button (``_validate_api_key``) persists a *valid* key, so a user
  who never clicks the dialog's OK button is still covered; and
* "Show machine fingerprint" (``_show_machine_fingerprint``) runs off the UI
  thread and toggles the button disabled -> enabled around the read.

The QGIS/PyQt stubs from conftest make the module importable; the dialog's
layout widgets are never exercised. Under the stubs ``QThread.start()`` is a
no-op, so the worker's ``run()`` never fires — the completion slots are driven
directly to verify the button is always restored.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import unittest
from unittest import mock

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


if __name__ == "__main__":
    unittest.main()
