"""One progress bar for a multi-phase run, weighted by the work it really does.

The Map Converter used to split its bar into fixed phase spans (resolve
0-20 %, download 20-70 %, convert 70-100 %), whatever each phase cost, and the
download span was fed by a max-only per-group fraction — so a gap-repair retry
pass (which reports small ``done/total`` values again) left the bar frozen
while tiles were still being fetched.

:class:`WorkProgress` replaces that with *slots* of estimated work in one
common unit (one downloaded 256² source tile = 1 unit; see the ``*_UNITS_*``
weights). Slots may be re-estimated or grow at any time (a retry pass adds its
tiles when it starts). The shown fraction ``f`` follows the *remaining-work*
rule: finishing ``Δ`` units advances ``f`` by ``Δ · (1 − f) / remaining``.
Hence

* ``f`` never decreases — added work only slows the bar down;
* every unit of finished work moves it — it cannot freeze while work is done;
* it reaches exactly 1.0 when the last outstanding unit finishes.

The converter prints download progress only every 10 % of a pass and ingest
progress every 10 tiles, so between two lines the bar may also creep forward
at the smoothed rate — never past the point the next line is certain to
report (*ceiling*), so the creep is always confirmed, never taken back.

Pure Python, thread-safe (one worker thread writes, the GUI timer reads),
no Qt / QGIS imports.
"""

from __future__ import annotations

import math
import re
import threading
import time
from typing import Any, Callable, Dict, Hashable, List, NamedTuple, Optional, Tuple

#: Work units of one output pixel block of 256×256 px, per activity. One
#: downloaded + decoded 256² XYZ source tile is the unit (1.0). These are
#: rough relative costs; a wrong ratio only changes how fast the bar moves in
#: one phase relative to another (and the ETA early in a run) — it can never
#: make the bar go backwards or stop.
DOWNLOAD_UNITS_PER_SOURCE_TILE = 1.0
CONVERT_UNITS_PER_BLOCK = 0.5      # aether_converter ingest, per 256² output px
RENDER_UNITS_PER_BLOCK = 4.0       # QGIS writeRaster of a rendered server

_BLOCK_PX = 256 * 256


def tile_blocks(size_px: int) -> float:
    """Number of 256² blocks in a *size_px*² output tile."""
    return max(1.0, float(size_px) * float(size_px) / _BLOCK_PX)


# ---------------------------------------------------------------------------
# Converter output — only lines the toolkit already prints (see
# aether-tools crates/aether_converter/src/download.rs and main.rs).
# ---------------------------------------------------------------------------

#: ``[Download] {pct}% ({done}/{total}) — {mb:.1} MB/s, {n} errors, {m} in-flight``
#: (download.rs, printed at every 10 % of a pass and at the end). The tail is
#: optional so an older/shorter line still yields the counts.
_DL_LINE_RE = re.compile(
    r"\[Download\]\s+(\d+)%\s+\((\d+)/(\d+)\)"
    r"(?:.*?([\d.]+)\s*MB/s)?(?:.*?(\d+)\s+errors?)?(?:.*?(\d+)\s+in-flight)?")
#: ``[Download] z={z} tiles={nx}x{ny}={total} (~{mb}MB) connections={c}`` —
#: printed once per pass before any tile is fetched: the pass total, up front.
_DL_HEADER_RE = re.compile(r"\[Download\]\s+z=(\d+)\s+tiles=(\d+)x(\d+)=(\d+)")
#: ``[Rust] Progress: {curr}/{total}`` — ingest, every 10 tiles and at the end.
_INGEST_RE = re.compile(r"Progress:\s*(\d+)/(\d+)")


class DownloadLine(NamedTuple):
    pct: int
    done: int
    total: int
    mbps: Optional[float]
    errors: Optional[int]
    in_flight: Optional[int]


def parse_download_line(line: str) -> Optional[DownloadLine]:
    """The fields of a ``[Download] X% (a/b) — …`` progress line, or None."""
    m = _DL_LINE_RE.search(line)
    if not m:
        return None
    mbps = float(m.group(4)) if m.group(4) else None
    errs = int(m.group(5)) if m.group(5) else None
    infl = int(m.group(6)) if m.group(6) else None
    return DownloadLine(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        mbps, errs, infl)


def parse_download_header(line: str) -> Optional[Tuple[int, int]]:
    """``(zoom, source tiles)`` from a pass header line, or None."""
    m = _DL_HEADER_RE.search(line)
    return (int(m.group(1)), int(m.group(4))) if m else None


def parse_ingest_progress(line: str) -> Optional[Tuple[int, int]]:
    """``(done, total)`` from an ingest ``Progress: a/b`` line, or None."""
    m = _INGEST_RE.search(line)
    return (int(m.group(1)), int(m.group(2))) if m else None


def next_download_report(done: int, total: int) -> int:
    """The ``done`` value of the converter's NEXT progress line in a pass.

    It prints when ``done*100//total`` crosses a multiple of 10, and at the
    end — so the next line is at the first decile boundary above *done*.
    """
    if total <= 0:
        return done
    pct = done * 100 // total
    target = (pct // 10 + 1) * 10
    nxt = -(-target * total // 100)        # ceil(target * total / 100)
    while nxt * 100 // total < target:     # integer-exact
        nxt += 1
    return min(max(nxt, done + 1), total)


def next_ingest_report(done: int, total: int) -> int:
    """The next ``Progress:`` value the ingest prints (every 10, and the end)."""
    return min((done // 10 + 1) * 10, total) if total > 0 else done


def format_eta(seconds: Optional[float]) -> str:
    """``"ETA 45 s"`` / ``"ETA 3 min 20 s"`` / ``"ETA 1 h 05 min"``; "" if unknown."""
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return ""
    s = int(round(seconds))
    if s < 60:
        return f"ETA {max(s, 1)} s"
    if s < 3600:
        return f"ETA {s // 60} min {s % 60:02d} s"
    return f"ETA {s // 3600} h {(s % 3600) // 60:02d} min"


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

class WorkProgress:
    """Monotonic fraction over re-estimable slots of work (see module doc)."""

    #: Smoothing factor of the rate EMA and the minimum sample window.
    RATE_ALPHA = 0.3
    RATE_MIN_SAMPLE_S = 1.0
    #: No ETA before this much of the run has been measured.
    ETA_MIN_ELAPSED_S = 3.0
    #: Quiet longer than this (no tile finished) and the ETA stretches with
    #: the silence instead of standing still (rate × QUIET_S / quiet).
    QUIET_S = 5.0

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._total: Dict[Hashable, float] = {}
        self._done: Dict[Hashable, float] = {}
        self._f = 0.0
        # Extrapolation between converter lines: units already shown beyond
        # the real done count of slot `_ceil_key`, capped at `_ceil`.
        self._ceil_key: Optional[Hashable] = None
        self._ceil = 0.0
        self._virtual = 0.0
        self._last_t: Optional[float] = None
        self._progress_t: Optional[float] = None   # last real advance
        # Rate (units / s), exponentially smoothed over >= 1 s windows.
        self._t0 = self._clock()
        self._sample_t = self._t0
        self._acc = 0.0
        self._rate: Optional[float] = None

    # -- totals ------------------------------------------------------------
    def set_work(self, key: Hashable, units: float) -> None:
        """(Re-)estimate slot *key*'s total. Never below what it has done."""
        with self._lock:
            done = self._done.get(key, 0.0)
            self._total[key] = max(float(units), done, 0.0)
            self._done.setdefault(key, 0.0)

    def add_work(self, key: Hashable, units: float) -> None:
        with self._lock:
            self.set_work(key, self._total.get(key, 0.0) + max(0.0, units))

    def work(self, key: Hashable) -> float:
        with self._lock:
            return self._total.get(key, 0.0)

    def done(self, key: Hashable) -> float:
        with self._lock:
            return self._done.get(key, 0.0)

    @property
    def total_units(self) -> float:
        with self._lock:
            return sum(self._total.values())

    @property
    def done_units(self) -> float:
        with self._lock:
            return sum(self._done.values())

    # -- progress ----------------------------------------------------------
    def set_done(self, key: Hashable, units: float,
                 ceiling: Optional[float] = None) -> float:
        """Slot *key* has done *units* (absolute; lower values are ignored).

        *ceiling*, when given, is the done count the slot's NEXT report is
        certain to reach; until it arrives the shown value may creep towards
        it (:meth:`shown`). Returns the new fraction.
        """
        with self._lock:
            now = self._clock()
            old = self._done.get(key, 0.0)
            new = max(old, float(units))
            if new > self._total.get(key, 0.0):
                self._total[key] = new          # the estimate was low
            delta = new - old
            if delta > 0:
                virtual = self._virtual if key == self._ceil_key else 0.0
                # Outstanding units BEFORE this update (this slot's delta is
                # still in it), minus what the creep already showed.
                remaining = self._remaining_locked() - virtual
                eff = delta - virtual
                if key == self._ceil_key:
                    self._virtual = max(0.0, virtual - delta)
                self._done[key] = new
                if eff > 0 and remaining > 0:
                    self._f += eff * (1.0 - self._f) / remaining
                if self._remaining_locked() <= 1e-9:
                    self._f = 1.0
                self._f = min(self._f, 1.0)
                self._sample_rate_locked(now, delta)
                self._progress_t = now
            else:
                self._done.setdefault(key, 0.0)
            if ceiling is not None:
                if key != self._ceil_key:
                    self._virtual = 0.0
                self._ceil_key = key
                self._ceil = min(float(ceiling), self._total.get(key, 0.0))
            elif key == self._ceil_key and delta > 0:
                self._ceil_key = None
                self._virtual = 0.0
            self._last_t = now
            return self._f

    def advance(self, key: Hashable, units: float,
                ceiling: Optional[float] = None) -> float:
        with self._lock:
            return self.set_done(key, self._done.get(key, 0.0) + units, ceiling)

    def finish(self, key: Hashable) -> float:
        """Slot *key* is complete (whatever its estimate said)."""
        with self._lock:
            return self.set_done(key, self._total.get(key, 0.0))

    def finish_where(self, pred: Callable[[Hashable], bool]) -> float:
        """Finish every slot whose key satisfies *pred* (estimates a phase
        registered but never needed must not keep the bar below 100 %)."""
        with self._lock:
            for key in [k for k in self._total if pred(k)]:
                self.finish(key)
            return self._f

    def finish_all(self) -> float:
        return self.finish_where(lambda _k: True)

    def _remaining_locked(self) -> float:
        return sum(max(0.0, self._total[k] - self._done.get(k, 0.0))
                   for k in self._total)

    def _sample_rate_locked(self, now: float, delta: float) -> None:
        self._acc += delta
        dt = now - self._sample_t
        if dt >= self.RATE_MIN_SAMPLE_S:
            inst = self._acc / dt
            self._rate = (inst if self._rate is None else
                          self.RATE_ALPHA * inst
                          + (1.0 - self.RATE_ALPHA) * self._rate)
            self._acc = 0.0
            self._sample_t = now

    # -- reading -----------------------------------------------------------
    def fraction(self) -> float:
        with self._lock:
            return self._f

    def shown(self, now: Optional[float] = None) -> float:
        """The displayed fraction: :meth:`fraction` plus the confirmed creep
        towards the active slot's ceiling at the smoothed rate. Monotonic."""
        with self._lock:
            now = self._clock() if now is None else now
            key = self._ceil_key
            if (key is not None and self._rate and self._last_t is not None
                    and self._f < 1.0):
                room = self._ceil - self._done.get(key, 0.0)
                target = min(room, self._rate * max(0.0, now - self._last_t))
                extra = target - self._virtual
                if extra > 0:
                    remaining = self._remaining_locked() - self._virtual
                    if remaining > extra:
                        self._f += extra * (1.0 - self._f) / remaining
                        self._virtual = target
            return self._f

    def rate(self) -> Optional[float]:
        with self._lock:
            return self._rate

    def eta_s(self, now: Optional[float] = None) -> Optional[float]:
        """Seconds left at the smoothed rate; None until it is measured."""
        with self._lock:
            now = self._clock() if now is None else now
            if (not self._rate or self._rate <= 0
                    or now - self._t0 < self.ETA_MIN_ELAPSED_S):
                return None
            rate = self._rate
            quiet = self.quiet_s(now)
            if quiet > self.QUIET_S:
                rate *= self.QUIET_S / quiet
            remaining = max(0.0, self._remaining_locked() - self._virtual)
            return remaining / rate

    def quiet_s(self, now: Optional[float] = None) -> float:
        """Seconds since work last advanced (0 before the first advance)."""
        with self._lock:
            if self._progress_t is None:
                return 0.0
            now = self._clock() if now is None else now
            return max(0.0, now - self._progress_t)


class RunProgress(WorkProgress):
    """:class:`WorkProgress` plus the status line the GUI shows under the bar.

    The worker states its phase (``set_phase``) and, while it counts
    something, the counters (``set_detail``); :meth:`snapshot` renders
    ``(percent, label)`` — e.g. ``Downloading terrain z15 (pass 2/4):
    1,240/3,600 source tiles · 12.4 MB/s · 3 errors · ETA 2 min 05 s``.
    """

    #: Quiet this long and the label says so.
    STALL_NOTE_S = 10.0

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        super().__init__(clock)
        self._phase = ""
        self._detail: Dict[str, Any] = {}
        self._stopped = False

    def set_phase(self, text: str, detail: Optional[Dict[str, Any]] = None,
                  reset: bool = False) -> None:
        """State the phase; a new *text* (or *reset*) clears the counters."""
        with self._lock:
            if reset or text != self._phase:
                self._detail = {}
            self._phase = text
            if detail:
                self._detail.update({k: v for k, v in detail.items()
                                     if v is not None})

    def stop(self, text: str) -> None:
        """The run ended (done, cancelled, failed): no creep, no ETA."""
        with self._lock:
            self._stopped = True
            self._phase = text
            self._detail = {}

    @property
    def stopped(self) -> bool:
        return self._stopped

    def shown(self, now: Optional[float] = None) -> float:
        with self._lock:
            if self._stopped:
                return self._f
            return super().shown(now)

    def set_detail(self, **detail: Any) -> None:
        with self._lock:
            self._detail.update({k: v for k, v in detail.items()
                                 if v is not None})

    def percent(self, now: Optional[float] = None) -> int:
        f = self.shown(now)
        # 100 only when the work really is complete.
        return 100 if f >= 1.0 else min(99, int(f * 100))

    def label(self, now: Optional[float] = None) -> str:
        with self._lock:
            d = dict(self._detail)
            text = self._phase
            if d.get("pass") and d.get("passes") and d["pass"] > 1:
                text += f" (pass {d['pass']}/{d['passes']})"
            parts: List[str] = []
            if d.get("total"):
                parts.append(f"{int(d.get('done', 0)):,}/{int(d['total']):,} "
                             f"{d.get('unit', 'tiles')}")
            if d.get("mbps") is not None:
                parts.append(f"{d['mbps']:.1f} MB/s")
            if d.get("errors"):
                parts.append(f"{d['errors']} error"
                             + ("s" if d["errors"] != 1 else ""))
            running = self._f < 1.0 and not self._stopped
            quiet = self.quiet_s(now) if running else 0.0
            if quiet >= self.STALL_NOTE_S:
                # Honest about a stall (e.g. the converter backing off on a
                # few failing tiles): the bar holds, the label says why.
                parts.append(f"no tile finished for {int(quiet)} s")
            eta = format_eta(self.eta_s(now)) if running else ""
            if eta:
                parts.append(eta)
            if parts:
                text = (text + ": " if text else "") + " · ".join(parts)
            return text

    def snapshot(self, now: Optional[float] = None) -> Tuple[int, str]:
        with self._lock:
            return self.percent(now), self.label(now)
