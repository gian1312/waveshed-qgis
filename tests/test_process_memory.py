"""waveshed.core.process_memory — QGIS's own resident memory for the one log
line at a run's start (owner, 2026-11-07: about 20 GB of the 31 GB in use
were attributed to nothing once the converters printed their peaks)."""

import conftest  # noqa: F401 — installs QGIS/PyQt stubs into sys.modules

import ctypes
import sys
import unittest
from ctypes import wintypes
from types import SimpleNamespace
from unittest import mock

from waveshed.core import process_memory as pm

STATUS = (
    "Name:\tQGIS\nVmPeak:\t 9000000 kB\nVmSize:\t 8000000 kB\n"
    "VmHWM:\t 6349824 kB\nVmRSS:\t 4927488 kB\nThreads:\t42\n"
)


class TestProcStatusParsing(unittest.TestCase):
    def test_rss_and_high_water_mark_in_bytes(self):
        self.assertEqual(pm._proc_status_bytes(STATUS, "VmRSS"),
                         4927488 * 1024)
        self.assertEqual(pm._proc_status_bytes(STATUS, "VmHWM"),
                         6349824 * 1024)

    def test_missing_zero_or_odd_unit_is_unknown(self):
        self.assertIsNone(pm._proc_status_bytes(STATUS, "VmSwap"))
        self.assertIsNone(pm._proc_status_bytes("VmRSS:\t 0 kB\n", "VmRSS"))
        self.assertIsNone(pm._proc_status_bytes("VmRSS:\t 12 MB\n", "VmRSS"))
        self.assertIsNone(pm._proc_status_bytes("VmRSS:\t x kB\n", "VmRSS"))


class TestMemoryLine(unittest.TestCase):
    def test_both_figures(self):
        with mock.patch.object(pm, "resident_bytes",
                               return_value=(4812 * 1024 ** 2,
                                             6201 * 1024 ** 2)):
            self.assertEqual(pm.memory_line(),
                             "  QGIS process: 4,812 MB resident (peak 6,201 MB)")

    def test_peak_only_as_on_macos(self):
        with mock.patch.object(pm, "resident_bytes",
                               return_value=(None, 300 * 1024 ** 2)):
            self.assertEqual(pm.memory_line("QGIS"), "  QGIS: peak 300 MB")

    def test_nothing_known_means_no_line(self):
        with mock.patch.object(pm, "resident_bytes",
                               return_value=(None, None)):
            self.assertIsNone(pm.memory_line())


#: What ``GetCurrentProcess`` returns through a HANDLE restype on 64-bit
#: Windows: the pseudo-handle ``(HANDLE)-1`` as an unsigned value.
PSEUDO_HANDLE = 2 ** 64 - 1
MB = 1024 ** 2


class _FakeFunc:
    """A ctypes foreign function stand-in: keeps the prototype it is given
    (``argtypes`` None and ``restype`` c_int, ctypes' undeclared defaults)
    and answers through *impl*."""

    def __init__(self, impl):
        self.impl = impl
        self.argtypes = None
        self.restype = ctypes.c_int
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.impl(*args)


class _FakeDll:
    """``ctypes.windll.<dll>``: ``dll[name]`` hands out a fresh function
    (KeyError → AttributeError, as for a missing export). Taking the shared
    attribute (``dll.name``) fails the test — declaring a prototype there
    would change it for every other module in the QGIS process."""

    def __init__(self, **exports):
        self.exports = exports
        self.handed_out = {}

    def __getitem__(self, name):
        if name not in self.exports:
            raise AttributeError(f"function {name!r} not found")
        func = self.handed_out[name] = _FakeFunc(self.exports[name])
        return func

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        raise AssertionError(f"the shared function object .{name} was used")


class _FakeWindll:
    def __init__(self, kernel32, psapi=None):
        self.kernel32 = kernel32
        self._psapi = psapi

    @property
    def psapi(self):
        if self._psapi is None:  # psapi.dll cannot be loaded
            raise OSError("[WinError 126] The specified module could not be found")
        return self._psapi


def _answers(working, peak, ok=1):
    """GetProcessMemoryInfo writing *working* / *peak* into the caller's
    PROCESS_MEMORY_COUNTERS (``ctypes.byref(...)._obj``), returning *ok*."""
    def impl(handle, counters_ref, cb):
        counters = counters_ref._obj
        assert cb == counters.cb == ctypes.sizeof(counters)
        if ok:
            counters.WorkingSetSize = working
            counters.PeakWorkingSetSize = peak
        return ok
    return impl


def _kernel32(**exports):
    return _FakeDll(GetCurrentProcess=lambda: PSEUDO_HANDLE, **exports)


class TestWindowsReader(unittest.TestCase):
    """``_windows()`` on any OS, through a fake ``ctypes.windll``: the owner's
    Windows QGIS logged no ``QGIS process:`` line (2026-11-07)."""

    def _run(self, windll):
        with mock.patch.object(ctypes, "windll", windll, create=True):
            return pm._windows()

    def assert_declared(self, func):
        self.assertIs(func.restype, wintypes.BOOL)
        handle_t, counters_p, size_t = func.argtypes
        self.assertIs(handle_t, wintypes.HANDLE)
        self.assertIs(size_t, wintypes.DWORD)
        layout = [(name, t) for name, t in counters_p._type_._fields_]
        self.assertEqual(
            layout,
            [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)]
            + [(name, ctypes.c_size_t) for name in (
                "PeakWorkingSetSize", "WorkingSetSize",
                "QuotaPeakPagedPoolUsage", "QuotaPagedPoolUsage",
                "QuotaPeakNonPagedPoolUsage", "QuotaNonPagedPoolUsage",
                "PagefileUsage", "PeakPagefileUsage")])

    def test_kernel32_export_first_with_declared_prototypes(self):
        psapi = _FakeDll(GetProcessMemoryInfo=_answers(1 * MB, 2 * MB))
        kernel32 = _kernel32(K32GetProcessMemoryInfo=_answers(4812 * MB, 6201 * MB))
        self.assertEqual(self._run(_FakeWindll(kernel32, psapi)),
                         (4812 * MB, 6201 * MB))
        gcp = kernel32.handed_out["GetCurrentProcess"]
        self.assertIs(gcp.restype, wintypes.HANDLE)
        self.assertEqual(gcp.argtypes, [])
        k32 = kernel32.handed_out["K32GetProcessMemoryInfo"]
        self.assert_declared(k32)
        self.assertEqual(len(k32.calls), 1)
        self.assertEqual(k32.calls[0][0], PSEUDO_HANDLE)  # passed on intact
        self.assertEqual(psapi.handed_out, {}, "psapi is only the fallback")

    def test_psapi_when_kernel32_has_no_k32_export(self):
        psapi = _FakeDll(GetProcessMemoryInfo=_answers(300 * MB, 400 * MB))
        self.assertEqual(self._run(_FakeWindll(_kernel32(), psapi)),
                         (300 * MB, 400 * MB))
        func = psapi.handed_out["GetProcessMemoryInfo"]
        self.assert_declared(func)
        self.assertEqual(func.calls[0][0], PSEUDO_HANDLE)

    def test_a_failing_call_is_unknown(self):
        kernel32 = _kernel32(K32GetProcessMemoryInfo=_answers(0, 0, ok=0))
        psapi = _FakeDll(GetProcessMemoryInfo=_answers(0, 0, ok=0))
        self.assertEqual(self._run(_FakeWindll(kernel32, psapi)), (None, None))
        self.assertEqual(len(kernel32.handed_out["K32GetProcessMemoryInfo"].calls), 1)
        self.assertEqual(len(psapi.handed_out["GetProcessMemoryInfo"].calls), 1)

    def test_a_call_that_raises_falls_back_then_is_unknown(self):
        def boom(*args):
            raise OSError("access violation")
        kernel32 = _kernel32(K32GetProcessMemoryInfo=boom)
        psapi = _FakeDll(GetProcessMemoryInfo=_answers(5 * MB, 6 * MB))
        self.assertEqual(self._run(_FakeWindll(kernel32, psapi)), (5 * MB, 6 * MB))
        self.assertEqual(self._run(_FakeWindll(_kernel32(K32GetProcessMemoryInfo=boom))),
                         (None, None))  # and psapi.dll does not even load

    def test_no_function_anywhere_is_unknown(self):
        self.assertEqual(self._run(_FakeWindll(_kernel32())), (None, None))

    def test_the_line_on_windows(self):
        kernel32 = _kernel32(K32GetProcessMemoryInfo=_answers(4812 * MB, 6201 * MB))
        with mock.patch.object(pm, "sys", SimpleNamespace(platform="win32")), \
                mock.patch.object(ctypes, "windll", _FakeWindll(kernel32), create=True):
            self.assertEqual(pm.memory_line(),
                             "  QGIS process: 4,812 MB resident (peak 6,201 MB)")


class TestThisPlatform(unittest.TestCase):
    def test_reads_this_process_without_raising(self):
        current, peak = pm.resident_bytes()
        if sys.platform.startswith("linux") or sys.platform == "win32":
            self.assertIsInstance(current, int)
            self.assertGreater(current, 1024 ** 2)
            self.assertIsInstance(peak, int)
            self.assertGreaterEqual(peak, current)
        elif sys.platform == "darwin":
            self.assertIsInstance(peak, int)

    def test_a_failing_platform_reader_is_swallowed(self):
        with mock.patch.object(pm, "_linux", side_effect=OSError("no proc")), \
                mock.patch.object(pm, "_windows",
                                  side_effect=OSError("no psapi")), \
                mock.patch.object(pm, "_macos", side_effect=OSError("no ru")):
            self.assertEqual(pm.resident_bytes(), (None, None))
            self.assertIsNone(pm.memory_line())


if __name__ == "__main__":
    unittest.main()
