"""One-shot diagnostic for the `.abt` stripe/patch gaps.

Run this once against a terrain cache and hand the output (and the written
``abt_diag_report.json``) back. It gathers everything needed to locate the
root cause of the scattered missing patches, which neither ``scan_abt.py``
(full-row/col only) nor the download's ``100% OK`` stat can see:

  1. Scans every ``.abt`` for fully-zero BLOCKS (the scattered patches) and
     maps each to its source slippy tile ``z/x/y``.
  2. Re-fetches those exact source PNGs from the tile server and DECODES them
     itself (pure-stdlib PNG decoder) — reporting the PNG colour type / bit
     depth and the decoded elevation stats. This is the decisive split:
       * gap tile 404 / fetch error        -> source availability / fetch
       * gap tile is NOT 8-bit RGB(A)       -> the converter's decode_png
         mis-decodes it (assumes RGB8) -> writes garbage/zeros though the
         fetch "succeeded" (your "100% OK is wrong")
       * gap tile decodes to real, varied   -> the tile was fine; its data was
         terrain on refetch                    lost in strip ASSEMBLY
       * gap tile decodes to flat / blank   -> server returned a blank tile
  3. Also samples a few NON-gap ("good") tiles so their format is a baseline.
  4. Cross-references every gap tile against a download log, if given
     (``[Download] first connect error … x=…/y=…``, ``SLOW tile … x=…/y=…``).

Determinism (race vs deterministic): run the download twice into two cache
dirs and pass ``--compare dirA dirB`` to diff the gap sets.

Only deps: numpy (bundled with QGIS Python) + stdlib. No PIL/GDAL needed.

.abt header (LE, 44 B): [6:8]=width u16, [8:16]=ul_lat f64, [16:24]=ul_lon f64,
[24:32]=scale_y f64, [32:40]=scale_x f64, [42:44]=stride u16. Body @44, each row
``stride`` bytes, first ``width*2`` are i16 elevations.

Usage:
    python tools/abt_diag.py --cache <hash-dir> --zoom 14 [--log qgis.log]
    python tools/abt_diag.py --compare <dirA> <dirB> --zoom 14
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import struct
import sys
import urllib.request
import zlib

try:
    import numpy as np
except ImportError:
    print("[Error] numpy required. Run with QGIS's Python.", file=sys.stderr)
    raise SystemExit(1)

# The plugin's own .abt reader, so this diagnostic and the plugin can never
# disagree about a tile's geometry. It used to recompute the row stride from
# the width instead of reading the header field, and skipped the legacy
# scale_x correction — which put every reported gap coordinate off by a factor
# of `size` on tiles written by older converter builds. That is exactly the
# kind of bug you reach for this script to chase.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from waveshed.core import abt as abt_reader  # noqa: E402

DEFAULT_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
_COLOR = {0: "grayscale", 2: "rgb", 3: "palette", 4: "grayscale+a", 6: "rgba"}


# --------------------------------------------------------------------------- #
# .abt
# --------------------------------------------------------------------------- #
def load_abt(path: str):
    """``(elev, ul_lat, ul_lon, deg_per_px_y, deg_per_px_x)`` or None.

    Geometry comes from :func:`waveshed.core.abt.read_header`; only the pixel
    body is read here (``read_tile`` copies row by row, which is needlessly slow
    for a whole-cache sweep). Tiles are square, so one pixel resolution serves
    both axes.
    """
    header = abt_reader.read_header(path)
    if header is None:
        return None
    size, stride = header.size, header.stride
    need = abt_reader.HEADER_SIZE + size * stride
    data = np.fromfile(path, dtype=np.uint8)
    if stride < size * 2 or data.size < need:
        return None
    body = data[abt_reader.HEADER_SIZE:need].reshape(size, stride)
    elev = np.ascontiguousarray(body[:, : size * 2]).view(np.int16)
    return elev, header.ul_lat, header.ul_lon, header.pixel_res, header.pixel_res


# A pixel at/under this i16 value renders as NaN/transparent in the map-
# converter inspector, which keeps only elev > MIN_VALID_ELEV_M. Taken from the
# plugin rather than restated: this was hard-coded at -10000 (a -5000 *metre*
# floor), which sits one count below the converter's -9999 void sentinel — so a
# tile that was nothing but holes was reported as having no holes at all.
NODATA_I16 = abt_reader.MIN_VALID_ELEV_COUNTS


def hole_blocks(elev, block: int):
    """Blocks that are entirely a 'hole'. Returns (r0, c0, kind, i16_value):
    kind 'nodata' = all pixels <= NODATA_I16 (what shows as NaN); 'zero' = all 0.
    The i16_value exposes the actual sentinel (-32768 = i16::MIN unwritten,
    -19998 ~= a -9999 m GDAL nodata, etc.) so we can trace where it came from."""
    h, w = elev.shape
    nbr, nbc = h // block, w // block
    if nbr == 0 or nbc == 0:
        return []
    core = elev[: nbr * block, : nbc * block].reshape(nbr, block, nbc, block)
    nod = (core <= NODATA_I16).all(axis=(1, 3))
    zer = ~core.any(axis=(1, 3))
    out = []
    for br, bc in zip(*np.where(nod)):
        out.append((int(br) * block, int(bc) * block, "nodata",
                    int(np.median(core[br, :, bc, :]))))
    for br, bc in zip(*np.where(zer & ~nod)):
        out.append((int(br) * block, int(bc) * block, "zero", 0))
    return out


def lonlat_to_tile(lon, lat, z):
    n = 2 ** z
    xt = int((lon + 180.0) / 360.0 * n)
    yt = int((1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n)
    return xt, yt


def infer_zoom(scale_x_deg, center_lat, z_max=15):
    """Reproduce terrain_adapter._try_rust_download's zoom pick from the tile's
    own resolution, so --zoom need not be supplied."""
    res_m = max(scale_x_deg * 111_111.0, 1e-6)
    cos_lat = max(math.cos(math.radians(center_lat)), 0.1)
    z = math.ceil(math.log2(40_075_000.0 * cos_lat / (res_m * 256.0)))
    return min(max(int(z), 0), z_max)


def first_abt_params(cache):
    """(scale_x_deg, centre_lat) of the first readable .abt under *cache*."""
    if cache and os.path.isfile(cache):
        cands = [cache]
    else:
        cands = []
        for root, _, files in os.walk(cache or "."):
            cands += [os.path.join(root, f) for f in sorted(files)
                      if f.lower().endswith(".abt")]
    for p in cands:
        loaded = load_abt(p)
        if loaded:
            elev, ul_lat, _ul_lon, sy, sx = loaded
            return sx, ul_lat - (elev.shape[0] / 2) * sy
    return None


def scan_cache(cache: str, block: int, zoom: int):
    """Return {(z,x,y): [(abt, row, col, lat, lon), ...]} of gap tiles."""
    gaps: dict = {}
    n_tiles = n_gapblocks = 0
    for root, _, files in os.walk(cache):
        for f in sorted(files):
            if not f.lower().endswith(".abt"):
                continue
            loaded = load_abt(os.path.join(root, f))
            if loaded is None:
                continue
            n_tiles += 1
            elev, ul_lat, ul_lon, sy, sx = loaded
            for (r0, c0, kind, val) in hole_blocks(elev, block):
                n_gapblocks += 1
                lat = ul_lat - (r0 + block / 2) * sy
                lon = ul_lon + (c0 + block / 2) * sx
                key = lonlat_to_tile(lon, lat, zoom) if zoom else (0, 0)
                gaps.setdefault((zoom, *key), []).append(
                    (f, r0, c0, lat, lon, kind, val))
    return gaps, n_tiles, n_gapblocks


def sample_good_tile(cache: str, block: int, zoom: int, gap_keys: set):
    """Find one non-gap tile coord as a format baseline."""
    for root, _, files in os.walk(cache):
        for f in sorted(files):
            if not f.lower().endswith(".abt"):
                continue
            loaded = load_abt(os.path.join(root, f))
            if loaded is None:
                continue
            elev, ul_lat, ul_lon, sy, sx = loaded
            h, w = elev.shape
            # centre of the tile, which normally holds real terrain
            lat = ul_lat - (h / 2) * sy
            lon = ul_lon + (w / 2) * sx
            key = (zoom, *lonlat_to_tile(lon, lat, zoom))
            if key not in gap_keys:
                return key
    return None


# --------------------------------------------------------------------------- #
# stdlib PNG decode (8-bit RGB / RGBA, non-interlaced) -> terrarium elevation
# --------------------------------------------------------------------------- #
def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def decode_png(data: bytes) -> dict:
    """Return {'ok':bool, 'w','h','color_type','bit_depth','interlace',
    'note', and if decoded: 'elev_min/max/mean','flat','zero_frac'}."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return {"ok": False, "note": "not a PNG"}
    off = 8
    w = h = bd = ct = inter = None
    idat = bytearray()
    while off + 8 <= len(data):
        ln = struct.unpack_from(">I", data, off)[0]
        typ = data[off + 4:off + 8]
        body = data[off + 8:off + 8 + ln]
        off += 12 + ln
        if typ == b"IHDR":
            w, h, bd, ct, _comp, _filt, inter = struct.unpack(">IIBBBBB", body)
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
    info = {"ok": True, "w": w, "h": h, "bit_depth": bd,
            "color_type": _COLOR.get(ct, ct), "interlace": inter}
    if ct not in (2, 6) or bd != 8 or inter != 0:
        # This is exactly what the converter's decode_png cannot handle.
        info["note"] = "UNSUPPORTED by 8-bit-RGB decode_png -> mis-decoded"
        return info
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as e:
        info["ok"] = False
        info["note"] = f"IDAT inflate failed: {e}"
        return info
    bpp = 3 if ct == 2 else 4
    stride = w * bpp
    if len(raw) < (stride + 1) * h:
        info["ok"] = False
        info["note"] = f"IDAT short: {len(raw)} < {(stride + 1) * h}"
        return info
    out = bytearray(stride * h)
    prev = bytearray(stride)
    pos = 0
    for y in range(h):
        ft = raw[pos]; pos += 1
        line = bytearray(raw[pos:pos + stride]); pos += stride
        if ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif ft == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif ft == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif ft == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                c = prev[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(a, prev[i], c)) & 255
        out[y * stride:(y + 1) * stride] = line
        prev = line
    px = np.frombuffer(bytes(out), dtype=np.uint8).reshape(h, w, bpp).astype(np.float64)
    elev = px[:, :, 0] * 256.0 + px[:, :, 1] + px[:, :, 2] / 256.0 - 32768.0
    info.update(elev_min=round(float(elev.min()), 1),
                elev_max=round(float(elev.max()), 1),
                elev_mean=round(float(elev.mean()), 1),
                flat=bool(elev.min() == elev.max()),
                zero_frac=round(float((elev == 0).mean()), 4))
    return info


def fetch(url: str, timeout=20):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "abt_diag"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "code", None)
        return code or "ERR", str(e).encode()


def probe_tile(z, x, y, url_tpl):
    status, body = fetch(url_tpl.format(z=z, x=x, y=y))
    rec = {"z": z, "x": x, "y": y, "http": status}
    if isinstance(status, int) and status == 200 and isinstance(body, bytes):
        rec.update(decode_png(body))
    else:
        rec["note"] = body.decode("utf-8", "replace")[:120]
    return rec


# --------------------------------------------------------------------------- #
def parse_log_tiles(path: str) -> set:
    """Pull (x,y) pairs the download flagged (failed/slow/connect)."""
    hits = set()
    try:
        txt = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return hits
    for m in re.finditer(r"x=(\d+)/y=(\d+)", txt):
        hits.add((int(m.group(1)), int(m.group(2))))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="One-shot .abt gap diagnostic.")
    ap.add_argument("--cache", help="Cache dir with .abt tiles to inspect.")
    ap.add_argument("--compare", nargs=2, metavar=("DIRA", "DIRB"),
                    help="Diff gap sets between two caches (determinism test).")
    ap.add_argument("--zoom", type=int, default=0,
                    help="XYZ zoom (from the log, e.g. 14). Auto-inferred from "
                         "the .abt resolution if omitted.")
    ap.add_argument("--url", default=DEFAULT_URL, help="Tile URL template.")
    ap.add_argument("--block", type=int, default=64, help="Zero-block px.")
    ap.add_argument("--log", help="QGIS/terrain log to cross-ref failed tiles.")
    ap.add_argument("--max-fetch", type=int, default=30,
                    help="Cap tiles re-fetched (default 30).")
    ap.add_argument("--out", default="abt_diag_report.json")
    args = ap.parse_args()

    zoom = args.zoom
    if not zoom:
        src = args.cache or (args.compare[0] if args.compare else None)
        params = first_abt_params(src) if src else None
        if not params:
            print("[Error] could not read any .abt to infer zoom; pass --zoom.",
                  file=sys.stderr)
            return 1
        zoom = infer_zoom(*params)
        print(f"[auto] inferred XYZ zoom = {zoom} from .abt resolution "
              f"(override with --zoom)\n")

    report: dict = {"zoom": zoom, "block": args.block}

    if args.compare:
        ga, _, _ = scan_cache(args.compare[0], args.block, zoom)
        gb, _, _ = scan_cache(args.compare[1], args.block, zoom)
        sa, sb = set(ga), set(gb)
        report["compare"] = {
            "gaps_A": len(sa), "gaps_B": len(sb),
            "common": sorted(map(list, sa & sb)),
            "only_A": sorted(map(list, sa - sb)),
            "only_B": sorted(map(list, sb - sa)),
        }
        verdict = ("DETERMINISTIC (same gap tiles both runs -> decode/geometry, "
                   "not a race)" if sa == sb and sa else
                   "NON-DETERMINISTIC (gap tiles differ between runs -> a race "
                   "in fetch/assembly)" if sa != sb else
                   "no gaps in either run")
        report["compare"]["verdict"] = verdict
        print(json.dumps(report["compare"], indent=2))
        print("\nVERDICT:", verdict)
        json.dump(report, open(args.out, "w"), indent=2)
        print(f"\nWrote {args.out}")
        return 0

    if not args.cache:
        print("[Error] --cache or --compare required", file=sys.stderr)
        return 1

    gaps, n_tiles, n_blocks = scan_cache(args.cache, args.block, zoom)
    log_tiles = parse_log_tiles(args.log) if args.log else set()
    good_key = sample_good_tile(args.cache, args.block, zoom, set(gaps))

    print(f"Scanned {n_tiles} .abt tile(s): {n_blocks} hole-block(s) in "
          f"{len(gaps)} distinct source tile(s) @ z={zoom}\n")

    # Distinct sentinel values across all holes -> where they came from.
    sentinels: dict = {}
    for recs in gaps.values():
        for (_f, _r, _c, _lat, _lon, kind, val) in recs:
            sentinels[(kind, val)] = sentinels.get((kind, val), 0) + 1
    if sentinels:
        print("Hole sentinels (kind: i16 -> metres, block count):")
        for (kind, val), n in sorted(sentinels.items(), key=lambda kv: -kv[1]):
            print(f"    {kind}: i16={val} ({val * 0.5:.0f} m)  x{n} blocks")
        report["sentinels"] = [
            {"kind": k, "i16": v, "metres": v * 0.5, "blocks": n}
            for (k, v), n in sentinels.items()]
        print()

    probes = []
    print("Re-fetching + decoding hole tiles (and one baseline good tile)…")
    to_probe = list(gaps)[: args.max_fetch]
    for (z, x, y) in to_probe:
        kind, val = gaps[(z, x, y)][0][5], gaps[(z, x, y)][0][6]
        rec = probe_tile(z, x, y, args.url)
        rec.update(in_download_log=(x, y) in log_tiles, role="GAP",
                   hole_kind=kind, hole_i16=val)
        probes.append(rec)
        fmt = f"{rec.get('color_type')}/{rec.get('bit_depth')}bit"
        extra = rec.get("note", "")
        stats = ("" if "elev_mean" not in rec else
                 f" src[{rec['elev_min']}..{rec['elev_max']}] "
                 f"{'FLAT' if rec['flat'] else 'varied'}")
        print(f"  HOLE z{z}/x{x}/y{y}  {kind}({val * 0.5:.0f}m)  "
              f"refetch http={rec['http']} {fmt}{stats}  "
              f"logged={rec['in_download_log']}  {extra}")
    if good_key:
        z, x, y = good_key
        g = probe_tile(z, x, y, args.url)
        g["role"] = "GOOD-baseline"
        probes.append(g)
        print(f"  GOOD z{z}/x{x}/y{y} http={g['http']} "
              f"{g.get('color_type')}/{g.get('bit_depth')}bit "
              f"{'FLAT' if g.get('flat') else 'varied'} "
              f"{g.get('note','')}")

    report.update(n_tiles=n_tiles, n_zero_blocks=n_blocks,
                  n_gap_tiles=len(gaps), probes=probes,
                  gap_tiles=sorted(map(list, gaps)))
    json.dump(report, open(args.out, "w"), indent=2)

    # Verdict hints from what we saw.
    fetched = [p for p in probes if p.get("role") == "GAP"]
    print("\n--- read this ---")
    if not gaps:
        print("No hole blocks found (no <=-2500 m nodata sentinel, no zeros). If "
              "you still see NaN stripes, they're thinner than one --block; lower "
              "--block or tell me and I'll switch to per-row/col nodata scanning.")
    else:
        nod = [p for p in fetched if p.get("hole_kind") == "nodata"]
        good_refetch = [p for p in fetched if p.get("elev_mean") is not None
                        and not p.get("flat") and p.get("http") == 200]
        bad_fmt = [p for p in fetched if "UNSUPPORTED" in p.get("note", "")]
        errs = [p for p in fetched if not (isinstance(p.get("http"), int)
                                           and p["http"] == 200)]
        if nod:
            print(f"* {len(nod)}/{len(fetched)} holes are a NEGATIVE NODATA "
                  "sentinel — the inspector's NaN. A missing XYZ tile fills as 0 "
                  "(valid 0 m), so this is NOT the download path: it's the LOCAL "
                  "DEM / ingest leaving pixels as nodata instead of ground. The "
                  "sentinel value above says the origin (-32768 = never written; "
                  "~-9999 m = a GDAL nodata carried through the warp).")
        if good_refetch:
            print(f"* {len(good_refetch)}/{len(fetched)} hole locations DO have "
                  "real varied terrain on the tile server -> the ground exists; "
                  "the local ingest/warp dropped it (fillable), not a true gap.")
        if bad_fmt:
            print(f"* {len(bad_fmt)}/{len(fetched)} refetched tiles are NOT "
                  "8-bit RGB -> decode_png would mis-handle them.")
        if errs and not good_refetch:
            print(f"* {len(errs)}/{len(fetched)} refetches errored "
                  "(offline, or a genuinely absent source tile).")
        if args.log:
            logged = [p for p in fetched if p.get("in_download_log")]
            print(f"* {len(logged)}/{len(fetched)} holes were flagged in the log.")
        else:
            print("* (pass --log to mark which holes the download flagged.)")
    print(f"\nWrote {args.out} — send me that file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
