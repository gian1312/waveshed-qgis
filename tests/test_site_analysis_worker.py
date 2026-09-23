"""Unit tests for the Site Analysis worker's failure contract.

QGIS (and the PyQt stubs the worker's QThread needs) come from conftest.py.

Pinned behaviour (2026-09-21): when ``aether_core`` exits non-zero for one
(site x altitude) job, the run STOPS there — ``finished_err`` fires exactly
once, ``finished_ok`` never, and no terrain is prepared for the jobs that
follow. A worker that warned and carried on would spend the full terrain
bill (1 deg x 1 deg tiles at 90 m, minutes each) on jobs the engine will
refuse identically.

The engine's ``[E:Unsupported resolution ...]`` refusal (a hardcoded GPU-path
list in engine builds up to v0.4.2, removed since) additionally gets an
actionable hint appended, because the plugin itself offers 90/250 m and the
raw engine line reads as if the plugin were wrong.
"""

# Bootstrap stubs before any plugin import.
import conftest  # noqa: F401

import os
import tempfile
import unittest
from unittest import mock

from waveshed.core import binary_manager as bm
from waveshed.core.binary_manager import engine_error_hint
from waveshed.core.job_builder import CoverageParams
import waveshed.gui.site_analysis_tab as sat


_ENGINE_TAIL = [
    "[Core] Compute backend: GPU (auto-detected)",
    "[S:Configuring Job (Mode: LOS)]",
    "[E:Unsupported resolution 90m. Allowed: [2.0, 5.0, 10.0, 30.0]]",
]


def _jobs(n: int):
    return [
        (
            CoverageParams(
                tx_lat=46.8345, tx_lon=9.7948, resolution_m=90,
                max_range_km=350, output_name=f"site1_alt{i + 1}",
            ),
            f"Site 1 (46.8345, 9.7948) @ {500.0 * (i + 1):.1f}m AGL",
        )
        for i in range(n)
    ]


class EngineErrorHintTests(unittest.TestCase):

    def test_unsupported_resolution_names_the_engine_update(self):
        hint = engine_error_hint("\n".join(_ENGINE_TAIL))
        self.assertIn("Update the engine", hint)
        self.assertIn("2, 5, 10 and 30 m", hint)

    def test_unknown_failure_gets_no_hint(self):
        self.assertEqual(engine_error_hint("[E:terrain tile missing]"), "")
        self.assertEqual(engine_error_hint(""), "")


class WorkerAbortsOnFirstEngineFailure(unittest.TestCase):

    def _run(self, n_jobs: int, engine_version="0.4.3"):
        """Drive the shipped worker with the engine failing on job 1."""
        errs, oks = [], []
        with tempfile.TemporaryDirectory() as d:
            worker = sat._SiteAnalysisWorker(
                _jobs(n_jobs), mock.Mock(), os.path.join(d, "out"))
            worker.finished_err.connect(errs.append)
            worker.finished_ok.connect(oks.append)

            def fail_engine(exe, args, on_line, **kw):
                for line in _ENGINE_TAIL:
                    on_line(line)
                return 1

            with mock.patch.object(
                sat, "prepare_terrain", return_value=os.path.join(d, "abt")
            ) as prep, mock.patch.object(
                bm, "read_engine_version", return_value=engine_version
            ), mock.patch.object(
                sat, "find_binary", return_value="aether_core"
            ), mock.patch.object(
                sat.api_key, "apply_license_env"
            ), mock.patch.object(
                sat.terrain_adapter, "run_converter_streaming",
                side_effect=fail_engine,
            ) as core, mock.patch.object(
                sat.subprocess, "run"
            ) as export:
                worker.run()
        return errs, oks, prep, core, export

    def test_first_failure_ends_the_run(self):
        errs, oks, prep, core, export = self._run(n_jobs=3)
        self.assertEqual(len(errs), 1, errs)
        self.assertEqual(oks, [])
        # Nothing after the failing job: no more terrain, no more engine
        # calls, and no export of a result that does not exist.
        self.assertEqual(prep.call_count, 1)
        self.assertEqual(core.call_count, 1)
        export.assert_not_called()

    def test_error_names_the_job_and_carries_the_hint(self):
        errs, _oks, *_ = self._run(n_jobs=1)
        msg = errs[0]
        self.assertIn("aether_core exited with code 1", msg)
        self.assertIn("Site 1 (46.8345, 9.7948) @ 500.0m AGL", msg)
        self.assertIn("[E:Unsupported resolution 90m", msg)
        self.assertIn("Update the engine", msg)

    def test_old_engine_is_refused_before_any_terrain(self):
        """A 90 m job on an engine that predates --version never downloads."""
        errs, oks, prep, core, export = self._run(n_jobs=2, engine_version=None)
        self.assertEqual(len(errs), 1, errs)
        self.assertEqual(oks, [])
        prep.assert_not_called()
        core.assert_not_called()
        export.assert_not_called()
        self.assertIn("cannot run a 90 m analysis", errs[0])
        self.assertIn("Nothing was downloaded", errs[0])

    def test_new_engine_passes_the_gate(self):
        """The same job on 0.4.3 reaches terrain and the engine."""
        _errs, _oks, prep, core, _export = self._run(n_jobs=1, engine_version="0.4.3")
        self.assertEqual(prep.call_count, 1)
        self.assertEqual(core.call_count, 1)


if __name__ == "__main__":
    unittest.main()
