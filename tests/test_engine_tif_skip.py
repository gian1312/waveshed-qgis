"""The plugin exports the GeoTIFF only when aether_core has not already done so."""

import os
import tempfile
import time
import unittest

import conftest  # noqa: F401 — installs QGIS/PyQt stubs

from waveshed.core import binary_manager as bm

DONE = ['[Core] Tile file complete: 11662 tiles written',
        '[Core] GeoTIFF export complete: "G:/out/site1.tif"']


class TestEngineExportedTif(unittest.TestCase):
    def _files(self, d, tif_after=True):
        bit = os.path.join(d, "site1.bit")
        tif = os.path.join(d, "site1.tif")
        with open(bit, "wb") as fh:
            fh.write(b"x" * 10)
        t0 = time.time() - 100
        os.utime(bit, (t0, t0))
        with open(tif, "wb") as fh:
            fh.write(b"y" * 10)
        t1 = t0 + (50 if tif_after else -50)
        os.utime(tif, (t1, t1))
        return bit, tif

    def test_engine_export_is_trusted_once(self):
        with tempfile.TemporaryDirectory() as d:
            bit, tif = self._files(d)
            self.assertTrue(bm.engine_exported_tif(DONE, tif, bit))

    def test_no_engine_line_means_export(self):
        with tempfile.TemporaryDirectory() as d:
            bit, tif = self._files(d)
            self.assertFalse(bm.engine_exported_tif(DONE[:1], tif, bit))
            self.assertFalse(bm.engine_exported_tif(
                ["[Core] GeoTIFF export failed with exit code: 1"], tif, bit))

    def test_a_stale_tif_from_an_earlier_run_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as d:
            bit, tif = self._files(d, tif_after=False)
            self.assertFalse(bm.engine_exported_tif(DONE, tif, bit))

    def test_a_missing_or_empty_tif_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as d:
            bit, tif = self._files(d)
            open(tif, "wb").close()
            self.assertFalse(bm.engine_exported_tif(DONE, tif, bit))
            os.remove(tif)
            self.assertFalse(bm.engine_exported_tif(DONE, tif, bit))


if __name__ == "__main__":
    unittest.main()
