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
    """Only the two constants _real_mask reads."""
    MIN_VALID_ELEV_M = -5000.0
    ELEV_STEP_M = 0.5


def test_the_converters_void_is_not_terrain():
    import numpy as np
    plugin = {"abt": _FakeAbt}
    # -9999 counts is what aether_converter writes for a pixel no source
    # covered. The plugin's own floor is -10000 counts, one BELOW it, so the
    # plugin's rule alone calls every hole valid ground at -4999.5 m.
    grid = np.array([[tr.CONVERTER_VOID_COUNTS, 1120, -20000]], dtype="int16")
    assert (grid > _FakeAbt.MIN_VALID_ELEV_M / _FakeAbt.ELEV_STEP_M).tolist() == \
        [[True, True, False]]
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


def test_a_clean_but_partial_run_is_reported_as_incomplete(tmp_path):
    results = tr.Results()
    results.add("1.1", "encoding", "encoding terrarium", tr.PASS)
    results.add("-", "engine.present", "engine binaries", tr.SKIP, "not installed")
    payload = tr.write_report(results, {"build": "b", "project": "p.qgs", "rows": []},
                              tmp_path, "ab")
    assert payload["failed"] == 0
    assert payload["skipped"] == 1
    markdown = (tmp_path / "results.md").read_text(encoding="utf-8")
    assert "Result: **INCOMPLETE**" in markdown


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
