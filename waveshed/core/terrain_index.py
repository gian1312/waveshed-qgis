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
  stat on the Windows mount is ~3 ms), new or changed files are probed with
  GDAL in a thread pool (GDAL releases the GIL for I/O, measured ~7x on 8
  threads), removed files drop out. Never call :meth:`FolderIndex.refresh`
  on the GUI thread — the GUI runs it in ``gui/terrain_folder_scan``.

Pure GDAL/OSR (no QGIS objects), so it is thread-safe and testable without a
QGIS instance.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple

import numpy as np
from osgeo import gdal

try:
    from osgeo import osr
except ImportError:  # the unit-test stub of osgeo has no osr; tests that
    osr = None       # exercise the index swap the real package in

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
    px_x = math.hypot(gt[1], gt[4])
    px_y = math.hypot(gt[2], gt[5])
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
    clat = (wgs_bbox["north"] + wgs_bbox["south"]) / 2.0
    unit = _crs_unit(crs["crs_wkt"], crs["geographic"])
    if crs["geographic"]:
        deg = math.degrees(unit)
        px_x_m = px_x * deg * _M_PER_DEG_LON_EQ * max(math.cos(math.radians(clat)), 0.01)
        px_y_m = px_y * deg * _M_PER_DEG_LAT
    else:
        px_x_m, px_y_m = px_x * unit, px_y * unit
    return {
        "native_bounds": native,
        "wgs84_bounds": wgs_bbox,
        "footprint": [[round(x, 7), round(y, 7)] for x, y in ring],
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


def probe_geotiff_fast(path: str, siblings: Optional[set] = None,
                       file_size: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """The index entry for an ordinary GeoTIFF without GDAL, or None when the
    file needs the GDAL probe (see the section comment). *siblings*: lower-cased
    paths of every file in the folder (from the scan) — sidecars are looked up
    there instead of with a disk call each."""
    if path.startswith("/vsi") or _ext(path) not in (".tif", ".tiff"):
        return None
    stem = os.path.splitext(path)[0]
    for suffix in _SIDECAR_SUFFIXES:
        for cand in (path + suffix, stem + suffix):
            if (cand.lower() in siblings) if siblings is not None else os.path.exists(cand):
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
            entry["reason"] = ("truncated or corrupt — its pixels cannot be "
                               "read" + (f" ({msg[:100]})" if msg else ""))
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
    return hashlib.md5(_norm_folder(folder).encode("utf-8"),
                       usedforsecurity=False).hexdigest()


def index_path(folder: str) -> str:
    from .terrain_adapter import get_cache_dir  # deferred: circular import
    return os.path.join(get_cache_dir(), "index", folder_key(folder) + ".json")


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
        if (doc.get("schema") != INDEX_SCHEMA
                or _norm_folder(doc.get("folder", "")) != _norm_folder(self.folder)):
            return
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
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass

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
            for p in removed:
                del self.entries[p]
            check()

            t1 = time.perf_counter()
            if to_probe:
                to_probe.sort()
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    probe = lambda path: probe_file(path, names, wanted[path])  # noqa: E731
                    for i, (p, ent) in enumerate(zip(to_probe,
                                                     ex.map(probe, to_probe))):
                        st = wanted[p]
                        ent["size"], ent["mtime_ns"] = st
                        self.entries[p] = ent
                        if i % 25 == 0:
                            tick(i, len(to_probe), "reading")
                            if should_cancel is not None and should_cancel():
                                ex.shutdown(wait=False, cancel_futures=True)
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
            self.refreshed_at = time.time()
            self.last_stats = RefreshStats(
                files=len(wanted), new=new, changed=changed,
                removed=len(removed), unchanged=unchanged,
                stat_s=t_stat, probe_s=t_probe,
                total_s=time.perf_counter() - t0)
            return self.last_stats

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
