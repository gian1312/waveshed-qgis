"""OpenFreeMap z14 buildings: ONE code path for every terrain route.

The Map Converter's Download Buildings… row and Site Analysis's "Include
buildings" (360° and range profile, every band) burn the same OpenFreeMap
z14 vector tiles through the converter's ``buildings_pbf_dir`` (toolkit
CONTRACT §9a: an ingest field and a download field). Everything they share
lives here, GUI-free:

* :class:`BuildingCover` — which z14 tiles a run needs: per output tile the
  cover tiles whose buildings can reach it (the tile grown by
  :data:`REACH_DEG`), or every tile of a plain list of extents;
* :func:`ensure_tiles` — the strict fetch into THE store
  (``terrain_adapter.osm_buildings_store_dir``): afterwards every tile is in
  the store or was answered "no data" by the server — else
  ``openfreemap.FetchFailed`` (cause + missing count);
* :func:`batch_view` — the directory one converter run is handed: exactly
  the fetched tiles it needs (the converter decodes every file in it);
  :func:`ready_view` / :func:`complete_view` — such a directory named
  before its fetch ends, for a converter that waits for its marker file
  (:func:`write_ready_marker`; a download from aether_converter 0.2.10,
  toolkit CONTRACT changelog 45 / §9b; an ingest from 0.2.12, item 48);
  :func:`ready_list` / :func:`complete_list` — from 0.2.14 (changelog 51)
  no directory at all: a list file of the tiles, read straight from the
  store, which is the ready signal too (:func:`write_building_list`);
* :class:`BuildingsFetch` — that fetch in its own thread, next to the
  terrain work: the Map Converter's ``_BuildingsBranch`` (each ingest
  batch waits for its own tiles — or, on >= 0.2.12, is launched at once
  and released through :meth:`BuildingsFetch.link_when_ready`, on
  >= 0.2.14 :meth:`BuildingsFetch.list_when_ready`) and Site
  Analysis's download / ingest (``terrain_adapter._BuildingsBesideConverter``);
* :class:`BurnMonitor` — the converter's own lines, read: a tile it could
  not decode (it skips it and goes on), a run that carried the field and
  reported no burn (an engine ignoring it), buildings decoded but none
  drawn over a whole run.
"""

from __future__ import annotations

import os
import re
import threading
import time
from typing import (Any, Callable, Dict, Iterable, List, Optional, Sequence,
                    Tuple)

from . import hashes, openfreemap, terrain_adapter

#: The source id building pools and Map Converter fingerprints are keyed on.
SOURCE_ID = terrain_adapter._OSM_BUILDINGS_SOURCE_ID

#: How far past its own edge a z14 tile's buildings can reach an output
#: tile — the vector-tile buffer (OpenMapTiles: 4 px of 256 = 1/64 tile),
#: taken at 1/16 tile.
REACH_DEG = 360.0 / (1 << openfreemap.BUILDING_ZOOM) / 16.0

#: Bytes one z14 tile is taken to occupy in the store when a run is priced —
#: an upper-end guess (OpenFreeMap answers gzip; a dense city tile is
#: ~30-60 KB, countryside a few KB).
TILE_BYTES_ESTIMATE = 50 * 1024

#: ``FetchFailed.cause`` of a fetch none of whose tiles has data
#: (``openfreemap.fetch_building_tiles``, :class:`BuildingsFetch`).
NO_DATA_CAUSE = "no tile with data"

#: The marker file a download job waits for before it burns its
#: ``buildings_pbf_dir`` (job field ``buildings_pbf_ready``, aether_converter
#: >= 0.2.10, toolkit CONTRACT changelog 45 / §9b): written into the view
#: LAST, once every tile is linked (:func:`write_ready_marker`). Not a
#: ``{z}_{x}_{y}.pbf`` name, so nothing decodes it.
READY_MARKER = "_ready"

#: ``buildings_pbf_wait_s``: how long the converter waits for the marker
#: (or, aether_converter >= 0.2.14, the ``buildings_pbf_list`` file)
#: before it fails the run (``Error: Building tiles not ready: …``), sent
#: explicitly: four hours (owner, 2026-11-05; the contract's own default is
#: 1800 s). The clock runs from the batch's start, terrain time included;
#: a fetch that fails or is stopped kills the converter at once (both
#: tabs), so this only has to catch a fetch that hangs.
READY_WAIT_S = 14400


def _union(bounds: Iterable[Dict[str, float]]) -> Optional[Dict[str, float]]:
    out: Optional[Dict[str, float]] = None
    for b in bounds:
        if not b:
            continue
        if out is None:
            out = {k: float(b[k]) for k in ("south", "north", "west", "east")}
            continue
        out["south"] = min(out["south"], b["south"])
        out["north"] = max(out["north"], b["north"])
        out["west"] = min(out["west"], b["west"])
        out["east"] = max(out["east"], b["east"])
    return out


def _output_tile_bbox(res: int, tile: Dict[str, Any]) -> Dict[str, float]:
    """WGS84 bbox of one enumerated output tile — the Map Converter's
    arithmetic (``_tile_bbox_of``: the resolution's tile extent)."""
    sub = terrain_adapter._subtile_degrees(res)
    return {"north": tile["ul_lat"], "south": tile["ul_lat"] - sub,
            "west": tile["ul_lon"], "east": tile["ul_lon"] + sub}


class BuildingCover:
    """The z14 tiles over *extents* (``cover``: the union of
    ``openfreemap.tiles_for_bbox`` of each) and which of them a run needs.

    Two ways to ask. Per output tile (the Map Converter): :meth:`members`
    are the cover tiles whose buildings can reach it (the tile grown by
    :data:`REACH_DEG`) — the ONE rule of its fingerprint contribution
    (:meth:`tag`), of the tiles a run fetches (:meth:`needed`), of which
    jobs carry ``buildings_pbf_dir`` and of what a batch's view holds
    (:meth:`view_names`). For a plain list of extents (Site Analysis: the
    pool tiles a run builds) :meth:`needed` / :meth:`view_names` without
    tiles answer for the whole cover. ``present`` is set by the fetch: the
    needed tiles the store holds with data (file names)."""

    def __init__(self, extents: Iterable[Dict[str, float]]) -> None:
        self.extents = [dict(e) for e in extents if e]
        self.cover: set = set()
        for e in self.extents:
            self.cover.update((x, y) for _z, x, y
                              in openfreemap.tiles_for_bbox(e))
        self.present: set = set()
        self._memo: Dict[str, Tuple[Tuple[Tuple[int, int], ...],
                                    Tuple[int, int, int, int]]] = {}

    def bbox(self) -> Optional[Dict[str, float]]:
        """The union of the extents (None without any)."""
        return _union(self.extents)

    def _range(self, res: int, tile: Dict[str, Any]
               ) -> Tuple[Tuple[Tuple[int, int], ...],
                          Tuple[int, int, int, int]]:
        hit = self._memo.get(tile["filename"])
        if hit is None:
            reach = terrain_adapter._expand_bbox(_output_tile_bbox(res, tile),
                                                 REACH_DEG)
            z = openfreemap.BUILDING_ZOOM
            x0 = openfreemap.lon_to_tile_x(reach["west"], z)
            x1 = openfreemap.lon_to_tile_x(reach["east"], z)
            y0 = openfreemap.lat_to_tile_y(reach["north"], z)
            y1 = openfreemap.lat_to_tile_y(reach["south"], z)
            inside = tuple((x, y) for y in range(y0, y1 + 1)
                           for x in range(x0, x1 + 1) if (x, y) in self.cover)
            hit = self._memo[tile["filename"]] = (inside, (x0, x1, y0, y1))
        return hit

    def members(self, res: int, tile: Dict[str, Any]
                ) -> Tuple[Tuple[int, int], ...]:
        """``(x, y)`` of the cover tiles whose buildings reach *tile*."""
        return self._range(res, tile)[0]

    def tag(self, res: int, tile: Dict[str, Any]) -> str:
        """The tile's fingerprint contribution: :data:`SOURCE_ID` (the id
        Site Analysis keys its pool on) plus which z14 tiles feed this tile
        — so moving a row's extent rebuilds exactly the tiles whose
        buildings change. "" when none reaches it (its pixels are the same
        as without buildings)."""
        inside, (x0, x1, y0, y1) = self._range(res, tile)
        if not inside:
            return ""
        tag = f"{SOURCE_ID}@{x0}-{x1},{y0}-{y1}"
        if len(inside) != (x1 - x0 + 1) * (y1 - y0 + 1):
            tag += "#" + hashes.md5(";".join(
                f"{x}_{y}" for x, y in inside).encode()).hexdigest()[:16]
        return tag

    def needed(self, tiles: Optional[Sequence[Tuple[int, Dict[str, Any]]]]
               = None) -> List[Tuple[int, int, int]]:
        """The ``(z, x, y)`` tiles the output *tiles* ``[(res, tile)]`` need
        (None: the whole cover), sorted north to south, west to east."""
        if tiles is None:
            need = set(self.cover)
        else:
            need = set()
            for res, tile in tiles:
                need.update(self.members(res, tile))
        z = openfreemap.BUILDING_ZOOM
        return [(z, x, y) for x, y in sorted(need, key=lambda t: (t[1], t[0]))]

    def view_names(self, tiles: Optional[Sequence[Tuple[int, Dict[str, Any]]]]
                   = None) -> List[str]:
        """File names of the needed tiles of *tiles* (None: the whole
        cover) the store holds with data — exactly what one converter run's
        ``buildings_pbf_dir`` gets (it decodes the whole directory once)."""
        names = {openfreemap.tile_filename(z, x, y)
                 for z, x, y in self.needed(tiles)}
        return sorted(names & self.present)


# ---------------------------------------------------------------------------
# The strict fetch and the per-run view
# ---------------------------------------------------------------------------

def resolve_template(should_cancel: Optional[Callable[[], bool]] = None,
                     log: Optional[Callable[[str], None]] = None) -> str:
    """The tile URL template TileJSON names NOW
    (``openfreemap.resolve_tile_template_strict``: no fallback, no session
    reuse); raises ``FetchFailed`` / ``FetchCancelled``."""
    template = openfreemap.resolve_tile_template_strict(
        should_cancel=should_cancel)
    if log is not None:
        log(f"  buildings: {template}")
    return template


def ensure_tiles(tiles: Iterable[Tuple[int, int, int]], *,
                 should_cancel: Optional[Callable[[], bool]] = None,
                 on_start: Optional[Callable[[int, int], None]] = None,
                 on_progress: Optional[Callable[[int, int, int], None]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 template: Optional[str] = None,
                 require_data: bool = True) -> openfreemap.FetchResult:
    """Make THE store (``terrain_adapter.osm_buildings_store_dir``) hold
    every tile of *tiles* that has data — or raise.

    The strict fetch (``openfreemap.fetch_building_tiles``: tiles already
    stored cost nothing, the rest ``FETCH_WORKERS`` at a time, each answer
    checked before an atomic write, ``TILE_ATTEMPTS`` per tile): a tile that
    still fails, an undecodable answer, an unreadable TileJSON → ``FetchFailed``
    (cause + missing count); Stop (*should_cancel*) → ``FetchCancelled``.
    HTTP 404 / an empty answer = no data there; a fetch where NO tile has
    data is a ``FetchFailed`` too unless *require_data* is False (then
    ``present`` is empty — the caller decides). *template* skips the
    TileJSON read (a caller fetching in groups resolves it once). *log*
    gets the cache/fetch counts and a line per 10 %; *on_start(cached,
    to_fetch)* / *on_progress(done, to_fetch, bytes)* as for the fetch."""
    tiles = list(tiles)
    store = terrain_adapter.osm_buildings_store_dir()
    if template is None:
        template = resolve_template(should_cancel, log)
    state = {"next": 10, "t0": time.monotonic()}

    def started(cached: int, todo: int) -> None:
        state["t0"] = time.monotonic()
        if log is not None:
            log(f"  buildings: {cached:,} tile(s) already in the cache, "
                f"{todo:,} to fetch ({openfreemap.FETCH_WORKERS} at a time) "
                f"→ {store}")
        if on_start is not None:
            on_start(cached, todo)

    def progress(done: int, todo: int, nbytes: int) -> None:
        if log is not None:
            pct = done * 100 // max(todo, 1)
            if pct >= state["next"]:
                state["next"] = (pct // 10 + 1) * 10
                mbps = nbytes / 1e6 / max(time.monotonic() - state["t0"],
                                          1e-3)
                log(f"  buildings: {pct}% ({done:,}/{todo:,} tiles, "
                    f"{nbytes / 1e6:,.1f} MB, {mbps:.1f} MB/s)")
        if on_progress is not None:
            on_progress(done, todo, nbytes)

    return openfreemap.fetch_building_tiles(
        tiles, store, template, should_cancel=should_cancel,
        on_start=started, on_progress=progress, require_data=require_data)


def batch_view(names: Sequence[str], view: Optional[str] = None
               ) -> Optional[str]:
    """The ``buildings_pbf_dir`` of one converter run: the view
    ``<cache>/views/<md5 of the names>`` holding exactly *names* (store
    tiles with data — hard links, copies where the file system has none;
    ``terrain_adapter.link_pbf_view``, the ONE linker). None for no names.
    *view*: a directory named before the fetch ended (a download already
    waiting for its marker) — made to hold exactly *names*, none included.
    Raises ``RuntimeError`` / ``OSError`` when a tile cannot be linked."""
    names = sorted(set(names))
    if not names and view is None:
        return None
    if view is None:
        view = terrain_adapter.osm_buildings_view_dir(names)
    terrain_adapter.link_pbf_view(terrain_adapter.osm_buildings_store_dir(),
                                  view, names)
    return view


def remove_ready_marker(view: str, marker: str = READY_MARKER) -> bool:
    """Delete a marker an earlier run left in *view* — before a download
    is launched with it (§9b: a marker that already exists counts, and the
    converter would burn the directory as it finds it). True when none is
    there now; False when it could not be removed (the caller must not
    launch a job waiting for it)."""
    path = os.path.join(view, marker)
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        # Only a regular file is a marker to the converter (§9b).
        return not os.path.isfile(path)
    return True


def write_ready_marker(view: str, marker: str = READY_MARKER) -> str:
    """Declare *view* complete for the download waiting for it: the marker
    written LAST, after every tile is linked (the converter reads the
    directory once, right after it sees the marker), atomically — a
    temporary file renamed onto the name. Returns its path; raises
    ``OSError`` when it cannot be written."""
    path = os.path.join(view, marker)
    tmp = os.path.join(view, f".{marker}.{os.getpid()}."
                             f"{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("building tiles complete\n")
        terrain_adapter.replace_with_retry(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return path


def ready_view(names: Sequence[str], marker: str = READY_MARKER
               ) -> Optional[str]:
    """The ``buildings_pbf_dir`` of a converter run launched BEFORE its
    building tiles are in, waiting for *marker* there (``buildings_pbf_
    ready``: a Site Analysis download, aether_converter >= 0.2.10, toolkit
    CONTRACT §9b; a Site Analysis ingest or a Map Converter batch, >= 0.2.12,
    CONTRACT item 48): ``terrain_adapter.osm_buildings_view_dir(names,
    marker=…)`` — named from the tiles the run NEEDS (its own key, never a
    view the sequential path links) — created, with a marker an earlier run
    left removed (:func:`remove_ready_marker`: a marker that exists counts).
    None when the directory cannot be made or that marker cannot be removed:
    the caller must not launch a job waiting for it."""
    view = terrain_adapter.osm_buildings_view_dir(names, marker=marker)
    try:
        terrain_adapter.ensure_engine_dirs(view)
    except RuntimeError:
        return None
    return view if remove_ready_marker(view, marker) else None


def complete_view(view: str, names: Sequence[str],
                  marker: str = READY_MARKER) -> str:
    """Fill a view named before its fetch ended (:func:`ready_view`) and
    release the converter waiting for it: exactly *names* linked
    (:func:`batch_view` with *view* — store tiles with data, none when there
    are none) and THEN the marker written (:func:`write_ready_marker`: the
    converter reads the directory once, right after it sees the marker).
    Returns the marker's path; raises ``RuntimeError`` / ``OSError`` when a
    tile cannot be linked or the marker cannot be written."""
    batch_view(names, view=view)
    return write_ready_marker(view, marker)


def remove_building_list(path: str) -> bool:
    """Delete a list an earlier run left at *path* — before a converter run
    is launched with it (the list IS the ready signal, CONTRACT changelog
    51: one that exists is decoded at once). True when nothing is there
    now; False when it could not be removed (the caller must not launch a
    job waiting for it)."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        return not os.path.lexists(path)
    return True


def write_building_list(path: str, names: Iterable[str]) -> str:
    """Write the list file *path* — *names*, one ``{z}_{x}_{y}.pbf`` per
    line (``\\n``, UTF-8) — atomically: a temporary file next to it renamed
    onto the name (``terrain_adapter.replace_with_retry``), so a converter
    waiting for it never reads half a list. Returns *path*; raises
    ``OSError`` when it cannot be written (no temporary file is left)."""
    directory = os.path.dirname(path)
    tmp = os.path.join(directory, f".{os.path.basename(path)}."
                                  f"{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        os.makedirs(directory, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.writelines(f"{name}\n" for name in names)
        terrain_adapter.replace_with_retry(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return path


def ready_list(names: Sequence[str]) -> Optional[str]:
    """The ``buildings_pbf_list`` of a converter run launched BEFORE its
    building tiles are in (aether_converter >= 0.2.14, toolkit CONTRACT
    changelog 51 — the list mode of :func:`ready_view`: no view, the
    converter reads the listed tiles straight from THE store, and waits for
    the list as for a marker): ``terrain_adapter.osm_buildings_list_path(
    names)`` — named from the tiles the run NEEDS — its directory and THE
    store (the job's ``buildings_pbf_dir``: on a fresh cache the fetch has
    not made it yet when the converter starts) made, and a list an earlier
    run left there removed (:func:`remove_building_list`). None when a
    directory cannot be made or that list cannot be removed: the caller
    must not launch a job waiting for it (it takes the view)."""
    path = terrain_adapter.osm_buildings_list_path(names)
    try:
        terrain_adapter.ensure_engine_dirs(
            os.path.dirname(path), terrain_adapter.osm_buildings_store_dir())
    except RuntimeError:
        return None
    return path if remove_building_list(path) else None


def complete_list(path: str, names: Sequence[str]) -> str:
    """Release the converter waiting for the list *path*
    (:func:`ready_list`): exactly *names* — the store tiles with data,
    sorted, each once; none when there are none — written atomically
    (:func:`write_building_list`). Returns *path*; raises ``RuntimeError``
    when it cannot be written."""
    try:
        return write_building_list(path, sorted(set(names)))
    except OSError as exc:
        raise RuntimeError(f"Building tile list {path} could not be written "
                           f"({exc})") from exc


def drop_undecodable(bad: Sequence[Tuple[str, str]]) -> str:
    """Building tiles the converter could not decode (``(name, error)``)
    leave the store and every view (``terrain_adapter.drop_pbf_tiles``), so
    the next run fetches them anew. Returns the run's error text."""
    names = sorted({name for name, _err in bad})
    terrain_adapter.drop_pbf_tiles(names)
    return (f"Buildings: the converter could not decode {len(names):,} "
            f"OpenFreeMap tile(s) (first: {bad[0][0]}: {bad[0][1]}). They "
            f"were removed from the cache — run again to download them "
            f"anew.")


# ---------------------------------------------------------------------------
# The fetch thread (both tabs)
# ---------------------------------------------------------------------------

def _default_failure_text(cause: str) -> str:
    return f"Buildings download failed (OpenFreeMap z14): {cause}."


class BuildingsFetch:
    """The strict OpenFreeMap z14 fetch in its own thread
    (``waveshed-buildings``), next to the terrain work — ONE implementation
    for the Map Converter (``_BuildingsBranch``: next to its terrain
    download and ingest queue, each batch waiting for its own tiles) and
    Site Analysis (next to its terrain download or ingest, which waits for
    the view's marker file — aether_converter >= 0.2.10 / 0.2.12).

    *groups* (lists of ``(z, x, y)``) are fetched in that order, a tile
    with the first group naming it: one TileJSON read
    (:func:`resolve_template`), one store listing (tiles there cost
    nothing), then :func:`ensure_tiles` per group — a group without data
    is fine, the whole fetch is judged at the end. :meth:`wait_for` blocks
    until given file names are in the store (or known to have no data):
    the cached ones at once, each group's after it, the last group's after
    *on_fetched*; with *require_data* also until some tile has data (a
    fetch where none has fails, cause :data:`NO_DATA_CAUSE`; without it
    ``present`` may be empty and the caller decides). *cover* (a
    :class:`BuildingCover`) gets ``present`` as it grows.

    A converter launched BEFORE its tiles are in (it waits for a marker in
    its view — aether_converter >= 0.2.12 for an ingest, CONTRACT item 48)
    is released through :meth:`link_when_ready` (:meth:`when_ready`, the
    callback form of :meth:`ready_for`): the view is linked and its marker
    written as soon as the names are in — at once when they are — never
    after a failure or a stop (its converter is killed then). On >= 0.2.14
    (CONTRACT changelog 51) through :meth:`list_when_ready` instead: the
    same rule, its list file written (nothing linked).

    Callbacks, all on the fetch thread: *log* — the template, the
    cache/fetch counts and a line per 10 % of the tiles to fetch;
    *on_begin()* first; *on_start(cached, to_fetch)* once the store is
    listed; *on_progress(done, to_fetch, bytes, mb_per_s)* per fetched
    tile (the whole fetch's counts; not after *should_cancel()*);
    *on_fetched(result)* when every tile is in, before the last names are
    announced — an exception it raises fails the fetch with its own text;
    *on_failed(cause)* after :attr:`error` is set; *on_end()* always, last.

    A tile still failing after its retries, an undecodable answer, an
    unreadable TileJSON → :attr:`error` = *failure_text(cause)* (default
    "Buildings download failed (OpenFreeMap z14): <cause>."), :attr:`cause`
    the bare cause. *should_cancel()* (polled throughout) →
    :attr:`cancelled`, quietly — also for a failure seen after it.
    :attr:`result` is the whole fetch's ``openfreemap.FetchResult``;
    :attr:`started` / :attr:`ended` its ``time.monotonic()`` bounds."""

    def __init__(self, groups: Iterable[Sequence[Tuple[int, int, int]]], *,
                 should_cancel: Optional[Callable[[], bool]] = None,
                 log: Optional[Callable[[str], None]] = None,
                 cover: Optional[BuildingCover] = None,
                 require_data: bool = True,
                 on_begin: Optional[Callable[[], None]] = None,
                 on_start: Optional[Callable[[int, int], None]] = None,
                 on_progress: Optional[Callable[[int, int, int, float],
                                                None]] = None,
                 on_fetched: Optional[Callable[[openfreemap.FetchResult],
                                               None]] = None,
                 on_failed: Optional[Callable[[str], None]] = None,
                 on_end: Optional[Callable[[], None]] = None,
                 failure_text: Optional[Callable[[str], str]] = None,
                 name: str = "waveshed-buildings") -> None:
        seen: set = set()
        self.groups: List[List[Tuple[int, int, int]]] = []
        for group in groups:
            fresh = [t for t in group if t not in seen]
            seen.update(fresh)
            if fresh:
                self.groups.append(fresh)
        self.tiles = [t for g in self.groups for t in g]
        self.cover = cover
        self.require_data = require_data
        self.error: Optional[str] = None
        self.cause: Optional[str] = None
        self.cancelled = False
        self.result: Optional[openfreemap.FetchResult] = None
        self.started: Optional[float] = None
        self.ended: Optional[float] = None
        self._should_cancel = should_cancel
        self._log = log
        self._on_begin = on_begin
        self._on_start = on_start
        self._on_progress = on_progress
        self._on_fetched = on_fetched
        self._on_failed = on_failed
        self._on_end = on_end
        self._failure_text = failure_text or _default_failure_text
        self._cv = threading.Condition()
        # Names in the store with data, or known to have none.
        self._done: set = set()
        self._has_data = False
        self._over = False
        # (names, action) registered by when_ready, not due yet.
        self._pending: List[Tuple[set, Callable[[], None]]] = []
        self._thread = threading.Thread(target=self._run, name=name,
                                        daemon=True)

    # -- the thread ----------------------------------------------------------
    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: Optional[float] = None) -> None:
        self._thread.join(timeout)

    def alive(self) -> bool:
        return self._thread.is_alive()

    def cancel_requested(self) -> bool:
        return self._should_cancel is not None and bool(self._should_cancel())

    # -- waiting for names ---------------------------------------------------
    def _ready_locked(self, names: set) -> bool:
        return (self._over or self.error is not None or self.cancelled
                or ((self._has_data or not self.require_data)
                    and names <= self._done))

    def ready_for(self, names: Any) -> bool:
        """Would :meth:`wait_for` return at once for *names*?"""
        with self._cv:
            return self._ready_locked(set(names))

    def wait_for(self, names: Any,
                 should_cancel: Optional[Callable[[], bool]] = None) -> None:
        """Block until every file name of *names* is in the store (or known
        to have no data) and — with *require_data* — the fetch has data
        somewhere; or the fetch failed, was stopped or ended, or
        *should_cancel()* is true. The caller then checks :attr:`error` /
        :attr:`cancelled`."""
        names = set(names)
        with self._cv:
            while not self._ready_locked(names):
                if should_cancel is not None and should_cancel():
                    return
                self._cv.wait(0.2)

    # -- releasing a converter that waits for its view's marker ------------
    def _due_locked(self, names: set) -> bool:
        """:meth:`ready_for` because *names* are IN — the fetch neither
        failed nor was stopped: each is in the store or known to have no
        data (with *require_data*: and some tile has data), or the fetch
        has ended with every tile it had (a name it never had: nothing more
        comes for it)."""
        if self.error is not None or self.cancelled:
            return False
        return self._over or ((self._has_data or not self.require_data)
                              and names <= self._done)

    def when_ready(self, names: Any, action: Callable[[], None]) -> bool:
        """Run *action()* once *names* are in (:meth:`_due_locked`): at
        once, in the caller's thread, when they are (True — what it raises
        propagates); else on the fetch thread right after they are
        announced (False — what it raises there fails the fetch in its own
        words: :attr:`error` = its text, *on_failed*, as an *on_fetched*
        error does). After a failure or a stop it never runs (False; the
        caller checks :attr:`error` / :attr:`cancelled` — its converter is
        killed)."""
        names = set(names)
        with self._cv:
            if self.error is not None or self.cancelled:
                return False
            now = self._due_locked(names)
            if not now:
                self._pending.append((names, action))
        if now:
            action()
        return now

    def link_when_ready(self, names: Any, view: str,
                        link_names: Callable[[], Sequence[str]],
                        on_linked: Optional[Callable[[str, int], None]] = None,
                        marker: str = READY_MARKER) -> bool:
        """Release a converter launched before its building tiles are in,
        waiting for *marker* in *view* (:func:`ready_view`): once *names*
        are in (:meth:`when_ready`), *view* gets exactly ``link_names()``
        and THEN the marker (:func:`complete_view`), and *on_linked(view,
        number linked)* hears of it. True when that happened at once, in
        the caller's thread; a tile that cannot be linked or a marker that
        cannot be written raises ``RuntimeError("Buildings: …")`` there —
        and on the fetch thread fails the fetch with that text."""
        def complete() -> None:
            got = list(link_names())
            try:
                complete_view(view, got, marker)
            except (RuntimeError, OSError) as exc:
                raise RuntimeError(f"Buildings: {exc}") from exc
            if on_linked is not None:
                on_linked(view, len(got))
        return self.when_ready(names, complete)

    def list_when_ready(self, names: Any, path: str,
                        list_names: Callable[[], Sequence[str]],
                        on_listed: Optional[Callable[[str, int], None]]
                        = None) -> bool:
        """The list counterpart of :meth:`link_when_ready`
        (aether_converter >= 0.2.14, ``buildings_pbf_list``): once *names*
        are in (:meth:`when_ready`, the same rule), the list file *path*
        (:func:`ready_list`) is written atomically with exactly
        ``list_names()`` (:func:`complete_list`) — nothing is linked, the
        converter reads them from the store — and *on_listed(path, number
        listed)* hears of it. True when that happened at once, in the
        caller's thread; a list that cannot be written raises
        ``RuntimeError("Buildings: …")`` there — and on the fetch thread
        fails the fetch with that text. Never after a failure or a stop."""
        def complete() -> None:
            got = list(list_names())
            try:
                complete_list(path, got)
            except (RuntimeError, OSError) as exc:
                raise RuntimeError(f"Buildings: {exc}") from exc
            if on_listed is not None:
                on_listed(path, len(set(got)))
        return self.when_ready(names, complete)

    def _mark(self, names: Any, data: bool, over: bool = False
              ) -> List[Callable[[], None]]:
        """Announce *names*; the :meth:`when_ready` actions now due are
        returned (the caller runs them, outside the lock) — none, and none
        kept, once the fetch failed or was stopped."""
        with self._cv:
            self._done.update(names)
            if data:
                self._has_data = True
            if over:
                self._over = True
            due: List[Callable[[], None]] = []
            if self.error is not None or self.cancelled:
                self._pending = []
            elif self._pending:
                keep = []
                for wanted, action in self._pending:
                    if self._due_locked(wanted):
                        due.append(action)
                    else:
                        keep.append((wanted, action))
                self._pending = keep
            self._cv.notify_all()
        return due

    def _say(self, msg: str) -> None:
        if self._log is not None:
            self._log(msg)

    def _run(self) -> None:
        store = terrain_adapter.osm_buildings_store_dir()
        t0 = self.started = time.monotonic()
        state = {"next_pct": 10, "t_fetch": t0}
        # Inside on_fetched or a when_ready action: its text is the error.
        hook = False

        def progress(done: int, todo: int, nbytes: int) -> None:
            if self.cancel_requested():
                return
            mbps = nbytes / 1e6 / max(time.monotonic() - state["t_fetch"],
                                      1e-3)
            pct = done * 100 // max(todo, 1)
            if pct >= state["next_pct"]:
                state["next_pct"] = (pct // 10 + 1) * 10
                self._say(f"  buildings: {pct}% ({done:,}/{todo:,} tiles, "
                          f"{nbytes / 1e6:,.1f} MB, {mbps:.1f} MB/s)")
            if self._on_progress is not None:
                self._on_progress(done, todo, nbytes, mbps)

        def announce(names: Any, data: bool, over: bool = False) -> None:
            # The when_ready actions now due run here, on the fetch thread:
            # what one raises is the fetch's error in its own words.
            nonlocal hook
            due = self._mark(names, data, over)
            if due:
                hook = True
                for action in due:
                    action()
                hook = False

        try:
            if self._on_begin is not None:
                self._on_begin()
            # The one strict fetch: TileJSON read now, then each group in
            # order.
            template = resolve_template(self._should_cancel, self._log)
            name_of = {t: openfreemap.tile_filename(*t) for t in self.tiles}
            listing = openfreemap.store_listing(store)
            cached = sorted(n for n in name_of.values() if n in listing)
            todo_total = len(self.tiles) - len(cached)
            state["t_fetch"] = time.monotonic()
            self._say(f"  buildings: {len(cached):,} tile(s) already in the "
                      f"cache, {todo_total:,} to fetch "
                      f"({openfreemap.FETCH_WORKERS} at a time) → {store}")
            if self._on_start is not None:
                self._on_start(len(cached), todo_total)
            present = set(cached)
            if cached:
                if self.cover is not None:
                    self.cover.present = set(present)
                announce(cached, True)
            done = {"tiles": 0, "bytes": 0, "fetched": 0, "empty": 0}
            last: List[str] = []
            for gi, group in enumerate(self.groups):
                todo = [t for t in group if name_of[t] not in present]
                if not todo:
                    continue
                base, base_bytes = done["tiles"], done["bytes"]

                def group_progress(n: int, _todo: int, nbytes: int,
                                   _base: int = base,
                                   _bytes: int = base_bytes) -> None:
                    progress(_base + n, todo_total, _bytes + nbytes)

                try:
                    # A group without any data is fine here (the whole
                    # fetch is judged below).
                    res = ensure_tiles(
                        todo, template=template,
                        should_cancel=self._should_cancel,
                        on_progress=group_progress, require_data=False)
                    got = set(res.present)
                    done["fetched"] += res.fetched
                    done["empty"] += res.empty
                    done["bytes"] += res.nbytes
                except openfreemap.FetchFailed as exc:
                    raise self._whole_fetch_failure(
                        exc, todo_total - done["tiles"] - len(todo))
                done["tiles"] += len(todo)
                present |= got
                if self.cover is not None:
                    self.cover.present = set(present)
                names = [name_of[t] for t in todo]
                if gi == len(self.groups) - 1:
                    last = names         # announced after on_fetched
                else:
                    announce(names, bool(got))
            if not present and self.require_data:
                total = len(self.tiles)
                raise openfreemap.FetchFailed(
                    f"none of the {total:,} building tiles has data — the "
                    f"server answered every one with \"no data\" "
                    f"({template})", missing=total, total=total,
                    cause=NO_DATA_CAUSE)
            result = openfreemap.FetchResult(
                len(self.tiles), len(cached), done["fetched"], done["empty"],
                sum(listing[n] for n in cached) + done["bytes"],
                time.monotonic() - t0, tuple(sorted(present)))
            self.result = result
            if self._on_fetched is not None:
                hook = True
                self._on_fetched(result)
                hook = False
            announce(last, bool(present), over=True)
        except openfreemap.FetchCancelled:
            self.cancelled = True
        except Exception as exc:  # noqa: BLE001 — reported through error
            if self.cancel_requested():
                # Stop, or the caller no longer needs the tiles.
                self.cancelled = True
            else:
                cause = (str(exc) if isinstance(exc, (RuntimeError, OSError))
                         else f"unexpected error: {exc!r}")
                self.cause = cause
                self.error = cause if hook else self._failure_text(cause)
                if self._on_failed is not None:
                    self._on_failed(cause)
        finally:
            self.ended = time.monotonic()
            try:
                if self._on_end is not None:
                    self._on_end()
            finally:
                self._mark((), False, over=True)

    def _whole_fetch_failure(self, exc: Any, later: int) -> Any:
        """A group's ``FetchFailed`` in the whole fetch's numbers: its own
        missing tiles plus the *later* groups' tiles never fetched, of every
        tile of the run."""
        if not exc.cause:
            return exc
        missing = exc.missing + max(0, later)
        total = len(self.tiles)
        return openfreemap.FetchFailed(
            f"{missing:,} of {total:,} building tiles missing — tile "
            f"{exc.cause}", missing=missing, total=total, cause=exc.cause)


# ---------------------------------------------------------------------------
# The converter's own lines
# ---------------------------------------------------------------------------

class UndecodableBuildings(terrain_adapter.ConverterCancelled):
    """Stops a converter run that could not decode building tiles (it only
    warns and goes on) — raised from the line handler when
    :meth:`BurnMonitor.feed` says so, so the shared runner kills the process
    (an ingest: before its first tile is written)."""


#: ``[Warn] buildings <z>_<x>_<y>.pbf: <error>`` — buildings.rs
#: ``load_pbf_building_dir`` skipped a tile it could not decode (ingest:
#: before the first tile; download: in the post-pass, before the burn).
UNDECODABLE_RE = re.compile(r"\[Warn\] buildings (\S+\.pbf): (.*)$")
#: main.rs, once per ingest run and directory (``… tiles in "<dir>" (once
#: for the run)...``), and its progress ``… tiles: n/m`` (toolkit 0.2.9).
DECODING_PREFIX = "[Rust] Decoding building tiles"
#: The header of a run with ``buildings_pbf_list`` (aether_converter >=
#: 0.2.14, CONTRACT changelog 51): ``[Rust] Decoding N listed building tiles
#: from "<dir>" (once for the run)...`` — a "decoding" line like the one
#: above (the progress and ``Building tiles decoded:`` lines are unchanged).
LISTED_DECODING_RE = re.compile(
    r"^\[Rust\] Decoding [\d,]+ listed building tiles\b")
#: ingest.rs ``apply_buildings_pbf``, once per output tile of a job that
#: carries the field: ``[Info] buildings_pbf_dir: N tile(s), no buildings in
#: extent`` or ``… N tile(s), B building(s) (A after dedup), D drawn, P
#: pixel(s) raised``.
INGEST_MARKER = "[Info] buildings_pbf_dir:"
#: download.rs ``apply_buildings_post_pass``, once per download job:
#: ``[Buildings] N pbf tile(s), no buildings in extent`` or ``[Buildings] N
#: pbf tile(s) → B building(s) (D dup dropped) → H/T abt tile(s), P px in
#: S s``; a repair pass ``[Buildings] repair: B building(s), P repaired px
#: raised`` (only when it patched pixels). A ``buildings_file`` burn prints
#: ``[Buildings] <path>: …`` — never matched here.
DOWNLOAD_RE = re.compile(r"^\[Buildings\] (\d+) pbf tile\(s\)")
REPAIR_PREFIX = "[Buildings] repair:"
#: A job with ``buildings_pbf_ready`` whose marker was not there at the
#: first look — a download (aether_converter >= 0.2.10, CONTRACT §9b) or an
#: ingest (>= 0.2.12, CONTRACT item 48: once, when a tile has to wait):
#: ``[Buildings] waiting for the building tiles (<marker path>)``, then
#: ``[Buildings] building tiles ready after <s.s> s`` — neither a report.
#: A job with ``buildings_pbf_list`` (>= 0.2.14, changelog 51) prints the
#: same two lines, the LIST's path in the first.
WAITING_PREFIX = "[Buildings] waiting for the building tiles"
READY_RE = re.compile(r"^\[Buildings\] building tiles ready after "
                      r"([\d.]+) s")
EMPTY_MARK = "no buildings in extent"
_PBF_TILES_RE = re.compile(r"(\d+) (?:pbf )?tile\(s\)")
_BUILDINGS_RE = re.compile(r"(\d+) building\(s\)")
_DRAWN_RE = re.compile(r"(\d+) drawn, (\d+) pixel")
_ABT_TILES_RE = re.compile(r"(\d+)/(\d+) abt tile\(s\), (\d+) px")
_REPAIR_PX_RE = re.compile(r"(\d+) repaired px raised")


class BurnMonitor:
    """What the converter said about the buildings of a run — one monitor
    per run of the plugin (a Map Converter conversion, one
    ``prepare_terrain``), fed with the lines of every converter run that
    carries ``buildings_pbf_dir`` (each ingest batch, each download pass).

    :meth:`begin` opens such a converter run (*expected_pbf_jobs*: how many
    reports it owes — the ingest jobs carrying the field, 1 for a download
    pass, 0 for a repair pass), :meth:`feed` reads its lines, :meth:`end`
    closes it. :meth:`verdict` is the plugin run's: undecodable tiles, a
    converter run that carried the field and reported no burn, or buildings
    decoded with none drawn over everything fed (one batch at the edge of
    the buildings legitimately draws none — the whole run cannot).
    :attr:`empty` — every report said "no buildings in extent" — is no
    error (the tiles hold no building there); the caller warns."""

    def __init__(self, expected_pbf_jobs: int = 0, what: str = "ingest"
                 ) -> None:
        self.bad_tiles: List[Tuple[str, str]] = []
        self.reports = 0            #: report lines, every converter run
        self.empty_reports = 0      #: of them "no buildings in extent"
        #: Buildings decoded — the largest report (every line of a run
        #: reports the same decoded directory).
        self.decoded = 0
        self.drawn = 0              #: buildings drawn (ingest lines, summed)
        self.raised = 0             #: pixels raised (ingest + download)
        self.tiles = 0              #: terrain tiles the reports were about
        self.pbf_tiles = 0          #: building tiles read (largest report)
        #: Reports whose numbers could not be read (a format this monitor
        #: does not know): the run is then never judged "drew nothing".
        self.unreadable = 0
        self.silent: List[str] = []  #: why each silent converter run failed
        #: Waits for the building tiles a converter run announced (a job
        #: with ``buildings_pbf_ready``) and the seconds they took, summed
        #: (the current run's: ``batch_waited_s``).
        self.waits = 0
        self.waited_s = 0.0
        #: The kind of the last line fed: "undecodable", "decoding",
        #: "report", "wait" or None (any other line).
        self.last_kind: Optional[str] = None
        self.begin(expected_pbf_jobs, what)

    # -- one converter run -------------------------------------------------
    def begin(self, expected_pbf_jobs: int, what: str = "ingest") -> None:
        """Open the next converter run (the totals go on)."""
        self.expected = max(0, int(expected_pbf_jobs))
        self.what = what
        self.batch_reports = 0
        self.batch_drawn = 0
        self.batch_raised = 0
        self.batch_waited_s = 0.0
        self.batch_bad: List[Tuple[str, str]] = []

    def feed(self, line: str) -> bool:
        """Read one converter line; True = stop this converter run NOW (it
        warned it could not decode building tiles — the line after the
        warnings, so every warned tile is known)."""
        self.last_kind = None
        bad = UNDECODABLE_RE.search(line)
        if bad:
            entry = (bad.group(1), bad.group(2).strip())
            self.batch_bad.append(entry)
            self.bad_tiles.append(entry)
            self.last_kind = "undecodable"
            return False
        if (line.startswith(DECODING_PREFIX)
                or LISTED_DECODING_RE.match(line)):
            self.last_kind = "decoding"
            return False
        if line.startswith(WAITING_PREFIX):
            self.waits += 1
            self.last_kind = "wait"
            return False
        ready = READY_RE.search(line)
        if ready:
            self.waited_s += float(ready.group(1))
            self.batch_waited_s += float(ready.group(1))
            self.last_kind = "wait"
            return False
        if self.batch_bad:
            return True
        if INGEST_MARKER in line:
            self.last_kind = "report"
            self._report(line, ingest=True)
        elif line.startswith(REPAIR_PREFIX) or DOWNLOAD_RE.search(line):
            self.last_kind = "report"
            self._report(line, ingest=False)
        return False

    def _report(self, line: str, ingest: bool) -> None:
        self.reports += 1
        self.batch_reports += 1
        if ingest:
            self.tiles += 1
        n_pbf = _PBF_TILES_RE.search(line)
        if n_pbf:
            self.pbf_tiles = max(self.pbf_tiles, int(n_pbf.group(1)))
        if EMPTY_MARK in line:
            self.empty_reports += 1
            return
        decoded = _BUILDINGS_RE.search(line)
        if decoded:
            self.decoded = max(self.decoded, int(decoded.group(1)))
        px: Optional[int] = None
        if ingest:
            drawn = _DRAWN_RE.search(line)
            if drawn:
                self.drawn += int(drawn.group(1))
                self.batch_drawn += int(drawn.group(1))
                px = int(drawn.group(2))
        else:
            whole = _ABT_TILES_RE.search(line)
            repair = _REPAIR_PX_RE.search(line)
            if whole:
                self.tiles += int(whole.group(2))
                px = int(whole.group(3))
            elif repair:
                px = int(repair.group(1))
        if px is None or not decoded:
            self.unreadable += 1
            return
        self.raised += px
        self.batch_raised += px

    def end(self, ok: bool = True, tiles: Optional[int] = None
            ) -> Optional[str]:
        """Close the current converter run. *ok*: it exited 0 (a failed run
        owes no report). Its problem text when it owed reports and printed
        none — an engine that ignores the field (also kept for
        :meth:`verdict`) — else None. *tiles*: what it wrote, for the text."""
        if not ok or not self.expected or self.batch_reports or self.batch_bad:
            return None
        if self.what == "download":
            msg = ("aether_converter downloaded the terrain without reporting "
                   "a burn of the building tiles it was given (no "
                   "'[Buildings] … pbf tile(s)' line) — this engine does not "
                   "apply buildings_pbf_dir to downloads. Update the engine "
                   "binaries (Settings → Download Binaries).")
        else:
            n = self.expected if tiles is None else tiles
            msg = (f"aether_converter wrote {n} tile(s) without reporting a "
                   f"burn of the building tiles it was given (no "
                   f"'{INGEST_MARKER}' line) — this engine does not apply "
                   f"buildings_pbf_dir. Update the engine binaries "
                   f"(Settings → Download Binaries).")
        self.silent.append(msg)
        return msg

    # -- the plugin run ----------------------------------------------------
    @property
    def empty(self) -> bool:
        """Every report said "no buildings in extent" (and there was one)."""
        return self.reports > 0 and self.empty_reports == self.reports

    def nothing_drawn(self) -> Optional[str]:
        """The zero-drawn rule alone: buildings decoded, none drawn and no
        pixel raised over everything fed — its error text, else None."""
        if (self.decoded <= 0 or self.drawn > 0 or self.raised > 0
                or self.unreadable):
            return None
        return (f"Buildings: the converter decoded {self.decoded:,} "
                f"building(s) from the {self.pbf_tiles:,} OpenFreeMap tile(s) "
                f"it was given but drew none onto the {self.tiles:,} terrain "
                f"tile(s) it wrote — the terrain would carry no buildings. "
                f"(Buildings only in the tiles' outer margin, beyond the "
                f"terrain, are not drawn: if the area really has none, run "
                f"without buildings.)")

    def verdict(self) -> Optional[str]:
        """The run's error text, else None: undecodable building tiles, a
        converter run that carried the field and reported no burn, or
        buildings decoded with none drawn over everything fed."""
        if self.bad_tiles:
            first = self.bad_tiles[0]
            names = sorted({n for n, _e in self.bad_tiles})
            return (f"Buildings: the converter could not decode "
                    f"{len(names):,} OpenFreeMap tile(s) (first: {first[0]}: "
                    f"{first[1]}).")
        if self.silent:
            return self.silent[0]
        return self.nothing_drawn()

    def summary(self) -> str:
        """``burned onto … — … drawn, … px raised`` for the log."""
        if self.empty:
            return (f"{self.reports:,} report(s), no buildings in the "
                    f"{self.pbf_tiles:,} OpenFreeMap tile(s)")
        drawn = (f"{self.drawn:,} building(s) drawn, " if self.drawn else "")
        return (f"{self.decoded:,} building(s) decoded, {drawn}"
                f"{self.raised:,} px raised")
