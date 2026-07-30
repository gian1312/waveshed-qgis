"""Unit tests for runtime data-source attribution.

Several sources the plugin downloads are ODbL and must be credited in the UI.
These tests pin the credits that carry a legal obligation.

QGIS stubs provided by conftest.py.
"""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import unittest

from waveshed.core.attribution import (
    DATA_SOURCES,
    attribution_lines,
    attribution_text,
    source_credit,
)


class TestDataSources(unittest.TestCase):
    def test_every_source_is_fully_described(self):
        self.assertTrue(DATA_SOURCES)
        for s in DATA_SOURCES:
            for field in (s.name, s.credit, s.licence, s.url):
                self.assertTrue(field.strip(), f"empty field on {s.name!r}")
            self.assertTrue(s.url.startswith("https://"), s.url)

    def test_odbl_sources_credit_openstreetmap(self):
        # ODbL obliges naming the contributors, not just the tile host.
        for s in DATA_SOURCES:
            if "ODbL" in s.licence:
                self.assertIn(
                    "OpenStreetMap", s.credit,
                    f"{s.name} is ODbL but does not credit OpenStreetMap",
                )

    def test_terrain_and_building_sources_both_present(self):
        names = " ".join(s.name for s in DATA_SOURCES).lower()
        self.assertIn("terrarium", names)     # live elevation download
        self.assertIn("openfreemap", names)   # buildings
        self.assertIn("copernicus", names)
        self.assertIn("swissalti", names)

    def test_no_source_we_do_not_actually_fetch(self):
        # The web app credits Microsoft Global ML Building Footprints while
        # fetching OpenFreeMap. Crediting a dataset we never request is both
        # wrong and an attribution failure — keep it out of the plugin.
        text = attribution_text().lower()
        self.assertNotIn("microsoft", text)


class TestRendering(unittest.TestCase):
    def test_one_line_per_source(self):
        self.assertEqual(len(attribution_lines()), len(DATA_SOURCES))

    def test_line_carries_name_credit_and_licence(self):
        line = attribution_lines()[0]
        s = DATA_SOURCES[0]
        self.assertIn(s.name, line)
        self.assertIn(s.credit, line)
        self.assertIn(s.licence, line)

    def test_text_joins_all_lines(self):
        self.assertEqual(attribution_text().count("\n"), len(DATA_SOURCES) - 1)


class TestSourceCredit(unittest.TestCase):
    def test_lookup_is_case_insensitive_substring(self):
        self.assertIn("OpenStreetMap", source_credit("openfreemap"))
        self.assertIn("OpenMapTiles", source_credit("OpenFreeMap"))

    def test_unknown_source_returns_empty_not_partial(self):
        # Better to render nothing than a misleading half-credit.
        self.assertEqual(source_credit("nonexistent dataset"), "")


if __name__ == "__main__":
    unittest.main()
