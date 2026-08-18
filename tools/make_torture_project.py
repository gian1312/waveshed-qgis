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

TERRARIUM_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
COPERNICUS_URL = ("https://copernicus-dem-30m.s3.amazonaws.com/"
                  "Copernicus_DSM_COG_10_{tile}_DEM/Copernicus_DSM_COG_10_{tile}_DEM.tif")
SKADI_URL = "https://s3.amazonaws.com/elevation-tiles-prod/skadi/{lat}/{name}.hgt.gz"

COP_BERN = "N46_00_E007_00"
COP_DEADSEA = "N31_00_E035_00"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Cfg:
    """Resolved configuration — every path absolute, every option typed."""

    maptiler_key: str = ""
    mapbox_token: str = ""
    nextzen_key: str = ""
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
        mapbox_token=str(keys.get("mapbox_token", "") or "").strip(),
        nextzen_key=str(keys.get("nextzen_key", "") or "").strip(),
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
    )
    return cfg


# ---------------------------------------------------------------------------
# Reporting — every skipped row is recorded, never silently dropped
# ---------------------------------------------------------------------------

@dataclass
class Report:
    built: List[str] = field(default_factory=list)
    skipped: List[Tuple[str, str]] = field(default_factory=list)

    def ok(self, what: str) -> None:
        self.built.append(what)
        print(f"  [ok]   {what}")

    def skip(self, what: str, why: str) -> None:
        self.skipped.append((what, why))
        print(f"  [skip] {what} — {why}")


REPORT = Report()


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
                        (COP_DEADSEA, "Copernicus GLO-30, Dead Sea")):
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
    for z in cfg.terrain_rgb_zooms:
        x0, y0 = deg2tile(AOI_N, AOI_W, z)
        x1, y1 = deg2tile(AOI_S, AOI_E, z)
        for x in range(min(x0, x1), max(x0, x1) + 1):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                dest = root / str(z) / str(x) / f"{y}.png"
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
        REPORT.skip("all derived DEM fixtures", "Copernicus source tile missing — run --stages fetch")
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
    _check_no_void_fill(base)
    REPORT.ok(f"base_wgs84.tif ({_shape(base)})")

    halo = base_dir / "halo_wgs84.tif"
    if fresh(halo):
        gdal.Translate(str(halo), str(src_bern), options=gdal.TranslateOptions(
            format="GTiff", projWin=[HALO_W, HALO_N, HALO_E, HALO_S],
            outputType=gdal.GDT_Float32))
    _check_no_void_fill(halo)
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
        _check_no_void_fill(dead)
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
        _check_no_void_fill(dst)
        REPORT.ok(f"{name} (EPSG:{epsg}, outside-source = -9999 nodata)")

    # --- synthetic rasters in CRS the AOI cannot reach ----------------------
    # Krovak (5514) covers Czechia and US survey feet (2229) covers California,
    # so warping Swiss ground into them would be out of domain and meaningless.
    # What these two fixtures test is CRS handling, not elevation values, so a
    # deterministic synthetic surface in the right place is the honest fixture.
    _synthetic(var / "krovak5514.tif", 5514, -760000.0, -1030000.0, 30.0, fresh, note="Prague")
    REPORT.ok("krovak5514.tif (EPSG:5514 — must hard-error at ingest: 'Projection not found')")
    _synthetic(var / "feet_2229.tif", 2229, 6400000.0, 1900000.0, 100.0, fresh, note="Los Angeles")
    REPORT.ok("feet_2229.tif (EPSG:2229, US survey feet — geotransform is in feet too)")

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
        REPORT.skip("big/monolith.tif", "make_monolith = false in torture.local.ini")

    _buildings(cfg, fresh)


def _check_no_void_fill(path: Path, limit: float = 0.005) -> None:
    """Warn loudly if a clip contains a suspicious amount of exact 0 m.

    A ``projWin`` that reaches past the source tile is filled with zeros by
    GDAL, and a fixture with a 0 m band in it is worse than no fixture at all:
    it looks exactly like the silent provider failure these tests hunt for.
    """
    gdal, _o, _s = _gdal()
    ds = gdal.Open(str(path))
    arr = ds.GetRasterBand(1).ReadAsArray()
    ds = None
    frac = float((arr == 0).mean())
    if frac > limit:
        REPORT.skip(f"{path.name} 0 m check",
                    f"{frac * 100:.1f}% of pixels are exactly 0 m — the clip window "
                    f"probably reaches past the source tile. Fix the AOI constants "
                    f"in tools/make_torture_project.py and rebuild with --force.")


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
    for tile in cfg.swiss_tiles:
        matches = sorted(src_dir.glob(f"swissaltiregio_{tile}_*.xyz.zip"))
        if not matches:
            REPORT.skip(f"swiss tile {tile}", "not present in swissalti_tiles")
            continue
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
    {"name": "Huge (must be refused)", "west": -10.0, "south": 35.0, "east": 30.0, "north": 60.0,
     "res_m": "2", "stresses": "Size warning / 2 M-tile cap error — must never hang."},
    {"name": "Fiji antimeridian", "west": 179.6, "south": -18.3, "east": 180.0, "north": -17.9,
     "res_m": "30", "stresses": "Antimeridian unsupported — must fail loudly, not wrap."},
    {"name": "NRW (WCS coverage)", "west": 6.9, "south": 51.2, "east": 7.3, "north": 51.5,
     "res_m": "30", "stresses": "EPSG:25832 end-to-end through the WCS provider."},
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


def xyz_uri(url: str, *, zmax: Optional[int] = None, zmin: int = 0,
            interpretation: str = "") -> str:
    """A QGIS XYZ layer URI, encoded the way QGIS itself writes one."""
    enc = urllib.parse.quote(url, safe="/")
    parts = ["type=xyz", f"url={enc}"]
    if zmax is not None:
        parts.append(f"zmax={zmax}")
    parts.append(f"zmin={zmin}")
    if interpretation:
        parts.append(f"interpretation={interpretation}")
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
        parts.append(f"tileDimensions={urllib.parse.quote(dimensions, safe='')}")
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

    def rel(p: Path) -> str:
        return "./" + p.relative_to(out).as_posix()

    def add_file(row: str, group: str, name: str, path: Path, crs: str, expect: str,
                 *, provider: str = "gdal", kind: str = "raster",
                 geometry: str = "", source: str = "", style: str = "",
                 color: str = "227,26,28,255", checked: bool = False) -> None:
        if not path.exists():
            REPORT.skip(f"layer {name}", f"missing fixture {path.name}")
            return
        layers.append(Layer(row=row, group=group, name=name, provider=provider,
                            source=source or rel(path), crs=crs, kind=kind,
                            geometry=geometry, expect=expect, style=style,
                            color=color, checked=checked))

    # ---------------- §1 XYZ ------------------------------------------------
    layers.append(Layer("1.1", G_XYZ, "1.1 AWS Terrarium — z15 + interpretation (happy path)",
                        "wms", xyz_uri(TERRARIUM_URL, zmax=15, interpretation="terrarium"),
                        "EPSG:3857",
                        expect="Downloads through the shared Rust downloader on BOTH tabs. "
                               "Reference result for every other provider."))
    layers.append(Layer("1.2", G_XYZ, "1.2 AWS Terrarium — worst-case defaults (z18, no interpretation)",
                        "wms", xyz_uri(TERRARIUM_URL, zmax=18), "EPSG:3857",
                        expect="Must infer terrarium from the URL and clamp z18 -> z15 with a "
                               "log warning. Never flat-sea tiles."))
    if cfg.maptiler_key:
        layers.append(Layer("1.3a", G_XYZ, "1.3a MapTiler Terrain-RGB v1 (PNG, needs key)", "wms",
                            xyz_uri("https://api.maptiler.com/tiles/terrain-rgb/{z}/{x}/{y}.png"
                                    f"?key={cfg.maptiler_key}", zmax=12,
                                    interpretation="mapboxterrain"), "EPSG:3857",
                            expect="The terrain-rgb decode formula against a third-party "
                                   "encoder. Elevations plausible, not ~ -32000 m."))
        layers.append(Layer("1.4", G_XYZ, "1.4 MapTiler Terrain-RGB v2 (WebP, needs key)", "wms",
                            xyz_uri("https://api.maptiler.com/tiles/terrain-rgb-v2/{z}/{x}/{y}.webp"
                                    f"?key={cfg.maptiler_key}", zmax=12,
                                    interpretation="mapboxterrain"), "EPSG:3857",
                            expect="WebP codec — the toolkit decodes PNG only. Must fail loudly "
                                   "naming the decode error, never silent 0 m."))
    else:
        REPORT.skip("layers 1.3a + 1.4 (MapTiler Terrain-RGB v1/v2)",
                    "no maptiler_key in torture.local.ini")
    if (out / "terrain-rgb").is_dir():
        zmax = max(cfg.terrain_rgb_zooms)
        layers.append(Layer("1.3b", G_XYZ, "1.3b Local Terrain-RGB fixture (no key, offline)", "wms",
                            xyz_uri(cfg.terrain_rgb_base_url + "/{z}/{x}/{y}.png",
                                    zmax=zmax, zmin=min(cfg.terrain_rgb_zooms),
                                    interpretation="mapboxterrain"), "EPSG:3857",
                            expect="Same decode path as 1.3a, deterministic and offline. "
                                   "Serve it first: cd data/torture && python -m http.server 8000"))
    else:
        REPORT.skip("layer 1.3b (local Terrain-RGB)", "fixture not generated — run --stages fetch")
    layers.append(Layer("1.5", G_XYZ, "1.5 Ambiguous encoding (terrarium + terrain-rgb in the URL)",
                        "wms", xyz_uri("https://s3.amazonaws.com/elevation-tiles-prod/"
                                       "terrarium-terrain-rgb-mirror/{z}/{x}/{y}.png", zmax=15),
                        "EPSG:3857",
                        expect="Must raise the encoding-ambiguity error and refuse to run "
                               "(the canvas stays empty — that is not the test)."))
    layers.append(Layer("1.6", G_XYZ, "1.6 Dead endpoint", "wms",
                        xyz_uri("https://tiles.example.invalid/terrarium/{z}/{x}/{y}.png", zmax=15,
                                interpretation="terrarium"), "EPSG:3857",
                        expect="Total fetch failure -> hard error, nothing cached, no flat-0 terrain."))
    layers.append(Layer("1.7", G_XYZ, "1.7 OpenStreetMap XYZ (imagery classifier trap)", "wms",
                        xyz_uri("https://tile.openstreetmap.org/{z}/{x}/{y}.png", zmax=19),
                        "EPSG:3857", checked=True,
                        expect="Must be classified as imagery and never offered in the DEM picker."))
    if cfg.mapbox_token:
        layers.append(Layer("1.8", G_XYZ, "1.8 Mapbox Terrain-RGB (needs token)", "wms",
                            xyz_uri("https://api.mapbox.com/v4/mapbox.terrain-rgb/{z}/{x}/{y}.pngraw"
                                    f"?access_token={cfg.mapbox_token}", zmax=15,
                                    interpretation="mapboxterrain"), "EPSG:3857",
                            expect="known_services pins api.mapbox.com to encoding=mapbox, "
                                   "max_zoom=15. Must not decode as terrarium (~ -32000 m)."))
    else:
        REPORT.skip("layer 1.8 (Mapbox Terrain-RGB)", "no mapbox_token in torture.local.ini")
    if cfg.nextzen_key:
        layers.append(Layer("1.9", G_XYZ, "1.9 Nextzen Terrarium (needs key)", "wms",
                            xyz_uri("https://tile.nextzen.org/tilezen/terrain/v1/512/terrarium/"
                                    "{z}/{x}/{y}.png?api_key=" + cfg.nextzen_key, zmax=15),
                            "EPSG:3857",
                            expect="Second terrarium producer — cross-provider agreement check."))
    else:
        REPORT.skip("layer 1.9 (Nextzen Terrarium)", "no nextzen_key in torture.local.ini")

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
                          "file, never silently skip. Open question.")
    add_file("2.17", G_LOCAL, "2.17 SRTM .hgt", dem / "hgt" / "N46E007.hgt",
             "EPSG:4326", "Non-TIFF -> materialize helper path.")
    add_file("2.18", G_LOCAL, "2.18 USGSDEM ascii.dem", dem / "variants" / "ascii.dem",
             "EPSG:4326", "Exotic format -> materialize helper path.")
    add_file("2.19", G_LOCAL, "2.19 US survey feet (EPSG:2229, synthetic)",
             dem / "variants" / "feet_2229.tif", "EPSG:2229",
             "+units honored: the geotransform of a feet CRS is in feet too.")
    add_file("2.20", G_LOCAL, "2.20 Krovak (EPSG:5514, synthetic)",
             dem / "variants" / "krovak5514.tif", "EPSG:5514",
             "Unsupported by proj4rs — ingest must hard-error naming the CRS "
             "before any tile is written.")
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
                        expect="Remote COG. Known broken in the plugin: abspath() collapses "
                               "https:// -> https:/ (A10). Slow to open — that is the network."))

    # ---------------- §3 buildings ------------------------------------------
    bld = out / "buildings"
    add_file("3.3", G_BLD, "3.3 OSM buildings, Bern (GeoJSON)", bld / "osm_bern.geojson",
             "EPSG:4326", "Raw Overpass output, and the source the other fixtures in this "
                          "group are derived from; the plugin's own OSM download button "
                          "covers the built-in path (row 3.3 proper).",
             provider="ogr", kind="vector", geometry="Polygon", style="fill",
             color="55,126,184,255")
    if (bld / "multi.gpkg").is_file():
        layers.append(Layer("3.2a", G_BLD, "3.2a multi.gpkg | layer 'buildings' (west half)", "ogr",
                            "./buildings/multi.gpkg|layername=buildings", "EPSG:4326",
                            kind="vector", geometry="Polygon", style="fill", color="77,175,74,255",
                            expect="The |layername= sublayer path — the converter must receive "
                                   "the SELECTED layer."))
        layers.append(Layer("3.2b", G_BLD, "3.2b multi.gpkg | layer 'other_layer' (east half)", "ogr",
                            "./buildings/multi.gpkg|layername=other_layer", "EPSG:4326",
                            kind="vector", geometry="Polygon", style="fill", color="152,78,163,255",
                            expect="Run with THIS one selected: buildings must land in the east "
                                   "half. West-half output = the sublayer bug is back."))
    add_file("3.5", G_BLD, "3.5 Wrong-CRS shapefile (EPSG:2056)", bld / "lv95_buildings.shp",
             "EPSG:2056", "Reprojection-on-conversion path.",
             provider="ogr", kind="vector", geometry="Polygon", style="fill",
             color="255,127,0,255")
    add_file("3.6", G_BLD, "3.6 FlatGeobuf passthrough", bld / "direct.fgb",
             "EPSG:4326", "Must NOT be re-converted, and must not be handed over with the "
                          "wrong CRS.",
             provider="ogr", kind="vector", geometry="Polygon", style="fill",
             color="166,86,40,255")
    if cfg.swissbuildings3d and cfg.swissbuildings3d.exists():
        layers.append(Layer("3.1", G_BLD, "3.1 swissBUILDINGS3D 3.0", "ogr",
                            str(cfg.swissbuildings3d).replace("\\", "/"), "EPSG:2056",
                            kind="vector", geometry="Polygon", style="fill",
                            color="228,26,28,255",
                            expect="Real GDB/GPKG -> FGB conversion; LV95 vector CRS."))
    else:
        REPORT.skip("layer 3.1 (swissBUILDINGS3D)",
                    "no swissbuildings3d path in torture.local.ini (manual download)")

    # ---------------- §4 servers --------------------------------------------
    layers.append(Layer("4.1", G_SRV, "4.1 terrestris WMS — OSM-WMS (imagery)", "wms",
                        wms_uri("https://ows.terrestris.de/osm/service", "OSM-WMS"), "EPSG:3857",
                        expect="Classifier: imagery. The ARGB32 single-band trap — must never "
                               "be offered as a DEM."))
    layers.append(Layer("4.2", G_SRV, "4.2 swisstopo WMTS — pixelkarte-farbe (imagery)", "wms",
                        wmts_uri("https://wmts.geo.admin.ch/EPSG/3857/1.0.0/WMTSCapabilities.xml",
                                 "ch.swisstopo.pixelkarte-farbe", tile_matrix_set="3857_19",
                                 style="ch.swisstopo.pixelkarte-farbe", fmt="image/jpeg",
                                 dimensions="Time=current"), "EPSG:3857",
                        expect="Same as 4.1, through the WMTS code path."))
    layers.append(Layer("4.3", G_SRV, "4.3 NRW WCS — nw_dgm (elevation coverage, EPSG:25832)", "wcs",
                        wcs_uri("https://www.wcs.nrw.de/geobasis/wcs_nw_dgm", "nw_dgm",
                                crs="EPSG:25832"), "EPSG:25832",
                        expect="Coverage provider -> real Float32 values; band heuristics may "
                               "classify it as DEM. Third real CRS end-to-end. Zoom to the "
                               "'NRW' box in the run matrix — it covers North Rhine-Westphalia only."))
    layers.append(Layer("4.4", G_SRV, "4.4 USGS 3DEP WMS — elevation as GeoTIFF", "wms",
                        wms_uri("https://elevation.nationalmap.gov/arcgis/services/3DEPElevation/"
                                "ImageServer/WMSServer", "3DEPElevation", crs="EPSG:3857",
                                fmt="image/tiff"), "EPSG:3857",
                        expect="URL contains 'elevation' -> classified as DEM; image/tiff returns "
                               "real Float32. The ArcGIS ImageServer form of the same service is "
                               "registered as a Browser connection by the bootstrap script. "
                               "USA only — no data over Europe."))
    layers.append(Layer("4.5", G_SRV, "4.5 swisstopo WMS — pixelkarte-farbe (imagery)", "wms",
                        wms_uri("https://wms.geo.admin.ch/", "ch.swisstopo.pixelkarte-farbe",
                                crs="EPSG:2056"), "EPSG:2056",
                        expect="WMS imagery classification, free, no key."))
    layers.append(Layer("4.6", G_SRV, "4.6 swisstopo WMS — swissALTI3D hillshade (THE TRAP)", "wms",
                        wms_uri("https://wms.geo.admin.ch/",
                                "ch.swisstopo.swissalti3d-reliefschattierung", crs="EPSG:2056"),
                        "EPSG:2056",
                        expect="Looks like terrain, IS a rendered picture. The classifier must "
                               "call it imagery — picking it as a DEM is the exact silent "
                               "failure the provider guard exists for."))

    # ---------------- §5 run matrix -----------------------------------------
    vec = out / "vectors"
    add_file("5.1", G_RUN, "5.1 Run matrix (bboxes)", vec / "run_matrix.geojson", "EPSG:4326",
             "One polygon per case in guide §5. Read the bbox off the attribute table, "
             "type it into the Map Converter.",
             provider="ogr", kind="vector", geometry="Polygon", style="fill",
             color="227,26,28,255", checked=True)
    add_file("5.2", G_RUN, "5.2 Sites (TX/RX, incl. -430 m AMSL)", vec / "sites.geojson",
             "EPSG:4326", "Site-table entries: heights and AGL/AMSL modes are attributes.",
             provider="ogr", kind="vector", geometry="Point", style="point",
             color="0,128,0,255", checked=True)
    add_file("5.3", G_RUN, "5.3 P2P links", vec / "p2p_links.geojson", "EPSG:4326",
             "Endpoints for the P2P tab and the batch CSVs in batch/.",
             provider="ogr", kind="vector", geometry="LineString", style="line",
             color="55,126,184,255", checked=True)

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


def _srs(parent: ET.Element, authid: str) -> None:
    """A <spatialrefsys> block QGIS can actually restore (see _CRS_DEFS)."""
    srs = ET.SubElement(parent, "spatialrefsys")
    spec = _CRS_DEFS.get(authid)
    if spec is None:
        # Not a project CRS we ship a definition for — the authid is all we
        # have. Layers still get their CRS from their own provider.
        ET.SubElement(srs, "authid").text = authid
        return
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
            print("  [note] no GDAL here — rasters keep QGIS's default flat-grey "
                  "rendering. Re-run --stages project with QGIS's Python for a stretch.")
            return cache
        for lyr in missing:
            path = lyr.source
            if path.startswith("./"):
                path = str(cfg.out_dir / path[2:])
            try:
                ds = gdal.Open(path)
                mn, mx, mean, std = ds.GetRasterBand(1).GetStatistics(True, True)
                ds = None
                # mean +- 2 sigma, clipped: a full min/max stretch on a tile that
                # spans the whole Alps renders the 500 m valley floor as black.
                cache[lyr.source] = [max(mn, mean - 2 * std), min(mx, mean + 2 * std)]
            except Exception:
                continue
        cache_path.write_text(json.dumps(cache, indent=1), encoding="utf-8")
    return cache


def _raster_pipe(parent: ET.Element, vmin: float, vmax: float) -> None:
    """A stretched single-band grey renderer (attribute is grayBand, not band)."""
    pipe = ET.SubElement(parent, "pipe")
    rr = ET.SubElement(pipe, "rasterrenderer", {
        "type": "singlebandgray", "grayBand": "1", "opacity": "1", "alphaBand": "-1",
        "gradient": "BlackToWhite", "nodataColor": ""})
    ET.SubElement(rr, "rasterTransparency")
    origin = ET.SubElement(rr, "minMaxOrigin")
    for tag, text in (("limitsType", "MinMax"), ("extent", "WholeRaster"),
                      ("statAccuracy", "Estimated"), ("cumulativeCutLower", "0.02"),
                      ("cumulativeCutUpper", "0.98"), ("stdDevFactor", "2")):
        ET.SubElement(origin, tag).text = text
    ce = ET.SubElement(rr, "contrastEnhancement")
    ET.SubElement(ce, "minValue").text = f"{vmin:.4f}"
    ET.SubElement(ce, "maxValue").text = f"{vmax:.4f}"
    ET.SubElement(ce, "algorithm").text = "StretchToMinimumMaximum"


def _layer_id(layer: Layer) -> str:
    slug = "".join(c if c.isalnum() else "_" for c in f"{layer.row}_{layer.name}")[:48]
    digest = hashlib.md5(f"{layer.row}|{layer.source}".encode("utf-8")).hexdigest()[:8]
    return f"tt_{slug}_{digest}"


def write_project(cfg: Cfg, layers: Sequence[Layer]) -> Path:
    """Write the .qgs. Written by hand, deliberately: QGIS is not importable
    outside QGIS, and the whole point is that this runs anywhere."""
    ids = {id(lyr): _layer_id(lyr) for lyr in layers}
    raster_stats = _raster_stats(cfg, layers)

    root = ET.Element("qgis", {
        "projectname": "Waveshed torture set",
        "version": cfg.qgis_version,
        "saveUser": "", "saveUserFull": "",
    })
    ET.SubElement(root, "homePath", {"path": ""})
    ET.SubElement(root, "title").text = "Waveshed torture set — TEST_DATA_GUIDE.md"
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
            "hasScaleBasedVisibilityFlag": "0", "minScale": "1e+08", "maxScale": "0",
            "autoRefreshTime": "0", "refreshOnNotifyEnabled": "0",
            "refreshOnNotifyMessage": "", "styleCategories": "AllStyleCategories",
            **({"geometry": lyr.geometry} if lyr.geometry else {}),
        })
        ET.SubElement(ml, "id").text = ids[id(lyr)]
        ET.SubElement(ml, "datasource").text = lyr.source
        ET.SubElement(ml, "layername").text = lyr.name
        if lyr.crs:
            # Documentation, mostly: for every layer here the data provider
            # reports the same CRS and wins, which is what we want — the file
            # (or the service) is the authority on its own coordinates.
            _srs(ET.SubElement(ml, "srs"), lyr.crs)
        prov = ET.SubElement(ml, "provider")
        prov.text = lyr.provider
        if lyr.kind == "vector":
            prov.set("encoding", "UTF-8")
        abstract = ET.SubElement(ml, "abstract")
        abstract.text = f"[{lyr.row}] {lyr.expect}"
        if lyr.style:
            _symbol(ml, lyr.style, lyr.color)
        stats = raster_stats.get(lyr.source)
        if stats and stats[1] > stats[0]:
            _raster_pipe(ml, stats[0], stats[1])
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
    ET.SubElement(macros, "pythonCode", {"type": "QString"}).text = _setup_code(cfg, MACRO)
    gui = ET.SubElement(props, "Gui")
    ET.SubElement(gui, "CanvasColorBluePart", {"type": "int"}).text = "255"
    ET.SubElement(gui, "CanvasColorGreenPart", {"type": "int"}).text = "255"
    ET.SubElement(gui, "CanvasColorRedPart", {"type": "int"}).text = "255"
    ET.SubElement(root, "visibility-presets")
    ET.SubElement(root, "transformContext")
    meta = ET.SubElement(root, "projectMetadata")
    ET.SubElement(meta, "title").text = "Waveshed torture set"
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

def _connections(cfg: Cfg) -> Dict[str, List[Dict[str, str]]]:
    xyz = [
        {"name": "TT 1.1 Terrarium AWS (z15)", "url": TERRARIUM_URL,
         "zmin": "0", "zmax": "15", "interpretation": "terrarium"},
        {"name": "TT 1.2 Terrarium AWS (worst-case defaults)", "url": TERRARIUM_URL,
         "zmin": "0", "zmax": "18", "interpretation": ""},
        {"name": "TT 1.5 Ambiguous encoding",
         "url": "https://s3.amazonaws.com/elevation-tiles-prod/terrarium-terrain-rgb-mirror/{z}/{x}/{y}.png",
         "zmin": "0", "zmax": "15", "interpretation": ""},
        {"name": "TT 1.6 Dead endpoint",
         "url": "https://tiles.example.invalid/terrarium/{z}/{x}/{y}.png",
         "zmin": "0", "zmax": "15", "interpretation": "terrarium"},
        {"name": "TT 1.7 OpenStreetMap", "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
         "zmin": "0", "zmax": "19", "interpretation": ""},
        {"name": "TT 1.3b Local Terrain-RGB", "url": cfg.terrain_rgb_base_url + "/{z}/{x}/{y}.png",
         "zmin": str(min(cfg.terrain_rgb_zooms)), "zmax": str(max(cfg.terrain_rgb_zooms)),
         "interpretation": "mapboxterrain"},
    ]
    if cfg.maptiler_key:
        xyz.append({"name": "TT 1.3a MapTiler Terrain-RGB v1",
                    "url": f"https://api.maptiler.com/tiles/terrain-rgb/{{z}}/{{x}}/{{y}}.png?key={cfg.maptiler_key}",
                    "zmin": "0", "zmax": "12", "interpretation": "mapboxterrain"})
        xyz.append({"name": "TT 1.4 MapTiler Terrain-RGB v2 (WebP)",
                    "url": f"https://api.maptiler.com/tiles/terrain-rgb-v2/{{z}}/{{x}}/{{y}}.webp?key={cfg.maptiler_key}",
                    "zmin": "0", "zmax": "12", "interpretation": "mapboxterrain"})
    if cfg.mapbox_token:
        xyz.append({"name": "TT 1.8 Mapbox Terrain-RGB",
                    "url": f"https://api.mapbox.com/v4/mapbox.terrain-rgb/{{z}}/{{x}}/{{y}}.pngraw?access_token={cfg.mapbox_token}",
                    "zmin": "0", "zmax": "15", "interpretation": "mapboxterrain"})

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

from qgis.core import Qgis, QgsMessageLog, QgsProject, QgsSettings

CONNECTIONS = __CONNECTIONS_JSON__
TILE_PORT = __TILE_PORT__
TILE_AUTOSTART = __TILE_AUTOSTART__
LOG_TAG = "Waveshed torture"

_server = None


def _log(message, level=None):
    QgsMessageLog.logMessage(message, LOG_TAG,
                             getattr(Qgis, "Info", 0) if level is None else level)


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
    _log(f"{n} Browser connections registered (F5 in the Browser panel to see them)")
    return n


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
    return f"Terrain-RGB fixture on http://127.0.0.1:{TILE_PORT}/terrain-rgb/"


def stop_tile_server():
    global _server
    if _server is not None:
        try:
            _server.shutdown()
            _server.server_close()
        finally:
            _server = None


def check_layers():
    """``(total, [invalid names])`` — a layer that failed is named, not silent."""
    layers = sorted(QgsProject.instance().mapLayers().values(), key=lambda l: l.name())
    bad = [l for l in layers if not l.isValid()]
    for lyr in bad:
        _log(f"INVALID layer: {lyr.name()} -> {lyr.source()}", getattr(Qgis, "Warning", 1))
    return len(layers), [l.name() for l in bad]


def setup():
    """Everything the torture set needs, in one call. Returns a summary line."""
    connections = register_connections()
    tiles = start_tile_server()
    total, bad = check_layers()
    summary = (f"{total} layers ({len(bad)} invalid), "
               f"{connections} Browser connections, {tiles}")
    _log(summary)
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


def _setup_code(cfg: Cfg, template: str) -> str:
    """Fill *template*'s placeholders with this build's connections and port."""
    host, port = _tile_server_target(cfg)
    body = (SETUP_BODY
            .replace("__CONNECTIONS_JSON__", json.dumps(_connections(cfg), indent=4))
            .replace("__TILE_PORT__", str(port))
            .replace("__TILE_AUTOSTART__",
                     "True" if (cfg.tile_server_autostart and host) else "False"))
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


def write_bootstrap(cfg: Cfg) -> Path:
    path = cfg.out_dir / "qgis_console_bootstrap.py"
    text = _setup_code(cfg, BOOTSTRAP).replace("__SELF__", str(path).replace("\\", "/"))
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
    bat = ["@echo off",
           "REM Opens the Waveshed torture project in QGIS.",
           "REM Generated by tools/make_torture_project.py - edit QGIS= below",
           "REM if your QGIS lives somewhere else.",
           "setlocal",
           'set "PROJECT=%~dp0waveshed_torture.qgs"',
           'set "QGIS="']
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
        "# Opens the Waveshed torture project in QGIS. Override with QGIS=/path/to/qgis\n"
        'HERE=$(cd "$(dirname "$0")" && pwd)\n'
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
    add("1. Double-click **`open_in_qgis.bat`** (Windows) or run `./open_in_qgis.sh`.")
    add(f"   Opening `{project.name}` in QGIS by hand does the same thing.")
    add("2. Click **Enable macros** in the bar QGIS shows at the top.")
    add("")
    add("That is the whole setup. The project sets itself up on open:")
    add("")
    add("* registers every Browser connection of guide §1 and §4, XYZ")
    add("  *interpretation* included (nothing but the connection dialog can set that);")
    add(f"* serves the local Terrain-RGB fixture on `http://127.0.0.1:{_tile_server_target(cfg)[1]}/`")
    add("  so row 1.3b works with no second terminal;")
    add("* checks that every layer resolved and names any that did not.")
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
        for what, why in REPORT.skipped:
            add(f"| {what} | {why} |")
        add("")
        add("Fill in the missing key or path in `torture.local.ini` and re-run")
        add("`python3 tools/make_torture_project.py` — nothing already built is re-downloaded.")
        add("")

    add("## Keys")
    add("")
    add("| Key | Needed for | Get it |")
    add("|---|---|---|")
    add("| `maptiler_key` | rows 1.3a + 1.4 (Terrain-RGB v1 PNG / v2 WebP) | https://cloud.maptiler.com/account/keys/ |")
    add("| `mapbox_token` | row 1.8 (api.mapbox.com terrain-rgb) | https://account.mapbox.com/access-tokens/ |")
    add("| `nextzen_key` | row 1.9 (nextzen terrarium) | https://developers.nextzen.org/ |")
    add("")
    add("Everything else — AWS Terrain Tiles, swisstopo, terrestris, NRW WCS, USGS 3DEP,")
    add("Copernicus GLO-30, SRTM, Overpass, OpenFreeMap — needs no account.")
    add("")

    path = cfg.out_dir / "README.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def stage_project(cfg: Cfg) -> None:
    print("\n== project ==")
    cfg.out_dir.mkdir(parents=True, exist_ok=True)
    layers = build_catalogue(cfg)
    project = write_project(cfg, layers)
    bootstrap = write_bootstrap(cfg)
    launchers = write_launchers(cfg)
    readme = write_readme(cfg, layers, project)
    print(f"  [ok]   {len(layers)} layers -> {project}")
    print("  [ok]   self-setup macro embedded (connections + tile server + layer check)")
    for path in launchers:
        print(f"  [ok]   {path.name}")
    print(f"  [ok]   {bootstrap.name} (fallback when macros are off)")
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

    print(f"\nbuilt {len(REPORT.built)} things, skipped {len(REPORT.skipped)}")
    if REPORT.skipped:
        print("skipped:")
        for what, why in REPORT.skipped:
            print(f"  - {what}: {why}")
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
