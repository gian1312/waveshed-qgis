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
report (*ceiling*), so the creep is always confirmed, never taken back. The
creep is kept PER SLOT (own ceiling, own rate, own shown-ahead units): the Map
Converter converts from files while it downloads, and two slots reporting in
turn must neither reset nor double-count each other's creep.

An ingest batch whose jobs carry ``buildings_pbf_dir`` first decodes the
building tiles (toolkit 0.2.9: ``[Rust] Decoding building tiles: n/m`` every
1,000 tiles, ``[Rust] Building tiles decoded: …`` at the end): that decode is
the first :data:`BUILDING_DECODE_SHARE` of the batch's slot, so the bar moves
and creeps through it. The ETA is blanked (:data:`ETA_UNKNOWN`) once no slot
has reported progress for :attr:`WorkProgress.ETA_BLANK_S` — a silence is
never extrapolated.

Pure Python, thread-safe (worker threads write — several at once while the
Map Converter downloads and converts concurrently — the GUI timer reads),
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
#: ``[Rust] Decoding building tiles: {n}/{m}`` — an ingest batch decoding its
#: ``buildings_pbf_dir``, every :data:`BUILDING_DECODE_STEP` tiles (toolkit
#: 0.2.9), before its first ``Progress:`` line. Thousands separators are
#: tolerated.
_DECODE_RE = re.compile(
    r"\[Rust\] Decoding building tiles:\s*([\d,]+)\s*/\s*([\d,]+)")
#: ``[Rust] Building tiles decoded: {m} tile(s), {b} building(s) in {s} s`` —
#: the end of that decode (toolkit 0.2.9).
_DECODED_RE = re.compile(
    r"\[Rust\] Building tiles decoded:\s*([\d,]+)\s*tile\(s\)"
    r"(?:,\s*([\d,]+)\s*building\(s\))?(?:\s*in\s*([\d.]+)\s*s)?")

#: The converter prints its building-decode progress every this many tiles.
BUILDING_DECODE_STEP = 1000
#: Share of an ingest batch's work its building decode stands for (a batch
#: whose jobs carry ``buildings_pbf_dir``): the decode is the first part of
#: the batch, so the bar moves — and creeps — through it instead of standing
#: still until the first ``Progress:`` line. A rough share; a wrong one only
#: changes how fast the bar moves in that part.
BUILDING_DECODE_SHARE = 0.1


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


def _count(text: Optional[str]) -> int:
    return int(text.replace(",", "")) if text else 0


def parse_building_decode(line: str) -> Optional[Tuple[int, int]]:
    """``(decoded, total)`` from a ``[Rust] Decoding building tiles: n/m``
    line, or None (also for the header, ``… tiles in "<dir>"`` — or, with
    ``buildings_pbf_list`` (toolkit 0.2.14), ``[Rust] Decoding N listed
    building tiles from "<dir>"`` — which moves no bar)."""
    m = _DECODE_RE.search(line)
    return (_count(m.group(1)), _count(m.group(2))) if m else None


def parse_buildings_decoded(line: str
                            ) -> Optional[Tuple[int, int, Optional[float]]]:
    """``(tiles, buildings, seconds)`` from the ``[Rust] Building tiles
    decoded: …`` line that ends the decode, or None."""
    m = _DECODED_RE.search(line)
    if not m:
        return None
    return (_count(m.group(1)), _count(m.group(2)),
            float(m.group(3)) if m.group(3) else None)


def next_decode_report(done: int, total: int) -> int:
    """The decoded count the NEXT decode line is certain to reach (the next
    multiple of :data:`BUILDING_DECODE_STEP`, or the end line's total)."""
    if total <= 0:
        return done
    return min((done // BUILDING_DECODE_STEP + 1) * BUILDING_DECODE_STEP,
               total)


#: The label's ETA while the run is stalled (:attr:`WorkProgress.ETA_BLANK_S`).
ETA_UNKNOWN = "ETA —"


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
    #: No slot has reported progress for this long: the ETA is unknown
    #: (:meth:`eta_s` None, the label says :data:`ETA_UNKNOWN`) — stretching
    #: the rate over minutes of silence (a repair pass being prepared, a
    #: decode) extrapolated to "days" on the owner's run.
    ETA_BLANK_S = 30.0

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        self._clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._total: Dict[Hashable, float] = {}
        self._done: Dict[Hashable, float] = {}
        self._f = 0.0
        # Extrapolation between converter lines, per slot: the done count its
        # next report is certain to reach (`_ceil`), the units already shown
        # beyond its real done count (`_virtual`), its last report time and
        # its own smoothed rate (window start + units since, then the EMA).
        self._ceil: Dict[Hashable, float] = {}
        self._virtual: Dict[Hashable, float] = {}
        self._slot_t: Dict[Hashable, float] = {}
        self._slot_win: Dict[Hashable, Tuple[float, float]] = {}
        self._slot_rate: Dict[Hashable, float] = {}
        self._progress_t: Optional[float] = None   # last real advance
        # Rate (units / s), exponentially smoothed over >= 1 s windows.
        self._t0 = self._clock()
        self._sample_t = self._t0
        self._acc = 0.0
        self._rate: Optional[float] = None

    # -- totals ------------------------------------------------------------
    def set_work(self, key: Hashable, units: float) -> None:
        """(Re-)estimate slot *key*'s total. Never below what it has done.

        A slot that has done nothing yet starts its rate window now (it is
        sized right before its work starts)."""
        with self._lock:
            done = self._done.get(key, 0.0)
            self._total[key] = max(float(units), done, 0.0)
            self._done.setdefault(key, 0.0)
            if done <= 0.0:
                self._slot_win[key] = (self._clock(), 0.0)

    def add_work(self, key: Hashable, units: float) -> None:
        with self._lock:
            self.set_work(key, self._total.get(key, 0.0) + max(0.0, units))

    def work(self, key: Hashable) -> float:
        with self._lock:
            return self._total.get(key, 0.0)

    def keys(self) -> List[Hashable]:
        """The registered slots — e.g. to re-estimate every slot of one kind
        (the Map Converter zeroes its download slots before sizing them from
        the tiles Phase 2 left)."""
        with self._lock:
            return list(self._total)

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
                virtual = self._virtual.get(key, 0.0)
                # Outstanding units BEFORE this update (this slot's delta is
                # still in it), minus what the creep of EVERY slot already
                # showed; of the delta only what this slot's creep has not
                # shown yet moves the bar.
                remaining = self._remaining_locked() - self._virtual_total()
                eff = delta - virtual
                self._virtual[key] = max(0.0, virtual - delta)
                self._done[key] = new
                if eff > 0 and remaining > 0:
                    self._f += eff * (1.0 - self._f) / remaining
                if self._remaining_locked() <= 1e-9:
                    self._f = 1.0
                self._f = min(self._f, 1.0)
                self._sample_rate_locked(now, delta)
                self._sample_slot_rate_locked(key, now, delta)
                self._progress_t = now
            else:
                self._done.setdefault(key, 0.0)
            if ceiling is not None:
                self._ceil[key] = min(float(ceiling),
                                      self._total.get(key, 0.0))
            elif delta > 0:
                self._ceil.pop(key, None)
                self._virtual.pop(key, None)
            self._slot_t[key] = now
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

    def _virtual_total(self) -> float:
        return sum(self._virtual.values())

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

    def _sample_slot_rate_locked(self, key: Hashable, now: float,
                                 delta: float) -> None:
        """The slot's own rate — what its creep runs at (the run's rate sums
        every concurrent slot)."""
        start, acc = self._slot_win.get(key, (self._t0, 0.0))
        acc += delta
        dt = now - start
        if dt >= self.RATE_MIN_SAMPLE_S:
            inst = acc / dt
            old = self._slot_rate.get(key)
            self._slot_rate[key] = (inst if old is None else
                                    self.RATE_ALPHA * inst
                                    + (1.0 - self.RATE_ALPHA) * old)
            start, acc = now, 0.0
        self._slot_win[key] = (start, acc)

    # -- reading -----------------------------------------------------------
    def fraction(self) -> float:
        with self._lock:
            return self._f

    def shown(self, now: Optional[float] = None) -> float:
        """The displayed fraction: :meth:`fraction` plus the confirmed creep
        of every slot towards its own ceiling at its own smoothed rate.
        Monotonic."""
        with self._lock:
            now = self._clock() if now is None else now
            if self._f >= 1.0:
                return self._f
            for key, ceil in list(self._ceil.items()):
                rate = self._slot_rate.get(key)
                last = self._slot_t.get(key)
                if not rate or last is None:
                    continue
                room = ceil - self._done.get(key, 0.0)
                target = min(room, rate * max(0.0, now - last))
                extra = target - self._virtual.get(key, 0.0)
                if extra > 0:
                    remaining = self._remaining_locked() - self._virtual_total()
                    if remaining > extra:
                        self._f += extra * (1.0 - self._f) / remaining
                        self._virtual[key] = target
            return self._f

    def rate(self) -> Optional[float]:
        with self._lock:
            return self._rate

    def eta_s(self, now: Optional[float] = None) -> Optional[float]:
        """Seconds left at the smoothed rate; None until it is measured, and
        None while no slot has reported progress for :data:`ETA_BLANK_S`
        (:meth:`eta_stalled`)."""
        with self._lock:
            now = self._clock() if now is None else now
            if (not self._rate or self._rate <= 0
                    or now - self._t0 < self.ETA_MIN_ELAPSED_S):
                return None
            rate = self._rate
            quiet = self.quiet_s(now)
            if quiet >= self.ETA_BLANK_S:
                return None
            if quiet > self.QUIET_S:
                rate *= self.QUIET_S / quiet
            remaining = max(0.0, self._remaining_locked()
                            - self._virtual_total())
            return remaining / rate

    def eta_stalled(self, now: Optional[float] = None) -> bool:
        """An ETA was being measured, but no slot has reported progress for
        :data:`ETA_BLANK_S`: the label shows :data:`ETA_UNKNOWN` instead of
        extrapolating the silence."""
        with self._lock:
            now = self._clock() if now is None else now
            return (bool(self._rate) and self._progress_t is not None
                    and now - self._t0 >= self.ETA_MIN_ELAPSED_S
                    and self.quiet_s(now) >= self.ETA_BLANK_S)

    def eta_text(self, now: Optional[float] = None) -> str:
        """The label's ETA part: ``ETA 3 min 20 s``, :data:`ETA_UNKNOWN`
        while stalled (:meth:`eta_stalled`), "" before it is measured."""
        with self._lock:
            if self.eta_stalled(now):
                return ETA_UNKNOWN
            return format_eta(self.eta_s(now))

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

    Work that runs CONCURRENTLY (the Map Converter's Phase 2 next to its pool
    downloads) is stated per activity (``set_activity`` / ``end_activity``):
    while any is active the label names each with its own counters, in start
    order, joined by ``"; "`` — ``Converting tiles from local files: 120/531
    tiles; Downloading terrain z15 (2, 5 m): 12,345/193,978 source tiles,
    25.1 MB/s · ETA 12 min 30 s`` — the run's stall note and ETA once at the
    end.
    """

    #: Quiet this long and the label says so.
    STALL_NOTE_S = 10.0

    def __init__(self, clock: Optional[Callable[[], float]] = None) -> None:
        super().__init__(clock)
        self._phase = ""
        self._detail: Dict[str, Any] = {}
        self._stopped = False
        # Concurrent activities: key -> [text, counters], in start order.
        self._acts: Dict[Hashable, List[Any]] = {}

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

    def set_activity(self, key: Hashable, text: str,
                     detail: Optional[Dict[str, Any]] = None,
                     reset: bool = False) -> None:
        """State one of the activities running at the same time (its place
        in the label is kept while it runs); a new *text* (or *reset*)
        clears its counters."""
        with self._lock:
            act = self._acts.get(key)
            if act is None or reset or act[0] != text:
                act = [text, {}]
                self._acts[key] = act
            if detail:
                act[1].update({k: v for k, v in detail.items()
                               if v is not None})

    def end_activity(self, key: Hashable) -> None:
        with self._lock:
            self._acts.pop(key, None)

    def activities(self) -> List[str]:
        """The texts of the activities running now, in start order."""
        with self._lock:
            return [t for t, _d in self._acts.values()]

    def stop(self, text: str) -> None:
        """The run ended (done, cancelled, failed): no creep, no ETA."""
        with self._lock:
            self._stopped = True
            self._phase = text
            self._detail = {}
            self._acts = {}

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

    @staticmethod
    def _counters(text: str, d: Dict[str, Any]) -> Tuple[str, List[str]]:
        """*text* with its pass note, and the counter parts of *d*."""
        if d.get("repair") and d.get("pass") and d.get("passes"):
            text += f" (repair pass {d['pass']}/{d['passes']})"
        elif d.get("pass") and d.get("passes") and d["pass"] > 1:
            text += f" (pass {d['pass']}/{d['passes']})"
        parts: List[str] = []
        if d.get("total") and d.get("step"):
            # A step of the activity with its own counter, named first:
            # ``decoding building tiles 12,000/20,808``.
            parts.append(f"{d['step']} {int(d.get('done', 0)):,}/"
                         f"{int(d['total']):,}")
        elif d.get("step"):
            # A step without a counter: ``waiting for building tiles``.
            parts.append(str(d["step"]))
        elif d.get("total"):
            parts.append(f"{int(d.get('done', 0)):,}/{int(d['total']):,} "
                         f"{d.get('unit', 'tiles')}")
        if d.get("mbps") is not None:
            parts.append(f"{d['mbps']:.1f} MB/s")
        if d.get("errors"):
            parts.append(f"{d['errors']} error"
                         + ("s" if d["errors"] != 1 else ""))
        return text, parts

    def label(self, now: Optional[float] = None) -> str:
        with self._lock:
            running = self._f < 1.0 and not self._stopped
            if self._acts and not self._stopped:
                shown = []
                for act_text, d in self._acts.values():
                    act_text, counts = self._counters(act_text, d)
                    shown.append(act_text + (": " + ", ".join(counts)
                                             if counts else ""))
                text, parts = "; ".join(shown), []
                sep = " · "
            else:
                text, parts = self._counters(self._phase, self._detail)
                sep = ": "
            quiet = self.quiet_s(now) if running else 0.0
            if quiet >= self.STALL_NOTE_S:
                # Honest about a stall (e.g. the converter backing off on a
                # few failing tiles): the bar holds, the label says why.
                parts.append(f"no tile finished for {int(quiet)} s")
            eta = self.eta_text(now) if running else ""
            if eta:
                parts.append(eta)
            if parts:
                text = (text + sep if text else "") + " · ".join(parts)
            return text

    def snapshot(self, now: Optional[float] = None) -> Tuple[int, str]:
        with self._lock:
            return self.percent(now), self.label(now)
