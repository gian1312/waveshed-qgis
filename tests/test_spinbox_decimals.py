"""A spin box rounds a value to its CURRENT decimals (2 by default).

``spin.setValue(0.005)`` followed by ``spin.setDecimals(4)`` leaves 0.01:
that is how every ITM job of the 360° tab sent a ground conductivity of
0.01 S/m (wet ground) instead of the documented default 0.005 until
2026-11-14. This scan runs without Qt: for every ``x.setValue(<literal>)``
it looks for an ``x.setDecimals(n)`` within the next lines and fails when
the literal does not survive rounding to 2 decimals.
"""

import pathlib
import re
import unittest

PLUGIN = pathlib.Path(__file__).resolve().parents[1] / "waveshed"
_VALUE = re.compile(
    r"([\w\.]+)\.setValue\(\s*(-?[0-9]*\.?[0-9]+(?:e-?\d+)?)\s*\)")


def value_before_decimals(root: pathlib.Path = PLUGIN) -> list:
    hits = []
    for path in sorted(root.rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            m = _VALUE.search(line)
            if not m:
                continue
            widget, value = m.group(1), float(m.group(2))
            later = re.compile(re.escape(widget) + r"\.setDecimals\((\d+)\)")
            for following in lines[i + 1:i + 8]:
                if later.search(following):
                    if round(value, 2) != value:
                        hits.append(f"{path.relative_to(root.parent)}:{i + 1}: "
                                    f"{widget}.setValue({value}) before setDecimals")
                    break
    return hits


class TestSpinBoxDecimals(unittest.TestCase):
    def test_no_value_is_rounded_by_a_later_setdecimals(self):
        self.assertEqual(value_before_decimals(), [])

    def test_the_scan_catches_the_itm_conductivity_case(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            pkg = pathlib.Path(tmp) / "pkg"
            pkg.mkdir()
            (pkg / "w.py").write_text(
                "s.spin_sgm.setRange(0.0001, 10.0)\n"
                "s.spin_sgm.setValue(0.005)\n"
                "s.spin_sgm.setDecimals(4)\n", encoding="utf-8")
            self.assertEqual(len(value_before_decimals(pkg)), 1)


if __name__ == "__main__":
    unittest.main()
