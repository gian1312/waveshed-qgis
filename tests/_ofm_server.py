"""A stand-in OpenFreeMap for the building tests: real Mapbox Vector Tiles
over real HTTP.

* :func:`building_tile` encodes a z14 tile the way OpenFreeMap serves one —
  a ``Tile`` with a ``building`` layer of polygon features (``render_height``
  tags) plus an unrelated layer — with a tiny protobuf encoder (no
  dependency), so the plugin's check and the REAL converter decode the same
  bytes.
* :class:`OfmServer` serves ``/planet`` (TileJSON naming
  ``/planet/<snapshot>/{z}/{x}/{y}.pbf``) and the tiles, gzip-encoded when
  the client accepts it (as OpenFreeMap does), and can answer chosen tiles
  with an HTTP error, garbage, nothing, or slowly. Every tile request is
  recorded with its time. Optionally it also serves terrarium terrain tiles
  under ``/terrain/{z}/{x}/{y}.png`` for a real download.
"""

from __future__ import annotations

import gzip
import http.server
import json
import math
import re
import struct
import threading
import time
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# -- protobuf / MVT encoding -------------------------------------------------


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _key(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _ld(field: int, payload: bytes) -> bytes:
    return _key(field, 2) + _varint(len(payload)) + payload


def _zz(n: int) -> int:
    return (n << 1) ^ (n >> 31)


def _packed(field: int, values: Sequence[int]) -> bytes:
    return _ld(field, b"".join(_varint(v) for v in values))


def polygon_geometry(ring: Sequence[Tuple[int, int]]) -> List[int]:
    """MVT command integers of one closed ring (tile coordinates, the
    closing point not repeated)."""
    cmds = [1 | (1 << 3)]
    x0, y0 = ring[0]
    cmds += [_zz(x0), _zz(y0)]
    cmds.append(2 | ((len(ring) - 1) << 3))
    cx, cy = x0, y0
    for x, y in ring[1:]:
        cmds += [_zz(x - cx), _zz(y - cy)]
        cx, cy = x, y
    cmds.append(7 | (1 << 3))
    return cmds


def _feature(geometry: List[int], tags: Sequence[int], gtype: int = 3,
             fid: int = 1) -> bytes:
    return (_key(1, 0) + _varint(fid) + _packed(2, tags)
            + _key(3, 0) + _varint(gtype) + _packed(4, geometry))


def _double_value(v: float) -> bytes:
    return _key(3, 1) + struct.pack("<d", float(v))


def _layer(name: str, features: List[bytes], keys: List[str],
           values: List[bytes], extent: int = 4096) -> bytes:
    body = _ld(1, name.encode("utf-8"))
    for f in features:
        body += _ld(2, f)
    for k in keys:
        body += _ld(3, k.encode("utf-8"))
    for v in values:
        body += _ld(4, v)
    body += _key(5, 0) + _varint(extent) + _key(15, 0) + _varint(2)
    return body


def building_tile(squares: Sequence[Tuple[int, int, int, int, float]] = (),
                  ) -> bytes:
    """One z14 tile: a ``building`` layer with a square polygon per
    ``(x0, y0, x1, y1, render_height)`` (tile coordinates, extent 4096) and
    a ``water`` layer — the shape of an OpenFreeMap tile, uncompressed."""
    features = []
    values = []
    for i, (x0, y0, x1, y1, h) in enumerate(squares):
        ring = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        features.append(_feature(polygon_geometry(ring), [0, i], fid=i + 1))
        values.append(_double_value(h))
    layers = [_layer("water", [_feature(polygon_geometry(
                  [(0, 0), (64, 0), (64, 64), (0, 64)]), [])], [], [])]
    if squares:
        layers.append(_layer("building", features, ["render_height"],
                             values))
    return b"".join(_ld(3, layer) for layer in layers)


def tile_lonlat(z: int, x: int, y: int, px: float, py: float
                ) -> Tuple[float, float]:
    """``(lon, lat)`` of tile coordinate (px, py) — the converter's own
    mapping (mvt.rs ``decode_geometry``: lon linear in x, lat linear between
    the tile's north and south edges)."""
    n = 1 << z

    def lat_of(row: float) -> float:
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * row / n))))

    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north, south = lat_of(y), lat_of(y + 1)
    return (west + px / 4096.0 * (east - west),
            north + py / 4096.0 * (south - north))


# -- the server ----------------------------------------------------------------

_TILE_RE = re.compile(r"^/planet/(?P<snap>[^/]+)/(?P<z>\d+)/(?P<x>\d+)/"
                      r"(?P<y>\d+)\.pbf$")
_TERRAIN_RE = re.compile(r"^/terrain/(?P<z>\d+)/(?P<x>\d+)/(?P<y>\d+)\.png$")


class OfmServer:
    """``with OfmServer(...) as srv:`` — a threaded local OpenFreeMap.

    *tiles* ``{(x, y): bytes}`` (uncompressed MVT) answers those z14 tiles;
    any other tile gets *default* (bytes, or None = an empty 200 answer —
    OpenFreeMap's "no data"). *fail* ``{(x, y): status}`` answers an HTTP
    error (every attempt, or the first *fail_times* attempts); *garbage*
    tiles get an HTML page with status 200; *delay_s* holds every tile
    answer that long (*delay_fn(x, y)* per tile). *terrain(z, x, y)* →
    PNG bytes serves ``/terrain``; *tilejson_status* / *tilejson_delay_s*
    shape the ``/planet`` answer; *drop_idle* closes every connection after
    one answer WITHOUT saying so (a keep-alive connection the server dropped
    while idle). ``connections`` counts the TCP connections accepted;
    ``requests`` lists ``(time, x, y)`` of
    every building-tile request, ``terrain_requests`` the terrain ones."""

    SNAPSHOT = "20261018_080001_pt"

    def __init__(self, tiles: Optional[Dict[Tuple[int, int], bytes]] = None,
                 default: Optional[bytes] = None,
                 fail: Optional[Dict[Tuple[int, int], int]] = None,
                 fail_times: Optional[int] = None,
                 garbage: Sequence[Tuple[int, int]] = (),
                 delay_s: float = 0.0,
                 delay_fn: Optional[Callable[[int, int], float]] = None,
                 terrain: Optional[Callable[[int, int, int], bytes]] = None,
                 tilejson_status: int = 200,
                 tilejson_delay_s: float = 0.0,
                 drop_idle: bool = False) -> None:
        self.tiles = dict(tiles or {})
        self.default = default
        self.fail = dict(fail or {})
        self.fail_times = fail_times
        self.garbage = set(garbage)
        self.delay_s = delay_s
        self.delay_fn = delay_fn
        self.terrain = terrain
        self.tilejson_status = tilejson_status
        self.tilejson_delay_s = tilejson_delay_s
        self.drop_idle = drop_idle
        self.connections = 0
        self.requests: List[Tuple[float, int, int]] = []
        self.terrain_requests: List[Tuple[float, int, int, int]] = []
        self.tilejson_requests = 0
        self._attempts: Dict[Tuple[int, int], int] = {}
        self._lock = threading.Lock()
        self._server: Optional[http.server.ThreadingHTTPServer] = None

    # -- URLs
    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def tilejson_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/planet"

    @property
    def template(self) -> str:
        return (f"http://127.0.0.1:{self.port}/planet/{self.SNAPSHOT}/"
                "{z}/{x}/{y}.pbf")

    def terrain_uri(self, zmax: int = 12) -> str:
        return ("type=xyz&url=http%3A//127.0.0.1%3A"
                f"{self.port}/terrain/%7Bz%7D/%7Bx%7D/%7By%7D.png"
                f"&zmax={zmax}&interpretation=terrarium")

    def requested(self) -> set:
        with self._lock:
            return {(x, y) for _t, x, y in self.requests}

    # -- lifecycle
    def __enter__(self) -> "OfmServer":
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_a):
                pass

            def setup(self):
                super().setup()
                with outer._lock:
                    outer.connections += 1

            def _send(self, status: int, body: bytes,
                      ctype: str = "application/vnd.mapbox-vector-tile",
                      headers: Optional[Dict[str, str]] = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):  # noqa: N802 — http.server API
                path = self.path.split("?", 1)[0]
                if path == "/planet":
                    with outer._lock:
                        outer.tilejson_requests += 1
                    if outer.tilejson_delay_s:
                        time.sleep(outer.tilejson_delay_s)
                    if outer.tilejson_status != 200:
                        self._send(outer.tilejson_status, b"down",
                                   "text/plain")
                        return
                    doc = {"tilejson": "3.0.0", "tiles": [outer.template],
                           "maxzoom": 14}
                    self._send(200, json.dumps(doc).encode(),
                               "application/json")
                    return
                m = _TERRAIN_RE.match(path)
                if m and outer.terrain is not None:
                    z, x, y = (int(m.group(k)) for k in "zxy")
                    with outer._lock:
                        outer.terrain_requests.append(
                            (time.monotonic(), z, x, y))
                    self._send(200, outer.terrain(z, x, y), "image/png")
                    return
                m = _TILE_RE.match(path)
                if not m or m.group("snap") != outer.SNAPSHOT:
                    self._send(404, b"", "text/plain")
                    return
                x, y = int(m.group("x")), int(m.group("y"))
                with outer._lock:
                    outer.requests.append((time.monotonic(), x, y))
                    outer._attempts[(x, y)] = outer._attempts.get(
                        (x, y), 0) + 1
                    attempt = outer._attempts[(x, y)]
                delay = (outer.delay_fn(x, y) if outer.delay_fn is not None
                         else outer.delay_s)
                if delay:
                    time.sleep(delay)
                status = outer.fail.get((x, y))
                if status and (outer.fail_times is None
                               or attempt <= outer.fail_times):
                    self._send(status, b"busy", "text/plain",
                               {"Retry-After": "0"} if status in (429, 503)
                               else None)
                    return
                if (x, y) in outer.garbage:
                    self._send(200, b"<html><body>502 Bad Gateway</body>"
                                    b"</html>", "text/html")
                    return
                body = outer.tiles.get((x, y), outer.default)
                if not body:
                    self._send(200, b"")
                    return
                if "gzip" in (self.headers.get("Accept-Encoding") or ""):
                    self._send(200, gzip.compress(body),
                               headers={"Content-Encoding": "gzip"})
                else:
                    self._send(200, body)
                if outer.drop_idle:
                    self.close_connection = True

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 512

        self._server = Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever,
                         daemon=True).start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()
