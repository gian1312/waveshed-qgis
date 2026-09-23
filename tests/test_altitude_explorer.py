"""Unit tests for the Altitude Explorer's *per-band* altitude reference.

Every altitude in the dock says what it is measured from, so "100 m AGL" and
"2500 m AMSL" can be on the map at the same time — the above-ground ones drawn
on the raster the solver wrote, the sea-level ones on its twin.  What is tested
here is the part of that which is decidable without a running QGIS: the band
object and its label, the split that decides which raster each band reaches,
the layer visibility that follows from it, the readout sentence, and the fact
that a renderer built from only one reference's bands still nests them.

QGIS stubs come from conftest.py; the handful of extra Qt/QGIS classes the dock
needs are installed below rather than in conftest, so this file owns them.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import sys
import unittest
from unittest import mock

_stub_gui = sys.modules["qgis.gui"]
_stub_qtgui = sys.modules["qgis.PyQt.QtGui"]
for _name in ("QgsColorButton", "QgsDockWidget"):
    if not hasattr(_stub_gui, _name):
        setattr(_stub_gui, _name,
                type(_name, (), {"__init__": lambda self, *a, **kw: None}))
if not hasattr(_stub_qtgui, "QPixmap"):
    _stub_qtgui.QPixmap = type("QPixmap", (), {
        "__init__": lambda self, *a, **kw: None,
        "fill": lambda self, *a: None,
    })

from qgis.PyQt.QtCore import Qt  # noqa: E402 — after the stubs above

from waveshed.core import min_alt as ma  # noqa: E402
from waveshed.core import result_loader as rl  # noqa: E402
from waveshed.gui import altitude_explorer as ae  # noqa: E402


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

class _Color:
    """Enough QColor for a band: build from r/g/b (or copy one), read it back."""

    def __init__(self, *args):
        if len(args) == 1 and isinstance(args[0], _Color):
            self._rgb = args[0]._rgb
        elif len(args) >= 3:
            self._rgb = tuple(int(v) for v in args[:3])
        else:
            self._rgb = (0, 0, 0)
        self.alpha = 1.0

    def red(self):
        return self._rgb[0]

    def green(self):
        return self._rgb[1]

    def blue(self):
        return self._rgb[2]

    def setAlphaF(self, alpha):
        self.alpha = alpha

    def isValid(self):
        return True


class _Item:
    """QListWidgetItem stand-in that remembers what was set on it."""

    def __init__(self, text=""):
        self._text = text
        self._flags = 0
        self._check_state = None
        self.icon = None

    def text(self):
        return self._text

    def setText(self, text):
        self._text = text

    def flags(self):
        return self._flags

    def setFlags(self, flags):
        self._flags = flags

    def setIcon(self, icon):
        self.icon = icon

    def setCheckState(self, state):
        self._check_state = state

    def checkState(self):
        return self._check_state


class _ListWidget:
    """QListWidget stand-in — only the calls _rebuild_band_list makes."""

    def __init__(self):
        self._items = []
        self._row = -1

    def blockSignals(self, _on):
        pass

    def clear(self):
        self._items = []
        self._row = -1

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def item(self, row):
        return self._items[row] if 0 <= row < len(self._items) else None

    def setCurrentRow(self, row):
        self._row = row

    def currentRow(self):
        return self._row

    def texts(self):
        return [item.text() for item in self._items]


class _Toggle:
    """A radio button / check box that only has to answer isChecked()."""

    def __init__(self, checked=False):
        self._checked = checked

    def isChecked(self):
        return self._checked

    def setChecked(self, checked):
        self._checked = bool(checked)

    def setSuffix(self, _suffix):
        pass


class _Label:
    def __init__(self):
        self.text = ""

    def setText(self, text):
        self.text = text


class _Layer:
    """A QgsRasterLayer stand-in the renderer can be hung on."""

    def __init__(self, name):
        self.layer_name = name
        self.renderer = None
        self.repaints = 0

    def dataProvider(self):
        return f"provider:{self.layer_name}"

    def setRenderer(self, renderer):
        self.renderer = renderer

    def triggerRepaint(self):
        self.repaints += 1


def _dock(**attrs):
    """A dock carrying only the attributes the code path under test reads.

    Built with ``__new__`` so the *methods* are the real ones — only the
    widgets and the project lookups are doubles.
    """
    dock = ae.AltitudeExplorerDock.__new__(ae.AltitudeExplorerDock)
    for key, value in attrs.items():
        setattr(dock, key, value)
    return dock


def _bands(*spec):
    """``_Band`` objects from ``(altitude_m, reference)`` pairs."""
    return [ae._Band(altitude, _Color(1, 2, 3), reference=reference)
            for altitude, reference in spec]


def _alt_bands(*spec):
    """GUI-free bands from ``(altitude_m, reference)`` pairs."""
    return [ma.AltitudeBand(altitude, (1, 2, 3), True, reference)
            for altitude, reference in spec]


class _PatchMixin:
    def patch(self, target, name, value):
        patcher = mock.patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)
        return value


# ---------------------------------------------------------------------------
# The band object
# ---------------------------------------------------------------------------

class TestBandObject(_PatchMixin, unittest.TestCase):
    def setUp(self):
        self.patch(ae, "QColor", _Color)

    def test_a_band_defaults_to_above_ground(self):
        band = ae._Band(100.0, _Color(1, 2, 3))
        self.assertEqual(band.reference, ma.REF_AGL)
        self.assertEqual(band.label, "100 m AGL")

    def test_a_band_keeps_the_reference_it_was_given(self):
        band = ae._Band(2500.0, _Color(1, 2, 3), reference=ma.REF_AMSL)
        self.assertEqual(band.reference, ma.REF_AMSL)
        self.assertEqual(band.label, "2500 m AMSL")

    def test_an_unknown_reference_reads_as_above_ground(self):
        # State written before the reference existed comes back as None.
        self.assertEqual(
            ae._Band(100.0, _Color(1, 2, 3), reference=None).reference,
            ma.REF_AGL,
        )
        self.assertEqual(
            ae._Band(100.0, _Color(1, 2, 3), reference="sea-ish").reference,
            ma.REF_AGL,
        )

    def test_the_reference_travels_to_the_renderer_view(self):
        band = ae._Band(2500.0, _Color(4, 5, 6), reference=ma.REF_AMSL)
        flat = band.as_altitude_band()
        self.assertEqual(flat.altitude_m, 2500.0)
        self.assertEqual(flat.color, (4, 5, 6))
        self.assertTrue(flat.visible)
        self.assertEqual(flat.reference, ma.REF_AMSL)

    def test_the_reference_can_be_changed_in_place(self):
        # The "Measured from" radios edit the selected band, they do not
        # replace it — the editor strip tracks band identity.
        band = ae._Band(300.0, _Color(1, 2, 3))
        band.reference = ma.REF_AMSL
        self.assertEqual(band.label, "300 m AMSL")
        self.assertEqual(band.as_altitude_band().reference, ma.REF_AMSL)


# ---------------------------------------------------------------------------
# The band list
# ---------------------------------------------------------------------------

class TestBandListRows(_PatchMixin, unittest.TestCase):
    def setUp(self):
        self.patch(ae, "QColor", _Color)
        self.patch(ae, "QListWidgetItem", _Item)
        self.band_list = _ListWidget()

    def _dock_with(self, bands, selected=None):
        dock = _dock(
            _bands=list(bands),
            _selected_band=selected,
            _reference=ma.REF_AGL,
            band_list=self.band_list,
        )
        dock._sync_band_editor = lambda: None  # exercised in its own test
        return dock

    def test_every_row_is_labelled_with_its_own_reference(self):
        dock = self._dock_with(_bands(
            (100.0, ma.REF_AGL), (2500.0, ma.REF_AMSL), (300.0, ma.REF_AGL),
        ))
        dock._rebuild_band_list()
        self.assertEqual(
            self.band_list.texts(),
            ["100 m AGL", "300 m AGL", "2500 m AMSL"],
        )

    def test_rows_are_grouped_by_reference_not_interleaved_by_altitude(self):
        # 300 m AGL must not be read as "between 100 m AGL and 2500 m AMSL":
        # the two references are different measurements, not one ladder.
        dock = self._dock_with(_bands(
            (2500.0, ma.REF_AMSL), (300.0, ma.REF_AGL), (900.0, ma.REF_AMSL),
        ))
        dock._rebuild_band_list()
        self.assertEqual(
            [band.reference for band in dock._bands],
            [ma.REF_AGL, ma.REF_AMSL, ma.REF_AMSL],
        )
        self.assertEqual(
            self.band_list.texts(),
            ["300 m AGL", "900 m AMSL", "2500 m AMSL"],
        )

    def test_a_hidden_band_keeps_its_row_unticked(self):
        bands = _bands((100.0, ma.REF_AGL), (2500.0, ma.REF_AMSL))
        bands[1].visible = False
        dock = self._dock_with(bands)
        dock._rebuild_band_list()
        self.assertEqual(self.band_list.item(0).checkState(),
                         Qt.CheckState.Checked)
        self.assertEqual(self.band_list.item(1).checkState(),
                         Qt.CheckState.Unchecked)

    def test_the_selected_row_sets_what_the_radios_show(self):
        bands = _bands((100.0, ma.REF_AGL), (2500.0, ma.REF_AMSL))
        dock = self._dock_with(bands)
        dock._rebuild_band_list(select=bands[1])
        self.assertIs(dock._selected_band, bands[1])
        self.assertEqual(dock._reference, ma.REF_AMSL)
        self.assertEqual(self.band_list.currentRow(), 1)

    def test_dragging_a_band_relabels_it_in_its_own_reference(self):
        # The row text is rewritten in place mid-drag; before per-band
        # references it used the dock's one, which is now the *selection's*.
        bands = _bands((100.0, ma.REF_AGL), (2500.0, ma.REF_AMSL))
        dock = self._dock_with(bands)
        dock._rebuild_band_list(select=bands[1])
        dock._apply = lambda: None
        dock._set_selected_altitude(2600)
        self.assertEqual(self.band_list.item(1).text(), "2600 m AMSL")
        self.assertEqual(self.band_list.item(0).text(), "100 m AGL")


# ---------------------------------------------------------------------------
# Which raster gets which bands
# ---------------------------------------------------------------------------

class TestRenderSplit(_PatchMixin, unittest.TestCase):
    """Item-by-item cover of the rendering rule: AGL bands go to the layer the
    solver wrote, AMSL bands to its sea-level twin, and a layer whose reference
    no band uses is not painted at all."""

    def setUp(self):
        self.patch(ae, "QColor", _Color)
        self.patch(ae, "estimate_altitude_range_m", lambda layer: (0.0, 500.0))
        self.band_calls = []
        self.ramp_calls = []

        def build_band_renderer(provider, band, bands, reference=ma.REF_AGL,
                                **kwargs):
            self.band_calls.append({
                "provider": provider, "bands": list(bands),
                "reference": reference,
            })
            return ("bands", provider)

        def build_min_alt_renderer(provider, band, **kwargs):
            self.ramp_calls.append(dict(kwargs, provider=provider))
            return ("ramp", provider)

        self.patch(ae, "build_band_renderer", build_band_renderer)
        self.patch(ae, "build_min_alt_renderer", build_min_alt_renderer)

        self.site = _Layer("site")
        self.twin = _Layer("site (AMSL)")

    def _dock_with(self, bands, *, twin=True, limiting=True, nested=True,
                   reference=ma.REF_AGL):
        dock = _dock(
            _bands=list(bands),
            _reference=reference,
            chk_threshold=_Toggle(limiting),
            radio_bands=_Toggle(nested),
            radio_shade=_Toggle(not nested),
            lbl_readout=_Label(),
        )
        dock._checked_layers = lambda: [self.site]
        dock._twin_of = lambda layer: self.twin if twin else None
        return dock

    def test_each_raster_is_handed_only_the_bands_measured_its_way(self):
        dock = self._dock_with(_bands(
            (100.0, ma.REF_AGL), (300.0, ma.REF_AGL), (2500.0, ma.REF_AMSL),
        ))
        dock._apply()

        self.assertEqual([call["provider"] for call in self.band_calls],
                         ["provider:site", "provider:site (AMSL)"])
        agl, amsl = self.band_calls
        self.assertEqual([band.altitude_m for band in agl["bands"]],
                         [100.0, 300.0])
        self.assertEqual(agl["reference"], ma.REF_AGL)
        self.assertEqual([band.altitude_m for band in amsl["bands"]], [2500.0])
        self.assertEqual(amsl["reference"], ma.REF_AMSL)
        # Both surfaces are painted at once — that is the feature.
        self.assertEqual(self.site.renderer, ("bands", "provider:site"))
        self.assertEqual(self.twin.renderer, ("bands", "provider:site (AMSL)"))

    def test_the_bands_reach_the_renderer_stamped_with_their_reference(self):
        dock = self._dock_with(_bands(
            (100.0, ma.REF_AGL), (2500.0, ma.REF_AMSL),
        ))
        dock._apply()
        for call in self.band_calls:
            with self.subTest(reference=call["reference"]):
                self.assertTrue(all(band.reference == call["reference"]
                                    for band in call["bands"]))

    def test_a_reference_with_no_band_is_not_painted(self):
        dock = self._dock_with(_bands((100.0, ma.REF_AGL)))
        dock._apply()
        self.assertEqual(len(self.band_calls), 1)
        self.assertEqual(self.band_calls[0]["provider"], "provider:site")
        self.assertIsNone(self.twin.renderer)

    def test_a_sea_level_band_paints_the_twin_alone(self):
        dock = self._dock_with(_bands((2500.0, ma.REF_AMSL)),
                               reference=ma.REF_AMSL)
        dock._apply()
        self.assertEqual([call["provider"] for call in self.band_calls],
                         ["provider:site (AMSL)"])
        self.assertIsNone(self.site.renderer)

    def test_a_sea_level_band_with_no_twin_yet_paints_nothing_and_says_so(self):
        dock = self._dock_with(_bands((2500.0, ma.REF_AMSL)), twin=False,
                               reference=ma.REF_AMSL)
        dock._apply()
        self.assertEqual(self.band_calls, [])
        self.assertIn("No sea-level view", dock.lbl_readout.text)

    def test_the_continuous_ramp_follows_the_selected_reference_only(self):
        # One ramp is one surface: it cannot mean two measurements at once.
        dock = self._dock_with(
            _bands((100.0, ma.REF_AGL), (2500.0, ma.REF_AMSL)),
            nested=False, reference=ma.REF_AMSL,
        )
        dock._apply()
        self.assertEqual(self.band_calls, [])
        self.assertEqual([call["provider"] for call in self.ramp_calls],
                         ["provider:site (AMSL)"])
        # …clipped at the top band *of that reference*, not the global top.
        self.assertEqual(self.ramp_calls[0]["threshold_m"], 2500.0)
        self.assertEqual(self.ramp_calls[0]["reference"], ma.REF_AMSL)

    def test_the_full_range_ramp_follows_the_selected_reference_too(self):
        dock = self._dock_with(
            _bands((100.0, ma.REF_AGL), (2500.0, ma.REF_AMSL)),
            limiting=False, reference=ma.REF_AGL,
        )
        dock._apply()
        self.assertEqual([call["provider"] for call in self.ramp_calls],
                         ["provider:site"])
        self.assertIsNone(self.ramp_calls[0]["threshold_m"])

    def test_the_ramp_range_is_taken_per_reference(self):
        # An AGL span and an AMSL span are different numbers; sharing one ramp
        # across both would make one site's red mean two things.
        spans = {"provider:site": (0.0, 400.0),
                 "provider:site (AMSL)": (1200.0, 3000.0)}
        self.patch(ae, "estimate_altitude_range_m",
                   lambda layer: spans[layer.dataProvider()])
        dock = self._dock_with(
            _bands((100.0, ma.REF_AGL), (2500.0, ma.REF_AMSL)), nested=False,
        )
        for reference, expected in ((ma.REF_AGL, (0.0, 400.0)),
                                    (ma.REF_AMSL, (1200.0, 3000.0))):
            with self.subTest(reference=reference):
                self.ramp_calls.clear()
                dock._reference = reference
                dock._apply()
                self.assertEqual(
                    (self.ramp_calls[0]["min_ramp_m"],
                     self.ramp_calls[0]["max_ramp_m"]),
                    expected,
                )


class TestLayerVisibility(_PatchMixin, unittest.TestCase):
    """A raster whose reference no band uses is taken off the canvas, and the
    two halves of a pair are on together when both references are in use."""

    def setUp(self):
        self.patch(ae, "QColor", _Color)
        self.site = _Layer("site")
        self.twin = _Layer("site (AMSL)")
        self.visible = {}

    def _sync(self, bands, *, nested=True, limiting=True,
              reference=ma.REF_AGL, twin=True):
        dock = _dock(
            _bands=list(bands),
            _reference=reference,
            chk_threshold=_Toggle(limiting),
            radio_bands=_Toggle(nested),
        )
        dock._checked_layers = lambda: [self.site]
        dock._twin_of = lambda layer: self.twin if twin else None
        dock._set_layer_visible = lambda layer, show: self.visible.__setitem__(
            layer.layer_name, show,
        )
        dock._sync_layer_visibility()
        return self.visible

    def test_both_halves_are_on_when_both_references_are_in_use(self):
        visible = self._sync(_bands((100.0, ma.REF_AGL),
                                    (2500.0, ma.REF_AMSL)))
        self.assertEqual(visible, {"site": True, "site (AMSL)": True})

    def test_the_twin_is_hidden_while_every_band_is_above_ground(self):
        visible = self._sync(_bands((100.0, ma.REF_AGL)))
        self.assertEqual(visible, {"site": True, "site (AMSL)": False})

    def test_the_original_is_hidden_while_every_band_is_sea_level(self):
        visible = self._sync(_bands((2500.0, ma.REF_AMSL)),
                             reference=ma.REF_AMSL)
        self.assertEqual(visible, {"site": False, "site (AMSL)": True})

    def test_the_continuous_ramp_shows_one_half_whatever_the_bands_say(self):
        visible = self._sync(
            _bands((100.0, ma.REF_AGL), (2500.0, ma.REF_AMSL)),
            nested=False, reference=ma.REF_AMSL,
        )
        self.assertEqual(visible, {"site": False, "site (AMSL)": True})


class TestNeedsTwins(_PatchMixin, unittest.TestCase):
    """A sea-level twin is built on demand — when the first sea-level altitude
    appears over a layer that has none."""

    def setUp(self):
        self.patch(ae, "QColor", _Color)
        self.site = _Layer("site")
        self.twin = _Layer("site (AMSL)")

    def _dock_with(self, bands, *, twin, reference=ma.REF_AGL):
        dock = _dock(
            _bands=list(bands),
            _reference=reference,
            chk_threshold=_Toggle(True),
            radio_bands=_Toggle(True),
        )
        dock._checked_layers = lambda: [self.site]
        dock._twin_of = lambda layer: self.twin if twin else None
        return dock

    def test_above_ground_bands_never_ask_for_a_twin(self):
        dock = self._dock_with(_bands((100.0, ma.REF_AGL)), twin=False)
        self.assertFalse(dock._needs_twins())

    def test_the_first_sea_level_band_asks_for_one(self):
        dock = self._dock_with(
            _bands((100.0, ma.REF_AGL), (2500.0, ma.REF_AMSL)), twin=False,
        )
        self.assertTrue(dock._needs_twins())

    def test_an_existing_twin_is_not_rebuilt(self):
        dock = self._dock_with(_bands((2500.0, ma.REF_AMSL)), twin=True,
                               reference=ma.REF_AMSL)
        self.assertFalse(dock._needs_twins())

    def test_a_failed_build_brings_the_altitudes_back_to_ground(self):
        bands = _bands((100.0, ma.REF_AGL), (2500.0, ma.REF_AMSL))
        dock = self._dock_with(bands, twin=False, reference=ma.REF_AMSL)
        dock._ensure_twins = lambda: False  # nothing could be built
        finished = []
        dock._finish_reference_change = lambda: finished.append(True)

        self.assertTrue(dock._amsl_pending())  # caller stands down
        self.assertEqual([band.reference for band in bands],
                         [ma.REF_AGL, ma.REF_AGL])
        self.assertEqual(dock._reference, ma.REF_AGL)
        self.assertEqual(finished, [True])  # …and the dock was redrawn

    def test_a_started_build_leaves_the_sea_level_band_alone(self):
        bands = _bands((2500.0, ma.REF_AMSL))
        dock = self._dock_with(bands, twin=False, reference=ma.REF_AMSL)
        dock._ensure_twins = lambda: True  # building in the background
        self.assertTrue(dock._amsl_pending())
        self.assertEqual(bands[0].reference, ma.REF_AMSL)


# ---------------------------------------------------------------------------
# The readout
# ---------------------------------------------------------------------------

class TestReadout(unittest.TestCase):
    def test_mixed_references_are_listed_one_by_one(self):
        text = ae._readout_text(
            2, _alt_bands((100.0, ma.REF_AGL), (300.0, ma.REF_AGL),
                          (2500.0, ma.REF_AMSL)),
            use_bands=True, nested=True, reference=ma.REF_AGL,
        )
        self.assertIn(
            "3 altitudes on 2 layers: 100 m AGL, 300 m AGL, 2500 m AMSL", text,
        )

    def test_one_reference_keeps_the_shared_suffix(self):
        text = ae._readout_text(
            1, _alt_bands((100.0, ma.REF_AGL), (300.0, ma.REF_AGL)),
            use_bands=True, nested=True, reference=ma.REF_AGL,
        )
        self.assertEqual(
            text, "Reachable at 100, 300 m AGL — nested bands on 1 layer.",
        )

    def test_a_single_sea_level_band_reads_as_one(self):
        text = ae._readout_text(
            1, _alt_bands((2500.0, ma.REF_AMSL)),
            use_bands=True, nested=True, reference=ma.REF_AMSL,
        )
        self.assertEqual(text, "Reachable at ≤ 2500 m AMSL on 1 layer.")

    def test_hiding_one_of_two_references_drops_back_to_the_simple_wording(self):
        bands = [
            ma.AltitudeBand(100.0, (1, 2, 3), True, ma.REF_AGL),
            ma.AltitudeBand(2500.0, (1, 2, 3), False, ma.REF_AMSL),
        ]
        text = ae._readout_text(2, bands, use_bands=True, nested=True,
                                reference=ma.REF_AGL)
        self.assertEqual(text, "Reachable at ≤ 100 m AGL on 2 layers.")
        self.assertNotIn("AMSL", text)

    def test_all_hidden_counts_every_altitude(self):
        bands = [
            ma.AltitudeBand(100.0, (1, 2, 3), False, ma.REF_AGL),
            ma.AltitudeBand(2500.0, (1, 2, 3), False, ma.REF_AMSL),
        ]
        text = ae._readout_text(2, bands, use_bands=True, nested=True,
                                reference=ma.REF_AGL)
        self.assertEqual(text, "All 2 altitudes are hidden — tick one to draw it.")

    def test_the_shading_style_names_the_reference_it_followed(self):
        # The rule (one ramp, the selected altitude's reference) is documented
        # nowhere else the user looks.
        text = ae._readout_text(
            1, _alt_bands((100.0, ma.REF_AGL), (2500.0, ma.REF_AMSL)),
            use_bands=True, nested=False, reference=ma.REF_AMSL,
        )
        self.assertIn("≤ 2500 m AMSL", text)
        self.assertIn("coloured by the altitude each point needs", text)
        self.assertNotIn("100 m AGL", text)

    def test_the_full_range_ramp_names_the_reference_it_followed(self):
        for reference, phrase in ((ma.REF_AGL, "above ground"),
                                  (ma.REF_AMSL, "above sea level")):
            with self.subTest(reference=reference):
                text = ae._readout_text(
                    2, _alt_bands((100.0, ma.REF_AGL)), use_bands=False,
                    nested=True, reference=reference,
                )
                self.assertIn(f"measured {phrase}", text)
                self.assertIn("2 layers", text)

    def test_layers_still_waiting_for_a_sea_level_view_are_counted(self):
        text = ae._readout_text(
            1, _alt_bands((2500.0, ma.REF_AMSL)), use_bands=True, nested=True,
            reference=ma.REF_AMSL, undriven=2,
        )
        self.assertIn("2 more await a sea-level view.", text)

    def test_nothing_painted_at_all(self):
        self.assertEqual(
            ae._readout_text(0, [], use_bands=True, nested=True,
                             reference=ma.REF_AGL),
            "No layers selected.",
        )
        self.assertIn(
            "No sea-level view",
            ae._readout_text(0, _alt_bands((2500.0, ma.REF_AMSL)),
                             use_bands=True, nested=True,
                             reference=ma.REF_AMSL, undriven=1),
        )


# ---------------------------------------------------------------------------
# The renderer built from one reference's bands
# ---------------------------------------------------------------------------

class _RampShader:
    """QgsColorRampShader stand-in that keeps the items it is handed."""

    Type = rl.QgsColorRampShader.Type

    class ColorRampItem:
        def __init__(self, value, color, label=""):
            self.value = value
            self.color = color
            self.label = label

    def __init__(self):
        self.items = []
        self.ramp_type = None
        self.clip = None

    def setColorRampType(self, ramp_type):
        self.ramp_type = ramp_type

    def setColorRampItemList(self, items):
        self.items = list(items)

    def setClip(self, clip):
        self.clip = clip


class _Shader:
    def __init__(self):
        self.function = None

    def setRasterShaderFunction(self, function):
        self.function = function


class _PseudoColorRenderer:
    def __init__(self, provider, band, shader):
        self.provider = provider
        self.band = band
        self.shader = shader


class TestSubsetRenderer(_PatchMixin, unittest.TestCase):
    """The nested-band renderer is now built per layer from *that layer's*
    bands, so each subset has to nest on its own."""

    def setUp(self):
        self.patch(rl, "QgsColorRampShader", _RampShader)
        self.patch(rl, "QgsRasterShader", _Shader)
        self.patch(rl, "QgsSingleBandPseudoColorRenderer", _PseudoColorRenderer)
        self.patch(rl, "QColor", _Color)
        self.mixed = _alt_bands(
            (100.0, ma.REF_AGL), (300.0, ma.REF_AGL), (2500.0, ma.REF_AMSL),
        )
        self.grouped = ma.split_bands_by_reference(self.mixed)

    def _items(self, reference):
        renderer = rl.build_band_renderer(
            f"provider:{reference}", 1, self.grouped[reference],
            reference=reference,
        )
        return renderer.shader.function.items

    def test_the_above_ground_subset_keeps_its_rings(self):
        items = self._items(ma.REF_AGL)
        self.assertEqual([item.value for item in items], [
            ma.altitude_to_raw(100.0), ma.altitude_to_raw(300.0),
            ma.MIN_ALT_SENTINEL,
        ])
        self.assertEqual([item.label for item in items],
                         ["≤ 100 m AGL", "100 – 300 m AGL", ""])

    def test_the_sea_level_subset_starts_its_own_nesting(self):
        # 2500 m is the first ring on the twin — "≤ 2500 m AMSL", never
        # "300 – 2500", which would be a band of the *other* raster's ladder.
        items = self._items(ma.REF_AMSL)
        self.assertEqual([item.value for item in items],
                         [ma.altitude_to_raw(2500.0), ma.MIN_ALT_SENTINEL])
        self.assertEqual([item.label for item in items],
                         ["≤ 2500 m AMSL", ""])

    def test_neither_subset_carries_the_other_s_altitudes(self):
        agl = {item.value for item in self._items(ma.REF_AGL)}
        amsl = {item.value for item in self._items(ma.REF_AMSL)}
        self.assertNotIn(ma.altitude_to_raw(2500.0), agl)
        self.assertNotIn(ma.altitude_to_raw(100.0), amsl)
        self.assertNotIn(ma.altitude_to_raw(300.0), amsl)

    def test_every_subset_is_a_discrete_ramp_ending_transparent(self):
        for reference in ma.REFERENCES:
            with self.subTest(reference=reference):
                items = self._items(reference)
                values = [item.value for item in items]
                self.assertEqual(values, sorted(values))
                self.assertEqual(len(set(values)), len(values))
                self.assertEqual(items[-1].value, ma.MIN_ALT_SENTINEL)
                self.assertEqual(items[-1].color.alpha, 1.0)
                self.assertEqual(
                    (items[-1].color.red(), items[-1].color.green(),
                     items[-1].color.blue()), (0, 0, 0),
                )

    def test_the_visible_band_colours_survive_the_split(self):
        bands = [
            ma.AltitudeBand(100.0, (10, 20, 30), True, ma.REF_AGL),
            ma.AltitudeBand(2500.0, (40, 50, 60), True, ma.REF_AMSL),
        ]
        grouped = ma.split_bands_by_reference(bands)
        renderer = rl.build_band_renderer(
            "provider", 1, grouped[ma.REF_AMSL], reference=ma.REF_AMSL,
        )
        first = renderer.shader.function.items[0]
        self.assertEqual((first.color.red(), first.color.green(),
                          first.color.blue()), (40, 50, 60))


if __name__ == "__main__":
    unittest.main()
