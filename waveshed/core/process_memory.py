"""The QGIS process's own resident memory, for one log line at a run's start.

The owner's run of 2026-11-07: 31 GB of RAM, the converter processes' peaks
now in their own lines (toolkit changelog 50), and still about 20 GB in use
that no line attributes — QGIS itself, with a 43,650-file folder index and
whatever else the session holds. One line at the start of a Map Converter or
Site Analysis run names QGIS's share, so a log can say where the memory went.

Standard library only (no psutil in a QGIS plugin): Windows through
``GetProcessMemoryInfo`` (``WorkingSetSize`` / ``PeakWorkingSetSize``), Linux
through ``/proc/self/status`` (``VmRSS`` / ``VmHWM``), macOS through
``getrusage`` (``ru_maxrss`` in bytes; no current figure). Never raises:
``None`` where the OS does not say, and the line is then omitted.
"""

from __future__ import annotations

import contextlib
import sys
from typing import Optional, Tuple


def resident_bytes() -> Tuple[Optional[int], Optional[int]]:
    """``(current, peak)`` resident set of this process in bytes; either
    may be None when the platform does not report it."""
    with contextlib.suppress(Exception):  # informative only, never fatal
        if sys.platform == "win32":
            return _windows()
        if sys.platform.startswith("linux"):
            return _linux()
        if sys.platform == "darwin":
            return _macos()
    return None, None


def memory_line(label: str = "QGIS process") -> Optional[str]:
    """``  QGIS process: 4,812 MB resident (peak 6,201 MB)`` — or None when
    nothing is known."""
    current, peak = resident_bytes()
    if current is None and peak is None:
        return None
    parts = []
    if current is not None:
        parts.append(f"{current / 1024 ** 2:,.0f} MB resident")
    if peak is not None:
        parts.append(f"peak {peak / 1024 ** 2:,.0f} MB")
    return f"  {label}: " + (" (".join(parts) + ")" if len(parts) == 2
                             else parts[0])


#: Where ``GetProcessMemoryInfo`` lives, in the order tried: kernel32's
#: ``K32`` export (Windows 7 and later), then psapi.dll (older systems; on
#: newer ones a forwarder to the same function).
_WINDOWS_MEMORY_INFO = (("kernel32", "K32GetProcessMemoryInfo"),
                        ("psapi", "GetProcessMemoryInfo"))


def _windows() -> Tuple[Optional[int], Optional[int]]:
    """``(WorkingSetSize, PeakWorkingSetSize)`` of this process.

    Every prototype is declared: undeclared, ctypes treats the HANDLE
    ``GetCurrentProcess`` returns — pointer-sized on 64-bit Windows — as a C
    ``int``, so the pseudo-handle need not arrive intact. The functions are
    taken with ``dll[name]``, a fresh function object each time, never as
    the shared ``ctypes.windll.kernel32.<name>`` attribute: another module in
    the QGIS process may have declared that one its own way, and declaring
    ours there would change theirs. ``(None, None)`` when no function
    answers."""
    import ctypes
    from ctypes import wintypes

    class _Counters(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS (psapi.h)
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    get_current_process = ctypes.windll.kernel32["GetCurrentProcess"]
    get_current_process.argtypes = []
    get_current_process.restype = wintypes.HANDLE
    handle = get_current_process()  # the pseudo-handle, never closed
    for dll_name, func_name in _WINDOWS_MEMORY_INFO:
        counters = _Counters()
        counters.cb = ctypes.sizeof(_Counters)
        try:
            func = getattr(ctypes.windll, dll_name)[func_name]
            func.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Counters),
                             wintypes.DWORD]
            func.restype = wintypes.BOOL
            ok = func(handle, ctypes.byref(counters), counters.cb)
        except Exception:  # noqa: BLE001 — export or DLL missing: next one
            ok = False
        if ok:
            return (int(counters.WorkingSetSize),
                    int(counters.PeakWorkingSetSize))
    return None, None


def _linux() -> Tuple[Optional[int], Optional[int]]:
    with open("/proc/self/status", encoding="ascii", errors="replace") as fh:
        text = fh.read()
    return _proc_status_bytes(text, "VmRSS"), _proc_status_bytes(text, "VmHWM")


def _proc_status_bytes(text: str, key: str) -> Optional[int]:
    """The ``<key>:\\t  8576 kB`` value of a ``/proc/<pid>/status`` text in
    bytes, None when absent, not in kB or 0."""
    for line in text.splitlines():
        if line.startswith(key + ":"):
            value = line.split(":", 1)[1].strip()
            if not value.endswith("kB"):
                return None
            try:
                kb = int(value[:-2].strip())
            except ValueError:
                return None
            return kb * 1024 if kb > 0 else None
    return None


def _macos() -> Tuple[Optional[int], Optional[int]]:
    import resource
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # bytes on macOS
    return None, (int(peak) if peak > 0 else None)


__all__ = ["resident_bytes", "memory_line"]
