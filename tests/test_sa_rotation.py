"""360° tab: a row's AZ Rotation is enabled only when its asset has an
antenna pattern (S3, 2026-11-14).

The rotation reaches the job only together with a pattern
(job_builder._build_tx), so on a blank or pattern-less asset the spin box
was a dead input. It now follows the row's asset, the store refresh, a
rename and the mode switch. QGIS/Qt are stubbed by conftest.py; the real
widgets are driven in .tmp/prop_tab2/S3/drive_s3.py.
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401

import inspect
import unittest

from qgis.PyQt import QtWidgets

from waveshed.gui.site_analysis_tab import (
    AZ_ROTATION_OFF_TIP, AZ_ROTATION_TIP, SiteAnalysisTab, _COL_ASSET,
    _COL_AZ_ROTATION, asset_has_pattern,
)

AZ = [[0, 1.0], [180, 0.1]]
ASSETS = {
    "PatAz": {"azimuth_pattern": {"data": AZ}, "elevation_pattern": {"data": []}},
    "PatEl": {"azimuth_pattern": {}, "elevation_pattern": {"data": [[0, 1.0]]}},
    "NoPat": {"azimuth_pattern": {"data": []}, "elevation_pattern": {"data": []}},
    "Bare": {},
}


class _Combo(QtWidgets.QComboBox):
    def __init__(self, text=""):
        self.text = text

    def currentText(self):
        return self.text


class _Spin:
    def __init__(self):
        self.enabled, self.tip = True, ""

    def setEnabled(self, v):
        self.enabled = v

    def setToolTip(self, t):
        self.tip = t


class _Table:
    def __init__(self, names):
        self.rows = [{_COL_ASSET: _Combo(n), _COL_AZ_ROTATION: _Spin()} for n in names]

    def rowCount(self):
        return len(self.rows)

    def cellWidget(self, row, col):
        return self.rows[row].get(col)


def _tab(names, mode="LOSS"):
    tab = SiteAnalysisTab.__new__(SiteAnalysisTab)
    tab._asset_cache = dict(ASSETS)
    tab._current_mode = mode
    tab.sites_table = _Table(names)
    return tab


def _states(tab):
    return [tab.sites_table.cellWidget(r, _COL_AZ_ROTATION).enabled
            for r in range(tab.sites_table.rowCount())]


class AssetHasPattern(unittest.TestCase):

    def test_either_axis_counts_and_empty_data_does_not(self):
        self.assertTrue(asset_has_pattern(ASSETS["PatAz"]))
        self.assertTrue(asset_has_pattern(ASSETS["PatEl"]))
        self.assertFalse(asset_has_pattern(ASSETS["NoPat"]))
        self.assertFalse(asset_has_pattern(ASSETS["Bare"]))
        self.assertFalse(asset_has_pattern(None))


class RowState(unittest.TestCase):

    def test_blank_patternless_unresolved_and_patterned_rows(self):
        tab = _tab(["", "NoPat", "Bare", "Gone", "PatAz", "PatEl"])
        tab._update_az_rotation_states()
        self.assertEqual(_states(tab), [False, False, False, False, True, True])
        self.assertEqual(tab.sites_table.cellWidget(0, _COL_AZ_ROTATION).tip,
                         AZ_ROTATION_OFF_TIP)
        self.assertEqual(tab.sites_table.cellWidget(4, _COL_AZ_ROTATION).tip,
                         AZ_ROTATION_TIP)

    def test_geometric_modes_disable_every_row(self):
        for mode in ("LOS", "MIN_ALT"):
            tab = _tab(["PatAz"], mode)
            tab._update_az_rotation_states()
            self.assertEqual(_states(tab), [False], mode)

    def test_asset_change_follows(self):
        tab = _tab(["NoPat"])
        tab._update_az_rotation_states()
        tab.sites_table.cellWidget(0, _COL_ASSET).text = "PatAz"
        tab._on_asset_changed(0, "PatAz")  # height auto-fill hits stub widgets
        self.assertEqual(_states(tab), [True])
        tab.sites_table.cellWidget(0, _COL_ASSET).text = ""
        tab._on_asset_changed(0, "")
        self.assertEqual(_states(tab), [False])

    def test_store_refresh_rename_and_mode_switch_recompute(self):
        for call in ("refresh_assets", "rename_asset", "set_mode"):
            src = getattr(SiteAnalysisTab, call)
            self.assertIn("_update_az_rotation_states", inspect.getsource(src), call)

    def test_rename_relabels_then_recomputes(self):
        tab = _tab(["PatAz"])
        tab.sites_table.rows[0][_COL_ASSET] = _RenamableCombo("PatAz")
        tab._update_az_rotation_states()
        tab.rename_asset("PatAz", "Renamed")
        self.assertEqual(tab.sites_table.cellWidget(0, _COL_ASSET).currentText(), "Renamed")
        self.assertEqual(_states(tab), [True], "rename lost the pattern state")


class _RenamableCombo(_Combo):
    def findText(self, text):
        return 0 if text == self.text else -1

    def blockSignals(self, v):
        return False

    def setItemText(self, idx, text):
        self.text = text


if __name__ == "__main__":
    unittest.main()
