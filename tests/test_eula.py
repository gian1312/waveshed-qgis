"""Pins the engine-EULA presentation: key points, the not-exhaustive sentence,
the non-commercial grant, version tracking and the consent dialog wiring.

The canonical EULA text lives on the website
(AETHER_Web/web/static/legal/aether-engine-eula.md); the plugin owns only the
key-points summary shown above it in the download consent dialog.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import unittest
from unittest import mock

from waveshed.core import eula
from waveshed.gui import settings_dialog as sd


def _points() -> str:
    return " ".join(eula.KEY_POINTS).lower()


class TestKeyPoints(unittest.TestCase):
    def test_non_commercial_grant(self):
        first = eula.KEY_POINTS[0].lower()
        self.assertIn("non-commercial use only", first)
        for word in ("private", "hobby", "education", "research", "individual"):
            self.assertIn(word, first)

    def test_commercial_governmental_organisational_need_written_permission(self):
        p = _points()
        for word in ("commercial", "governmental", "organisational",
                     "public bodies", "ngos", "associations",
                     "prior written permission", eula.CONTACT_EMAIL):
            self.assertIn(word, p)

    def test_model_output_and_reliance(self):
        p = _points()
        self.assertIn("propagation model", p)
        self.assertIn("not measurements", p)
        for word in ("safety-of-life", "regulatory", "planning", "financial",
                     "independent verification"):
            self.assertIn(word, p)

    def test_no_warranty_no_liability(self):
        p = _points()
        self.assertIn("no warranty", p)
        self.assertIn("no liability", p)
        self.assertIn("maximum extent permitted by law", p)

    def test_not_exhaustive_sentence(self):
        self.assertEqual(
            eula.NOT_EXHAUSTIVE,
            "This summary is not exhaustive. The full EULA text below governs; "
            "where this summary and the full text differ, the full text applies.",
        )

    def test_html_carries_every_point_and_the_sentence(self):
        html = eula.key_points_html()
        self.assertIn("Key points", html)
        self.assertIn(eula.NOT_EXHAUSTIVE, html)
        self.assertEqual(html.count("<li>"), len(eula.KEY_POINTS))


class TestVersion(unittest.TestCase):
    def test_parse_version_line(self):
        text = "# EULA\n\n**Version:** 2026-09-23 (draft) · **Last updated:** x"
        self.assertEqual(eula.parse_eula_version(text), "2026-09-23")
        self.assertIsNone(eula.parse_eula_version("no version here"))

    def test_record_and_read_acceptance(self):
        eula.record_acceptance("2026-09-23")
        self.assertEqual(eula.accepted_version(), "2026-09-23")


# --- consent dialog -------------------------------------------------------

class _Rec:
    """Records every widget the consent dialog builds."""

    labels: list = []
    browsers: list = []


class _FakeLabel:
    def __init__(self, text: str = "", *a) -> None:
        self.text = text
        _Rec.labels.append(self)

    def __getattr__(self, name):
        return lambda *a, **kw: None


class _FakeBrowser:
    def __init__(self, *a) -> None:
        self.markdown = None
        _Rec.browsers.append(self)

    def setMarkdown(self, text: str) -> None:
        self.markdown = text

    def __getattr__(self, name):
        return lambda *a, **kw: None


class _FakeDialog:
    DialogCode = sd.QDialog.DialogCode
    result = 1

    def __init__(self, *a) -> None:
        pass

    def exec(self) -> int:
        return _FakeDialog.result

    def __getattr__(self, name):
        return lambda *a, **kw: None


class _Anything:
    def __init__(self, *a, **kw) -> None:
        self.accepted = mock.MagicMock()
        self.rejected = mock.MagicMock()

    def __getattr__(self, name):
        return lambda *a, **kw: None


EULA_TEXT = "# Aether Engine EULA\n\n**Version:** {v} (draft)\n\nFull text."


class TestConsentDialog(unittest.TestCase):
    def setUp(self):
        _Rec.labels, _Rec.browsers = [], []
        _FakeDialog.result = 1
        self._patches = [
            mock.patch.object(sd, "QDialog", _FakeDialog),
            mock.patch.object(sd, "QLabel", _FakeLabel),
            mock.patch.object(sd, "QTextBrowser", _FakeBrowser),
            mock.patch.object(sd, "QVBoxLayout", _Anything),
            mock.patch.object(sd, "QDialogButtonBox", type(
                "BB", (_Anything,), {"ButtonRole": sd.QDialogButtonBox.ButtonRole})),
        ]
        for p in self._patches:
            p.start()
        self.dlg = sd.SettingsDialog.__new__(sd.SettingsDialog)

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def _texts(self) -> str:
        return "\n".join(str(lbl.text) for lbl in _Rec.labels)

    def test_key_points_on_top_and_full_text_below(self):
        text = EULA_TEXT.format(v=eula.SUMMARY_EULA_VERSION)
        ok = self.dlg._confirm_engine_download("0.4.3", eula.EULA_URL, text)
        self.assertTrue(ok)
        self.assertIn("Key points", _Rec.labels[1].text)   # right after the intro
        self.assertIn(eula.NOT_EXHAUSTIVE, _Rec.labels[1].text)
        self.assertEqual(_Rec.browsers[0].markdown, text)
        self.assertNotIn("summary was written for", self._texts())
        self.assertEqual(eula.accepted_version(), eula.SUMMARY_EULA_VERSION)

    def test_version_mismatch_is_flagged(self):
        self.dlg._confirm_engine_download("0.4.3", eula.EULA_URL, EULA_TEXT.format(v="2099-01-01"))
        self.assertIn("full text below is version 2099-01-01 and governs", self._texts())
        self.assertEqual(eula.accepted_version(), "2099-01-01")

    def test_unloaded_text_falls_back_to_link(self):
        self.dlg._confirm_engine_download("0.4.3", eula.EULA_URL, "")
        self.assertEqual(_Rec.browsers, [])
        texts = self._texts()
        self.assertIn("could not be loaded", texts)
        self.assertIn(eula.EULA_URL, texts)
        self.assertIn(eula.NOT_EXHAUSTIVE, texts)

    def test_cancel_records_nothing(self):
        eula.record_acceptance("before")
        _FakeDialog.result = 0
        self.assertFalse(
            self.dlg._confirm_engine_download("0.4.3", eula.EULA_URL, "")
        )
        self.assertEqual(eula.accepted_version(), "before")


if __name__ == "__main__":
    unittest.main()
