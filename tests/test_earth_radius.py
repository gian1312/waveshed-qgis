"""Earth Radius Mode: Real (k = 1) / 4/3, default by mode family (owner,
2026-11-15: "default 1 in los, default 4/3 in rf mode").

Pins: the two choices (ADVANCED behind ``EARTH_RADIUS_ADVANCED_ENABLED``),
the default per family (LOS / LOS Floor → REAL, Propagation → FOUR_THIRDS),
the user's pick kept per family, the engine gate (``EARTH_REAL_MIN_ENGINE``:
an older or unknown engine greys Real out and selects 4/3), and the
``propagation.earth_radius_mode`` the 360° and P2P tabs put in the job.

Logic tests on the conftest stubs with a combo stand-in that has an item
model; the real dialog was driven separately (PyQGIS offscreen).
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import unittest
from unittest import mock

from waveshed.core import binary_manager as bm
from waveshed.core.job_builder import build_coverage_job, build_p2p_job
from waveshed.gui import itm_params
import waveshed.gui.p2p_tab as p2p_mod
import waveshed.gui.site_analysis_tab as site_mod

from test_patterns_engine import DIRECTIONAL, OMNI, _p2p_tab, _site_tab

NEW = "0.5.2"
OLD = "0.5.1"


_QT = mock.patch.object(itm_params, "Qt", mock.Mock())  # stub has no ToolTipRole


def setUpModule():
    _QT.start()


def tearDownModule():
    _QT.stop()


class _Item:
    def __init__(self):
        self.enabled = True

    def setEnabled(self, on):
        self.enabled = on


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, fn):
        self.slots.append(fn)


class _Combo:
    """QComboBox stand-in with an item model (what the availability code
    needs) and ``pick`` = the user choosing an item."""

    def __init__(self):
        self.items = []  # [label, data, _Item, tip]
        self.index = -1
        self.props = {}
        self.activated = _Signal()

    def addItem(self, label, data):
        self.items.append([label, data, _Item(), ""])
        if self.index < 0:
            self.index = 0

    def count(self):
        return len(self.items)

    def model(self):
        return self

    def item(self, i):
        return self.items[i][2]

    def itemData(self, i, role=None):
        return self.items[i][1]

    def setItemData(self, i, value, role=None):
        self.items[i][3] = value

    def findData(self, data):
        for i, it in enumerate(self.items):
            if it[1] == data:
                return i
        return -1

    def currentIndex(self):
        return self.index

    def setCurrentIndex(self, i):
        self.index = i

    def currentData(self):
        return self.items[self.index][1]

    def currentText(self):
        return self.items[self.index][0]

    def setToolTip(self, tip):
        self.tip = tip

    def property(self, name):
        return self.props.get(name)

    def setProperty(self, name, value):
        self.props[name] = value

    def values(self):
        return [it[1] for it in self.items]

    def enabled(self, value):
        return self.items[self.findData(value)][2].enabled

    def pick(self, value):
        self.setCurrentIndex(self.findData(value))
        for fn in self.activated.slots:
            fn(self.index)


def _combo():
    combo = _Combo()
    itm_params.setup_earth_radius_combo(combo)
    return combo


def _apply(combo, mode, version=NEW, model="ITM", backend="AUTO"):
    itm_params.apply_earth_radius_availability(
        combo, mode, model if mode == "LOSS" else "", backend, version)
    return itm_params.earth_radius_value(combo)


class Choices(unittest.TestCase):

    def test_two_choices_no_advanced(self):
        combo = _combo()
        self.assertEqual(combo.values(), ["REAL", "FOUR_THIRDS"])
        self.assertEqual([it[0] for it in combo.items], ["Real (k = 1)", "4/3 earth"])

    def test_advanced_behind_the_flag(self):
        with mock.patch.object(itm_params, "EARTH_RADIUS_ADVANCED_ENABLED", True):
            self.assertEqual(_combo().values(), ["REAL", "FOUR_THIRDS", "ADVANCED"])

    def test_a_stored_advanced_never_reaches_a_job(self):
        combo = _combo()
        combo.props["waveshed_wanted_LOS"] = "ADVANCED"
        combo.props["waveshed_wanted_RF"] = "ADVANCED"
        self.assertEqual(_apply(combo, "LOS"), "REAL")
        self.assertEqual(_apply(combo, "LOSS"), "FOUR_THIRDS")
        # A stand-in combo whose text is ADVANCED (no item data).
        for family, want in (("LOS", "REAL"), ("RF", "FOUR_THIRDS")):
            stub = mock.Mock(currentData=mock.Mock(return_value=None),
                             currentText=mock.Mock(return_value="ADVANCED"),
                             property=mock.Mock(return_value=family))
            self.assertEqual(itm_params.earth_radius_value(stub), want)

    def test_engine_gate_constant(self):
        self.assertEqual(bm.EARTH_REAL_MIN_ENGINE, "0.5.2")
        self.assertTrue(bm.engine_supports_real_earth("0.5.2"))
        self.assertTrue(bm.engine_supports_real_earth("0.6.0"))
        self.assertFalse(bm.engine_supports_real_earth("0.5.1"))
        self.assertFalse(bm.engine_supports_real_earth(None))


class DefaultsAndMemory(unittest.TestCase):

    def test_defaults_per_family(self):
        combo = _combo()
        self.assertEqual(_apply(combo, "LOS"), "REAL")
        self.assertEqual(_apply(combo, "MIN_ALT"), "REAL")
        self.assertEqual(_apply(combo, "LOSS", model="SIMPLE_LOSS"), "FOUR_THIRDS")
        self.assertEqual(_apply(combo, "LOSS", model="ITM"), "FOUR_THIRDS")
        self.assertEqual(_apply(combo, "LOSS", model="ITM", backend="CPU"), "FOUR_THIRDS")

    def test_pick_is_kept_per_family(self):
        combo = _combo()
        _apply(combo, "LOS")
        combo.pick("FOUR_THIRDS")                      # LOS: user picks 4/3
        self.assertEqual(_apply(combo, "LOSS"), "FOUR_THIRDS")  # RF default
        combo.pick("REAL")                             # RF: user picks Real
        self.assertEqual(_apply(combo, "LOS"), "FOUR_THIRDS")   # LOS pick back
        self.assertEqual(_apply(combo, "MIN_ALT"), "FOUR_THIRDS")  # same family
        self.assertEqual(_apply(combo, "LOSS", model="SIMPLE_LOSS"), "REAL")
        self.assertEqual(_apply(combo, "LOS"), "FOUR_THIRDS")

    def test_backend_change_keeps_the_pick(self):
        combo = _combo()
        _apply(combo, "LOSS")
        combo.pick("REAL")
        self.assertEqual(_apply(combo, "LOSS", backend="CPU"), "REAL")
        self.assertEqual(_apply(combo, "LOSS", backend="GPU"), "REAL")


class EngineGate(unittest.TestCase):

    def test_old_or_unknown_engine_greys_real_and_selects_four_thirds(self):
        for version in (OLD, None):
            combo = _combo()
            self.assertEqual(_apply(combo, "LOS", version=version), "FOUR_THIRDS")
            self.assertFalse(combo.enabled("REAL"))
            self.assertTrue(combo.enabled("FOUR_THIRDS"))
            tip = combo.items[combo.findData("REAL")][3]
            self.assertIn("0.5.2 or newer", tip)

    def test_a_real_pick_returns_with_a_new_engine(self):
        combo = _combo()
        _apply(combo, "LOSS")
        combo.pick("REAL")
        self.assertEqual(_apply(combo, "LOSS", version=OLD), "FOUR_THIRDS")
        self.assertEqual(_apply(combo, "LOSS", version=NEW), "REAL")
        self.assertTrue(combo.enabled("REAL"))

    def test_no_planned_wording(self):
        self.assertNotIn("planned", itm_params.EARTH_RADIUS_TIP)
        for tip in itm_params.earth_radius_unavailable("LOS", "", "AUTO", OLD).values():
            self.assertNotIn("planned", tip)


class JobValue(unittest.TestCase):
    """``propagation.earth_radius_mode`` in the job a tab builds."""

    def _site(self, mode, version, pick=None):
        tab = _site_tab(mode=mode)
        tab.combo_earth_radius = _combo()
        tab._current_mode = mode
        with mock.patch.object(site_mod, "read_engine_version", return_value=version):
            tab._update_earth_radius()
        if pick:
            tab.combo_earth_radius.pick(pick)
        jobs = tab._build_jobs()
        self.assertTrue(jobs)
        return {build_coverage_job(p, "/abt", "/out")["propagation"]["earth_radius_mode"]
                for p, _ in jobs}

    def test_site_los_new_engine_real(self):
        self.assertEqual(self._site("LOS", NEW), {"REAL"})
        self.assertEqual(self._site("MIN_ALT", NEW), {"REAL"})

    def test_site_los_old_engine_four_thirds(self):
        self.assertEqual(self._site("LOS", OLD), {"FOUR_THIRDS"})

    def test_site_rf_default_four_thirds_and_real_pick(self):
        self.assertEqual(self._site("LOSS", NEW), {"FOUR_THIRDS"})
        self.assertEqual(self._site("LOSS", NEW, pick="REAL"), {"REAL"})

    def _p2p(self, mode, version, pick=None):
        tab = _p2p_tab(mode=mode)
        tab.combo_earth_radius = _combo()
        tab._is_loss_mode = mode == "LOSS"
        with mock.patch.object(p2p_mod, "read_engine_version", return_value=version):
            tab._update_earth_radius()
        if pick:
            tab.combo_earth_radius.pick(pick)
        with mock.patch.object(p2p_mod, "list_assets",
                               return_value=[dict(DIRECTIONAL, _path="/s/d.json"),
                                             dict(OMNI, _path="/s/o.json")]):
            params = tab._collect_params()
        return build_p2p_job(params, "/abt", "/out", "/b.csv")["propagation"][
            "earth_radius_mode"]

    def test_p2p(self):
        self.assertEqual(self._p2p("LOS", NEW), "REAL")
        self.assertEqual(self._p2p("LOS", OLD), "FOUR_THIRDS")
        self.assertEqual(self._p2p("LOSS", NEW), "FOUR_THIRDS")
        self.assertEqual(self._p2p("LOSS", NEW, pick="REAL"), "REAL")


if __name__ == "__main__":
    unittest.main()
