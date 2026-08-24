#!/usr/bin/env python3
"""Build the Waveshed *torture-test* QGIS project from ``TEST_DATA_GUIDE.md``.

What this is
------------
``tests/TEST_DATA_GUIDE.md`` describes every dataset needed to challenge the
terrain and buildings pipeline, and how to wire each one into QGIS by hand.
Doing that by hand takes a day and is impossible to repeat identically. This
tool does it: it downloads the key-free sources, fabricates the derived
fixtures with GDAL, and writes one QGIS project whose layer tree *is* the
guide — one group per section, one layer per row, named by row number.

    python3 tools/make_torture_project.py                 # everything
    python3 tools/make_torture_project.py --stages project  # rebuild the .qgs only
    python3 tools/make_torture_project.py --list          # what would be built

Run it with a GDAL-equipped Python — QGIS's own Python or the OSGeo4W shell
(``python-qgis.bat``). GDAL (plus numpy, which QGIS ships) is needed by the
fabricate stage and by the Terrain-RGB re-encoder in the fetch stage; the
vectors and project stages are pure standard library, so a bare Python can
still refresh the project after you fill in an API key. Anything that cannot
run is skipped and reported, never half-built.

Configuration lives in ``torture.local.ini`` (gitignored; copy of
``torture.local.template.ini``). That file documents which API keys are needed
and where to get them — only three rows of the whole matrix need one.

Everything is written under ``data/torture/`` (gitignored), and every stage is
idempotent: existing downloads and fixtures are kept, the project is rewritten.
Rows whose input is missing (no API key, no GDAL feature, no swissALTIRegio
tiles) are skipped, listed on stdout and recorded in the generated
``data/torture/README.md`` — never silently dropped.

Deliberate omissions
--------------------
* The two broken-georeferencing fixtures are built into ``dem/broken/`` but are
  NOT added as project layers: QGIS hands a CRS-less raster the project CRS, so
  the plugin would pass a ``crs`` the file does not have and the converter's
  "add crs" hard error would never fire. Test them through the Map Converter's
  *folder* input, which passes the file paths straight through.
* Rendered-server rows whose QGIS URI is version-dependent (ArcGIS ImageServer)
  ship as a *connection* rather than a layer — see the generated
  ``qgis_console_bootstrap.py``.
"""

from __future__ import annotations

import argparse
import configparser
import gzip
import hashlib
import json
import math
import os
import shutil
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from xml.etree import ElementTree as ET

REPO = Path(__file__).resolve().parents[1]
USER_AGENT = "waveshed-torture-builder/1.0 (QGIS plugin test-data tool)"

# ---------------------------------------------------------------------------
# Fixed geography
# ---------------------------------------------------------------------------
# One AOI every provider covers, so a run over it can be compared across
# providers: AWS terrarium, Copernicus GLO-30, SRTM, the swissALTIRegio tiles,
# swisstopo WMS/WMTS and the OSM buildings all contain it. Bern, Switzerland.
#
# The box is bounded by the two *finite* sources, not by taste:
#   Copernicus tile N46_00_E007_00  ->  lon 7.000..8.000, lat 46.000..47.000
#   the four swissALTIRegio tiles   ->  lon 7.245..7.500, lat 46.900..47.075
# Clipping past either edge fills the gap with 0 m, which is indistinguishable
# from the silent provider failure the whole torture set exists to catch, so
# every clip is checked against _zero_fraction() after it is written.
AOI_W, AOI_S, AOI_E, AOI_N = 7.35, 46.90, 7.47, 46.99
# A larger box for the WGS84 partner in the mixed-CRS folder, so it *surrounds*
# the LV95 tile next to it instead of matching it.
HALO_W, HALO_S, HALO_E, HALO_N = 7.20, 46.87, 7.42, 46.999
# Central Bern — dense enough to make a real buildings fixture, small enough
# for Overpass to answer in seconds.
BLD_W, BLD_S, BLD_E, BLD_N = 7.42, 46.94, 7.46, 46.96
# The point every elevation-tile row is decoded at, well inside the AOI and
# inside the local fixture's tile coverage at every zoom it holds. Real ground
# here is 500-700 m; PROBE_BAND is the window a decode has to land in to count
# as terrain at all. It is deliberately wide — its job is to catch 0 m (flat
# sea, the silent provider failure), -10000 m (a black tile) and -32000 m (the
# wrong encoding), not to pin the metre.
PROBE_LAT, PROBE_LON = 46.945, 7.41
PROBE_BAND = (200.0, 2000.0)

TERRARIUM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
COPERNICUS_URL = ("https://copernicus-dem-30m.s3.amazonaws.com/"
                  "Copernicus_DSM_COG_10_{tile}_DEM/Copernicus_DSM_COG_10_{tile}_DEM.tif")
SKADI_URL = "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{lat}/{name}.hgt.gz"

COP_BERN = "N46_00_E007_00"
COP_DEADSEA = "N31_00_E035_00"
# Real ground for the two CRS the Bern AOI cannot reach: US survey feet
# (California) and Krovak (Czechia). A synthetic ramp would sit in the right
# place with the right units but no relief — and every interpolation of a plane
# is exact, so it could not expose a resampling or half-pixel error either.
COP_LA = "N34_00_W119_00"
COP_PRAGUE = "N50_00_E014_00"
#: ``fixture -> (source tile, EPSG, clip window as W N E S)``. The file names
#: carry the place, because the first version of these two was a synthetic ramp
#: under the bare name and a rebuild must never have to overwrite a file QGIS
#: still holds open — on Windows a loaded raster cannot be deleted.
FOREIGN_CRS_FIXTURES = {
    "feet_2229_losangeles.tif": (COP_LA, 2229, [-118.50, 34.20, -118.20, 34.00]),
    "krovak5514_prague.tif": (COP_PRAGUE, 5514, [14.30, 50.20, 14.60, 50.00]),
}
#: These two live in their own folder and are NOT project layers. EPSG:2229 and
#: EPSG:5514 are the only CRS here that publish more than one datum
#: transformation, so they are the only reason QGIS ever shows "Multiple
#: operations are possible ...". Worse, that dialog sorts by accuracy, so its
#: top entry for 2229 is a 2 m GRID-BASED operation: pick it without
#: us_noaa_cshpgn.tif installed and the transform fails and the layer draws
#: nothing. What these fixtures actually test — the converter reading a feet
#: geotransform, and Krovak failing cleanly at ingest — happens through the Map
#: Converter's folder input, where no QGIS transform is involved at all.
FOREIGN_CRS_DIR = "foreign_crs"
#: Superseded fixtures, removed on sight (best effort — see _drop_legacy).
LEGACY_FIXTURES = (
    "dem/variants/feet_2229.tif", "dem/variants/krovak5514.tif",
    "dem/variants/feet_2229.tif.aux.xml", "dem/variants/krovak5514.tif.aux.xml",
    # Renamed into foreign_crs/ and left behind under their new names too.
    "dem/variants/feet_2229_losangeles.tif", "dem/variants/krovak5514_prague.tif",
    "dem/variants/feet_2229_losangeles.tif.aux.xml",
    "dem/variants/krovak5514_prague.tif.aux.xml",
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Cfg:
    """Resolved configuration — every path absolute, every option typed."""

    maptiler_key: str = ""
    out_dir: Path = REPO / "data" / "torture"
    swissalti_tiles: Optional[Path] = None
    swissbuildings3d: Optional[Path] = None
    qgis_version: str = "3.34.0-Prizren"
    project_crs: str = "EPSG:4326"
    swiss_tiles: Tuple[str, ...] = ()
    terrain_rgb_zooms: Tuple[int, ...] = (10, 11, 12, 13)
    terrain_rgb_base_url: str = "http://localhost:8000/terrain-rgb"
    make_monolith: bool = True
    monolith_mb: int = 600
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    tile_server_autostart: bool = True
    qgis_exe: str = ""
    dem_stretch_min: float = 0.0
    dem_stretch_max: float = 3000.0
    pin_datum_transforms: bool = False
    suppress_datum_prompt: bool = True


def _resolve(value: str) -> Optional[Path]:
    value = (value or "").strip()
    if not value:
        return None
    p = Path(value)
    return p if p.is_absolute() else (REPO / p)


def load_config(path: Path) -> Cfg:
    """Read *path* (INI), falling back to the template, then to the defaults."""
    parser = configparser.ConfigParser()
    if path.is_file():
        parser.read(path, encoding="utf-8")
    else:
        print(f"[warn] {path.name} not found — using defaults (no API keys). "
              f"Copy torture.local.template.ini to {path.name} to configure.")

    keys = parser["keys"] if parser.has_section("keys") else {}
    paths = parser["paths"] if parser.has_section("paths") else {}
    opts = parser["options"] if parser.has_section("options") else {}

    def opt(name: str, default: str) -> str:
        return str(opts.get(name, default) or default).strip()

    out_dir = _resolve(paths.get("out_dir", "")) or (REPO / "data" / "torture")
    cfg = Cfg(
        maptiler_key=str(keys.get("maptiler_key", "") or "").strip(),
        out_dir=out_dir,
        swissalti_tiles=_resolve(paths.get("swissalti_tiles", "data/swissaltiregio_tiles")),
        swissbuildings3d=_resolve(paths.get("swissbuildings3d", "")),
        qgis_version=opt("qgis_version", "3.34.0-Prizren"),
        project_crs=opt("project_crs", "EPSG:4326"),
        swiss_tiles=tuple(t.strip() for t in opt(
            "swiss_tiles", "2585-1194,2595-1194,2585-1204,2595-1204").split(",") if t.strip()),
        terrain_rgb_zooms=tuple(int(z) for z in opt(
            "terrain_rgb_zooms", "10,11,12,13").split(",") if z.strip()),
        terrain_rgb_base_url=opt("terrain_rgb_base_url",
                                 "http://localhost:8000/terrain-rgb").rstrip("/"),
        make_monolith=opt("make_monolith", "true").lower() in ("1", "true", "yes", "on"),
        monolith_mb=int(opt("monolith_mb", "600")),
        overpass_url=opt("overpass_url", "https://overpass-api.de/api/interpreter"),
        tile_server_autostart=opt("tile_server_autostart", "true").lower()
        in ("1", "true", "yes", "on"),
        qgis_exe=str(paths.get("qgis_exe", "") or "").strip(),
        dem_stretch_min=float(opt("dem_stretch_min", "0")),
        dem_stretch_max=float(opt("dem_stretch_max", "3000")),
        pin_datum_transforms=opt("pin_datum_transforms", "false").lower()
        in ("1", "true", "yes", "on"),
        suppress_datum_prompt=opt("suppress_datum_prompt", "true").lower()
        in ("1", "true", "yes", "on"),
    )
    return cfg


# ---------------------------------------------------------------------------
# Reporting — every skipped row is recorded, never silently dropped
# ---------------------------------------------------------------------------

@dataclass
class Report:
    built: List[str] = field(default_factory=list)
    #: ``(what, why, by_design)``. *by_design* marks a row this build was never
    #: meant to produce — a deliberate exclusion or a switched-off option — as
    #: opposed to one it could not produce. tools/torture_runner.py counts only
    #: the latter against a run's completeness; otherwise the suite reports
    #: "incomplete" forever and people stop reading the exit code.
    skipped: List[Tuple[str, str, bool]] = field(default_factory=list)

    def ok(self, what: str) -> None:
        self.built.append(what)
        print(f"  [ok]   {what}")

    def skip(self, what: str, why: str, by_design: bool = False) -> None:
        self.skipped.append((what, why, by_design))
        print(f"  [skip] {what} — {why}{' (by design)' if by_design else ''}")


REPORT = Report()

#: Set once per run, so the project file, the macro and the render check all
#: report the SAME build. A one-element list because it is filled in later.
BUILD_STAMP = [""]


# ---------------------------------------------------------------------------
# HTTP + slippy-map helpers
# ---------------------------------------------------------------------------

def http_get(url: str, *, timeout: int = 120, data: Optional[bytes] = None) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def download(url: str, dest: Path, *, force: bool = False, timeout: int = 300) -> str:
    """Download *url* to *dest* unless it is already there. Returns ok/skip."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and dest.stat().st_size > 0 and not force:
        return "skip"
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh, 1 << 20)
    tmp.replace(dest)
    mb = dest.stat().st_size / 1e6
    print(f"    downloaded {dest.name} ({mb:.1f} MB, {time.perf_counter() - t0:.1f} s)")
    return "ok"


def deg2tile(lat: float, lon: float, zoom: int) -> Tuple[int, int]:
    """Slippy-map tile containing (*lat*, *lon*) at *zoom*."""
    n = 2.0 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return max(0, min(int(n) - 1, x)), max(0, min(int(n) - 1, y))


# ---------------------------------------------------------------------------
# GDAL access (lazy — the non-fabricate stages must run without it)
# ---------------------------------------------------------------------------

def _gdal():
    try:
        from osgeo import gdal, ogr, osr  # noqa: F401
    except ImportError:
        raise SystemExit(
            "[error] The fabricate stage needs GDAL. Run this with a "
            "GDAL-equipped Python — QGIS's Python or the OSGeo4W shell "
            "(python-qgis.bat) — or use --stages fetch,vectors,project."
        )
    gdal.UseExceptions()
    return gdal, ogr, osr


def _numpy():
    try:
        import numpy
    except ImportError:
        raise SystemExit("[error] The fabricate stage needs numpy (QGIS ships it).")
    return numpy


# ---------------------------------------------------------------------------
# Terrain-RGB re-encoder (guide row 1.3b)
# ---------------------------------------------------------------------------
# Terrarium and Terrain-RGB are two published encodings of the same thing.
#   terrarium:   elevation = (R*256 + G + B/256) - 32768
#   terrain-rgb: elevation = -10000 + 0.1 * (R*65536 + G*256 + B)
# Re-encoding real terrarium tiles gives a Terrain-RGB service that is
# deterministic, offline and free — and it is not circular, because the
# decoder under test implements the published formula independently in Rust.
# Spec checkpoints (from TEST_DATA_GUIDE.md): 0 m => (1,134,160),
# -10000 m => (0,0,0). Both are asserted before a single tile is written.

def _terrain_rgb_bytes(elev_m):
    np = _numpy()
    v = np.rint((elev_m + 10000.0) / 0.1)
    v = np.clip(v, 0, 2 ** 24 - 1).astype(np.uint32)
    r = ((v >> 16) & 0xFF).astype(np.uint8)
    g = ((v >> 8) & 0xFF).astype(np.uint8)
    b = (v & 0xFF).astype(np.uint8)
    return r, g, b


def _self_test_encoder() -> None:
    np = _numpy()
    r, g, b = _terrain_rgb_bytes(np.array([0.0, -10000.0]))
    got = [(int(r[i]), int(g[i]), int(b[i])) for i in range(2)]
    if got != [(1, 134, 160), (0, 0, 0)]:
        raise SystemExit(f"[error] terrain-rgb encoder self-test failed: {got}")


def _reencode_tile(png_bytes: bytes) -> bytes:
    """Terrarium PNG bytes -> Terrain-RGB PNG bytes (via GDAL /vsimem)."""
    gdal, _ogr, _osr = _gdal()
    np = _numpy()
    src_path = "/vsimem/terrarium_src.png"
    dst_path = "/vsimem/terrain_rgb_dst.png"
    gdal.FileFromMemBuffer(src_path, png_bytes)
    try:
        ds = gdal.Open(src_path)
        arr = ds.ReadAsArray().astype(np.float64)          # (bands, h, w)
        w, h = ds.RasterXSize, ds.RasterYSize
        ds = None
        elev = arr[0] * 256.0 + arr[1] + arr[2] / 256.0 - 32768.0
        r, g, b = _terrain_rgb_bytes(elev)
        mem = gdal.GetDriverByName("MEM").Create("", w, h, 3, gdal.GDT_Byte)
        for i, band in enumerate((r, g, b), start=1):
            mem.GetRasterBand(i).WriteArray(band)
        out_ds = gdal.GetDriverByName("PNG").CreateCopy(dst_path, mem)
        out_ds.FlushCache()
        out_ds = None
        mem = None
        stat = gdal.VSIStatL(dst_path)
        fh = gdal.VSIFOpenL(dst_path, "rb")
        out = gdal.VSIFReadL(1, stat.size, fh)
        gdal.VSIFCloseL(fh)
        return bytes(out)
    finally:
        for p in (src_path, dst_path):
            try:
                gdal.Unlink(p)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Stage: fetch
# ---------------------------------------------------------------------------

def stage_fetch(cfg: Cfg, force: bool = False) -> None:
    print("\n== fetch ==")
    out = cfg.out_dir

    for tile, label in ((COP_BERN, "Copernicus GLO-30, Bern"),
                        (COP_DEADSEA, "Copernicus GLO-30, Dead Sea"),
                        (COP_LA, "Copernicus GLO-30, Los Angeles (feet CRS)"),
                        (COP_PRAGUE, "Copernicus GLO-30, Prague (Krovak CRS)")):
        dest = out / "dem" / "source" / f"Copernicus_DSM_COG_10_{tile}_DEM.tif"
        try:
            download(COPERNICUS_URL.format(tile=tile), dest, force=force)
            REPORT.ok(f"{label} -> {dest.relative_to(out)}")
        except Exception as exc:
            REPORT.skip(label, f"download failed: {exc}")

    hgt = out / "dem" / "hgt" / "N46E007.hgt"
    if hgt.is_file() and not force:
        REPORT.ok("SRTM N46E007.hgt (cached)")
    else:
        try:
            gz = out / "dem" / "hgt" / "N46E007.hgt.gz"
            download(SKADI_URL.format(lat="N46", name="N46E007"), gz, force=force)
            with gzip.open(gz, "rb") as src, open(hgt, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            gz.unlink()
            REPORT.ok("SRTM N46E007.hgt (AWS skadi)")
        except Exception as exc:
            REPORT.skip("SRTM .hgt", f"download failed: {exc}")

    _fetch_terrain_rgb(cfg, force=force)
    _fetch_osm_buildings(cfg, force=force)


def _fetch_terrain_rgb(cfg: Cfg, force: bool = False) -> None:
    """Build the local, key-free Terrain-RGB fixture (guide row 1.3b)."""
    try:
        _gdal()
        _numpy()
    except SystemExit:
        REPORT.skip("local Terrain-RGB fixture (row 1.3b)",
                    "re-encoding tiles needs GDAL + numpy — re-run --stages fetch "
                    "with QGIS's Python; the rest of the fetch stage is done")
        return
    try:
        _self_test_encoder()
    except SystemExit:
        raise
    except Exception as exc:
        REPORT.skip("local Terrain-RGB fixture", f"encoder unavailable: {exc}")
        return

    root = cfg.out_dir / "terrain-rgb"
    made = cached = failed = 0
    wanted = []
    for z in cfg.terrain_rgb_zooms:
        x0, y0 = deg2tile(AOI_N, AOI_W, z)
        x1, y1 = deg2tile(AOI_S, AOI_E, z)
        wanted += [(z, x, y)
                   for x in range(min(x0, x1), max(x0, x1) + 1)
                   for y in range(min(y0, y1), max(y0, y1) + 1)]
    print(f"    Terrain-RGB fixture: {len(wanted)} tiles to check "
          f"(zooms {','.join(str(z) for z in cfg.terrain_rgb_zooms)})")
    seen = 0
    for z in cfg.terrain_rgb_zooms:
        x0, y0 = deg2tile(AOI_N, AOI_W, z)
        x1, y1 = deg2tile(AOI_S, AOI_E, z)
        for x in range(min(x0, x1), max(x0, x1) + 1):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                dest = root / str(z) / str(x) / f"{y}.png"
                seen += 1
                if seen % 10 == 0 or seen == len(wanted):
                    print(f"      {seen}/{len(wanted)} tiles "
                          f"({made} written, {cached} cached, {failed} failed)", flush=True)
                if dest.is_file() and not force:
                    cached += 1
                    continue
                try:
                    src = http_get(TERRARIUM_URL.format(z=z, x=x, y=y), timeout=60)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(_reencode_tile(src))
                    made += 1
                except SystemExit:
                    raise
                except Exception:
                    failed += 1
    # The same tiles under a name that matches nothing in known_services.json, so
    # a layer pointing here can only be decoded from its interpretation= token.
    for alias in ("neutral-tiles", "terrarium-terrain-rgb-mirror"):
        # neutral-tiles matches nothing in known_services.json (row 1.10);
        # terrarium-terrain-rgb-mirror matches BOTH families (row 1.5).
        target = cfg.out_dir / alias
        for src_tile in root.rglob("*.png"):
            dst_tile = target / src_tile.relative_to(root)
            if not dst_tile.exists():
                dst_tile.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_tile, dst_tile)

    if made or cached:
        REPORT.ok(f"local Terrain-RGB fixture: {made} tiles written, {cached} cached, "
                  f"{failed} failed (zooms {','.join(str(z) for z in cfg.terrain_rgb_zooms)})")
    else:
        REPORT.skip("local Terrain-RGB fixture", "no tiles could be fetched")


def _fetch_osm_buildings(cfg: Cfg, force: bool = False) -> None:
    dest = cfg.out_dir / "buildings" / "osm_bern.geojson"
    if dest.is_file() and not force:
        REPORT.ok("OSM buildings (cached)")
        return
    query = (f"[out:json][timeout:120];way[\"building\"]"
             f"({BLD_S},{BLD_W},{BLD_N},{BLD_E});out geom;")
    try:
        raw = http_get(cfg.overpass_url, data=urllib.parse.urlencode({"data": query}).encode(),
                       timeout=180)
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        REPORT.skip("OSM buildings (Overpass)", f"request failed: {exc}")
        return

    feats = []
    for el in payload.get("elements", []):
        geom = el.get("geometry")
        if el.get("type") != "way" or not geom or len(geom) < 4:
            continue
        ring = [[round(p["lon"], 7), round(p["lat"], 7)] for p in geom]
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        tags = el.get("tags", {})
        feats.append({
            "type": "Feature",
            "properties": {
                "osm_id": el.get("id"),
                "building": tags.get("building", "yes"),
                "height": tags.get("height", ""),
                "levels": tags.get("building:levels", ""),
                "name": tags.get("name", ""),
            },
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        })
    if not feats:
        REPORT.skip("OSM buildings (Overpass)", "no building ways returned")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps({"type": "FeatureCollection", "features": feats}),
                    encoding="utf-8")
    REPORT.ok(f"OSM buildings: {len(feats)} polygons -> {dest.name}")


# ---------------------------------------------------------------------------
# Stage: fabricate (GDAL)
# ---------------------------------------------------------------------------

def stage_fabricate(cfg: Cfg, force: bool = False) -> None:
    print("\n== fabricate ==")
    gdal, _ogr, _osr = _gdal()
    out = cfg.out_dir
    src_bern = out / "dem" / "source" / f"Copernicus_DSM_COG_10_{COP_BERN}_DEM.tif"
    if not src_bern.is_file():
        # Only the DERIVED DEM fixtures need this tile. Returning here also
        # skipped the swissALTIRegio tiles and every buildings fixture — rows
        # 2.8, 2.9, 3.2a, 3.2b, 3.5 and 3.6 vanished from the project under a
        # message that mentions neither, which reads as "one download failed"
        # and is in fact "a third of the catalogue is gone".
        REPORT.skip("all derived DEM fixtures (rows 2.2-2.7, 2.10-2.23)",
                    "Copernicus source tile missing — run --stages fetch")

        def fresh_only(path: Path) -> bool:
            return force or not path.is_file() or path.stat().st_size == 0

        (out / "dem" / "swiss").mkdir(parents=True, exist_ok=True)
        _swiss_tiles(cfg, fresh_only)      # LV95 tiles, rows 2.8/2.9 — own source
        _drop_legacy(cfg)
        _buildings(cfg, fresh_only)        # rows 3.2a/3.2b/3.5/3.6 — own source
        return

    base_dir = out / "dem" / "base"
    var = out / "dem" / "variants"
    for d in (base_dir, var, out / "dem" / "mixed_crs", out / "dem" / "overlap",
              out / "dem" / "nested" / "sub", out / "dem" / "broken",
              out / "dem" / "swiss", out / "dem" / "zip", out / "dem" / "big"):
        d.mkdir(parents=True, exist_ok=True)

    def fresh(path: Path) -> bool:
        return force or not path.is_file() or path.stat().st_size == 0

    # --- baseline: a small Float32 WGS84 clip every variant derives from ----
    base = base_dir / "base_wgs84.tif"
    if fresh(base):
        gdal.Translate(str(base), str(src_bern), options=gdal.TranslateOptions(
            format="GTiff", projWin=[AOI_W, AOI_N, AOI_E, AOI_S],
            outputType=gdal.GDT_Float32))
    _check_no_void_fill(out, base)
    REPORT.ok(f"base_wgs84.tif ({_shape(base)})")

    halo = base_dir / "halo_wgs84.tif"
    if fresh(halo):
        gdal.Translate(str(halo), str(src_bern), options=gdal.TranslateOptions(
            format="GTiff", projWin=[HALO_W, HALO_N, HALO_E, HALO_S],
            outputType=gdal.GDT_Float32))
    _check_no_void_fill(out, halo)
    REPORT.ok("halo_wgs84.tif (surrounds the LV95 tile in mixed_crs/)")

    base_i16 = base_dir / "base_i16.tif"
    if fresh(base_i16):
        gdal.Translate(str(base_i16), str(base), options=gdal.TranslateOptions(
            format="GTiff", outputType=gdal.GDT_Int16))
    REPORT.ok("base_i16.tif")

    # --- Dead Sea (negative elevations end to end) --------------------------
    src_ds = out / "dem" / "source" / f"Copernicus_DSM_COG_10_{COP_DEADSEA}_DEM.tif"
    dead = base_dir / "deadsea_wgs84.tif"
    if src_ds.is_file():
        if fresh(dead):
            gdal.Translate(str(dead), str(src_ds), options=gdal.TranslateOptions(
                format="GTiff", projWin=[35.35, 31.60, 35.65, 31.35],
                outputType=gdal.GDT_Float32))
        _check_no_void_fill(out, dead)
        REPORT.ok("deadsea_wgs84.tif (negative elevations)")
    else:
        REPORT.skip("deadsea_wgs84.tif", "Dead Sea source tile missing")

    # --- projected-CRS variants --------------------------------------------
    # dstNodata matters: reprojecting a lat/lon box into a projected CRS leaves
    # wedges outside the source, and gdalwarp fills those with 0 by default.
    # A fixture with built-in 0 m corners is poison for a test set whose whole
    # job is spotting silent 0 m terrain, so the outside is declared nodata.
    for name, epsg in (("utm32.tif", 32632), ("merc3857.tif", 3857), ("lv95_2056.tif", 2056)):
        dst = var / name
        if fresh(dst):
            gdal.Warp(str(dst), str(base), options=gdal.WarpOptions(
                dstSRS=f"EPSG:{epsg}", resampleAlg="near", format="GTiff",
                dstNodata=-9999.0))
        _check_no_void_fill(out, dst)
        REPORT.ok(f"{name} (EPSG:{epsg}, outside-source = -9999 nodata)")

    # --- synthetic rasters in CRS the AOI cannot reach ----------------------
    # Krovak (5514) covers Czechia and US survey feet (2229) covers California,
    # so warping Swiss ground into them would be out of domain and meaningless.
    # What these two fixtures test is CRS handling, not elevation values, so a
    # deterministic synthetic surface in the right place is the honest fixture.
    foreign = out / "dem" / FOREIGN_CRS_DIR
    foreign.mkdir(parents=True, exist_ok=True)
    for name, (tile, epsg, window) in FOREIGN_CRS_FIXTURES.items():
        dst = foreign / name
        src = out / "dem" / "source" / f"Copernicus_DSM_COG_10_{tile}_DEM.tif"
        if not src.is_file():
            REPORT.skip(name, f"source tile {tile} missing — run --stages fetch")
            continue
        if fresh(dst):
            try:
                clip = foreign / f"_clip_{name}"
                gdal.Translate(str(clip), str(src), options=gdal.TranslateOptions(
                    format="GTiff", projWin=window, outputType=gdal.GDT_Float32))
                gdal.Warp(str(dst), str(clip), options=gdal.WarpOptions(
                    dstSRS=f"EPSG:{epsg}", resampleAlg="near", format="GTiff",
                    dstNodata=-9999.0))
                clip.unlink()
            except Exception as exc:
                REPORT.skip(name, f"could not write it ({exc}). If the torture "
                                  f"project is open in QGIS, close it and re-run — "
                                  f"a loaded raster cannot be replaced.")
                continue
        _check_no_void_fill(out, dst)
        REPORT.ok(f"{FOREIGN_CRS_DIR}/{name} (EPSG:{epsg}, real Copernicus terrain over "
                  f"{'Los Angeles' if epsg == 2229 else 'Prague'}) — folder input, not a layer")

    # --- nodata variants, with real voids punched in ------------------------
    _nodata_variant(var / "f32_nan.tif", base, gdal.GDT_Float32, float("nan"), fresh)
    REPORT.ok("f32_nan.tif (NaN voids — must read as -9999, never 0 m)")
    _nodata_variant(var / "i16_nodata.tif", base, gdal.GDT_Int16, -32768, fresh)
    REPORT.ok("i16_nodata.tif")
    _nodata_variant(var / "i32_nodata.tif", base, gdal.GDT_Int32, -2147483648, fresh)
    REPORT.ok("i32_nodata.tif (low 16 bits of the nodata value are zero)")

    # --- compression / layout torture (the converter reads these directly) --
    for name, kwargs in (
        ("deflate.tif", dict(creationOptions=["COMPRESS=DEFLATE"])),
        ("tiled.tif", dict(creationOptions=["TILED=YES"])),
        ("cog.tif", dict(format="COG", creationOptions=["COMPRESS=DEFLATE"])),
        ("zstd_cog.tif", dict(format="COG", creationOptions=["COMPRESS=ZSTD"])),
    ):
        dst = var / name
        if fresh(dst):
            try:
                gdal.Translate(str(dst), str(base), options=gdal.TranslateOptions(
                    format=kwargs.get("format", "GTiff"),
                    creationOptions=kwargs.get("creationOptions", [])))
            except Exception as exc:
                REPORT.skip(name, f"GDAL cannot create it here: {exc}")
                continue
        REPORT.ok(name)

    # --- exotic format -> materialize path ----------------------------------
    ascii_dem = var / "ascii.dem"
    if fresh(ascii_dem):
        try:
            gdal.Translate(str(ascii_dem), str(base), options=gdal.TranslateOptions(
                format="USGSDEM"))
        except Exception as exc:
            REPORT.skip("ascii.dem (USGSDEM)", f"driver refused the source: {exc}")
    if ascii_dem.is_file():
        REPORT.ok("ascii.dem (USGSDEM — non-TIFF, materialize helper path)")

    # --- more non-TIFF formats: the materialize path is the thinnest part ---
    # The folder scanner takes .tif/.tiff/.dem/.hgt only, so these are layer-only
    # inputs and every one of them goes through materialize_for_converter.
    for name, driver, opts in (("erdas.img", "HFA", []),
                               ("bil/base.bil", "EHdr", []),
                               ("ignored_formats/base.asc", "AAIGrid", [])):
        dst = var / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if fresh(dst):
            try:
                gdal.Translate(str(dst), str(base), options=gdal.TranslateOptions(
                    format=driver, creationOptions=opts))
            except Exception as exc:
                REPORT.skip(name, f"{driver} driver refused it: {exc}")
                continue
        REPORT.ok(f"{name} ({driver} — layer-only input, materialize path)")

    # A file that is a GeoTIFF right up until it is not. Nothing should ever
    # silently skip it.
    truncated = out / "dem" / "broken" / "truncated.tif"
    if fresh(truncated):
        data = base.read_bytes()
        truncated.write_bytes(data[: int(len(data) * 0.4)])
    REPORT.ok("broken/truncated.tif (40 % of a GeoTIFF — must fail loudly, never skip)")

    # --- broken georeferencing (folder-input tests only) --------------------
    nocrs = out / "dem" / "broken" / "broken_nocrs.tif"
    nogt = out / "dem" / "broken" / "broken_nogt.tif"
    if fresh(nocrs):
        shutil.copy2(base, nocrs)
        ds = gdal.Open(str(nocrs), gdal.GA_Update)
        ds.SetProjection("")
        ds = None
    REPORT.ok("broken/broken_nocrs.tif (folder input -> converter must say: add \"crs\")")
    if fresh(nogt):
        shutil.copy2(base, nogt)
        ds = gdal.Open(str(nogt), gdal.GA_Update)
        wkt = ds.GetProjection()
        ds.SetGeoTransform((0.0, 1.0, 0.0, 0.0, 0.0, 1.0))
        ds.SetProjection(wkt)   # keep the CRS: the missing georeferencing is the test
        ds = None
    REPORT.ok("broken/broken_nogt.tif (folder input -> hard error, no filename georeferencing)")

    # --- swissALTIRegio LV95 tiles -----------------------------------------
    _swiss_tiles(cfg, fresh)

    # --- folder cases -------------------------------------------------------
    swiss_dir = out / "dem" / "swiss"
    swiss_tifs = sorted(swiss_dir.glob("*.tif"))
    mixed = out / "dem" / "mixed_crs"
    if swiss_tifs:
        target = mixed / swiss_tifs[0].name
        if fresh(target):
            shutil.copy2(swiss_tifs[0], target)
        if fresh(mixed / "halo_wgs84.tif"):
            shutil.copy2(halo, mixed / "halo_wgs84.tif")
        REPORT.ok("mixed_crs/ (one LV95 tile + one WGS84 tile in ONE folder)")

        vrt = out / "dem" / "mosaic.vrt"
        if fresh(vrt):
            ds = gdal.BuildVRT(str(vrt), [str(p) for p in swiss_tifs])
            ds.FlushCache()
            ds = None
        REPORT.ok(f"mosaic.vrt (over {len(swiss_tifs)} LV95 tiles — layer only, never scanned in a folder)")
    else:
        REPORT.skip("mixed_crs/ and mosaic.vrt", "no swissALTIRegio tiles converted")

    ov_a = out / "dem" / "overlap" / "a_base.tif"
    ov_b = out / "dem" / "overlap" / "b_base_plus50.tif"
    if fresh(ov_a):
        shutil.copy2(base, ov_a)
    if fresh(ov_b):
        shutil.copy2(base, ov_b)
        ds = gdal.Open(str(ov_b), gdal.GA_Update)
        band = ds.GetRasterBand(1)
        band.WriteArray(band.ReadAsArray() + 50.0)
        ds = None
    REPORT.ok("overlap/ (a_base wins by sort order on every machine; b is +50 m)")

    nested_top = out / "dem" / "nested" / "top_west.tif"
    nested_sub = out / "dem" / "nested" / "sub" / "sub_east.tif"
    mid_lon = (AOI_W + AOI_E) / 2.0
    if fresh(nested_top):
        gdal.Translate(str(nested_top), str(base), options=gdal.TranslateOptions(
            format="GTiff", projWin=[AOI_W, AOI_N, mid_lon, AOI_S]))
    if fresh(nested_sub):
        gdal.Translate(str(nested_sub), str(base), options=gdal.TranslateOptions(
            format="GTiff", projWin=[mid_lon, AOI_N, AOI_E, AOI_S]))
    REPORT.ok("nested/ (west half at top level, east half in sub/ — both must be found)")

    # --- /vsizip fixture ----------------------------------------------------
    zip_path = out / "dem" / "zip" / "dem_in_zip.zip"
    if fresh(zip_path):
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(base, arcname="base_wgs84.tif")
    REPORT.ok("zip/dem_in_zip.zip (/vsizip prefix handling)")

    # --- >512 MB monolith ---------------------------------------------------
    monolith = out / "dem" / "big" / "monolith.tif"
    if cfg.make_monolith:
        if fresh(monolith):
            ds = gdal.Open(str(base))
            cols, rows = ds.RasterXSize, ds.RasterYSize
            ds = None
            scale = math.sqrt((cfg.monolith_mb * 1e6) / (cols * rows * 4.0))
            w, h = int(cols * scale), int(rows * scale)
            print(f"    building {w}x{h} Float32 monolith (~{w * h * 4 / 1e6:.0f} MB)…")
            gdal.Translate(str(monolith), str(base), options=gdal.TranslateOptions(
                format="GTiff", width=w, height=h, resampleAlg="near",
                outputType=gdal.GDT_Float32))
        REPORT.ok(f"big/monolith.tif ({monolith.stat().st_size / 1e6:.0f} MB > 512 MB "
                  f"-> window-copy materialize path)")
    else:
        REPORT.skip("big/monolith.tif", "make_monolith = false in torture.local.ini",
                    by_design=True)

    _drop_legacy(cfg)
    _buildings(cfg, fresh)


def _drop_legacy(cfg: Cfg) -> None:
    """Remove fixtures a later version replaced, if the OS lets us.

    On Windows a raster loaded in QGIS cannot be deleted, and that is not worth
    failing a build over — the file is simply no longer referenced by anything.
    """
    for rel_path in LEGACY_FIXTURES:
        stale = cfg.out_dir / rel_path
        if not stale.exists():
            continue
        try:
            stale.unlink()
            REPORT.ok(f"removed superseded fixture {rel_path}")
        except OSError:
            REPORT.skip(f"superseded fixture {rel_path}",
                        "could not be deleted (open in QGIS?) — nothing references "
                        "it any more, delete it by hand when convenient")


#: Fixtures that failed the 0 m check, kept next to the project so the finding
#: survives a later ``--stages project`` run. A row built on one of these gets a
#: ``known_fail`` rather than shipping as if it were sound.
SUSPECT_FIXTURES_FILE = ".suspect_fixtures.json"


def _suspect_fixtures(out_dir: Path) -> Dict[str, str]:
    try:
        return json.loads((out_dir / SUSPECT_FIXTURES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _check_no_void_fill(out_dir: Path, path: Path, limit: float = 0.005) -> None:
    """Record loudly if a clip contains a suspicious amount of exact 0 m.

    A ``projWin`` that reaches past the source tile is filled with zeros by
    GDAL, and a fixture with a 0 m band in it is worse than no fixture at all:
    it looks exactly like the silent provider failure these tests hunt for.

    Warning alone was not enough: the caller reported ``[ok]`` on the next line
    and the row entered the catalogue with ``expect_renders=True``, so the
    suspect fixture passed the automated run. The finding is written down
    instead, and ``build_catalogue`` turns it into the row's ``known_fail``.
    """
    gdal, _o, _s = _gdal()
    ds = gdal.Open(str(path))
    arr = ds.GetRasterBand(1).ReadAsArray()
    ds = None
    frac = float((arr == 0).mean())
    suspects = _suspect_fixtures(out_dir)
    key = path.name
    if frac > limit:
        why = (f"{frac * 100:.1f}% of its pixels are exactly 0 m — the clip window "
               f"probably reaches past the source tile, and a 0 m band looks exactly "
               f"like the silent provider failure this set hunts for. Fix the AOI "
               f"constants in tools/make_torture_project.py and rebuild with --force.")
        REPORT.skip(f"{path.name} 0 m check", why)
        suspects[key] = why
    else:
        suspects.pop(key, None)
    try:
        (out_dir / SUSPECT_FIXTURES_FILE).write_text(
            json.dumps(suspects, indent=1), encoding="utf-8")
    except OSError:
        pass


def _shape(path: Path) -> str:
    gdal, _o, _s = _gdal()
    ds = gdal.Open(str(path))
    txt = f"{ds.RasterXSize}x{ds.RasterYSize} {gdal.GetDataTypeName(ds.GetRasterBand(1).DataType)}"
    ds = None
    return txt


def _synthetic(dst: Path, epsg: int, x0: float, y0: float, pixel: float,
               fresh, *, note: str = "") -> None:
    """A small deterministic ramp raster in *epsg*, 1000x1000 px."""
    if not fresh(dst):
        return
    gdal, _ogr, osr = _gdal()
    np = _numpy()
    n = 1000
    ds = gdal.GetDriverByName("GTiff").Create(str(dst), n, n, 1, gdal.GDT_Float32)
    ds.SetGeoTransform((x0, pixel, 0.0, y0, 0.0, -pixel))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(epsg)
    ds.SetProjection(srs.ExportToWkt())
    yy, xx = np.mgrid[0:n, 0:n]
    ds.GetRasterBand(1).WriteArray(200.0 + xx * 0.2 + yy * 0.1)
    ds.GetRasterBand(1).SetDescription(f"synthetic ramp, {note}")
    ds = None


def _nodata_variant(dst: Path, base: Path, dtype, nodata, fresh) -> None:
    """Copy *base* to *dst* in *dtype* with *nodata* set AND a void punched in."""
    if not fresh(dst):
        return
    gdal, _ogr, _osr = _gdal()
    gdal.Translate(str(dst), str(base), options=gdal.TranslateOptions(
        format="GTiff", outputType=dtype, noData=nodata))
    ds = gdal.Open(str(dst), gdal.GA_Update)
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray()
    h, w = arr.shape
    arr[h // 3: h // 3 + h // 6, w // 3: w // 3 + w // 6] = nodata
    band.WriteArray(arr)
    band.SetNoDataValue(float(nodata))
    ds = None


def _swiss_tiles(cfg: Cfg, fresh) -> None:
    """Convert the configured swissALTIRegio .xyz.zip tiles to LV95 GeoTIFFs."""
    src_dir = cfg.swissalti_tiles
    if not src_dir or not src_dir.is_dir():
        REPORT.skip("swissALTIRegio LV95 tiles", "swissalti_tiles path not found")
        return
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from xyz_to_geotiff import convert_one
    except Exception as exc:
        REPORT.skip("swissALTIRegio LV95 tiles", f"xyz_to_geotiff unavailable: {exc}")
        return

    out_dir = cfg.out_dir / "dem" / "swiss"
    done = 0
    total = len(cfg.swiss_tiles)
    for i, tile in enumerate(cfg.swiss_tiles, start=1):
        matches = sorted(src_dir.glob(f"swissaltiregio_{tile}_*.xyz.zip"))
        if not matches:
            REPORT.skip(f"swiss tile {tile}", "not present in swissalti_tiles")
            continue
        print(f"    swissALTIRegio {i}/{total}: {tile} "
              f"(xyz -> GeoTIFF, ~20 s unless already converted)…", flush=True)
        name, status, detail = convert_one(str(matches[0]), str(out_dir))
        if status == "fail":
            REPORT.skip(f"swiss tile {tile}", detail)
        else:
            done += 1
    if done:
        REPORT.ok(f"swissALTIRegio: {done} LV95 (EPSG:2056) tiles in dem/swiss/")


def _buildings(cfg: Cfg, fresh) -> None:
    """Derive the buildings fixtures from the OSM GeoJSON (guide §3)."""
    gdal, ogr, _osr = _gdal()
    src = cfg.out_dir / "buildings" / "osm_bern.geojson"
    if not src.is_file():
        REPORT.skip("buildings fixtures", "osm_bern.geojson missing — run --stages fetch")
        return
    bdir = cfg.out_dir / "buildings"

    # 3.2 multi-layer GPKG. The two layers are disjoint halves of the same
    # city block, so a run that silently used the FIRST layer instead of the
    # selected one is visible on the map, not just in a log line.
    multi = bdir / "multi.gpkg"
    if fresh(multi):
        if multi.exists():
            multi.unlink()
        mid = (BLD_W + BLD_E) / 2.0
        gdal.VectorTranslate(str(multi), str(src), options=gdal.VectorTranslateOptions(
            format="GPKG", layerName="buildings", spatFilter=[BLD_W, BLD_S, mid, BLD_N]))
        gdal.VectorTranslate(str(multi), str(src), options=gdal.VectorTranslateOptions(
            format="GPKG", layerName="other_layer", accessMode="update",
            spatFilter=[mid, BLD_S, BLD_E, BLD_N]))
    REPORT.ok("buildings/multi.gpkg (layers: buildings = west half, other_layer = east half)")

    lv95 = bdir / "lv95_buildings.shp"
    if fresh(lv95):
        gdal.VectorTranslate(str(lv95), str(src), options=gdal.VectorTranslateOptions(
            format="ESRI Shapefile", dstSRS="EPSG:2056", reproject=True))
    REPORT.ok("buildings/lv95_buildings.shp (EPSG:2056 — reprojection-on-conversion)")

    fgb = bdir / "direct.fgb"
    if fresh(fgb):
        gdal.VectorTranslate(str(fgb), str(src), options=gdal.VectorTranslateOptions(
            format="FlatGeobuf", layerName="direct"))
    REPORT.ok("buildings/direct.fgb (must be passed through, not re-converted)")


# ---------------------------------------------------------------------------
# Stage: vectors (pure stdlib — run matrix, sites, links, batch CSVs)
# ---------------------------------------------------------------------------

RUN_MATRIX: Sequence[Dict[str, Any]] = (
    {"name": "Bern reference AOI", "west": AOI_W, "south": AOI_S, "east": AOI_E, "north": AOI_N,
     "res_m": "10 / 30", "stresses": "The one box every provider covers — cross-provider agreement."},
    {"name": "Andes", "west": -71.2, "south": -34.9, "east": -70.6, "north": -34.5,
     "res_m": "30", "stresses": "Negative coordinates through plan (clap regression)."},
    {"name": "London", "west": -0.3, "south": 51.3, "east": 0.4, "north": 51.7,
     "res_m": "30", "stresses": "Prime-meridian sign crossing."},
    {"name": "Zurich 2 m snap grid", "west": 8.21, "south": 47.35, "east": 8.77, "north": 47.62,
     "res_m": "2 (+30)", "stresses": "The 0.1 deg snap grid (double-snap regression)."},
    {"name": "Norway", "west": 10.4, "south": 59.9, "east": 11.1, "north": 60.3,
     "res_m": "5 + 90", "stresses": "cos(lat) halo; check tile seams at the edges."},
    {"name": "Dead Sea", "west": 35.35, "south": 31.35, "east": 35.65, "north": 31.6,
     "res_m": "30", "stresses": "Negative elevations; -430 m AMSL site entry."},
    {"name": "One tile", "west": 7.40, "south": 46.94, "east": 7.45, "north": 46.99,
     "res_m": "30", "stresses": "Minimal case."},
    # must_fail: the engine has to REFUSE this case. Emitted to the manifest so
    # tools/torture_runner.py asserts it in that direction — it used to expect
    # exit 0 from every case, so a correctly-refusing engine was reported as a
    # failure and an engine that planned half a continent was reported as a pass.
    {"name": "Huge (must be refused)", "west": -10.0, "south": 35.0, "east": 30.0, "north": 60.0,
     "res_m": "2", "refused_by": "plugin",
     "stresses": "Size warning / 2 M-tile cap error — must never hang. The refusal is "
                 "the PLUGIN's (terrain_size_warning); `plan` is pure geometry and has "
                 "no size policy, so asserting a plan failure asserted it of the wrong "
                 "component."},
    # 179.6 -> 180.0 touches the antimeridian without crossing it, so the case
    # named for the crossing never tested one. It straddles now.
    {"name": "Fiji antimeridian", "west": 179.6, "south": -18.3, "east": 180.4, "north": -17.9,
     "res_m": "30", "must_fail": True,
     "stresses": "Antimeridian unsupported — must fail loudly, not wrap."},
    {"name": "NRW (WCS coverage)", "west": 6.9, "south": 51.2, "east": 7.3, "north": 51.5,
     "res_m": "30", "stresses": "EPSG:25832 end-to-end through the WCS provider (row 4.3)."},
    {"name": "Colorado (3DEP)", "west": -105.5, "south": 39.5, "east": -105.0, "north": 40.0,
     "res_m": "30", "stresses": "The only ground rows 4.4a/4.4b cover — 3DEP is USA-only."},
    {"name": "Los Angeles (feet CRS)", "west": -118.5, "south": 34.0, "east": -118.2,
     "north": 34.2, "res_m": "30",
     "stresses": "Row 2.19: EPSG:2229, US survey feet, geotransform in feet."},
    {"name": "Prague (Krovak)", "west": 14.3, "south": 50.0, "east": 14.6, "north": 50.2,
     "res_m": "30", "stresses": "Row 2.20: EPSG:5514 — must hard-error, cleanly."},
)

SITES: Sequence[Dict[str, Any]] = (
    {"name": "Bern TX", "lat": 46.9481, "lon": 7.4474, "height_m": 30.0, "mode": "AGL",
     "note": "Primary transmitter for the reference AOI."},
    {"name": "Thun RX", "lat": 46.7580, "lon": 7.6280, "height_m": 10.0, "mode": "AGL",
     "note": "P2P partner, 24 km, terrain-obstructed."},
    {"name": "Gurten RX", "lat": 46.9200, "lon": 7.4370, "height_m": 5.0, "mode": "AGL",
     "note": "P2P partner, 3 km, clear LOS."},
    {"name": "Dead Sea shore", "lat": 31.5000, "lon": 35.5000, "height_m": -430.0, "mode": "AMSL",
     "note": "Negative AMSL must be accepted; the AGL floor stays untouched."},
    {"name": "Andes site", "lat": -34.7000, "lon": -70.9000, "height_m": 30.0, "mode": "AGL",
     "note": "Negative lat and lon."},
    {"name": "Fiji site", "lat": -18.1000, "lon": 179.8000, "height_m": 30.0, "mode": "AGL",
     "note": "Antimeridian."},
)

P2P_LINKS: Sequence[Dict[str, Any]] = (
    {"name": "Bern -> Thun (obstructed)", "a": (7.4474, 46.9481), "b": (7.6280, 46.7580)},
    {"name": "Bern -> Gurten (clear)", "a": (7.4474, 46.9481), "b": (7.4370, 46.9200)},
    {"name": "Dead Sea -> Masada (negative AMSL)", "a": (35.5000, 31.5000), "b": (35.3536, 31.3156)},
)


def stage_vectors(cfg: Cfg, force: bool = False) -> None:
    print("\n== vectors + batch CSVs ==")
    vdir = cfg.out_dir / "vectors"
    vdir.mkdir(parents=True, exist_ok=True)

    feats = []
    for case in RUN_MATRIX:
        w, s, e, n = case["west"], case["south"], case["east"], case["north"]
        feats.append({
            "type": "Feature",
            "properties": {k: case[k] for k in ("name", "res_m", "stresses")},
            "geometry": {"type": "Polygon", "coordinates": [[
                [w, s], [e, s], [e, n], [w, n], [w, s]]]},
        })
    (vdir / "run_matrix.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "features": feats}, indent=1), encoding="utf-8")
    REPORT.ok(f"vectors/run_matrix.geojson ({len(feats)} cases from guide §5)")

    feats = [{
        "type": "Feature",
        "properties": {k: st[k] for k in ("name", "height_m", "mode", "note")},
        "geometry": {"type": "Point", "coordinates": [st["lon"], st["lat"]]},
    } for st in SITES]
    (vdir / "sites.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "features": feats}, indent=1), encoding="utf-8")
    REPORT.ok(f"vectors/sites.geojson ({len(feats)} sites)")

    feats = [{
        "type": "Feature",
        "properties": {"name": lk["name"]},
        "geometry": {"type": "LineString", "coordinates": [list(lk["a"]), list(lk["b"])]},
    } for lk in P2P_LINKS]
    (vdir / "p2p_links.geojson").write_text(json.dumps(
        {"type": "FeatureCollection", "features": feats}, indent=1), encoding="utf-8")
    REPORT.ok(f"vectors/p2p_links.geojson ({len(feats)} links)")

    _batch_csvs(cfg)


def _batch_csvs(cfg: Cfg) -> None:
    """The BATCH_P2P drills of guide §6 — one file per expected outcome.

    The parser (``p2p_tab._parse_batch_csv``) takes 6 fields, ``S``/``R``,
    lat, lon, altitude, ``AGL``/``AMSL``, and rejects by line number — so each
    rejection case is its own file with the bad row on a known line.
    """
    bdir = cfg.out_dir / "batch"
    bdir.mkdir(parents=True, exist_ok=True)
    header = "# type,id,lat,lon,altitude_m,mode\n"

    files = {
        "batch_ok.csv": (header +
                         "S,bern_tx,46.9481,7.4474,30.0,AGL\n"
                         "R,thun_rx,46.7580,7.6280,10.0,AGL\n"
                         "R,gurten_rx,46.9200,7.4370,5.0,AGL\n"),
        "batch_amsl_negative_ok.csv": (header +
                                       "S,dead_sea_tx,31.5000,35.5000,-430.0,AMSL\n"
                                       "R,masada_rx,31.3156,35.3536,-380.0,AMSL\n"),
        "batch_reject_agl_half_metre.csv": (header +
                                            "S,bern_tx,46.9481,7.4474,30.0,AGL\n"
                                            "R,too_low,46.7580,7.6280,0.5,AGL\n"),
        "batch_reject_non_numeric.csv": (header +
                                         "S,bern_tx,46.9481,7.4474,30.0,AGL\n"
                                         "R,bad_alt,46.7580,7.6280,thirty,AGL\n"),
        "batch_reject_bad_mode.csv": (header +
                                      "S,bern_tx,46.9481,7.4474,30.0,AGL\n"
                                      "R,bad_mode,46.7580,7.6280,10.0,ASL\n"),
    }
    for name, text in files.items():
        (bdir / name).write_text(text, encoding="utf-8")
    REPORT.ok(f"batch/ ({len(files)} CSVs: 2 accepted, 3 rejected by line number)")


# ---------------------------------------------------------------------------
# Layer catalogue
# ---------------------------------------------------------------------------

@dataclass
class Layer:
    row: str                    # guide row, e.g. "1.1"
    group: str
    name: str
    provider: str               # "wms" | "wcs" | "gdal" | "ogr"
    source: str
    crs: str = ""
    kind: str = "raster"        # "raster" | "vector"
    geometry: str = ""          # vector only
    expect: str = ""
    checked: bool = False
    style: str = ""             # "" | "fill" | "point" | "line"
    color: str = "227,26,28,255"
    # How the checklist wants this row exercised:
    #   both      run it through Site Analysis AND the Map Converter
    #   error     same, but the run must fail loudly instead of producing terrain
    #   reject    never run: the plugin must refuse to offer it as a DEM
    #   buildings a coverage run with buildings on, plus a converter run
    #   none      reference geometry, nothing to run
    check: str = "both"
    # How to draw it: "values" = single-band grey + stretch (elevation),
    # "picture" = colour-data (a rendered image), "" = decide from the file's
    # own statistics. A service layer has no statistics to decide from, which
    # is why it has to be said here.
    render: str = ""
    # Smallest map scale (largest denominator) at which the layer draws. Some
    # coverage services refuse a request for their whole extent, so the fix is
    # to stop QGIS asking for it.
    min_scale: float = 0.0
    # Machine-checkable expectations, consumed by tools/torture_runner.py.
    expect_class: str = ""       # "dem" | "imagery"  (classify_raster_layer)
    expect_encoding: str = ""    # "terrarium" | "mapbox" | "error"
    expect_zmax: int = 0         # effective zmax after resolve_zmax() clamping
    expect_renders: bool = True  # False for the rows that must draw nothing
    # The image format the service must actually serve, by magic bytes — not by
    # Content-Type and not by the URL's extension, because row 1.4's service
    # answers WebP for a ".png" request. "" = do not check.
    expect_format: str = ""      # "png" | "webp" | "jpeg"
    # Whether the toolkit's PNG-only tile decoder can read those bytes.
    # Defaults to "ok" for png and "error" for anything else, so a row only
    # spells it out when it means something surprising.
    expect_decode: str = ""      # "ok" | "error"
    # Plausible ground at the manifest's elevation_probe, in metres, decoded
    # with the encoding THE PLUGIN resolves. This is what separates "the tiles
    # arrived" from "the terrain is real": a black tile, a flat-sea tile and a
    # Terrain-RGB tile decoded as Terrarium all arrive with HTTP 200.
    expect_elev_m: Tuple[float, float] = ()
    #: Substring the failure must contain, for rows that must NOT serve tiles.
    #: Without it "zero tiles arrived" is also what a pulled cable looks like.
    expect_failure: str = ""
    expect_features: int = 0     # vector rows: minimum feature count
    #: The deepest zoom this service really publishes. The plugin must KNOW it
    #: (known_services.json "max_zoom"), because that is what clamps a
    #: hand-added layer carrying QGIS's default zmax=18 — without it every
    #: request past the real limit fails, the converter writes those tiles as
    #: 0 m, and the analysis completes over a flat sea.
    expect_service_zmax: int = 0
    #: Tile edge in pixels the service really serves. The plugin's resolution
    #: maths assumes 256; a service serving 512 px @2x tiles is reported at
    #: half its real resolution.
    expect_tile_px: int = 0
    #: Resolution the pipeline tier runs this row at. The tile extent ladder
    #: (ABT_EXTENT_DEG) ties resolution to tile size, so a fixture that covers
    #: only a small box cannot fill a 0.5 deg 30 m tile — it needs a finer
    #: resolution whose tile fits inside the ground it actually has.
    pipeline_res_m: int = 30
    #: Analysis range for that run, in km.
    pipeline_range_km: int = 3
    #: Metres this row's terrain may differ from the catalogue's REFERENCE
    #: source over the same ground before it is wrong. 0 = not comparable
    #: (a different continent, or not elevation at all). Two independent DEMs
    #: of the same ground agree to a few metres; anything an order of magnitude
    #: past that is a pipeline fault, not a dataset difference.
    expect_agrees_m: float = 0.0
    #: WGS84 [west, south, east, north] of a file raster, filled in by
    #: _write_footprints. The torture runner's tier-B ingest places its test
    #: tile over THIS box — the fixtures are scattered (Bern, the Dead Sea,
    #: Los Angeles, Prague) and a tile placed over the reference AOI would come
    #: back empty for three of them and be reported as a hole.
    footprint: Tuple[float, ...] = ()
    known_fail: str = ""         # documented open finding: reported, does not fail a run
    #: Which checks that note actually covers. An unscoped known_fail used to
    #: turn ANY failure on the row into an expected one, so a note about the
    #: encoding verdict also swallowed "the layer does not load".
    known_fail_checks: Tuple[str, ...] = ()


# What QGIS actually accepts in `interpretation=`, measured on 3.44.7:
#     terrariumterrain -> Float32   maptilerterrain -> Float32
#     terrarium, mapboxterrain, terrainrgb, mapbox, ... -> silently IGNORED,
#     the layer stays an ARGB32 picture.
# TEST_DATA_GUIDE.md tells the reader to add "interpretation=terrarium" or
# "interpretation=mapboxterrain" by hand; QGIS ignores both. The plugin reads
# the parameter as a substring, so "terrariumterrain" still resolves to
# terrarium — but "maptilerterrain" contains neither "mapbox" nor "terrarium",
# so the plugin cannot see it at all. Row 1.10 exists to pin that down.
QGIS_INTERPRETATION = {
    "terrarium": "terrariumterrain",
    "terrariumterrain": "terrariumterrain",
    "mapbox": "maptilerterrain",
    "mapboxterrain": "maptilerterrain",
    "maptilerterrain": "maptilerterrain",
}


def xyz_uri(url: str, *, zmax: Optional[int] = None, zmin: int = 0,
            interpretation: str = "") -> str:
    """A QGIS XYZ layer URI.

    The tile URL goes in RAW — literal ``{z}``, literal ``://``. QGIS 3.22
    percent-decoded this parameter, QGIS 3.44 does not: give the modern
    provider ``https%3A//host/%7Bz%7D/...`` and it requests exactly that
    string, gets nothing, and the layer renders blank while still reporting
    itself valid. Measured on 3.44: encoded = 1 colour, raw = 707.

    The one thing a raw URL cannot carry is a second query parameter, because
    an unescaped ``&`` would end the ``url`` value. No service used here needs
    one; if that ever changes, this is where it breaks.

    *interpretation* is given in the plugin's vocabulary ("terrarium" /
    "mapbox") and translated to the token QGIS actually honours — see
    QGIS_INTERPRETATION.
    """
    parts = ["type=xyz", f"url={url}"]
    if zmax is not None:
        parts.append(f"zmax={zmax}")
    parts.append(f"zmin={zmin}")
    if interpretation:
        parts.append(f"interpretation="
                     f"{QGIS_INTERPRETATION.get(interpretation, interpretation)}")
    return "&".join(parts)


def wms_uri(url: str, layer: str, *, crs: str = "EPSG:3857",
            fmt: str = "image/png", style: str = "") -> str:
    return ("contextualWMSLegend=0"
            f"&crs={crs}&dpiMode=7&featureCount=10&format={fmt}"
            f"&layers={layer}&styles={style}&url={url}")


def wmts_uri(url: str, layer: str, *, tile_matrix_set: str, style: str,
             crs: str = "EPSG:3857", fmt: str = "image/jpeg",
             dimensions: str = "") -> str:
    parts = [f"crs={crs}", "dpiMode=7", "featureCount=10", f"format={fmt}",
             f"layers={layer}", f"styles={style}"]
    if dimensions:
        # Raw, for the same reason the tile URL is raw: percent-encoding is not
        # decoded by the provider. Observed on 3.44.7 — with `Time=current` the
        # layer issues real tile requests
        # (.../ch.swisstopo.pixelkarte-farbe/default/current/3857/10/533/360.jpeg);
        # omit the dimension entirely and it issues none and draws blank. So the
        # dimension is required, and it has to be readable.
        parts.append(f"tileDimensions={dimensions}")
    parts += [f"tileMatrixSet={tile_matrix_set}", "tilePixelRatio=0", f"url={url}"]
    return "&".join(parts)


def wcs_uri(url: str, identifier: str, *, crs: str, fmt: str = "GTiff") -> str:
    return (f"cache=PreferNetwork&crs={crs}&format={fmt}"
            f"&identifier={identifier}&url={url}")


G_XYZ = "1 · XYZ elevation services (guide §1)"
G_LOCAL = "2 · Local rasters (guide §2)"
G_BLD = "3 · Buildings (guide §3)"
G_SRV = "4 · Servers: WMS / WMTS / WCS (guide §4)"
G_RUN = "5 · Run matrix, sites, links (guide §5/§6)"


def build_catalogue(cfg: Cfg) -> List[Layer]:
    """Every layer the project *could* contain, with its availability check."""
    out = cfg.out_dir
    layers: List[Layer] = []
    suspects = _suspect_fixtures(out)

    def rel(p: Path) -> str:
        return "./" + p.relative_to(out).as_posix()

    def add_file(row: str, group: str, name: str, path: Path, crs: str, expect: str,
                 *, provider: str = "gdal", kind: str = "raster",
                 geometry: str = "", source: str = "", style: str = "",
                 color: str = "227,26,28,255", checked: bool = False,
                 check: str = "both", **expectations: Any) -> None:
        """Add a file-backed row, or record why it could not be added.

        ``**expectations`` reaches ``Layer`` untouched, so a file row can carry
        the same machine-checkable expectations a service row can. Without it,
        the 28 rows built through here took the dataclass defaults and their
        entire test was "the layer loaded and drew more than one colour".
        """
        if not path.exists():
            REPORT.skip(f"row {row} ({name})", f"missing fixture {path.name}")
            return
        # Every §2 fixture is a single-band elevation raster, so the classifier
        # has to say "dem" for all of them. Defaulting it here rather than
        # repeating it on 23 rows keeps the claim in one place — and it is a
        # real claim: a fixture that regressed to 3-band RGB, or a classifier
        # that started reading these as imagery, fails it.
        if kind == "raster" and provider == "gdal":
            expectations.setdefault("expect_class", "dem")
        suspect = suspects.get(path.name)
        if suspect and not expectations.get("known_fail"):
            # The fixture is known to contain a 0 m band. The row still ships —
            # losing coverage is worse — but it says so, and it goes red the day
            # the fixture is rebuilt correctly.
            expectations["known_fail"] = f"{path.name}: {suspect}"
            expectations["known_fail_checks"] = ("render", "ingest")
        layers.append(Layer(row=row, group=group, name=name, provider=provider,
                            source=source or rel(path), crs=crs, kind=kind,
                            geometry=geometry, expect=expect, style=style,
                            color=color, checked=checked, check=check,
                            **expectations))

    # ---------------- §1 XYZ ------------------------------------------------
    layers.append(Layer("1.1", G_XYZ, "1.1 AWS Terrarium — z15 + interpretation (happy path)",
                        "wms", xyz_uri(TERRARIUM_URL, zmax=15, interpretation="terrarium"),
                        "EPSG:3857",
                        expect="Downloads through the shared Rust downloader on BOTH tabs. "
                               "Reference result for every other provider.",
                        expect_class="dem", expect_encoding="terrarium", expect_zmax=15,
                        expect_format="png", expect_elev_m=PROBE_BAND,
                        expect_service_zmax=15, expect_tile_px=256,
                        expect_agrees_m=25.0))
    layers.append(Layer("1.2", G_XYZ, "1.2 AWS Terrarium — RAW RGB, no interpretation, z18 (the noise IS the test)",
                        "wms", xyz_uri(TERRARIUM_URL, zmax=18), "EPSG:3857",
                        expect="Deliberately has NO interpretation, so QGIS draws the raw "
                               "terrarium bytes as a picture — the garish false-colour noise is "
                               "correct, and is what a user gets when they forget the setting. "
                               "The plugin must still infer terrarium from the URL and clamp "
                               "z18 -> z15 with a log warning. Never flat-sea tiles.",
                        expect_class="dem", expect_encoding="terrarium", expect_zmax=15,
                        expect_format="png", expect_elev_m=PROBE_BAND,
                        expect_service_zmax=15, expect_tile_px=256,
                        expect_agrees_m=25.0))
    if cfg.maptiler_key:
        layers.append(Layer("1.3a", G_XYZ, "1.3a MapTiler Terrain-RGB v1 (PNG, key) — service stops at z12", "wms",
                            xyz_uri("https://api.maptiler.com/tiles/terrain-rgb/{z}/{x}/{y}.png"
                                    f"?key={cfg.maptiler_key}", zmax=12,
                                    interpretation="mapboxterrain"), "EPSG:3857",
                            expect="The terrain-rgb decode formula against an independent "
                                   "encoder — measured 659.3 m mean where AWS terrarium reads "
                                   "659.5 m on the same tile. Elevations plausible, not ~ -32000 m. "
                                   "MapTiler's own tiles.json caps terrain-rgb v1 at zoom 12, so "
                                   "zmax=12 is the service's limit, not ours. "
                                   "Note MapTiler serves 512 px (@2x) tiles while the zoom math "
                                   "assumes 256 px, so a z12 tile carries z13 detail.",
                            expect_class="dem", expect_encoding="mapbox", expect_zmax=12,
                            expect_format="png", expect_elev_m=PROBE_BAND,
                            expect_agrees_m=25.0,
                            # Measured 2026-08-24: z12 serves, z13+ answers HTTP
                            # 400, and every tile is 512x512.
                            expect_service_zmax=12, expect_tile_px=512))
        # zmax=14, not 12: MapTiler's own tiles.json reports maxzoom 14 for
        # terrain-rgb-v2 (v1 is the one that stops at 12). Capping it at 12 hid
        # the two deepest zooms — and those are where a codec problem shows up.
        layers.append(Layer("1.4", G_XYZ, "1.4 MapTiler Terrain-RGB v2 (WebP, needs key)", "wms",
                            xyz_uri("https://api.maptiler.com/tiles/terrain-rgb-v2/{z}/{x}/{y}.webp"
                                    f"?key={cfg.maptiler_key}", zmax=14,
                                    interpretation="mapboxterrain"), "EPSG:3857",
                            expect="WebP codec — the toolkit decodes PNG only. Must fail loudly "
                                   "naming the decode error, never silent 0 m. (v2 answers WebP "
                                   "even for a .png request, so the extension cannot save you.)",
                            check="error", expect_class="dem", expect_encoding="mapbox",
                            expect_zmax=14,
                            # The whole row: the bytes must BE WebP, and the
                            # PNG-only toolkit decoder must refuse them. Qt
                            # decodes WebP happily, so "the tiles arrived and
                            # the picture drew" was never evidence of anything.
                            # This goes red the day MapTiler starts serving PNG
                            # here (the row would no longer test a codec) or the
                            # day the toolkit learns WebP (update the row).
                            expect_format="webp", expect_decode="error",
                            expect_service_zmax=14, expect_tile_px=512,
                            # The row's contract, verbatim: fail loudly NAMING
                            # THE DECODE ERROR. A generic "tiles could not be
                            # fetched" is the wrong failure — it sends the user
                            # looking at their network instead of the codec.
                            expect_failure="decode"))
    else:
        REPORT.skip("layers 1.3a + 1.4 (MapTiler Terrain-RGB v1/v2)",
                    "no maptiler_key in torture.local.ini")
    # Zoom range from what is ON DISK, not from the config that asked for it.
    # A fetch that produced z10-z12 and failed at z13 still leaves the directory
    # there, and advertising zmax=13 over it made every z13 request a 404 that
    # the runner's small tile sample was unlikely to hit.
    def _fixture_zooms(folder: Path) -> List[int]:
        try:
            return sorted(int(d.name) for d in folder.iterdir()
                          if d.is_dir() and d.name.isdigit() and any(d.rglob("*.png")))
        except OSError:
            return []

    rgb_zooms = _fixture_zooms(out / "terrain-rgb")
    if rgb_zooms and rgb_zooms != sorted(cfg.terrain_rgb_zooms):
        REPORT.skip("terrain-rgb zoom levels "
                    f"{sorted(set(cfg.terrain_rgb_zooms) - set(rgb_zooms))}",
                    f"asked for in torture.local.ini, not on disk — rows 1.3b/1.5/1.10 "
                    f"advertise z{min(rgb_zooms)}-z{max(rgb_zooms)} instead")
    if rgb_zooms:
        zmax = max(rgb_zooms)
        layers.append(Layer("1.3b", G_XYZ,
                            f"1.3b Local Terrain-RGB fixture — Bern box only, "
                            f"z{min(rgb_zooms)}-z{zmax}, offline", "wms",
                            xyz_uri(cfg.terrain_rgb_base_url + "/{z}/{x}/{y}.png",
                                    zmax=zmax, zmin=min(rgb_zooms),
                                    interpretation="mapboxterrain"), "EPSG:3857",
                            expect="Same decode path as 1.3a, deterministic and offline — the "
                                   "project serves it itself on open. No key, no internet. "
                                   "Blank outside the Bern box or outside z10-13: it is a "
                                   "fixture, not a service. Widen it with terrain_rgb_zooms "
                                   "in torture.local.ini.",
                            expect_class="dem", expect_encoding="mapbox",
                            expect_format="png", expect_elev_m=PROBE_BAND,
                            expect_tile_px=256,
                            pipeline_res_m=2, pipeline_range_km=1))
    else:
        REPORT.skip("row 1.3b (local Terrain-RGB)", "fixture not generated — run --stages fetch")
    neutral_zooms = _fixture_zooms(out / "neutral-tiles")
    if neutral_zooms:
        layers.append(Layer(
            "1.10", G_XYZ, "1.10 Interpretation-only decode (no hint in the URL)", "wms",
            xyz_uri(cfg.terrain_rgb_base_url.replace("/terrain-rgb", "/neutral-tiles")
                    + "/{z}/{x}/{y}.png", zmax=max(neutral_zooms),
                    zmin=min(neutral_zooms), interpretation="mapbox"),
            "EPSG:3857",
            expect="The same Terrain-RGB tiles as 1.3b, served under a name no "
                   "known_services entry matches, so ONLY the interpretation can "
                   "decode them. QGIS honours it (the layer draws as elevation). The "
                   "plugin looks for 'mapbox'/'terrarium' inside the token and QGIS's "
                   "token is 'maptilerterrain', which contains neither — so expect the "
                   "terrarium default and terrain near -32000 m. That is the finding.",
            expect_class="dem", expect_encoding="mapbox",
            expect_format="png", expect_elev_m=PROBE_BAND,
            pipeline_res_m=2, pipeline_range_km=1,
            known_fail="xyz_encoding() matches the substrings 'mapbox'/'terrarium'; QGIS's "
                       "token is 'maptilerterrain', which contains neither, so the plugin "
                       "falls back to Terrarium and the ground decodes to about -32350 m. "
                       "Open finding — this turns green when the plugin learns the token.",
            # Scoped: the note covers the encoding verdict and the elevation it
            # produces, and NOTHING else. Unscoped, it also swallowed "the layer
            # does not load", which has nothing to do with the finding.
            known_fail_checks=("encoding", "pipeline", "pipeline.agrees")))
    # Served from the local fixture, not from AWS. The test is the plugin's
    # ambiguity error, which fires on the URL string before a single tile is
    # fetched — so the tiles never needed to be missing. Pointed at a path that
    # does not exist, every tile 404s and QGIS retries each one three times:
    # >100 errors in the log and a visibly slower project once the layer is
    # switched on. Locally it renders instead.
    # Guarded like 1.3b and 1.10, and for the same reason: unguarded, a build
    # whose fetch stage never produced the mirror shipped row 1.5 as a layer
    # that 404s everywhere while still claiming expect_renders=True. The row's
    # own point — "renders fine, plugin must REFUSE it" — cannot be made by a
    # layer that renders nothing.
    mirror_zooms = _fixture_zooms(out / "terrarium-terrain-rgb-mirror")
    if mirror_zooms:
        ambiguous = cfg.terrain_rgb_base_url.replace(
            "/terrain-rgb", "/terrarium-terrain-rgb-mirror")
        layers.append(Layer("1.5", G_XYZ,
                            "1.5 Ambiguous encoding — renders fine, plugin must REFUSE it",
                            "wms", xyz_uri(ambiguous + "/{z}/{x}/{y}.png",
                                           zmax=max(mirror_zooms),
                                           zmin=min(mirror_zooms)), "EPSG:3857",
                            expect="URL names both terrarium and terrain-rgb, so the plugin "
                                   "cannot know how the pixels decode and must raise the "
                                   "ambiguity error and refuse to run. The layer itself draws "
                                   "normally — a blank canvas would tell you nothing about that.",
                            check="error", expect_class="dem", expect_encoding="error",
                            expect_format="png", expect_failure="decode",
                            pipeline_res_m=2, pipeline_range_km=1))
    else:
        REPORT.skip("row 1.5 (ambiguous encoding)",
                    "the terrarium-terrain-rgb-mirror fixture was not generated — "
                    "run --stages fetch")
    layers.append(Layer("1.6", G_XYZ, "1.6 Dead endpoint (DNS failure by design)", "wms",
                        xyz_uri("https://tiles.example.invalid/terrarium/{z}/{x}/{y}.png", zmax=15,
                                interpretation="terrarium"), "EPSG:3857",
                        expect="Total fetch failure -> hard error, nothing cached, no flat-0 terrain.",
                        check="error", expect_class="dem", expect_encoding="terrarium",
                        expect_renders=False))
    layers.append(Layer("1.7", G_XYZ, "1.7 OpenStreetMap XYZ (imagery classifier trap)", "wms",
                        xyz_uri("https://tile.openstreetmap.org/{z}/{x}/{y}.png", zmax=19),
                        "EPSG:3857", checked=True,
                        expect="Must be classified as imagery and never offered in the DEM picker.",
                        check="reject", expect_class="imagery", expect_format="png",
                        expect_tile_px=256))
    # Deliberately absent: api.mapbox.com (row 1.8 of earlier drafts) and
    # nextzen. Mapbox wants payment details for a token, and the only thing the
    # live endpoint adds over 1.3a/1.3b is Mapbox's own bytes — its URL rules
    # (encoding=mapbox, max_zoom=15) are already pinned by unit tests in
    # tests/test_terrain_adapter.py. Nextzen stopped accepting signups and is
    # winding down; it served the same Tilezen dataset as row 1.1, so
    # "agreement between two terrarium producers" was near-tautological anyway.

    # ---------------- §2 local rasters --------------------------------------
    dem = out / "dem"
    add_file("2.1", G_LOCAL, "2.1 Copernicus GLO-30 N46E007 (full tile, EPSG:4326)",
             dem / "source" / f"Copernicus_DSM_COG_10_{COP_BERN}_DEM.tif", "EPSG:4326",
             "Geographic passthrough — must stay byte-identical to pre-refactor output.")
    add_file("2.2", G_LOCAL, "2.2 base_wgs84.tif (Float32 baseline clip)",
             dem / "base" / "base_wgs84.tif", "EPSG:4326",
             "The baseline every other fixture derives from.")
    add_file("2.3", G_LOCAL, "2.3 base_i16.tif (Int16)",
             dem / "base" / "base_i16.tif", "EPSG:4326", "Integer sample type.")
    add_file("2.4", G_LOCAL, "2.4 Dead Sea (negative elevations)",
             dem / "base" / "deadsea_wgs84.tif", "EPSG:4326",
             "Negative elevations end-to-end; pair with the -430 m AMSL site.")
    add_file("2.5", G_LOCAL, "2.5 UTM 32N (EPSG:32632)", dem / "variants" / "utm32.tif",
             "EPSG:32632", "Generic projected CRS — impossible before the refactor.")
    add_file("2.6", G_LOCAL, "2.6 Web Mercator (EPSG:3857)", dem / "variants" / "merc3857.tif",
             "EPSG:3857", "Per-file bounds transform for a projected folder member.")
    add_file("2.7", G_LOCAL, "2.7 LV95 warp (EPSG:2056)", dem / "variants" / "lv95_2056.tif",
             "EPSG:2056", "somerc through proj4rs; compare against the real swisstopo tiles.")
    add_file("2.8", G_LOCAL, "2.8 swissALTIRegio LV95 tile (real 10 m data)",
             *_first_swiss(dem / "swiss"),
             "The LV95/proj4rs parity case. Output may shift <= 1 source px vs "
             "pre-refactor — documented in CONTRACT v2.0.")
    add_file("2.9", G_LOCAL, "2.9 mosaic.vrt over the LV95 tiles", dem / "mosaic.vrt",
             "EPSG:2056", "VRT works as a LAYER only — never scanned inside a terrain folder.")
    add_file("2.10", G_LOCAL, "2.10 Float32 + NaN nodata", dem / "variants" / "f32_nan.tif",
             "EPSG:4326", "Voids must read as -9999, not 0 m (the F4 regression).")
    add_file("2.11", G_LOCAL, "2.11 Int16 + nodata -32768", dem / "variants" / "i16_nodata.tif",
             "EPSG:4326", "Classic integer nodata.")
    add_file("2.12", G_LOCAL, "2.12 Int32 + nodata -2147483648", dem / "variants" / "i32_nodata.tif",
             "EPSG:4326", "Low 16 bits of the nodata value are zero — truncation trap.")
    add_file("2.13", G_LOCAL, "2.13 DEFLATE-compressed GeoTIFF", dem / "variants" / "deflate.tif",
             "EPSG:4326", "The converter reads this directly (no GDAL) — should work.")
    add_file("2.14", G_LOCAL, "2.14 Tiled GeoTIFF", dem / "variants" / "tiled.tif",
             "EPSG:4326", "tiff-crate tiling — verify.")
    add_file("2.15", G_LOCAL, "2.15 COG (DEFLATE)", dem / "variants" / "cog.tif",
             "EPSG:4326", "Local Cloud-Optimized GeoTIFF.")
    add_file("2.16", G_LOCAL, "2.16 COG (ZSTD)", dem / "variants" / "zstd_cog.tif",
             "EPSG:4326", "Likely UNREADABLE by the tiff crate -> must fail loudly naming the "
                          "file, never silently skip. Open question.", check="error")
    add_file("2.17", G_LOCAL, "2.17 SRTM .hgt", dem / "hgt" / "N46E007.hgt",
             "EPSG:4326", "Non-TIFF -> materialize helper path.")
    add_file("2.18", G_LOCAL, "2.18 USGSDEM ascii.dem", dem / "variants" / "ascii.dem",
             "EPSG:4326", "Exotic format -> materialize helper path.")
    # 2.19 (EPSG:2229) and 2.20 (EPSG:5514) are the only CRS here that publish
    # more than one datum transformation, so they are the only rows that can
    # raise "Multiple operations are possible ...". They are still layers: the
    # macro picks a working operation locally on open (see
    # choose_local_transforms) rather than this file baking one in, because a
    # pipeline generated on the building machine is exactly what broke them.
    add_file("2.19", G_LOCAL, "2.19 US survey feet (EPSG:2229) — real Los Angeles terrain",
             dem / FOREIGN_CRS_DIR / "feet_2229_losangeles.tif", "EPSG:2229",
             "+units honored: the geotransform of a feet CRS is in feet too, so a unit "
             "slip shows up as a factor of 3.28. Real relief, not a ramp — a plane "
             "interpolates exactly and would hide a resampling error. Zoom to the "
             "'Los Angeles' run-matrix box. EPSG:2229 offers four datum "
             "transformations, three of them grid-based; the macro selects the "
             "grid-free one on this machine so the layer draws without you choosing.")
    add_file("2.20", G_LOCAL, "2.20 Krovak (EPSG:5514) — real Prague terrain",
             dem / FOREIGN_CRS_DIR / "krovak5514_prague.tif", "EPSG:5514",
             "Unsupported by proj4rs — ingest must hard-error naming the CRS before any "
             "tile is written. Real terrain so the failure is about the CRS, not the "
             "data. Zoom to the 'Prague' run-matrix box.", check="error",
             # The guide quotes this as the pass condition, and quoted
             # expectations are contract text. Every other check:error row falls
             # back to "the refusal must name the file"; this one names the CRS
             # instead, so the wording is pinned here.
             expect_failure="EPSG:5514")
    add_file("2.21", G_LOCAL, "2.21 >512 MB monolith", dem / "big" / "monolith.tif",
             "EPSG:4326", "Past the materialize threshold — window-copy, per-tile RAM bound.")
    if (dem / "zip" / "dem_in_zip.zip").is_file():
        # QgsPathResolver strips the /vsi prefix, resolves the rest against the
        # project folder and puts the prefix back, so a relative /vsizip path
        # survives — which keeps the project portable between the WSL and the
        # Windows view of the same folder.
        layers.append(Layer("2.22", G_LOCAL, "2.22 /vsizip zipped GeoTIFF", "gdal",
                            "/vsizip/" + rel(dem / "zip" / "dem_in_zip.zip") + "/base_wgs84.tif",
                            "EPSG:4326",
                            expect="/vsi prefix handling on the plugin side (abspath() must not "
                                   "mangle it)."))
    layers.append(Layer("2.23", G_LOCAL, "2.23 /vsicurl remote COG (Copernicus)", "gdal",
                        "/vsicurl/" + COPERNICUS_URL.format(tile=COP_BERN), "EPSG:4326",
                        expect="Remote COG through /vsicurl. Was known broken (A10: abspath() "
                               "collapsed https:// -> https:/); /vsi* sources are materialized "
                               "before abspath is reached now and it ingests cleanly. Slow to "
                               "open — that is the network."))
    # The three non-TIFF formats the fabricate stage has always built and no row
    # ever claimed. CHECKLIST 3.k even told the reader "row 2.26 proves the same
    # file loads fine as a layer" — a row that did not exist anywhere. They are
    # layer-only inputs (the folder scanner takes .tif/.tiff/.dem/.hgt only), so
    # each one exercises materialize_for_converter, which is the thinnest part
    # of that path.
    add_file("2.24", G_LOCAL, "2.24 ERDAS Imagine .img", dem / "variants" / "erdas.img",
             "EPSG:4326", "HFA — layer-only input; the converter reaches it through "
                          "materialize_for_converter, never through the folder scanner.")
    add_file("2.25", G_LOCAL, "2.25 ESRI .bil (EHdr)", dem / "variants" / "bil" / "base.bil",
             "EPSG:4326", "Band-interleaved raw with a sidecar header — the format most "
                          "likely to lose its geotransform on the way through.")
    add_file("2.26", G_LOCAL, "2.26 ASCII grid .asc",
             dem / "variants" / "ignored_formats" / "base.asc", "EPSG:4326",
             "The counterpart to checklist 3.k: the folder scanner must refuse this "
             "extension, and the very same file must still load and convert as a LAYER.")

    # ---------------- §3 buildings ------------------------------------------
    bld = out / "buildings"
    add_file("3.3", G_BLD, "3.3 OSM buildings, Bern (GeoJSON)", bld / "osm_bern.geojson",
             "EPSG:4326", "Raw Overpass output, and the source the other fixtures in this "
                          "group are derived from; the plugin's own OSM download button "
                          "covers the built-in path (row 3.3 proper).",
             provider="ogr", kind="vector", geometry="Polygon", style="fill",
             color="55,126,184,255", check="buildings", expect_features=500)
    if (bld / "multi.gpkg").is_file():
        layers.append(Layer("3.2a", G_BLD, "3.2a multi.gpkg | layer 'buildings' (west half)", "ogr",
                            "./buildings/multi.gpkg|layername=buildings", "EPSG:4326",
                            kind="vector", geometry="Polygon", style="fill", color="77,175,74,255",
                            expect="The |layername= sublayer path — the converter must receive "
                                   "the SELECTED layer.", check="buildings",
                            expect_features=200))
        layers.append(Layer("3.2b", G_BLD, "3.2b multi.gpkg | layer 'other_layer' (east half)", "ogr",
                            "./buildings/multi.gpkg|layername=other_layer", "EPSG:4326",
                            kind="vector", geometry="Polygon", style="fill", color="152,78,163,255",
                            expect="Run with THIS one selected: buildings must land in the east "
                                   "half. West-half output = the sublayer bug is back.",
                            check="buildings", expect_features=200))
    add_file("3.5", G_BLD, "3.5 Wrong-CRS shapefile (EPSG:2056)", bld / "lv95_buildings.shp",
             "EPSG:2056", "Reprojection-on-conversion path.",
             provider="ogr", kind="vector", geometry="Polygon", style="fill",
             color="255,127,0,255", check="buildings", expect_features=500)
    add_file("3.6", G_BLD, "3.6 FlatGeobuf passthrough", bld / "direct.fgb",
             "EPSG:4326", "Must NOT be re-converted, and must not be handed over with the "
                          "wrong CRS.",
             provider="ogr", kind="vector", geometry="Polygon", style="fill",
             color="166,86,40,255", check="buildings", expect_features=500)
    if cfg.swissbuildings3d and cfg.swissbuildings3d.exists():
        layers.append(Layer("3.1", G_BLD, "3.1 swissBUILDINGS3D 3.0", "ogr",
                            str(cfg.swissbuildings3d).replace("\\", "/"), "EPSG:2056",
                            kind="vector", geometry="Polygon", style="fill",
                            color="228,26,28,255",
                            expect="Real GDB/GPKG -> FGB conversion; LV95 vector CRS.",
                            check="buildings"))
    else:
        REPORT.skip("layer 3.1 (swissBUILDINGS3D)",
                    "no swissbuildings3d path in torture.local.ini (manual download)")

    # ---------------- §4 servers --------------------------------------------
    layers.append(Layer("4.1", G_SRV, "4.1 terrestris WMS — OSM-WMS (imagery)", "wms",
                        wms_uri("https://ows.terrestris.de/osm/service", "OSM-WMS"), "EPSG:3857",
                        expect="Classifier: imagery. The ARGB32 single-band trap — must never "
                               "be offered as a DEM.", check="reject", expect_class="imagery"))
    layers.append(Layer("4.2", G_SRV, "4.2 swisstopo WMTS — pixelkarte-farbe (imagery)", "wms",
                        wmts_uri("https://wmts.geo.admin.ch/EPSG/3857/1.0.0/WMTSCapabilities.xml",
                                 "ch.swisstopo.pixelkarte-farbe", tile_matrix_set="3857_19",
                                 style="ch.swisstopo.pixelkarte-farbe", fmt="image/jpeg",
                                 dimensions="Time=current"), "EPSG:3857",
                        expect="Same as 4.1, through the WMTS code path.", check="reject",
                        expect_class="imagery"))
    layers.append(Layer("4.3", G_SRV, "4.3 NRW WCS — nw_dgm (elevation coverage, EPSG:25832)", "wcs",
                        wcs_uri("https://www.wcs.nrw.de/geobasis/wcs_nw_dgm", "nw_dgm",
                                crs="EPSG:25832"), "EPSG:25832",
                        min_scale=250000,
                        expect="Coverage provider -> real Float32 values; band heuristics may "
                               "classify it as DEM. Third real CRS end-to-end. Hidden above "
                               "1:250 000 on purpose: the server answers HTTP 400 to a request "
                               "for its FULL extent (measured: 400 at every pixel size, while a "
                               "150 km box succeeds), and the full extent is exactly what QGIS "
                               "asks for when the canvas is zoomed out past NRW. Zoom to the "
                               "'NRW' run-matrix box."))
    # 3DEP twice, deliberately. The WMS path can only ever hand QGIS a picture:
    # Qt decodes the response with QImage, and QImage refuses 32-bit samples
    # ("Sorry, can not handle images with 32-bit samples"), so a WMS layer asking
    # for image/tiff draws nothing ANYWHERE — including over Colorado. Values
    # have to come from the coverage service instead.
    # 4.4a is NOT a project layer. Measured on four consecutive loads, ESRI's
    # WCS took 19.7 s, 56.2 s, 82.1 s and 96.2 s just to describe the coverage,
    # and intermittently failed with "Cannot describe coverage" — a tax on every
    # project open and a random bad-layer dialog. It ships as a Browser
    # connection instead (registered by the macro); add it when testing the
    # coverage-values path.
    REPORT.skip("layer 4.4a (USGS 3DEP WCS)",
                "ships as a Browser connection, not a layer: measured 20-96 s per "
                "load and intermittent 'Cannot describe coverage'", by_design=True)
    layers.append(Layer("4.4b", G_SRV, "4.4b USGS 3DEP WMS — rendered picture (URL-hint trap)",
                        "wms",
                        wms_uri("https://elevation.nationalmap.gov/arcgis/services/3DEPElevation/"
                                "ImageServer/WMSServer", "3DEPElevation", crs="EPSG:4326",
                                fmt="image/png"), "EPSG:4326",
                        expect="The same service as 4.4a, as a PICTURE. Its URL contains "
                               "'elevation', so _classify_by_url calls it a DEM — but the bytes "
                               "are a rendering, exactly like the 4.6 hillshade. Running terrain "
                               "over it is the silent failure to catch. USA only.",
                        check="reject",
                        # This row existed to catch a live defect and shipped
                        # with expect_class blank, which made the runner skip
                        # the check — the one row aimed at the bug was the one
                        # configured not to look. Measured on QGIS 3.44.7:
                        # classify_raster_layer() returns "dem" and
                        # dem_layer_warning() returns None, so the plugin offers
                        # a rendered picture as terrain with no warning at all.
                        expect_class="imagery",
                        known_fail="_classify_by_url matches 'elevation' in "
                                   "elevation.nationalmap.gov and returns 'dem' before the "
                                   "rendered-provider guard can fire, so this WMS picture is "
                                   "offered as a DEM with no warning. Open finding — this "
                                   "turns green when the URL hints yield to the provider kind.",
                        known_fail_checks=("classify", "classify.reject")))
    layers.append(Layer("4.5", G_SRV, "4.5 swisstopo WMS — pixelkarte-farbe (imagery)", "wms",
                        wms_uri("https://wms.geo.admin.ch/", "ch.swisstopo.pixelkarte-farbe",
                                crs="EPSG:2056"), "EPSG:2056",
                        expect="WMS imagery classification, free, no key.", check="reject",
                        expect_class="imagery"))
    layers.append(Layer("4.6", G_SRV, "4.6 swisstopo WMS — swissALTI3D hillshade (THE TRAP)", "wms",
                        wms_uri("https://wms.geo.admin.ch/",
                                "ch.swisstopo.swissalti3d-reliefschattierung", crs="EPSG:2056"),
                        "EPSG:2056",
                        expect="Looks like terrain, IS a rendered picture. The classifier must "
                               "call it imagery — picking it as a DEM is the exact silent "
                               "failure the provider guard exists for.", check="reject",
                        expect_class="imagery"))

    # ---------------- §5 run matrix -----------------------------------------
    vec = out / "vectors"
    add_file("5.1", G_RUN, "5.1 Run matrix (bboxes)", vec / "run_matrix.geojson", "EPSG:4326",
             "One polygon per case in guide §5. Read the bbox off the attribute table, "
             "type it into the Map Converter.",
             provider="ogr", kind="vector", geometry="Polygon", style="fill",
             color="227,26,28,255", checked=True, check="none",
             expect_features=len(RUN_MATRIX))
    add_file("5.2", G_RUN, "5.2 Sites (TX/RX, incl. -430 m AMSL)", vec / "sites.geojson",
             "EPSG:4326", "Site-table entries: heights and AGL/AMSL modes are attributes.",
             provider="ogr", kind="vector", geometry="Point", style="point",
             color="0,128,0,255", checked=True, check="none",
             expect_features=len(SITES))
    add_file("5.3", G_RUN, "5.3 P2P links", vec / "p2p_links.geojson", "EPSG:4326",
             "Endpoints for the P2P tab and the batch CSVs in batch/.",
             provider="ogr", kind="vector", geometry="LineString", style="line",
             color="55,126,184,255", checked=True, check="none",
             expect_features=len(P2P_LINKS))

    # How each service layer draws. A WMS/XYZ/WCS provider has no statistics for
    # QGIS to invent a renderer from, so it is decided here: a layer carrying an
    # elevation interpretation is values, everything else from a rendered
    # service is a picture.
    for lyr in layers:
        if lyr.render or lyr.kind != "raster":
            continue
        if lyr.provider == "wcs":
            lyr.render = "values"
        elif lyr.provider == "wms":
            lyr.render = "values" if "interpretation=" in lyr.source else "picture"

    return layers


def _first_swiss(swiss_dir: Path) -> Tuple[Path, str]:
    tifs = sorted(swiss_dir.glob("*.tif")) if swiss_dir.is_dir() else []
    return (tifs[0] if tifs else swiss_dir / "missing.tif"), "EPSG:2056"


# ---------------------------------------------------------------------------
# Stage: project (.qgs + console bootstrap + README)
# ---------------------------------------------------------------------------

# QgsCoordinateReferenceSystem::readXml only looks at <authid> when a valid
# <srsid> (a QGIS-internal srs.db row id, not the EPSG code) is present, and
# falls back to <wkt>/<proj4> otherwise. Writing an authid alone therefore
# yields an INVALID project CRS — silently, and the canvas then reports no CRS
# at all. So the two CRS a project can sensibly be in are spelled out in full.
#: Filled in from GDAL at build time — canonical, and not something a human
#: retypes. The literals below are only the no-GDAL fallback: a hand-typed
#: EPSG:3857 WKT that omitted EXTENSION["PROJ4", ...] is one of the things that
#: made web-mercator layers fragile.
_CRS_DEFS_RESOLVED: Dict[str, Dict[str, str]] = {}

_CRS_DEFS: Dict[str, Dict[str, str]] = {
    "EPSG:4326": {
        "description": "WGS 84",
        "proj4": "+proj=longlat +datum=WGS84 +no_defs",
        "wkt": ('GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563,'
                'AUTHORITY["EPSG","7030"]],AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,'
                'AUTHORITY["EPSG","8901"]],UNIT["degree",0.0174532925199433,'
                'AUTHORITY["EPSG","9122"]],AXIS["Latitude",NORTH],AXIS["Longitude",EAST],'
                'AUTHORITY["EPSG","4326"]]'),
        "geographic": "true",
        "projectionacronym": "longlat",
        "ellipsoidacronym": "EPSG:7030",
        "units": "degrees",
    },
    "EPSG:3857": {
        "description": "WGS 84 / Pseudo-Mercator",
        "proj4": ("+proj=merc +a=6378137 +b=6378137 +lat_ts=0 +lon_0=0 +x_0=0 +y_0=0 "
                  "+k=1 +units=m +nadgrids=@null +wktext +no_defs"),
        "wkt": ('PROJCS["WGS 84 / Pseudo-Mercator",GEOGCS["WGS 84",DATUM["WGS_1984",'
                'SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],'
                'AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0,AUTHORITY["EPSG","8901"]],'
                'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
                'AUTHORITY["EPSG","4326"]],PROJECTION["Mercator_1SP"],'
                'PARAMETER["central_meridian",0],PARAMETER["scale_factor",1],'
                'PARAMETER["false_easting",0],PARAMETER["false_northing",0],'
                'UNIT["metre",1,AUTHORITY["EPSG","9001"]],AXIS["Easting",EAST],'
                'AXIS["Northing",NORTH],AUTHORITY["EPSG","3857"]]'),
        "geographic": "false",
        "projectionacronym": "merc",
        "ellipsoidacronym": "EPSG:7030",
        "units": "meters",
    },
}


def _crs_def(authid: str) -> Optional[Dict[str, str]]:
    """A complete CRS definition for *authid*, or None if we cannot build one.

    Two are hard-coded so the project stage runs without GDAL; anything else is
    derived from the EPSG code at build time and cached.
    """
    if not authid.upper().startswith("EPSG:"):
        return _CRS_DEFS.get(authid)
    if authid in _CRS_DEFS_RESOLVED:
        return _CRS_DEFS_RESOLVED[authid]
    try:
        from osgeo import osr
        code = int(authid.split(":")[1])
        srs = osr.SpatialReference()
        if srs.ImportFromEPSG(code) != 0:
            return None
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        proj4 = srs.ExportToProj4().strip()
    except Exception:
        # No GDAL: fall back to the two definitions typed out below.
        return _CRS_DEFS.get(authid)
    acronym = ""
    for token in proj4.split():
        if token.startswith("+proj="):
            acronym = token.split("=", 1)[1]
    ellipsoid = srs.GetAuthorityCode("SPHEROID") or srs.GetAuthorityCode("DATUM")
    _CRS_DEFS_RESOLVED[authid] = {
        "description": srs.GetName() or authid,
        "proj4": proj4,
        "wkt": srs.ExportToWkt(),
        "geographic": "true" if srs.IsGeographic() else "false",
        "projectionacronym": acronym,
        "ellipsoidacronym": f"EPSG:{ellipsoid}" if ellipsoid else "",
        "units": "degrees" if srs.IsGeographic() else "meters",
    }
    return _CRS_DEFS_RESOLVED[authid]


def _srs(parent: ET.Element, authid: str) -> None:
    """A <spatialrefsys> block QGIS can actually restore.

    This has teeth: ``QgsCoordinateReferenceSystem::readXml`` only looks at
    ``<authid>`` when a valid ``<srsid>`` (a QGIS-internal srs.db row id, not
    the EPSG code) sits next to it, and otherwise falls through to
    ``<wkt>``/``<proj4>``. An authid-only block therefore restores as an
    INVALID CRS that still *reports* the right authid — so it looks correct
    everywhere except where it matters: every coordinate transform out of that
    layer fails, and QGIS draws it at raw coordinates or not at all. Worse, a
    CRS in the XML stops QGIS falling back to the provider's own CRS.

    So: write a complete definition, or write nothing and let the provider
    decide. Never an authid on its own.
    """
    spec = _crs_def(authid)
    if spec is None:
        return
    srs = ET.SubElement(parent, "spatialrefsys")
    ET.SubElement(srs, "wkt").text = spec["wkt"]
    ET.SubElement(srs, "proj4").text = spec["proj4"]
    ET.SubElement(srs, "srid").text = authid.split(":")[-1]
    ET.SubElement(srs, "authid").text = authid
    ET.SubElement(srs, "description").text = spec["description"]
    ET.SubElement(srs, "projectionacronym").text = spec["projectionacronym"]
    ET.SubElement(srs, "ellipsoidacronym").text = spec["ellipsoidacronym"]
    ET.SubElement(srs, "geographicflag").text = spec["geographic"]


def _symbol(parent: ET.Element, style: str, color: str) -> None:
    """A single-symbol renderer in the long-supported <prop> form."""
    renderer = ET.SubElement(parent, "renderer-v2", {
        "type": "singleSymbol", "forceraster": "0", "symbollevels": "0", "enableorderby": "0"})
    symbols = ET.SubElement(renderer, "symbols")
    sym_type = {"fill": "fill", "point": "marker", "line": "line"}[style]
    sym = ET.SubElement(symbols, "symbol", {
        "type": sym_type, "name": "0", "alpha": "1", "clip_to_extent": "1", "force_rhr": "0"})
    cls = {"fill": "SimpleFill", "point": "SimpleMarker", "line": "SimpleLine"}[style]
    lyr = ET.SubElement(sym, "layer", {"class": cls, "enabled": "1", "locked": "0", "pass": "0"})
    props: Dict[str, str] = {}
    if style == "fill":
        props = {"color": "0,0,0,0", "style": "solid", "outline_color": color,
                 "outline_style": "solid", "outline_width": "0.6", "outline_width_unit": "MM",
                 "joinstyle": "bevel", "offset": "0,0", "offset_unit": "MM"}
    elif style == "point":
        props = {"name": "circle", "color": color, "outline_color": "0,0,0,255",
                 "outline_style": "solid", "outline_width": "0.2", "size": "3",
                 "size_unit": "MM", "angle": "0", "offset": "0,0", "offset_unit": "MM",
                 "horizontal_anchor_point": "1", "vertical_anchor_point": "1"}
    else:
        props = {"line_color": color, "line_style": "solid", "line_width": "0.8",
                 "line_width_unit": "MM", "capstyle": "square", "joinstyle": "bevel",
                 "offset": "0", "offset_unit": "MM", "use_custom_dash": "0"}
    for k, v in props.items():
        ET.SubElement(lyr, "prop", {"k": k, "v": v})


def _raster_stats(cfg: Cfg, layers: Sequence[Layer]) -> Dict[str, List[float]]:
    """``{source: [min, max]}`` for the file rasters, cached on disk.

    Without a contrast stretch QGIS draws a DEM as one flat grey rectangle —
    readable, but useless for checking at a glance that terrain landed where it
    should. The numbers need GDAL, the project does not, so they are computed
    once and cached next to the project.
    """
    cache_path = cfg.out_dir / ".raster_stats.json"
    cache: Dict[str, List[float]] = {}
    if cache_path.is_file():
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except ValueError:
            cache = {}

    missing = [lyr for lyr in layers
               if lyr.kind == "raster" and lyr.provider == "gdal" and lyr.source not in cache]
    if missing:
        try:
            from osgeo import gdal
            gdal.UseExceptions()
        except ImportError:
            REPORT.skip("contrast stretch for the file rasters",
                        "no GDAL in this interpreter — every DEM keeps QGIS's default "
                        "flat-grey rendering. Re-run --stages project with QGIS's Python.")
            return cache
        for lyr in missing:
            path = lyr.source
            prefix = ""
            if path.startswith("/vsi"):
                # "/vsizip/./dem/x.zip/inner.tif" -> prefix + resolved tail.
                head, _, tail = path[1:].partition("/")
                prefix, path = f"/{head}/", tail
            if path.startswith("./"):
                path = str(cfg.out_dir / path[2:])
            path = prefix + path
            try:
                ds = gdal.Open(path)
                mn, mx, mean, std = ds.GetRasterBand(1).GetStatistics(True, True)
                ds = None
                # mean +- 2 sigma, clipped: a full min/max stretch on a tile that
                # spans the whole Alps renders the 500 m valley floor as black.
                cache[lyr.source] = [max(mn, mean - 2 * std), min(mx, mean + 2 * std)]
            except Exception as exc:
                REPORT.skip(f"contrast stretch for {Path(lyr.source).name}",
                            f"statistics could not be read: {str(exc).splitlines()[0][:70]}")
                continue
        cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    return cache


def _raster_pipe(parent: ET.Element, vmin: float = 0.0, vmax: float = 0.0,
                 kind: str = "gray", live_stretch: bool = False) -> None:
    """The renderer a raster layer ships with.

    Every layer needs one written down. QGIS can invent a renderer for a file
    raster because it can read the file's statistics; for a WMS/XYZ/WCS layer
    it cannot (that would mean downloading the world), so an elevation service
    with no renderer in the project draws as one flat nothing. That is exactly
    what happened to every service layer here until this existed.

    ``kind="colordata"`` is what a rendered picture needs (ARGB32 from the
    server); ``kind="gray"`` plus a stretch is what elevation values need.
    """
    pipe = ET.SubElement(parent, "pipe")
    if kind == "colordata":
        ET.SubElement(pipe, "rasterrenderer", {
            "type": "singlebandcolordata", "band": "1", "opacity": "1",
            "alphaBand": "-1", "nodataColor": ""})
        return
    rr = ET.SubElement(pipe, "rasterrenderer", {
        "type": "singlebandgray", "grayBand": "1", "opacity": "1", "alphaBand": "-1",
        "gradient": "BlackToWhite", "nodataColor": ""})
    ET.SubElement(rr, "rasterTransparency")
    origin = ET.SubElement(rr, "minMaxOrigin")
    # A remote elevation service has no statistics to stretch from, and one
    # fixed range cannot suit both a 400 m valley and a 4000 m ridge. Letting
    # QGIS recompute from whatever is on screen is what a person would do by
    # hand with "Stretch using current extent", on every zoom.
    for tag, text in (("limitsType", "MinMax"),
                      ("extent", "UpdatedCanvas" if live_stretch else "WholeRaster"),
                      ("statAccuracy", "Estimated"), ("cumulativeCutLower", "0.02"),
                      ("cumulativeCutUpper", "0.98"), ("stdDevFactor", "2")):
        ET.SubElement(origin, tag).text = text
    ce = ET.SubElement(rr, "contrastEnhancement")
    ET.SubElement(ce, "minValue").text = f"{vmin:.4f}"
    ET.SubElement(ce, "maxValue").text = f"{vmax:.4f}"
    ET.SubElement(ce, "algorithm").text = "StretchToMinimumMaximum"


def _operation_via_qgis(src: str, dest: str) -> Tuple[int, str]:
    """``(candidate_count, best_grid_free_pipeline)`` for *src*->*dest*, via QGIS."""
    try:
        from qgis.core import QgsCoordinateReferenceSystem, QgsDatumTransform
    except ImportError:
        return 0, ""
    try:
        ops = QgsDatumTransform.operations(QgsCoordinateReferenceSystem(src),
                                           QgsCoordinateReferenceSystem(dest))
    except Exception:
        return 0, ""
    usable = [o for o in ops if o.isAvailable and "grids=" not in o.proj
              and "hgridshift" not in o.proj and "vgridshift" not in o.proj]
    if not usable:
        usable = [o for o in ops if o.isAvailable]
    if not usable:
        return len(ops), ""
    usable.sort(key=lambda o: o.accuracy if o.accuracy >= 0 else 999)
    return len(ops), usable[0].proj


def _operation_via_projinfo(src: str, dest: str) -> Tuple[int, str]:
    """Same, via the ``projinfo`` binary that ships with PROJ (so, with GDAL)."""
    try:
        import subprocess
        out = subprocess.run(
            ["projinfo", "-s", src, "-t", dest, "--spatial-test", "intersects", "-o", "PROJ"],
            capture_output=True, text=True, timeout=60).stdout
    except Exception:
        return 0, ""
    count = out.count("Operation No.")
    for block in out.split("Operation No."):
        if "grid missing" in block or "+grids=" in block or "hgridshift" in block:
            continue
        _head, sep, tail = block.partition("PROJ string:")
        if not sep:
            continue
        lines = []
        for line in tail.splitlines():
            if line.strip().startswith("---") or (lines and not line.strip()):
                break
            if line.strip():
                lines.append(line.strip())
        if lines and lines[0].startswith("+proj"):
            return count, " ".join(lines)
    return count, ""


def _coordinate_operations(layers: Sequence[Layer], project_crs: str) -> Dict[str, str]:
    """``{layer_crs: proj_pipeline}`` to pin in the project.

    OFF BY DEFAULT, and it should stay off. A pinned operation is a PROJ
    pipeline string baked in by the machine that generated the project; if the
    opening machine's PROJ does not accept it, every layer in that CRS becomes
    unplaceable ("Forward transform of bounding box failed") and a service layer
    that cannot compute an extent is reported invalid. That was observed on a
    real machine for EPSG:3857, then again for EPSG:2229 and EPSG:5514, while
    the same project worked where it was built. Unpinned, QGIS computes a
    transform that works locally — which is the whole point.

    The dialog this was meant to suppress ("Multiple operations are possible
    for converting coordinates between ...") is handled instead by
    suppress_datum_prompt, which changes a QGIS preference and touches no maths.

    A *grid-free* operation is preferred even when a grid-based one is more
    accurate, for two reasons: it does not depend on which PROJ grids happen to
    be installed on the machine, and the engine under test applies parameter
    based (+towgs84) datum shifts only — no NTv2/NADGRIDS — so this is the
    operation that matches what the pipeline actually does. allowFallback stays
    on, so QGIS may still substitute something better locally.
    """
    wanted = sorted({lyr.crs for lyr in layers if lyr.crs and lyr.crs != project_crs})
    found: Dict[str, str] = {}
    for authid in wanted:
        count, pipeline = _operation_via_qgis(authid, project_crs)
        if not count:
            count, pipeline = _operation_via_projinfo(authid, project_crs)
        if count <= 1:
            # Exactly one way to do it: QGIS will never ask, and pinning can
            # only substitute a pipeline string generated by THIS machine's PROJ
            # for one the opening machine would have computed correctly itself.
            # That is not theoretical — a pinned EPSG:3857 operation broke every
            # 3857 layer on a different PROJ build while working here.
            continue
        if pipeline:
            found[authid] = pipeline
        else:
            REPORT.skip(f"coordinate operation {authid} -> {project_crs}",
                        f"{count} candidates but none usable here — QGIS may ask "
                        f"which transformation to use when the project opens")
    if found:
        REPORT.ok(f"pinned {len(found)} datum transformation(s) "
                  f"({', '.join(sorted(found))}) — the only CRS here with more "
                  f"than one candidate operation")
    return found


def _layer_id(layer: Layer) -> str:
    slug = "".join(c if c.isalnum() else "_" for c in f"{layer.row}_{layer.name}")[:48]
    digest = hashlib.md5(f"{layer.row}|{layer.source}".encode("utf-8")).hexdigest()[:8]
    return f"tt_{slug}_{digest}"


def write_project(cfg: Cfg, layers: Sequence[Layer]) -> Path:
    """Write the .qgs. Written by hand, deliberately: QGIS is not importable
    outside QGIS, and the whole point is that this runs anywhere."""
    ids = {id(lyr): _layer_id(lyr) for lyr in layers}
    raster_stats = _raster_stats(cfg, layers)
    build = BUILD_STAMP[0]

    root = ET.Element("qgis", {
        "projectname": f"Waveshed torture set - build {build}",
        "version": cfg.qgis_version,
        "saveUser": "", "saveUserFull": "",
    })
    ET.SubElement(root, "homePath", {"path": ""})
    # The build stamp is in the window title, in a project property and in the
    # macro's first log line. Without it there is no way to tell a real fault
    # from a project that was simply never reloaded — which has cost real time.
    ET.SubElement(root, "title").text = f"Waveshed torture set - build {build}"
    ET.SubElement(root, "transaction", {"mode": "Disabled"})
    ET.SubElement(root, "projectFlags", {"set": ""})
    _srs(ET.SubElement(root, "projectCrs"), cfg.project_crs)

    # --- layer tree ---------------------------------------------------------
    tree = ET.SubElement(root, "layer-tree-group")
    ET.SubElement(tree, "customproperties")
    for group in (G_XYZ, G_LOCAL, G_BLD, G_SRV, G_RUN):
        members = [lyr for lyr in layers if lyr.group == group]
        if not members:
            continue
        gnode = ET.SubElement(tree, "layer-tree-group", {
            "name": group, "checked": "Qt::Checked", "expanded": "1"})
        ET.SubElement(gnode, "customproperties")
        for lyr in members:
            node = ET.SubElement(gnode, "layer-tree-layer", {
                "id": ids[id(lyr)], "name": lyr.name, "source": lyr.source,
                "providerKey": lyr.provider,
                "checked": "Qt::Checked" if lyr.checked else "Qt::Unchecked",
                "expanded": "0", "legend_exp": "", "legend_split_behavior": "0",
                "patch_size": "-1,-1"})
            ET.SubElement(node, "customproperties")

    # The tree is ordered like the guide so a row is easy to find, but drawing
    # order must be the opposite of that: reference geometry on top, basemaps
    # at the bottom, or the OSM tiles of row 1.7 paint over the run matrix.
    # That is exactly what the custom layer order exists for.
    draw_order = [lyr for group in (G_RUN, G_BLD, G_LOCAL, G_SRV, G_XYZ)
                  for lyr in layers if lyr.group == group]
    custom = ET.SubElement(tree, "custom-order", {"enabled": "1"})
    for lyr in draw_order:
        ET.SubElement(custom, "item").text = ids[id(lyr)]

    ET.SubElement(root, "relations")

    # --- canvas -------------------------------------------------------------
    canvas = ET.SubElement(root, "mapcanvas", {"name": "theMapCanvas", "annotationsVisible": "1"})
    ET.SubElement(canvas, "units").text = _CRS_DEFS.get(cfg.project_crs, {}).get("units", "degrees")
    extent = ET.SubElement(canvas, "extent")
    # Europe — the Bern reference AOI, the NRW WCS box and the Dead Sea are all
    # reachable from here; the other run-matrix cases are a "zoom to layer" away.
    for tag, val in (("xmin", "-20"), ("ymin", "30"), ("xmax", "40"), ("ymax", "60")):
        ET.SubElement(extent, tag).text = val
    ET.SubElement(canvas, "rotation").text = "0"
    _srs(ET.SubElement(canvas, "destinationsrs"), cfg.project_crs)
    ET.SubElement(canvas, "rendermaptile").text = "0"

    # --- layers -------------------------------------------------------------
    projectlayers = ET.SubElement(root, "projectlayers")
    for lyr in layers:
        ml = ET.SubElement(projectlayers, "maplayer", {
            "type": lyr.kind,
            "hasScaleBasedVisibilityFlag": "1" if lyr.min_scale else "0",
            "minScale": f"{lyr.min_scale:g}" if lyr.min_scale else "1e+08",
            "maxScale": "0",
            "autoRefreshTime": "0", "refreshOnNotifyEnabled": "0",
            "refreshOnNotifyMessage": "", "styleCategories": "AllStyleCategories",
            **({"geometry": lyr.geometry} if lyr.geometry else {}),
        })
        ET.SubElement(ml, "id").text = ids[id(lyr)]
        ET.SubElement(ml, "datasource").text = lyr.source
        ET.SubElement(ml, "layername").text = lyr.name
        if lyr.crs:
            # Not documentation: what is written here WINS over the provider's
            # own CRS, so it has to be a complete, restorable definition.
            srs_holder = ET.SubElement(ml, "srs")
            _srs(srs_holder, lyr.crs)
            if len(srs_holder) == 0:
                ml.remove(srs_holder)   # no definition -> let the provider decide
                # ...and stop the catalogue claiming a CRS the project does not
                # assert. write_manifest runs after this, so blanking it here
                # keeps the runner from checking an expectation nothing set.
                REPORT.skip(f"row {lyr.row} CRS ({lyr.crs})",
                            "no complete CRS definition could be built on this machine — "
                            "the layer takes the provider's CRS and the row's CRS check "
                            "is dropped")
                lyr.crs = ""
        prov = ET.SubElement(ml, "provider")
        prov.text = lyr.provider
        if lyr.kind == "vector":
            prov.set("encoding", "UTF-8")
        abstract = ET.SubElement(ml, "abstract")
        abstract.text = f"[{lyr.row}] {lyr.expect}"
        if lyr.style:
            _symbol(ml, lyr.style, lyr.color)
        if lyr.kind == "raster":
            stats = raster_stats.get(lyr.source)
            if lyr.render == "picture":
                _raster_pipe(ml, kind="colordata")
            elif lyr.render == "values":
                _raster_pipe(ml, cfg.dem_stretch_min, cfg.dem_stretch_max,
                             live_stretch=True)
            elif stats and stats[1] > stats[0]:
                _raster_pipe(ml, stats[0], stats[1])
            else:
                # A file raster whose statistics could not be read — still give
                # it a renderer rather than leaving it to chance.
                _raster_pipe(ml, cfg.dem_stretch_min, cfg.dem_stretch_max)
        ET.SubElement(ml, "blendMode").text = "0"

    order = ET.SubElement(root, "layerorder")
    for lyr in layers:
        ET.SubElement(order, "layer", {"id": ids[id(lyr)]})

    # --- properties ---------------------------------------------------------
    props = ET.SubElement(root, "properties")
    paths = ET.SubElement(props, "Paths")
    ET.SubElement(paths, "Absolute", {"type": "bool"}).text = "false"
    # QgsProject reads <projectCrs> ONLY when this property says on-the-fly
    # reprojection is enabled. Without it the project loads with no CRS at all
    # and every layer is drawn in its own coordinates.
    stamp = ET.SubElement(props, "WaveshedTorture")
    ET.SubElement(stamp, "build", {"type": "QString"}).text = build
    refsys = ET.SubElement(props, "SpatialRefSys")
    ET.SubElement(refsys, "ProjectionsEnabled", {"type": "int"}).text = "1"
    ET.SubElement(refsys, "ProjectCrs", {"type": "QString"}).text = cfg.project_crs
    crs_spec = _CRS_DEFS.get(cfg.project_crs)
    if crs_spec:
        ET.SubElement(refsys, "ProjectCRSProj4String",
                      {"type": "QString"}).text = crs_spec["proj4"]
    # The whole setup, run by QGIS itself when the project opens (qgis.utils
    # execs this and calls openProject()). One "Enable macros" click instead of
    # a console session and a second terminal for the tile server.
    macros = ET.SubElement(props, "Macros")
    ET.SubElement(macros, "pythonCode", {"type": "QString"}).text = _setup_code(cfg, layers, MACRO)
    gui = ET.SubElement(props, "Gui")
    ET.SubElement(gui, "CanvasColorBluePart", {"type": "int"}).text = "255"
    ET.SubElement(gui, "CanvasColorGreenPart", {"type": "int"}).text = "255"
    ET.SubElement(gui, "CanvasColorRedPart", {"type": "int"}).text = "255"
    ET.SubElement(root, "visibility-presets")
    # Pin one datum transformation per CRS so the project never opens with the
    # "Multiple operations are possible ..." dialog. Both spellings are written:
    # the compact attribute form that QGIS has always read, and the <src>/<dest>
    # children that newer builds write themselves.
    context = ET.SubElement(root, "transformContext")
    if cfg.pin_datum_transforms:
        for authid, pipeline in _coordinate_operations(layers, cfg.project_crs).items():
            entry = ET.SubElement(context, "srcDest", {
                "source": authid, "dest": cfg.project_crs,
                "coordinateOp": pipeline, "allowFallback": "1"})
            _srs(ET.SubElement(entry, "src"), authid)
            _srs(ET.SubElement(entry, "dest"), cfg.project_crs)
    meta = ET.SubElement(root, "projectMetadata")
    # QgsProject::title() reads the METADATA title, not <title> — so the build
    # stamp has to live here to be visible without opening anything.
    ET.SubElement(meta, "title").text = f"Waveshed torture set - build {build}"
    ET.SubElement(meta, "abstract").text = (
        "Generated by tools/make_torture_project.py from tests/TEST_DATA_GUIDE.md. "
        "Every layer is named by its guide row; the row's expectation is in the "
        "layer abstract (Layer Properties -> Information). See README.md next to "
        "this project.")
    ET.SubElement(root, "Annotations")
    ET.SubElement(root, "Layouts")

    if hasattr(ET, "indent"):
        ET.indent(root, space="  ")
    path = cfg.out_dir / "waveshed_torture.qgs"
    path.write_text("<!DOCTYPE qgis PUBLIC 'http://mrcc.com/qgis.dtd' 'SYSTEM'>\n"
                    + ET.tostring(root, encoding="unicode") + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Browser connections — written by a script the user runs in QGIS
# ---------------------------------------------------------------------------

def _connections(cfg: Cfg, layers: Sequence[Layer]) -> Dict[str, List[Dict[str, str]]]:
    """Browser connections, built FROM THE CATALOGUE — never beside it.

    These used to be a parallel literal list, and it drifted: the row 1.5
    connection pointed at a path that does not exist on AWS (>100 404s and a
    visibly slower project on every pan, the exact behaviour the layer was
    changed to avoid), row 1.4's connection said zmax=12 where the layer says
    14 — hiding the two deepest zooms, which is where a codec problem shows up
    — and row 1.10 had no connection at all. Deriving them from the same Layer
    objects that produce the project makes all three impossible.
    """
    xyz = []
    for lyr in layers:
        if "type=xyz" not in lyr.source:
            continue
        params = dict(urllib.parse.parse_qsl(lyr.source))
        xyz.append({
            "name": f"TT {lyr.name.split(' ', 1)[0]} "
                    f"{lyr.name.split(' ', 1)[1][:44] if ' ' in lyr.name else ''}".strip(),
            "url": urllib.parse.unquote(params.get("url", "")),
            "zmin": params.get("zmin", "0"),
            "zmax": params.get("zmax", "15"),
            "interpretation": params.get("interpretation", ""),
        })
    return {
        "xyz": xyz,
        "wms": [
            {"name": "TT 4.1 terrestris OSM WMS", "url": "https://ows.terrestris.de/osm/service"},
            {"name": "TT 4.2 swisstopo WMTS",
             "url": "https://wmts.geo.admin.ch/EPSG/3857/1.0.0/WMTSCapabilities.xml"},
            {"name": "TT 4.4 USGS 3DEP WMS",
             "url": "https://elevation.nationalmap.gov/arcgis/services/3DEPElevation/ImageServer/WMSServer"},
            {"name": "TT 4.5 swisstopo WMS", "url": "https://wms.geo.admin.ch/"},
        ],
        "wcs": [
            {"name": "TT 4.3 NRW DGM WCS", "url": "https://www.wcs.nrw.de/geobasis/wcs_nw_dgm"},
            {"name": "TT 4.4a USGS 3DEP WCS",
             "url": "https://elevation.nationalmap.gov/arcgis/services/3DEPElevation/"
                    "ImageServer/WCSServer"},
        ],
        "arcgis": [
            {"name": "TT 4.4 USGS 3DEP ImageServer",
             "url": "https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer"},
        ],
    }


# The whole setup, shared verbatim by the project macro (runs on open, one
# click) and the console script (the fallback when macros are switched off).
# Written once so the two can never drift apart.
SETUP_BODY = '''
import os
import time

from qgis.core import Qgis, QgsMessageLog, QgsProject, QgsSettings

CONNECTIONS = __CONNECTIONS_JSON__
TILE_PORT = __TILE_PORT__
TILE_AUTOSTART = __TILE_AUTOSTART__
LOG_TAG = "Waveshed torture"
DEM_STRETCH = (__DEM_MIN__, __DEM_MAX__)
PROBE_URLS = __PROBE_URLS_JSON__
SUPPRESS_DATUM_PROMPT = __SUPPRESS_PROMPT__
BUILD = "__BUILD__"
STEPS = 6

_server = None


def _log(message, level=None):
    QgsMessageLog.logMessage(message, LOG_TAG,
                             getattr(Qgis, "Info", 0) if level is None else level)


class _Step:
    """Log a step's start and its result, so a hang is visible as a start with
    no matching finish. Silence is the one thing a setup routine must not do."""

    def __init__(self, index, title):
        self.index = index
        self.title = title
        self.t0 = 0.0

    def __enter__(self):
        self.t0 = time.monotonic()
        _log(f"[{self.index}/{STEPS}] {self.title} ...")
        return self

    def ok(self, result):
        _log(f"[{self.index}/{STEPS}] {self.title}: {result} "
             f"({time.monotonic() - self.t0:.2f} s)")

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            _log(f"[{self.index}/{STEPS}] {self.title} FAILED after "
                 f"{time.monotonic() - self.t0:.2f} s: {exc}",
                 getattr(Qgis, "Critical", 2))
        return False


def torture_dir():
    """The folder holding the project — everything else is relative to it."""
    name = QgsProject.instance().fileName()
    if name:
        return os.path.dirname(name)
    return os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else ""


def register_connections():
    """Write the Browser connections of TEST_DATA_GUIDE.md sections 1 and 4.

    Includes the XYZ *interpretation* (Terrarium / Mapbox Terrain RGB), which
    nothing but the connection dialog can otherwise set. Both the classic
    settings group (qgis/connections-xyz/...) and the newer settings-tree group
    (connections/xyz/items/...) are written, because QGIS moved them between
    versions; whichever one your build does not read is inert.
    """
    s = QgsSettings()

    def write(keys):
        for key, value in keys.items():
            s.setValue(key, value)

    n = 0
    for conn in CONNECTIONS["xyz"]:
        for base in (f"qgis/connections-xyz/{conn['name']}",
                     f"connections/xyz/items/{conn['name']}"):
            write({
                f"{base}/url": conn["url"],
                f"{base}/zmin": conn["zmin"],
                f"{base}/zmax": conn["zmax"],
                f"{base}/username": "",
                f"{base}/password": "",
                f"{base}/authcfg": "",
                f"{base}/referer": "",
                f"{base}/tilePixelRatio": 0,
                f"{base}/interpretation": conn["interpretation"],
            })
        n += 1
    for conn in CONNECTIONS["wms"]:
        for base in (f"qgis/connections-wms/{conn['name']}",
                     f"connections/ows/items/wms/connections/items/{conn['name']}"):
            write({
                f"{base}/url": conn["url"],
                f"{base}/dpiMode": 7,
                f"{base}/ignoreAxisOrientation": False,
                f"{base}/ignoreGetFeatureInfoURI": False,
                f"{base}/ignoreGetMapURI": False,
                f"{base}/invertAxisOrientation": False,
                f"{base}/smoothPixmapTransform": False,
            })
        n += 1
    for conn in CONNECTIONS["wcs"]:
        for base in (f"qgis/connections-wcs/{conn['name']}",
                     f"connections/ows/items/wcs/connections/items/{conn['name']}"):
            write({f"{base}/url": conn["url"], f"{base}/dpiMode": 7})
        n += 1
    for conn in CONNECTIONS["arcgis"]:
        for base in (f"qgis/connections-arcgismapserver/{conn['name']}",
                     f"qgis/connections-arcgisfeatureserver/{conn['name']}",
                     f"connections/arcgis/items/{conn['name']}"):
            write({f"{base}/url": conn["url"]})
        n += 1
    return (f"{len(CONNECTIONS['xyz'])} XYZ, {len(CONNECTIONS['wms'])} WMS/WMTS, "
            f"{len(CONNECTIONS['wcs'])} WCS, {len(CONNECTIONS['arcgis'])} ArcGIS "
            f"(F5 in the Browser panel to see them)"), n


def start_tile_server():
    """Serve the local Terrain-RGB fixture (row 1.3b) from inside QGIS.

    A daemon thread bound to 127.0.0.1 only, serving the project folder, so
    row 1.3b needs no second terminal and no second thing to remember. It dies
    with QGIS, or when the project is closed (closeProject below).
    """
    global _server
    import functools
    import http.server
    import threading

    root = torture_dir()
    if not TILE_AUTOSTART or not root or not os.path.isdir(os.path.join(root, "terrain-rgb")):
        return "no local tile fixture — row 1.3b is not built"
    if _server is not None:
        return f"already serving on http://127.0.0.1:{TILE_PORT}/"
    tiles = sum(len(files) for _r, _d, files in os.walk(os.path.join(root, "terrain-rgb")))

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass          # do not spam the QGIS log with one line per tile

    try:
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", TILE_PORT),
            functools.partial(Handler, directory=root))
    except OSError as exc:
        return (f"port {TILE_PORT} is busy ({exc}) — if something else is already "
                f"serving this folder, row 1.3b works anyway")
    threading.Thread(target=server.serve_forever, daemon=True,
                     name="waveshed-torture-tiles").start()
    _server = server
    return (f"{tiles} tiles on http://127.0.0.1:{TILE_PORT}/terrain-rgb/ "
            f"(row 1.3b is live)")


def stop_tile_server():
    global _server
    if _server is not None:
        try:
            _server.shutdown()
            _server.server_close()
        finally:
            _server = None


def check_coordinate_transforms():
    """Check every pinned datum transformation ON THIS MACHINE, drop the bad ones.

    A pinned operation is a PROJ pipeline string baked in by whichever machine
    generated the project. If the opening machine's PROJ does not accept it, the
    transform silently fails and every layer in that CRS becomes unplaceable —
    the log fills with "Forward transform of bounding box failed" and a WMTS
    layer that cannot compute an extent is reported invalid. QGIS would have
    computed a working transform by itself; the pin is what took that away.

    So each pin is tried here, on the exact thing that fails: transforming a
    real layer extent. Anything that does not survive is removed and named.
    """
    from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                           QgsCoordinateTransformContext, QgsPointXY)

    project = QgsProject.instance()
    context = project.transformContext()
    pinned = dict(context.coordinateOperations())
    destination = project.crs()

    if SUPPRESS_DATUM_PROMPT:
        # Stop QGIS asking which datum transformation to use. A preference, not
        # the transformation — unlike pinning a pipeline into the project, which
        # is what broke layers on other machines.
        #
        # The App SECTION is the one that matters: QgsSettings sections prefix
        # the key, and an app preference written at the root lands in
        # [Projections] while QGIS reads it from [app]. Both are written, and
        # this only takes effect from the NEXT open — layers (and the dialog)
        # come before project macros run.
        settings = QgsSettings()
        key = "Projections/promptWhenMultipleTransformsExist"
        settings.setValue(key, False)
        try:
            settings.setValue(key, False, QgsSettings.App)
        except Exception:
            pass
        settings.sync()

    # Every CRS in the project is checked, pinned or not: if a transform is
    # broken here, the layers in that CRS will not draw and the reason should
    # be named rather than left as "Forward transform ... failed" in the log.
    checked = {lyr.crs().authid() for lyr in project.mapLayers().values()
               if lyr.crs().isValid() and lyr.crs() != destination}
    for authid in checked:
        pinned.setdefault((authid, destination.authid()), "")
    plain = QgsCoordinateTransformContext()      # deliberately unpinned
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")

    broken = []
    for key in pinned:
        source_id = key[0] if isinstance(key, (tuple, list)) else str(key)
        crs = QgsCoordinateReferenceSystem(source_id)
        limits = crs.bounds()                     # the CRS's area of use, in WGS84
        if not crs.isValid() or limits.isEmpty():
            continue
        # Round-trip a point that is certainly inside this CRS's domain: convert
        # it INTO the CRS without the project's pins, then back out WITH them.
        # If the pinned pipeline is wrong, the return trip lands somewhere else.
        # Deliberately not using a layer's extent(): QGIS reports the extent of
        # an XYZ layer in degrees even though its CRS is EPSG:3857, so a check
        # built on that tests nothing.
        # Off-origin sample points, NOT the centre of the area of use: the
        # centre of EPSG:3857 is (0, 0), where a pipeline that does nothing at
        # all still returns the right answer. Three points, worst error wins.
        samples = [QgsPointXY(limits.xMinimum() + limits.width() * fx,
                              limits.yMinimum() + limits.height() * fy)
                   for fx, fy in ((0.25, 0.25), (0.75, 0.7), (0.6, 0.35))]
        try:
            inward = QgsCoordinateTransform(wgs84, crs, plain)
            outward = QgsCoordinateTransform(crs, destination, context)
            if not (inward.isValid() and outward.isValid()):
                broken.append(source_id)
                continue
            back_to_wgs84 = (QgsCoordinateTransform(destination, wgs84, plain)
                             if destination != wgs84 else None)
            worst = 0.0
            for sample in samples:
                point = outward.transform(inward.transform(sample))
                if back_to_wgs84 is not None:
                    point = back_to_wgs84.transform(point)
                worst = max(worst, abs(point.x() - sample.x()),
                            abs(point.y() - sample.y()))
            # 0.5 deg is ~55 km: far beyond any datum difference, so this only
            # ever fires on a pipeline that is actually broken.
            usable = worst == worst and worst < 0.5
        except Exception:
            usable = False
        if not usable:
            broken.append(source_id)

    pin_count = len(context.coordinateOperations())
    if not broken:
        return (f"{len(pinned)} CRS checked, all transform correctly here"
                + (f" ({pin_count} pinned)" if pin_count else " (none pinned)"))

    rebuilt = QgsCoordinateTransformContext()
    for key, operation in context.coordinateOperations().items():
        source_id = key[0] if isinstance(key, (tuple, list)) else str(key)
        dest_id = key[1] if isinstance(key, (tuple, list)) and len(key) > 1 else destination.authid()
        if source_id in broken or not operation:
            continue
        rebuilt.addCoordinateOperation(QgsCoordinateReferenceSystem(source_id),
                                       QgsCoordinateReferenceSystem(dest_id),
                                       operation, True)
    project.setTransformContext(rebuilt)
    project.setDirty(False)
    for source_id in broken:
        pinned_here = (source_id, destination.authid()) in context.coordinateOperations()
        _log(f"  {source_id} -> {destination.authid()} does NOT transform correctly on this "
             f"machine" + (" — the pinned operation has been dropped, QGIS will compute its "
                           "own instead" if pinned_here else
                           " and nothing in the project is pinning it, so this is a local PROJ "
                           "problem: check Settings > Options > Transformations"),
             getattr(Qgis, "Warning", 1))
    if any((s_id, destination.authid()) in context.coordinateOperations() for s_id in broken):
        _log("  RELOAD the project (Project > Revert) so the layers that failed to "
             "load under the bad transformation come back.", getattr(Qgis, "Warning", 1))
    return (f"{len(pinned)} CRS checked, {len(broken)} BROKEN here: "
            f"{', '.join(broken)}")


def choose_local_transforms():
    """Pick a working datum transformation HERE for any CRS that offers several.

    Only EPSG:2229 and EPSG:5514 do, and QGIS's dialog sorts by accuracy — so
    its top entry for 2229 is a 2 m GRID-BASED operation that fails outright
    when the grid is not installed, which is the trap that made row 2.19 draw
    nothing. The choice is made from what PROJ reports on THIS machine and only
    kept if it survives a round trip, so nothing generated elsewhere is trusted.
    """
    from qgis.core import (QgsCoordinateReferenceSystem, QgsCoordinateTransform,
                           QgsCoordinateTransformContext, QgsDatumTransform, QgsPointXY,
                           QgsRasterLayer)

    project = QgsProject.instance()
    context = project.transformContext()
    destination = project.crs()
    plain = QgsCoordinateTransformContext()
    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    chosen, failed = [], []

    # One layer per CRS to test against — the real extent, because that is the
    # object QGIS fails on.
    probe_layer = {}
    for layer in project.mapLayers().values():
        if isinstance(layer, QgsRasterLayer) and layer.crs().isValid():
            try:
                extent = layer.extent()
            except Exception:
                continue
            if not extent.isEmpty():
                probe_layer.setdefault(layer.crs().authid(), layer)

    for authid in sorted({lyr.crs().authid() for lyr in project.mapLayers().values()
                          if lyr.crs().isValid() and lyr.crs() != destination}):
        crs = QgsCoordinateReferenceSystem(authid)
        try:
            operations = QgsDatumTransform.operations(crs, destination)
        except Exception:
            continue
        if len(operations) < 2:
            continue                      # nothing to choose, nothing to prompt about
        layer = probe_layer.get(authid)
        if layer is None:
            continue

        # Order: grid-free first (a missing grid is the usual killer), then by
        # accuracy. But TRY THEM ALL — "grid-free" is not the discriminator for
        # every CRS, and assuming it is left EPSG:5514 broken.
        candidates = [o for o in operations if o.isAvailable]
        candidates.sort(key=lambda o: (
            1 if ("grids=" in o.proj or "hgridshift" in o.proj or "vgridshift" in o.proj) else 0,
            o.accuracy if o.accuracy >= 0 else 999))

        extent = layer.extent()

        # FIRST: does QGIS already manage without being told anything? On the
        # machine this was chased down on, a point transformed fine, single
        # candidate CRS (2056, 32632) drew fine — and 2229/5514 failed for all
        # four operations. The one thing those two had that the others did not
        # was an operation added to the context by this function. So the
        # default is tried first, and if it works nothing is added at all.
        try:
            box = QgsCoordinateTransform(crs, destination, context) \
                .transformBoundingBox(extent)
            if not box.isEmpty() and box.xMinimum() == box.xMinimum():
                chosen.append((authid, "QGIS's own default (nothing pinned)"))
                continue
        except Exception as exc:
            _log(f"  {authid}: default transform cannot place this extent ({exc}), "
                 f"trying the {len(candidates)} published operations")

        winner = None
        for operation in candidates:
            trial = QgsCoordinateTransformContext(context)
            trial.addCoordinateOperation(crs, destination, operation.proj, True)
            try:
                # THE call QGIS makes and that fails: a bounding box, not a
                # point. It samples across the rectangle, so it can fail where a
                # single mid-domain point succeeds — which is why validating a
                # point passed while the layer stayed blank.
                box = QgsCoordinateTransform(crs, destination, trial) \
                    .transformBoundingBox(extent)
            except Exception as exc:
                # Never swallow this again: without the reason, "none of 4
                # operations work" is a dead end for whoever reads the log.
                _log(f"    {operation.name[:44]}: {exc}")
                continue
            if box.isEmpty() or box.xMinimum() != box.xMinimum():
                continue
            limits = destination.bounds()
            if destination.isGeographic() and not limits.isEmpty():
                if (box.xMinimum() < limits.xMinimum() - 1
                        or box.xMaximum() > limits.xMaximum() + 1
                        or box.yMinimum() < limits.yMinimum() - 1
                        or box.yMaximum() > limits.yMaximum() + 1):
                    continue
            context, winner = trial, operation
            break

        if winner is None:
            failed.append(f"{authid} (none of {len(candidates)} operations, nor QGIS's "
                          f"default, can transform the extent of {layer.name()[:30]}: "
                          f"{extent.xMinimum():.1f},{extent.yMinimum():.1f} : "
                          f"{extent.xMaximum():.1f},{extent.yMaximum():.1f})")
            continue
        needs_grid = ("grids=" in winner.proj or "hgridshift" in winner.proj)
        chosen.append((authid, f"{winner.name.split('+')[-1].strip()[:38]} "
                               f"({winner.accuracy:g} m"
                               f"{', needs a grid' if needs_grid else ', no grid'})"))

    if not chosen and not failed:
        return "no CRS here offers a choice"

    project.setTransformContext(context)
    fixed_ids = {authid for authid, _detail in chosen}

    # Extents were computed during load, before this ran, so the affected layers
    # have to be asked again — and then the answer has to be CHECKED, per layer,
    # in the project CRS. Reporting the choice without confirming the layer can
    # now be placed is how this looked fixed while staying broken.
    verified = []
    for layer in project.mapLayers().values():
        if layer.crs().authid() not in fixed_ids:
            continue
        try:
            layer.reload()
        except Exception:
            pass
        try:
            layer.updateExtents()
        except Exception:
            pass
        try:
            box = QgsCoordinateTransform(layer.crs(), destination, context) \
                .transformBoundingBox(layer.extent())
            placed = not box.isEmpty() and box.xMinimum() == box.xMinimum()
        except Exception:
            placed = False
        verified.append((layer.name(), placed, box if placed else None))
        layer.triggerRepaint()
    project.setDirty(False)

    for authid, detail in chosen:
        _log(f"  {authid}: using {detail}")
    for name, placed, box in verified:
        if placed:
            _log(f"  {name[:44]} placed at {box.xMinimum():.3f},{box.yMinimum():.3f} : "
                 f"{box.xMaximum():.3f},{box.yMaximum():.3f}")
        else:
            _log(f"  {name[:44]} STILL cannot be placed — it will not draw",
                 getattr(Qgis, "Warning", 1))
            failed.append(f"{name[:40]} (extent still not transformable)")
    for line in failed:
        _log("  NO working transformation for " + line, getattr(Qgis, "Warning", 1))
    ok_layers = sum(1 for _n, placed, _b in verified if placed)
    return (f"{len(chosen)} chosen locally, {ok_layers}/{len(verified)} layers placed"
            + (f", {len(failed)} unresolved" if failed else ""))


def repair_renderers():
    """Make every raster layer's renderer match what its provider really returns.

    The project ships a renderer for each layer, but which one is right depends
    on the QGIS version: before 3.28 an XYZ layer ignores `interpretation=` and
    hands back an ARGB32 picture, from 3.28 on the same layer returns Float32
    elevation. A grey renderer on a picture (or colour-data on elevation) draws
    nothing at all, which is how a whole group of layers can look "empty" while
    being perfectly valid. So the decision is made here, against the provider.
    """
    from qgis.core import (QgsContrastEnhancement, QgsRasterLayer,
                           QgsSingleBandColorDataRenderer, QgsSingleBandGrayRenderer)

    image_types = set()
    for name in ("ARGB32", "ARGB32_Premultiplied"):
        for holder in (getattr(Qgis, "DataType", None), Qgis):
            value = getattr(holder, name, None) if holder is not None else None
            if value is not None:
                image_types.add(value)

    fixed = []
    for lyr in QgsProject.instance().mapLayers().values():
        if not isinstance(lyr, QgsRasterLayer) or not lyr.isValid():
            continue
        provider = lyr.dataProvider()
        try:
            is_picture = provider.dataType(1) in image_types
        except Exception:
            continue
        current = type(lyr.renderer()).__name__
        if is_picture and current != "QgsSingleBandColorDataRenderer":
            lyr.setRenderer(QgsSingleBandColorDataRenderer(provider, 1))
            fixed.append(f"{lyr.name()[:28]} -> colour")
        elif not is_picture and current == "QgsSingleBandColorDataRenderer":
            renderer = QgsSingleBandGrayRenderer(provider, 1)
            enhancement = QgsContrastEnhancement(provider.dataType(1))
            enhancement.setContrastEnhancementAlgorithm(
                QgsContrastEnhancement.StretchToMinimumMaximum)
            enhancement.setMinimumValue(DEM_STRETCH[0])
            enhancement.setMaximumValue(DEM_STRETCH[1])
            renderer.setContrastEnhancement(enhancement)
            lyr.setRenderer(renderer)
            fixed.append(f"{lyr.name()[:28]} -> grey")
        else:
            continue
        lyr.triggerRepaint()
    if fixed:
        # Repainting marks the project dirty; this is a repair, not an edit, and
        # QGIS should not ask to save it on the way out.
        QgsProject.instance().setDirty(False)
    return fixed


def probe_hosts():
    """Ask every remote host in this project whether it answers, from HERE.

    Through QgsNetworkAccessManager, so the machine's proxy configuration and
    QGIS's own network settings apply exactly as they do for the layers. This
    is the difference between "the URI is wrong" and "this machine cannot reach
    that host", which is not something a person can tell from a blank canvas.
    """
    from qgis.core import QgsBlockingNetworkRequest, QgsNetworkAccessManager
    from qgis.PyQt.QtCore import QUrl
    from qgis.PyQt.QtNetwork import QNetworkRequest

    attribute = getattr(QNetworkRequest, "Attribute", QNetworkRequest)
    manager = QgsNetworkAccessManager.instance()
    previous_timeout = manager.timeout()
    manager.setTimeout(8000)
    reachable, problems = 0, []
    try:
        for label, url, expect_fail in PROBE_URLS:
            request = QNetworkRequest(QUrl(url))
            try:
                request.setAttribute(attribute.FollowRedirectsAttribute, True)
            except Exception:
                pass
            blocking = QgsBlockingNetworkRequest()
            try:
                code = blocking.head(request, True)
                status = blocking.reply().attribute(attribute.HttpStatusCodeAttribute)
                detail = blocking.errorMessage()
            except Exception as exc:
                # `exc` is bound only inside this block. Reading it further down
                # raised NameError, which setup()'s catch-all reported as "setup
                # failed" — so a single throwing head() silently skipped the
                # renderer repair and the invalid-layer report as well.
                code, status, detail = -1, None, str(exc)
            # Any HTTP answer means the host is reachable; only transport errors
            # (DNS, timeout, proxy, TLS) count against it.
            ok = code == 0 or status is not None
            if ok:
                reachable += 1
            if ok == expect_fail:
                problems.append(f"{label}: "
                                + (f"unexpectedly reachable (HTTP {status})" if ok else
                                   f"UNREACHABLE ({' '.join((detail or '').split())[:70]})"))
    finally:
        manager.setTimeout(previous_timeout)
    for problem in problems:
        _log("  network: " + problem, getattr(Qgis, "Warning", 1))
    return f"{reachable}/{len(PROBE_URLS)} hosts answered" + (
        f", {len(problems)} unexpected" if problems else "")


def check_layers():
    """``(total, [invalid names])`` — a layer that failed is named, not silent."""
    layers = sorted(QgsProject.instance().mapLayers().values(), key=lambda l: l.name())
    bad = [l for l in layers if not l.isValid()]
    for lyr in bad:
        _log(f"INVALID layer: {lyr.name()} -> {lyr.source()}", getattr(Qgis, "Warning", 1))
    return len(layers), [l.name() for l in bad]


def setup():
    """Everything the torture set needs, in one call. Returns a summary line.

    Every step announces itself before it runs and reports its result and
    duration after, so the log answers "is it stuck, or done?" on its own.
    """
    t0 = time.monotonic()
    _log(f"setup starting - build {BUILD} - {STEPS} steps, project: "
         f"{os.path.basename(QgsProject.instance().fileName()) or '(none)'}")
    try:
        from qgis.utils import iface
        iface.messageBar().pushMessage(
            "Waveshed torture set", "setting up - progress in Log Messages -> "
            "Waveshed torture", level=getattr(Qgis, "Info", 0), duration=4)
    except Exception:
        pass

    with _Step(1, "Browser connections") as step:
        detail, connections = register_connections()
        step.ok(detail)
    with _Step(2, "local Terrain-RGB tile server") as step:
        tiles = start_tile_server()
        step.ok(tiles)
    with _Step(3, "coordinate transforms") as step:
        selection = choose_local_transforms()
        transforms = check_coordinate_transforms() + f" | {selection}"
        step.ok(transforms + (" | datum prompt off from the next open"
                              if SUPPRESS_DATUM_PROMPT else ""))
    with _Step(4, "network reachability") as step:
        network = probe_hosts()
        step.ok(network)
    with _Step(5, "renderer check") as step:
        fixed = repair_renderers()
        step.ok(f"{len(fixed)} renderer(s) adjusted to this QGIS build"
                + (": " + "; ".join(fixed[:4]) if fixed else " — project was already right"))
    with _Step(6, "layer check") as step:
        total, bad = check_layers()
        step.ok(f"{total} layers, {len(bad)} invalid"
                + (": " + ", ".join(bad[:5]) if bad else ""))

    summary = (f"build {BUILD} - {total} layers ({len(bad)} invalid), "
               f"{connections} Browser connections, {network}, {transforms}, {tiles}")
    _log(f"setup complete in {time.monotonic() - t0:.2f} s - {summary}")
    try:
        from qgis.utils import iface
        level = getattr(Qgis, "Warning", 1) if bad else getattr(Qgis, "Success", 3)
        iface.messageBar().pushMessage("Waveshed torture set ready", summary,
                                       level=level, duration=15)
    except Exception:
        pass              # no GUI (headless / console-less run) — the log has it
    print("[torture] " + summary)
    return summary
'''

BOOTSTRAP = '''"""Set up the torture set by hand — the fallback when macros are off.

The project normally does this itself: open ``waveshed_torture.qgs`` and click
**Enable macros** in the bar at the top. Use this script only if macros are
disabled in your profile (Settings -> Options -> General -> Enable macros).

Run it inside QGIS: Plugins -> Python Console -> "Show editor" -> open this
file -> Run. Or paste this one line into the console:

    exec(open(r"__SELF__", encoding="utf-8").read())

It registers the Browser connections, starts the local Terrain-RGB tile server
for row 1.3b, and reports any layer that failed to load.
"""
__SETUP_BODY__

setup()
'''

MACRO = '''"""Waveshed torture set — project macro (see tools/make_torture_project.py).

QGIS runs this when the project opens, once you allow macros. It registers the
Browser connections, serves the local Terrain-RGB fixture on 127.0.0.1 so row
1.3b works without a second terminal, and reports any layer that failed to
load. Nothing here touches your data or the network beyond localhost.

Set Settings -> Options -> General -> Enable macros = "Always" to skip the
prompt on every open. Everything it does is also in qgis_console_bootstrap.py.
"""
__SETUP_BODY__


def openProject():
    try:
        setup()
    except Exception as exc:          # never let a macro break project loading
        _log(f"setup failed: {exc}", getattr(Qgis, "Critical", 2))


def saveProject():
    pass


def closeProject():
    try:
        stop_tile_server()
    except Exception:
        pass
'''


def _probe_urls(cfg: Cfg) -> List[List[Any]]:
    """``[label, url, expected_to_fail]`` — one cheap HEAD per remote host."""
    host, port = _tile_server_target(cfg)
    probes: List[List[Any]] = [
        ["AWS terrain tiles (rows 1.1/1.2)",
         "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/10/533/361.png", False],
        ["OpenStreetMap tiles (row 1.7)",
         "https://tile.openstreetmap.org/10/533/361.png", False],
        ["terrestris WMS (row 4.1)", "https://ows.terrestris.de/osm/service", False],
        ["swisstopo WMTS capabilities (row 4.2)",
         "https://wmts.geo.admin.ch/EPSG/3857/1.0.0/WMTSCapabilities.xml", False],
        ["swisstopo WMS (rows 4.5/4.6)", "https://wms.geo.admin.ch/", False],
        ["NRW WCS (row 4.3)", "https://www.wcs.nrw.de/geobasis/wcs_nw_dgm", False],
        ["USGS 3DEP (rows 4.4a/4.4b)",
         "https://elevation.nationalmap.gov/arcgis/services/3DEPElevation/ImageServer/"
         "WCSServer?SERVICE=WCS&REQUEST=GetCapabilities&VERSION=1.0.0", False],
        ["Copernicus S3 (row 2.23 /vsicurl)",
         COPERNICUS_URL.format(tile=COP_BERN), False],
        ["dead endpoint — MUST fail (row 1.6)",
         "https://tiles.example.invalid/terrarium/10/533/361.png", True],
    ]
    if cfg.maptiler_key:
        probes.insert(2, ["MapTiler (rows 1.3a/1.4)",
                          f"https://api.maptiler.com/tiles/terrain-rgb/10/533/361.png"
                          f"?key={cfg.maptiler_key}", False])
    if host:
        probes.append(["local tile server (rows 1.3b/1.10)",
                       f"http://{host}:{port}/terrain-rgb/12/2131/1440.png", False])
    return probes


def _setup_code(cfg: Cfg, layers: Sequence[Layer], template: str) -> str:
    """Fill *template*'s placeholders with this build's connections and port."""
    host, port = _tile_server_target(cfg)
    body = (SETUP_BODY
            .replace("__CONNECTIONS_JSON__", json.dumps(_connections(cfg, layers), indent=4))
            .replace("__TILE_PORT__", str(port))
            .replace("__TILE_AUTOSTART__",
                     "True" if (cfg.tile_server_autostart and host) else "False")
            .replace("__DEM_MIN__", repr(cfg.dem_stretch_min))
            .replace("__DEM_MAX__", repr(cfg.dem_stretch_max))
            # repr(), not json.dumps(): this is exec'd as Python and JSON
            # booleans are lower-case, which is a NameError there.
            .replace("__PROBE_URLS_JSON__", repr(_probe_urls(cfg)))
            .replace("__SUPPRESS_PROMPT__", repr(cfg.suppress_datum_prompt))
            .replace("__BUILD__", BUILD_STAMP[0]))
    return template.replace("__SETUP_BODY__", body)


def _tile_server_target(cfg: Cfg) -> Tuple[str, int]:
    """``(host, port)`` of the Terrain-RGB fixture URL, ``("", 0)`` if remote.

    Only a loopback URL may be auto-served: pointing a listener at someone
    else's host because a config string said so is not this tool's business.
    """
    parsed = urllib.parse.urlparse(cfg.terrain_rgb_base_url)
    host = (parsed.hostname or "").lower()
    if host not in ("localhost", "127.0.0.1", "::1"):
        return "", 0
    return host, parsed.port or (443 if parsed.scheme == "https" else 80)


RENDER_CHECK = '''"""Render every layer and report the ones that draw nothing.

Run it in QGIS: Plugins -> Python Console, then

    exec(open(r"__SELF__", encoding="utf-8").read())

Why this exists: a layer can be perfectly valid and still draw nothing — wrong
renderer for the provider's data type, a service that has no data where you are
looking, a codec QGIS cannot decode. "45 layers, 0 invalid" does not catch any
of that. This renders each layer over ground where it should HAVE data and
counts the distinct colours that come out.

It takes a couple of minutes: every remote layer is really fetched.
"""

from qgis.core import (QgsCoordinateReferenceSystem, QgsMapRendererSequentialJob,
                       QgsMapSettings, QgsProject, QgsRasterLayer, QgsRectangle,
                       QgsVectorLayer)
from qgis.PyQt.QtCore import QSize
from qgis.PyQt.QtGui import QColor

# Where each row actually has data. Anything not listed is probed over its own
# extent, or over the reference AOI when that extent is global.
PROBES = __PROBES_JSON__
REFERENCE_AOI = __AOI_JSON__
#: The rows the catalogue says must draw NOTHING, written from the same Layer
#: objects that produced the project — so this sentence can never tell you a
#: correctly-rendering layer is a failure.
EXPECTED_EMPTY = __EXPECTED_EMPTY__


def _probe_extent(layer):
    for prefix, box in PROBES.items():
        if layer.name().startswith(prefix):
            return QgsRectangle(*box)
    extent = layer.extent()
    if extent.isEmpty() or extent.width() > 60 or extent.height() > 60:
        return QgsRectangle(*REFERENCE_AOI)
    return extent


def render_check(only=""):
    project = QgsProject.instance()
    build, _ok = project.readEntry("WaveshedTorture", "/build")
    print(f"project build: {build or '(no build stamp - this project predates them)'}")
    print(f"project file : {project.fileName()}")
    layers = sorted(project.mapLayers().values(), key=lambda l: l.name())
    print(f"{'layer':52s} {'colours':>7s}  verdict")
    empty = []
    for layer in layers:
        if only and not layer.name().startswith(only):
            continue
        if not layer.isValid():
            reason = " ".join((layer.error().summary() or "").split())[:90]
            print(f"{layer.name()[:52]:52s} {'-':>7s}  INVALID: {reason}")
            empty.append(layer.name())
            continue
        # A scale-limited layer (4.3, 4.4a) would be hidden at this thumbnail
        # size, which says nothing about whether its data is reachable. The
        # limit is a rule about the canvas, not about the layer, so it is
        # switched off for the probe and put back afterwards.
        had_limit = layer.hasScaleBasedVisibility()
        if had_limit:
            layer.setScaleBasedVisibility(False)
        settings = QgsMapSettings()
        settings.setLayers([layer])
        settings.setOutputSize(QSize(160, 160))
        settings.setBackgroundColor(QColor(255, 255, 255))
        settings.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
        settings.setExtent(_probe_extent(layer))
        job = QgsMapRendererSequentialJob(settings)
        job.start()
        job.waitForFinished()
        image = job.renderedImage()
        if had_limit:
            layer.setScaleBasedVisibility(True)
        colours = {image.pixel(x, y) for x in range(0, 160, 4) for y in range(0, 160, 4)}
        if len(colours) <= 1:
            provider_error = ""
            if isinstance(layer, QgsRasterLayer):
                provider_error = " ".join((layer.dataProvider().lastError() or "").split())[:70]
            verdict = "EMPTY - draws nothing" + (f" [{provider_error}]" if provider_error else "")
            empty.append(layer.name())
        elif len(colours) <= 3:
            verdict = "nearly uniform - check the stretch"
        else:
            verdict = "ok"
        kind = "R" if isinstance(layer, QgsRasterLayer) else (
            "V" if isinstance(layer, QgsVectorLayer) else "?")
        print(f"{layer.name()[:52]:52s} {len(colours):7d}  {kind} {verdict}")

    print()
    if empty:
        print(f"{len(empty)} layer(s) drew nothing:")
        for name in empty:
            print(f"  - {name}")
        print("Expected to be empty: " + (EXPECTED_EMPTY or "no layer at all"))
        print("1.3b and 1.10 need the local tile server — they are blank unless the "
              "project macro ran (or you serve data/torture yourself).")
        print("A remote layer that is INVALID or blank here but works in a browser is "
              "usually the network: proxy settings, or Settings -> Options -> Network "
              "-> Timeout (the swisstopo WMTS capabilities document is 1.7 MB).")
    else:
        print("every layer drew something over ground where it should have data.")
    return empty


render_check()
'''


def write_render_check(cfg: Cfg, layers: Sequence[Layer]) -> Path:
    """The check my own validation could not do: does each layer actually draw?"""
    probes = {}
    for case in RUN_MATRIX:
        box = [case["west"], case["south"], case["east"], case["north"]]
        if case["name"].startswith("NRW"):
            probes["4.3"] = box
        elif case["name"].startswith("Colorado"):
            probes["4.4"] = box
        elif case["name"].startswith("Los Angeles"):
            probes["2.19"] = box
        elif case["name"].startswith("Prague"):
            probes["2.20"] = box
        elif case["name"].startswith("Dead Sea"):
            probes["2.4"] = box
    path = cfg.out_dir / "qgis_console_render_check.py"
    empty = ", ".join(f"{l.row} ({l.expect})" if len(l.expect) < 40 else l.row
                      for l in layers if not l.expect_renders)
    text = (RENDER_CHECK
            .replace("__EXPECTED_EMPTY__", json.dumps(empty))
            .replace("__PROBES_JSON__", json.dumps(probes, indent=4))
            .replace("__AOI_JSON__", json.dumps([AOI_W, AOI_S, AOI_E, AOI_N]))
            .replace("__SELF__", str(path).replace("\\", "/")))
    path.write_text(text, encoding="utf-8")
    return path


def write_bootstrap(cfg: Cfg, layers: Sequence[Layer]) -> Path:
    path = cfg.out_dir / "qgis_console_bootstrap.py"
    text = _setup_code(cfg, layers, BOOTSTRAP).replace("__SELF__", str(path).replace("\\", "/"))
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Launchers
# ---------------------------------------------------------------------------

def _qgis_exe_candidates(cfg: Cfg) -> List[str]:
    """Where to look for qgis-bin.exe, most specific first."""
    found: List[str] = []
    if cfg.qgis_exe:
        found.append(cfg.qgis_exe)
    deploy = REPO / "deploy.local.ini"
    if deploy.is_file():
        parser = configparser.ConfigParser()
        try:
            parser.read(deploy, encoding="utf-8")
            exe = parser.get("paths", "qgis_exe", fallback="").strip()
            if exe and exe not in found:
                found.append(exe)
        except configparser.Error:
            pass
    found += [r"C:\OSGeo4W\bin\qgis-bin.exe", r"C:\OSGeo4W64\bin\qgis-bin.exe",
              r"C:\OSGeo4W\bin\qgis-ltr-bin.exe"]
    seen: List[str] = []
    for exe in found:
        if exe and exe.lower() not in {e.lower() for e in seen}:
            seen.append(exe)
    return seen


def write_launchers(cfg: Cfg) -> List[Path]:
    """One double-clickable file per platform that opens the project in QGIS."""
    written: List[Path] = []

    # ASCII only, CRLF: this is read by cmd.exe on whatever code page the
    # machine happens to use.
    repo_from_here = os.path.relpath(REPO, cfg.out_dir).replace("/", "\\")
    bat = ["@echo off",
           "REM Opens the Waveshed torture project in QGIS.",
           "REM Generated by tools/make_torture_project.py - edit QGIS= below",
           "REM if your QGIS lives somewhere else.",
           "setlocal",
           'set "PROJECT=%~dp0waveshed_torture.qgs"',
           f'set "REPO=%~dp0{repo_from_here}"',
           "",
           "REM --- 1. refuse to run while QGIS holds the project -------------",
           "REM A running QGIS can write its in-memory copy back over the file",
           "REM we are about to generate, which is how a 'stale' project happens.",
           'tasklist /FI "IMAGENAME eq qgis-bin.exe" 2>NUL | find /I "qgis-bin.exe" >NUL',
           "if not errorlevel 1 (",
           "  echo.",
           "  echo QGIS is already running. Close it completely first - otherwise it",
           "  echo can save its old copy of the project over the fresh one.",
           "  echo.",
           "  pause",
           "  exit /b 1",
           ")",
           "",
           "REM --- 2. regenerate the project so it cannot be out of date ------",
           'set "PYQGIS="']
    for candidate in (r"C:\OSGeo4W\bin\python-qgis.bat",
                      r"C:\OSGeo4W\bin\python-qgis-ltr.bat",
                      r"C:\OSGeo4W64\bin\python-qgis.bat"):
        bat.append(f'if not defined PYQGIS if exist "{candidate}" '
                   f'set "PYQGIS={candidate}"')
    bat += [
        "if defined PYQGIS (",
        "  echo Refreshing the project ...",
        '  call "%PYQGIS%" "%REPO%\\tools\\make_torture_project.py" --stages project',
        "  if errorlevel 1 (",
        "    echo.",
        "    echo Project refresh FAILED - opening whatever is on disk instead.",
        "    echo.",
        "    pause",
        "  )",
        ") else (",
        "  echo [warn] No OSGeo4W python found, so the project was NOT refreshed.",
        "  echo [warn] Check the build stamp in the QGIS window title.",
        ")",
        "",
        "REM --- 3. open it ------------------------------------------------",
        'set "QGIS="',
    ]
    for candidate in _qgis_exe_candidates(cfg):
        bat.append(f'if not defined QGIS if exist "{candidate}" set "QGIS={candidate}"')
    bat += [
        'if not defined QGIS (',
        '  echo Could not find qgis-bin.exe automatically.',
        '  echo Set QGIS= in this file, or just open waveshed_torture.qgs in QGIS.',
        '  pause',
        '  exit /b 1',
        ')',
        'start "" "%QGIS%" --project "%PROJECT%"',
        'endlocal',
    ]
    bat_path = cfg.out_dir / "open_in_qgis.bat"
    bat_path.write_bytes(("\r\n".join(bat) + "\r\n").encode("utf-8"))
    written.append(bat_path)

    sh_path = cfg.out_dir / "open_in_qgis.sh"
    sh_path.write_text(
        "#!/bin/sh\n"
        "# Opens the Waveshed torture project in QGIS, regenerating it first so it\n"
        "# can never be out of date. Override with QGIS=/path/to/qgis\n"
        'HERE=$(cd "$(dirname "$0")" && pwd)\n'
        f'REPO=$(cd "$HERE/{os.path.relpath(REPO, cfg.out_dir)}" && pwd)\n'
        'if pgrep -x qgis >/dev/null 2>&1; then\n'
        '  echo "QGIS is already running - close it first, or it may save its old"\n'
        '  echo "copy of the project over the fresh one."\n'
        '  exit 1\n'
        'fi\n'
        'echo "Refreshing the project ..."\n'
        '"${PYTHON:-python3}" "$REPO/tools/make_torture_project.py" --stages project || \\\n'
        '  echo "[warn] refresh failed - opening what is on disk"\n'
        'exec "${QGIS:-qgis}" --project "$HERE/waveshed_torture.qgs"\n',
        encoding="utf-8")
    try:
        sh_path.chmod(0o755)
    except OSError:
        pass
    written.append(sh_path)
    return written


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------

def _md(text: str) -> str:
    """Escape a value for a Markdown table cell (``|`` is the column break)."""
    return text.replace("|", "\\|")


def _row_sort_key(layer: Layer) -> Tuple[int, int, str]:
    """Sort '2.9' before '2.10' and '3.2a' before '3.3'."""
    head, _, tail = layer.row.partition(".")
    digits = "".join(c for c in tail if c.isdigit())
    return int(head) if head.isdigit() else 99, int(digits or 0), tail


# Resolution -> tile geometry. Fixed by the engine (ABT_EXTENT_DEG); the byte
# sizes are exact, which makes them the cheapest possible check that a run
# produced what it claimed to.
RES_TABLE = (
    (2, "0.1", 5556, "1.99984", "62,582,828"),
    (5, "0.25", 5556, "4.99960", "62,582,828"),
    (10, "0.25", 2780, "9.99200", "15,657,004"),
    (30, "0.5", 1852, "29.99757", "7,111,724"),
    (90, "1.0", 1236, "89.89563", "3,164,204"),
    (250, "2.0", 892, "249.12780", "1,598,508"),
)


def write_checklist(cfg: Cfg, layers: Sequence[Layer], project: Path) -> Path:
    """The run order: what to test, in which order, and what 'pass' means.

    Generated rather than written by hand so the row list can never drift from
    the project: a row skipped for a missing key simply is not in here.
    """
    lines: List[str] = []
    add = lines.append
    aoi = f"{AOI_W} {AOI_S} {AOI_E} {AOI_N}"
    by_group = {g: sorted((l for l in layers if l.group == g), key=_row_sort_key)
                for g in (G_XYZ, G_LOCAL, G_BLD, G_SRV, G_RUN)}

    # The rows that make up the one-hour smoke test: one reference source per
    # acquisition path, plus the traps that fail silently when broken.
    smoke = {"1.1", "1.2", "1.3b", "2.2", "2.8", "2.10", "3.2b", "4.3", "4.6"}

    def rows(group: str, intro: str = "") -> None:
        if intro:
            add(intro)
            add("")
        for lyr in by_group[group]:
            if lyr.check == "none":
                continue
            name = lyr.name[len(lyr.row):].lstrip() if lyr.name.startswith(lyr.row) else lyr.name
            mark = " ⚡" if lyr.row in smoke else ""
            add(f"- [ ]{mark} **{lyr.row}** {name}")
            if lyr.check == "both":
                add("  - [ ] Site Analysis — coverage over the reference AOI, 30 m")
                add("  - [ ] Map Converter — same bbox, 30 m, then diff against the above")
            elif lyr.check == "error":
                add("  - [ ] Site Analysis — the run must FAIL, loudly and by name")
                add("  - [ ] Map Converter — same failure, same message")
                add("  - [ ] nothing was cached: no new pool dir, no half-written tiles")
            elif lyr.check == "reject":
                add("  - [ ] switch it on: it must NOT be offered in the Site Analysis DEM picker")
                add("  - [ ] Map Converter: same refusal (or the imagery warning)")
            elif lyr.check == "buildings":
                add("  - [ ] coverage run with this as the buildings source")
                add("  - [ ] Map Converter run with this layer selected")
            add(f"  - pass: {lyr.expect}")
        add("")

    add("# Waveshed torture set — test checklist")
    add("")
    add(f"Generated {time.strftime('%Y-%m-%d %H:%M')} by `tools/make_torture_project.py` "
        f"from `tests/TEST_DATA_GUIDE.md` + `tests/TERRAIN_PROVIDERS.md`.")
    add("")
    add("Work top to bottom: the phases are ordered so that a failure early on")
    add("explains the failures after it. **Every terrain source goes through both")
    add("paths — Site Analysis *and* the Map Converter. A divergence between them")
    add("is a bug by definition.** Quoted expectations are contract text: match on")
    add("them, and record the exact message when something differs.")
    add("")
    add("Short on time? The items marked ⚡ are the smoke test (~1 h).")
    add("")
    add("**Do not hand-check what a script can check.** Run")
    add("`tools/torture_runner.py` under QGIS's Python first: it verifies every")
    add("layer loads, carries the URI this catalogue wrote, draws over ground where")
    add("its row has data, that every elevation-tile row DECODES to plausible metres")
    add("through the plugin's own encoding verdict, and that the plugin's verdicts")
    add("(dem/imagery, refused-as-terrain, terrarium/mapbox, the z18->z15 clamp)")
    add("match what this document says — around 340 checks in a couple of minutes.")
    add("It exits 0 only when everything this catalogue declares actually ran and")
    add("passed; 1 on a failure, 2 when the run was incomplete. What is left below")
    add("is what genuinely needs a person: dialog wording, picker contents, and")
    add("looking at the terrain.")
    add("")
    add("## Build under test")
    add("")
    add("- [ ] plugin version ............ `______`  (Plugins ▸ Manage ▸ Waveshed)")
    add("- [ ] engine binary build date ... `______`  (Settings ▸ binary dir)")
    add("- [ ] QGIS version .............. `______`")
    add("- [ ] machine / GPU / OS ........ `______`")
    add("- [ ] date, tester .............. `______`")
    add("")
    add("---")
    add("")

    # ---------------- phase 0 ----------------------------------------------
    add("## Phase 0 — Preflight (~10 min, once per build)")
    add("")
    add("- [ ] ⚡ **0.1** All three binaries in ONE directory")
    add("  - `aether_core`, `aether_converter`, `aether_export` — `discover_binary_dir()`")
    add("    takes the first directory holding all three, so a partial build dir is")
    add("    skipped and `PATH` silently wins instead.")
    add("  - pass: the Settings dialog reports all three found, at the path you expect.")
    add("- [ ] **0.2** The engine is new enough to plan")
    add("  ```")
    add(f"  aether_converter plan --south {AOI_S} --north {AOI_N} --west {AOI_W} --east {AOI_E} --resolutions 30")
    add("  ```")
    add("  - pass: prints an `aether-plan/1` JSON document with the tile list. The Map")
    add("    Converter cross-checks this against its own enumeration before every run")
    add("    and aborts on any difference — or on an engine too old to have `plan`.")
    add("- [ ] **0.3** Cache points at a scratch directory you can delete")
    add("  - QgsSettings `waveshed/cache_dir` (default `~/.aether/cache`).")
    add("  - pool key = `md5(\"v4|\" + source_identity(source) [+ buildings])`,")
    add("    view key = `md5(\"v4|\" + RAW uri + tile names + resolution)`.")
    add("- [ ] ⚡ **0.4** Settings ▸ Clear cache — `pool/` and `views/` are empty")
    add("- [ ] ⚡ **0.5** Open the project, click **Enable macros**")
    add("  - **check the build stamp first.** The log's first and last lines carry")
    add("    `build <date> <time> (N layers)`, and so does the QGIS window title. If it")
    add("    is not the build you just generated, you are looking at a project QGIS")
    add("    still had open — close it, reopen, and start again. Nothing else in this")
    add("    document is meaningful until that stamp matches.")
    add("  - pass: the log tab *Waveshed torture* ends with `setup complete` and")
    add(f"    `{len(layers)} layers (0 invalid)`. Any INVALID line names its layer — fix that first.")
    add("- [ ] **0.6** The local tile fixture answers")
    add(f"  - open `{cfg.terrain_rgb_base_url}/12/2131/1440.png` in a browser → a PNG.")
    add("- [ ] ⚡ **0.6b** Run `qgis_console_render_check.py` from the Python console")
    add("  - it renders every layer over ground where that row HAS data and counts the")
    add("    colours that come out. \"0 invalid\" does not mean \"draws something\": a")
    add("    layer with a broken CRS, the wrong renderer for its data type, or a codec")
    add("    QGIS cannot decode is valid and blank.")
    blank = [l for l in layers if not l.expect_renders]
    add("  - pass: only " + (", ".join(f"**{l.row}**" for l in blank) or "no layer")
        + " report EMPTY. Anything else is a real finding — write it down.")
    add("- [ ] ⚡ **0.6c** Step 3/6 of the setup log reads")
    add("  `N CRS checked, all transform correctly here (none pinned)`")
    add("  - the project pins **no** datum transformations. It used to, and that")
    add("    broke every layer in the pinned CRS on machines whose PROJ did not")
    add("    accept the baked-in pipeline (EPSG:3857 took out all of group 1 and the")
    add("    WMTS row; 2229 and 5514 took out 2.19/2.20). QGIS computes a transform")
    add("    that works locally, which is the whole point.")
    add("  - if QGIS asks **\"Select Transformation\"** on this open: pick the TOP")
    add("    entry (highest accuracy; avoid any row that says a grid is missing) and")
    add("    tick the remember/don't-ask box. It asks once per CRS pair, and only")
    add("    rows 2.19 (EPSG:2229) and 2.20 (EPSG:5514) can trigger it — every other")
    add("    CRS here publishes a single operation. Any choice is fine: the engine")
    add("    applies parameter-based (+towgs84) shifts only, so the differences are")
    add("    metre-level and below what this set measures.")
    add("  - the macro also switches the prompt off in your profile, but that only")
    add("    takes effect from the NEXT open — layers load, and the dialog appears,")
    add("    before any project macro runs. Settings -> Options -> Transformations")
    add("    to turn it back on.")
    add("  - if a CRS is reported BROKEN here, layers in it cannot draw and that is")
    add("    a local PROJ problem, not a project one.")
    add("- [ ] **0.7** Free disk noted: `______` GB")
    add("  - a 2 m tile is 62 MB; the atlas cap is ~3.86 GB (~62 tiles) and **nothing")
    add("    pre-flights it**.")
    add("")

    # ---------------- phase 1 ----------------------------------------------
    add("## Phase 1 — XYZ elevation services (guide §1)")
    add("")
    add(f"Reference AOI for every row: `{aoi}` (W S E N), 30 m, LOS.")
    add("Row 1.1 first — it is the reference every later row is compared against.")
    add("")
    rows(G_XYZ)
    add("Then, still on XYZ:")
    add("")
    add("- [ ] ⚡ **1.a** After the 1.1 run, a pool directory exists under `cache/pool/<md5>/`")
    add("  with `tile_N46.50E7.00_30m.abt`-style names, and the log carries a")
    add("  `[Stats]` line.")
    add("- [ ] ⚡ **1.b** Re-run the identical AOI → pool hit, no re-download, visibly faster.")
    add("- [ ] **1.c** Re-spell the URI (reorder the params on the layer, or re-add it")
    add("  from the Browser connection) → the *view* rebuilds but the *pool* is reused:")
    add("  no second download of byte-identical tiles.")
    add("- [ ] **1.d** Row 1.2 logged the clamp warning naming z15 and the flat-sea risk.")
    add("- [ ] **1.e** Add `interpretation=terrarium` to the 1.2 layer → the pool must NOT")
    add("  move (an undecided URI already keys as terrarium).")
    add("- [ ] **1.f** Cross-provider: 1.1 vs 1.3b over the same AOI agree within the DEMs'")
    add("  own accuracy. In Europe a 45–55 m offset is the vertical-datum gap (F1), not a bug.")
    add("- [ ] **1.h** Interpretation tokens. Measured on QGIS 3.44.7: only")
    add("  `terrariumterrain` and `maptilerterrain` are honoured; `terrarium`,")
    add("  `mapboxterrain`, `terrainrgb` are silently ignored and the layer stays an")
    add("  ARGB32 picture. `TEST_DATA_GUIDE.md` §1 tells the reader to add")
    add("  `interpretation=terrarium` / `interpretation=mapboxterrain` by hand — both")
    add("  are inert. The guide needs correcting, and `xyz_encoding()` needs to learn")
    add("  `maptilerterrain` (row 1.10 reproduces the consequence).")
    add("- [ ] **1.g** Confirm which is current: `TEST_DATA_GUIDE.md` still carries a")
    add("  2026-08-12 note that the Map Converter's XYZ path bypasses the toolkit")
    add("  resampler, while `TERRAIN_PROVIDERS.md` §7 marks that killed the same day.")
    add("  Whichever is true, one of the two documents needs correcting.")
    add("")

    # ---------------- phase 2 ----------------------------------------------
    add("## Phase 2 — Local rasters (guide §2)")
    add("")
    rows(G_LOCAL)

    # ---------------- phase 3 ----------------------------------------------
    add("## Phase 3 — Folders and paths (no layer — folder input or CLI)")
    add("")
    add("These are the cases a QGIS *layer* cannot express. Use the Map Converter's")
    add("folder input, or drive `aether_converter ingest` directly.")
    add("")
    add("- [ ] ⚡ **3.a** `dem/swiss/` as a terrain folder")
    add("  - pass: all 4 LV95 tiles picked up, terrain continuous across tile seams.")
    add("- [ ] **3.b** `dem/mixed_crs/` — one LV95 tile + one WGS84 tile in ONE folder")
    add("  - pass: each file read in its own CRS (per-file GeoKeys win over the")
    add("    folder-level guess), terrain continuous, nothing offset by kilometres.")
    add("- [ ] **3.c** `dem/overlap/` — `a_base.tif` vs `b_base_plus50.tif`, same ground")
    add("  - pass: alphabetically-first wins, so the output equals `a_base`. Identical")
    add("    on every run and every machine.")
    add("- [ ] **3.d** `dem/nested/` — west half at top level, east half in `sub/`")
    add("  - pass: BOTH paths find both files (recursive, sorted). The Map Converter")
    add("    used to scan flat.")
    add("- [ ] **3.e** `.vrt` is NOT scanned in a folder — drop `mosaic.vrt` into a")
    add("  terrain folder and confirm it is ignored, while the same file added as a")
    add("  *layer* works (row 2.9).")
    add("- [ ] **3.f** `dem/broken/broken_nocrs.tif` as a folder input")
    add("  - pass: hard error naming the file and telling you to add `\"crs\"`. (As a")
    add("    QGIS layer this never fires — QGIS hands a CRS-less raster the project CRS.)")
    add("- [ ] **3.g** `dem/broken/broken_nogt.tif` as a folder input")
    add("  - pass: hard error. Filename-based georeferencing was deliberately removed,")
    add("    so there is nothing to fall back on.")
    add("- [ ] **3.h** A pre-built `.abt` pool directory as the terrain source")
    add("  - pass: flat listing only, by design — sub-folders are not searched.")
    add("- [ ] **3.i** Empty folder, and a folder holding only unsupported extensions")
    add("- [ ] **3.l** `dem/foreign_crs/feet_2229_losangeles.tif` (EPSG:2229, US survey feet)")
    add("  as a Map Converter folder input")
    add("  - pass: +units honored — the geotransform of a feet CRS is in feet too, so a")
    add("    unit slip shows up as a factor of 3.28. Real Los Angeles terrain, so a")
    add("    resampling error cannot hide in a flat ramp.")
    add("  - not a project layer on purpose: EPSG:2229 publishes four datum")
    add("    transformations, three of them grid-based, and QGIS's dialog offers the")
    add("    grid-based 2 m one first. Picking it without us_noaa_cshpgn.tif installed")
    add("    breaks the transform and the layer draws nothing.")
    add("- [ ] **3.m** `dem/foreign_crs/krovak5514_prague.tif` (EPSG:5514) as a folder input")
    add("  - pass: ingest hard-errors naming the CRS (\"Projection not found\") before")
    add("    any tile is written. Real Prague terrain, so the failure is about the CRS.")
    add("- [ ] **3.j** `dem/broken/truncated.tif` (40 % of a GeoTIFF) as a folder input")
    add("  - pass: fails loudly naming the file. Never skipped, never counted as covered.")
    add("- [ ] **3.k** `dem/variants/ignored_formats/` as a terrain folder — holds only")
    add("  `base.asc`, which the scanner does not accept")
    add("  - pass: the error lists the accepted extensions instead of reporting an empty")
    add("    result, and row 2.26 proves the same file loads fine as a layer.")
    add("  - pass: the error names the four accepted ones (`.tif .tiff .dem .hgt`) and")
    add("    mentions `.abt` tiles directly in the folder.")
    add("")

    # ---------------- phase 4 ----------------------------------------------
    add("## Phase 4 — Buildings (guide §3)")
    add("")
    rows(G_BLD)
    add("- [ ] ⚡ **4.a** Row 3.3 proper: the plugin's own **Download OSM buildings**")
    add("  button over the reference AOI — the built-in path, including height tags.")
    add("- [ ] **4.b** Row 3.4: tick **OSM buildings** on a coverage run (OpenFreeMap")
    add("  PBF, z14 MVT fetch).")
    add("- [ ] **4.c** Repeat 4.b with the network cut")
    add("  - pass: the run FAILS. It must never cache building-free terrain under a")
    add("    buildings cache key — that poisons every later run silently.")
    add("- [ ] **4.d** Same AOI, once with buildings and once without")
    add("  - pass: two different pool keys; the buildings fingerprint is part of the key.")
    add("")

    # ---------------- phase 5 ----------------------------------------------
    add("## Phase 5 — Servers: WMS / WMTS / WCS / ArcGIS (guide §4)")
    add("")
    rows(G_SRV)
    add("- [ ] **5.0a** Row **4.4a** (3DEP WCS, real elevation values) is a Browser")
    add("  connection, not a layer: measured 20-96 s per load, and intermittent")
    add("  \"Cannot describe coverage\". Add it from the Browser when testing the")
    add("  coverage-values path, and expect it to be slow.")
    add("- [ ] **5.a** ArcGIS ImageServer (3DEP) added from the Browser connection the")
    add("  macro registered — the QGIS URI for an ImageServer is version-dependent, so")
    add("  it ships as a connection rather than a layer.")
    add("  - pass: it classifies as a rendered provider; over the USA it returns data.")
    add("- [ ] **5.b** Rendered providers are exported through QGIS per tile — confirm")
    add("  the temp GeoTIFFs are WGS84 and are cleaned up afterwards.")
    add("- [ ] **5.c** A rendered-server run at 2 m vs 30 m: the export happens at the")
    add("  finer of native/finest-output resolution, not at the canvas resolution.")
    add("")

    # ---------------- phase 6 ----------------------------------------------
    add("## Phase 6 — Geometry run matrix (guide §5)")
    add("")
    add("Every case as a Map Converter conversion, and where sensible a coverage run.")
    add("The boxes are the **5.1 Run matrix** layer — read the bbox off its attribute")
    add("table.")
    add("")
    for case in RUN_MATRIX:
        add(f"- [ ] **6.{RUN_MATRIX.index(case) + 1}** {case['name']} — "
            f"`{case['west']} {case['south']} {case['east']} {case['north']}` @ {case['res_m']} m")
        add(f"  - pass: {case['stresses']}")
    add("")

    # ---------------- phase 7 ----------------------------------------------
    add("## Phase 7 — Resolution sweep")
    add("")
    add("Row 1.1 (or 2.2) over the reference AOI at each resolution. Tile extent and")
    add("size are fixed per resolution, and the byte sizes are exact — the cheapest")
    add("check that a run produced what it says it did.")
    add("")
    add("| res m | extent° | size px | exact res m | bytes/tile |")
    add("|---|---|---|---|---|")
    for res, extent, size, exact, size_b in RES_TABLE:
        add(f"| {res} | {extent} | {size} | {exact} | {size_b} |")
    add("")
    for res, extent, size, exact, size_b in RES_TABLE:
        mark = " ⚡" if res == 30 else ""
        add(f"- [ ] **7.{res}m**{mark} run at {res} m → tiles are exactly {size_b} bytes, "
            f"{size}×{size} px, {extent}° extent")
    add("- [ ] **7.a** 2 m only for cap testing: ~62 tiles is the practical ceiling")
    add("  (62 MB each against a ~3.86 GB terrain atlas).")
    add("- [ ] **7.b** Tile count matches the AOI in both tabs, and the Map Converter's")
    add("  `plan` cross-check passes silently.")
    add("")

    # ---------------- phase 8 ----------------------------------------------
    add("## Phase 8 — Heights, batch CSVs, Processing (guide §6)")
    add("")
    add("The height floor is 1.0 m AGL; AMSL is an absolute elevation and may be")
    add("negative (down to -500 m).")
    add("")
    add("- [ ] ⚡ **8.1** `batch/batch_ok.csv` — 3 rows, accepted, runs.")
    add("- [ ] ⚡ **8.2** `batch/batch_amsl_negative_ok.csv` — -430 m AMSL accepted.")
    add("- [ ] **8.3** `batch/batch_reject_agl_half_metre.csv`")
    add("  - pass: rejected at **line 3**, message names the 1.0 m minimum and offers")
    add("    AMSL as the alternative.")
    add("- [ ] **8.4** `batch/batch_reject_non_numeric.csv`")
    add("  - pass: `Line 3: altitude is not a number, got 'thirty'`")
    add("- [ ] **8.5** `batch/batch_reject_bad_mode.csv`")
    add("  - pass: `Line 3: mode must be AGL or AMSL, got 'ASL'`")
    add("- [ ] **8.6** 0.5 m typed into EVERY height input — site table, P2P A and B,")
    add("  both Processing algorithms, the asset default")
    add("  - pass: all reject; switching that input to AMSL opens the minimum to -500 m.")
    add("- [ ] **8.7** The Dead Sea site (-430 m AMSL, layer 5.2) runs end to end over")
    add("  row 2.4 terrain — negative elevations all the way through, AGL floor untouched.")
    add("- [ ] **8.8** P2P from the **5.3 P2P links** layer: obstructed link vs clear link")
    add("  - pass: the obstructed one reports an obstruction; the profile plot matches.")
    add("- [ ] **8.9** Both Processing algorithms (coverage + P2P) run headless with the")
    add("  same parameters and produce the same numbers as the dialogs.")
    add("")

    # ---------------- phase 9 ----------------------------------------------
    add("## Phase 9 — Cache and operational drills (guide §6)")
    add("")
    add("- [ ] ⚡ **9.1** Same area twice → pool hit (fast, no download).")
    add("- [ ] **9.2** `touch <pool>/<tile>.abt.rebuild` → that tile alone is refetched.")
    add("- [ ] **9.3** Kill the network mid-download (Site Analysis)")
    add("  - pass: the run fails; the SECOND run heals via `.rebuild` flags. No")
    add("    permanently-poisoned pool.")
    add("  - pass: 0 tiles fetched logs `ERROR: Rust download z={zoom}: 0 of {total}")
    add("    source tile(s) fetched — nothing was downloaded, so these tiles would be")
    add("    flat 0 m terrain.` and falls back to the slow path.")
    add("  - pass: a strict majority failed → `Tile download failed: {failed} of")
    add("    {attempted} terrain tiles ({pct}%) could not be fetched; …`")
    add("  - pass: below that threshold the run SUCCEEDS by design, logging only")
    add("    `[Stats] ERRORS (n): timeout=…, HTTP_4xx=…`")
    add("- [ ] **9.4** `waveshed/download_max_passes` = 1 → retries disabled, failures surface.")
    add("- [ ] **9.5** `waveshed/download_connections` (default 256, floor 32) → throttling")
    add("  changes throughput, not results.")
    add("- [ ] **9.6** Fill the target volume → `Insufficient disk space: need ~{} MB for")
    add("  {} .abt output files, …` raised BEFORE any fetch.")
    add("- [ ] **9.7** Swap an **old** `aether_converter` into the binary dir")
    add("  - pass: the Map Converter aborts with \"engine binaries predate this plugin version\".")
    add("- [ ] **9.8** Map Converter **Rebuild existing tiles** rebuilds; Site Analysis has")
    add("  no equivalent, so Clear cache is the only lever there.")
    add("- [ ] **9.9** Cancel a running job")
    add("  - known: Cancel does NOT stop the terrain phase (F6). Confirm it still leaves")
    add("    no corrupt tile behind.")
    add("- [ ] **9.10** Two runs over the same AOI at once")
    add("  - known: the pool has no lock and no atomic write (F6) — corrupt tiles then")
    add("    cache forever. Confirm whether this still reproduces.")
    add("- [ ] **9.11** Settings ▸ Clear cache while a job runs → no crash.")
    add("")

    # ---------------- phase 10 ---------------------------------------------
    add("## Phase 10 — Verify the output, not the absence of errors")
    add("")
    add("```")
    add("python3 tools/abt_diag.py --cache <pool-dir> --out report.json")
    add("python3 tools/abt_diag.py --compare <dirA> <dirB>")
    add("python3 tools/abt_view.py --cache <pool-dir> --render <png-dir>")
    add("python3 tools/scan_abt.py --cache <pool-dir> --zero-frac 0.01")
    add("```")
    add("")
    add("- [ ] ⚡ **10.1** `abt_diag`: no **zero** blocks over land. A zero block means the")
    add("  provider failed and the pipeline did not notice — the single most important")
    add("  check in this document.")
    add("- [ ] ⚡ **10.2** Voids read as `-9999` (`nodata` blocks), never 0 m. Rows 2.10–2.12")
    add("  and the UTM corners of 2.5 are the fixtures that carry real voids.")
    add("- [ ] **10.3** Tile COUNT matches the AOI and file sizes match Phase 7 exactly.")
    add("- [ ] **10.4** Re-run the same job → `--compare` reports byte-identical tiles.")
    add("- [ ] **10.5** `scan_abt --zero-frac 0.01` finds no full zero rows/cols and no")
    add("  stride overflow.")
    add("- [ ] **10.6** `abt_view` renders look like terrain, not like noise or a shifted copy.")
    add("- [ ] **10.7** Same AOI through `download` vs `ingest`, diffed")
    add("  - known: they disagree by half a cell (F2) — ingest samples NW corners,")
    add("    download samples cell centres. 5 m at 10 m resolution, 125 m at 250 m.")
    add("- [ ] **10.8** The exported GeoTIFF (`aether_export`) opens in QGIS, is a COG,")
    add("  and lands on the same ground as its terrain.")
    add("")

    # ---------------- phase 11 ---------------------------------------------
    add("## Phase 11 — Known-broken: confirm, do not file")
    add("")
    add("From `TERRAIN_PROVIDERS.md` §7. Each is expected to fail; the check is that it")
    add("fails the *documented* way and nothing worse.")
    add("")
    add("- [ ] **11.1** A10 — `/vsicurl/` paths mangled by `abspath()` (`https://` → `https:/`).")
    add("- [ ] **11.2** F2 — `download` and `ingest` half a pixel apart.")
    add("- [ ] **11.3** F3 — bilinear downsampling deletes ridge crests → optimistic LOS.")
    add("- [ ] **11.4** F1 — no vertical-datum handling: 45–55 m cross-source offsets in Europe.")
    add("- [ ] **11.5** F5 — no pre-flight against the ~3.86 GB atlas cap (warning sits at 50 GB).")
    add("- [ ] **11.6** F6 — pool has no lock, no atomic write; Cancel does not stop terrain.")
    add("- [ ] **11.7** F7 — antimeridian and >85° unhandled (run-matrix case 'Fiji').")
    add("- [ ] **11.8** F8 — grid square in degrees → E–W oversampled by 1/cos(lat).")
    add("- [ ] **11.9** Checkerboard energy on real alpine terrain sits near **8.7 %** even")
    add("  in an ideal pipeline — intrinsic to a running-max horizon test. Do not chase it to zero.")
    add("")

    # ---------------- sign-off ---------------------------------------------
    add("## Sign-off")
    add("")
    add("- [ ] Every phase above either ticked or explicitly waived (note why).")
    add("- [ ] Failures written up with the exact message and the row number.")
    add("- [ ] Anything newly broken that is not in Phase 11 → new finding.")
    add("- [ ] `TODO.md` updated; stale notes in the two test docs corrected (see 1.g).")
    add("")
    add("Result: `______`  ·  tester: `______`  ·  date: `______`")
    add("")

    boxes = sum(1 for line in lines if line.lstrip().startswith("- [ ]"))
    lines.insert(3, f"**{boxes} checks** across {len(layers)} layers. "
                    f"Reference AOI `{aoi}` (W S E N), Bern.")

    path = cfg.out_dir / "CHECKLIST.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_readme(cfg: Cfg, layers: Sequence[Layer], project: Path) -> Path:
    lines: List[str] = []
    add = lines.append
    add("# Waveshed torture set — generated")
    add("")
    add(f"Built by `tools/make_torture_project.py` on "
        f"{time.strftime('%Y-%m-%d %H:%M')} from `tests/TEST_DATA_GUIDE.md`.")
    add("Everything in this folder is gitignored and can be deleted and rebuilt.")
    add("")
    add("## Start here")
    add("")
    add(f"This build: **{BUILD_STAMP[0]}**. The same stamp is in the QGIS window title")
    add("and in the macro's log line — if they disagree, QGIS is showing you an older")
    add("project it still had open.")
    add("")
    add("1. Double-click **`open_in_qgis.bat`** (Windows) or run `./open_in_qgis.sh`.")
    add(f"   Opening `{project.name}` in QGIS by hand does the same thing.")
    add("2. Click **Enable macros** in the bar QGIS shows at the top.")
    add("3. Work through **`CHECKLIST.md`** — the run order, with what \"pass\" means")
    add("   for every row, and the contract text to match error messages against.")
    add("")
    add("That is the whole setup. The project sets itself up on open, logging each")
    add("step and its duration to *Log Messages -> Waveshed torture*:")
    add("")
    add("* registers every Browser connection of guide §1 and §4, XYZ")
    add("  *interpretation* included (nothing but the connection dialog can set that);")
    add(f"* serves the local Terrain-RGB fixture on `http://127.0.0.1:{_tile_server_target(cfg)[1]}/`")
    add("  so row 1.3b works with no second terminal;")
    add("* probes every remote host through QGIS's own network stack, so a proxy or")
    add("  firewall shows up as `UNREACHABLE` with the reason instead of as a blank")
    add("  layer you have to guess about;")
    add("* checks that every layer resolved and names any that did not;")
    add("* matches each raster's renderer to what its provider actually returns, so")
    add("  the same project draws correctly on QGIS builds either side of 3.28 (where")
    add("  `interpretation=` turns an elevation XYZ layer from a picture into values).")
    add("")
    add("No datum transformations are pinned into the project — baking a PROJ")
    add("pipeline in breaks every layer in that CRS on a machine whose PROJ does not")
    add("accept it. The \"Multiple operations are possible …\" prompt is switched off")
    add("by the macro instead, which changes a preference and not the maths.")
    add("")
    add("**Most of the checklist is automated.** Run it under QGIS's Python:")
    add("")
    add("```")
    add('"C:\\OSGeo4W\\bin\\python-qgis.bat" tools/torture_runner.py')
    add("```")
    add("")
    add("~340 checks in a couple of minutes: every layer loads, carries the URI")
    add("this build wrote, draws where its row has data, and every elevation-tile")
    add("row decodes to plausible metres. Results land in `results.md` next to the")
    add("project — it lists failures, unexpected passes, checks that never ran and")
    add("rows this build could not produce, so a green summary means the whole")
    add("catalogue ran. Exit 0 = complete and clean, 1 = failure, 2 = incomplete.")
    add("Add the Aether binaries (`AETHER_BIN_DIR`) and it also cross-checks")
    add("`aether_converter plan` against the plugin's own tile enumeration for every")
    add("run-matrix case, and ingests each local raster and reads the .abt back.")
    add("")
    add("Then run **`qgis_console_render_check.py`** in the Python console once: it")
    add("renders every layer over ground where that row has data and names the ones")
    add("that draw nothing. Only the rows the catalogue marks as blank should —")
    add("the script prints that list itself.")
    add("")
    add("It reports what it did in the message bar and under *View -> Panels ->")
    add("Log Messages -> Waveshed torture*. To skip the macro prompt for good:")
    add("*Settings -> Options -> General -> Enable macros = Always*. If macros are")
    add("switched off in your profile, run `qgis_console_bootstrap.py` from the")
    add("Python console instead — same code, same result.")
    add("")
    add("Most layers are off by default — switch on the one you are testing.")
    add("Each layer's expectation is in its abstract (Layer Properties ->")
    add("Information) and repeated below.")
    add("")
    add(f"**Reference AOI**: `{AOI_W} {AOI_S} {AOI_E} {AOI_N}` (W S E N, Bern). Every")
    add("local fixture, both Copernicus and SRTM tiles, the swissALTIRegio tiles,")
    add("the OSM buildings and all three swisstopo services cover it — so the same")
    add("run through two providers is a real comparison. It is the first polygon")
    add("in the run-matrix layer.")
    add("")
    add("## First test run")
    add("")
    add("1. Switch on **1.1 AWS Terrarium** and run Site Analysis over the")
    add("   reference AOI at 30 m. That is the reference result.")
    add("2. Settings -> Clear cache (a second run with a changed source will")
    add("   otherwise serve the first run's tiles).")
    add("3. Switch 1.1 off, switch on **2.2 base_wgs84.tif**, run the same AOI at")
    add("   the same resolution. The two must agree within the DEMs' own accuracy —")
    add("   a 45-55 m offset in Europe is the vertical-datum gap (F1), not your test.")
    add("4. Repeat every row through **both** paths: Site Analysis *and* the Map")
    add("   Converter. Divergence between them is a bug by definition.")
    add("")
    add("## Layers")
    add("")
    for group in (G_XYZ, G_LOCAL, G_BLD, G_SRV, G_RUN):
        members = sorted((lyr for lyr in layers if lyr.group == group), key=_row_sort_key)
        if not members:
            continue
        add(f"### {group}")
        add("")
        add("| Row | Layer | Expected |")
        add("|---|---|---|")
        for lyr in members:
            name = lyr.name[len(lyr.row):].lstrip() if lyr.name.startswith(lyr.row) else lyr.name
            add(f"| {lyr.row} | {_md(name)} | {_md(lyr.expect)} |")
        add("")

    add("## Fixtures with no layer (folder / CLI inputs)")
    add("")
    add("These are deliberately *not* project layers — they are exercised through")
    add("the Map Converter's folder input or the converter CLI, where the file")
    add("path is passed through untouched.")
    add("")
    add("| Path | What it tests |")
    add("|---|---|")
    add("| `dem/broken/broken_nocrs.tif` | Converter must error naming the file and say `add \"crs\"`. As a QGIS *layer* this never fires: QGIS hands a CRS-less raster the project CRS. |")
    add("| `dem/broken/broken_nogt.tif` | No geotransform -> hard error (filename georeferencing was deliberately removed). |")
    add("| `dem/mixed_crs/` | LV95 tile + WGS84 tile in ONE folder. Each file read in its own CRS; terrain continuous, nothing offset by km. |")
    add("| `dem/overlap/` | `a_base.tif` and `b_base_plus50.tif` cover the same ground, 50 m apart in value. Alphabetically-first must win, identically on every machine. |")
    add("| `dem/nested/` | West half at the top level, east half in `sub/`. Both paths must find both (recursive, sorted). |")
    add("| `dem/swiss/` | Folder of real LV95 tiles. |")
    add("| `batch/*.csv` | BATCH_P2P drills — 2 accepted (incl. -430 m AMSL), 3 rejected by line number (0.5 m AGL, non-numeric altitude, bad mode). |")
    add("")
    add("## Drills with no data at all (guide §6)")
    add("")
    add("* Swap an **old** `aether_converter` into the binary dir -> Map Converter must abort: \"engine binaries predate this plugin version\".")
    add("* Kill the network mid-download (Site Analysis) -> second run heals via `.rebuild` flags; no permanently-poisoned pool.")
    add("* Same area twice -> pool hit; re-spelled XYZ URI -> view rebuilds, pool reused; Settings -> Clear cache works.")
    add("* 0.5 m in every height input (site table, P2P A/B, both Processing algorithms, asset default) must be rejected; switching to AMSL opens the minimum to -500 m.")
    add("* Row 3.4: tick \"OSM buildings\" on a coverage run (OpenFreeMap PBF, z14 MVT), then repeat with the network cut — the run must FAIL, not cache building-free terrain under a buildings key.")
    add("")

    if REPORT.skipped:
        add("## Skipped in this build")
        add("")
        add("| What | Why |")
        add("|---|---|")
        for what, why, by_design in REPORT.skipped:
            add(f"| {what} | {why}{' *(by design)*' if by_design else ''} |")
        add("")
        add("Fill in the missing key or path in `torture.local.ini` and re-run")
        add("`python3 tools/make_torture_project.py` — nothing already built is re-downloaded.")
        add("")

    add("## Keys")
    add("")
    add("One key for the whole set, free tier, no payment details:")
    add("")
    add("| Key | Needed for | Get it |")
    add("|---|---|---|")
    add("| `maptiler_key` | rows 1.3a + 1.4 (Terrain-RGB v1 PNG / v2 WebP) | https://cloud.maptiler.com/account/keys/ |")
    add("")
    add("Everything else — AWS Terrain Tiles, swisstopo, terrestris, NRW WCS, USGS 3DEP,")
    add("Copernicus GLO-30, SRTM, Overpass, OpenFreeMap — needs no account at all.")
    add("")
    add("Two providers were considered and rejected:")
    add("")
    add("* **Mapbox** — a token needs payment details on file, and the live endpoint")
    add("  adds nothing over row 1.3a: same published formula, and the URL rules that")
    add("  pin `api.mapbox.com` to `encoding=mapbox`/`max_zoom=15` are already covered")
    add("  by unit tests in `tests/test_terrain_adapter.py`.")
    add("* **Nextzen** — no longer accepting signups and winding down. It served the")
    add("  same Tilezen dataset as row 1.1 (AWS Terrain Tiles), so comparing the two")
    add("  proved nothing. `known_services.json` still matches `nextzen` URLs, which")
    add("  stays useful for a self-hosted Tilezen mirror.")
    add("")
    add("The meaningful cross-provider comparisons are all key-free and already here:")
    add("terrarium (1.1) vs Copernicus GLO-30 (2.1) vs SRTM (2.17) vs swissALTIRegio (2.8).")
    add("")

    path = cfg.out_dir / "README.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_footprints(cfg: Cfg, layers: Sequence[Layer]) -> Optional[Path]:
    """One polygon per local raster, so "where is row 2.19?" is a click.

    The fixtures are deliberately scattered — Bern, the Dead Sea, Los Angeles,
    Prague — and at continental zoom a 12 km clip is a speck. Without this the
    project looks like random specks on an empty world.
    """
    try:
        from osgeo import gdal, osr
        gdal.UseExceptions()
    except ImportError:
        REPORT.skip("fixture footprints layer", "needs GDAL — re-run --stages project "
                                                "with QGIS's Python")
        return None

    wgs84 = osr.SpatialReference()
    wgs84.ImportFromEPSG(4326)
    wgs84.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)

    feats = []
    for lyr in layers:
        if lyr.kind != "raster" or lyr.provider != "gdal":
            continue
        path = lyr.source
        prefix = ""
        if path.startswith("/vsi"):
            head, _, tail = path[1:].partition("/")
            prefix, path = f"/{head}/", tail
        if path.startswith("./"):
            path = str(cfg.out_dir / path[2:])
        try:
            ds = gdal.Open(prefix + path)
            gt = ds.GetGeoTransform()
            w, h = ds.RasterXSize, ds.RasterYSize
            srs = ds.GetSpatialRef()
            ds = None
        except Exception:
            continue
        # Walk the outline instead of taking four corners: a projected CRS bends
        # its edges, and a rotated Krovak box drawn as a quad would lie.
        steps = 12
        edge = ([(i / steps, 0.0) for i in range(steps)]
                + [(1.0, i / steps) for i in range(steps)]
                + [(1.0 - i / steps, 1.0) for i in range(steps)]
                + [(0.0, 1.0 - i / steps) for i in range(steps)])
        pts = [(gt[0] + gt[1] * u * w + gt[2] * v * h,
                gt[3] + gt[4] * u * w + gt[5] * v * h) for u, v in edge]
        if srs is not None and srs.IsProjected():
            try:
                srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
                tr = osr.CoordinateTransformation(srs, wgs84)
                pts = [tr.TransformPoint(x, y)[:2] for x, y in pts]
            except Exception:
                continue
        ring = [[round(x, 6), round(y, 6)] for x, y in pts]
        ring.append(ring[0])
        xs = [x for x, _y in ring]
        ys = [y for _x, y in ring]
        lyr.footprint = (round(min(xs), 6), round(min(ys), 6),
                         round(max(xs), 6), round(max(ys), 6))
        feats.append({
            "type": "Feature",
            "properties": {
                "row": lyr.row,
                "layer": lyr.name,
                "crs": lyr.crs,
                "size_px": f"{w}x{h}",
                "path": lyr.source,
            },
            "geometry": {"type": "Polygon", "coordinates": [ring]},
        })

    if not feats:
        REPORT.skip("row 5.0 (fixture footprints)",
                    "no local raster could be opened, so there is nothing to outline")
        return None
    path = cfg.out_dir / "vectors" / "fixture_footprints.geojson"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, indent=1),
                    encoding="utf-8")
    REPORT.ok(f"vectors/fixture_footprints.geojson ({len(feats)} local rasters located)")
    return path


def write_manifest(cfg: Cfg, layers: Sequence[Layer], project: Path) -> Path:
    """Machine-readable form of the catalogue, for tools/torture_runner.py.

    The expectations live with the rows that define them, so an automated run
    and the human checklist can never disagree about what a row should do.
    """
    rows = []
    for lyr in layers:
        rows.append({
            "row": lyr.row,
            "name": lyr.name,
            "group": lyr.group,
            "provider": lyr.provider,
            "source": lyr.source,
            "crs": lyr.crs,
            "kind": lyr.kind,
            "geometry": lyr.geometry,
            "render": lyr.render,
            "check": lyr.check,
            "expect": lyr.expect,
            "expect_class": lyr.expect_class,
            "expect_encoding": lyr.expect_encoding,
            "expect_zmax": lyr.expect_zmax,
            "expect_renders": lyr.expect_renders,
            "expect_format": lyr.expect_format,
            "expect_decode": lyr.expect_decode,
            "expect_elev_m": list(lyr.expect_elev_m),
            "expect_failure": lyr.expect_failure,
            "expect_features": lyr.expect_features,
            "expect_service_zmax": lyr.expect_service_zmax,
            "expect_tile_px": lyr.expect_tile_px,
            "pipeline_res_m": lyr.pipeline_res_m,
            "pipeline_range_km": lyr.pipeline_range_km,
            "expect_agrees_m": lyr.expect_agrees_m,
            "footprint": list(lyr.footprint),
            "known_fail": lyr.known_fail,
            "known_fail_checks": list(lyr.known_fail_checks),
            "min_scale": lyr.min_scale,
        })
    # The run matrix carries its own resolutions and its own direction. Emitting
    # only the bbox is what let the runner plan "Huge (must be refused)" at 30 m
    # and then report the engine's correct refusal as a failure.
    probes = {}
    for case in RUN_MATRIX:
        probes[case["name"]] = {
            "bbox": [case["west"], case["south"], case["east"], case["north"]],
            "res_m": case["res_m"],
            "must_fail": bool(case.get("must_fail")),
            "refused_by": case.get("refused_by", "engine" if case.get("must_fail") else ""),
            "stresses": case["stresses"],
        }
    payload = {
        "build": BUILD_STAMP[0],
        "project": project.name,
        "reference_aoi": [AOI_W, AOI_S, AOI_E, AOI_N],
        # One point inside the reference AOI, with the ground that is really
        # there. Every elevation-tile row is decoded at this point and checked
        # against its own plausible band.
        "elevation_probe": {"lat": PROBE_LAT, "lon": PROBE_LON,
                            "note": "Bern, reference AOI — real ground is 500-700 m"},
        # The row every other Bern source is measured against end to end. 1.1 is
        # the guide's own "reference result for every other provider".
        "reference_row": "1.1",
        "run_matrix": probes,
        "resolutions": [r[0] for r in RES_TABLE],
        "tile_bytes": {str(r[0]): int(r[4].replace(",", "")) for r in RES_TABLE},
        # Rows the catalogue defines and this build could NOT produce. Without
        # them the runner's denominator is "the rows that happened to exist",
        # and a row that vanished because its fixture failed to download is
        # indistinguishable from a row that passed.
        "skipped_rows": [{"what": what, "why": why, "by_design": by_design}
                         for what, why, by_design in REPORT.skipped],
        "rows": rows,
    }
    path = cfg.out_dir / "manifest.json"
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    return path


def stage_project(cfg: Cfg) -> None:
    print("\n== project ==")
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    layers = build_catalogue(cfg)
    if _write_footprints(cfg, layers) is not None:
        layers.append(Layer(
            "5.0", G_RUN, "5.0 Fixture footprints — where every local raster sits", "ogr",
            "./vectors/fixture_footprints.geojson", "EPSG:4326", kind="vector",
            geometry="Polygon", style="fill", color="152,78,163,255", checked=True,
            check="none",
            expect="One outline per local raster, labelled by row in the attribute table. "
                   "Identify a box to see which row it is, or filter to a row and use "
                   "Zoom to Layer. Edges are walked, not cornered, so a rotated "
                   "projected fixture (2.20 Krovak) is drawn honestly.",
            expect_features=sum(1 for l in layers
                                if l.kind == "raster" and l.provider == "gdal"
                                and l.footprint)))
    # Seconds, not minutes: two refreshes in the same minute must still be
    # distinguishable, or "is this stale?" is unanswerable again.
    BUILD_STAMP[0] = f"{time.strftime('%Y-%m-%d %H:%M:%S')} ({len(layers)} layers)"
    project = write_project(cfg, layers)
    bootstrap = write_bootstrap(cfg, layers)
    launchers = write_launchers(cfg)
    render_check = write_render_check(cfg, layers)
    manifest = write_manifest(cfg, layers, project)
    checklist = write_checklist(cfg, layers, project)
    readme = write_readme(cfg, layers, project)
    print(f"  [ok]   {len(layers)} layers -> {project}")
    boxes = checklist.read_text(encoding="utf-8").count("- [ ]")
    print(f"  [ok]   {checklist.name} ({boxes} checks)")
    print("  [ok]   self-setup macro embedded (connections + tile server + layer check)")
    for path in launchers:
        print(f"  [ok]   {path.name}")
    print(f"  [ok]   {bootstrap.name} (fallback when macros are off)")
    print(f"  [ok]   {render_check.name} (does every layer actually DRAW?)")
    print(f"  [ok]   {manifest.name} ({len(layers)} rows, for tools/torture_runner.py)")
    print(f"  [ok]   {readme}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=str(REPO / "torture.local.ini"),
                    help="INI config (default: torture.local.ini at the repo root)")
    ap.add_argument("--stages", default="fetch,fabricate,vectors,project",
                    help="comma-separated: fetch, fabricate, vectors, project")
    ap.add_argument("--force", action="store_true",
                    help="re-download and re-fabricate everything")
    ap.add_argument("--list", action="store_true",
                    help="print the layer catalogue and exit (builds nothing)")
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config))
    print(f"repo:    {REPO}")
    print(f"out_dir: {cfg.out_dir}")

    if args.list:
        for lyr in build_catalogue(cfg):
            print(f"  {lyr.row:<6} {lyr.group[:1]}  {lyr.name}")
        return 0

    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    for stage in stages:
        t0 = time.perf_counter()
        if stage == "fetch":
            stage_fetch(cfg, force=args.force)
        elif stage == "fabricate":
            stage_fabricate(cfg, force=args.force)
        elif stage == "vectors":
            stage_vectors(cfg, force=args.force)
        elif stage == "project":
            stage_project(cfg)
        else:
            print(f"[warn] unknown stage {stage!r} — ignored")
            continue
        print(f"  -- {stage} finished in {time.perf_counter() - t0:.1f} s")

    print(f"\nbuilt {len(REPORT.built)} things, skipped {len(REPORT.skipped)}")
    if REPORT.skipped:
        print("skipped:")
        for what, why, by_design in REPORT.skipped:
            print(f"  - {what}: {why}{' (by design)' if by_design else ''}")
    print("\nStart:")
    print(f"  1. open {cfg.out_dir / 'open_in_qgis.bat'}")
    print(f"     (or open {cfg.out_dir / 'waveshed_torture.qgs'} in QGIS)")
    print("  2. click 'Enable macros' in the bar QGIS shows at the top")
    print("  The project then registers its Browser connections, serves the local")
    print("  Terrain-RGB fixture and checks every layer. Details in "
          f"{cfg.out_dir / 'README.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
