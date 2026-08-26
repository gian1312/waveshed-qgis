"""Buildings vector sources → the one thing the converter can read.

``aether_converter`` reads building geometry from **FlatGeobuf and nothing
else**, in **WGS84 and nothing else**: there is no GDAL/OGR in the toolkit, by
design (see ``ingest.rs::apply_buildings``, which opens the path with
``FgbReader``). Everything a user can hand the plugin — a shapefile in
EPSG:2056, a GeoJSON, an ESRI file geodatabase, a GeoPackage sublayer that
QGIS spells ``…/x.gpkg|layername=buildings`` — therefore has to be converted
*here*, in the plugin, before the path reaches an ingest job. A source that is
not converted is not refused by the converter: it prints one ``[Warn]`` line,
exits 0 and writes terrain with no buildings on it.

This lived in the Map Converter tab's worker and was reachable from nowhere
else, so the Site Analysis path (``terrain_adapter.prepare_terrain`` — used by
both GUI tabs, both Processing algorithms and the torture runner) handed the
raw path straight to the converter. It belongs in ``core/`` because it is
business logic with no GUI in it: the GUI imports it back, so there is exactly
one copy.

GDAL is QGIS's own (``osgeo``) — never a pip dependency.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from typing import Callable, List, Optional, Tuple

from .terrain_adapter import source_fingerprint

#: Directory name (under the system temp dir) holding cached conversions.
_FGB_CACHE_DIRNAME = "aether_fgb"


def _log(msg: str) -> None:
    try:
        from qgis.core import Qgis, QgsMessageLog
        QgsMessageLog.logMessage(msg, "Waveshed-Terrain", Qgis.MessageLevel.Info)
    except Exception:  # noqa: BLE001 — logging must never break a conversion
        pass


def fgb_cache_dir() -> str:
    """The shared, session-spanning directory cached conversions live in.

    Created on demand. One place, because the name is half of the cache
    identity: four separate spellings of it in the GUI is four caches.
    """
    out_dir = os.path.join(tempfile.gettempdir(), _FGB_CACHE_DIRNAME)
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def _fgb_translate_options(src_crs: str = "", promote: bool = True) -> List[str]:
    """GDAL VectorTranslate options for a → FlatGeobuf/WGS84 conversion.

    A file with no embedded CRS reprojects to garbage unless GDAL is told what
    it is already in, so *src_crs* (the detected or user-supplied CRS) is
    passed as ``-s_srs`` when known.

    *promote* controls ``-nlt PROMOTE_TO_MULTI``. It must be OFF for a
    TIN/PolyhedralSurface source (swissBUILDINGS3D 3.0 GDB solids/roofs):
    promotion turns those into MultiSurface, which the FlatGeobuf writer
    rejects feature by feature — and ``-skipfailures`` then delivered a
    structurally valid, EMPTY .fgb that burned nothing. Written natively, a
    TIN Z reads fine (the converter walks it like a MultiPolygon).

    Deliberately no ``-zfield``: the converter reads geometry Z as an
    ABSOLUTE roof elevation, while an OSM ``height`` attribute is metres above
    ground — writing 12 m into Z over 540 m Bern terrain puts every roof half
    a kilometre underground, and the engine drops the lot. Attribute heights
    are the converter's job; ours is to hand it readable, correctly projected
    geometry with its attributes intact.
    """
    options = [
        "-f", "FlatGeobuf",
        "-t_srs", "EPSG:4326",
        "-lco", "SPATIAL_INDEX=YES",
        "-skipfailures",
    ]
    if promote:
        options[4:4] = ["-nlt", "PROMOTE_TO_MULTI"]
    if src_crs:
        options += ["-s_srs", src_crs]
    return options


def _open_layer(path: str, layer_name: str = ""):
    """The named (or first) OGR layer of *path*, with its dataset, or None.

    The dataset is returned alongside the layer because OGR ties a layer's
    lifetime to it — let the dataset go and the layer reads freed memory.
    """
    try:
        from osgeo import ogr
        ds = ogr.Open(path)
        if ds is None:
            return None, None
        layer = ds.GetLayerByName(layer_name) if layer_name else ds.GetLayer(0)
        return (ds, layer) if layer is not None else (None, None)
    except Exception:  # noqa: BLE001 — an unreadable source is "no layer"
        return None, None


def _is_surface_collection_source(path: str, layer_name: str = "") -> bool:
    """Does the (sub)layer carry TIN/PolyhedralSurface/Triangle geometry?"""
    ds, layer = _open_layer(path, layer_name)
    if layer is None:
        return False
    try:
        from osgeo import ogr
        flat = ogr.GT_Flatten(layer.GetGeomType())
        return flat in (ogr.wkbTIN, ogr.wkbPolyhedralSurface, ogr.wkbTriangle)
    except Exception:  # noqa: BLE001 — unknown geometry: keep the default
        return False
    finally:
        del layer, ds


def _layer_feature_count(path: str, layer_name: str = "") -> int:
    """Feature count of the (sub)layer in *path*, or -1 when unknowable."""
    ds, layer = _open_layer(path, layer_name)
    if layer is None:
        return -1
    try:
        return int(layer.GetFeatureCount())
    except Exception:  # noqa: BLE001 — a source that cannot count is unknown
        return -1
    finally:
        del layer, ds


def _split_sublayer(uri: str) -> Tuple[str, str]:
    """Split a QGIS OGR URI into ``(file path, sublayer name)``.

    QGIS hands a GeoPackage/GDB/KML sublayer over as
    ``…/x.gpkg|layername=buildings``. Neither ``os.path.isfile`` nor
    ``os.path.splitext`` understands that suffix, so the extension test in
    ``resolve_buildings_source`` never matched — no conversion happened and the
    raw URI went to the converter, whose ``File::open`` fails with ENOENT and
    which only warns. Anything other than ``layername=`` (``layerid=``,
    ``geometrytype=``, …) yields an empty name, which converts every layer
    rather than the wrong one.
    """
    head, sep, tail = uri.partition("|")
    if not sep:
        return uri, ""
    for part in tail.split("|"):
        key, _, value = part.partition("=")
        if key.strip().lower() == "layername":
            return head, value
    return head, ""


def _fgb_cache_name(src_path: str, src_crs: str = "",
                    layer_name: str = "") -> str:
    """File name for a cached conversion of *src_path* declared as *src_crs*.

    The conversion cache lives in a shared temp directory that survives across
    QGIS sessions, so the name has to pin everything that determines the
    output. Keying on the base name alone caused three separate faults:

    * two sources sharing a base name (``a/buildings.shp``, ``b/buildings.shp``)
      returned each other's conversion;
    * re-declaring a source's CRS reused the conversion made under the old one;
    * a conversion cached before ``-s_srs`` was honoured kept being returned,
      so the fix silently never applied.

    Including the source fingerprint and the CRS makes each of those a
    different file, and leaves pre-existing entries unreachable rather than
    wrong.

    *layer_name* rides along too: two sublayers of one GeoPackage are two
    different conversions of the same file.
    """
    basename = os.path.splitext(os.path.basename(src_path))[0]
    ident = f"{source_fingerprint(src_path)}|{src_crs}|{layer_name}"
    digest = hashlib.md5(ident.encode("utf-8")).hexdigest()[:12]
    return f"{basename}_{digest}.fgb"


def _discard(path: str) -> None:
    """Remove *path* — file or directory — best effort, never raising."""
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        else:
            os.remove(path)
    except OSError:
        pass


def _convert_to_fgb(src_path: str, out_dir: str, src_crs: str = "",
                    layer_name: str = "") -> Optional[str]:
    """Convert a vector file (GDB/SHP/GPKG/GeoJSON) to FlatGeobuf via GDAL.

    *src_path* must be a plain filesystem path — GDAL does not understand
    QGIS's ``|layername=`` suffix, so a container's sublayer is selected with
    *layer_name* instead (see :func:`_split_sublayer`).

    Returns path to the output .fgb file, or None on failure.
    """
    try:
        from osgeo import gdal
        out_path = os.path.join(
            out_dir, _fgb_cache_name(src_path, src_crs, layer_name))
        if os.path.exists(out_path):
            return out_path

        # Convert under a temp name and rename on success, so a conversion that
        # fails or is interrupted cannot leave a partial .fgb behind that the
        # next run would return straight out of the cache.
        #
        # The temp name KEEPS the .fgb extension. GDAL's FlatGeobuf driver
        # writes a single file when the target name ends in .fgb and a
        # DIRECTORY dataset when it does not, so a plain ".part" suffix turned
        # every conversion into a directory named "<cache name>.fgb" holding
        # the real file inside it. Nothing failed loudly — the converter scans
        # a directory of .fgb parts too — but the cache never held the file it
        # claims to, and anything opening it as one gets EISDIR.
        stem, ext = os.path.splitext(out_path)
        tmp_path = f"{stem}.part{ext}"
        _discard(tmp_path)

        options = _fgb_translate_options(
            src_crs,
            promote=not _is_surface_collection_source(src_path, layer_name))
        # `layers` is only passed when there is one, so the far commoner
        # whole-file conversion issues exactly the call it always has.
        if layer_name:
            result = gdal.VectorTranslate(tmp_path, src_path, options=options,
                                          layers=[layer_name])
        else:
            result = gdal.VectorTranslate(tmp_path, src_path, options=options)
        if result is None:
            _discard(tmp_path)
            return None
        result = None  # Close dataset

        # -skipfailures means VectorTranslate "succeeds" no matter how many
        # features the writer rejected. An output with NONE of the source's
        # features is not a conversion, it is an empty file wearing the cache
        # name — returned once, it burns nothing forever. Fail it here, and
        # say so when features were merely dropped rather than lost wholesale.
        src_n = _layer_feature_count(src_path, layer_name)
        dst_n = _layer_feature_count(tmp_path)
        if dst_n == 0 and src_n != 0:
            _discard(tmp_path)
            _log(f"Vector conversion wrote 0 of {src_n} features for "
                 f"{src_path} — refusing the empty result")
            return None
        if 0 < src_n and 0 <= dst_n < src_n:
            _log(f"Vector conversion dropped {src_n - dst_n} of {src_n} "
                 f"features for {src_path} (unwritable geometry)")
        os.replace(tmp_path, out_path)
        return out_path
    except Exception as exc:  # noqa: BLE001 — any GDAL failure is "no output"
        _log(f"Vector conversion failed for {src_path}: {exc}")
        return None


def is_converter_readable(path: str) -> bool:
    """Can ``aether_converter`` open *path* as a buildings source?

    Its answer is "an .fgb file, or a directory of them" — ``apply_buildings``
    scans a directory for ``*.fgb`` and otherwise opens the path itself. A
    ``…|layername=`` URI is not a file, so it is not readable, which is the
    whole point of :func:`resolve_buildings_source` running first.
    """
    if not path:
        return False
    if os.path.isdir(path):
        try:
            return any(name.lower().endswith(".fgb")
                       for name in os.listdir(path))
        except OSError:
            return False
    return os.path.isfile(path) and path.lower().endswith(".fgb")


def resolve_buildings_source(
    path: str,
    src_crs: str = "",
    log: Optional[Callable[[str], None]] = None,
) -> str:
    """Resolve a buildings layer source to a converter-readable FGB path.

    *path* is a QGIS layer source: a plain file, a directory (.gdb or a set of
    .fgb parts), or a container sublayer URI (``…|layername=x``). *src_crs* is
    the layer's declared CRS when QGIS knows one — needed only for a file that
    carries no CRS of its own, since the conversion always targets EPSG:4326.

    Returns the path to hand the converter. That is a converted ``.fgb``
    wherever a conversion was possible; where it was not, the input comes back
    unchanged (absolutised) rather than being dropped — the caller decides
    what an unreadable source means, via :func:`is_converter_readable`.
    """
    say = log or _log

    # A container sublayer arrives as "<file>|layername=<x>". Split it off
    # before every isfile/splitext test below — on the joined URI isfile() is
    # always False and splitext() yields ".gpkg|layername=x", so the conversion
    # was skipped twice over and the raw URI reached the converter, which
    # cannot open it.
    file_path, sublayer = _split_sublayer(path)

    # GDB → FGB conversion.
    is_gdb = False
    if file_path.lower().endswith(".gdb") and os.path.isdir(file_path):
        is_gdb = True
    elif os.path.isdir(file_path):
        try:
            is_gdb = any(f.lower().endswith(".gdbtable")
                         for f in os.listdir(file_path))
        except OSError:
            pass

    if is_gdb:
        say(f"  Converting GDB → FlatGeobuf: {path}")
        fgb = _convert_to_fgb(file_path, fgb_cache_dir(), src_crs, sublayer)
        if fgb:
            return os.path.abspath(fgb)
        say("  GDB conversion failed.")

    # SHP/GPKG/GeoJSON → FGB.
    ext = (os.path.splitext(file_path)[1].lower()
           if os.path.isfile(file_path) else "")
    if ext in (".shp", ".gpkg", ".geojson"):
        say(f"  Converting {ext} → FlatGeobuf...")
        fgb = _convert_to_fgb(file_path, fgb_cache_dir(), src_crs, sublayer)
        if fgb:
            return os.path.abspath(fgb)
        say(f"  WARNING: {ext} → FlatGeobuf conversion failed; the converter "
            f"reads FlatGeobuf only, so these buildings would be ignored.")

    # Already FlatGeobuf (or an unrecognised container): only safe to pass
    # straight through when it is already WGS84.
    if src_crs and src_crs != "EPSG:4326":
        say(f"  Reprojecting buildings {src_crs} → EPSG:4326...")
        fgb = _convert_to_fgb(file_path, fgb_cache_dir(), src_crs, sublayer)
        if fgb:
            return os.path.abspath(fgb)
        say(f"  WARNING: could not reproject buildings from {src_crs}; "
            "they may be placed incorrectly.")

    # Absolutise the file part only: abspath() on the whole URI prefixes the
    # cwd to it, and the suffix has to survive for anything downstream that
    # still understands it.
    resolved = os.path.abspath(file_path)
    return f"{resolved}|layername={sublayer}" if sublayer else resolved
