"""aether_core's console: what the log panel shows, what the file keeps.

aether_core prints its whole story to stdout — the lines a user reads (the
``[Core]`` job summary, the GPU line, the licence, the tiles selected, the
output grid, the export result, every warning and error) and the lines a
developer reads: the resource solver's dump (``[… INFO aether_core::solver]
…``, sixteen lines a run, and the ``[Core] Solver Solution:`` block), the
grid, geometry, eviction, pipeline-cache and startup internals, the
per-wedge progress and profile rows, the polar-grid sample
(``[DIAG-POLAR]``, twenty), the profiling table, the dispatch scheduler
(``[SCHED]``), the ``[P:n]`` / ``[S:…]`` progress markers the tabs parse
(owner, 2026-11-07: 31 of the 47 lines of a 360° run on 0.4.10 are the
second kind). The QGIS Log Messages panel shows the first kind only
(:func:`is_diagnostic`); every line of both kinds goes to today's engine log
file beside the converter diagnostics — ``<cache>/logs/aether_core-<YYYYmmdd>
.log``, rotated with them by ``terrain_adapter._rotate_diag_files`` — so a
bug report still carries everything.

Nothing that could be a problem leaves the panel: a line that says error,
panic, fail or warn goes through whatever else it looks like, and when the
file cannot be written the panel gets every line, as before.
"""

from __future__ import annotations

import contextlib
import os
import re
import time
from typing import Callable, Optional, TextIO

from . import terrain_adapter

#: Shown whatever else the line looks like: a problem is never masked.
_KEEP_WORDS = re.compile(r"error|panic|fail|warn|\*\*\*", re.IGNORECASE)
#: env_logger's ``[2026-10-24T08:33:52Z INFO aether_core::solver] …`` prefix:
#: level and module.
_ENV_LOGGER = re.compile(r"^\[\d{4}-\d{2}-\d{2}T[\d:.]+Z\s+(\w+)\s+([\w:]+)\]")
#: Modules whose INFO/DEBUG lines are developer diagnostics (the resource
#: solver's dump). ``aether_core::crypto`` — the licence line — is not one.
_DIAG_MODULES = ("aether_core::solver",)
_NOISE_PREFIXES = (
    "[DIAG",            # [DIAG-POLAR] sample, [DIAG] erp/min_db echo
    "[Debug]",
    "[SCHED]",          # dispatch scheduler knobs
    "[P:", "[S:",       # progress markers: parsed by the tabs, not read
    "[TiledInput]",
    "[Cache] Evicted",
    "[Export] Sparse:", "[Export] Overview",
    # The run's internals (owner, 2026-11-07, from a real 0.4.10 console:
    # 31 of the 47 lines of a 360° run; the file keeps every one). What
    # stays: licence, GPU adapter + backend, ERP, Job, Limits, Spatial
    # Index, Selected tile resolutions, Output Grid, Output raster, Range
    # profile, the export-complete and Success lines, the two Profile
    # lines, and every warning or error.
    "[Core] GPU probe",
    "[Core] Cropped Bounds",
    "[Core] Terrain Format",
    "[Core] Grid will use",
    "[Core] Tile Grid",
    "[Core] Grid Origin",
    "[Core] TX Pixel",
    "[Core] Inputs:",
    "[Core] Solver Solution",      # + its indented rows, see below
    "[Core] Geometry:",
    "[Core] Execution:",
    "[Core] Macro-Blocks",
    "[Core] pipeline cache",
    "[Core] Output Tile Grid",
    "[Core] Terrain eviction",
    "[Core] Precomputation",
    "[Core] Output tile eviction",
    "[Core] Output tile precomputation",
    "[Core] startup:",
    "[Core] Final flush",
    "[Core] Tile file complete",
    "[Core] Exporting to GeoTIFF",  # the "export complete" line follows
    "[Export] TILED",
    "[Export] SUCCESS",             # the figures behind "export complete"
)
#: The two ``[Profile]`` lines a user may want; the rest is the per-phase
#: breakdown behind them.
_PROFILE_KEEP = ("[Profile] Total wall time", "[Profile] Finalize + write")
#: The profiling table's frame.
_BOX_CHARS = "╔╠║╚╟╢═╗╣╝"
#: Per-wedge rows under "[Profile] Top 5 slowest wedges" and "GPU poll + CPU
#: merge time per wedge": ``     W01: 540ms | …`` and ``     W01 7ms ████``.
_WEDGE_ROW = re.compile(r"^\s+W\d+[: ]")
#: The per-wedge progress line: ``[W01/02] 0.0°-360.0° | 8802 Blocks | …``.
_WEDGE_LINE = re.compile(r"^\[W\d+/\d+\]")


def is_diagnostic(line: str) -> bool:
    """Is *line* of aether_core's console for the file only, not the panel?

    An indented line is a continuation of a dump above it (the solver
    solution's ``  Theta (Wedge Angle): …`` rows, the per-wedge profile
    rows): file only — unless it says there is a problem."""
    s = line.strip()
    if not s:
        return True
    if _KEEP_WORDS.search(s):
        return False
    if line[:1].isspace():
        return True
    m = _ENV_LOGGER.match(s)
    if m:
        level, module = m.group(1).upper(), m.group(2)
        return (level in ("INFO", "DEBUG", "TRACE")
                and module.startswith(_DIAG_MODULES))
    if s[0] in _BOX_CHARS:
        return True
    if s.startswith(_NOISE_PREFIXES):
        return True
    if s.startswith("[Profile]"):
        return not s.startswith(_PROFILE_KEEP)
    return bool(_WEDGE_LINE.match(s) or _WEDGE_ROW.match(line))


def engine_log_path() -> str:
    """Today's engine log file, beside the converter diagnostics."""
    return os.path.join(terrain_adapter.get_cache_dir(), "logs",
                        f"aether_core-{time.strftime('%Y%m%d')}.log")


#: What this session announced last: today's path, or "!<dir>" for a
#: directory that could not be made (each logged once).
_announced: Optional[str] = None


class EngineLog:
    """One aether_core run's console: every line to today's file, the lines
    a user reads to *panel*. *label* names the run in the file. Never
    raises: a file that cannot be opened or written leaves the panel with
    every line (said once per directory)."""

    def __init__(self, label: str, panel: Callable[[str], None]) -> None:
        self._panel = panel
        self._fh: Optional[TextIO] = None
        self._open(label)

    def _open(self, label: str) -> None:
        global _announced
        log_dir = ""
        try:
            path = engine_log_path()
            log_dir = os.path.dirname(path)
            os.makedirs(log_dir, exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8", buffering=1)
            self._fh.write(f"=== {time.strftime('%Y-%m-%d %H:%M:%S')} "
                           f"aether_core — {label} ===\n")
            if _announced != path:
                _announced = path
                terrain_adapter._rotate_diag_files(log_dir)
                self._panel(f"engine log (full console): {path}")
        except Exception as exc:  # noqa: BLE001 — logging never stops a run
            self._close_file()
            if _announced != "!" + log_dir:
                _announced = "!" + log_dir
                self._panel(f"engine log: no file ({log_dir or 'cache dir'}: "
                            f"{exc}) — the panel shows every line")

    def line(self, text: str) -> None:
        """One console line: to the file; to the panel unless it is a
        diagnostic (every line when there is no file)."""
        if self._fh is not None:
            try:
                self._fh.write(text + "\n")
            except Exception:  # noqa: BLE001 — disk full, file gone: panel
                self._close_file()
        if self._fh is None or not is_diagnostic(text):
            self._panel(text)

    def _close_file(self) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            with contextlib.suppress(Exception):
                fh.close()

    def close(self) -> None:
        """End of the run: the file is closed (the panel is untouched)."""
        self._close_file()

    def __enter__(self) -> "EngineLog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
