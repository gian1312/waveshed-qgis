"""Persistent per-folder footprint index for local terrain folders.

A terrain folder (Settings → Local terrain dir, or Map Converter → Add Folder)
can hold tens of thousands of rasters in any mix of CRSs and formats. Every
decision the plugin makes about it — which files exist, which are usable,
where each one lies, which file wins where two overlap, whether the folder
reaches across the analysis area, which pool tiles are stale — is answered
from ONE index per folder instead of opening every file on every run:

* stored in the terrain cache (``<cache>/index/<md5(folder)>.json``), keyed by
  the folder path;
* per file: size + mtime (the change detector), readable flag + reason,
  driver, CRS (WKT, EPSG authid or none, a proj string for non-EPSG CRSs),
  native and WGS84 bounds plus a densified WGS84 footprint ring, pixel size
  X/Y (native units and metres), data type, band count, compression,
  geotransform shape (rotated / flipped), scale/offset and nodata;
* refreshed incrementally: unchanged files cost one ``stat`` (threaded — a
  stat on the Windows mount is ~3 ms), new or changed plain GeoTIFFs are
  probed by ONE ``aether_converter probe`` run (native, parallel; entries
  built from its header facts, CRS work batched per EPSG code), everything
  else — and every GeoTIFF the probe does not vouch for — with GDAL in a
  thread pool (GDAL releases the GIL for I/O, measured ~7x on 8 threads),
  removed files drop out. Never call :meth:`FolderIndex.refresh` on the GUI
  thread — the GUI runs it in ``gui/terrain_folder_scan``.

Pure GDAL/OSR (no QGIS objects), so it is thread-safe and testable without a
QGIS instance.
"""

from __future__ import annotations

import contextlib
import gc
import itertools
import json
import math
import os
import re
import struct
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple

import numpy as np
from osgeo import gdal

from . import hashes

try:
    from osgeo import osr
except ImportError:  # the unit-test stub of osgeo has no osr; tests that
    osr = None       # exercise the index swap the real package in

#: No console window for the probe on Windows. ``sys.platform``, the gate of
#: every Windows path (as ``terrain_adapter._is_windows``, which cannot be
#: imported here — it imports this module); evaluated once, at import.
_SUBPROCESS_FLAGS = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    if sys.platform == "win32" else 0
)


def _log(msg: str) -> None:
    try:
        from qgis.core import Qgis, QgsMessageLog
        QgsMessageLog.logMessage(msg, "Waveshed-Terrain", Qgis.MessageLevel.Info)
    except Exception:  # noqa: BLE001 — logging never breaks a scan
        pass

#: Bump when the stored layout changes (the whole index is dropped).
INDEX_SCHEMA = "ti1"
#: Bump when what a probe records changes (every file is re-probed once).
PROBE_VERSION = 1

#: Threads for stat and probe passes. More than ~16 buys nothing on the
#: Windows mount (measured: 8 threads 4 ms/file, 16-32 threads no better).
SCAN_WORKERS = 16

#: A refresh younger than this is reused as is (the GUI pre-flight scans,
#: then the worker thread starts a second later and must not re-stat 40k
#: files).
FRESH_S = 30.0

#: Raster extensions the folder scan considers. Whitelist on purpose: every
#: sidecar (``.aux.xml``, ``.ovr``, ``.msk``, ``.hdr``, ``.prj``, world files
#: ``.tfw``/``.wld``/…, ``.rrd``) and every vector or text file is excluded by
#: construction. Anything that is not a plain GeoTIFF reaches the converter
#: through ``terrain_adapter.materialize_for_converter`` (a native-resolution
#: GDAL window copy).
RASTER_EXTS = frozenset((
    ".tif", ".tiff", ".gtif",          # GeoTIFF / COG
    ".dem", ".hgt",                    # USGS DEM, SRTM
    ".asc",                            # ESRI ASCII grid
    ".img",                            # ERDAS Imagine
    ".bil", ".bip", ".bsq", ".flt",    # ESRI raw (with .hdr sidecar)
    ".xyz",                            # gridded XYZ (irregular → unusable)
    ".jp2", ".j2k",                    # JPEG 2000
    ".nc",                             # netCDF (single variable)
    ".grd",                            # Surfer / GMT grid
    ".dt0", ".dt1", ".dt2",            # DTED
    ".ter", ".bt", ".rst", ".sdat", ".kea",
))

#: Containers whose members are scanned through ``/vsizip/``.
ARCHIVE_EXTS = frozenset((".zip",))

#: Excluded even though GDAL reads them, with the reason (kept here so the
#: reason travels with the rule). A ``.vrt`` in a terrain folder is almost
#: always a mosaic OF the files beside it: scanning it would add every one of
#: those files a second time, at one priority, and its member paths are
#: relative to wherever it was built and may not resolve here at all. As a
#: QGIS LAYER a .vrt works (torture rows 2.9 / folder:vrt-ignored).
EXCLUDED_EXTS = {
    ".vrt": "a VRT is a mosaic of other files (usually the ones beside it) — "
            "scanning it would add them twice; load it as a layer instead",
}

#: GeoTIFF compressions the converter's TIFF reader decodes on every build.
#: Everything else (ZSTD, LERC, JPEG, JXL, LZMA, WEBP, …) is window-copied
#: to an uncompressed GeoTIFF first — decided here, by the file's own
#: metadata, never by its extension.
CONVERTER_COMPRESSIONS = frozenset(("", "NONE", "LZW", "DEFLATE", "PACKBITS"))

#: A plain GeoTIFF above this size is window-copied per tile (decode RAM).
MATERIALIZE_MAX_TIFF_BYTES = 512 * 1024 * 1024

#: Halo, in source pixels, around a tile when deciding which files can
#: contribute (mirrors terrain_adapter._SOURCE_HALO_PX).
SOURCE_HALO_PX = 4

# Overlap priority (per folder, QgsSettings "waveshed/overlap_priority/<md5>")
PRIORITY_FINEST = "finest"
PRIORITY_NAME = "name"
PRIORITY_LABELS = {
    PRIORITY_FINEST: "finest resolution wins",
    PRIORITY_NAME: "file name order",
}

# Problem kinds (pre-flight)
KIND_UNUSABLE = "unusable"
KIND_NOCRS = "nocrs"
KIND_SUSPECT = "suspect"

#: Pixel sizes within this ratio count as "the same resolution".
SAME_RES_RATIO = 1.25

_M_PER_DEG_LAT = 110_574.0
_M_PER_DEG_LON_EQ = 111_320.0


class ScanCancelled(Exception):
    """The caller's should_cancel() asked a refresh to stop."""


class RefreshStats(NamedTuple):
    files: int
    new: int
    changed: int
    removed: int
    unchanged: int
    stat_s: float
    probe_s: float
    total_s: float
    reused: bool = False
    #: "converter" when ``aether_converter probe`` read the new/changed
    #: GeoTIFFs, "" when only the in-process probes ran.
    probe_engine: str = ""
    #: Mean cost of opening one NEW file, ms — the converter probe's own
    #: average (its ``[Probe] … open X ms avg`` line, also in the probe log
    #: line); None when no probe ran.
    open_ms: Optional[float] = None


# ---------------------------------------------------------------------------
# Folder listing
# ---------------------------------------------------------------------------

def _ext(name: str) -> str:
    return os.path.splitext(name.lower())[1]


def is_candidate_name(name: str) -> bool:
    """True when a file name looks like a terrain raster the scan should probe."""
    low = os.path.basename(name).lower()
    # macOS AppleDouble forks (._x.tif) and Office lock files are not rasters.
    if low.startswith("._") or low.startswith("~$"):
        return False
    return _ext(low) in RASTER_EXTS


def _walk(folder: str) -> Tuple[List[str], List[str]]:
    """``(raster files, archives)`` under *folder*, recursive, unsorted."""
    files: List[str] = []
    archives: List[str] = []
    for root, _dirs, names in os.walk(folder):
        for n in names:
            if is_candidate_name(n):
                files.append(os.path.join(root, n))
            elif _ext(n) in ARCHIVE_EXTS and not n.startswith("._"):
                archives.append(os.path.join(root, n))
    return files, archives


def _walk_stat(folder: str, workers: int = 16
               ) -> Tuple[List[str], List[str],
                          Dict[str, Optional[Tuple[int, int]]], set]:
    """:func:`_walk` plus each file's ``(size, mtime_ns)`` and the set of every
    file path in the tree (lower-cased; for sidecar lookups without a disk
    call per candidate). On Windows ``DirEntry.stat()`` comes with the
    listing, so a repeat scan of an unchanged 43 000-file folder costs no
    per-file call; elsewhere it is a syscall, made in parallel."""
    files: List[str] = []
    archives: List[str] = []
    stats: Dict[str, Optional[Tuple[int, int]]] = {}
    names: set = set()
    free_stat = os.name == "nt"
    stack = [folder]
    while stack:
        root = stack.pop()
        try:
            it = os.scandir(root)
        except OSError:
            continue
        with it:
            for de in it:
                try:
                    if de.is_dir():
                        if not de.is_symlink():   # as os.walk: no link descent
                            stack.append(de.path)
                        continue
                except OSError:
                    continue
                n = de.name
                names.add(de.path.lower())
                if is_candidate_name(n):
                    files.append(de.path)
                elif _ext(n) in ARCHIVE_EXTS and not n.startswith("._"):
                    archives.append(de.path)
                else:
                    continue
                if free_stat:
                    try:
                        st = de.stat()
                        stats[de.path] = (int(st.st_size), int(st.st_mtime_ns))
                    except OSError:
                        stats[de.path] = None
    if not free_stat:
        targets = files + archives
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for path, st in zip(targets, ex.map(_stat, targets)):
                stats[path] = st
    return files, archives, stats, names


def vsizip_path(archive: str, member: str) -> str:
    return "/vsizip/" + os.path.abspath(archive).replace("\\", "/") + "/" + member


def archive_members(archive: str) -> List[str]:
    """Raster members of a zip, as ``/vsizip/`` paths (sorted)."""
    root = "/vsizip/" + os.path.abspath(archive).replace("\\", "/")
    try:
        names = gdal.ReadDirRecursive(root) or []
    except Exception:  # noqa: BLE001 — an unreadable zip has no members
        names = []
    return sorted(vsizip_path(archive, n) for n in names
                  if not n.endswith("/") and is_candidate_name(n))


def name_key(path: str) -> str:
    """Sort key of the folder's FILE-NAME order: a zip member sorts where its
    archive does (``/vsizip/`` would otherwise put every member first)."""
    return path[len("/vsizip/"):] if path.startswith("/vsizip/") else path


def list_rasters(folder: str) -> List[str]:
    """Every terrain raster under *folder* (recursive, zip members included),
    sorted ascending by path — the folder's file-name order."""
    files, archives = _walk(folder)
    out = list(files)
    for a in archives:
        out.extend(archive_members(a))
    return sorted(out, key=name_key)


# ---------------------------------------------------------------------------
# Probing one file
# ---------------------------------------------------------------------------

_tls = threading.local()


def _wgs84() -> osr.SpatialReference:
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _transform(wkt: str, inverse: bool = False) -> Optional[osr.CoordinateTransformation]:
    """Cached (per thread) transform between *wkt* and WGS84."""
    cache = getattr(_tls, "xforms", None)
    if cache is None:
        cache = _tls.xforms = {}
    key = (wkt, inverse)
    if key not in cache:
        try:
            src = osr.SpatialReference()
            src.ImportFromWkt(wkt)
            src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            dst = _wgs84()
            cache[key] = (osr.CoordinateTransformation(dst, src) if inverse
                          else osr.CoordinateTransformation(src, dst))
        except Exception:  # noqa: BLE001
            cache[key] = None
    return cache[key]


def _densify_ring(corners: List[Tuple[float, float]], per_edge: int
                  ) -> List[Tuple[float, float]]:
    pts: List[Tuple[float, float]] = []
    n = len(corners)
    for i in range(n):
        (x0, y0), (x1, y1) = corners[i], corners[(i + 1) % n]
        for k in range(per_edge):
            f = k / per_edge
            pts.append((x0 + (x1 - x0) * f, y0 + (y1 - y0) * f))
    return pts


def _transform_points(ct: Any, pts: List[Tuple[float, float]]
                      ) -> List[Tuple[float, float]]:
    out: List[Tuple[float, float]] = []
    try:
        res = ct.TransformPoints([(x, y) for x, y in pts])
    except Exception:  # noqa: BLE001 — fall back point by point
        res = []
        for x, y in pts:
            try:
                res.append(ct.TransformPoint(x, y))
            except Exception:  # noqa: BLE001
                res.append((float("inf"), float("inf"), 0.0))
    for r in res:
        x, y = float(r[0]), float(r[1])
        if math.isfinite(x) and math.isfinite(y):
            out.append((x, y))
    return out


def _corners(gt: Tuple[float, ...], xsize: int, ysize: int
             ) -> List[Tuple[float, float]]:
    return [(gt[0] + gt[1] * px + gt[2] * py, gt[3] + gt[4] * px + gt[5] * py)
            for px, py in ((0, 0), (xsize, 0), (xsize, ysize), (0, ysize))]


def _bounds(pts: Iterable[Tuple[float, float]]) -> Optional[Dict[str, float]]:
    pts = list(pts)
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return {"west": min(xs), "east": max(xs), "south": min(ys), "north": max(ys)}


def _parse_srs(wkt: str) -> Optional[osr.SpatialReference]:
    """A usable horizontal CRS from *wkt*, or None (absent, LOCAL_CS husk,
    geocentric, unparseable)."""
    if not wkt:
        return None
    srs = osr.SpatialReference()
    try:
        if srs.ImportFromWkt(wkt) != 0:
            return None
    except Exception:  # noqa: BLE001
        return None
    if srs.IsLocal() or not (srs.IsProjected() or srs.IsGeographic()):
        return None
    return srs


def _crs_fields(srs: osr.SpatialReference) -> Dict[str, Any]:
    """CRS facts the index stores: WKT, EPSG authid (or ""), proj string."""
    node = "PROJCS" if srs.IsProjected() else "GEOGCS"
    authid = ""
    try:
        if (srs.GetAuthorityName(None) or "").upper() == "EPSG":
            authid = f"EPSG:{srs.GetAuthorityCode(None)}"
        elif (srs.GetAuthorityName(node) or "").upper() == "EPSG" \
                and not srs.IsCompound():
            authid = f"EPSG:{srs.GetAuthorityCode(node)}"
        if not authid:
            probe = srs.Clone()
            if probe.AutoIdentifyEPSG() == 0 and \
                    (probe.GetAuthorityName(None) or "").upper() == "EPSG":
                authid = f"EPSG:{probe.GetAuthorityCode(None)}"
        if not authid:
            # ESRI-flavoured WKT (.prj sidecars) carries no authority; let
            # PROJ name it when it is sure.
            for match, confidence in (srs.FindMatches() or [])[:1]:
                if confidence >= 90 and \
                        (match.GetAuthorityName(None) or "").upper() == "EPSG":
                    authid = f"EPSG:{match.GetAuthorityCode(None)}"
    except Exception:  # noqa: BLE001
        authid = ""
    try:
        proj4 = srs.ExportToProj4() or ""
    except Exception:  # noqa: BLE001
        proj4 = ""
    return {
        "crs_wkt": srs.ExportToWkt(),
        "crs_authid": authid,
        "crs_proj4": proj4.strip(),
        "geographic": bool(srs.IsGeographic()),
    }


def _crs_unit(wkt: str, geographic: bool) -> float:
    """Angular (radians per unit) or linear (metres per unit) unit of *wkt*,
    cached per thread — parsing the WKT per file cost ~1 ms."""
    cache = getattr(_tls, "units", None)
    if cache is None:
        cache = _tls.units = {}
    key = (wkt, geographic)
    if key not in cache:
        srs = osr.SpatialReference()
        srs.ImportFromWkt(wkt)
        cache[key] = ((srs.GetAngularUnits() or math.radians(1.0)) if geographic
                      else (srs.GetLinearUnits() or 1.0))
    return cache[key]


def _geo_fields(gt: Tuple[float, ...], xsize: int, ysize: int,
                crs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Bounds, WGS84 footprint and pixel sizes for a georeferenced raster in
    *crs*; None when the CRS cannot be transformed to WGS84 there."""
    corners = _corners(gt, xsize, ysize)
    native = _bounds(corners)
    if crs["crs_authid"] == "EPSG:4326":
        ring = _densify_ring(corners, 4)
        wgs_bbox = dict(native)
    else:
        ct = _transform(crs["crs_wkt"])
        if ct is None:
            return None
        ring = _transform_points(ct, _densify_ring(corners, 4))
        dense = _transform_points(ct, _densify_ring(corners, 20))
        if len(ring) < len(corners) * 4 or len(dense) < 4:
            return None
        wgs_bbox = _bounds(dense + ring)
    return _geo_result(gt, native, wgs_bbox, ring,
                       _crs_unit(crs["crs_wkt"], crs["geographic"]),
                       crs["geographic"])


def _geo_result(gt: Tuple[float, ...], native: Dict[str, float],
                wgs_bbox: Dict[str, float], ring: Iterable[Tuple[float, float]],
                unit: float, geographic: bool,
                footprint: Optional[List[List[float]]] = None) -> Dict[str, Any]:
    """The geometry fields of an entry (shared by :func:`_geo_fields` and the
    batched :func:`_geo_fields_many`, so both compute them identically;
    *footprint*: the ring already rounded by :func:`_round7`)."""
    px_x = math.hypot(gt[1], gt[4])
    px_y = math.hypot(gt[2], gt[5])
    if geographic:
        clat = (wgs_bbox["north"] + wgs_bbox["south"]) / 2.0
        deg = math.degrees(unit)
        px_x_m = px_x * deg * _M_PER_DEG_LON_EQ * max(math.cos(math.radians(clat)), 0.01)
        px_y_m = px_y * deg * _M_PER_DEG_LAT
    else:
        px_x_m, px_y_m = px_x * unit, px_y * unit
    return {
        "native_bounds": native,
        "wgs84_bounds": wgs_bbox,
        "footprint": (footprint if footprint is not None else
                      [[round(x, 7), round(y, 7)] for x, y in ring]),
        "px_x": px_x, "px_y": px_y,
        "px_x_m": px_x_m, "px_y_m": px_y_m,
        "px_m": math.sqrt(max(px_x_m, 1e-9) * max(px_y_m, 1e-9)),
    }


def _stat(path: str) -> Optional[Tuple[int, int]]:
    """(size, mtime_ns) of a file — of the containing archive for /vsizip/."""
    real = archive_of(path) or path
    try:
        st = os.stat(real)
    except OSError:
        return None
    return int(st.st_size), int(st.st_mtime_ns)


def dir_mtime_ns(folder: str) -> Optional[int]:
    """The folder's own mtime (ns) — changes when an entry directly in it is
    added, removed or renamed; None when it cannot be stat'ed."""
    try:
        return int(os.stat(folder).st_mtime_ns)
    except OSError:
        return None


def archive_of(path: str) -> Optional[str]:
    """The zip a ``/vsizip/`` path points into, or None."""
    if not path.startswith("/vsizip/"):
        return None
    rest = path[len("/vsizip/"):]
    low = rest.lower()
    i = low.find(".zip/")
    if i < 0:
        return rest if low.endswith(".zip") else None
    return rest[:i + 4]



# ---------------------------------------------------------------------------
# Fast GeoTIFF probe
#
# GDAL's open + metadata calls cost ~40 ms per file (the IMAGE_STRUCTURE /
# default metadata dicts alone ~20 ms), which is minutes for a swissALTI3D
# folder of 43 000 tiles. An ordinary GeoTIFF answers every question the
# index asks from its first IFD: one small read, no GDAL. Only the common,
# fully understood case is handled here — one band, a standard sample type,
# a known compression, a single tiepoint or a transformation matrix, an EPSG
# code in the GeoKeys, no sidecar that could override any of it. Everything
# else returns None and goes through the GDAL probe, so unusual files are
# judged exactly as before. ``tests/test_terrain_index_fast.py`` holds the two
# probes to identical answers.

_TIFF_TYPES = {1: ("B", 1), 2: ("B", 1), 3: ("H", 2), 4: ("I", 4), 5: ("I", 8),
               6: ("b", 1), 7: ("B", 1), 8: ("h", 2), 9: ("i", 4), 10: ("i", 8),
               11: ("f", 4), 12: ("d", 8), 16: ("Q", 8), 17: ("q", 8), 18: ("Q", 8)}
_TIFF_DTYPES = {(8, 1): "Byte", (8, 2): "Int8", (16, 1): "UInt16", (16, 2): "Int16",
                (32, 1): "UInt32", (32, 2): "Int32", (32, 3): "Float32",
                (64, 3): "Float64"}
#: TIFF compression code → GDAL's IMAGE_STRUCTURE COMPRESSION ("" = none).
_TIFF_COMPRESSION = {1: "", 5: "LZW", 8: "DEFLATE", 32946: "DEFLATE",
                     32773: "PACKBITS", 50000: "ZSTD", 50001: "WEBP",
                     34925: "LZMA"}
#: Sidecars GDAL reads next to a .tif that can change CRS, georeferencing,
#: nodata or scale — their presence sends the file to the GDAL probe.
_SIDECAR_SUFFIXES = (".aux.xml", ".prj", ".tfw", ".tifw", ".tiffw", ".wld",
                     ".imd", ".rpb")
#: GeoKeys that change an EPSG CRS: an explicit datum shift (TOWGS84).
#: Citations, units and ellipsoid values GDAL writes next to the EPSG code are
#: descriptive — GDAL builds the CRS from the code — so they do not count; a
#: parameter-defined CRS is caught by its code (32767 = user-defined).
_USER_GEOKEYS = {2062}
_GDAL_MD_ITEM = re.compile(
    r'<Item\s+name="(SCALE|OFFSET)"\s+sample="0"\s+role="(?:scale|offset)"\s*>'
    r'\s*([^<]+?)\s*</Item>', re.IGNORECASE)


class _HeadReader:
    """File-like ``seek``/``read`` served from the first 64 KB (one read) —
    an ordinary GeoTIFF's header, IFD and tag arrays all sit there, and each
    separate small read is a round trip on a network or mounted volume."""

    HEAD = 65536

    def __init__(self, fh) -> None:
        self._fh = fh
        self._buf = fh.read(self.HEAD)
        self._pos = 0

    def seek(self, pos: int) -> None:
        self._pos = pos

    def read(self, n: int) -> bytes:
        start, end = self._pos, self._pos + n
        self._pos = end
        if end <= len(self._buf):
            return self._buf[start:end]
        self._fh.seek(start)
        return self._fh.read(n)


def _tiff_ifd0(fh) -> Optional[Tuple[str, Dict[int, Tuple[int, int, bytes, int]]]]:
    """``(byte order, {tag: (type, count, inline bytes, offset)})`` of IFD 0."""
    head = fh.read(16)
    if len(head) < 8 or head[:2] not in (b"II", b"MM"):
        return None
    bo = "<" if head[:2] == b"II" else ">"
    magic = struct.unpack(bo + "H", head[2:4])[0]
    if magic == 42:
        big, ifd = False, struct.unpack(bo + "I", head[4:8])[0]
    elif magic == 43 and len(head) >= 16:
        big, ifd = True, struct.unpack(bo + "Q", head[8:16])[0]
    else:
        return None
    fh.seek(ifd)
    raw = fh.read(8 if big else 2)
    if len(raw) < (8 if big else 2):
        return None
    n = struct.unpack(bo + ("Q" if big else "H"), raw)[0]
    esize = 20 if big else 12
    body = fh.read(n * esize)
    if n <= 0 or n > 4096 or len(body) < n * esize:
        return None
    tags: Dict[int, Tuple[int, int, bytes, int]] = {}
    inline = 8 if big else 4
    for i in range(n):
        e = body[i * esize:(i + 1) * esize]
        if big:
            tag, typ, cnt = struct.unpack(bo + "HHQ", e[:12])
            val = e[12:20]
        else:
            tag, typ, cnt = struct.unpack(bo + "HHI", e[:8])
            val = e[8:12]
        off = struct.unpack(bo + ("Q" if big else "I"), val)[0]
        tags[tag] = (typ, cnt, val, off)
    return bo, tags


def _tiff_values(fh, bo: str, entry: Tuple[int, int, bytes, int], big: bool):
    typ, cnt, val, off = entry
    fmt = _TIFF_TYPES.get(typ)
    if fmt is None or cnt <= 0 or cnt > 50_000_000:
        return None
    code, size = fmt
    nbytes = cnt * size
    if nbytes <= (8 if big else 4):
        data = val[:nbytes]
    else:
        fh.seek(off)
        data = fh.read(nbytes)
        if len(data) < nbytes:
            return None
    if typ == 2:
        return data.split(b"\0", 1)[0].decode("latin-1")
    if typ in (5, 10):
        nums = struct.unpack(bo + code * (2 * cnt), data)
        return [nums[i] / nums[i + 1] if nums[i + 1] else 0.0
                for i in range(0, len(nums), 2)]
    return list(struct.unpack(bo + code * cnt, data))


def _epsg_crs(code: int) -> Optional[Dict[str, Any]]:
    """``_crs_fields`` for an EPSG code — cached per thread (one lookup per CRS)."""
    cache = getattr(_tls, "epsg", None)
    if cache is None:
        cache = _tls.epsg = {}
    if code not in cache:
        srs = osr.SpatialReference()
        try:
            ok = srs.ImportFromEPSG(int(code)) == 0
        except Exception:  # noqa: BLE001
            ok = False
        cache[code] = (_crs_fields(srs) if ok and not srs.IsLocal()
                       and (srs.IsProjected() or srs.IsGeographic()) else None)
    return cache[code]


def _has_sidecar(path: str, siblings: Optional[set]) -> bool:
    """True when a sidecar that can override CRS, georeferencing, nodata or
    scale sits next to *path* (looked up in *siblings*, the scan's lower-cased
    listing, when given)."""
    stem = os.path.splitext(path)[0]
    for suffix in _SIDECAR_SUFFIXES:
        for cand in (path + suffix, stem + suffix):
            if (cand.lower() in siblings) if siblings is not None else os.path.exists(cand):
                return True
    return False


def probe_geotiff_fast(path: str, siblings: Optional[set] = None,
                       file_size: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """The index entry for an ordinary GeoTIFF without GDAL, or None when the
    file needs the GDAL probe (see the section comment). *siblings*: lower-cased
    paths of every file in the folder (from the scan) — sidecars are looked up
    there instead of with a disk call each."""
    if path.startswith("/vsi") or _ext(path) not in (".tif", ".tiff"):
        return None
    if _has_sidecar(path, siblings):
        return None
    try:
        with open(path, "rb") as raw_fh:
            fh = _HeadReader(raw_fh)
            parsed = _tiff_ifd0(fh)
            if parsed is None:
                return None
            bo, tags = parsed
            fh.seek(2)
            big = struct.unpack(bo + "H", fh.read(2))[0] == 43

            def get(tag, default=None):
                ent = tags.get(tag)
                return default if ent is None else _tiff_values(fh, bo, ent, big)

            def one(tag, default=None):
                vals = get(tag)
                return default if not vals else vals[0]

            if one(254, 0) & 1 or one(277, 1) != 1 or 320 in tags or 338 in tags:
                return None
            if one(262, 1) not in (0, 1):
                return None
            xsize, ysize = one(256), one(257)
            dtype = _TIFF_DTYPES.get((one(258, 1), one(339, 1)))
            compression = _TIFF_COMPRESSION.get(one(259, 1))
            if not xsize or not ysize or dtype is None or compression is None:
                return None
            # Truncation, as the GDAL probe tests it: every block is in the file.
            if 324 in tags:
                offs, cnts = get(324), get(325)
            else:
                offs, cnts = get(273), get(279)
            if not offs or not cnts or len(offs) != len(cnts):
                return None
            end = max((o + c for o, c in zip(offs, cnts) if c), default=0)
            fsize = (file_size if file_size is not None
                     else os.fstat(raw_fh.fileno()).st_size)
            if end > fsize:
                return None
            # Georeferencing.
            mt = get(34264)
            tp, sc = get(33922), get(33550)
            if mt and len(mt) >= 16:
                if mt[1] or mt[4] or tp:
                    return None
                gt = [mt[3], mt[0], 0.0, mt[7], 0.0, mt[5]]
            elif tp and len(tp) == 6 and sc and len(sc) >= 2:
                gt = [tp[3] - tp[0] * sc[0], sc[0], 0.0,
                      tp[4] + tp[1] * sc[1], 0.0, -sc[1]]
            else:
                return None
            keys = get(34735)
            if not keys or len(keys) < 4:
                return None
            geokeys: Dict[int, int] = {}
            for i in range(4, 4 + 4 * keys[3], 4):
                if i + 3 >= len(keys):
                    return None
                key, loc, _cnt, value = keys[i:i + 4]
                if loc != 0:
                    if key in (1024, 1025, 2048, 3072):
                        return None
                    geokeys[key] = -1
                else:
                    geokeys[key] = value
            if _USER_GEOKEYS & set(geokeys):
                return None
            model = geokeys.get(1024)
            code = geokeys.get(3072) if model == 1 else geokeys.get(2048) if model == 2 else None
            if not code or code == 32767:
                return None
            if geokeys.get(1025) == 2:      # RasterPixelIsPoint: GDAL's corner shift
                gt[0] -= gt[1] * 0.5
                gt[3] -= gt[5] * 0.5
            nodata_txt = get(42113)
            md = get(42112)
    except (OSError, struct.error, ValueError, TypeError, OverflowError):
        return None
    crs = _epsg_crs(int(code))
    if crs is None:
        return None
    nodata = None
    if isinstance(nodata_txt, str) and nodata_txt.strip():
        try:
            nodata = float(nodata_txt.strip())
        except ValueError:
            return None
    scale, offset = 1.0, 0.0
    if isinstance(md, str) and md:
        for name, value in _GDAL_MD_ITEM.findall(md):
            try:
                if name.upper() == "SCALE":
                    scale = float(value) or 1.0
                else:
                    offset = float(value)
            except ValueError:
                return None
    gt = tuple(float(v) for v in gt)
    if gt == (0.0, 1.0, 0.0, 0.0, 0.0, 1.0):
        return None   # GDAL's "no geotransform" default — let it say so
    geo = _geo_fields(gt, int(xsize), int(ysize), crs)
    if geo is None:
        return None
    entry: Dict[str, Any] = {
        "probe": PROBE_VERSION, "readable": True, "reason": "", "driver": "GTiff",
        "bands": 1, "xsize": int(xsize), "ysize": int(ysize), "dtype": dtype,
        "compression": compression, "nbits": "", "scale": scale, "offset": offset,
        "nodata": nodata, "geotransform": list(gt), "rotated": False,
        "flipped": bool(gt[1] < 0.0 or gt[5] > 0.0),
    }
    entry.update(crs)
    entry.update(geo)
    return entry

#: ``reason`` of a file whose pixels are not all there — GDAL's bottom-right
#: block read failed (probe_file, + GDAL's message) or the converter probe's
#: ``last_chunk_end`` lies past the file / is null (entries_from_probe, + what
#: the probe saw). One text, so both paths classify and word it alike.
TRUNCATED_REASON = "truncated or corrupt — its pixels cannot be read"


def probe_file(path: str, siblings: Optional[set] = None,
               st: Optional[Tuple[int, int]] = None) -> Dict[str, Any]:
    """Everything the index records about one raster (see module doc).

    Never raises: a file that cannot be used comes back with
    ``readable=False`` and a ``reason`` naming what is wrong, in words the
    pre-flight prompt shows as they are. A file with no CRS is readable but
    has ``crs_wkt=""`` (``KIND_NOCRS``: the Map Converter offers to assign
    one; nothing ever inherits another file's CRS silently).
    """
    # *st*: (size, mtime_ns) the folder scan already has — one disk call less.
    if st is None:
        st = _stat(path)
    fast = probe_geotiff_fast(path, siblings, st[0] if st else None)
    if fast is not None:
        if st is not None:
            fast["size"], fast["mtime_ns"] = st
        return fast
    entry: Dict[str, Any] = {"probe": PROBE_VERSION, "readable": False,
                             "reason": ""}
    if st is not None:
        entry["size"], entry["mtime_ns"] = st
    gdal.PushErrorHandler("CPLQuietErrorHandler")
    try:
        gdal.ErrorReset()
        try:
            ds = gdal.Open(path, gdal.GA_ReadOnly)
        except Exception:  # noqa: BLE001 — exceptions mode on in the host
            ds = None
        if ds is None:
            msg = gdal.GetLastErrorMsg().strip()
            entry["reason"] = ("GDAL cannot open it as a raster"
                               + (f" ({msg[:120]})" if msg else ""))
            return entry
        drv = ds.GetDriver()
        entry["driver"] = drv.ShortName if drv is not None else ""
        entry["bands"] = ds.RasterCount
        entry["xsize"], entry["ysize"] = ds.RasterXSize, ds.RasterYSize
        subs = ds.GetSubDatasets() or []
        if ds.RasterCount == 0:
            entry["reason"] = (f"holds {len(subs)} variables/sub-datasets and "
                               "no single elevation grid — export the "
                               "elevation variable to its own file"
                               if subs else "has no raster band")
            return entry
        band = ds.GetRasterBand(1)
        entry["dtype"] = gdal.GetDataTypeName(band.DataType)
        struct = ds.GetMetadata("IMAGE_STRUCTURE") or {}
        entry["compression"] = (struct.get("COMPRESSION") or "").upper()
        entry["nbits"] = (band.GetMetadataItem("NBITS", "IMAGE_STRUCTURE")
                          or "")
        scale = band.GetScale()
        offset = band.GetOffset()
        entry["scale"] = 1.0 if scale in (None, 0) else float(scale)
        entry["offset"] = 0.0 if offset is None else float(offset)
        entry["nodata"] = band.GetNoDataValue()
        interp = band.GetColorInterpretation()
        if ds.RasterCount >= 2:
            entry["reason"] = (f"{ds.RasterCount} bands — an image (RGB, "
                               "hillshade or a rendered map), not an "
                               "elevation raster; elevation is one band")
            return entry
        if interp == gdal.GCI_PaletteIndex or band.GetColorTable() is not None:
            entry["reason"] = ("a paletted (colour-table) image, not an "
                               "elevation raster")
            return entry
        try:
            gt = ds.GetGeoTransform(can_return_null=True)
        except TypeError:
            gt = ds.GetGeoTransform()
        if gt is not None and tuple(gt) == (0.0, 1.0, 0.0, 0.0, 0.0, 1.0):
            gt = None
        if gt is None:
            entry["reason"] = ("no georeferencing (no geotransform) — "
                               "assign one (e.g. gdal_edit.py -a_ullr …); "
                               "georeferencing is never derived from names")
            return entry
        gt = tuple(float(v) for v in gt)
        entry["geotransform"] = list(gt)
        entry["rotated"] = bool(gt[2] != 0.0 or gt[4] != 0.0)
        entry["flipped"] = bool(gt[1] < 0.0 or gt[5] > 0.0)
        # Truncation: GDAL opens a cut-off file from its header alone; the
        # bottom-right block is the last thing written, so reading it is
        # the cheapest test that the pixels really are there.
        bx, by = band.GetBlockSize()
        bx = max(1, min(bx or 1, ds.RasterXSize))
        by = max(1, min(by or 1, ds.RasterYSize))
        gdal.ErrorReset()
        try:
            data = band.ReadRaster(ds.RasterXSize - bx, ds.RasterYSize - by,
                                   bx, by)
        except Exception:  # noqa: BLE001
            data = None
        if data is None or gdal.GetLastErrorType() >= gdal.CE_Failure:
            msg = gdal.GetLastErrorMsg().strip()
            entry["reason"] = (TRUNCATED_REASON
                               + (f" ({msg[:100]})" if msg else ""))
            return entry
        srs = _parse_srs(ds.GetProjection() or "")
        ds = band = None
        entry["readable"] = True
        if srs is None:
            entry.update({"crs_wkt": "", "crs_authid": "", "crs_proj4": "",
                          "geographic": False})
            entry["reason"] = "no coordinate system (CRS)"
            return entry
        crs = _crs_fields(srs)
        entry.update(crs)
        geo = _geo_fields(gt, entry["xsize"], entry["ysize"], crs)
        if geo is None:
            entry["readable"] = False
            entry["reason"] = (f"its CRS ({crs['crs_authid'] or 'custom'}) "
                               "cannot be transformed to WGS84 here")
            return entry
        entry.update(geo)
        return entry
    finally:
        gdal.PopErrorHandler()


# ---------------------------------------------------------------------------
# Converter probe (``aether_converter probe``, schema aether-probe/1)
#
# The in-process probes above are GIL-bound Python per file (43,650 swissALTI3D
# tiles: 84 s). ``aether_converter probe`` (>= 0.2.7, aether-tools CONTRACT
# §1.2a) reads the same header facts natively, in parallel, one JSON line per
# file; every fact of an ``ok: true`` line equals GDAL's value. Entries are
# built from those facts exactly as probe_geotiff_fast builds them: CRS once
# per EPSG code, the WGS84 footprint of ALL files in that CRS in one batched
# TransformPoints (see _geo_fields_many). The probe reads no sidecar, so
# files with a sidecar never go to it. Truncation as GDAL's bottom-right block
# read judges it: ``last_chunk_end`` (toolkit changelog 35 — the end of that
# block) past the file size, or null, gives the entry GDAL gives a truncated
# file (readable False, TRUNCATED_REASON, no CRS/geometry); a line without the
# field (a converter built before it) cannot vouch for compressed pixels, so
# such files go to GDAL; an uncompressed file shorter than its pixels goes to
# GDAL as before. ``ok: false`` lines, unknown codecs/types, CRS-less files and
# anything else this mapping does not vouch for go through probe_file
# unchanged.
# ---------------------------------------------------------------------------

#: The ``schema`` of every probe line.
PROBE_SCHEMA = "aether-probe/1"
#: Fewer new/changed GeoTIFFs than this are read in-process: launching the
#: converter (an AV-scanned exe on Windows) costs more than it saves.
PROBE_MIN_FILES = 64
#: Probe dtype -> (GDAL type name, bytes per sample). i8/u64/i64 are left to
#: GDAL (its Int8 / 64-bit nodata handling differs by version).
_PROBE_DTYPES = {"u8": ("Byte", 1), "u16": ("UInt16", 2), "i16": ("Int16", 2),
                 "u32": ("UInt32", 4), "i32": ("Int32", 4),
                 "f32": ("Float32", 4), "f64": ("Float64", 8)}
_PROBE_NODATA = {"nan": float("nan"), "inf": float("inf"),
                 "-inf": float("-inf")}
#: ``[Probe] N files, K ok, S s, open X ms avg, read Y ms avg`` (stderr).
_PROBE_SUMMARY = re.compile(
    r"\[Probe\]\s+(\d+) files, (\d+) ok, ([0-9.]+)s, "
    r"open ([0-9.]+) ms avg, read ([0-9.]+) ms avg")
#: WGS84 bounds tolerance (degrees, ~2 mm) within which a file's 16-point
#: footprint ring stands in for the 80-point edge sampling of _geo_fields:
#: with ring samples h = 1/4 edge apart, the edge's true extreme lies within
#: |Δ²|/8 of the ring's (Δ² = the ring's largest second difference), and the
#: 80-point extreme lies between the two. Files above it get the 80 points.
_RING_BBOX_EPS_DEG = 2e-8


class ProbeRun(NamedTuple):
    """One ``aether_converter probe`` run."""
    facts: Dict[str, Dict[str, Any]]   # path -> its line (ok or not)
    files: int                         # paths listed
    elapsed_s: float
    open_ms: Optional[float]           # the probe's own averages (stderr)
    read_ms: Optional[float]
    returncode: Optional[int]
    cancelled: bool
    error: str = ""


@contextlib.contextmanager
def _no_gc():
    """Cyclic GC paused while tens of thousands of small dicts/lists are
    built (it re-scans them on every few hundred allocations: building the
    footprints of 43,650 files took 3x longer with it on). Reference counting
    still frees everything; the previous state is restored."""
    was = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if was:
            gc.enable()


def probe_converter() -> Optional[str]:
    """The ``aether_converter`` that has ``probe`` (``--version`` >=
    ``binary_manager.CONVERTER_PROBE_MIN_VERSION``), or None — absent, older
    or unknown means the in-process probes, unchanged. Never raises."""
    try:
        from . import binary_manager as bm   # deferred: QGIS imports
        exe = bm.find_binary("aether_converter")
        version = bm.probe_converter_version(exe)
        if version is None or bm.compare_versions(
                version, bm.CONVERTER_PROBE_MIN_VERSION) < 0:
            return None
        return exe
    except Exception:  # noqa: BLE001 — no converter: in-process probes
        return None


def _probe_candidate(path: str, siblings: Optional[set]) -> bool:
    """True when *path* goes to the converter probe: a plain .tif/.tiff with
    no sidecar (the probe reads none), listable on one UTF-8 line."""
    if path.startswith("/vsi") or _ext(path) not in (".tif", ".tiff"):
        return False
    if "\n" in path or "\r" in path or not path.strip():
        return False
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not _has_sidecar(path, siblings)


def _probe_command(exe: str, list_path: str) -> List[str]:
    return [exe, "probe", "--list", list_path]


def _kill(proc: Any) -> None:
    try:
        proc.kill()
    except Exception:  # noqa: BLE001 — already gone
        pass


def run_probe(exe: str, paths: List[str],
              progress: Optional[Callable[[int, int], None]] = None,
              should_cancel: Optional[Callable[[], bool]] = None) -> ProbeRun:
    """Run ``aether_converter probe --list`` ONCE over *paths*.

    Lines arrive in list order and are parsed as they come (overlapping the
    converter's own I/O). *progress(done, total)* every 256 lines. Cancel:
    *should_cancel* is polled every 0.1 s by a watcher that kills the process;
    the lines read until then are kept (``cancelled`` True). A line whose
    ``path`` is not the listed one, or that does not parse, is dropped (the
    caller probes that file in-process). Never raises."""
    t0 = time.perf_counter()
    facts: Dict[str, Dict[str, Any]] = {}
    rc: Optional[int] = None
    err_text = ""
    error = ""
    cancelled = False
    n = len(paths)
    fd, list_path = tempfile.mkstemp(prefix="waveshed_probe_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(paths) + "\n")
        # stderr to a file: one summary line, but a panic must not fill a
        # pipe nobody reads while stdout is being drained.
        with tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(
                _probe_command(exe, list_path),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                creationflags=_SUBPROCESS_FLAGS)
            stop = threading.Event()
            hit = threading.Event()

            def watch() -> None:
                while not stop.wait(0.1):
                    if should_cancel is not None and should_cancel():
                        hit.set()
                        _kill(proc)
                        return

            watcher = threading.Thread(target=watch, name="probe-cancel",
                                       daemon=True)
            watcher.start()
            try:
                i = 0
                loads = json.loads
                with _no_gc():
                    for line in proc.stdout:
                        if i < n:
                            try:
                                f = loads(line)
                            except ValueError:
                                f = None
                            if isinstance(f, dict) and f.get("path") == paths[i]:
                                facts[paths[i]] = f
                        i += 1
                        if progress is not None and i % 256 == 0:
                            progress(min(i, n), n)
                rc = proc.wait()
            finally:
                stop.set()
                if proc.poll() is None:
                    _kill(proc)
                    proc.wait()
                watcher.join()
            cancelled = hit.is_set() or bool(
                should_cancel is not None and should_cancel())
            err.seek(0)
            err_text = err.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 — the in-process probes take over
        error = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass
    m = _PROBE_SUMMARY.search(err_text)
    if not error and rc not in (0, None) and not cancelled:
        error = f"exit {rc}: " + (err_text.strip().splitlines() or [""])[-1][:200]
    return ProbeRun(facts=facts, files=n, elapsed_s=time.perf_counter() - t0,
                    open_ms=float(m.group(4)) if m else None,
                    read_ms=float(m.group(5)) if m else None,
                    returncode=rc, cancelled=cancelled, error=error)


def _probe_base(f: Dict[str, Any]
                ) -> Optional[Tuple[Dict[str, Any], int, Tuple[float, ...], int, int]]:
    """``(entry without CRS/geometry, epsg, geotransform, xsize, ysize)`` of one
    probe line — the fields probe_geotiff_fast records for that file — or None
    when the line is not one this shortcut vouches for (GDAL decides)."""
    if f.get("ok") is not True or f.get("schema") != PROBE_SCHEMA:
        return None
    try:
        code = f["epsg"]
        if type(code) is not int or code <= 0 or code == 32767:
            return None
        dt = _PROBE_DTYPES.get(f["dtype"])
        comp = _TIFF_COMPRESSION.get(f["compression"]["code"])
        if dt is None or comp is None or f["bands"] != 1 or f.get("proj") is not None:
            return None
        w, h = int(f["width"]), int(f["height"])
        gt = tuple(map(float, f["geotransform"]))
        if (w <= 0 or h <= 0 or len(gt) != 6
                or not math.isfinite(sum(gt))      # NaN/inf anywhere
                or gt == (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)):   # GDAL: "no geotransform"
            return None
        if comp == "":
            # Uncompressed blocks have fixed sizes: a file shorter than its
            # pixels is cut off (or sparse) — GDAL's block read judges it.
            size = f.get("size")
            if f.get("tiled"):
                tw, th = int(f["tile_w"]), int(f["tile_h"])
                need = -(-w // tw) * -(-h // th) * tw * th * dt[1]
            else:
                need = w * h * dt[1]
            if type(size) is not int or size < need:
                return None
        # Where the pixel data ends (the bottom-right block GDAL reads):
        # past the file or unreadable = truncated, GDAL's verdict.
        cut = ""
        if "last_chunk_end" in f:
            end, size = f["last_chunk_end"], f.get("size")
            if type(size) is not int:
                return None
            if end is None:
                cut = "its block offsets cannot be read"
            elif type(end) is not int or end < 0:
                return None
            elif end > size:
                cut = f"its pixel data ends at byte {end:,}, the file has {size:,}"
        elif comp != "":
            return None     # a pre-changelog-35 converter: GDAL judges
        nodata = f.get("nodata")
        if isinstance(nodata, str):
            if nodata not in _PROBE_NODATA:
                return None
            nodata = _PROBE_NODATA[nodata]
        elif nodata is not None:
            nodata = float(nodata)
        scale = f.get("scale")
        scale = 1.0 if scale in (None, 0) else float(scale)
        offset = f.get("offset")
        offset = 0.0 if offset is None else float(offset)
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    entry: Dict[str, Any] = {
        "probe": PROBE_VERSION, "readable": True, "reason": "", "driver": "GTiff",
        "bands": 1, "xsize": w, "ysize": h, "dtype": dt[0],
        "compression": comp, "nbits": "", "scale": scale, "offset": offset,
        "nodata": nodata, "geotransform": list(gt),
        "rotated": bool(gt[2] != 0.0 or gt[4] != 0.0),
        "flipped": bool(gt[1] < 0.0 or gt[5] > 0.0),
    }
    if cut:
        entry["readable"] = False
        entry["reason"] = f"{TRUNCATED_REASON} ({cut})"
    return entry, code, gt, w, h


def _densify_np(cx: np.ndarray, cy: np.ndarray, per_edge: int
                ) -> Tuple[np.ndarray, np.ndarray]:
    """:func:`_densify_ring` for many rings at once (rows of 4 corners) —
    the same float operations in the same order, so the same points."""
    f = np.arange(per_edge, dtype=np.float64) / per_edge
    nx, ny = np.roll(cx, -1, axis=1), np.roll(cy, -1, axis=1)
    rx = cx[:, :, None] + (nx - cx)[:, :, None] * f
    ry = cy[:, :, None] + (ny - cy)[:, :, None] * f
    return rx.reshape(len(cx), -1), ry.reshape(len(cy), -1)


def _transform_np(ct: Any, xs: np.ndarray, ys: np.ndarray
                  ) -> Tuple[np.ndarray, np.ndarray]:
    """ONE batched TransformPoints of every point (non-finite = failed).
    Each distinct point is transformed once — neighbouring tiles of a grid
    share corners and edges (43,650 swissALTI3D rings: 698k points, 307k
    distinct) — and the same input gives the same output."""
    c = np.empty(xs.size, dtype=np.complex128)
    c.real, c.imag = xs.ravel(), ys.ravel()
    uniq, inv = np.unique(c, return_inverse=True)
    pts = np.column_stack([uniq.real, uniq.imag])
    try:
        res = ct.TransformPoints(pts)
    except Exception:  # noqa: BLE001 — bindings without numpy input
        res = ct.TransformPoints(pts.tolist())
    width = len(res[0]) if len(res) else 3
    out = np.fromiter(itertools.chain.from_iterable(res), dtype=np.float64,
                      count=width * len(res)).reshape(len(pts), width)
    inv = inv.reshape(-1)
    return out[inv, 0].reshape(xs.shape), out[inv, 1].reshape(xs.shape)


def _round7(a: np.ndarray) -> np.ndarray:
    """``round(x, 7)`` of every element, exactly as Python rounds: numpy's
    scaled rounding, redone with :func:`round` for the values whose scaled
    fraction lies within 1e-6 of a tie (the only ones it can get wrong)."""
    out = np.round(a, 7)
    with np.errstate(invalid="ignore"):
        frac = np.abs(np.modf(a * 1e7)[0])
        risky = np.nonzero(np.abs(frac - 0.5) < 1e-6)[0] if a.ndim == 1 else \
            np.nonzero((np.abs(frac - 0.5) < 1e-6).reshape(-1))[0]
    if risky.size:
        flat, src = out.reshape(-1), a.reshape(-1)
        for i in risky.tolist():
            flat[i] = round(float(src[i]), 7)
    return out


def _geo_fields_many(items: List[Tuple[Tuple[float, ...], int, int]],
                     crs: Dict[str, Any]) -> List[Optional[Dict[str, Any]]]:
    """:func:`_geo_fields` of many rasters in ONE CRS (``(gt, xsize, ysize)``
    each), with one batched TransformPoints for every file's 16-point
    footprint ring — not one call per file — plus one for the 80-point edge
    sampling of the files whose ring cannot stand in for it
    (:data:`_RING_BBOX_EPS_DEG`). Ring, footprint, native bounds and pixel
    sizes are identical to :func:`_geo_fields`; WGS84 bounds identical, or
    within that tolerance."""
    n = len(items)
    if n == 0:
        return []
    gt = np.array([it[0] for it in items], dtype=np.float64)
    xs = np.array([it[1] for it in items], dtype=np.float64)
    ys = np.array([it[2] for it in items], dtype=np.float64)
    zero = np.zeros(n)
    px = np.stack([zero, xs, xs, zero], axis=1)
    py = np.stack([zero, zero, ys, ys], axis=1)
    cx = gt[:, 0:1] + gt[:, 1:2] * px + gt[:, 2:3] * py      # _corners
    cy = gt[:, 3:4] + gt[:, 4:5] * px + gt[:, 5:6] * py
    rx, ry = _densify_np(cx, cy, 4)
    ok = np.ones(n, dtype=bool)
    single: List[int] = []           # rows computed file by file instead
    if crs["crs_authid"] == "EPSG:4326":
        lon, lat = rx, ry
        west, east = cx.min(axis=1), cx.max(axis=1)
        south, north = cy.min(axis=1), cy.max(axis=1)
    else:
        ct = _transform(crs["crs_wkt"])
        if ct is None:
            return [None] * n
        try:
            lon, lat = _transform_np(ct, rx, ry)
        except Exception:  # noqa: BLE001 — file by file, as _geo_fields does
            return [_geo_fields(tuple(it[0]), it[1], it[2], crs) for it in items]
        ok = (np.isfinite(lon) & np.isfinite(lat)).all(axis=1)
        west, east = lon.min(axis=1), lon.max(axis=1)
        south, north = lat.min(axis=1), lat.max(axis=1)
        # Edge samples s0..s4 (4 ring points + the next corner) per edge.
        idx = np.array([[e * 4 + j for j in range(4)] + [((e + 1) % 4) * 4]
                        for e in range(4)])
        with np.errstate(invalid="ignore"):
            bound = np.zeros(n)
            for v in (lon, lat):
                s = v[:, idx]
                d2 = np.abs(s[:, :, :-2] - 2.0 * s[:, :, 1:-1] + s[:, :, 2:])
                bound = np.maximum(bound, d2.reshape(n, -1).max(axis=1) / 8.0)
        dense_rows = np.nonzero(ok & ~(bound <= _RING_BBOX_EPS_DEG))[0]
        dlon = None
        if dense_rows.size:
            dx, dy = _densify_np(cx[dense_rows], cy[dense_rows], 20)
            try:
                dlon, dlat = _transform_np(ct, dx, dy)
            except Exception:  # noqa: BLE001 — file by file, as _geo_fields
                single = dense_rows.tolist()
        if dlon is not None:
            fin = np.isfinite(dlon) & np.isfinite(dlat)
            ok[dense_rows] &= fin.sum(axis=1) >= 4
            # Ring points are among the dense ones (same inputs), so this is
            # _bounds(dense + ring).
            west[dense_rows] = np.minimum(
                np.where(fin, dlon, np.inf).min(axis=1), west[dense_rows])
            east[dense_rows] = np.maximum(
                np.where(fin, dlon, -np.inf).max(axis=1), east[dense_rows])
            south[dense_rows] = np.minimum(
                np.where(fin, dlat, np.inf).min(axis=1), south[dense_rows])
            north[dense_rows] = np.maximum(
                np.where(fin, dlat, -np.inf).max(axis=1), north[dense_rows])
    unit = _crs_unit(crs["crs_wkt"], crs["geographic"])
    geographic = crs["geographic"]
    is_wgs = crs["crs_authid"] == "EPSG:4326"
    cxl, cyl = cx.tolist(), cy.tolist()
    with np.errstate(invalid="ignore"):
        fps = np.stack([_round7(lon), _round7(lat)], axis=-1).tolist()
    wl, el, sl, nl = west.tolist(), east.tolist(), south.tolist(), north.tolist()
    okl = ok.tolist()
    single_set = set(single)
    out: List[Optional[Dict[str, Any]]] = []
    for i in range(n):
        if i in single_set:
            out.append(_geo_fields(tuple(items[i][0]), items[i][1],
                                   items[i][2], crs))
            continue
        if not okl[i]:
            out.append(None)
            continue
        cxi, cyi = cxl[i], cyl[i]
        native = {"west": min(cxi), "east": max(cxi),
                  "south": min(cyi), "north": max(cyi)}
        wgs = (dict(native) if is_wgs else
               {"west": wl[i], "east": el[i], "south": sl[i], "north": nl[i]})
        out.append(_geo_result(items[i][0], native, wgs, (), unit,
                               geographic, footprint=fps[i]))
    return out


def entries_from_probe(facts: Dict[str, Dict[str, Any]]
                       ) -> Dict[str, Dict[str, Any]]:
    """Index entries (without size/mtime) for the probe lines in *facts* that
    the shortcut covers — the entry probe_geotiff_fast / the GDAL probe gives
    the same file. Files missing from the result go through probe_file."""
    with _no_gc():
        groups: Dict[int, List[Tuple[str, Dict[str, Any], Tuple[float, ...], int, int]]] = {}
        out: Dict[str, Dict[str, Any]] = {}
        for path, f in facts.items():
            base = _probe_base(f)
            if base is not None:
                entry, code, gt, w, h = base
                if not entry["readable"]:
                    # Truncated: GDAL's probe stops there too, before CRS.
                    out[path] = entry
                    continue
                groups.setdefault(code, []).append((path, entry, gt, w, h))
        for code, members in groups.items():
            crs = _epsg_crs(code)
            if crs is None:
                continue
            geos = _geo_fields_many([(m[2], m[3], m[4]) for m in members], crs)
            for (path, entry, _gt, _w, _h), geo in zip(members, geos):
                if geo is None:
                    continue
                entry.update(crs)
                entry.update(geo)
                out[path] = entry
        return out


def crs_is_usable(crs_text: str) -> bool:
    """True when *crs_text* (authid / WKT / proj string) is a horizontal CRS."""
    srs = osr.SpatialReference()
    try:
        if srs.SetFromUserInput(crs_text) != 0:
            return False
    except Exception:  # noqa: BLE001
        return False
    return bool(not srs.IsLocal() and (srs.IsProjected() or srs.IsGeographic()))


def with_crs(entry: Dict[str, Any], crs_text: str) -> Optional[Dict[str, Any]]:
    """*entry* (a CRS-less file) georeferenced in *crs_text* (an authid,
    WKT or proj string the user assigned). None when *crs_text* is unusable
    or the geometry cannot be transformed."""
    srs = osr.SpatialReference()
    try:
        if srs.SetFromUserInput(crs_text) != 0:
            return None
    except Exception:  # noqa: BLE001
        return None
    if srs.IsLocal() or not (srs.IsProjected() or srs.IsGeographic()):
        return None
    out = dict(entry)
    crs = _crs_fields(srs)
    out.update(crs)
    gt = entry.get("geotransform")
    if not gt:
        return None
    if crs["geographic"]:
        nb = _bounds(_corners(tuple(gt), entry["xsize"], entry["ysize"]))
        if (nb["west"] < -180.5 or nb["east"] > 360.5
                or nb["south"] < -90.5 or nb["north"] > 90.5):
            return None            # projected coordinates read as degrees
    geo = _geo_fields(tuple(gt), entry["xsize"], entry["ysize"], crs)
    if geo is None:
        return None
    w = geo["wgs84_bounds"]
    if not (-180.5 <= w["west"] <= w["east"] <= 360.5
            and -90.5 <= w["south"] <= w["north"] <= 90.5):
        return None
    out.update(geo)
    out["reason"] = ""
    out["crs_assigned"] = crs_text
    return out


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def problem_of(entry: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """``(kind, reason)`` when *entry* cannot be used as is, else None."""
    if not entry.get("readable"):
        return KIND_UNUSABLE, entry.get("reason") or "unreadable"
    if not entry.get("crs_wkt"):
        return KIND_NOCRS, entry.get("reason") or "no coordinate system (CRS)"
    if entry.get("dtype") == "Byte":
        return KIND_SUSPECT, ("8-bit single band — a picture, not "
                              "elevation? (its values 0-255 would be read "
                              "as metres)")
    return None


def materialize_reason(entry: Dict[str, Any], path: str) -> Optional[str]:
    """Why *path* must be window-copied by GDAL before the converter can read
    it, or None when it is handed over as is. Decided by the file's own
    metadata (driver, compression, geotransform, scale), not by extension."""
    if path.startswith("/vsi"):
        return "virtual path"
    if entry.get("driver") and entry["driver"] != "GTiff":
        return f"{entry['driver']} format"
    comp = (entry.get("compression") or "").upper()
    if comp not in CONVERTER_COMPRESSIONS:
        return f"{comp} compression"
    if entry.get("rotated") or entry.get("flipped"):
        return "rotated/flipped geotransform"
    if needs_unscale(entry):
        return "scale/offset"
    nbits = str(entry.get("nbits") or "")
    if nbits and nbits not in ("8", "16", "32", "64"):
        return f"{nbits}-bit samples"
    if (entry.get("size") or 0) > MATERIALIZE_MAX_TIFF_BYTES:
        return "larger than 512 MB"
    return None


def needs_unscale(entry: Dict[str, Any]) -> bool:
    return (abs(float(entry.get("scale", 1.0)) - 1.0) > 1e-12
            or abs(float(entry.get("offset", 0.0))) > 1e-12)


def crs_arg(entry: Dict[str, Any]) -> Optional[str]:
    """The ``crs`` the ingest job states for this file: ``EPSG:n`` when it has
    one, else its proj string (a USER:/custom CRS used to abort the run —
    the converter's GeoKey reader only knows EPSG codes)."""
    if entry.get("crs_authid", "").startswith("EPSG:"):
        return entry["crs_authid"]
    proj = entry.get("crs_proj4") or ""
    return proj if proj.startswith("+") else None


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------

def _norm_folder(folder: str) -> str:
    return os.path.normcase(os.path.abspath(folder))


def folder_key(folder: str) -> str:
    return hashes.md5(_norm_folder(folder).encode("utf-8")).hexdigest()


def index_dir() -> str:
    """``<cache>/index`` — where every folder's index file lives
    (``terrain_adapter.clear_index`` empties it)."""
    from .terrain_adapter import get_cache_dir  # deferred: circular import
    return os.path.join(get_cache_dir(), "index")


def index_path(folder: str) -> str:
    return os.path.join(index_dir(), folder_key(folder) + ".json")


#: Long per-file strings that are the same for every file in one CRS.
_SHARED_FIELDS = ("crs_wkt", "crs_proj4")


class FolderIndex:
    """The footprint index of one folder. Use :func:`get_index`."""

    def __init__(self, folder: str) -> None:
        self.folder = os.path.abspath(folder)
        self.entries: Dict[str, Dict[str, Any]] = {}
        self.archives: Dict[str, Dict[str, Any]] = {}
        self.refreshed_at = 0.0
        self.last_stats: Optional[RefreshStats] = None
        #: Bumped whenever a refresh changes :attr:`entries` (new, changed or
        #: removed files) — with :attr:`dir_mtime_ns` the signature a
        #: run's ``terrain_adapter.TerrainPrep`` is checked against.
        self.generation = 0
        #: The folder's own mtime read right before the listing of the last
        #: completed refresh (None: no refresh yet, or not stat-able).
        self.dir_mtime_ns: Optional[int] = None
        self.lock = threading.RLock()
        self._loaded = False

    # -- persistence -------------------------------------------------------
    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(index_path(self.folder), "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError):
            return
        stored = doc.get("folder") or ""
        if (doc.get("schema") != INDEX_SCHEMA
                or _norm_folder(stored) != _norm_folder(self.folder)):
            return
        # The entries are keyed by full paths in the spelling the index was
        # saved with. On Windows another spelling of the same folder (letter
        # case: normcase gives both one index) would list every file under
        # new keys — all "new", the whole folder probed again — so the
        # listing keeps the stored spelling.
        if stored != self.folder:
            self.folder = stored
        # CRS strings are stored once per distinct CRS (a 40k-file folder
        # is one or two CRSs; inline WKT was ~1.5 KB per file).
        table = doc.get("crs") or {}
        entries = doc.get("entries") or {}
        for e in entries.values():
            for field in _SHARED_FIELDS:
                ref = e.get(field)
                if isinstance(ref, str) and ref.startswith("@"):
                    e[field] = table.get(ref[1:], "")
        self.entries = entries
        self.archives = doc.get("archives") or {}

    def save(self) -> None:
        path = index_path(self.folder)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        table: Dict[str, str] = {}
        ids: Dict[str, str] = {}
        entries = {}
        for key, e in self.entries.items():
            out = dict(e)
            for field in _SHARED_FIELDS:
                val = out.get(field)
                if isinstance(val, str) and len(val) > 24:
                    ref = ids.get(val)
                    if ref is None:
                        ref = ids[val] = str(len(ids))
                        table[ref] = val
                    out[field] = "@" + ref
            entries[key] = out
        doc = {"schema": INDEX_SCHEMA, "folder": self.folder,
               "saved": time.time(), "crs": table, "entries": entries,
               "archives": self.archives}
        fd, tmp = tempfile.mkstemp(prefix=".idx_", dir=os.path.dirname(path))
        try:
            # One write: json.dump streams ~200 tiny writes per file, which
            # made saving a 2,500-file index take 2.8 s on a slow volume.
            text = json.dumps(doc, separators=(",", ":"))
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            # Windows refuses the rename while another process holds either
            # file open (Defender scanning the index just written, a second
            # QGIS reading it) and lets go a moment later — retried like
            # every pool tile.
            from .terrain_adapter import replace_with_retry  # deferred: circular
            replace_with_retry(tmp, path)
        except OSError as exc:
            try:
                os.remove(tmp)
            except OSError:
                pass
            # Never silent: an index that is not saved costs the next
            # session a read of every file again.
            _log(f"folder index {self.folder}: could not be saved to {path} "
                 f"({exc}) — the next session reads every new or changed "
                 "file again")

    # -- refresh -----------------------------------------------------------
    def refresh(self, progress: Optional[Callable[[int, int, str], None]] = None,
                should_cancel: Optional[Callable[[], bool]] = None,
                max_age_s: float = 0.0,
                workers: int = SCAN_WORKERS) -> RefreshStats:
        """Bring the index up to date with the folder (see module doc)."""
        with self.lock:
            self.load()
            if (max_age_s > 0 and self.last_stats is not None
                    and time.time() - self.refreshed_at < max_age_s):
                return self.last_stats._replace(reused=True)
            t0 = time.perf_counter()

            def check() -> None:
                if should_cancel is not None and should_cancel():
                    raise ScanCancelled()

            def tick(done: int, total: int, phase: str) -> None:
                if progress is not None:
                    try:
                        progress(done, total, phase)
                    except Exception:  # noqa: BLE001 — progress never aborts
                        pass

            tick(0, 0, "listing")
            # Before the listing: an entry added or removed while it runs
            # leaves the folder's mtime different from this value.
            folder_mtime = dir_mtime_ns(self.folder)
            files, archives, stats, names = _walk_stat(self.folder, workers)
            check()
            tick(len(stats), len(stats), "checking")
            t_stat = time.perf_counter() - t0

            wanted: Dict[str, Tuple[int, int]] = {}
            for p in files:
                if stats.get(p) is not None:
                    wanted[p] = stats[p]
            new_archives: Dict[str, Dict[str, Any]] = {}
            for a in archives:
                st = stats.get(a)
                if st is None:
                    continue
                old = self.archives.get(a)
                if old and (old.get("size"), old.get("mtime_ns")) == st:
                    members = old.get("members") or []
                else:
                    members = archive_members(a)
                new_archives[a] = {"size": st[0], "mtime_ns": st[1],
                                   "members": members}
                for m in members:
                    wanted[m] = st

            to_probe = []
            unchanged = new = changed = 0
            for p, st in wanted.items():
                old = self.entries.get(p)
                if old is None:
                    new += 1
                    to_probe.append(p)
                elif ((old.get("size"), old.get("mtime_ns")) != st
                      or old.get("probe") != PROBE_VERSION):
                    changed += 1
                    to_probe.append(p)
                else:
                    unchanged += 1
            removed = [p for p in self.entries if p not in wanted]
            if to_probe or removed:
                # Before the entries change — also when Cancel stops the
                # probe half-way (what was read is kept).
                self.generation += 1
            for p in removed:
                del self.entries[p]
            check()

            t1 = time.perf_counter()
            engine = ""
            open_ms: Optional[float] = None
            rest = to_probe
            if to_probe:
                to_probe.sort()
                rest = to_probe
                cands = [p for p in to_probe if _probe_candidate(p, names)]
                exe = probe_converter() if len(cands) >= PROBE_MIN_FILES else None
                if exe is not None:
                    got, open_ms, cancelled = self._probe_with_converter(
                        exe, cands, wanted, tick, should_cancel)
                    for p, ent in got.items():
                        self.entries[p] = ent
                    if cancelled:
                        self.archives.update(new_archives)
                        self.save()
                        raise ScanCancelled()
                    if got:
                        engine = "converter"
                        rest = [p for p in to_probe if p not in got]
                if rest:
                    # The bulk read starts (the GUI's cold-scan note keys on
                    # this tick, not on the first finished file).
                    tick(0, len(rest), "reading")
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    probe = lambda path: probe_file(path, names, wanted[path])  # noqa: E731
                    for i, (p, ent) in enumerate(zip(rest,
                                                     ex.map(probe, rest))):
                        st = wanted[p]
                        ent["size"], ent["mtime_ns"] = st
                        self.entries[p] = ent
                        if i % 25 == 0:
                            tick(i, len(rest), "reading")
                            if should_cancel is not None and should_cancel():
                                try:
                                    ex.shutdown(wait=False, cancel_futures=True)
                                except TypeError:
                                    # Python 3.8 has no cancel_futures: the
                                    # pending probes finish (slower, correct).
                                    ex.shutdown(wait=True)
                                # Keep what was probed: the next refresh
                                # only probes the rest.
                                self.archives.update(new_archives)
                                self.save()
                                raise ScanCancelled()
            t_probe = time.perf_counter() - t1
            self.archives = new_archives
            if to_probe or removed or not os.path.exists(index_path(self.folder)):
                self.save()
            tick(len(to_probe), len(to_probe), "done")
            self.dir_mtime_ns = folder_mtime
            self.refreshed_at = time.time()
            self.last_stats = RefreshStats(
                files=len(wanted), new=new, changed=changed,
                removed=len(removed), unchanged=unchanged,
                stat_s=t_stat, probe_s=t_probe,
                total_s=time.perf_counter() - t0,
                probe_engine=engine, open_ms=open_ms)
            return self.last_stats

    def _probe_with_converter(self, exe: str, paths: List[str],
                              wanted: Dict[str, Tuple[int, int]],
                              tick: Callable[[int, int, str], None],
                              should_cancel: Optional[Callable[[], bool]],
                              ) -> Tuple[Dict[str, Dict[str, Any]],
                                         Optional[float], bool]:
        """``(entries, mean open ms, cancelled)`` for *paths* from ONE
        ``aether_converter probe`` run (see :func:`run_probe`). Only files
        whose probe line matches the scan's size+mtime and that
        :func:`entries_from_probe` covers get an entry; the caller probes the
        rest in-process. On cancel the lines read so far still become
        entries (the next refresh probes only the rest)."""
        # The bulk read starts: the probe's own ticks come every 256 lines.
        tick(0, len(paths), "reading")
        run = run_probe(exe, paths,
                        progress=lambda d, t: tick(d, t, "reading"),
                        should_cancel=should_cancel)
        if run.error:
            _log(f"folder index {self.folder}: converter probe failed "
                 f"({run.error}) — reading the files in-process")
        tick(len(paths), len(paths), "indexing")
        t_build = time.perf_counter()
        # A file that changed between the listing and the probe goes to the
        # in-process probe (the entry is bound to the listing's size+mtime).
        facts = {p: f for p, f in run.facts.items()
                 if (f.get("size"), f.get("mtime_ns")) == wanted.get(p)}
        got = entries_from_probe(facts)
        for p, ent in got.items():
            ent["size"], ent["mtime_ns"] = wanted[p]
        t_build = time.perf_counter() - t_build
        open_txt = f"{run.open_ms:.2f}" if run.open_ms is not None else "?"
        read_txt = f"{run.read_ms:.2f}" if run.read_ms is not None else "?"
        _log(f"folder index {self.folder} probe: {len(run.facts)} files in "
             f"{run.elapsed_s:.1f} s (open {open_txt} ms, read {read_txt} ms "
             f"avg), entries built in {t_build:.2f} s; "
             f"{len(paths) - len(got)} left to the in-process probe"
             + (" — cancelled" if run.cancelled else ""))
        return got, run.open_ms, run.cancelled

    # -- queries -----------------------------------------------------------
    def paths(self) -> List[str]:
        return sorted(self.entries)

    def problems(self, crs_overrides: Optional[Dict[str, str]] = None
                 ) -> List[Tuple[str, str, str]]:
        """``[(path, kind, reason)]`` for every file that cannot be used as
        is — sorted by path. A CRS-less file with an override is fine."""
        crs_overrides = crs_overrides or {}
        out = []
        for p in sorted(self.entries, key=name_key):
            prob = problem_of(self.entries[p])
            if prob is None:
                continue
            if prob[0] == KIND_NOCRS and p in crs_overrides:
                continue
            out.append((p, prob[0], prob[1]))
        return out

    def fingerprint(self, path: str) -> str:
        e = self.entries.get(path) or {}
        return f"{e.get('size')}:{e.get('mtime_ns')}"


_INDEXES: Dict[str, FolderIndex] = {}
_INDEXES_LOCK = threading.Lock()


def get_index(folder: str) -> FolderIndex:
    """The (process-wide, shared) index object for *folder*."""
    key = _norm_folder(folder) + "|" + index_path(folder)
    with _INDEXES_LOCK:
        idx = _INDEXES.get(key)
        if idx is None:
            idx = _INDEXES[key] = FolderIndex(folder)
        return idx


def expire_all() -> None:
    """Make the next refresh of every index re-check the folder (one stat
    per file) — called when a run starts, so in-run reuse never spans runs."""
    with _INDEXES_LOCK:
        for idx in _INDEXES.values():
            idx.refreshed_at = 0.0


def forget_indexes() -> None:
    """Drop every in-memory index (tests; a cache-dir change)."""
    with _INDEXES_LOCK:
        _INDEXES.clear()
    with _VOID_LOCK:
        _VOID_CACHE.clear()


# ---------------------------------------------------------------------------
# Decisions about problem files (pre-flight answers)
# ---------------------------------------------------------------------------

DECISION_SKIP = "skip"
DECISION_USE = "use"            # suspect (8-bit) files the user kept

_APPROVED: Dict[str, Dict[str, Tuple[str, str]]] = {}
_APPROVED_LOCK = threading.Lock()


def approve(folder: str, decisions: Dict[str, str],
            index: Optional[FolderIndex] = None) -> None:
    """Record the user's pre-flight answer for *folder*: ``{path: "skip" |
    "use"}``. Bound to each file's size+mtime, so a file that changes later
    must be confirmed again."""
    index = index or get_index(folder)
    with _APPROVED_LOCK:
        book = _APPROVED.setdefault(_norm_folder(folder), {})
        for p, d in decisions.items():
            book[p] = (d, index.fingerprint(p))


def approved_decisions(folder: str, index: Optional[FolderIndex] = None
                       ) -> Dict[str, str]:
    """The still-valid recorded answers for *folder* (see :func:`approve`)."""
    index = index or get_index(folder)
    with _APPROVED_LOCK:
        book = dict(_APPROVED.get(_norm_folder(folder), {}))
    return {p: d for p, (d, fp) in book.items()
            if p in index.entries and index.fingerprint(p) == fp}


def clear_approvals() -> None:
    with _APPROVED_LOCK:
        _APPROVED.clear()


def unresolved_problems(index: FolderIndex,
                        decisions: Optional[Dict[str, str]] = None,
                        crs_overrides: Optional[Dict[str, str]] = None
                        ) -> List[Tuple[str, str, str]]:
    """Problems the user has not answered (a run must refuse these)."""
    decisions = decisions or {}
    out = []
    for p, kind, reason in index.problems(crs_overrides):
        d = decisions.get(p)
        if d == DECISION_SKIP:
            continue
        if d == DECISION_USE and kind == KIND_SUSPECT:
            continue
        out.append((p, kind, reason))
    return out


def format_problems(problems: List[Tuple[str, str, str]], folder: str,
                    limit: int = 12) -> str:
    """One line per file (relative path — reason), capped at *limit*."""
    lines = []
    for p, _kind, reason in problems[:limit]:
        lines.append(f"• {display_path(p, folder)} — {reason}")
    if len(problems) > limit:
        lines.append(f"• … and {len(problems) - limit} more (see the "
                     "Waveshed-Terrain log)")
    return "\n".join(lines)


def display_path(path: str, folder: str) -> str:
    real = path
    if path.startswith("/vsizip/"):
        real = path[len("/vsizip/"):]
    try:
        rel = os.path.relpath(real, folder)
    except ValueError:
        return path
    return path if rel.startswith("..") else rel


# ---------------------------------------------------------------------------
# Source infos (what build_tile_sources consumes), in priority order
# ---------------------------------------------------------------------------

def entry_info(path: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    """The ``build_tile_sources`` info dict for one usable index entry."""
    return {
        "path": path,
        "crs": None,                       # QGIS object not needed (osr)
        "crs_wkt": entry.get("crs_wkt", ""),
        "crs_authid": entry.get("crs_authid", ""),
        "crs_arg": crs_arg(entry),
        "native_bounds": entry["native_bounds"],
        "wgs84_bounds": entry["wgs84_bounds"],
        "footprint": entry.get("footprint"),
        "halo_deg": SOURCE_HALO_PX * float(entry.get("px_m", 1.0)) / 111_111.0,
        "px_m": float(entry.get("px_m", 0.0)),
        "px_x": entry.get("px_x"), "px_y": entry.get("px_y"),
        "materialize": materialize_reason(entry, path),
        "unscale": needs_unscale(entry),
        "rotated": bool(entry.get("rotated") or entry.get("flipped")),
        "nodata": entry.get("nodata"),
        "fp": f"{entry.get('size')}:{entry.get('mtime_ns')}",
        # Lazily known (ensure_void_flags): None until a run needed it.
        "has_voids": entry.get("has_voids"),
    }


def order_infos(infos: List[Dict[str, Any]], mode: str) -> List[Dict[str, Any]]:
    """Priority order (first = wins where files overlap)."""
    if mode == PRIORITY_NAME:
        return sorted(infos, key=lambda i: name_key(i["path"]))
    # Finest first; pixel sizes rounded so float noise between equal-grid
    # tiles (and between X/Y) cannot reorder a homogeneous set — ties keep
    # file-name order, identically on every machine.
    return sorted(infos, key=lambda i: (round(math.log(max(i["px_m"], 1e-6)), 3),
                                        name_key(i["path"])))


def source_infos(folder: str, mode: str,
                 decisions: Optional[Dict[str, str]] = None,
                 crs_overrides: Optional[Dict[str, str]] = None,
                 index: Optional[FolderIndex] = None,
                 ) -> Tuple[List[Dict[str, Any]], List[Tuple[str, str, str]]]:
    """``(usable infos in priority order, skipped problems)`` for *folder*.

    Raises RuntimeError naming every unanswered problem file — a run never
    drops a file silently: the GUI asks first (``gui/terrain_folder_scan``),
    and a caller without a prompt (Processing) is refused loudly.
    """
    index = index or get_index(folder)
    decisions = decisions or {}
    crs_overrides = crs_overrides or {}
    open_problems = unresolved_problems(index, decisions, crs_overrides)
    if open_problems:
        raise RuntimeError(
            f"{len(open_problems)} file(s) in the terrain folder cannot be "
            f"used as they are:\n{folder}\n\n"
            + format_problems(open_problems, folder)
            + "\n\nFix or remove them, or run from the Waveshed dialog, which "
              "offers to skip them (and to assign a CRS to files without "
              "one in the Map Converter).")
    infos: List[Dict[str, Any]] = []
    skipped: List[Tuple[str, str, str]] = []
    for p in sorted(index.entries):
        e = index.entries[p]
        prob = problem_of(e)
        if prob is not None and prob[0] == KIND_NOCRS and p in crs_overrides:
            assigned = with_crs(e, crs_overrides[p])
            if assigned is None:
                raise RuntimeError(
                    f"The CRS assigned to {display_path(p, folder)} "
                    f"({crs_overrides[p]}) is not usable for it.")
            infos.append(entry_info(p, assigned))
            continue
        if prob is not None:
            if decisions.get(p) == DECISION_USE and prob[0] == KIND_SUSPECT:
                infos.append(entry_info(p, e))
            else:
                skipped.append((p, prob[0], prob[1]))
            continue
        infos.append(entry_info(p, e))
    for info in infos:
        info["index_folder"] = index.folder     # ensure_void_flags keeps here
    return order_infos(infos, mode), skipped


def describe_order(infos: List[Dict[str, Any]], mode: str, folder: str,
                   limit: int = 8) -> str:
    """One log line stating the effective overlap order."""
    if not infos:
        return "overlap priority: no usable files"
    groups: List[Tuple[float, int]] = []
    for i in infos:
        r = round(i["px_m"], 1)
        if groups and groups[-1][0] == r:
            groups[-1] = (r, groups[-1][1] + 1)
        else:
            groups.append((r, 1))
    head = ", ".join(display_path(i["path"], folder) for i in infos[:limit])
    more = f", … (+{len(infos) - limit})" if len(infos) > limit else ""
    if mode == PRIORITY_NAME:
        res = ""
    else:
        res = " — " + ", ".join(f"{r:g} m ×{n}" for r, n in groups[:6]) + (
            ", …" if len(groups) > 6 else "")
    return (f"overlap priority: {PRIORITY_LABELS.get(mode, mode)}{res}; "
            f"order: {head}{more}")


def mixed_resolution_overlaps(infos: List[Dict[str, Any]],
                              ) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
    """Pairs (winner, loser) where overlapping files differ in resolution and
    the COARSER one wins — what name order can do. At most a handful."""
    n = len(infos)
    if n < 2:
        return []
    w = np.array([i["wgs84_bounds"]["west"] for i in infos])
    e = np.array([i["wgs84_bounds"]["east"] for i in infos])
    s = np.array([i["wgs84_bounds"]["south"] for i in infos])
    nn = np.array([i["wgs84_bounds"]["north"] for i in infos])
    px = np.array([max(i["px_m"], 1e-6) for i in infos])
    eps = 1e-9
    out = []
    for a in range(n - 1):
        b = slice(a + 1, n)
        hit = ((w[b] < e[a] - eps) & (e[b] > w[a] + eps)
               & (s[b] < nn[a] - eps) & (nn[b] > s[a] + eps)
               & (px[b] * SAME_RES_RATIO < px[a]))
        if hit.any():
            j = a + 1 + int(np.argmax(hit))
            out.append((infos[a], infos[j]))
            if len(out) >= 5:
                break
    return out


# ---------------------------------------------------------------------------
# Coverage (per-file WGS84 footprints, any CRS — no mosaic)
# ---------------------------------------------------------------------------

class Coverage(NamedTuple):
    covered: float                 # fraction of the bbox covered, 0..1
    internal_gap: float            # fraction lying INSIDE the data (holes)
    gap_bbox: Optional[Dict[str, float]]   # extent of the holes (WGS84)
    data_bbox: Optional[Dict[str, float]]  # union of footprints touching bbox
    files: int                     # files touching the bbox
    gap_km2: float = 0.0           # area of the holes


def _points_in_ring(xs: np.ndarray, ys: np.ndarray,
                    ring: List[List[float]]) -> np.ndarray:
    inside = np.zeros(xs.shape, dtype=bool)
    n = len(ring)
    for k in range(n):
        x0, y0 = ring[k]
        x1, y1 = ring[(k + 1) % n]
        if y0 == y1:
            continue
        cond = (y0 > ys) != (y1 > ys)
        xint = x0 + (ys - y0) * (x1 - x0) / (y1 - y0)
        inside ^= cond & (xs < xint)
    return inside


def _sandwiched(mask: np.ndarray) -> np.ndarray:
    """Cells with a covered cell somewhere left AND right in their row and
    above AND below in their column — the uncovered ones are holes in the
    data, not ground past its edge (an L- or C-shaped folder's open side
    has no covered cell beyond it)."""
    def before(m: np.ndarray, axis: int) -> np.ndarray:
        return np.maximum.accumulate(m, axis=axis)

    def after(m: np.ndarray, axis: int) -> np.ndarray:
        return np.flip(np.maximum.accumulate(np.flip(m, axis), axis=axis), axis)
    m = mask.astype(np.uint8)
    return ((before(m, 1) & after(m, 1) & before(m, 0) & after(m, 0))
            .astype(bool))


def coverage(infos: List[Dict[str, Any]], bbox: Dict[str, float],
             cells: int = 512) -> Coverage:
    """Which part of *bbox* the files' footprints cover, on a *cells*² grid
    of cell centres (each footprint in WGS84, whatever the file's CRS).

    ``internal_gap`` is the uncovered part lying INSIDE the data — a file
    missing in the middle of the folder — reported apart from ground simply
    past the data's edge. Only holes at least 3×3 cells across count, so a
    sub-cell sliver between two files' grids never reads as a gap."""
    W, E, S, N = bbox["west"], bbox["east"], bbox["south"], bbox["north"]
    if E <= W or N <= S:
        return Coverage(1.0, 0.0, None, None, 0)
    dx = (E - W) / cells
    dy = (N - S) / cells
    cx = W + (np.arange(cells) + 0.5) * dx
    cy = N - (np.arange(cells) + 0.5) * dy
    mask = np.zeros((cells, cells), dtype=bool)
    touching = []
    for info in infos:
        b = info["wgs84_bounds"]
        if b["east"] <= W or b["west"] >= E or b["north"] <= S or b["south"] >= N:
            continue
        touching.append(b)
        c0 = max(0, int(math.floor((b["west"] - W) / dx)))
        c1 = min(cells, int(math.ceil((b["east"] - W) / dx)))
        r0 = max(0, int(math.floor((N - b["north"]) / dy)))
        r1 = min(cells, int(math.ceil((N - b["south"]) / dy)))
        if c1 <= c0 or r1 <= r0:
            continue
        gx, gy = np.meshgrid(cx[c0:c1], cy[r0:r1])
        ring = info.get("footprint")
        if ring and len(ring) >= 3:
            hit = _points_in_ring(gx, gy, ring)
        else:
            hit = ((gx >= b["west"]) & (gx <= b["east"])
                   & (gy >= b["south"]) & (gy <= b["north"]))
        mask[r0:r1, c0:c1] |= hit
    covered = float(mask.mean())
    if not touching:
        return Coverage(0.0, 0.0, None, None, 0)
    data_bbox = {"west": min(b["west"] for b in touching),
                 "east": max(b["east"] for b in touching),
                 "south": min(b["south"] for b in touching),
                 "north": max(b["north"] for b in touching)}
    holes = _sandwiched(mask) & ~mask
    # Erode by one cell: a real hole survives, a 1-cell-wide sliver does not.
    core = holes.copy()
    core[1:, :] &= holes[:-1, :]
    core[:-1, :] &= holes[1:, :]
    core[:, 1:] &= holes[:, :-1]
    core[:, :-1] &= holes[:, 1:]
    if not core.any():
        return Coverage(covered, 0.0, None, data_bbox, len(touching))
    frac = float(holes.mean())
    rows = np.where(holes.any(axis=1))[0]
    cols = np.where(holes.any(axis=0))[0]
    gap_bbox = {"west": W + cols[0] * dx, "east": W + (cols[-1] + 1) * dx,
                "north": N - rows[0] * dy, "south": N - (rows[-1] + 1) * dy}
    clat = math.radians((N + S) / 2.0)
    area_km2 = ((E - W) * _M_PER_DEG_LON_EQ * math.cos(clat)
                * (N - S) * _M_PER_DEG_LAT) / 1e6
    return Coverage(covered, frac, gap_bbox, data_bbox, len(touching),
                    frac * area_km2)


# ---------------------------------------------------------------------------
# No-data pixels (lazy, per file: the Map Converter's XYZ skip rule)
# ---------------------------------------------------------------------------

#: A sample at or below this elevation (metres, after scale/offset) is no-data
#: to the converter: its samplers drop half-metre counts <= -5000, i.e.
#: -2500 m (ingest.rs sample_cell / mean_valid; abt.MIN_VALID_ELEV_M) — so
#: -9999 is a void even without a GDAL_NODATA tag. Half a count of margin: a
#: value that rounds into the floor is one too.
VOID_FLOOR_M = -2499.75
#: Pixels per read of :func:`scan_voids` (bounds the RAM of a big file).
VOID_CHUNK_PX = 1 << 22

#: (path, size:mtime) -> flag, for files without a folder index (single-file
#: layers) and as a session shortcut; cleared by forget_indexes().
_VOID_CACHE: Dict[Tuple[str, Optional[str]], bool] = {}
_VOID_LOCK = threading.Lock()


def _chunk_has_void(a: np.ndarray, nodata: Optional[float], scale: float,
                    offset: float) -> bool:
    if a.size == 0:
        return False
    if a.dtype.kind == "f" and not np.isfinite(a).all():
        return True                               # NaN / inf
    if scale == 0:
        floor_hit = offset <= VOID_FLOOR_M         # every sample is offset
    elif scale > 0:
        thr = (VOID_FLOOR_M - offset) / scale
        floor_hit = bool((a <= thr).any())
    else:
        thr = (VOID_FLOOR_M - offset) / scale
        floor_hit = bool((a >= thr).any())
    if floor_hit:
        return True
    if nodata is None or math.isnan(nodata):
        return False                              # NaN nodata: seen above
    return bool((a == nodata).any())


def _first_band(path: str) -> Tuple[Any, Any]:
    """``(dataset, band 1)`` of *path*, or ``(None, None)``."""
    try:
        ds = gdal.Open(path, gdal.GA_ReadOnly)
    except Exception:  # noqa: BLE001 — exceptions mode on in the host
        ds = None
    band = ds.GetRasterBand(1) if ds is not None else None
    return (ds, band) if band is not None else (None, None)


def scan_voids(path: str, should_cancel: Optional[Callable[[], bool]] = None
               ) -> Optional[bool]:
    """True when the raster at *path* holds ANY sample the converter reads as
    no-data — its declared nodata value, NaN/inf, or an elevation at or below
    :data:`VOID_FLOOR_M` (after scale/offset) —, False when it holds none,
    None when it cannot be read. First band (usable files have one), read in
    chunks of ``VOID_CHUNK_PX``; stops at the first void.
    :class:`ScanCancelled` once *should_cancel()* is true."""
    try:
        gdal.PushErrorHandler("CPLQuietErrorHandler")
    except Exception:  # noqa: BLE001 — no usable GDAL: nothing is known
        return None
    try:
        ds, band = _first_band(path)
        if band is None:
            return None
        nodata = band.GetNoDataValue()
        scale, offset = band.GetScale(), band.GetOffset()
        scale = 1.0 if scale is None else float(scale)
        offset = 0.0 if offset is None else float(offset)
        xs, ys = ds.RasterXSize, ds.RasterYSize
        rows = max(1, VOID_CHUNK_PX // max(xs, 1))
        for y0 in range(0, ys, rows):
            if should_cancel is not None and should_cancel():
                raise ScanCancelled()
            a = band.ReadAsArray(0, y0, xs, min(rows, ys - y0))
            if a is None or _chunk_has_void(a, nodata, scale, offset):
                return None if a is None else True
        return False
    except ScanCancelled:
        raise
    except Exception:  # noqa: BLE001 — unknown = not void-free, no error
        return None
    finally:
        gdal.PopErrorHandler()


VoidKey = Tuple[str, Optional[str]]


def _unknown_void_groups(infos: List[Dict[str, Any]]
                         ) -> Dict[VoidKey, List[Dict[str, Any]]]:
    """``{(path, size:mtime): [infos]}`` whose flag nothing knows yet (the
    session cache fills the rest in on the way)."""
    groups: Dict[VoidKey, List[Dict[str, Any]]] = {}
    for info in infos:
        if info.get("has_voids") is not None:
            continue
        key = (info["path"], info.get("fp"))
        with _VOID_LOCK:
            known = _VOID_CACHE.get(key)
        if known is not None:
            info["has_voids"] = known
        else:
            groups.setdefault(key, []).append(info)
    return groups


def _read_void_flags(keys: List[VoidKey],
                     should_cancel: Optional[Callable[[], bool]],
                     workers: int,
                     done: Dict[VoidKey, Optional[bool]]) -> None:
    """:func:`scan_voids` of every key's path in a pool, into *done*;
    :class:`ScanCancelled` (pending reads dropped) once *should_cancel()*."""
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(scan_voids, path, should_cancel)
                   for path, _fp in keys]
        try:
            for key, fut in zip(keys, futures):
                done[key] = fut.result()
                if should_cancel is not None and should_cancel():
                    raise ScanCancelled()
        except BaseException:
            for fut in futures:
                fut.cancel()
            raise


def ensure_void_flags(infos: List[Dict[str, Any]],
                      should_cancel: Optional[Callable[[], bool]] = None,
                      log: Optional[Callable[[str], None]] = None,
                      workers: int = SCAN_WORKERS) -> int:
    """Fill ``info["has_voids"]`` (:func:`scan_voids`) on every info of
    *infos* whose flag is not known yet; returns how many files were read.

    Lazy and once: a folder file's flag is kept in its index entry (saved
    with the index; a changed file is re-probed and so loses it), any other
    file's for the session. Read in the scan's thread pool — call it off the
    GUI thread; :class:`ScanCancelled` once *should_cancel()* is true (what
    was read is kept). A file that cannot be read stays None (= not known to
    be void-free). *log* gets one line: count, time, how many have voids."""
    groups = _unknown_void_groups(infos)
    if not groups:
        return 0
    t0 = time.perf_counter()
    keys = sorted(groups, key=lambda k: (k[0], str(k[1])))
    done: Dict[VoidKey, Optional[bool]] = {}
    try:
        _read_void_flags(keys, should_cancel, workers, done)
    finally:
        _keep_void_flags(groups, done)
    if log is not None:
        voids = sum(1 for v in done.values() if v)
        unknown = sum(1 for v in done.values() if v is None)
        log(f"  checking {len(keys):,} file(s) for no-data pixels: "
            f"{time.perf_counter() - t0:.1f} s — {voids:,} with no-data"
            + (f", {unknown:,} unreadable" if unknown else ""))
    return len(keys)


def _keep_void_flags(groups: Dict[VoidKey, List[Dict[str, Any]]],
                     done: Dict[VoidKey, Optional[bool]]) -> None:
    """Set the computed flags on the infos, the session cache and — bound to
    the same size+mtime — the folder index entries (saved once per index)."""
    folders: Dict[str, List[Tuple[Dict[str, Any], bool]]] = {}
    for key, flag in done.items():
        if flag is None:
            continue
        with _VOID_LOCK:
            _VOID_CACHE[key] = flag
        for info in groups[key]:
            info["has_voids"] = flag
            folder = info.get("index_folder")
            if folder:
                folders.setdefault(folder, []).append((info, flag))
    for folder, pairs in folders.items():
        index = get_index(folder)
        with index.lock:
            index.load()
            changed = False
            for info, flag in pairs:
                entry = index.entries.get(info["path"])
                if (entry is not None and entry.get("has_voids") != flag
                        and f"{entry.get('size')}:{entry.get('mtime_ns')}"
                        == info.get("fp")):
                    entry["has_voids"] = flag
                    changed = True
            if changed:
                index.save()


# ---------------------------------------------------------------------------
# Overlap-priority setting (per folder)
# ---------------------------------------------------------------------------

def _priority_key(folder: str) -> str:
    return f"waveshed/overlap_priority/{folder_key(folder)}"


def get_overlap_priority(folder: Optional[str]) -> str:
    """The folder's "where files overlap" rule (default: finest wins)."""
    if not folder:
        return PRIORITY_FINEST
    try:
        from qgis.core import QgsSettings
        val = QgsSettings().value(_priority_key(folder), PRIORITY_FINEST)
    except Exception:  # noqa: BLE001
        val = PRIORITY_FINEST
    return val if val in PRIORITY_LABELS else PRIORITY_FINEST


def set_overlap_priority(folder: str, mode: str) -> None:
    if mode not in PRIORITY_LABELS:
        raise ValueError(f"unknown overlap priority {mode!r}")
    from qgis.core import QgsSettings
    QgsSettings().setValue(_priority_key(folder), mode)


# ---------------------------------------------------------------------------
# osr bbox transform (materialize windows)
# ---------------------------------------------------------------------------

def bbox_from_wgs84(bbox: Dict[str, float], crs_wkt: str
                    ) -> Optional[Dict[str, float]]:
    """*bbox* (WGS84) as an axis-aligned bbox in *crs_wkt* (densified)."""
    ct = _transform(crs_wkt, inverse=True)
    if ct is None:
        return None
    corners = [(bbox["west"], bbox["north"]), (bbox["east"], bbox["north"]),
               (bbox["east"], bbox["south"]), (bbox["west"], bbox["south"])]
    return _bounds(_transform_points(ct, _densify_ring(corners, 20)))
