"""Map Converter → Inspect .abt: which tiles of a folder the preview reads.

The old Inspect mosaicked EVERY tile of the folder at the FINEST resolution
into a 16,384² canvas on the GUI thread: the owner's multi-resolution output
(2,438 tiles, 2 m to 250 m) meant ~60 GB of reads and a frozen QGIS, and
tiles of different resolutions overwrote each other in file order. Now:

* :func:`plan_folder` groups the folder's tiles by resolution from the header
  index (``core/abt_index.py`` — no tile body is opened), with each group's
  tile count, file bytes and :attr:`ResolutionGroup.coverage` — the share of
  the folder's extent (the union of every tile's extent) its tiles cover.
* :func:`default_choice`: the COARSEST resolution covering at least
  :data:`COVERS_FRACTION` (95 %) of that extent; when no single resolution
  does (the rings of a range-profile view), the coarsest resolutions first
  until together they do.
* :func:`choice_note` says what a choice reads (``abt.mosaic_read_bytes`` —
  only the rows the canvas samples) and warns when the chosen tiles hold more
  than :data:`BIG_READ_BYTES`.

The mosaic itself is ``abt.mosaic_to_geotiff(headers=…)``: only the chosen
tiles, coarse first so the finest wins, at most ``abt.INSPECT_MAX_DIM`` px,
with internal overviews. GUI-free; numpy is imported lazily.
"""

from __future__ import annotations

import math
import os
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import abt, abt_index

#: A resolution "covers the folder" from this share of its extent on.
COVERS_FRACTION = 0.95

#: Chosen tiles holding more than this get a warning in the dialog (with
#: the read volume — the user may still go ahead; Cancel stops the build).
BIG_READ_BYTES = 2 * 1024 ** 3

#: Above this many cells of the exact (edge-compressed) coverage grid the
#: coverage is sampled on :data:`_SAMPLE_CELLS`² cell centres instead.
_MAX_EXACT_CELLS = 4_000_000
_SAMPLE_CELLS = 2048

_MB = 1024 ** 2
_GB = 1024 ** 3

Rect = Tuple[float, float, float, float]          # west, south, east, north


def resolution_m(header: abt.TileHeader) -> int:
    """The tile's resolution in metres, snapped to the plugin's list (the
    rule of the pre-built folder selection)."""
    from .terrain_adapter import _abt_nominal_res
    return _abt_nominal_res(header)


def _rect(header: abt.TileHeader) -> Rect:
    span = header.size * header.pixel_res
    return (round(header.ul_lon, 9), round(header.ul_lat - span, 9),
            round(header.ul_lon + span, 9), round(header.ul_lat, 9))


class ResolutionGroup:
    """The tiles of one resolution in the folder."""

    __slots__ = ("res_m", "headers", "file_bytes", "coverage")

    def __init__(self, res_m: int, headers: List[abt.TileHeader],
                 file_bytes: int, coverage: float = 0.0) -> None:
        self.res_m = int(res_m)
        self.headers = headers
        self.file_bytes = int(file_bytes)
        #: Share (0..1) of the folder's extent its tiles cover.
        self.coverage = float(coverage)

    @property
    def count(self) -> int:
        return len(self.headers)


class InspectPlan:
    """What :func:`plan_folder` found: the resolution groups (finest first),
    the default choice, the ``.abt`` files whose header could not be read,
    and the coverage masks behind :meth:`coverage`."""

    def __init__(self, folder: str, groups: List[ResolutionGroup],
                 unreadable: List[str], masks: Dict[int, Any],
                 areas: Any, total_area: float) -> None:
        self.folder = folder
        self.groups = groups
        self.unreadable = unreadable
        self._masks = masks
        self._areas = areas
        self._total = total_area
        for g in groups:
            g.coverage = self.coverage([g.res_m])
        self.default: Tuple[int, ...] = default_choice(self)

    @property
    def tile_count(self) -> int:
        return sum(g.count for g in self.groups)

    def group(self, res_m: int) -> Optional[ResolutionGroup]:
        return next((g for g in self.groups if g.res_m == res_m), None)

    def coverage(self, resolutions: Sequence[int]) -> float:
        """Share of the folder's extent the tiles of *resolutions* cover
        together."""
        masks = [self._masks[r] for r in resolutions if r in self._masks]
        if not masks or self._total <= 0:
            return 0.0
        union = masks[0].copy()
        for m in masks[1:]:
            union |= m
        return float(self._areas[union].sum() / self._total)


def _coverage_masks(rects_by_res: Dict[int, List[Rect]]
                    ) -> Tuple[Dict[int, Any], Any, float]:
    """``(mask per resolution, cell areas, area of the union)`` on one grid
    over the folder: exact on the grid of every tile edge (cell area ∝
    Δlon · Δsin(lat)), or cell centres of a :data:`_SAMPLE_CELLS`² grid when
    the edges would make it larger than :data:`_MAX_EXACT_CELLS`."""
    import numpy as np

    rects = [r for rs in rects_by_res.values() for r in rs]
    if not rects:
        return {}, np.zeros((0, 0)), 0.0
    xs = np.unique([v for r in rects for v in (r[0], r[2])])
    ys = np.unique([v for r in rects for v in (r[1], r[3])])
    exact = (len(xs) - 1) * (len(ys) - 1) <= _MAX_EXACT_CELLS
    if exact:
        def span(lo: float, hi: float, edges: Any) -> Tuple[int, int]:
            return (int(np.searchsorted(edges, lo)),
                    int(np.searchsorted(edges, hi)))
        x_edges, y_edges = xs, ys
    else:
        x_edges = np.linspace(xs[0], xs[-1], _SAMPLE_CELLS + 1)
        y_edges = np.linspace(ys[0], ys[-1], _SAMPLE_CELLS + 1)
        cx = (x_edges[:-1] + x_edges[1:]) / 2
        cy = (y_edges[:-1] + y_edges[1:]) / 2

        def span(lo: float, hi: float, edges: Any) -> Tuple[int, int]:
            centres = cx if edges is x_edges else cy
            return (int(np.searchsorted(centres, lo)),
                    int(np.searchsorted(centres, hi)))
    nx, ny = max(len(x_edges) - 1, 0), max(len(y_edges) - 1, 0)
    areas = np.outer(np.diff(np.sin(np.radians(y_edges))),
                     np.diff(x_edges))
    masks: Dict[int, Any] = {}
    union = np.zeros((ny, nx), dtype=bool)
    for res, rs in rects_by_res.items():
        mask = np.zeros((ny, nx), dtype=bool)
        for w, s, e, n in rs:
            i0, i1 = span(w, e, x_edges)
            j0, j1 = span(s, n, y_edges)
            mask[j0:j1, i0:i1] = True
        masks[res] = mask
        union |= mask
    return masks, areas, float(areas[union].sum())


def plan_folder(folder: str,
                should_cancel: Optional[Callable[[], bool]] = None
                ) -> Optional[InspectPlan]:
    """The folder's tiles grouped by resolution, from the header index (one
    listing + one stat per tile; only new or changed tiles have their
    44-byte header read). None when *should_cancel* went True."""
    listing = abt_index.read_folder(folder)
    if should_cancel is not None and should_cancel():
        return None
    sizes = abt_index.tile_sizes(folder)
    by_res: Dict[int, List[abt.TileHeader]] = {}
    for h in listing.headers:
        by_res.setdefault(resolution_m(h), []).append(h)
    groups = []
    for res in sorted(by_res):
        headers = by_res[res]
        nbytes = sum(sizes.get(os.path.basename(h.path), 0) for h in headers)
        groups.append(ResolutionGroup(res, headers, nbytes))
    masks, areas, total = _coverage_masks(
        {g.res_m: [_rect(h) for h in g.headers] for g in groups})
    return InspectPlan(folder, groups, list(listing.unreadable), masks,
                       areas, total)


def default_choice(plan: InspectPlan) -> Tuple[int, ...]:
    """The coarsest resolution whose tiles cover >= :data:`COVERS_FRACTION`
    of the folder's extent; when none does, the coarsest resolutions first
    until together they do (or all of them)."""
    coarse_first = sorted((g.res_m for g in plan.groups), reverse=True)
    for res in coarse_first:
        if plan.coverage([res]) >= COVERS_FRACTION:
            return (res,)
    chosen: List[int] = []
    for res in coarse_first:
        chosen.append(res)
        if plan.coverage(chosen) >= COVERS_FRACTION:
            break
    return tuple(sorted(chosen))


def chosen_headers(plan: InspectPlan, resolutions: Sequence[int]
                   ) -> List[abt.TileHeader]:
    """The tiles of *resolutions* — the only ones the preview reads — in
    paint order (coarse first: the finest wins, ``abt.mosaic_order``)."""
    wanted = set(int(r) for r in resolutions)
    return abt.mosaic_order([h for g in plan.groups if g.res_m in wanted
                             for h in g.headers])


def read_estimate(plan: InspectPlan, resolutions: Sequence[int],
                  max_dim: int = abt.INSPECT_MAX_DIM
                  ) -> Tuple[int, int, Optional[abt.MosaicGrid]]:
    """``(file bytes of the chosen tiles, bytes the mosaic reads, its
    grid)``."""
    headers = chosen_headers(plan, resolutions)
    files = sum(g.file_bytes for g in plan.groups
                if g.res_m in set(resolutions))
    grid = abt.mosaic_grid(headers, max_dim)
    if grid is None:
        return files, 0, None
    return files, abt.mosaic_read_bytes(headers, grid), grid


def _size(n: float) -> str:
    """``850 MB`` / ``2.3 GB`` / ``106.4 GB``."""
    return (f"{n / _GB:,.1f} GB" if n >= _GB
            else f"{n / _MB:,.0f} MB")


def group_label(group: ResolutionGroup) -> str:
    """``30 m — 90 tiles, 1,201 MB, covers 100 % of the folder``."""
    return (f"{group.res_m} m — {group.count:,} tile(s), "
            f"{group.file_bytes / _MB:,.0f} MB, covers "
            f"{math.floor(group.coverage * 100 + 1e-9):.0f} % of the folder")


def choice_note(plan: InspectPlan, resolutions: Sequence[int],
                max_dim: int = abt.INSPECT_MAX_DIM) -> str:
    """What the dialog says about a choice: tiles, the bytes read (only the
    rows the canvas samples), the canvas, what it covers — and the warning
    when the chosen tiles hold more than :data:`BIG_READ_BYTES`."""
    if not resolutions:
        return "Pick at least one resolution."
    headers = chosen_headers(plan, resolutions)
    files, read, grid = read_estimate(plan, resolutions, max_dim)
    if grid is None:
        return "No tile of that resolution."
    text = (f"Reads {len(headers):,} tile(s) — about {_size(read)} of their "
            f"{_size(files)} — onto a {grid.width:,} × {grid.height:,} px "
            f"preview at ~{grid.resolution * 111_111:,.0f} m per pixel")
    cov = plan.coverage(resolutions)
    if cov < 0.995:
        text += f", covering {math.floor(cov * 100 + 1e-9):.0f} % of the folder"
    text += "."
    if len(resolutions) > 1:
        text += " Where resolutions overlap, the finest is shown."
    if files > BIG_READ_BYTES:
        text += (f"\n\nThe chosen tiles hold {_size(files)}: building the "
                 f"preview reads about {_size(read)} of them, which can take "
                 f"minutes on a slow or network disk. Cancel stops it.")
    return text
