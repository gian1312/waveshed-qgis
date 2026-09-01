"""Unit tests for tools/torture_runner.py.

The runner needs PyQGIS, a project and a network to do its job, so the parts
that decide PASS from FAIL are kept as plain functions and tested here instead.
That matters more than usual for this file: its whole purpose is to stop the
torture set reporting green when it is not, and a harness nobody checks is
exactly how it got there.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO))
from waveshed.core import abt as _abt  # noqa: E402

# Loaded by path: tools/ is not a package, and adding an __init__.py to it just
# to be importable would put it on the plugin's namespace.
_spec = importlib.util.spec_from_file_location(
    "torture_runner", REPO / "tools" / "torture_runner.py")
tr = importlib.util.module_from_spec(_spec)
sys.modules["torture_runner"] = tr
_spec.loader.exec_module(tr)


# ---------------------------------------------------------------------------
# Image format and elevation decoding — the checks that separate "the bytes
# arrived" from "the terrain is real"
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# URI parsing
# ---------------------------------------------------------------------------

XYZ = ("type=xyz&url=https://example.org/terrarium/%7Bz%7D/%7Bx%7D/%7By%7D.png"
       "&zmax=15&zmin=0&interpretation=terrariumterrain")


def test_xyz_template_is_unquoted():
    assert tr.xyz_template(XYZ) == "https://example.org/terrarium/{z}/{x}/{y}.png"


def test_xyz_template_is_empty_for_a_non_xyz_source():
    assert tr.xyz_template("/some/file.tif") == ""
    assert tr.xyz_template("crs=EPSG:25832&identifier=nw_dgm&url=https://wcs") == ""


def test_int_param_falls_back_instead_of_raising():
    params = tr.uri_params(XYZ)
    assert tr.int_param(params, "zmax", 99) == 15
    assert tr.int_param(params, "missing", 99) == 99
    assert tr.int_param({"zmax": ""}, "zmax", 99) == 99
    assert tr.int_param({"zmax": "deep"}, "zmax", 99) == 99


# ---------------------------------------------------------------------------
# Run matrix
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("spec,expected", [
    ("30", [30]),
    ("10 / 30", [10, 30]),
    ("2 (+30)", [2, 30]),
    ("5 + 90", [5, 90]),
    (2, [2]),
    ([90, 30], [30, 90]),
    ("", [30]),
    (None, [30]),
])
def test_parse_resolutions_reads_what_the_catalogue_writes_for_humans(spec, expected):
    assert tr.parse_resolutions(spec) == expected


def test_matrix_case_carries_the_direction_and_the_resolutions():
    case = tr.matrix_case({"bbox": [-10.0, 35.0, 30.0, 60.0], "res_m": "2",
                           "must_fail": True, "stresses": "the 2 M-tile cap"})
    assert case["bbox"] == [-10.0, 35.0, 30.0, 60.0]
    assert case["resolutions"] == [2]
    assert case["must_fail"] is True


def test_matrix_case_accepts_an_older_bare_bbox_manifest():
    case = tr.matrix_case([7.35, 46.9, 7.47, 46.99])
    assert case["bbox"] == [7.35, 46.9, 7.47, 46.99]
    assert case["resolutions"] == [30]
    assert case["must_fail"] is False
    assert tr.matrix_bbox([7.35, 46.9, 7.47, 46.99]) == [7.35, 46.9, 7.47, 46.99]


# ---------------------------------------------------------------------------
# --rows selection
# ---------------------------------------------------------------------------

def test_row_selected_stops_at_a_digit_boundary():
    assert tr.row_selected("2.2", ["2.2"])
    assert tr.row_selected("2.2a", ["2.2"])
    # The bug this rule exists for: --rows 2.2 used to pull in 2.20..2.23.
    assert not tr.row_selected("2.20", ["2.2"])
    assert tr.row_selected("2.20", ["2."])
    assert tr.row_selected("1.3b", ["1."])
    assert not tr.row_selected("4.3", ["1."])


def test_row_selected_with_no_filter_selects_everything():
    assert tr.row_selected("anything", [])


# ---------------------------------------------------------------------------
# Layer URI comparison — the check that catches a stale or hand-edited project
# ---------------------------------------------------------------------------

OUT = Path("/project/data/torture")


def test_source_matches_ignores_the_respelling_qgis_does():
    expected = ("type=xyz&url=https://tile.openstreetmap.org/{z}/{x}/{y}.png"
                "&zmax=19&zmin=0")
    # QGIS sorts the parameters, adds crs= and an empty format=, and
    # percent-encodes the placeholders. None of that changes the layer.
    actual = ("crs=EPSG:3857&format&type=xyz"
              "&url=https://tile.openstreetmap.org/%7Bz%7D/%7Bx%7D/%7By%7D.png"
              "&zmax=19&zmin=0")
    ok, why = tr.source_matches(expected, actual, OUT)
    assert ok, why


def test_source_matches_catches_a_stripped_parameter():
    expected = ("cache=PreferNetwork&crs=EPSG:25832&format=GTiff"
                "&identifier=nw_dgm&url=https://www.wcs.nrw.de/geobasis/wcs_nw_dgm")
    stripped = ("cache=PreferNetwork&crs=EPSG:25832&format=GTiff"
                "&url=https://www.wcs.nrw.de/geobasis/wcs_nw_dgm")
    ok, why = tr.source_matches(expected, stripped, OUT)
    assert not ok
    assert "identifier" in why


def test_source_matches_catches_an_edited_value():
    expected = "type=xyz&url=https://example.org/{z}.png&zmax=15"
    edited = "type=xyz&url=https://example.org/{z}.png&zmax=18"
    ok, why = tr.source_matches(expected, edited, OUT)
    assert not ok
    assert "zmax" in why


def test_source_matches_catches_a_removed_interpretation():
    expected = "type=xyz&url=https://example.org/{z}.png&interpretation=terrariumterrain"
    ok, why = tr.source_matches(expected, "type=xyz&url=https://example.org/{z}.png", OUT)
    assert not ok
    assert "interpretation" in why


def test_source_matches_resolves_relative_file_paths():
    ok, why = tr.source_matches("./dem/base/base_wgs84.tif",
                                "/project/data/torture/dem/base/base_wgs84.tif", OUT)
    assert ok, why


def test_source_matches_keeps_the_vsi_prefix_and_the_sublayer_suffix():
    ok, _ = tr.source_matches(
        "/vsizip/./dem/zip/dem_in_zip.zip/base_wgs84.tif",
        "/vsizip//project/data/torture/dem/zip/dem_in_zip.zip/base_wgs84.tif", OUT)
    assert ok
    ok, _ = tr.source_matches("./buildings/multi.gpkg|layername=buildings",
                              "/project/data/torture/buildings/multi.gpkg|layername=buildings",
                              OUT)
    assert ok
    # A different sublayer of the same file is a different layer.
    ok, why = tr.source_matches("./buildings/multi.gpkg|layername=buildings",
                                "/project/data/torture/buildings/multi.gpkg|layername=other_layer",
                                OUT)
    assert not ok


def test_source_matches_leaves_remote_paths_alone():
    url = "/vsicurl/https://example.org/dem.tif"
    ok, _ = tr.source_matches(url, url, OUT)
    assert ok


def test_source_matches_accepts_an_empty_expectation():
    ok, _ = tr.source_matches("", "anything at all", OUT)
    assert ok


# ---------------------------------------------------------------------------
# known_fail scoping
# ---------------------------------------------------------------------------

def test_known_fail_only_covers_the_checks_it_names():
    row = {"known_fail": "the encoding token is unreadable",
           "known_fail_checks": ["encoding", "tiles.elevation"]}
    assert tr.known_fail_for(row, "encoding")
    assert tr.known_fail_for(row, "tiles.elevation")
    # The bug this fixes: an unscoped note also swallowed "the layer does not
    # load", which has nothing to do with the finding.
    assert tr.known_fail_for(row, "layer.valid") == ""
    assert tr.known_fail_for(row, "tiles.transport") == ""


def test_a_row_without_a_note_is_never_excused():
    assert tr.known_fail_for({"known_fail": "", "known_fail_checks": []}, "encoding") == ""
    assert tr.known_fail_for({}, "encoding") == ""


def test_a_legacy_unscoped_note_covers_verdict_checks_only():
    row = {"known_fail": "an old manifest with no scope"}
    assert tr.known_fail_for(row, "encoding")
    assert tr.known_fail_for(row, "classify")
    assert tr.known_fail_for(row, "layer.valid") == ""
    assert tr.known_fail_for(row, "render") == ""


# ---------------------------------------------------------------------------
# Results: the accounting that stops a hole reading as a pass
# ---------------------------------------------------------------------------

def test_a_documented_failure_is_reported_but_does_not_fail_the_run():
    results = tr.Results()
    assert results.add("1.10", "encoding", "encoding mapbox", tr.FAIL,
                       "got terrarium", "known token gap") == tr.XFAIL
    assert results.count(tr.FAIL) == 0
    assert results.count(tr.XFAIL) == 1
    # The measurement survives alongside the note: that is how anyone notices
    # the finding changed shape rather than disappeared.
    assert "got terrarium" in results.entries[0]["detail"]


def test_a_documented_failure_that_starts_passing_fails_the_run():
    results = tr.Results()
    assert results.add("1.10", "encoding", "encoding mapbox", tr.PASS,
                       "", "known token gap") == tr.XPASS
    assert results.count(tr.XPASS) == 1
    assert "stale" in results.entries[0]["detail"]


def test_an_undocumented_failure_stays_a_failure():
    results = tr.Results()
    assert results.add("1.1", "encoding", "encoding terrarium", tr.FAIL, "got mapbox") == tr.FAIL
    assert results.count(tr.FAIL) == 1


def test_a_declared_check_that_never_ran_is_reported_not_omitted():
    results = tr.Results()
    results.plan("2.1", "layer.valid")
    results.plan("2.1", "ingest")
    results.add("2.1", "layer.valid", "layer valid", tr.PASS)
    results.finish()
    assert results.count(tr.NOTRUN) == 1
    missing = [e for e in results.entries if e["status"] == tr.NOTRUN][0]
    assert missing["id"] == "ingest"


def test_unplan_withdraws_a_check_that_turned_out_not_to_apply():
    results = tr.Results()
    results.plan("1.6", "tiles.elevation")
    results.unplan("1.6", "tiles.elevation")
    results.finish()
    assert results.count(tr.NOTRUN) == 0


def test_planning_the_same_check_twice_declares_it_once():
    results = tr.Results()
    results.plan("2.1", "layer.valid")
    results.plan("2.1", "layer.valid")
    assert len(results.planned) == 1


def test_notes_are_not_counted_as_passes():
    results = tr.Results()
    results.note("render (advisory, tile layer): 41 colours")
    assert results.count(tr.PASS) == 0
    assert results.notes == ["render (advisory, tile layer): 41 colours"]


# ---------------------------------------------------------------------------
# The checks a row declares
# ---------------------------------------------------------------------------

def _row(**kwargs):
    base = {"row": "x.y", "name": "n", "source": "", "crs": "", "kind": "raster",
            "check": "both", "expect_renders": True}
    base.update(kwargs)
    return base


def test_a_reject_row_declares_the_refusal_check():
    results = tr.Results()
    tr._plan_row_checks(_row(row="4.4b", check="reject", expect_class="imagery"), results)
    declared = {check for row, check in results.planned}
    # The row that existed to catch a live classifier bug shipped with a blank
    # expect_class, so the runner skipped it entirely. Both halves are declared
    # now: what the classifier says, and whether the plugin refuses the layer.
    assert "classify" in declared
    assert "classify.reject" in declared


def test_an_xyz_row_does_not_get_a_render_verdict():
    results = tr.Results()
    tr._plan_row_checks(_row(row="1.1", source=XYZ, expect_class="dem"), results)
    declared = {check for row, check in results.planned}
    # A render cannot test a tile layer, and whether its tiles become real
    # terrain is tier C's question — asked of the real downloader and the real
    # .abt, not of a second decoder living in the harness.
    assert "render" not in declared
    assert {"layer.valid", "classify"} <= declared


def test_a_file_row_declares_a_render_check():
    results = tr.Results()
    tr._plan_row_checks(_row(row="2.1", source="./dem/base/base_wgs84.tif"), results)
    declared = {check for row, check in results.planned}
    assert "render" in declared
    assert "tiles.transport" not in declared


def test_a_vector_row_declares_a_feature_count():
    results = tr.Results()
    tr._plan_row_checks(_row(row="5.1", kind="vector", source="./vectors/x.geojson"), results)
    assert ("5.1", "features") in results.planned


# ---------------------------------------------------------------------------
# .abt output inspection — "verify the output, not the absence of errors"
# ---------------------------------------------------------------------------

class _FakeAbt:
    """Only the two constants ``_real_mask`` reads.

    Taken from the plugin itself rather than copied: a hand-written floor here
    is exactly how this double would keep reporting green after the real one
    changed underneath it.
    """
    MIN_VALID_ELEV_M = _abt.MIN_VALID_ELEV_M
    ELEV_STEP_M = _abt.ELEV_STEP_M


def test_the_plugins_floor_rejects_the_engines_void():
    """``_check_void_sentinel``'s question, as a unit test.

    A floor BELOW the sentinel is the defect: -9999 counts then reads as valid
    ground at -4999.5 m, and every uncovered pixel is pasted into a mosaic and
    into the min-altitude surface as terrain instead of no-data.
    """
    assert tr.CONVERTER_VOID_COUNTS * _abt.ELEV_STEP_M <= _abt.MIN_VALID_ELEV_M


def test_the_converters_void_is_not_terrain():
    import numpy as np
    plugin = {"abt": _FakeAbt}
    # -9999 counts is what aether_converter writes for a pixel no source
    # covered, and the plugin's floor is the converter's own -5000-COUNT one,
    # so the plugin's rule alone already rejects it.
    grid = np.array([[tr.CONVERTER_VOID_COUNTS, 1120, -20000]], dtype="int16")
    assert (grid > _FakeAbt.MIN_VALID_ELEV_M / _FakeAbt.ELEV_STEP_M).tolist() == \
        [[False, True, False]]
    assert tr._real_mask(grid, plugin).tolist() == [[False, True, False]]


def test_real_mask_keeps_ordinary_negative_ground():
    import numpy as np
    plugin = {"abt": _FakeAbt}
    # The Dead Sea shore is -430 m; that is terrain, not a hole.
    grid = np.array([[-860, 0, 1120]], dtype="int16")
    assert tr._real_mask(grid, plugin).all()


# ---------------------------------------------------------------------------
# A must-fail ingest has to fail for the right reason
# ---------------------------------------------------------------------------

def test_an_engine_without_the_subcommand_is_not_a_refusal():
    ok, why = tr._refusal_is_the_right_one(
        {}, "error: unrecognized subcommand 'ingest'", "/f/zstd_cog.tif", 2)
    assert not ok
    assert "too old" in why


def test_a_silent_non_zero_exit_is_not_failing_loudly():
    ok, why = tr._refusal_is_the_right_one({}, "", "/f/zstd_cog.tif", 1)
    assert not ok
    assert "not one word" in why


def test_a_refusal_that_does_not_name_the_source_is_not_enough():
    ok, why = tr._refusal_is_the_right_one({}, "error: something went wrong",
                                           "/f/zstd_cog.tif", 1)
    assert not ok
    assert "without naming" in why


def test_a_refusal_naming_the_file_passes():
    ok, why = tr._refusal_is_the_right_one(
        {}, "error: cannot read /f/zstd_cog.tif: unsupported compression", 
        "/f/zstd_cog.tif", 1)
    assert ok, why


def test_expect_failure_pins_the_wording_when_the_catalogue_knows_it():
    row = {"expect_failure": "Projection not found"}
    ok, _ = tr._refusal_is_the_right_one(row, "error: Projection not found (EPSG:5514)",
                                         "/f/krovak.tif", 1)
    assert ok
    ok, why = tr._refusal_is_the_right_one(row, "error: krovak.tif is corrupt",
                                           "/f/krovak.tif", 1)
    assert not ok
    assert "without naming" in why


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------

def _manifest():
    return {"build": "test build", "project": "p.qgs", "rows": [],
            "skipped_rows": [{"what": "row 3.1", "why": "needs a manual download"}]}


def test_the_report_names_every_status_and_the_rows_that_were_never_built(tmp_path):
    results = tr.Results()
    results.add("1.1", "encoding", "encoding terrarium", tr.PASS)
    results.add("2.1", "render", "draws something", tr.FAIL, "blank")
    results.add("1.10", "encoding", "encoding mapbox", tr.FAIL, "got terrarium", "known gap")
    results.add("4.4b", "classify", "classified as imagery", tr.PASS, "", "known gap")
    results.plan("2.2", "ingest")
    results.finish()

    payload = tr.write_report(results, _manifest(), tmp_path, "ab")
    assert payload["passed"] == 1
    assert payload["failed"] == 1
    assert payload["known_failures"] == 1
    assert payload["unexpected_passes"] == 1
    assert payload["not_run"] == 1

    markdown = (tmp_path / "results.md").read_text(encoding="utf-8")
    assert "Result: **FAILED**" in markdown
    for heading in ("## Failures", "## Unexpected passes", "## Not run",
                    "## Known findings", "## Rows the generator could not build"):
        assert heading in markdown
    assert "needs a manual download" in markdown


def test_a_clean_but_partial_run_is_a_failed_run(tmp_path):
    # The old contract reported this as INCOMPLETE with its own exit code and
    # an --allow-incomplete escape hatch. The rule now: a check that does not
    # run is a failed run, with no flag that changes it.
    results = tr.Results()
    results.add("1.1", "encoding", "encoding terrarium", tr.PASS)
    results.add("-", "engine.present", "engine binaries", tr.SKIP, "not installed")
    payload = tr.write_report(results, {"build": "b", "project": "p.qgs", "rows": []},
                              tmp_path, "ab")
    assert payload["failed"] == 0
    assert payload["skipped"] == 1
    markdown = (tmp_path / "results.md").read_text(encoding="utf-8")
    assert "Result: **FAILED**" in markdown
    assert "did not run" in markdown


def test_a_complete_clean_run_says_so(tmp_path):
    results = tr.Results()
    results.plan("1.1", "encoding")
    results.add("1.1", "encoding", "encoding terrarium", tr.PASS)
    results.finish()
    payload = tr.write_report(results, {"build": "b", "project": "p.qgs", "rows": []},
                              tmp_path, "abc")
    assert payload["not_run"] == 0
    markdown = (tmp_path / "results.md").read_text(encoding="utf-8")
    assert "Result: **PASSED**" in markdown


# ---------------------------------------------------------------------------
# An unscoped legacy note must not manufacture a run failure
# ---------------------------------------------------------------------------

def test_a_legacy_note_excuses_a_failure_but_never_flags_a_pass():
    row = {"known_fail": "an old manifest with no scope"}
    results = tr.Results()
    # The note was never declared to be about `classify`; the scope was guessed.
    assert results.add("1.10", "classify", "classified as dem", tr.PASS, "",
                       tr.known_fail_for(row, "classify")) == tr.PASS
    assert results.add("1.10", "encoding", "encoding mapbox", tr.FAIL, "got terrarium",
                       tr.known_fail_for(row, "encoding")) == tr.XFAIL
    assert results.count(tr.XPASS) == 0


def test_a_scoped_note_that_starts_passing_still_fails_the_run():
    row = {"known_fail": "the token gap", "known_fail_checks": ["encoding"]}
    results = tr.Results()
    assert results.add("1.10", "encoding", "encoding mapbox", tr.PASS, "",
                       tr.known_fail_for(row, "encoding")) == tr.XPASS


# ---------------------------------------------------------------------------
# main(): a run that checks nothing is never a pass
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tier", ["z", "", "ab c", "d"])
def test_an_unrecognised_tier_is_refused(tier, capsys):
    # An unrecognised tier used to run zero checks and exit 0 with
    # "every check the catalogue declares ran and passed". Uppercase is
    # accepted rather than refused ("--tier A" is obviously tier A), which was
    # the other half of the same bug.
    assert tr.main(["--tier", tier]) == 2
    assert "--tier must be" in capsys.readouterr().out


def test_normalise_file_source_treats_a_windows_path_as_absolute():
    out = Path("/abs/out")
    assert tr.normalise_file_source(r"C:\data\x.tif", out) == "C:/data/x.tif"
    assert tr.normalise_file_source(r"/vsizip/C:\d\a.zip/i.tif", out) == "/vsizip/C:/d/a.zip/i.tif"


def test_source_matches_handles_a_uri_with_no_url_parameter():
    out = Path("/abs/out")
    ok, _ = tr.source_matches("type=mbtiles&path=./t.mbtiles",
                              "type=mbtiles&path=./t.mbtiles&crs=EPSG:3857", out)
    assert ok
    ok, why = tr.source_matches("type=mbtiles&path=./t.mbtiles", "type=mbtiles", out)
    assert not ok
    assert "path" in why


def test_parse_resolutions_refuses_a_resolution_this_system_has_no_tile_grid_for():
    # r"\d+" split "0.5" into [0, 5]; a 0 m resolution divides by zero inside
    # the plugin's tile geometry.
    assert tr.parse_resolutions("0.5") == []
    assert tr.parse_resolutions("2.0") == [2]


# ---------------------------------------------------------------------------
# Tile size — the plugin's resolution maths assumes 256 px
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# The end-of-run summary
# ---------------------------------------------------------------------------

def test_the_summary_repeats_every_finding_at_the_end(tmp_path, capsys):
    results = tr.Results()
    results.add("1.1", "encoding", "encoding terrarium", tr.PASS)
    results.add("1.3a", "zmax.service_limit", "the plugin knows this service stops at z12",
                tr.FAIL, "no max_zoom for this service")
    results.add("1.10", "encoding", "encoding mapbox", tr.FAIL, "got terrarium", "known gap")
    results.add("-", "engine.present", "engine binaries", tr.SKIP, "not installed")
    results.plan("2.2", "ingest")
    results.finish()
    payload = tr.write_report(results, _manifest(), tmp_path, "ab")
    capsys.readouterr()                       # discard the per-check lines
    tr.print_summary(results, payload, tmp_path)
    out = capsys.readouterr().out
    for heading in ("FAILED (1)", "KNOWN FINDINGS (1)", "NOT RUN (1)", "SKIPPED (1)",
                    "NOT BUILT (1)"):
        assert heading in out, heading
    assert "no max_zoom for this service" in out
    # The legend has to say what "known findings" means where it is read.
    # Compared with the wrapping collapsed: the block is wrapped to the terminal.
    flat = " ".join(out.split())
    assert "they do NOT fail the run" in flat
    assert "1 failed" in flat and "1 known findings" in flat


# ---------------------------------------------------------------------------
# Comparing two terrain directories — "not the same" must mean MEASURED
# ---------------------------------------------------------------------------

class _TwoDirAbt:
    """An .abt module whose tiles come from a ``{dir: grid}`` table."""

    ELEV_STEP_M = _abt.ELEV_STEP_M
    MIN_VALID_ELEV_M = _abt.MIN_VALID_ELEV_M
    grids: dict = {}

    @classmethod
    def list_tiles(cls, d):
        return [f"{d}/t.abt"] if cls.grids.get(d) is not None else []

    @staticmethod
    def read_header(path):
        return path

    @classmethod
    def read_tile(cls, header):
        return cls.grids[header.rsplit("/", 1)[0]]


def _agree_over(bare, built):
    import numpy as np
    _TwoDirAbt.grids = {"/x/bare": None if bare is None else np.array(bare, dtype="int16"),
                        "/x/built": None if built is None else np.array(built, dtype="int16")}
    return tr._agree("/x/bare", "/x/built", {"abt": _TwoDirAbt})


def test_agree_separates_an_unmeasurable_pair_from_a_real_difference():
    """A comparison that could not be made is not a difference.

    The buildings rows read "not the same" as proof that the burn happened, so
    a two-valued answer let a disjoint or unreadable pair report a successful
    burn without a single sample being compared.
    """
    # 40 counts = 20 m of building, well past the 0.5 m agreement tolerance.
    assert _agree_over([[1120, 1120]], [[1160, 1120]])[0] == tr.DIFFER
    assert _agree_over([[1120, 1120]], [[1120, 1120]])[0] == tr.SAME
    # One side wrote no tile at all: neither same nor different.
    verdict, why = _agree_over([[1120, 1120]], None)
    assert verdict == tr.INCOMPARABLE, why
    # Both sides are nothing but the converter's void sentinel.
    void = tr.CONVERTER_VOID_COUNTS
    assert _agree_over([[void, void]], [[void, void]])[0] == tr.INCOMPARABLE


# ---------------------------------------------------------------------------
# Coverage-output assertions — the checks that used to be "≥ 1 valid pixel"
# ---------------------------------------------------------------------------

import contextlib


@contextlib.contextmanager
def _real_osgeo():
    """Swap conftest's ``osgeo`` stub for the real package, then swap back.

    Same pattern as tests/test_buildings_source.py — the coverage checks
    read real GeoTIFFs, and a stubbed GDAL would test the stub.
    """
    stubs = {name: mod for name, mod in sys.modules.items()
             if name == "osgeo" or name.startswith("osgeo.")}
    for name in stubs:
        sys.modules.pop(name, None)
    try:
        yield
    finally:
        for name in [n for n in list(sys.modules)
                     if n == "osgeo" or n.startswith("osgeo.")]:
            sys.modules.pop(name, None)
        sys.modules.update(stubs)


class _Params:
    """The four fields _check_coverage reads off CoverageParams."""
    tx_lat, tx_lon = 46.945, 7.41
    max_range_km, resolution_m = 3, 30


def _write_cov_tif(path, values, nodata=-9999.0, lat=46.945, lon=7.41,
                   res_m=30.0):
    import numpy as np
    from osgeo import gdal
    gdal.UseExceptions()
    arr = np.asarray(values, dtype="float32")
    h, w = arr.shape
    px = res_m / 111_111.0
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(str(path), w, h, 1, gdal.GDT_Float32)
    ds.SetGeoTransform([lon - w / 2.0 * px, px, 0.0, lat + h / 2.0 * px, 0.0, -px])
    ds.SetProjection('GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",'
                     '6378137,298.257223563]],PRIMEM["Greenwich",0],'
                     'UNIT["degree",0.0174532925199433],AUTHORITY["EPSG","4326"]]')
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(nodata)
    band.WriteArray(arr)
    ds = None
    return path


def _disk(size=200, nodata=-9999.0, fill=80.0):
    import numpy as np
    arr = np.full((size, size), nodata, dtype="float32")
    yy, xx = np.mgrid[0:size, 0:size]
    r = size / 2.0 - 1
    arr[((yy - size / 2.0) ** 2 + (xx - size / 2.0) ** 2) <= r * r] = fill
    return arr


def _cov_statuses(tmp_path, values, row=None):
    with _real_osgeo():
        tif = _write_cov_tif(tmp_path / "c.tif", values)
        results = tr.Results()
        tr._check_coverage("t", Path(tif), _Params(), row or
                           {"expect_cov_valid_pct": [65.0, 85.0]}, results, {})
    return {e["id"].rsplit(".", 1)[1]: e["status"] for e in results.entries}


def test_a_full_disk_coverage_passes_all_three_structure_checks(tmp_path):
    st = _cov_statuses(tmp_path, _disk())
    assert st == {"grid": tr.PASS, "disk": tr.PASS, "stripes": tr.PASS}


def test_a_striped_coverage_fails_the_stripe_check(tmp_path):
    # THE 1.3a-class defect: alternating all-nodata rows inside the disk.
    # The old suite asserted "at least one valid pixel" and called it done.
    import numpy as np
    arr = _disk()
    arr[10:190:2, :] = -9999.0
    st = _cov_statuses(tmp_path, arr)
    assert st["stripes"] == tr.FAIL
    assert st["disk"] == tr.FAIL     # half the disk is gone too


def test_a_clipped_half_disk_fails_the_disk_band(tmp_path):
    arr = _disk()
    arr[:, 100:] = -9999.0
    st = _cov_statuses(tmp_path, arr)
    assert st["disk"] == tr.FAIL


def test_a_row_with_no_declared_disk_band_fails_rather_than_skips(tmp_path):
    st = _cov_statuses(tmp_path, _disk(), row={"expect_cov_valid_pct": []})
    assert st["disk"] == tr.FAIL


def test_a_missing_coverage_fails_all_three_by_name(tmp_path):
    results = tr.Results()
    tr._check_coverage("t", None, _Params(), {"expect_cov_valid_pct": [65, 85]},
                       results, {})
    assert [e["status"] for e in results.entries] == [tr.FAIL] * 3


def test_a_wrong_size_grid_fails_the_grid_check(tmp_path):
    st = _cov_statuses(tmp_path, _disk(size=120))     # 120 px vs ~200 expected
    assert st["grid"] == tr.FAIL


def test_an_off_centre_export_fails_the_grid_check(tmp_path):
    with _real_osgeo():
        tif = _write_cov_tif(tmp_path / "c.tif", _disk(), lat=47.4, lon=8.5)
        results = tr.Results()
        tr._check_coverage("t", Path(tif), _Params(),
                           {"expect_cov_valid_pct": [65, 85]}, results, {})
    st = {e["id"].rsplit(".", 1)[1]: e["status"] for e in results.entries}
    assert st["grid"] == tr.FAIL


def test_the_wedge_check_confines_and_detects_spill(tmp_path):
    import numpy as np
    size = 200
    arr = np.full((size, size), -9999.0, dtype="float32")
    yy, xx = np.mgrid[0:size, 0:size]
    de, dn = xx - size / 2.0, size / 2.0 - yy
    az = (np.degrees(np.arctan2(de, dn))) % 360.0
    r2 = (de ** 2 + dn ** 2)
    inside = (r2 <= (size / 2 - 1) ** 2) & (az >= 45) & (az <= 135)
    arr[inside] = 70.0
    with _real_osgeo():
        tif = _write_cov_tif(tmp_path / "w.tif", arr)
        ok, _why = tr._wedge_confined(str(tif), 46.945, 7.41, 40.0, 140.0)
        assert ok
        ok, why = tr._wedge_confined(str(tif), 46.945, 7.41, 200.0, 300.0)
    assert not ok and "outside" in why


# ---------------------------------------------------------------------------
# Cross-tab contract and stack priority — measured, not assumed
# ---------------------------------------------------------------------------

def _grids_env(table):
    import numpy as np
    _TwoDirAbt.grids = {d: (None if g is None else np.array(g, dtype="int16"))
                        for d, g in table.items()}
    return {"abt": _TwoDirAbt}


def test_cross_tab_fails_when_site_analysis_holds_less_terrain():
    void = tr.CONVERTER_VOID_COUNTS
    plugin = _grids_env({"/x/mc": [[100, 200], [300, 400]],
                         "/x/sa": [[100, void], [300, 400]]})
    ok, why = tr._cross_tab_contract("/x/mc", "/x/sa", plugin)
    assert not ok and "LESS terrain" in why


def test_cross_tab_fails_on_a_value_disagreement():
    plugin = _grids_env({"/x/mc": [[100, 200]], "/x/sa": [[100, 240]]})
    ok, why = tr._cross_tab_contract("/x/mc", "/x/sa", plugin)
    assert not ok and "disagree" in why


def test_cross_tab_passes_when_fill_only_adds():
    void = tr.CONVERTER_VOID_COUNTS
    plugin = _grids_env({"/x/mc": [[100, void]], "/x/sa": [[100, 0]]})
    ok, why = tr._cross_tab_contract("/x/mc", "/x/sa", plugin)
    assert ok and "fill only adds" in why


def test_stack_follows_needs_both_halves_of_the_priority_contract():
    void = tr.CONVERTER_VOID_COUNTS
    plugin = _grids_env({
        "/x/top": [[100, void]], "/x/base": [[555, 300]],
        "/x/good": [[100, 300]], "/x/blend": [[100, 999]],
        "/x/wrong_order": [[555, 300]],
    })
    ok, why = tr._stack_follows(["/x/good/t.abt"], "/x/top", "/x/base", plugin)
    assert ok and "takes the base's" in why
    ok, why = tr._stack_follows(["/x/blend/t.abt"], "/x/top", "/x/base", plugin)
    assert not ok and "second source" in why
    ok, why = tr._stack_follows(["/x/wrong_order/t.abt"], "/x/top", "/x/base",
                                plugin)
    assert not ok and "highest-priority" in why


def test_stack_follows_refuses_a_contribution_it_never_measured():
    # The saboteur's scenario 5: with a full-coverage top, "takes the base's
    # 0 samples" proved nothing, and a converter that dropped the second
    # source entirely passed. A priority combo whose base cannot contribute
    # must FAIL — and the one deliberate top-covers-all case must say what
    # it is measuring.
    plugin = _grids_env({
        "/x/full_top": [[100, 200]], "/x/base": [[555, 300]],
        "/x/stack": [[100, 200]],
    })
    ok, why = tr._stack_follows(["/x/stack/t.abt"], "/x/full_top", "/x/base",
                                plugin)
    assert not ok and "never measured" in why
    ok, why = tr._stack_follows(["/x/stack/t.abt"], "/x/full_top", "/x/base",
                                plugin, require_base=False)
    assert ok and "priority half only" in why


def test_identical_terrain_sees_what_agree_cannot():
    # inert_top's contract is byte equality. _agree intersects real masks,
    # so a stack with VOIDed samples "agreed" with a pristine base.
    void = tr.CONVERTER_VOID_COUNTS
    plugin = _grids_env({"/x/a": [[100, 200]], "/x/hole": [[100, void]],
                         "/x/same": [[100, 200]]})
    ok, why = tr._identical_terrain("/x/a", "/x/hole", plugin)
    assert not ok and "differ" in why
    ok, why = tr._identical_terrain("/x/a", "/x/same", plugin)
    assert ok


# ---------------------------------------------------------------------------
# Plugin pairing — the run must know WHICH waveshed it is testing
# ---------------------------------------------------------------------------

def test_pairing_refuses_a_plugin_outside_the_repo():
    # 2026-08-26: the QGIS console's preloaded installed plugin won every
    # import, the runner tested last week's code, and the mismatch surfaced
    # as an AttributeError crash plus 283 NOTRUNs. One line instead.
    problem = tr._pairing_problem("/qgis/python/plugins/waveshed",
                                  Path("/somewhere/repo"), {})
    assert problem is not None
    assert "installed" in problem and "deploy.py" in problem


def test_pairing_refuses_a_plugin_missing_the_driven_surface():
    class _Empty:
        pass

    plugin = {key: _Empty() for key, _names in tr._REQUIRED_PLUGIN_SURFACE}
    problem = tr._pairing_problem(str(tr.REPO / "waveshed"), tr.REPO, plugin)
    assert problem is not None
    assert "different change sets" in problem


def test_pairing_accepts_the_repo_plugin_with_the_full_surface():
    from types import SimpleNamespace
    plugin = {key: SimpleNamespace(**{name: object() for name in names})
              for key, names in tr._REQUIRED_PLUGIN_SURFACE}
    assert tr._pairing_problem(str(tr.REPO / "waveshed"), tr.REPO, plugin) is None


def test_the_required_surface_matches_the_real_repo_plugin():
    # The list the pairing check enforces must itself stay true: every name
    # it demands must exist in THIS repo's modules, or the check would
    # refuse the very pairing it exists to accept.
    import importlib
    modules = {
        "mc": "waveshed.gui.map_converter_tab",
        "sat": "waveshed.gui.site_analysis_tab",
        "p2p": "waveshed.gui.p2p_tab",
        "adapter": "waveshed.core.terrain_adapter",
        "result_loader": "waveshed.core.result_loader",
    }
    for key, names in tr._REQUIRED_PLUGIN_SURFACE:
        module = importlib.import_module(modules[key])
        missing = [n for n in names if not hasattr(module, n)]
        assert not missing, f"{modules[key]} lacks {missing}"


def test_tier_c_refuses_only_a_borrowed_qgis(monkeypatch):
    # 2026-08-26: after tiers a/b the runner's OWN QgsApplication exists, and
    # a guard built on "does an instance exist" refused a standalone
    # --tier abc run. The distinguishing fact is ownership, not existence.
    monkeypatch.setattr(tr, "running_inside_qgis", lambda: True)
    assert tr.borrowed_qgis(None) is True          # console: refuse
    assert tr.borrowed_qgis(object()) is False     # our own app: run
    monkeypatch.setattr(tr, "running_inside_qgis", lambda: False)
    assert tr.borrowed_qgis(None) is False         # standalone, c-only: run


def test_route_check_fails_a_silent_fallback():
    """Terrain that exists is not terrain that came the intended way.

    1.3b was observed falling back to the per-tile QGIS render when the
    fixture server was down — and the run still went green. A fallback that
    still produced terrain is a failed test, not a passed one.
    """
    results = tr.Results()
    row = {"expect_acquire": "download"}
    results.plan("t", "pipeline.route")
    tr._check_route("t", row, ["download"], ["render"], results)
    entry = results.entries[-1]
    assert entry["status"] == tr.FAIL
    assert "Site Analysis acquired via render" in entry["detail"]
    assert "silent fallback" in entry["detail"]


def test_route_check_passes_when_both_tabs_take_the_declared_route():
    results = tr.Results()
    row = {"expect_acquire": "sources"}
    results.plan("t", "pipeline.route")
    tr._check_route("t", row, ["sources", "sources"], ["sources"], results)
    assert results.entries[-1]["status"] == tr.PASS


def test_route_check_fails_when_nothing_was_recorded():
    # An empty record is not a pass: either the plugin surface moved or the
    # run acquired nothing — both are findings, never a shrug.
    results = tr.Results()
    row = {"expect_acquire": "download"}
    results.plan("t", "pipeline.route")
    tr._check_route("t", row, [], ["download"], results)
    entry = results.entries[-1]
    assert entry["status"] == tr.FAIL
    assert "recorded no acquisition" in entry["detail"]


def test_route_check_withdraws_when_the_catalogue_declares_nothing():
    # Pre-regeneration manifests carry no expect_acquire; the check must
    # visibly withdraw (unplan), never linger as NOTRUN or silently pass.
    results = tr.Results()
    results.plan("t", "pipeline.route")
    tr._check_route("t", {}, ["download"], ["download"], results)
    assert not [e for e in results.entries if e["id"] == "pipeline.route"]
    assert ("t", "pipeline.route") not in results.planned


# ---------------------------------------------------------------------------
# expect_format: the bytes the service really serves
# ---------------------------------------------------------------------------

def test_sniff_image_format_reads_magic_bytes_not_names():
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 16
    webp = b"RIFF" + b"\x40\0\0\0" + b"WEBP" + b"VP8 " + b"\0" * 16
    jpeg = b"\xff\xd8\xff\xe0" + b"\0" * 16
    assert tr.sniff_image_format(png) == "png"
    assert tr.sniff_image_format(webp) == "webp"
    assert tr.sniff_image_format(jpeg) == "jpeg"
    # Row 1.4's whole point: a .png URL answering WebP must read as webp.
    assert tr.sniff_image_format(webp) != "png"
    assert tr.sniff_image_format(b"<html>nope</html>").startswith("unknown")
    assert tr.sniff_image_format(b"").startswith("unknown")


def test_a_row_with_expect_format_declares_the_format_check():
    results = tr.Results()
    tr._plan_row_checks({"row": "1.4", "name": "x", "source": "type=xyz&url=u",
                         "crs": "", "expect_format": "webp",
                         "expect_tile_px": 512}, results)
    assert ("1.4", "xyz.format") in results.planned
    assert ("1.4", "xyz.tile_px") in results.planned


# ---------------------------------------------------------------------------
# Over-the-box scenarios and byte-exact tiles are DECLARED, so a scenario
# that never executes is a NOTRUN, not a vanished check
# ---------------------------------------------------------------------------

def _both_row(**extra):
    row = {"row": "1.3b", "name": "x", "source": "type=xyz&url=u", "crs": "",
           "kind": "raster", "check": "both", "expect_acquire": "download"}
    row.update(extra)
    return row


def test_a_both_row_declares_byte_exact_tiles():
    results = tr.Results()
    tr._plan_pipeline_row(_both_row(), {}, results, {"mc": None})
    assert ("1.3b", "pipeline.tile_bytes") in results.planned


def test_overrun_scenarios_declare_all_five_checks():
    results = tr.Results()
    row = _both_row(overrun=[{"id": "tile-overrun"}, {"id": "fully-outside"}])
    tr._plan_pipeline_row(row, {}, results, {"mc": None})
    for sid in ("tile-overrun", "fully-outside"):
        for part in ("run", "warned", "sea", "mc_void", "cov"):
            assert ("1.3b", f"overrun:{sid}.{part}") in results.planned, \
                f"overrun:{sid}.{part} must be declared up front"


def test_a_links_row_declares_the_verdict_check():
    results = tr.Results()
    tr._plan_pipeline_row({"row": "5.3", "name": "links", "kind": "vector",
                           "check": "none"}, {}, results, {"mc": None})
    assert ("5.3", "pipeline.verdicts") in results.planned


# ---------------------------------------------------------------------------
# P2P verdicts: excess loss over free space, from the contract CSV
# ---------------------------------------------------------------------------

def test_excess_loss_reads_the_contract_csv(tmp_path):
    csv_path = tmp_path / "p2p_0.csv"
    csv_path.write_text("Source_ID,Target_ID,Signal_dBm,Path_Loss_dB\n"
                        "Site_0,WP_0,-70.00,130.00\n", encoding="utf-8")
    # Bern -> Thun is ~24 km; free space at 900 MHz there is ~119 dB, so a
    # 130 dB path carries ~+11 dB of terrain.
    excess, detail = tr._excess_loss_db(str(csv_path),
                                        (46.9481, 7.4474), (46.7580, 7.6280))
    assert excess is not None and 5.0 < excess < 20.0, detail
    assert "dB" in detail


def test_excess_loss_refuses_a_csv_without_the_frozen_column(tmp_path):
    csv_path = tmp_path / "p2p_0.csv"
    csv_path.write_text("a,b\n1,2\n", encoding="utf-8")
    excess, detail = tr._excess_loss_db(str(csv_path), (46.9, 7.4), (46.8, 7.6))
    assert excess is None
    assert "Path_Loss_dB" in detail
