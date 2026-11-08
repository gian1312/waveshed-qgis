"""Header index of a folder of pre-built ``.abt`` tiles.

A Map Converter output folder holds thousands of tiles (the owner's: 2,463),
and every pre-built Site Analysis / P2P run needs each tile's header (corner,
size, pixel step) to choose its tiles. Opening every file on every call cost
~10 s on the container's Windows mount (one open + 44-byte read per file,
serially) and the listing another ~4 s (one ``isfile`` per name). The run
asked three to five times. Now:

* ONE in-memory index per folder (process-wide, :func:`get_index`): per tile
  name its ``(size, mtime_ns)`` and the header fields (None = not a readable
  tile), persisted to ``<cache>/index/abt-<md5(folder)>.json`` beside the
  raster folder index (``terrain_index.index_path`` / ``folder_key`` style;
  a missing or corrupt file is an empty index) for folders of at least
  :data:`PERSIST_MIN_TILES` tiles.
* :meth:`AbtHeaderIndex.names` is one ``scandir`` (no per-file call where the
  filesystem reports the entry type) — what ``is_abt_tile_dir`` /
  ``list_abt_tiles`` need; reused for :data:`FRESH_S` while the directory's
  own mtime is unchanged (an added, removed or renamed tile changes it).
* :meth:`AbtHeaderIndex.validate` adds one ``stat`` per tile (free from the
  listing on Windows, 16 threads elsewhere) and opens ONLY new or changed
  tiles; reused the same way. Its :attr:`AbtListing.signature` (md5 of every
  name + size + mtime) keys the selection memo in ``terrain_adapter``.

Thread-safe (one lock per folder); GUI-free.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import abt, hashes, terrain_index

#: Bump when the stored layout changes (the whole file is ignored).
SCHEMA = "abt1"

#: A listing / validation younger than this is reused as is — the GUI
#: pre-check and the worker of one run share it (as for the raster index).
FRESH_S = terrain_index.FRESH_S

#: Threads for the stat and header passes (container Windows mount, 2,463
#: tiles: stat 2.4 s serial / 0.5 s on 16 threads, open+read 10.2 s / 1.0 s).
WORKERS = 16

#: Below this many files the passes run serially (a pool costs more).
_POOL_MIN = 32

#: Folders with fewer tiles are not persisted: re-reading their headers
#: costs less than one more file per folder in the cache.
PERSIST_MIN_TILES = 64

#: Header fields kept per tile: (size_px, ul_lat, ul_lon, pixel_res, stride).
Fields = Tuple[int, float, float, float, int]


class AbtListing(NamedTuple):
    """The validated tiles of one folder, paths joined onto the caller's
    spelling of the folder."""

    #: Readable tiles, sorted by path.
    headers: List[abt.TileHeader]
    #: ``.abt`` files whose header could not be read, sorted.
    unreadable: List[str]
    #: md5 over every listed tile's name, size and mtime.
    signature: str


def index_path(folder: str) -> str:
    """``<cache>/index/abt-<md5(folder)>.json``."""
    return os.path.join(terrain_index.index_dir(),
                        "abt-" + terrain_index.folder_key(folder) + ".json")


def _dir_mtime(folder: str) -> Optional[int]:
    try:
        st = os.stat(folder)
    except OSError:
        return None
    return int(st.st_mtime_ns)


def _is_tile_name(name: str) -> bool:
    return name.lower().endswith(".abt")


def _scan(folder: str, with_stats: bool
          ) -> Optional[Tuple[List[str], Dict[str, Optional[Tuple[int, int]]]]]:
    """``(sorted .abt names, {name: (size, mtime_ns)})`` of the regular files
    directly in *folder* (not recursive — the engine's ``read_dir`` is not),
    None when it cannot be listed. Stats only with *with_stats*: from the
    listing on Windows, else one threaded ``stat`` per tile."""
    free_stat = with_stats and os.name == "nt"
    try:
        it = os.scandir(folder)
    except OSError:
        return None
    names: List[str] = []
    stats: Dict[str, Optional[Tuple[int, int]]] = {}
    with it:
        for de in it:
            if _is_tile_name(de.name) and _is_file(de):
                names.append(de.name)
                if free_stat:
                    stats[de.name] = _entry_stat(de)
    names.sort()
    if with_stats and not free_stat:
        stats.update(zip(names, _map(
            lambda name: _path_stat(os.path.join(folder, name)), names)))
    return names, stats


def _is_file(de: "os.DirEntry") -> bool:
    try:
        return de.is_file()
    except OSError:
        return False


def _entry_stat(de: "os.DirEntry") -> Optional[Tuple[int, int]]:
    try:
        st = de.stat()
    except OSError:
        return None
    return int(st.st_size), int(st.st_mtime_ns)


def _path_stat(path: str) -> Optional[Tuple[int, int]]:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return int(st.st_size), int(st.st_mtime_ns)


def _map(fn, items: Sequence) -> List:
    if len(items) < _POOL_MIN:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        return list(ex.map(fn, items))


def _fields(path: str) -> Optional[Fields]:
    h = abt.read_header(path)
    if h is None:
        return None
    return (int(h.size), float(h.ul_lat), float(h.ul_lon), float(h.pixel_res),
            int(h.stride))


def _signature(entries: Dict[str, Tuple[int, int, Optional[Fields]]]) -> str:
    md5 = hashes.md5()
    for name in sorted(entries):
        size, mtime_ns, _f = entries[name]
        md5.update(f"{name}\t{size}\t{mtime_ns}\n".encode("utf-8"))
    return md5.hexdigest()


class AbtHeaderIndex:
    """The header index of one folder. Use :func:`get_index`."""

    def __init__(self, folder: str) -> None:
        self.folder = os.path.abspath(folder)
        self.lock = threading.RLock()
        #: name -> (size, mtime_ns, header fields or None)
        self.entries: Dict[str, Tuple[int, int, Optional[Fields]]] = {}
        self._loaded = False
        self._names: Optional[List[str]] = None
        self._names_mtime: Optional[int] = None
        self._listed_at = 0.0
        self._valid_mtime: Optional[int] = None
        self._validated_at = 0.0
        self._signature = ""
        #: Files opened by the last validation (tests, logs).
        self.last_reads = 0

    # -- persistence -------------------------------------------------------
    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            with open(index_path(self.folder), "r", encoding="utf-8") as fh:
                doc = json.load(fh)
            if (doc.get("schema") != SCHEMA
                    or terrain_index._norm_folder(str(doc.get("folder", "")))
                    != terrain_index._norm_folder(self.folder)):
                return
            entries: Dict[str, Tuple[int, int, Optional[Fields]]] = {}
            for name, row in (doc.get("entries") or {}).items():
                if len(row) not in (2, 7):
                    continue              # not a row this schema writes: re-read
                fields = None
                if len(row) == 7:
                    fields = (int(row[2]), float(row[3]), float(row[4]),
                              float(row[5]), int(row[6]))
                entries[str(name)] = (int(row[0]), int(row[1]), fields)
        except (OSError, ValueError, TypeError, KeyError, IndexError,
                AttributeError):
            return  # missing or corrupt: an empty index, rebuilt on use
        self.entries = entries

    def save(self) -> None:
        path = index_path(self.folder)
        rows = {name: [size, mtime_ns] + (list(fields) if fields else [])
                for name, (size, mtime_ns, fields) in self.entries.items()}
        doc = {"schema": SCHEMA, "folder": self.folder, "saved": time.time(),
               "entries": rows}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".abtidx_",
                                       dir=os.path.dirname(path))
        except OSError:
            return
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(doc, separators=(",", ":")))
            os.replace(tmp, path)
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass

    # -- queries -----------------------------------------------------------
    def expire(self) -> None:
        """The next :meth:`names` / :meth:`validate` looks at the folder again."""
        with self.lock:
            self._listed_at = 0.0
            self._validated_at = 0.0

    def names(self, max_age_s: float = FRESH_S) -> List[str]:
        """Sorted names of the ``.abt`` tiles directly in the folder (one
        ``scandir``; reused for *max_age_s* while the folder's mtime holds)."""
        with self.lock:
            mtime = _dir_mtime(self.folder)
            if mtime is None:
                self._names, self._names_mtime = [], None
                return []
            if (self._names is not None and max_age_s > 0
                    and mtime == self._names_mtime
                    and time.time() - self._listed_at < max_age_s):
                return list(self._names)
            got = _scan(self.folder, with_stats=False)
            self._names = got[0] if got is not None else []
            self._names_mtime = mtime
            self._listed_at = time.time()
            return list(self._names)

    def validate(self, max_age_s: float = FRESH_S) -> None:
        """Bring :attr:`entries` up to date: one listing + one stat per tile,
        headers read only for new or changed tiles; saved when anything
        changed. Reused for *max_age_s* while the folder's mtime holds."""
        with self.lock:
            self.load()
            mtime = _dir_mtime(self.folder)
            if (max_age_s > 0 and self._validated_at
                    and mtime is not None and mtime == self._valid_mtime
                    and time.time() - self._validated_at < max_age_s):
                return
            got = _scan(self.folder, with_stats=True) if mtime is not None else None
            names, stats = got if got is not None else ([], {})
            now = time.time()
            self._names, self._names_mtime, self._listed_at = (
                list(names), mtime, now)
            fresh: Dict[str, Tuple[int, int, Optional[Fields]]] = {}
            to_read: List[Tuple[str, Tuple[int, int]]] = []
            for name in names:
                st = stats.get(name)
                if st is None:
                    continue            # vanished between listing and stat
                old = self.entries.get(name)
                if old is not None and (old[0], old[1]) == st:
                    fresh[name] = old
                else:
                    to_read.append((name, st))
            read = _map(lambda item: _fields(os.path.join(self.folder,
                                                          item[0])),
                        to_read)
            for (name, st), fields in zip(to_read, read):
                fresh[name] = (st[0], st[1], fields)
            changed = bool(to_read) or len(fresh) != len(self.entries)
            self.entries = fresh
            self.last_reads = len(to_read)
            self._signature = _signature(fresh)
            self._valid_mtime = mtime
            self._validated_at = now
            if (mtime is not None and len(fresh) >= PERSIST_MIN_TILES
                    and (changed or not os.path.exists(index_path(self.folder)))):
                self.save()

    def listing(self, base: str, max_age_s: float = FRESH_S) -> AbtListing:
        """:meth:`validate`, then the tiles as headers whose paths are
        ``os.path.join(base, name)`` (*base* = the caller's spelling)."""
        with self.lock:
            self.validate(max_age_s)
            headers: List[abt.TileHeader] = []
            unreadable: List[str] = []
            for name in sorted(self.entries):
                fields = self.entries[name][2]
                path = os.path.join(base, name)
                if fields is None:
                    unreadable.append(path)
                else:
                    headers.append(abt.TileHeader(path, *fields))
            return AbtListing(headers, unreadable, self._signature)


_INDEXES: Dict[str, AbtHeaderIndex] = {}
_INDEXES_LOCK = threading.Lock()


def get_index(folder: str) -> AbtHeaderIndex:
    """The (process-wide, shared) header index of *folder*."""
    key = terrain_index._norm_folder(folder) + "|" + index_path(folder)
    with _INDEXES_LOCK:
        idx = _INDEXES.get(key)
        if idx is None:
            idx = _INDEXES[key] = AbtHeaderIndex(folder)
        return idx


def read_folder(folder: str, max_age_s: float = FRESH_S) -> AbtListing:
    """The validated tiles of *folder* (see :class:`AbtListing`)."""
    return get_index(folder).listing(folder, max_age_s)


def tile_sizes(folder: str) -> Dict[str, int]:
    """``{name: bytes}`` of *folder*'s tiles as last validated — no file is
    touched (call :func:`read_folder` first; Inspect .abt prices its
    choices from it)."""
    idx = get_index(folder)
    with idx.lock:
        return {name: int(size) for name, (size, _m, _f)
                in idx.entries.items()}


def tile_names(folder: str, max_age_s: float = FRESH_S) -> List[str]:
    """Sorted ``.abt`` tile names directly in *folder* (see
    :meth:`AbtHeaderIndex.names`)."""
    return get_index(folder).names(max_age_s)


def expire(folder: Optional[str] = None) -> None:
    """Make the next look at *folder* (every folder when None) list and stat
    it again — a new run starts from the folder as it is now."""
    with _INDEXES_LOCK:
        if folder is None:
            targets = list(_INDEXES.values())
        else:
            key = terrain_index._norm_folder(folder) + "|"
            targets = [i for k, i in _INDEXES.items() if k.startswith(key)]
    for idx in targets:
        idx.expire()


def forget() -> None:
    """Drop every in-memory header index (tests; the index files cleared)."""
    with _INDEXES_LOCK:
        _INDEXES.clear()
