"""The 1 m antenna floor everywhere outside ``core.job_builder`` itself.

``job_builder`` holds the authoritative rejection (covered by
``test_model_validity.py``); this file covers the paths that reach the engine
without passing a spinbox:

* the P2P Processing algorithm's batch CSV. When ``BATCH_FILE`` is supplied the
  algorithm's TX/RX parameters are ignored entirely and the CSV drives every
  link, so the bounds declared in ``initAlgorithm`` never see those values —
  ``_parse_csv`` is the only gate a batch run passes through.
* the equivalent GUI parser, ``gui.p2p_tab._parse_batch_csv``, so the two
  cannot drift apart.
* asset JSON, which can be hand-edited to carry any default height.

The AGL/AMSL split is the thing most at risk of regressing: an AMSL altitude is
an absolute elevation and is legitimately zero or negative, so every case below
that floors an AGL value has a negative-AMSL twin that must survive.

QGIS stubs provided by conftest.py.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import json
import os
import tempfile
import unittest

from qgis.core import QgsProcessingException

from waveshed.algorithms.p2p import P2PAlgorithm
from waveshed.core.asset_manager import default_height_error, load_asset
from waveshed.core.job_builder import MIN_ANTENNA_AGL_M
from waveshed.gui.p2p_tab import _parse_batch_csv

_HEADER = "# type,id,lat,lon,alt,mode\n"


class _CsvCase(unittest.TestCase):
    """Writes CSV bodies to a temp file and parses them both ways."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)

    def _write(self, body: str) -> str:
        path = os.path.join(self._dir.name, "batch.csv")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(body)
        return path

    def parse_alg(self, body: str):
        return P2PAlgorithm._parse_csv(self._write(body))

    def parse_gui(self, body: str):
        return _parse_batch_csv(self._write(body))


class TestAltitudeFloor(_CsvCase):
    def test_sub_floor_agl_source_is_rejected(self):
        body = _HEADER + "S,TX,47.0,8.0,0.5,AGL\nR,RX,47.1,8.1,1.5,AGL\n"
        with self.assertRaises(QgsProcessingException) as ctx:
            self.parse_alg(body)
        message = str(ctx.exception)
        self.assertIn("line 2", message)
        self.assertIn(f"{MIN_ANTENNA_AGL_M:.1f} m", message)

    def test_sub_floor_agl_target_is_rejected(self):
        body = _HEADER + "S,TX,47.0,8.0,30.0,AGL\nR,RX,47.1,8.1,0.0,AGL\n"
        with self.assertRaises(QgsProcessingException) as ctx:
            self.parse_alg(body)
        self.assertIn("line 3", str(ctx.exception))

    def test_exactly_the_floor_is_accepted(self):
        body = _HEADER + (
            f"S,TX,47.0,8.0,{MIN_ANTENNA_AGL_M:.1f},AGL\n"
            f"R,RX,47.1,8.1,{MIN_ANTENNA_AGL_M:.1f},AGL\n"
        )
        entries = self.parse_alg(body)
        self.assertEqual([MIN_ANTENNA_AGL_M, MIN_ANTENNA_AGL_M],
                         [e[4] for e in entries])

    def test_negative_amsl_is_accepted(self):
        # Dead Sea shore and Schiphol: below sea level is a real place, and an
        # AMSL altitude is an absolute elevation, never a height above ground.
        body = _HEADER + "S,TX,31.5,35.5,-430.0,AMSL\nR,RX,52.3,4.76,-4.0,AMSL\n"
        entries = self.parse_alg(body)
        self.assertEqual([-430.0, -4.0], [e[4] for e in entries])
        self.assertEqual(["AMSL", "AMSL"], [e[5] for e in entries])

    def test_zero_amsl_is_accepted(self):
        body = _HEADER + "S,TX,47.0,8.0,0.0,AMSL\nR,RX,47.1,8.1,0.0,AMSL\n"
        self.assertEqual([0.0, 0.0], [e[4] for e in self.parse_alg(body)])

    def test_mixed_modes_only_floor_the_agl_row(self):
        body = _HEADER + "S,TX,31.5,35.5,-430.0,AMSL\nR,RX,47.1,8.1,0.2,AGL\n"
        with self.assertRaises(QgsProcessingException) as ctx:
            self.parse_alg(body)
        self.assertIn("line 3", str(ctx.exception))


class TestModeValidation(_CsvCase):
    def test_unknown_mode_is_rejected(self):
        body = _HEADER + "S,TX,47.0,8.0,30.0,ASL\nR,RX,47.1,8.1,1.5,AGL\n"
        with self.assertRaises(QgsProcessingException) as ctx:
            self.parse_alg(body)
        self.assertIn("AGL or AMSL", str(ctx.exception))

    def test_empty_mode_is_rejected(self):
        body = _HEADER + "S,TX,47.0,8.0,30.0,\nR,RX,47.1,8.1,1.5,AGL\n"
        with self.assertRaises(QgsProcessingException):
            self.parse_alg(body)

    def test_lowercase_mode_is_normalised(self):
        body = _HEADER + "S,TX,47.0,8.0,30.0,agl\nR,RX,47.1,8.1,1.5,amsl\n"
        self.assertEqual(["AGL", "AMSL"], [e[5] for e in self.parse_alg(body)])


class TestStructuralValidation(_CsvCase):
    def test_short_row_is_rejected(self):
        with self.assertRaises(QgsProcessingException):
            self.parse_alg(_HEADER + "S,TX,47.0,8.0,30.0\n")

    def test_unknown_row_type_is_rejected(self):
        with self.assertRaises(QgsProcessingException):
            self.parse_alg(_HEADER + "X,TX,47.0,8.0,30.0,AGL\n")

    def test_non_numeric_altitude_is_rejected(self):
        # Previously a bare ValueError escaped as an unhandled traceback.
        with self.assertRaises(QgsProcessingException) as ctx:
            self.parse_alg(_HEADER + "S,TX,47.0,8.0,high,AGL\n")
        self.assertIn("altitude", str(ctx.exception))

    def test_comments_and_blank_lines_are_skipped(self):
        body = _HEADER + "\nS,TX,47.0,8.0,30.0,AGL\n# trailing note\n"
        self.assertEqual(1, len(self.parse_alg(body)))


class TestGuiParserAgrees(_CsvCase):
    """gui.p2p_tab._parse_batch_csv must reject exactly the same rows."""

    def test_sub_floor_agl_is_rejected(self):
        body = _HEADER + "S,TX,47.0,8.0,0.5,AGL\nR,RX,47.1,8.1,1.5,AGL\n"
        with self.assertRaises(ValueError) as ctx:
            self.parse_gui(body)
        self.assertIn(f"{MIN_ANTENNA_AGL_M:.1f} m", str(ctx.exception))

    def test_negative_amsl_is_accepted(self):
        body = _HEADER + "S,TX,31.5,35.5,-430.0,AMSL\nR,RX,52.3,4.76,-4.0,AMSL\n"
        self.assertEqual([-430.0, -4.0], [e[4] for e in self.parse_gui(body)])

    def test_the_two_parsers_agree_on_a_clean_file(self):
        body = _HEADER + "S,TX,47.0,8.0,30.0,AGL\nR,RX,47.1,8.1,-4.0,AMSL\n"
        self.assertEqual(self.parse_gui(body), self.parse_alg(body))


class TestAssetDefaultHeight(unittest.TestCase):
    """A hand-edited asset JSON can carry any default height at all."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)

    def _asset_file(self, **fields) -> str:
        asset = {
            "name": "Hand edited",
            "frequency_mhz": 433.0,
            "peak_power_watts": 10.0,
            "antenna_gain_dbi": 0.0,
        }
        asset.update(fields)
        path = os.path.join(self._dir.name, "asset.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(asset, fh)
        return path

    def test_error_names_the_field_and_the_floor(self):
        message = default_height_error(0.3, "AGL")
        self.assertIsNotNone(message)
        self.assertIn("default_height_m", message)
        self.assertIn(f"{MIN_ANTENNA_AGL_M:.1f} m", message)

    def test_agl_at_the_floor_is_accepted(self):
        self.assertIsNone(default_height_error(MIN_ANTENNA_AGL_M, "AGL"))

    def test_amsl_below_sea_level_is_accepted(self):
        self.assertIsNone(default_height_error(-430.0, "AMSL"))

    def test_a_sub_floor_agl_asset_is_raised_to_the_floor_on_load(self):
        # Raised rather than raised-as-an-exception on purpose: list_assets
        # catches only JSON/OS errors, so throwing here would make the asset
        # vanish from the picker with no explanation. The job builder still
        # rejects anything that reaches it.
        path = self._asset_file(default_height_m=0.3, default_height_mode="AGL")
        self.assertEqual(MIN_ANTENNA_AGL_M, load_asset(path)["default_height_m"])

    def test_a_negative_amsl_asset_loads_untouched(self):
        path = self._asset_file(default_height_m=-4.0, default_height_mode="AMSL")
        self.assertEqual(-4.0, load_asset(path)["default_height_m"])

    def test_a_normal_asset_loads_untouched(self):
        path = self._asset_file(default_height_m=25.0, default_height_mode="AGL")
        self.assertEqual(25.0, load_asset(path)["default_height_m"])

    def test_an_old_asset_without_the_field_gets_the_default(self):
        asset = load_asset(self._asset_file())
        self.assertEqual(30.0, asset["default_height_m"])
        self.assertEqual("AGL", asset["default_height_mode"])


class _FakeSignal:
    def __init__(self):
        self._slots = []

    def connect(self, fn):
        self._slots.append(fn)

    def emit(self, *args):
        for fn in self._slots:
            fn(*args)


class _FakeCombo:
    """Just enough QComboBox to drive bind_height_mode."""

    def __init__(self, text="AGL"):
        self._text = text
        self.currentTextChanged = _FakeSignal()

    def currentText(self):
        return self._text

    def setCurrentText(self, text):
        self._text = text
        self.currentTextChanged.emit(text)


class _FakeSpin:
    """Records setMinimum; conftest's QDoubleSpinBox stub has no range at all."""

    def __init__(self):
        self.minimum = None

    def setMinimum(self, value):
        self.minimum = value


class TestModeDependentMinimum(unittest.TestCase):
    """gui.height_inputs.bind_height_mode.

    Tests the helper's own logic with a purpose-built pair of fakes. Real Qt
    clamping cannot be exercised here — conftest stubs QDoubleSpinBox as an
    inert mock with no range semantics — so this asserts which minimum is
    requested, not what Qt does with it.
    """

    def _bound(self, mode):
        from waveshed.gui.height_inputs import bind_height_mode

        spin, combo = _FakeSpin(), _FakeCombo(mode)
        bind_height_mode(spin, combo)
        return spin, combo

    def test_agl_gets_the_floor_immediately(self):
        spin, _ = self._bound("AGL")
        self.assertEqual(MIN_ANTENNA_AGL_M, spin.minimum)

    def test_amsl_opens_below_sea_level_immediately(self):
        from waveshed.gui.height_inputs import MIN_AMSL_M

        spin, _ = self._bound("AMSL")
        self.assertEqual(MIN_AMSL_M, spin.minimum)

    def test_switching_to_amsl_lowers_the_minimum(self):
        from waveshed.gui.height_inputs import MIN_AMSL_M

        spin, combo = self._bound("AGL")
        combo.setCurrentText("AMSL")
        self.assertEqual(MIN_AMSL_M, spin.minimum)

    def test_switching_back_to_agl_restores_the_floor(self):
        spin, combo = self._bound("AMSL")
        combo.setCurrentText("AGL")
        self.assertEqual(MIN_ANTENNA_AGL_M, spin.minimum)

    def test_amsl_range_clears_the_dead_sea(self):
        # -430 m is the lowest exposed land on Earth; the range has to hold it.
        from waveshed.gui.height_inputs import MIN_AMSL_M

        self.assertLessEqual(MIN_AMSL_M, -430.0)

    def test_an_unexpected_mode_falls_back_to_the_safe_floor(self):
        spin, combo = self._bound("AGL")
        combo.setCurrentText("")
        self.assertEqual(MIN_ANTENNA_AGL_M, spin.minimum)


class TestParameterBounds(unittest.TestCase):
    """The Processing parameter minimums.

    Asserted on the module constant rather than through the parameter objects:
    conftest stubs QgsProcessingParameterNumber as an inert class with no range
    semantics, so a bound cannot be exercised at that level here.
    """

    def test_p2p_algorithm_uses_the_shared_floor(self):
        from waveshed.algorithms import p2p as p2p_alg
        self.assertEqual(MIN_ANTENNA_AGL_M, p2p_alg.MIN_ANTENNA_AGL_M)

    def test_coverage_algorithm_uses_the_shared_floor(self):
        from waveshed.algorithms import coverage as coverage_alg
        self.assertEqual(MIN_ANTENNA_AGL_M, coverage_alg.MIN_ANTENNA_AGL_M)

    def test_no_height_parameter_declares_a_zero_minimum(self):
        # The old bound was minValue=0.0 on every height. Guard the source so a
        # re-added 0.0 minimum on a height parameter is caught.
        import inspect
        from waveshed.algorithms import coverage as coverage_alg
        from waveshed.algorithms import p2p as p2p_alg

        for module in (coverage_alg, p2p_alg):
            source = inspect.getsource(module)
            for block in source.split("self.addParameter(")[1:]:
                declaration = block.split(")\n")[0]
                if "HEIGHT" not in declaration:
                    continue
                self.assertNotIn(
                    "minValue=0.0", declaration,
                    f"{module.__name__}: a height parameter is back to a 0 m "
                    f"minimum",
                )


if __name__ == "__main__":
    unittest.main()
