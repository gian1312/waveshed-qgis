"""Credits for the data sources the plugin fetches at runtime.

Waveshed downloads third-party elevation and building data on the user's
behalf. Several of those sources carry an attribution requirement — ODbL in
particular — which is not satisfied by a licence file covering the plugin's own
code. This module is the single place those credits live, so the UI and the
packaged documentation cannot drift apart.

Only sources the plugin can actually request are listed. Nothing here describes
the Aether engine, which ships under its own EULA.
"""

from __future__ import annotations

from typing import List, NamedTuple


class DataSource(NamedTuple):
    """One fetchable dataset and the credit it requires."""

    name: str
    credit: str
    licence: str
    url: str


#: Elevation and building sources reachable from the plugin.
#:
#: Terrain entries cover the XYZ providers offered for the live download; the
#: building entry covers :mod:`waveshed.core.openfreemap`.
DATA_SOURCES: List[DataSource] = [
    DataSource(
        name="Mapzen / Terrarium terrain tiles",
        credit=(
            "© Mapzen, © OpenStreetMap contributors, and the source agencies "
            "listed by Mapzen (incl. SRTM, GMTED, NED, and national datasets)"
        ),
        licence="ODbL / mixed public-domain sources",
        url="https://github.com/tilezen/joerd/blob/master/docs/attribution.md",
    ),
    DataSource(
        name="OpenFreeMap building footprints",
        credit=(
            "© OpenStreetMap contributors, © OpenMapTiles, © OpenFreeMap"
        ),
        licence="ODbL",
        url="https://openfreemap.org",
    ),
    DataSource(
        name="Copernicus DEM",
        credit="Contains modified Copernicus data",
        licence="Copernicus licence",
        url="https://spacedata.copernicus.eu",
    ),
    DataSource(
        name="swissALTI3D / swissALTIRegio",
        credit="© swisstopo",
        licence="Swiss Open Government Data",
        url="https://www.swisstopo.admin.ch",
    ),
]


def attribution_lines() -> List[str]:
    """One ``name — credit (licence)`` line per source."""
    return [f"{s.name} — {s.credit} ({s.licence})" for s in DATA_SOURCES]


def attribution_text() -> str:
    """Plain-text credits block for dialogs and tooltips."""
    return "\n".join(attribution_lines())


def source_credit(name_fragment: str) -> str:
    """Credit line for the first source whose name contains *name_fragment*.

    Returns an empty string when nothing matches, so a caller can never render
    a partial or misleading credit.
    """
    frag = name_fragment.lower()
    for s in DATA_SOURCES:
        if frag in s.name.lower():
            return f"{s.credit} ({s.licence})"
    return ""
