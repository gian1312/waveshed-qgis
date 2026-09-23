"""The built-in help must stay true to the code it documents.

Three kinds of check, none of which can pass by construction:

* **Structure** — every topic in the index has an anchor in the rendered
  document and vice versa, every internal link resolves, and the HTML parses
  with its key tags balanced.
* **Coverage** — the document names the controls, constants and labels the
  plugin really builds. The checklist is *derived from the source* (the
  resolution list, the tile-extent table, the sites-table columns, the ITM
  parameter labels read out of ``site_analysis_tab``), so a rename in the GUI
  fails here instead of quietly leaving the help wrong.
* **Packaging** — the help package ships in the plugin ZIP.

The one thing this file must never do is assert on text it also supplies; the
expectations below all come from either the GUI modules or ``core``.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import List, Set

import pytest

from waveshed import help as help_pkg
from waveshed.core.job_builder import VALID_RESOLUTIONS
from waveshed.core.terrain_adapter import ABT_EXTENT_DEG

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HTML = help_pkg.build_html()
TEXT = help_pkg._plain_text(HTML)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _named_anchors(html: str) -> List[str]:
    """Every ``<a name="...">`` target in *html*, in order (duplicates kept)."""
    return re.findall(r'<a\s+name="([^"]+)"', html)


def _internal_links(html: str) -> Set[str]:
    """Every ``href="#..."`` target in *html*."""
    return set(re.findall(r'href="#([^"]+)"', html))


def _addrow_labels(source: str, marker: str, end_marker: str) -> List[str]:
    """``addRow("Label:", ...)`` labels between two markers in GUI source.

    Reads the labels straight out of the widget code, so the checklist below
    is the dialog's own wording rather than a copy of it.
    """
    start = source.index(marker)
    end = source.index(end_marker, start)
    return re.findall(r'addRow\(\s*"([^"]+)"', source[start:end])


def _combo_items(source: str, marker: str, end_marker: str) -> List[str]:
    """String literals passed to ``addItems([...])`` between two markers."""
    start = source.index(marker)
    end = source.index(end_marker, start)
    chunk = source[start:end]
    items: List[str] = []
    for block in re.findall(r"addItems\(\[(.*?)\]\)", chunk, re.DOTALL):
        items.extend(re.findall(r'"([^"]+)"', block))
    return items


class _BalanceChecker(HTMLParser):
    """Track a stack of the tags whose nesting the renderer depends on."""

    #: Tags that must be opened and closed. Void tags (br, hr, img) and the
    #: optional-close ones HTML allows are deliberately absent.
    TRACKED = {
        "h1", "h2", "h3", "p", "ul", "ol", "li", "table", "tr", "td", "th",
        "b", "i", "code", "pre", "a",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: List[str] = []
        self.errors: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in self.TRACKED:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag not in self.TRACKED:
            return
        if not self.stack:
            self.errors.append(f"</{tag}> with nothing open")
            return
        if self.stack[-1] != tag:
            self.errors.append(
                f"</{tag}> closes while <{self.stack[-1]}> is open")
            self.stack.pop()
            return
        self.stack.pop()


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

class TestStructure:
    def test_there_is_content(self):
        assert help_pkg.TOPICS, "the help has no topics at all"
        assert len(HTML) > 20_000, (
            f"the help document is only {len(HTML)} characters — that is not "
            f"the exhaustive manual it is supposed to be"
        )

    def test_every_topic_has_an_anchor(self):
        anchors = _named_anchors(HTML)
        missing = [t.anchor for t in help_pkg.TOPICS if t.anchor not in anchors]
        assert not missing, f"topics with no anchor in the document: {missing}"

    def test_every_anchor_belongs_to_a_topic(self):
        # "top" is the document's own contents anchor.
        known = {t.anchor for t in help_pkg.TOPICS} | {"top"}
        stray = [a for a in _named_anchors(HTML) if a not in known]
        assert not stray, f"anchors that no topic owns: {stray}"

    def test_anchors_are_unique(self):
        anchors = _named_anchors(HTML)
        duplicates = sorted({a for a in anchors if anchors.count(a) > 1})
        assert not duplicates, f"duplicate anchors: {duplicates}"

    def test_every_topic_title_is_in_the_document(self):
        for topic in help_pkg.TOPICS:
            assert topic.title in HTML, topic.anchor

    def test_internal_links_resolve(self):
        known = {t.anchor for t in help_pkg.TOPICS} | {"top"}
        broken = sorted(_internal_links(HTML) - known)
        assert not broken, f"links to anchors that do not exist: {broken}"

    def test_every_topic_is_linked_from_the_contents(self):
        links = _internal_links(HTML)
        missing = [t.anchor for t in help_pkg.TOPICS if t.anchor not in links]
        assert not missing, f"topics missing from the contents list: {missing}"

    def test_every_topic_has_a_body(self):
        thin = [t.anchor for t in help_pkg.TOPICS
                if len(help_pkg._plain_text(t.body).split()) < 120]
        assert not thin, f"topics with almost no text: {thin}"

    def test_external_links_are_absolute(self):
        hrefs = re.findall(r'href="([^"#][^"]*)"', HTML)
        bad = [h for h in hrefs if not h.startswith(("http://", "https://"))]
        assert not bad, f"links that are neither anchors nor URLs: {bad}"

    def test_html_parses_with_balanced_tags(self):
        checker = _BalanceChecker()
        checker.feed(HTML)
        checker.close()
        assert not checker.errors, "malformed HTML: " + "; ".join(
            checker.errors[:10])
        assert not checker.stack, (
            f"tags left open at the end of the document: {checker.stack[:10]}")

    def test_find_topic_is_forgiving(self):
        assert help_pkg.find_topic("#overview") is help_pkg.find_topic("overview")
        assert help_pkg.find_topic("OVERVIEW") is help_pkg.find_topic("overview")
        assert help_pkg.find_topic("no-such-topic") is None
        assert help_pkg.find_topic("") is None

    def test_default_anchor_exists(self):
        assert help_pkg.find_topic(help_pkg.DEFAULT_ANCHOR) is not None

    def test_search_filters(self):
        every = help_pkg.search("")
        assert len(every) == len(help_pkg.TOPICS)
        hits = help_pkg.search("machine fingerprint")
        assert hits and all(
            "fingerprint" in help_pkg._plain_text(t.body).lower()
            or "fingerprint" in " ".join(t.keywords).lower()
            for t in hits
        )
        assert help_pkg.search("zzzz-not-in-the-help") == []


# ---------------------------------------------------------------------------
# Coverage of what the code actually offers
# ---------------------------------------------------------------------------

class TestCoversTheUI:
    def test_every_valid_resolution_is_documented(self):
        for res in VALID_RESOLUTIONS:
            assert re.search(rf"\b{res}\s*m\b", TEXT), (
                f"resolution {res} m is offered in the UI but not mentioned "
                f"in the help")

    def test_tile_extents_are_documented(self):
        # The resolution -> tile-extent table is the terrain contract; the
        # help states it, so it has to agree with ABT_EXTENT_DEG.
        for res, extent in ABT_EXTENT_DEG.items():
            assert f"{extent}°" in HTML or f"{extent}&deg;" in HTML, (
                f"tile extent {extent} deg (resolution {res} m) is not in the "
                f"help")

    def test_site_table_columns_are_documented(self):
        from waveshed.gui import site_analysis_tab as sat

        for column in sat._SITE_COLUMNS:
            assert column in HTML, f"sites-table column {column!r} undocumented"
        for column in sat._ALT_COLUMNS:
            assert column in HTML, f"altitude column {column!r} undocumented"

    def test_itm_parameter_labels_are_documented(self):
        source = (PLUGIN_ROOT / "waveshed" / "gui"
                  / "site_analysis_tab.py").read_text(encoding="utf-8")
        labels = _addrow_labels(source, "def _build_itm_section",
                                "def _connect_signals")
        assert len(labels) >= 7, (
            f"only found {labels} in _build_itm_section — the scraper is "
            f"broken, not the help")
        for label in labels:
            assert label in HTML, f"ITM control {label!r} undocumented"

    def test_analysis_parameter_labels_are_documented(self):
        source = (PLUGIN_ROOT / "waveshed" / "gui"
                  / "site_analysis_tab.py").read_text(encoding="utf-8")
        labels = _addrow_labels(source, "def _build_analysis_section",
                                "def _build_itm_section")
        assert len(labels) >= 5, f"scraper found only {labels}"
        for label in labels:
            if not label:  # the empty label of the DEM info row
                continue
            assert label in HTML, f"analysis control {label!r} undocumented"

    def test_radio_climates_are_documented(self):
        source = (PLUGIN_ROOT / "waveshed" / "gui"
                  / "site_analysis_tab.py").read_text(encoding="utf-8")
        climates = _combo_items(source, "self.combo_climate = QComboBox()",
                                "Radio Climate:")
        assert len(climates) == 6, f"scraper found {climates}"
        for climate in climates:
            assert climate in HTML, f"radio climate {climate!r} undocumented"

    @pytest.mark.parametrize("label", [
        # Main dialog
        "Analysis Mode:", "Line of Sight (LOS)", "Propagation Loss",
        "LOS Floor", "P2P Link", "Assets", "Map Converter", "Settings",
        # Site analysis
        "Run Analysis", "Include buildings (OpenFreeMap)",
        "Earth Radius Mode:", "FOUR_THIRDS", "ADVANCED", "AUTO", "GPU", "CPU",
        # P2P
        "Pick from Map", "View Plots", "Curved Earth Profile",
        "Source_ID", "Signal_dBm", "Path_Loss_dB",
        # Assets
        "Antenna Patterns", "Save As", "Angle (deg)", "Gain (0-1)",
        # Map converter
        "Layer Stack (highest priority on top)", "Rebuild existing tiles",
        "Inspect .abt...", "Download Buildings...",
        # Settings
        "Download Binaries", "Auto-detect", "Show machine fingerprint",
        "Max VRAM budget (GB):", "Max RAM budget (GB):",
        "Download connections:", "Local terrain dir:", "Terrain cache:",
        # Altitude Explorer
        "Altitude Explorer", "Measured from:", "Ground (AGL)",
        "Sea level (AMSL)", "Add altitude", "Limit to these altitudes",
        "Merge selected", "Iso-altitude contours", "Required altitude",
        # Processing
        "Site Analysis (Coverage)", "Point-to-Point Link Analysis",
        "Batch CSV File", "MIN_ALT",
        # Engine / troubleshooting
        "aether_core", "aether_converter", "aether_export",
        "Unsupported resolution", "AETHER_BIN_DIR", "AETHER_LICENSE",
    ])
    def test_label_is_mentioned(self, label):
        assert label in HTML, f"{label!r} is in the UI but not in the help"

    @pytest.mark.parametrize("topic_anchor,needle", [
        ("physics", "291"),          # smooth-earth horizon of a 5000 m platform
        ("physics", "5.3 km"),       # bulge at 300 km
        ("physics", "4/3"),
        ("heights", "0.5 m"),        # terrain vertical quantum
        ("terrain", "sea level"),    # the no-data contract
        ("itm", "20 GHz"),           # ITM frequency envelope
        ("itm", "2000 km"),          # ITM distance envelope
        ("install", "Base58"),
        ("p2p", "AGL"),              # batch CSV mode column
        ("altitude-explorer", "65535"),
    ])
    def test_topic_states_key_fact(self, topic_anchor, needle):
        topic = help_pkg.find_topic(topic_anchor)
        assert topic is not None, topic_anchor
        assert needle in topic.body, (
            f"{needle!r} is missing from the {topic_anchor!r} topic")

    def test_min_alt_is_called_los_floor_in_prose(self):
        """The UI renamed MIN_ALT to "LOS Floor"; the old name may only appear
        as the engine's wire value, which the help says explicitly."""
        from waveshed.gui import main_dialog

        assert main_dialog._MIN_ALT_TOOLTIP.startswith("LOS Floor map:")
        assert HTML.count("LOS Floor") > HTML.count("MIN_ALT")
        assert "Minimum LOS Altitude" not in HTML

    def test_antenna_floors_match_the_code(self):
        from waveshed.core.job_builder import (
            MIN_ANTENNA_AGL_M,
            MIN_ANTENNA_AMSL_M,
        )

        assert f"{MIN_ANTENNA_AGL_M:.1f} m" in TEXT or "1 m" in TEXT
        assert f"{MIN_ANTENNA_AMSL_M:.0f} m" in TEXT.replace("‑", "-")


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------

class TestPackaging:
    def test_help_package_ships_in_the_zip(self):
        import package as pkg

        arcnames = {
            arc for _abs, arc in pkg.iter_plugin_files(
                str(PLUGIN_ROOT / "waveshed"), str(PLUGIN_ROOT))
        }
        for name in ("__init__.py", "content.py"):
            rel = f"waveshed/help/{name}".replace("/", "\\") \
                if "\\" in next(iter(arcnames), "") else f"waveshed/help/{name}"
            assert rel in arcnames, (
                f"{rel} is missing from the plugin ZIP contents: the help "
                f"package would not be installed")
        assert any("gui/help_tab.py" in a.replace("\\", "/")
                   for a in arcnames), "the Help widget is not packaged"

    def test_help_files_are_not_classified_as_skipped(self):
        import package as pkg

        for rel in ("waveshed/help/__init__.py", "waveshed/help/content.py",
                    "waveshed/gui/help_tab.py"):
            assert not pkg.is_skipped_file(rel), rel
            assert not pkg.is_secret(rel), rel
            assert not pkg.is_binary_artifact(rel), rel
