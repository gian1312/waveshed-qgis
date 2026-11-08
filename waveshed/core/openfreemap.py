"""Building footprints from OpenFreeMap vector tiles.

The Aether converter can burn building footprints into the terrain surface. It
reads them either from FlatGeobuf (absolute roof elevations in the geometry Z)
or from Mapbox Vector Tiles (a height above ground). This module fetches the
latter from `OpenFreeMap <https://openfreemap.org>`_ — the same source the
Waveshed web app uses, so both platforms model the same buildings.

Tiles are written as ``{z}_{x}_{y}.pbf``, which is the layout
``aether_converter``'s ``buildings_pbf_dir`` job field expects.

The data is OpenStreetMap-derived and carries an attribution requirement; see
:data:`ATTRIBUTION`. No API key is involved.
"""

from __future__ import annotations

import email.utils
import http.client
import json
import math
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import (Any, Callable, Dict, Iterable, List, NamedTuple, Optional,
                    Tuple)

# TileJSON endpoint. The tile URL it returns embeds a snapshot timestamp, so it
# has to be read at runtime rather than hard-coded — every fetch reads it
# (resolve_tile_template_strict); there is no fixed fallback URL: a guessed
# or stale snapshot path can answer every tile with "no data".
TILEJSON_URL = "https://tiles.openfreemap.org/planet"

# OpenMapTiles only carries the `building` layer from z14, so a coarser zoom
# would silently yield no buildings at all.
BUILDING_ZOOM = 14

# ODbL requires this to be shown wherever the data is presented.
ATTRIBUTION = (
    "Building data © OpenStreetMap contributors, © OpenMapTiles, "
    "© OpenFreeMap — ODbL"
)

_USER_AGENT = "waveshed-qgis"

# -- The strict fetch (:func:`fetch_building_tiles`; every caller goes
# through core/osm_buildings.ensure_tiles) ----------------------------------
#
# A Map Converter extent is easily 50,000 z14 tiles (16.6 deg² over the Alps:
# 50,796), so the fetch runs FETCH_WORKERS kept-alive connections at once
# (:class:`_TileClient`; measured 149 tiles/s steady from the container —
# ~6 min for that extent). It ends at the first tile that still fails after
# its attempts: every tile is either in the store, or answered "no data" by
# the server, or the run stops. No tile cap: a z14 tile covers ~2.4 km, so a
# large area is a large (but parallel, resumable) download.

#: Parallel connections (worker threads) of one strict fetch.
FETCH_WORKERS = 32
#: Socket timeout of one attempt (connect, and every read).
TILE_TIMEOUT_S = 20.0
#: Wall-clock cap of one attempt's whole answer (a trickling body).
TILE_DEADLINE_S = 90.0
#: Attempts per tile (the first and the retries) ...
TILE_ATTEMPTS = 4
#: ... and the pause before retry 1, 2, 3 (a 429/503 ``Retry-After`` wins,
#: capped at :data:`RETRY_AFTER_MAX_S`).
RETRY_BACKOFF_S = (1.0, 3.0, 8.0)
RETRY_AFTER_MAX_S = 30.0
#: After Stop or a failure, in-flight requests get this long to end before
#: the fetch returns without them (their answers are dropped, never written).
CANCEL_GRACE_S = 2.0
#: Attempts to read TileJSON before a strict fetch gives up.
TILEJSON_ATTEMPTS = 3


class FetchCancelled(Exception):
    """Stop arrived while building tiles were being fetched."""


class FetchFailed(RuntimeError):
    """The strict fetch could not provide every tile.

    *missing* tiles are not in the store (the failed one and every one not
    fetched yet); *total* were asked for; *cause* is the first failure."""

    def __init__(self, message: str, missing: int = 0, total: int = 0,
                 cause: str = "") -> None:
        super().__init__(message)
        self.missing = missing
        self.total = total
        self.cause = cause


class FetchResult(NamedTuple):
    """What :func:`fetch_building_tiles` delivered."""
    total: int              #: distinct tiles asked for
    cached: int             #: already in the store (no request)
    fetched: int            #: downloaded now, with data
    empty: int              #: the server has no data there (200/204 empty, 404)
    nbytes: int             #: bytes of every tile with data (cached + fetched)
    seconds: float          #: wall time of the fetch
    present: Tuple[str, ...]  #: file names with data in the store, sorted


def lon_to_tile_x(lon: float, zoom: int) -> int:
    """Slippy-map tile column containing *lon*."""
    n = 1 << zoom
    x = int((lon + 180.0) / 360.0 * n)
    return max(0, min(n - 1, x))


def lat_to_tile_y(lat: float, zoom: int) -> int:
    """Slippy-map tile row containing *lat* (Web Mercator)."""
    n = 1 << zoom
    lat = max(-85.05112878, min(85.05112878, lat))
    rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(rad)) / math.pi) / 2.0 * n)
    return max(0, min(n - 1, y))


def tiles_for_bbox(bbox: Dict[str, float],
                   zoom: int = BUILDING_ZOOM) -> List[Tuple[int, int, int]]:
    """Every ``(z, x, y)`` tile covering *bbox*."""
    x0 = lon_to_tile_x(bbox["west"], zoom)
    x1 = lon_to_tile_x(bbox["east"], zoom)
    # Tile rows count southwards, so the north edge gives the smaller index.
    y0 = lat_to_tile_y(bbox["north"], zoom)
    y1 = lat_to_tile_y(bbox["south"], zoom)
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    return [(zoom, x, y)
            for y in range(y0, y1 + 1)
            for x in range(x0, x1 + 1)]


def estimate_tile_count(bbox: Dict[str, float],
                        zoom: int = BUILDING_ZOOM) -> int:
    """Tile count for *bbox* without building the list."""
    x0 = lon_to_tile_x(bbox["west"], zoom)
    x1 = lon_to_tile_x(bbox["east"], zoom)
    y0 = lat_to_tile_y(bbox["north"], zoom)
    y1 = lat_to_tile_y(bbox["south"], zoom)
    return (abs(x1 - x0) + 1) * (abs(y1 - y0) + 1)


def _describe(exc: BaseException) -> str:
    """A failure in words: ``HTTP 503 Service Unavailable``, ``timed out``,
    ``connection refused`` — never a bare class name."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code} {exc.reason}".strip()
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        return _describe(reason) if isinstance(reason, BaseException) \
            else str(reason)
    if isinstance(exc, (TimeoutError,)) or "timed out" in str(exc).lower():
        return str(exc) or "timed out"
    if isinstance(exc, http.client.IncompleteRead):
        return (f"answer cut off after {len(exc.partial):,} bytes"
                + (f" of {exc.expected + len(exc.partial):,}"
                   if exc.expected else ""))
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def resolve_tile_template_strict(
    timeout: float = 15.0,
    attempts: Optional[int] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> str:
    """The tile URL template TileJSON names NOW — for a strict fetch.

    There is no fallback and no reuse of a session-cached answer:
    OpenFreeMap serves each snapshot under its own path, and a fixed URL (or
    a snapshot path gone stale in a long session) can answer every tile with
    "no data" — buildings that silently are not there. Read *attempts* times
    (1 s, 2 s apart); still unreadable → :class:`FetchFailed` naming the
    cause.
    """
    attempts = TILEJSON_ATTEMPTS if attempts is None else attempts
    last = ""
    for attempt in range(1, attempts + 1):
        if should_cancel is not None and should_cancel():
            raise FetchCancelled()
        box: Dict[str, Any] = {}

        def read() -> None:
            try:
                req = urllib.request.Request(
                    TILEJSON_URL, headers={"User-Agent": _USER_AGENT})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    box["doc"] = json.loads(resp.read().decode("utf-8"))
            except Exception as exc:  # noqa: BLE001 — named below
                box["exc"] = exc

        # On a helper thread, so Stop is seen within 0.1 s even while a
        # dead endpoint holds the request for its whole timeout.
        reader = threading.Thread(target=read, daemon=True,
                                  name="waveshed-tilejson")
        reader.start()
        while reader.is_alive():
            reader.join(0.1)
            if should_cancel is not None and should_cancel():
                raise FetchCancelled()
        if "exc" in box:
            last = _describe(box["exc"])
        else:
            doc = box.get("doc")
            tiles = doc.get("tiles") if isinstance(doc, dict) else None
            first = tiles[0] if tiles else None
            if (isinstance(first, str)
                    and all(k in first for k in ("{z}", "{x}", "{y}"))):
                return first
            last = "it names no {z}/{x}/{y} tile URL"
        if attempt < attempts:
            stop_at = time.monotonic() + float(attempt)
            while time.monotonic() < stop_at:
                if should_cancel is not None and should_cancel():
                    raise FetchCancelled()
                time.sleep(0.05)
    raise FetchFailed(
        f"the OpenFreeMap TileJSON ({TILEJSON_URL}) could not be read after "
        f"{attempts} attempts: {last}", cause=last)


# -- Mapbox Vector Tile check (what the converter's decoder needs) ----------

class _BadTile(ValueError):
    pass


def _varint(buf: bytes, i: int, end: int) -> Tuple[int, int]:
    shift = 0
    value = 0
    while True:
        if i >= end:
            raise _BadTile("truncated varint")
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if b < 0x80:
            return value, i
        shift += 7
        if shift >= 70:
            raise _BadTile("varint longer than 10 bytes")


def _fields(buf: bytes, start: int, end: int):
    """``(field, wire type, value or (start, end))`` of one protobuf message
    in ``buf[start:end]``; framing errors raise :class:`_BadTile`."""
    i = start
    while i < end:
        key, i = _varint(buf, i, end)
        field, wire = key >> 3, key & 7
        if field == 0:
            raise _BadTile("field number 0")
        if wire == 0:
            value, i = _varint(buf, i, end)
            yield field, wire, value
        elif wire == 1:
            if i + 8 > end:
                raise _BadTile("truncated 64-bit field")
            i += 8
            yield field, wire, None
        elif wire == 2:
            n, i = _varint(buf, i, end)
            if i + n > end:
                raise _BadTile(f"field {field} runs {i + n - end} bytes past "
                               f"its message")
            yield field, wire, (i, i + n)
            i += n
        elif wire == 5:
            if i + 4 > end:
                raise _BadTile("truncated 32-bit field")
            i += 4
            yield field, wire, None
        else:
            raise _BadTile(f"wire type {wire} (field {field})")


def _packed(buf: bytes, span: Tuple[int, int], what: str) -> None:
    a, b = span
    if b > a and buf[b - 1] >= 0x80:
        raise _BadTile(f"{what} ends inside a varint")


def _check_layer(buf: bytes, start: int, end: int) -> None:
    """One ``Layer``: its name must be text; the ``building`` layer (the one
    the converter reads) is walked field by field, every other one only
    until its name is known."""
    name: Optional[str] = None
    deferred: List[Tuple[int, int, Any]] = []
    for field, wire, value in _fields(buf, start, end):
        if field == 1:
            if wire != 2:
                raise _BadTile("layer name is not a string")
            try:
                name = bytes(buf[value[0]:value[1]]).decode("utf-8")
            except UnicodeDecodeError:
                raise _BadTile("layer name is not UTF-8") from None
            if name != "building":
                return
            for item in deferred:
                _check_layer_field(buf, *item)
            deferred = []
            continue
        if name is None:
            deferred.append((field, wire, value))
        else:
            _check_layer_field(buf, field, wire, value)
    if name is None:
        raise _BadTile("a layer has no name")


def _check_layer_field(buf: bytes, field: int, wire: int, value: Any) -> None:
    """A field of the ``building`` layer: features, keys, values, extent,
    version — the wire types the converter's decoder insists on."""
    if field == 2:                                  # Feature
        if wire != 2:
            raise _BadTile("building feature is not a message")
        for f, w, v in _fields(buf, value[0], value[1]):
            if f in (1, 3) and w != 0:
                raise _BadTile("building feature id/type is not a varint")
            if f in (2, 4):
                if w != 2:
                    raise _BadTile("building feature tags/geometry is not "
                                   "packed")
                _packed(buf, v, "building geometry" if f == 4 else
                        "building tags")
    elif field == 3:                                # key
        if wire != 2:
            raise _BadTile("building key is not a string")
        try:
            bytes(buf[value[0]:value[1]]).decode("utf-8")
        except UnicodeDecodeError:
            raise _BadTile("building key is not UTF-8") from None
    elif field == 4:                                # Value
        if wire != 2:
            raise _BadTile("building value is not a message")
        for f, w, v in _fields(buf, value[0], value[1]):
            if f == 1 and w == 2:
                try:
                    bytes(buf[v[0]:v[1]]).decode("utf-8")
                except UnicodeDecodeError:
                    raise _BadTile("building value is not UTF-8") from None
    elif field in (5, 15) and wire != 0:
        raise _BadTile("building layer extent/version is not a varint")


def tile_problem(data: bytes) -> Optional[str]:
    """Why *data* is not a vector tile the converter can decode, else None.

    gzip (OpenFreeMap answers ``Accept-Encoding: gzip`` with it; the
    converter un-gzips) is checked whole, CRC included; then the protobuf
    framing of the ``Tile`` and its ``Layer`` messages, and the ``building``
    layer field by field. An empty body is a valid empty tile. An HTML or
    JSON error page, a body cut off mid-message and corrupt gzip all fail
    here. Cost: a few ms for a dense city tile (zlib releases the GIL).
    """
    raw = data
    if data[:2] == b"\x1f\x8b":
        try:
            raw = zlib.decompress(data, 16 + zlib.MAX_WBITS)
        except zlib.error as exc:
            return f"corrupt gzip ({exc})"
    try:
        for field, wire, value in _fields(raw, 0, len(raw)):
            if field == 3:
                if wire != 2:
                    raise _BadTile("layer is not a message")
                _check_layer(raw, value[0], value[1])
    except _BadTile as exc:
        head = bytes(raw[:16])
        return (f"not a vector tile ({exc}; {len(raw):,} bytes, starts "
                f"{head!r})")
    return None


def _write_tile(dest_dir: str, name: str, data: bytes) -> None:
    """Write one tile into *dest_dir* atomically: a temp name of this
    process and thread, then ``os.replace`` — an interrupted write never
    leaves a tile a later run takes as downloaded, and two writers of one
    tile (both tabs, two QGIS) never share a temp file."""
    dest = os.path.join(dest_dir, name)
    tmp = f"{dest}.{os.getpid()}.{threading.get_ident()}.part"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def tile_url(template: str, z: int, x: int, y: int) -> str:
    """Substitute tile coordinates into a ``{z}/{x}/{y}`` template."""
    return (template
            .replace("{z}", str(z))
            .replace("{x}", str(x))
            .replace("{y}", str(y)))


def tile_filename(z: int, x: int, y: int) -> str:
    """Name the converter's ``buildings_pbf_dir`` parser expects."""
    return f"{z}_{x}_{y}.pbf"


# ---------------------------------------------------------------------------
# The strict fetch: every tile, in parallel, or an error
# ---------------------------------------------------------------------------

def store_listing(store_dir: str) -> Dict[str, int]:
    """``{file name: size}`` of the non-empty tiles in *store_dir* — ONE
    directory listing (sizes are free on Windows), no per-tile stat."""
    out: Dict[str, int] = {}
    try:
        with os.scandir(store_dir) as it:
            for entry in it:
                if not entry.name.endswith(".pbf"):
                    continue
                try:
                    if entry.is_file():
                        size = entry.stat().st_size
                        if size > 0:
                            out[entry.name] = size
                except OSError:
                    continue
    except OSError:
        pass
    return out


def _retry_after(headers: Any) -> Optional[float]:
    """Seconds a 429/503 ``Retry-After`` asks for (delta or HTTP date)."""
    value = headers.get("Retry-After") if headers is not None else None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
        return max(0.0, when.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def _read_body(resp: Any, t0: float, deadline: float) -> bytes:
    """The whole body of *resp*, read in chunks against *deadline* (seconds
    since *t0* for the whole answer), so a trickling server cannot hold a
    worker past it; a body cut off before its length raises
    :class:`http.client.IncompleteRead`."""
    chunks: List[bytes] = []
    while True:
        if time.monotonic() - t0 > deadline:
            raise TimeoutError(f"no complete answer within {deadline:.0f} s")
        chunk = resp.read(1 << 16)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _http_get(url: str, timeout: float, deadline: float) -> Tuple[int, bytes]:
    """``(status, body)`` of one GET through urllib (proxy settings and
    redirects honoured, one connection per request); HTTP errors raise
    :class:`urllib.error.HTTPError`."""
    req = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "gzip"})
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = int(getattr(resp, "status", 200) or 200)
        return status, _read_body(resp, t0, deadline)


#: A keep-alive connection the server closed while idle fails the next
#: request with one of these — retried once on a new connection.
_STALE_CONNECTION = (http.client.BadStatusLine, http.client.CannotSendRequest,
                     ConnectionResetError, ConnectionAbortedError,
                     BrokenPipeError)


class _TileClient:
    """One fetch worker's connection to the tile host.

    Keep-alive: one TLS handshake serves many tiles. Measured 2026-10-22
    from the container against tiles.openfreemap.org: a NEW connection per
    tile (urllib) tops out at 11-24 tiles/s whatever the parallelism (the
    server accepts only ~15-20 new connections per second from one
    address), while 16 kept-alive connections answer a tile in 0.055 s
    each (138 tiles/s including their own setup) — the difference between
    ~1 hour and a few minutes for a 50,000-tile extent. Through a proxy
    (``urllib.request.getproxies()`` names one for the scheme and the host
    is not bypassed) and for redirects, every request goes through urllib
    instead, exactly as before.
    """

    def __init__(self, template: str, timeout: float) -> None:
        parts = urllib.parse.urlsplit(tile_url(template, 0, 0, 0))
        self.scheme = (parts.scheme or "").lower()
        self.host = parts.hostname or ""
        self.port = parts.port
        self.timeout = timeout
        self.conn: Any = None
        try:
            self.proxied = (bool(urllib.request.getproxies().get(self.scheme))
                            and not urllib.request.proxy_bypass(self.host))
        except Exception:  # noqa: BLE001 — unknown: the proxy-aware path
            self.proxied = True

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001 — closing is best effort
                pass
            self.conn = None

    def get(self, url: str, deadline: float) -> Tuple[int, bytes]:
        """``(status, body)`` like :func:`_http_get`."""
        if self.proxied or self.scheme not in ("http", "https"):
            return _http_get(url, self.timeout, deadline)
        parts = urllib.parse.urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query
                                      else "")
        headers = {"User-Agent": _USER_AGENT, "Accept-Encoding": "gzip"}
        for _attempt in (1, 2):
            fresh = self.conn is None
            if fresh:
                cls = (http.client.HTTPSConnection if self.scheme == "https"
                       else http.client.HTTPConnection)
                self.conn = cls(self.host, self.port, timeout=self.timeout)
            t0 = time.monotonic()
            try:
                self.conn.request("GET", path, headers=headers)
                resp = self.conn.getresponse()
                body = _read_body(resp, t0, deadline)
            except _STALE_CONNECTION:
                self.close()
                if fresh:
                    raise
                continue
            except BaseException:
                self.close()
                raise
            if resp.will_close:
                self.close()
            if 300 <= resp.status < 400:
                return _http_get(url, self.timeout, deadline)
            if resp.status >= 400:
                raise urllib.error.HTTPError(url, resp.status, resp.reason,
                                             resp.headers, None)
            return resp.status, body
        raise http.client.RemoteDisconnected(
            "the server closed the connection twice")


def _fetch_one(client: "_TileClient", template: str,
               spec: Tuple[int, int, int], store_dir: str, deadline: float,
               attempts: int, stop: threading.Event
               ) -> Tuple[str, str, str, int]:
    """``(kind, name, detail, bytes)`` of one tile: ``ok`` (written),
    ``empty`` (the server has no data there), ``failed`` (*detail* says
    why, after *attempts*), ``cancelled`` (*stop* was set — nothing is
    written after it)."""
    z, x, y = spec
    name = tile_filename(z, x, y)
    url = tile_url(template, z, x, y)
    last = ""
    for attempt in range(1, attempts + 1):
        if stop.is_set():
            return ("cancelled", name, "", 0)
        wait: Optional[float] = None
        try:
            status, data = client.get(url, deadline)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return ("empty", name, "HTTP 404", 0)
            last = _describe(exc)
            if exc.code in (429, 503):
                wait = _retry_after(exc.headers)
        except Exception as exc:  # noqa: BLE001 — timeout, reset, refused…
            last = _describe(exc)
        else:
            if status == 204 or not data:
                return ("empty", name, f"HTTP {status}, no data", 0)
            problem = tile_problem(data)
            if problem is None:
                if stop.is_set():
                    return ("cancelled", name, "", 0)
                try:
                    _write_tile(store_dir, name, data)
                except OSError as exc:
                    # A full or vanished disk: retrying does not help.
                    return ("failed", name,
                            f"could not be written to {store_dir} ({exc})", 0)
                return ("ok", name, "", len(data))
            last = f"cannot be decoded: {problem}"
        if attempt < attempts:
            pause = (wait if wait is not None
                     else RETRY_BACKOFF_S[min(attempt, len(RETRY_BACKOFF_S))
                                          - 1])
            if stop.wait(min(pause, RETRY_AFTER_MAX_S)):
                return ("cancelled", name, "", 0)
    return ("failed", name, f"{last} (after {attempts} attempts)", 0)


def fetch_building_tiles(
    tiles: Iterable[Tuple[int, int, int]],
    store_dir: str,
    template: str,
    workers: Optional[int] = None,
    timeout: Optional[float] = None,
    deadline: Optional[float] = None,
    attempts: Optional[int] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    on_start: Optional[Callable[[int, int], None]] = None,
    on_progress: Optional[Callable[[int, int, int], None]] = None,
    require_data: bool = True,
) -> FetchResult:
    """Make *store_dir* hold every tile of *tiles* that has data — or fail.

    No tile limit, no silent gaps: tiles already in the store (one listing)
    cost nothing; the rest are fetched by *workers* threads at once, each
    tile up to *attempts* times (:data:`RETRY_BACKOFF_S`, ``Retry-After``),
    every answer checked (:func:`tile_problem`) before it is written
    (:func:`_write_tile`). HTTP 404 and an empty 200/204 answer mean the
    server has no data there — fine for one tile; a fetch where NO tile
    has data is a :class:`FetchFailed` (cause ``"no tile with data"``)
    unless *require_data* is False (then ``present`` is empty), as is the
    first tile that still fails (HTTP error, timeout, undecodable answer,
    write error): the fetch stops at once, naming it and how many tiles are
    missing.

    *should_cancel()* is polled every 0.1 s → :class:`FetchCancelled`; after
    Stop or a failure, requests in flight get :data:`CANCEL_GRACE_S` to end
    and nothing more is written. *on_start(cached, to_fetch)* is called once
    after the listing, *on_progress(done, to_fetch, bytes)* after every
    fetched tile — both on the calling thread. *workers*, *timeout*,
    *deadline*, *attempts* default to the module's constants at call time.
    """
    workers = FETCH_WORKERS if workers is None else workers
    timeout = TILE_TIMEOUT_S if timeout is None else timeout
    deadline = TILE_DEADLINE_S if deadline is None else deadline
    attempts = TILE_ATTEMPTS if attempts is None else attempts
    t0 = time.monotonic()
    os.makedirs(store_dir, exist_ok=True)
    wanted: Dict[str, Tuple[int, int, int]] = {}
    for spec in tiles:
        wanted.setdefault(tile_filename(*spec), tuple(spec))
    listing = store_listing(store_dir)
    cached = sorted(n for n in wanted if n in listing)
    todo = [wanted[n] for n in sorted(wanted) if n not in listing]
    if on_start is not None:
        on_start(len(cached), len(todo))
    fetched: List[str] = []
    empty = 0
    new_bytes = 0
    if todo:
        stop = threading.Event()
        results: "queue.Queue[Tuple[str, str, str, int]]" = queue.Queue()
        pending = iter(todo)
        lock = threading.Lock()

        def work() -> None:
            client = _TileClient(template, timeout)
            try:
                while not stop.is_set():
                    with lock:
                        spec = next(pending, None)
                    if spec is None:
                        return
                    try:
                        outcome = _fetch_one(client, template, spec,
                                             store_dir, deadline, attempts,
                                             stop)
                    except BaseException as exc:  # noqa: BLE001 — reported
                        outcome = ("failed", tile_filename(*spec),
                                   f"unexpected error: {_describe(exc)}", 0)
                    results.put(outcome)
            finally:
                client.close()

        threads = [threading.Thread(target=work, daemon=True,
                                    name=f"waveshed-buildings-{i}")
                   for i in range(max(1, min(int(workers), len(todo))))]
        for th in threads:
            th.start()
        done = 0
        try:
            while done < len(todo):
                if should_cancel is not None and should_cancel():
                    raise FetchCancelled()
                try:
                    kind, name, detail, nbytes = results.get(timeout=0.1)
                except queue.Empty:
                    if not any(th.is_alive() for th in threads) \
                            and results.empty():
                        raise FetchFailed(
                            "the download threads ended before every tile "
                            "was fetched",
                            missing=len(wanted) - len(cached) - len(fetched)
                            - empty, total=len(wanted))
                    continue
                if kind == "cancelled":
                    # Only after `stop` — set below, on the way out.
                    continue
                done += 1
                if kind == "ok":
                    fetched.append(name)
                    new_bytes += nbytes
                elif kind == "empty":
                    empty += 1
                else:
                    missing = len(wanted) - len(cached) - len(fetched) - empty
                    raise FetchFailed(
                        f"{missing:,} of {len(wanted):,} building tiles "
                        f"missing — tile {name}: {detail}",
                        missing=missing, total=len(wanted),
                        cause=f"{name}: {detail}")
                if on_progress is not None:
                    on_progress(done, len(todo), new_bytes)
        finally:
            stop.set()
            grace = time.monotonic() + CANCEL_GRACE_S
            for th in threads:
                th.join(max(0.0, grace - time.monotonic()))
    present = tuple(sorted(cached + fetched))
    if not present and require_data:
        raise FetchFailed(
            f"none of the {len(wanted):,} building tiles has data — the "
            f"server answered every one with \"no data\" ({template})",
            missing=len(wanted), total=len(wanted),
            cause="no tile with data")
    nbytes = sum(listing[n] for n in cached) + new_bytes
    return FetchResult(len(wanted), len(cached), len(fetched), empty, nbytes,
                       time.monotonic() - t0, present)
