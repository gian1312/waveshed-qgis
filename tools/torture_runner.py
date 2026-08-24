#!/usr/bin/env python3
"""Run the torture set automatically instead of clicking through it.

From a shell, with QGIS's Python:

    "C:\\OSGeo4W\\bin\\python-qgis.bat" tools/torture_runner.py
    python3 tools/torture_runner.py --rows 1.1,1.5,4.3
    python3 tools/torture_runner.py --tier a
    python3 tools/torture_runner.py --tier b --cases "Prague,One tile"

``--rows`` selects catalogue rows; ``--cases`` selects run-matrix cases, which
have names rather than row numbers. They are separate on purpose: one filter
doing both meant every ``--rows`` value silently deselected the whole tier-B
plan phase, and the run then reported that everything had passed.

Or pasted straight into the QGIS Python Console, which is the obvious thing to
try and therefore has to work:

    exec(open(r"<repo>/tools/torture_runner.py", encoding="utf-8").read())

In the console it borrows the running QGIS instead of starting one, ignores
QGIS's own command line, and uses the project you already have open if it is
this one. Output lands in the console; results.md is written either way.

WHERE THIS RUNS
---------------
It needs PyQGIS, so: QGIS's own Python. On Windows that is
``C:\\OSGeo4W\\bin\\python-qgis.bat``; on Linux any python that can
``import qgis.core``. Tier B additionally needs the Aether binaries
(``aether_core``, ``aether_converter``, ``aether_export``) — the same ones the
plugin uses, found the same way (``AETHER_BIN_DIR``, then ``PATH``). No display
is required: set ``QT_QPA_PLATFORM=offscreen`` and it runs headless, which is
also what makes it usable from CI.

WHAT IT CHECKS
--------------
Tier A — no engine needed, and this is most of the value:
  * every layer is present, loads, sits in the CRS the catalogue says, and
    carries the URI the catalogue wrote (a hand-edited or stale ``.qgs`` is
    otherwise indistinguishable from a correct one);
  * every layer actually DRAWS over ground where its row has data (a valid
    layer that renders nothing is the failure mode that keeps hiding here);
  * the plugin's own verdicts match the row's stated expectation —
    ``classify_raster_layer`` (dem vs imagery), ``dem_layer_warning`` (is this
    layer refused as terrain?), ``resolve_xyz_encoding`` (terrarium / mapbox /
    must-raise) and ``resolve_zmax`` (the z18 -> z15 clamp and its warning);
  * every coordinate transform the project needs works on THIS machine.

  Nothing in tier A decodes a tile, walks the slippy grid or recomputes a
  ground resolution. It used to, and a harness carrying its own copy of the
  logic under test can only ever agree with itself — those questions are tier
  C's, asked of the real downloader and the real .abt.

Tier C — the whole pipeline, and the reason this file exists:
  * every catalogue row is put THROUGH the plugin and the real engine — XYZ
    through the shared downloader, files and rendered services through the Map
    Converter's own router and job builder, buildings burned into the surface,
    sites as transmitters, links as P2P jobs — and what comes out the far end
    is measured: real .abt elevations, then a coverage, then an exported
    GeoTIFF;
  * both tabs build the same row and are compared, because the checklist's own
    rule is that a divergence between Site Analysis and the Map Converter is a
    bug by definition;
  * a row's terrain is compared against the catalogue's reference source over
    the same ground — wrong-but-plausible terrain makes a wrong-but-plausible
    coverage, and nothing else here can see it;
  * the Map Converter is also run over STACKS (priority order, mixed
    acquisition kinds, two resolutions at once, buildings, rerun-with-skip),
    which no single row can express;
  * every row gets its own cache root, torn down after it. A failed download
    leaves an all-zero tile in the pool and the next run reports a cache hit,
    so a pipeline test sharing one pool measures history, not the code.

Tier B — needs the engine binaries:
  * ``aether_converter plan`` agrees with the plugin's OWN tile enumeration for
    each run-matrix case. Not a re-implementation: ``_snap_bbox`` and
    ``_enumerate_tiles`` are imported from ``waveshed.gui.map_converter_tab``,
    so this runs the very cross-check the Map Converter runs before every
    conversion, at the resolutions each case names;
  * the run-matrix cases that must be REFUSED are asserted as refusals.
    Everything downstream of the tile grid belongs to tier C.

Everything is read from ``data/torture/manifest.json``, which the project
generator writes from the same catalogue that produces the layers and
CHECKLIST.md — so the automated run and the human checklist cannot disagree
about what a row is supposed to do.

HOW IT REPORTS
--------------
Six statuses, and the distinction between them is the point:

  PASS    the check ran and the expectation held
  FAIL    the check ran and the expectation did not hold
  XFAIL   a FAIL the manifest documents as an open finding, on the named check
  XPASS   a documented finding that no longer reproduces — the note is stale,
          and this FAILS the run, because the suite must go red when reality
          changes in either direction
  SKIP    the check does not apply, or its prerequisite is genuinely absent
  NOTRUN  the manifest declares this check and it did not execute — a hole in
          the run, counted and reported rather than silently omitted

Results go to ``data/torture/results.json`` and a Markdown summary, which names
the tiers and filters the run used — a filtered re-run overwrites the same two
files, and its report says PARTIAL rather than PASSED so nobody reads a slice
as the whole set. Exit code: 0 = everything the manifest declares ran and
passed; 1 = something FAILED (or XPASSed); 2 = the run was INCOMPLETE (skips,
not-runs, or a catalogue row this build could not produce) — pass
``--allow-incomplete`` to accept that as success. A suite that quietly drops
half its checks and still exits 0 is the failure mode this replaces, so a run
that made no checks at all is never a pass either.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# exec()'d from the QGIS Python Console there is no __file__, which used to
# kill this script on its first line with a bare NameError — i.e. "it does
# nothing". Everything below therefore works without knowing where this file is.
try:
    REPO: Optional[Path] = Path(__file__).resolve().parents[1]
except NameError:
    REPO = None

PASS, FAIL, SKIP, XFAIL, XPASS, NOTRUN = (
    "PASS", "FAIL", "SKIP", "XFAIL", "XPASS", "NOTRUN")

#: Statuses that make the run red.
BAD = (FAIL, XPASS)
#: Statuses that make the run incomplete rather than red.
INCOMPLETE = (SKIP, NOTRUN)

_MARK = {PASS: "ok    ", FAIL: "FAIL  ", SKIP: "skip  ",
         XFAIL: "xfail ", XPASS: "XPASS ", NOTRUN: "NOTRUN"}

def find_manifest(explicit: str = "") -> Optional[Path]:
    """The manifest, looked for in every place it can sensibly be.

    In the console the open project's own folder is the reliable one: this file
    may have been read from anywhere, and on Windows from another drive.
    """
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    if REPO is not None:
        candidates.append(REPO / "data" / "torture" / "manifest.json")
    try:
        from qgis.core import QgsProject
        opened = QgsProject.instance().fileName()
        if opened:
            candidates.append(Path(opened).parent / "manifest.json")
    except Exception:
        pass
    candidates.append(Path.cwd() / "manifest.json")
    candidates.append(Path.cwd() / "data" / "torture" / "manifest.json")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

def known_fail_for(row: Dict[str, Any], check_id: str) -> str:
    """The documented open finding covering *check_id* on *row*, or "".

    ``known_fail`` used to be one string that turned ANY failure on the row
    into an expected one — so a note about the encoding verdict also swallowed
    "the layer does not load". It is scoped now: ``known_fail_checks`` names
    the checks the note actually covers. A manifest that carries the old
    unscoped form is honoured for the checks it plausibly meant and nothing
    else (see ``_LEGACY_KNOWN_FAIL_CHECKS``) rather than for everything.
    """
    note = (row.get("known_fail") or "").strip()
    if not note:
        return ""
    scope = row.get("known_fail_checks")
    if scope:
        return note if check_id in scope else ""
    return LegacyNote(note) if check_id in _LEGACY_KNOWN_FAIL_CHECKS else ""


#: What an unscoped legacy ``known_fail`` is allowed to cover. Deliberately
#: narrow: these are verdict checks, the only kind the field was ever used for.
_LEGACY_KNOWN_FAIL_CHECKS = ("encoding", "classify", "classify.reject",
                             "tiles.elevation")


class LegacyNote(str):
    """A ``known_fail`` from a manifest that did not say which checks it covers.

    It still excuses a failure, but it must never turn a PASS into an XPASS:
    the scope was GUESSED, so "this check passes, therefore your note is
    stale" is a conclusion about a check the note may never have been about.
    Row 1.10's note is about the encoding token; applied unscoped it also
    lands on ``classify``, which passes — and manufactured a run-failing
    "stale note" out of nothing.
    """


class Results:
    """Every check that ran, with the row it belongs to.

    Also tracks the checks that were DECLARED and did not run. A suite whose
    denominator is "the checks I happened to make" cannot report a hole in
    itself; this one counts the holes.
    """

    def __init__(self) -> None:
        self.entries: List[Dict[str, str]] = []
        #: (row, check_id) the manifest declares. Filled in before the run.
        self.planned: List[Tuple[str, str]] = []
        self.executed: set = set()
        self.notes: List[str] = []

    # -- planning ----------------------------------------------------------
    def plan(self, row: str, check_id: str) -> None:
        if (row, check_id) not in self.planned:
            self.planned.append((row, check_id))

    def unplan(self, row: str, check_id: str) -> None:
        """Withdraw a planned check that turned out not to apply."""
        if (row, check_id) in self.planned:
            self.planned.remove((row, check_id))

    # -- recording ---------------------------------------------------------
    def add(self, row: str, check_id: str, label: str, status: str,
            detail: str = "", known_fail: str = "") -> str:
        """Record one check. Returns the status actually filed.

        A documented open finding (*known_fail*) turns FAIL into XFAIL — it is
        reported but does not fail the run, because the suite should go red
        when reality CHANGES, not when it matches what we already wrote down.
        The same note turns PASS into XPASS, which DOES fail the run: a finding
        that stopped reproducing is a stale note, and leaving it in place is
        how a suite drifts back into fiction.
        """
        if known_fail:
            if status == FAIL:
                # Keep what was measured. The note says why it is expected; the
                # measurement is how anyone notices the finding has changed
                # shape rather than disappeared.
                status = XFAIL
                detail = f"{detail} — known finding: {known_fail}" if detail else known_fail
            elif status == PASS and not isinstance(known_fail, LegacyNote):
                status, detail = XPASS, (
                    f"documented as broken, but it passed{' (' + detail + ')' if detail else ''} "
                    f"— the note is stale: {known_fail}")
        self.entries.append({"row": row, "id": check_id, "check": label,
                             "status": status, "detail": detail})
        self.executed.add((row, check_id))
        line = f"  {_MARK[status]} {row:6s} {label}"
        print(f"{line}{'  — ' + detail if detail and status != PASS else ''}", flush=True)
        return status

    def note(self, message: str) -> None:
        """Information that is not a check. Never counted as a pass."""
        self.notes.append(message)
        print(f"         {message}", flush=True)

    # -- accounting --------------------------------------------------------
    def finish(self) -> None:
        """File a NOTRUN for every declared check that never executed."""
        for row, check_id in self.planned:
            if (row, check_id) not in self.executed:
                self.entries.append({
                    "row": row, "id": check_id, "check": check_id,
                    "status": NOTRUN,
                    "detail": "declared by the manifest, never executed"})
                print(f"  {_MARK[NOTRUN]} {row:6s} {check_id}  — declared, never executed",
                      flush=True)

    def count(self, *statuses: str) -> int:
        return sum(1 for e in self.entries if e["status"] in statuses)


# ---------------------------------------------------------------------------
# QGIS bootstrap and the local fixture server
# ---------------------------------------------------------------------------

#: Written into out_dir by the generator; fetched over HTTP to prove that
#: whatever is listening on the fixture port is serving THIS directory.
SENTINEL = "manifest.json"


def serve_fixture(out_dir: Path, port: int = 8000):
    """Serve data/torture locally, as the project macro does on open.

    Rows 1.3b, 1.5 and 1.10 are fed by that server. Without it they render
    blank and the run reports three failures that say nothing about the code.
    """
    import functools
    import http.server
    import threading

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

    try:
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", port), functools.partial(Quiet, directory=str(out_dir)))
    except OSError:
        return None                      # already served (by QGIS, or by you)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def fixture_server_serves(out_dir: Path, base_url: str) -> Tuple[bool, str]:
    """Is *base_url*'s host really serving *out_dir*?

    "Port already in use, so somebody must be serving the fixture" was the old
    assumption, and it is wrong in the one case that matters: a stale server
    left over from another checkout answers 200 for every tile and the local
    rows then measure somebody else's bytes. Ask for a file only this build
    has, and compare it.
    """
    import urllib.error
    import urllib.request

    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return False, f"{base_url!r} is not an http(s) URL"
    probe = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/" + SENTINEL, "", ""))
    try:
        with urllib.request.urlopen(probe, timeout=10) as reply:
            served = reply.read()
    except Exception as exc:
        return False, f"{probe} — {exc}"
    try:
        local = (out_dir / SENTINEL).read_bytes()
    except OSError as exc:
        return False, f"cannot read {out_dir / SENTINEL}: {exc}"
    if served != local:
        return False, (f"{parts.netloc} answers, but its {SENTINEL} is not this build's "
                       f"({len(served)} bytes served vs {len(local)} local) — another "
                       f"server is on that port and the local rows would measure ITS tiles")
    return True, f"{parts.netloc} is serving {out_dir}"


def running_inside_qgis() -> bool:
    """True when this file is exec'd from the QGIS Python Console."""
    try:
        from qgis.core import QgsApplication
    except ImportError:
        return False
    return QgsApplication.instance() is not None


def start_qgis():
    """The QGIS application to use, or None when QGIS is already running.

    Pasting this file into the Python Console is the obvious thing to do, so it
    has to work there: inside QGIS there is already a QgsApplication and a
    second one would be wrong, so we borrow the running one and leave it alone.
    """
    if running_inside_qgis():
        return None
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from qgis.core import QgsApplication
    except ImportError:
        raise SystemExit(
            "[error] Could not import qgis.core. Run this with QGIS's own Python:\n"
            '        "C:\\\\OSGeo4W\\\\bin\\\\python-qgis.bat" tools/torture_runner.py\n'
            "        (or any python that can import qgis.core)")
    prefix = os.environ.get("QGIS_PREFIX_PATH", "/usr")
    QgsApplication.setPrefixPath(prefix, True)
    app = QgsApplication([], False)
    app.initQgis()
    return app


# ---------------------------------------------------------------------------
# Small pure helpers — unit-tested in tests/test_torture_runner.py
# ---------------------------------------------------------------------------

def uri_params(source: str) -> Dict[str, str]:
    """The ``k=v`` parameters of a QGIS layer URI.

    ``parse_qsl`` is what the plugin itself uses, so the runner sees exactly
    what the plugin sees — including its quirks (``+`` decodes to a space,
    blank values vanish). Reproducing them is deliberate: a check that parsed
    the URI "better" than the code under test would disagree with it for
    reasons that have nothing to do with the layer.
    """
    return dict(urllib.parse.parse_qsl(source))


def xyz_template(source: str) -> str:
    """The decoded ``url=`` tile template of an XYZ URI (``""`` if none)."""
    if "type=xyz" not in source:
        return ""
    return urllib.parse.unquote(uri_params(source).get("url", ""))


#: ``/vsizip/``, ``/vsicurl/`` and friends, possibly stacked.
_VSI = re.compile(r"^(?:/vsi[a-z0-9_]+/)+", re.I)
#: A Windows drive letter, which is an absolute path wherever it is read.
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def normalise_file_source(source: str, out_dir: Path) -> str:
    """A file-backed layer URI in a form both spellings of it agree on.

    The catalogue writes ``./dem/base/base_wgs84.tif`` so the project folder
    can be moved; QGIS stores the absolute path it resolved that to. Neither is
    wrong, so the comparison has to happen after resolving — while keeping the
    ``/vsi…/`` prefix and the ``|layername=`` suffix, which are part of the
    identity and not part of the path.
    """
    prefix = ""
    match = _VSI.match(source)
    if match:
        prefix, source = match.group(0), source[match.end():]
    tail = ""
    if "|" in source:
        source, _, rest = source.partition("|")
        tail = "|" + rest
    if "://" not in source:
        # A Windows drive letter is absolute wherever it is read, but POSIX
        # pathlib disagrees: it called "C:/data/x.tif" relative, glued out_dir
        # onto the front and then resolved the result against the CWD. A
        # Windows-generated project inspected from WSL or CI came out as a
        # garbage path — reported as a URI mismatch on every file row, and fed
        # to the engine as an ingest source that cannot exist. Foreign-flavoured
        # paths are normalised by separator only; nothing local can resolve them.
        if _DRIVE.match(source) or source.startswith("\\\\"):
            source = source.replace("\\", "/")
        else:
            path = Path(source)
            source = str(path if path.is_absolute() else (out_dir / source))
            try:
                source = str(Path(source).resolve())
            except OSError:
                pass
    return prefix + source.replace("\\", "/") + tail


def source_matches(expected: str, actual: str, out_dir: Path) -> Tuple[bool, str]:
    """Is *actual* the URI the catalogue described? ``(ok, what differs)``.

    Not a string compare: QGIS re-spells what it stores — it sorts the
    parameters, adds ``crs=`` and an empty ``format=``, percent-encodes the
    ``{z}`` placeholders and makes relative paths absolute. None of that
    changes the layer. What DOES change the layer is a parameter that went
    missing or came back different — a stripped WCS ``identifier``, an
    ``interpretation`` somebody removed by hand, a ``zmax`` edited in the GUI —
    and that is exactly what this reports. Extra parameters are allowed;
    missing or altered ones are not.
    """
    if not expected:
        return True, ""
    # A provider URI is "k=v&k=v"; a file source is a path, which may still
    # contain "=" in a "|layername=" suffix. Deciding on "url=" alone sent an
    # mbtiles/delimitedtext/postgres URI down the PATH branch, where out_dir
    # was prepended to the whole URI and the verdict was nonsense.
    looks_like_path = (expected.startswith(("/", "./", "../", "\\\\"))
                       or bool(_DRIVE.match(expected)) or bool(_VSI.match(expected)))
    if "=" in expected.partition("|")[0] and not looks_like_path:
        want = {k: urllib.parse.unquote(v) for k, v in urllib.parse.parse_qsl(expected)}
        got = {k: urllib.parse.unquote(v) for k, v in urllib.parse.parse_qsl(actual)}
        missing = sorted(k for k in want if k not in got)
        changed = sorted(f"{k}={got[k]!r}, catalogue says {want[k]!r}"
                         for k in want if k in got and got[k] != want[k])
        if not missing and not changed:
            return True, ""
        parts = []
        if missing:
            parts.append("missing " + ", ".join(missing))
        if changed:
            parts.append("; ".join(changed[:2]))
        return False, "; ".join(parts)
    want_path = normalise_file_source(expected, out_dir)
    got_path = normalise_file_source(actual, out_dir)
    if want_path == got_path:
        return True, ""
    return False, f"{_shorten(got_path)!r}, catalogue says {_shorten(want_path)!r}"


def int_param(params: Dict[str, str], key: str, default: int) -> int:
    """``params[key]`` as an int, *default* when absent or not a number."""
    raw = params.get(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def row_selected(row: str, only: Sequence[str]) -> bool:
    """Does *row* match one of the ``--rows`` filters?

    Prefixes, with a digit boundary: ``1.`` selects all of section 1, ``2.2``
    selects 2.2 and 2.2a but NOT 2.20. Plain ``startswith`` made ``--rows 2.2``
    quietly pull in 2.20 through 2.23, which is the sort of thing that makes a
    targeted re-run look like it reproduced something it never touched.
    """
    if not only:
        return True
    for pattern in only:
        if row == pattern:
            return True
        if not row.startswith(pattern):
            continue
        # A pattern that already ends on a separator is a section selector and
        # takes everything under it; otherwise the next character may not be a
        # digit, so "2.2" cannot claim "2.20".
        if pattern.endswith(".") or not row[len(pattern):len(pattern) + 1].isdigit():
            return True
    return False


def parse_resolutions(spec: Any, default: Sequence[int] = (30,)) -> List[int]:
    """Resolutions from a run-matrix case, however the manifest spells them.

    The catalogue writes them for humans — ``"10 / 30"``, ``"2 (+30)"``,
    ``"5 + 90"`` — and tier B used to ignore that and run everything at 30 m,
    which is how the "2 m snap grid" case got tested at 30 m and the case that
    only overflows at 2 m never overflowed.
    """
    if isinstance(spec, (list, tuple)):
        out = [int(r) for r in spec]
    elif isinstance(spec, (int, float)):
        out = [int(spec)]
    elif isinstance(spec, str):
        # Decimals stay whole: r"\d+" turned "0.5" into [0, 5], and a 0 m
        # resolution divides by zero deep inside the plugin's tile geometry.
        out = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", spec)]
    else:
        out = []
    usable = sorted({int(round(v)) for v in out if v >= 1})
    if usable:
        return usable
    # Something was written and none of it is a resolution this system has.
    # Saying so beats silently planning at the default.
    return [] if out else [int(r) for r in default]


def matrix_case(raw: Any) -> Dict[str, Any]:
    """One run-matrix case as ``{bbox, resolutions, must_fail, stresses}``.

    Accepts the rich form the generator writes now and the bare ``[w,s,e,n]``
    list older manifests carry, so a stale manifest degrades to "planned at
    30 m, expected to succeed" instead of crashing.
    """
    if isinstance(raw, dict):
        box = raw.get("bbox") or raw.get("box") or []
        return {"bbox": [float(v) for v in box],
                "resolutions": parse_resolutions(raw.get("res_m")),
                "must_fail": bool(raw.get("must_fail")),
                "refused_by": raw.get("refused_by", ""),
                "stresses": raw.get("stresses", "")}
    return {"bbox": [float(v) for v in raw], "resolutions": [30],
            "must_fail": False, "refused_by": "", "stresses": ""}


def matrix_bbox(raw: Any) -> List[float]:
    """``[west, south, east, north]`` of a run-matrix case in either form."""
    return matrix_case(raw)["bbox"]


# ---------------------------------------------------------------------------
# Tier A — the plugin's verdicts, whether layers draw, and what tiles decode to
# ---------------------------------------------------------------------------

def _probe_extent(row: Dict[str, Any], manifest: Dict[str, Any], layer):
    """Ground where this row is supposed to have data."""
    from qgis.core import QgsRectangle

    matrix = manifest.get("run_matrix") or {}
    named = {"4.3": "NRW (WCS coverage)", "4.4": "Colorado (3DEP)",
             "2.19": "Los Angeles (feet CRS)", "2.20": "Prague (Krovak)",
             "2.4": "Dead Sea"}
    # Longest prefix first, so "2.4" can never claim "2.42", and the digit
    # boundary in row_selected keeps "2.4" off "2.40".
    for prefix in sorted(named, key=len, reverse=True):
        if row_selected(row["row"], [prefix]) and named[prefix] in matrix:
            return QgsRectangle(*matrix_bbox(matrix[named[prefix]]))
    extent = layer.extent()
    if extent.isEmpty() or extent.width() > 60 or extent.height() > 60:
        aoi = manifest.get("reference_aoi")
        return QgsRectangle(*aoi) if aoi and len(aoi) == 4 else extent
    return extent


def _plan_row_checks(row: Dict[str, Any], results: Results) -> None:
    """Declare, before anything runs, what this row is going to be checked for.

    Anything declared here and not executed is reported as NOTRUN. That is the
    difference between "we made 197 checks" and "the manifest asked for 214 and
    17 never happened".
    """
    name = row["row"]
    for check_id in ("layer.present", "layer.valid", "layer.source"):
        results.plan(name, check_id)
    if row.get("crs"):
        results.plan(name, "layer.crs")
    if row.get("expect_class"):
        results.plan(name, "classify")
    if row.get("check") == "reject":
        results.plan(name, "classify.reject")
    if row.get("expect_encoding"):
        results.plan(name, "encoding")
    if row.get("expect_zmax"):
        results.plan(name, "zmax")
    if row.get("expect_service_zmax"):
        results.plan(name, "zmax.service_limit")
    if row.get("min_scale"):
        results.plan(name, "min_scale")
    if "type=xyz" not in (row.get("source") or ""):
        results.plan(name, "render")
    if row.get("kind") == "vector":
        results.plan(name, "features")


def open_project(manifest: Dict[str, Any], results: Results):
    """The torture project, opened once and shared by every tier.

    Returns the QgsProject, or None having said why. Both tiers need it: tier A
    reads the layers, and the pipeline tier hands the very same QgsMapLayer
    objects to the Map Converter's own resolver, which is the only way a
    rendered service (WMS/WMTS/ArcGIS) can be exported per tile at all.
    """
    from qgis.core import QgsProject

    project = QgsProject.instance()
    project_path = Path(manifest["__manifest_dir__"]) / manifest["project"]
    if ("-", "project.open") in results.executed:
        return project                      # already opened by an earlier tier
    results.plan("-", "project.open")
    if Path(project.fileName() or "x").resolve() == project_path.resolve():
        results.add("-", "project.open", "use the open project", PASS, manifest["build"])
        return project
    if project.isDirty() and project.fileName():
        # Pasted into the Python Console with another project open, `read()`
        # replaces it — unsaved edits and all. Losing somebody's work to a test
        # run is not a trade this harness gets to make.
        results.add("-", "project.open", "open the project", FAIL,
                    f"{Path(project.fileName()).name} is open with unsaved changes; "
                    f"opening {project_path.name} would discard them. Save or close it "
                    f"first, or run this from a shell instead of the console.")
        return None
    if project.read(str(project_path)):
        results.add("-", "project.open", "open the project", PASS, manifest["build"])
        return project
    results.add("-", "project.open", "open the project", FAIL,
                f"could not read {project_path}")
    return None


def tier_a(manifest: Dict[str, Any], results: Results, only: Sequence[str],
           allow_missing_plugin: bool) -> None:
    from qgis.core import (QgsMapRendererSequentialJob, QgsMapSettings,
                           QgsRasterLayer, QgsVectorLayer)
    from qgis.PyQt.QtCore import QSize
    from qgis.PyQt.QtGui import QColor

    results.plan("-", "plugin.import")
    try:
        from waveshed.core import terrain_adapter as adapter
        from waveshed.core.layer_utils import classify_raster_layer, dem_layer_warning
        results.add("-", "plugin.import", "import the waveshed plugin", PASS)
    except ImportError as exc:
        # This is not a detail. Without the plugin the run loses every verdict
        # check it exists to make — the classifier, the encoding resolver, the
        # zoom clamp — and the old runner reported that as a SKIP and still
        # exited 0. A green run that tested none of the plugin is worse than a
        # red one.
        adapter = None
        classify_raster_layer = dem_layer_warning = None
        results.add("-", "plugin.import", "import the waveshed plugin",
                    SKIP if allow_missing_plugin else FAIL,
                    f"{exc} — every plugin verdict check is lost; layer and render "
                    f"checks still run")

    out_dir = Path(manifest["__manifest_dir__"])
    project = open_project(manifest, results)
    if project is None:
        return

    by_name: Dict[str, List[Any]] = {}
    for layer in project.mapLayers().values():
        by_name.setdefault(layer.name(), []).append(layer)

    # This harness does NOT modify the project. An earlier version added a
    # coordinate operation here "to be self-sufficient", and on a machine where
    # adding one breaks the bounding-box transform it silently broke rows 2.19
    # and 2.20 — then reported the breakage it had just caused. A test that
    # changes what it measures is worse than no test.
    context = project.transformContext()
    destination = project.crs()
    _check_transforms(manifest, results, context, destination, project)

    rows = [r for r in manifest["rows"] if row_selected(r["row"], only)]
    if only and not rows:
        results.add("-", "rows.selected", f"--rows {','.join(only)} selects something",
                    FAIL, "no manifest row matches — the filter is a typo, and a run that "
                          "checks nothing must not report success")
        return
    for row in rows:
        _plan_row_checks(row, results)

    for row in rows:
        try:
            _check_row(row, by_name, manifest, results, out_dir, adapter,
                       classify_raster_layer, dem_layer_warning,
                       destination, context, QgsRasterLayer, QgsVectorLayer,
                       QgsMapSettings, QgsMapRendererSequentialJob, QSize, QColor)
        except Exception as exc:
            # One row must not end the run: everything after it would be
            # reported as NOTRUN, and the cause would be a traceback rather
            # than a named failing check.
            results.add(row["row"], "row.crashed", "the row's checks ran to completion",
                        FAIL, f"{type(exc).__name__}: {exc}")


def _check_row(row, by_name, manifest, results, out_dir, adapter,
               classify_raster_layer, dem_layer_warning, destination, context,
               QgsRasterLayer, QgsVectorLayer, QgsMapSettings,
               QgsMapRendererSequentialJob, QSize, QColor) -> None:
    """Every tier-A check for one catalogue row."""
    layer = _resolve_layer(row, by_name, results)
    if layer is None:
        return
    if not _check_layer_loads(row, layer, results, out_dir):
        return
    if isinstance(layer, QgsRasterLayer):
        _check_raster_verdicts(row, layer, results, adapter,
                               classify_raster_layer, dem_layer_warning)
    else:
        # The raster verdicts were planned from the row's own fields. If the
        # project layer is not a raster the catalogue and the project disagree
        # about what this row IS — say that once, and withdraw the checks that
        # can now neither run nor be dismissed (they would sit at NOTRUN
        # forever with no route to any outcome).
        if any(row.get(field) for field in ("expect_class", "expect_encoding", "expect_zmax")) \
                or row.get("check") == "reject":
            results.add(row["row"], "layer.kind", "is the raster the catalogue describes",
                        FAIL, f"the project layer is a {type(layer).__name__}, so none of "
                              f"this row's raster expectations can be checked")
        for check_id in ("classify", "classify.reject", "encoding", "zmax",
                         "zmax.service_limit"):
            results.unplan(row["row"], check_id)
    if not isinstance(layer, QgsVectorLayer):
        results.unplan(row["row"], "features")
    _check_min_scale(row, layer, results)

    extent = _probe_extent(row, manifest, layer)
    drew, description = _render_probe(row, layer, extent, destination, context,
                                      QgsMapSettings, QgsMapRendererSequentialJob,
                                      QSize, QColor)
    if "type=xyz" in (row.get("source") or ""):
        # A render cannot test a tile layer: QGIS fetches tiles in worker
        # threads and waitForFinished() returns when the render pass is over,
        # not when the tiles have arrived, so the picture reports whatever the
        # network cache happened to hold. Whether these tiles become real
        # terrain is settled in tier C, by the real downloader against the
        # real .abt — so this is a note, never a pass.
        results.unplan(row["row"], "render")
        results.note(f"{row['row']} render (advisory, tile layer): {description}")
    elif row["expect_renders"]:
        # Only "nothing at all" is a verdict. How much of the view a layer
        # fills is not: a point layer, an outline and a tile that covers a
        # corner of the probe box are all legitimately mostly blank.
        results.add(row["row"], "render", "draws something",
                    PASS if drew else FAIL,
                    description if drew else
                    "renders blank over ground where it should have data",
                    known_fail_for(row, "render"))
    else:
        results.add(row["row"], "render", "draws nothing (by design)",
                    PASS if not drew else FAIL,
                    "" if not drew else f"drew {description} but should be empty",
                    known_fail_for(row, "render"))

    if isinstance(layer, QgsVectorLayer):
        _check_features(row, layer, results)


def _resolve_layer(row: Dict[str, Any], by_name: Dict[str, List[Any]],
                   results: Results):
    """The project layer this row names, or None (having said why)."""
    found = by_name.get(row["name"]) or []
    if not found:
        results.add(row["row"], "layer.present", "layer present", FAIL,
                    "not in the project", known_fail_for(row, "layer.present"))
        return None
    if len(found) > 1:
        # Two layers with one name means the join key is ambiguous and every
        # verdict below is about whichever one happened to be first.
        results.add(row["row"], "layer.present", "layer present", FAIL,
                    f"{len(found)} layers share the name {row['name']!r} — the manifest "
                    f"cannot say which one it means")
        return None
    results.add(row["row"], "layer.present", "layer present", PASS)
    return found[0]


def _check_layer_loads(row: Dict[str, Any], layer, results: Results,
                       out_dir: Path) -> bool:
    """``layer.valid``, ``layer.source`` and ``layer.crs``. False = stop here."""
    if not layer.isValid():
        # Remote services throttle; one blip should not fail a suite.
        remote = row["provider"] in ("wms", "wcs")
        if remote:
            time.sleep(3)
            layer.reload()
        if not layer.isValid():
            results.add(row["row"], "layer.valid", "layer valid", FAIL,
                        " ".join((layer.error().summary() or "").split())[:90]
                        + (" (retried once)" if remote else ""),
                        known_fail_for(row, "layer.valid"))
            return False
        results.add(row["row"], "layer.valid", "layer valid", PASS,
                    "loaded only on the second try — the service is throttling")
    else:
        results.add(row["row"], "layer.valid", "layer valid", PASS)

    # The project and the manifest are written by the same run of the same
    # generator, so they agree — until somebody edits the .qgs by hand, or runs
    # against a project from an older build. Every verdict below is about the
    # layer's URI; if that URI is not the one the catalogue described, the row
    # is testing something nobody wrote down.
    actual_source = (layer.source() or "").strip()
    expected_source = (row.get("source") or "").strip()
    if not expected_source:
        results.unplan(row["row"], "layer.source")
    else:
        same, difference = source_matches(expected_source, actual_source, out_dir)
        results.add(row["row"], "layer.source", "URI matches the catalogue",
                    PASS if same else FAIL,
                    "" if same else f"{difference} — regenerate the project",
                    known_fail_for(row, "layer.source"))

    if row["crs"]:
        actual = layer.crs().authid()
        results.add(row["row"], "layer.crs", "CRS", PASS if actual == row["crs"] else FAIL,
                    "" if actual == row["crs"] else f"{actual} != {row['crs']}",
                    known_fail_for(row, "layer.crs"))
    return True


def _tail(text: str, width: int = 200) -> str:
    """The END of an engine message.

    ``aether_converter`` and ``aether_core`` print a banner first and the actual
    cause last ("Caused by: …"), so truncating from the front throws away the
    only part worth reading — which is what every ingest failure in this file
    reported until now.
    """
    text = " ".join(text.split())
    return text if len(text) <= width else "\u2026" + text[-(width - 1):]


def _shorten(text: str, width: int = 70) -> str:
    return text if len(text) <= width else text[:width - 1] + "\u2026"


def _check_raster_verdicts(row: Dict[str, Any], layer, results: Results, adapter,
                           classify_raster_layer, dem_layer_warning) -> None:
    """The plugin's own opinions about this layer."""
    source = layer.source() or ""

    if row.get("expect_class"):
        if classify_raster_layer is None:
            results.unplan(row["row"], "classify")
        else:
            actual = classify_raster_layer(layer)
            ok = actual == row["expect_class"]
            results.add(row["row"], "classify", f"classified as {row['expect_class']}",
                        PASS if ok else FAIL, "" if ok else f"got {actual}",
                        known_fail_for(row, "classify"))

    # ``check: reject`` is the catalogue saying "the plugin must never offer
    # this as terrain". Classification alone does not say that — 4.4b is a
    # rendered picture whose URL contains "elevation", and the classifier calls
    # it a DEM. The user-facing consequence is whether a warning appears, so
    # that is what gets asserted.
    if row.get("check") == "reject":
        if dem_layer_warning is None:
            results.unplan(row["row"], "classify.reject")
        else:
            warning = dem_layer_warning(layer)
            results.add(row["row"], "classify.reject", "refused as a DEM source",
                        PASS if warning else FAIL,
                        _shorten(" ".join((warning or "").split()), 80) if warning else
                        "the plugin offers this layer as terrain with no warning — it is a "
                        "rendered picture, and running an analysis over it fails silently",
                        known_fail_for(row, "classify.reject"))

    if row.get("expect_encoding"):
        if adapter is None or "type=xyz" not in source:
            results.unplan(row["row"], "encoding")
        else:
            try:
                actual = adapter.resolve_xyz_encoding(source)
            except RuntimeError:
                # The documented refusal: the URI names both families and the
                # plugin will not guess. Any OTHER exception is a bug in the
                # resolver, not the refusal the row asked for, so it is not
                # allowed to satisfy `expect_encoding: error`.
                actual = "error"
            except Exception as exc:
                actual = f"crashed: {type(exc).__name__}"
            ok = actual == row["expect_encoding"]
            results.add(row["row"], "encoding", f"encoding {row['expect_encoding']}",
                        PASS if ok else FAIL, "" if ok else f"got {actual}",
                        known_fail_for(row, "encoding"))

    _check_service_zmax(row, source, results, adapter)

    if row.get("expect_zmax"):
        if adapter is None or "type=xyz" not in source:
            results.unplan(row["row"], "zmax")
        else:
            try:
                actual, warning = adapter.resolve_zmax(source)
            except Exception as exc:
                actual, warning = None, None
                results.add(row["row"], "zmax", f"zmax clamps to {row['expect_zmax']}",
                            FAIL, f"resolve_zmax raised {type(exc).__name__}: {exc}",
                            known_fail_for(row, "zmax"))
            if actual is not None:
                ok = actual == row["expect_zmax"]
                raw = int_param(uri_params(source), "zmax", 15)
                # A clamp that is not a clamp is worth saying out loud: three of
                # the four rows carrying expect_zmax name the value already in
                # their URI, so the check passes without the clamp doing
                # anything. Only a row where raw > effective proves the clamp.
                clamped = raw != actual
                detail = "" if ok else f"got {actual}"
                if ok and clamped and not warning:
                    ok = False
                    detail = (f"clamped z{raw} -> z{actual} but produced no warning; the "
                              f"user is never told their layer's zoom was overridden")
                elif ok and not clamped:
                    detail = f"z{actual} (the URI's own value — this row does not test the clamp)"
                results.add(row["row"], "zmax", f"zmax clamps to {row['expect_zmax']}",
                            PASS if ok else FAIL, detail,
                            known_fail_for(row, "zmax"))


def _check_service_zmax(row: Dict[str, Any], source: str, results: Results,
                        adapter) -> None:
    """Does the plugin KNOW how deep this service goes?

    ``expect_zmax`` only says what ``resolve_zmax`` returns for the URI the
    catalogue wrote — and the catalogue writes the right zmax, so for a service
    the plugin has never heard of the check passes by echoing its own input.
    That is not the interesting question. The interesting question is what
    happens to the layer a USER adds, which arrives carrying QGIS's default
    ``zmax=18``: without a ``max_zoom`` entry the plugin cannot clamp it, every
    request past the real limit fails, the converter writes those tiles as 0 m
    and the analysis completes, confidently, over a flat sea-level plane.

    So this asks the question that cannot be answered by construction: does
    ``known_services.json`` know this service's real limit?
    """
    expected = int(row.get("expect_service_zmax") or 0)
    if not expected or adapter is None:
        results.unplan(row["row"], "zmax.service_limit")
        return
    template = xyz_template(source)
    known = adapter.service_max_zoom(template)
    if known == expected:
        # ...and prove the clamp really fires on a default-zmax layer.
        hand_added = re.sub(r"(^|&)zmax=\d+", r"\1zmax=18", source)
        if "zmax=" not in hand_added:
            hand_added += "&zmax=18"
        clamped, warning = adapter.resolve_zmax(hand_added)
        ok = clamped == expected and bool(warning)
        detail = (f"known_services says z{known}; a hand-added layer at QGIS's default "
                  f"zmax=18 is clamped to z{clamped}") if ok else (
            f"known_services says z{known}, but a layer at QGIS's default zmax=18 "
            f"resolves to z{clamped}" + ("" if warning else " with no warning"))
    else:
        ok = False
        detail = (f"known_services.json has no max_zoom for this service ({known!r}), so a "
                  f"hand-added layer keeps QGIS's default zmax=18. Every request past "
                  f"z{expected} fails, the converter writes those tiles as 0 m, and the "
                  f"analysis runs over a flat sea. Add \"max_zoom\": {expected}.")
    results.add(row["row"], "zmax.service_limit",
                f"the plugin knows this service stops at z{expected}",
                PASS if ok else FAIL, detail,
                known_fail_for(row, "zmax.service_limit"))


def _check_min_scale(row: Dict[str, Any], layer, results: Results) -> None:
    """A row that declares a scale limit must actually carry it.

    Row 4.3's server answers HTTP 400 to a request for its full extent, and the
    scale limit is what stops QGIS asking. The old runner switched that limit
    OFF to take its render probe and never checked it was there — so the one
    setting protecting the row was the one thing it could not see.
    """
    limit = row.get("min_scale") or 0
    if not limit:
        return
    on = bool(layer.hasScaleBasedVisibility())
    actual = float(layer.minimumScale() or 0.0)
    ok = on and abs(actual - float(limit)) <= max(1.0, float(limit) * 1e-6)
    results.add(row["row"], "min_scale", f"hidden above 1:{int(limit):,}",
                PASS if ok else FAIL,
                "" if ok else ("scale-based visibility is off" if not on else
                               f"limit is 1:{actual:,.0f}, catalogue says 1:{int(limit):,}"),
                known_fail_for(row, "min_scale"))


def _render_probe(row: Dict[str, Any], layer, extent, destination, context,
                  QgsMapSettings, QgsMapRendererSequentialJob, QSize, QColor
                  ) -> Tuple[bool, str]:
    """``(drew_anything, description)`` over ground where the row has data."""
    limited = layer.hasScaleBasedVisibility()
    was_dirty = None
    if limited:
        # Toggling this marks the project dirty. The toggle is restored below,
        # so the project is unchanged — but QGIS would still offer to save it on
        # the way out, over an edit that was never made.
        try:
            from qgis.core import QgsProject
            was_dirty = QgsProject.instance().isDirty()
        except Exception:
            was_dirty = None
        layer.setScaleBasedVisibility(False)
    try:
        settings = QgsMapSettings()
        settings.setLayers([layer])
        settings.setOutputSize(QSize(160, 160))
        settings.setBackgroundColor(QColor(255, 255, 255))
        settings.setDestinationCrs(destination)
        settings.setTransformContext(context)
        settings.setExtent(extent)

        def once() -> Tuple[int, float]:
            job = QgsMapRendererSequentialJob(settings)
            job.start()
            job.waitForFinished()
            image = job.renderedImage()
            pixels = [image.pixel(x, y) for x in range(0, 160, 2) for y in range(0, 160, 2)]
            return len(set(pixels)), sum(1 for v in pixels if v == 0xFFFFFFFF) / len(pixels)

        colours, blank = once()
        if colours <= 1 and row["expect_renders"] and row["provider"] in ("wms", "wcs"):
            # Remote services throttle. One blank render is not evidence.
            time.sleep(4)
            colours, blank = once()
    finally:
        if limited:
            layer.setScaleBasedVisibility(True)
            if was_dirty is False:
                try:
                    from qgis.core import QgsProject
                    QgsProject.instance().setDirty(False)
                except Exception:
                    pass
    return colours > 1, f"{colours} colours, {blank * 100:.0f}% blank"


def _check_features(row: Dict[str, Any], layer, results: Results) -> None:
    """Vector rows: a real count, against the catalogue's minimum."""
    count = layer.featureCount()
    minimum = int(row.get("expect_features") or 1)
    if count < 0:
        # Some providers report -1 for "unknown". Counting is then the only way
        # to find out, and it is cheap at these fixture sizes.
        count = sum(1 for _ in layer.getFeatures())
    ok = count >= minimum
    results.add(row["row"], "features", "has features", PASS if ok else FAIL,
                f"{count} features" + ("" if ok else f", catalogue expects >= {minimum}"),
                known_fail_for(row, "features"))


def _check_transforms(manifest: Dict[str, Any], results: Results, context,
                      destination, project) -> None:
    """Every CRS the project needs must transform on THIS machine.

    Both directions of the check matter and they fail independently: a point
    can transform where a bounding box cannot, and it is the bounding box that
    decides whether QGIS draws the layer at all.
    """
    from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform

    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    for authid in sorted({r["crs"] for r in manifest["rows"] if r["crs"]}):
        crs = QgsCoordinateReferenceSystem(authid)
        if not crs.isValid() or crs == destination:
            continue
        results.plan("-", f"crs.transform:{authid}")
        transform = QgsCoordinateTransform(crs, destination, context)
        bounds = crs.bounds()          # always WGS84 degrees
        ok, detail = transform.isValid() and not bounds.isEmpty(), ""
        if ok:
            # Round-trip, so the tolerance is in the units of the CRS we started
            # from instead of an assumption about the project's. The old check
            # asked whether the result was within +-200/+-100 "degrees", which
            # every projected project CRS fails by construction.
            try:
                forward = QgsCoordinateTransform(wgs84, crs, context)
                native = forward.transform(bounds.center())
                there = transform.transform(native)
                back = QgsCoordinateTransform(destination, crs, context).transform(there)
                error = math.hypot(back.x() - native.x(), back.y() - native.y())
                span = max(abs(native.x()), abs(native.y()), 1.0)
                ok = error <= span * 1e-6 + 1e-6
                detail = "" if ok else (f"round-trip through {destination.authid()} moves the "
                                        f"point by {error:,.3f} CRS units")
            except Exception as exc:
                ok, detail = False, str(exc)[:80]
        if not ok and not detail:
            detail = "fails on this machine — layers in this CRS cannot draw"
        results.add("-", f"crs.transform:{authid}",
                    f"transform {authid} -> {destination.authid()} (point)",
                    PASS if ok else FAIL, detail)

        # The call QGIS actually makes when placing a layer. A point can succeed
        # where this fails, and this is the one that decides whether it draws.
        probes = [l for l in project.mapLayers().values()
                  if l.crs().authid() == authid and not l.extent().isEmpty()]
        if not probes:
            continue
        results.plan("-", f"crs.place:{authid}")
        broken = []
        for probe in probes:
            try:
                box = QgsCoordinateTransform(crs, destination, context) \
                    .transformBoundingBox(probe.extent())
                # x == x is a NaN test: a transform can "succeed" and hand back
                # a box of NaNs, which QGIS then places nowhere at all.
                placed = (not box.isEmpty() and box.xMinimum() == box.xMinimum()
                          and box.yMinimum() == box.yMinimum())
                if not placed:
                    broken.append(f"{probe.name()}: empty or NaN bounding box")
            except Exception as exc:
                broken.append(f"{probe.name()}: {str(exc)[:60]}")
        results.add("-", f"crs.place:{authid}",
                    f"place every {authid} layer (bounding box)",
                    PASS if not broken else FAIL,
                    f"{len(probes)} layer(s)" if not broken else "; ".join(broken[:2]))


# ---------------------------------------------------------------------------
# Tier B — the engine
# ---------------------------------------------------------------------------

def find_binary(name: str) -> Optional[Path]:
    """Same search order the plugin uses, minus the QgsSettings entry."""
    candidates = []
    env = os.environ.get("AETHER_BIN_DIR")
    if env:
        candidates.append(Path(env))
    candidates += [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    candidates.append(Path.home() / ".aether" / "bin")
    for folder in candidates:
        for suffix in ("", ".exe"):
            candidate = folder / (name + suffix)
            if candidate.is_file():
                return candidate
    return None


def _plugin_enumeration(bbox: Dict[str, float], resolutions: Sequence[int]):
    """``(snapped_bbox, [filenames])`` from the PLUGIN's own tile enumeration.

    Imported, not re-implemented. A second implementation of the tile grid
    inside the test harness would agree with itself and prove nothing; these
    are the exact functions the Map Converter runs before every conversion.
    """
    from waveshed.gui.map_converter_tab import _enumerate_tiles, _snap_bbox
    snapped = _snap_bbox(bbox, list(resolutions))
    tiles = _enumerate_tiles(snapped, list(resolutions))
    return snapped, sorted(tile["filename"] for _res, tile in tiles)


def tier_b(manifest: Dict[str, Any], results: Results, only: Sequence[str],
           cases_only: Sequence[str], timeout: int) -> None:
    results.plan("-", "engine.present")
    converter = find_binary("aether_converter")
    if converter is None:
        results.add("-", "engine.present", "engine binaries", SKIP,
                    "aether_converter not found (set AETHER_BIN_DIR) — every tier B check "
                    "is a hole in this run, not a pass")
        return
    results.add("-", "engine.present", "engine binaries", PASS, str(converter))

    cases = {name: matrix_case(raw) for name, raw in (manifest.get("run_matrix") or {}).items()}
    # ``--rows`` selects catalogue ROWS ("1.1", "2."); run-matrix cases are named
    # ("Bern reference AOI"), so no --rows value can ever match one. Filtering
    # them with it silently emptied the plan phase and the run still reported
    # "every check the catalogue declares ran and passed". Cases have their own
    # selector, and a filter that matches nothing is said out loud.
    selected = {name: case for name, case in cases.items()
                if not cases_only or any(o.lower() in name.lower() for o in cases_only)}
    if cases_only and not selected:
        results.add("plan", "plan.selected", f"--cases {','.join(cases_only)} selects a case",
                    FAIL, "no run-matrix case matches — the filter is a typo, and a run "
                          "that checks nothing must not report success")
        return
    if only and not cases_only:
        results.note(f"--rows {','.join(only)} does not apply to the run matrix; all "
                     f"{len(selected)} plan cases run. Use --cases to narrow them.")
    for name in selected:
        results.plan("plan", f"plan:{name}")
        results.plan("plan", f"plan.enumeration:{name}")

    for name, case in selected.items():
        # One malformed case must not end the run: tier_a guards per row, and
        # this loop did not, so a case without a bbox died on an unpack four
        # frames away from anything readable.
        try:
            _check_plan_case(converter, name, case, results, timeout)
        except Exception as exc:
            results.add("plan", f"plan:{name}", f"plan {name}", FAIL,
                        f"the check itself raised {type(exc).__name__}: {exc}")
            results.unplan("plan", f"plan.enumeration:{name}")


def _check_plugin_refuses_size(name: str, case: Dict[str, Any],
                               results: Results) -> None:
    """A case the PLUGIN must refuse on size, asked of the plugin."""
    from waveshed.core.terrain_adapter import (estimate_terrain_disk_mb,
                                               terrain_size_warning)
    west, south, east, north = case["bbox"]
    lat, lon = (south + north) / 2.0, (west + east) / 2.0
    # Half the diagonal, so the sector covers the whole box.
    range_km = max(abs(north - south), abs(east - west)) * 111.0 / 2.0
    resolution = min(case["resolutions"])
    try:
        disk_mb = estimate_terrain_disk_mb(lat, lon, range_km, resolution)
        warning = terrain_size_warning(int(disk_mb))
    except Exception as exc:
        results.add("plan", f"plan:{name}", f"the plugin refuses {name} on size",
                    FAIL, f"{type(exc).__name__}: {exc}")
        return
    results.add("plan", f"plan:{name}", f"the plugin refuses {name} on size",
                PASS if warning else FAIL,
                f"{disk_mb:,.0f} MB at {resolution} m over {range_km:,.0f} km — "
                + (f"warned: {_shorten(' '.join(warning[0].split()), 90)}" if warning else
                   "no size warning at all, so nothing stops this run"))


def _check_plan_case(converter: Path, name: str, case: Dict[str, Any],
                     results: Results, timeout: int) -> None:
    """``aether_converter plan`` for one run-matrix case, both directions.

    ``plan`` is the cross-check the Map Converter runs before every conversion:
    cheap, offline, and it catches tile-geometry regressions immediately. The
    cases the catalogue marks ``must_fail`` are asserted as refusals — the old
    runner expected exit 0 from every case including "Huge (must be refused)",
    so a correctly-refusing engine was reported as a failure and an engine that
    happily planned half a continent was reported as a pass.
    """
    if len(case["bbox"]) != 4:
        results.add("plan", f"plan:{name}", f"plan {name}", FAIL,
                    f"the manifest gives this case no bbox ({case['bbox']!r})")
        results.unplan("plan", f"plan.enumeration:{name}")
        return
    resolutions = case["resolutions"]
    if not resolutions:
        results.add("plan", f"plan:{name}", f"plan {name}", FAIL,
                    "the manifest names no resolution this system supports for this case")
        results.unplan("plan", f"plan.enumeration:{name}")
        return
    west, south, east, north = case["bbox"]
    label = f"plan {name} @ {'/'.join(str(r) for r in resolutions)} m"
    command = [str(converter), "plan",
               "--south", repr(float(south)), "--north", repr(float(north)),
               "--west", repr(float(west)), "--east", repr(float(east)),
               "--resolutions", ",".join(str(r) for r in resolutions)]
    try:
        done = subprocess.run(command, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        # "must never hang" is part of what the Huge case asserts, so a timeout
        # is a failure even for a case that is supposed to be refused.
        results.add("plan", f"plan:{name}", label, FAIL,
                    f"no answer within {timeout}s — the case must be refused quickly, "
                    f"not hang")
        results.unplan("plan", f"plan.enumeration:{name}")
        return
    except Exception as exc:
        results.add("plan", f"plan:{name}", label, FAIL, str(exc)[:90])
        results.unplan("plan", f"plan.enumeration:{name}")
        return

    refused = done.returncode != 0
    complaint = " ".join(((done.stderr or "") + " " + (done.stdout or "")).split())[:110]
    if case.get("refused_by") == "plugin":
        # The refusal this case names is the PLUGIN's size guard, not a plan
        # failure: `plan` enumerates geometry and has no size policy at all.
        _check_plugin_refuses_size(name, case, results)
        results.unplan("plan", f"plan.enumeration:{name}")
        return
    if case["must_fail"]:
        results.add("plan", f"plan:{name}", label + " must be refused",
                    PASS if refused else FAIL,
                    complaint if refused else
                    f"exit 0 — the engine planned a case the catalogue says it must "
                    f"refuse ({case['stresses']})")
        # Nothing to cross-check against when the answer is a refusal.
        results.unplan("plan", f"plan.enumeration:{name}")
        return
    if refused:
        results.add("plan", f"plan:{name}", label, FAIL,
                    complaint or f"exit {done.returncode}")
        results.unplan("plan", f"plan.enumeration:{name}")
        return

    try:
        payload = json.loads(done.stdout)
    except ValueError:
        results.add("plan", f"plan:{name}", label, FAIL,
                    f"output was not JSON: {_shorten(done.stdout.strip(), 80)!r}")
        results.unplan("plan", f"plan.enumeration:{name}")
        return
    schema = payload.get("schema") if isinstance(payload, dict) else None
    if schema != "aether-plan/1":
        # The plugin refuses to convert against an unknown schema; so does this.
        results.add("plan", f"plan:{name}", label, FAIL,
                    f"schema {schema!r}, expected 'aether-plan/1' — this engine and this "
                    f"plugin do not share a plan contract")
        results.unplan("plan", f"plan.enumeration:{name}")
        return
    theirs = sorted(t.get("filename", "?") for t in payload.get("tiles", []))
    results.add("plan", f"plan:{name}", label, PASS,
                f"{payload.get('tile_count')} tiles")

    # The promise the docstring makes: the engine's grid and the PLUGIN's own
    # enumeration must be identical, filename for filename.
    bbox = {"west": float(west), "south": float(south),
            "east": float(east), "north": float(north)}
    try:
        _snapped, ours = _plugin_enumeration(bbox, resolutions)
    except Exception as exc:
        results.add("plan", f"plan.enumeration:{name}",
                    f"engine grid == plugin grid for {name}", FAIL,
                    f"the plugin's own enumeration could not be imported: {exc}")
        return
    same = theirs == ours and payload.get("tile_count") == len(ours)
    detail = f"{len(ours)} tiles agree"
    if not same:
        only_ours = [n for n in ours if n not in set(theirs)]
        only_theirs = [n for n in theirs if n not in set(ours)]
        detail = (f"plugin enumerates {len(ours)}, engine reports "
                  f"{payload.get('tile_count')} ({len(theirs)} filenames)")
        if only_ours:
            detail += f"; only the plugin has {only_ours[0]}"
        if only_theirs:
            detail += f"; only the engine has {only_theirs[0]}"
    results.add("plan", f"plan.enumeration:{name}",
                f"engine grid == plugin grid for {name}",
                PASS if same else FAIL, detail)


#: clap's spellings for "I do not have that subcommand", copied from the
#: plugin's own map_converter_tab._UNKNOWN_SUBCOMMAND_HINTS.
_ENGINE_TOO_OLD = ("unrecognized subcommand", "unknown subcommand",
                   "invalid subcommand", "wasn't expected", "wasn't recognized")


def _refusal_is_the_right_one(row: Dict[str, Any], complaint: str, source: str,
                              exit_code: int) -> Tuple[bool, str]:
    """Did a ``check: error`` row fail the way the catalogue says it must?

    Three ways a refusal can be the wrong one, all of which used to read as a
    pass: the engine has no such subcommand at all; it said nothing; or it said
    something unrelated to this source. ``expect_failure`` pins the wording
    where the catalogue knows it; otherwise the floor is the contract text
    every ``check: error`` row shares — fail LOUDLY, naming the file.
    """
    low = complaint.lower()
    if any(hint in low for hint in _ENGINE_TOO_OLD):
        return False, ("this engine has no such subcommand — that is an engine too old "
                       "for this plugin, not a refusal of the source")
    if not complaint:
        return False, (f"exit {exit_code} and not one word of explanation — the catalogue "
                       f"says this must fail LOUDLY, and a silent failure is "
                       f"indistinguishable from the engine falling over")
    wanted = (row.get("expect_failure") or "").strip()
    if wanted:
        if wanted.lower() in low:
            return True, _shorten(complaint, 110)
        return False, (f"refused, but without naming {wanted!r}: {_shorten(complaint, 90)}")
    # What "naming the source" means depends on what the source IS. A file row
    # must see its filename; a service row has no filename, so the host it
    # could not use is the identifying thing. Taking Path(...).name of a URI
    # produced "{y}.webp?key=…" and demanded the engine echo that.
    if "url=" in source or "://" in source:
        # A service has no filename. "Which source did it refuse?" is answered
        # by the catalogue pinning the wording in expect_failure, handled
        # above; with nothing pinned, a loud refusal is the whole contract.
        return True, _shorten(complaint, 110)
    stem = Path(_VSI.sub("", source).partition("|")[0]).name
    if stem and stem.lower() in low:
        return True, _shorten(complaint, 110)
    return False, (f"refused without naming the file ({stem!r}), so there is no way "
                   f"to tell WHICH source it refused: {_shorten(complaint, 80)}")


# ---------------------------------------------------------------------------
# Tier C — the pipeline, end to end, driven through the plugin
# ---------------------------------------------------------------------------
#
# Nothing here re-implements the plugin. Every stage is a call into it:
#
#   route a source        map_converter_tab._resolve_source_on_main_thread
#   enumerate tiles       map_converter_tab._snap_bbox / _enumerate_tiles
#   build ingest jobs     map_converter_tab._build_tile_jobs
#   download XYZ          terrain_adapter.ensure_pool_tiles
#   read a .abt back      core.abt.read_header / read_tile
#   build a core job      core.job_builder.build_coverage_job / build_p2p_job
#   purge the cache       terrain_adapter.clear_cache
#
# The engine binaries are the real ones. There is no stub anywhere in this
# file, and there must never be: a test double for the component under test
# proves only that the double behaves like the double.


class Pipeline:
    """One isolated pipeline environment, torn down after every row.

    The cache root is redirected to a scratch directory (the plugin reads it
    from ``QgsSettings("waveshed/cache_dir")``), so a run never touches the
    user's real terrain cache and never inherits a tile from the row before
    it. That isolation is not tidiness: a failed download leaves a full-size
    all-zero .abt in the pool, and the NEXT run of that layer reports a cache
    hit and produces a confident coverage over flat sea. A pipeline test
    sharing one pool measures history, not the code.
    """

    def __init__(self, scratch: Path, binaries: Dict[str, Path],
                 keep: bool = False) -> None:
        self.scratch = scratch
        self.binaries = binaries
        self.keep = keep
        self._previous_cache: Optional[str] = None

    # -- environment -------------------------------------------------------
    def __enter__(self) -> "Pipeline":
        from qgis.core import QgsSettings
        settings = QgsSettings()
        self._previous_cache = settings.value("waveshed/cache_dir", "")
        self.cache = self.scratch / "cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        settings.setValue("waveshed/cache_dir", str(self.cache))
        return self

    def __exit__(self, *exc) -> None:
        from qgis.core import QgsSettings
        from waveshed.core import terrain_adapter as adapter
        try:
            adapter.clear_cache()            # the plugin's own purge
        except Exception:
            pass
        settings = QgsSettings()
        if self._previous_cache:
            settings.setValue("waveshed/cache_dir", self._previous_cache)
        else:
            settings.remove("waveshed/cache_dir")
        if not self.keep:
            shutil.rmtree(self.scratch, ignore_errors=True)

    # -- engine ------------------------------------------------------------
    def engine(self, name: str, args: Sequence[Any], timeout: int) -> Tuple[int, str]:
        exe = self.binaries[name]
        try:
            done = subprocess.run([str(exe)] + [str(a) for a in args],
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, f"no answer within {timeout}s"
        return done.returncode, " ".join(((done.stdout or "") + " "
                                          + (done.stderr or "")).split())


def _plugin_modules():
    """The plugin entry points the pipeline drives. Imported once, lazily."""
    from waveshed.core import abt as abt_mod
    from waveshed.core import terrain_adapter as adapter
    from waveshed.core.job_builder import (CoverageParams, P2PParams,
                                           build_coverage_job, build_p2p_job)
    from waveshed.gui import map_converter_tab as mc
    return {"abt": abt_mod, "adapter": adapter, "mc": mc,
            "CoverageParams": CoverageParams, "P2PParams": P2PParams,
            "build_coverage_job": build_coverage_job,
            "build_p2p_job": build_p2p_job}


#: What ``aether_converter`` writes, in i16 counts, for a pixel no source
#: covered. Measured, because the plugin has no constant for it — and that is
#: the point of ``_check_void_sentinel``: the plugin's own validity floor
#: (MIN_VALID_ELEV_M, -5000 m = -10000 counts) sits one count BELOW this, so
#: every hole reads as valid ground at -4999.5 m.
CONVERTER_VOID_COUNTS = -9999


def _real_mask(grid, plugin):
    """Pixels that are terrain: above the plugin's floor AND not the sentinel.

    The second half is a workaround for the defect ``_check_void_sentinel``
    reports. Dropping it would make every comparison here treat a hole as
    ground, which is exactly the mistake being measured.
    """
    abt_mod = plugin["abt"]
    floor = abt_mod.MIN_VALID_ELEV_M / abt_mod.ELEV_STEP_M
    return (grid > floor) & (grid != CONVERTER_VOID_COUNTS)


def abt_report(paths: Sequence[str], plugin) -> Dict[str, Any]:
    """What a set of .abt tiles actually contains, via the plugin's reader."""
    abt_mod = plugin["abt"]
    real = total = 0
    lo = hi = None
    grids = []
    for path in paths:
        header = abt_mod.read_header(str(path))
        if header is None:
            return {"unreadable": str(path)}
        grid = abt_mod.read_tile(header)
        if grid is None:
            return {"unreadable": str(path)}
        grids.append((header, grid))
        total += int(grid.size)
        mask = _real_mask(grid, plugin)
        n = int(mask.sum())
        if n:
            real += n
            a, b = int(grid[mask].min()), int(grid[mask].max())
            lo = a if lo is None or a < lo else lo
            hi = b if hi is None or b > hi else hi
    out: Dict[str, Any] = {"tiles": len(paths), "real_px": real, "total_px": total,
                           "grids": grids}
    if real:
        out["min_m"] = lo * abt_mod.ELEV_STEP_M
        out["max_m"] = hi * abt_mod.ELEV_STEP_M
    return out


def abt_at(grids, lat: float, lon: float, plugin) -> Optional[float]:
    """The elevation the tiles hold at *lat*/*lon*, or None if outside them."""
    step = plugin["abt"].ELEV_STEP_M
    for header, grid in grids:
        row = int((header.ul_lat - lat) / header.pixel_res)
        col = int((lon - header.ul_lon) / header.pixel_res)
        if 0 <= row < grid.shape[0] and 0 <= col < grid.shape[1]:
            return float(grid[row, col]) * step
    return None


def _layer_entry(row: Dict[str, Any], layer, plugin, resolutions: Sequence[int],
                 extent: Optional[Dict[str, float]] = None):
    """One Map Converter stack entry for this row, built the tab's own way."""
    mc = plugin["mc"]
    kind = "buildings" if row.get("kind") == "vector" else "raster"
    return mc._LayerEntry(
        layer_type=kind,
        source_path=layer.source() if layer is not None else row["source"],
        qgis_layer=layer,
        crs_authid=row.get("crs") or "EPSG:4326",
        target_resolutions=list(resolutions),
        extent=extent,
    )


def _acquire(row, layer, plugin, pipe: Pipeline, bbox, resolutions, timeout):
    """Terrain for one row, through the plugin's own router.

    ``_resolve_source_on_main_thread`` is the single place that decides how a
    source is acquired — xyz through the shared downloader, a file untouched,
    a WMS/WMTS/ArcGIS service exported per tile through QGIS. Calling it here
    rather than deciding ourselves is the whole point: a router the test
    reimplements is a router the test cannot check.

    Returns ``(abt_paths, kind, note)``.
    """
    mc = plugin["mc"]
    adapter = plugin["adapter"]
    expected = mc._enumerate_tiles(mc._snap_bbox(bbox, list(resolutions)),
                                   list(resolutions))
    entry = _layer_entry(row, layer, plugin, resolutions, bbox)
    resolved = mc._resolve_source_on_main_thread(entry, expected)
    kind = resolved.get("kind") if isinstance(resolved, dict) else "buildings"

    tmp = pipe.scratch / "src"
    tmp.mkdir(parents=True, exist_ok=True)
    out = pipe.scratch / "abt"
    out.mkdir(parents=True, exist_ok=True)
    if kind == "file":
        info = adapter.source_file_info(resolved["path"], row.get("crs") or None)
        entries = [{"kind": "files", "infos": [info]}]
    elif kind == "rendered":
        entries = [{"kind": "rendered", "tiles": resolved["tiles"]}]
    elif kind == "xyz":
        # The MAP CONVERTER route for an XYZ layer: the shared pool fills
        # first, then each pool tile becomes an ingest source. Site Analysis
        # takes the pool tiles as terrain directly and never ingests them, so
        # the two tabs really are two paths here — which is exactly why the
        # `both_tabs` check compares them rather than assuming.
        pool = {}
        for res in sorted({r for r, _t in expected}):
            subtiles = [t for r, t in expected if r == res]
            pool[res] = adapter.ensure_pool_tiles(resolved["uri"], subtiles, res)
        entries = [{"kind": "xyz", "pool": pool}]
    else:
        raise RuntimeError(f"unroutable source kind {kind!r}")

    jobs, temps = mc._build_tile_jobs(expected, str(out), True, entries, None, str(tmp))
    made = []
    for job in jobs:
        job_file = tmp / (Path(job["output_path"]).stem + ".json")
        job_file.write_text(json.dumps(job), encoding="utf-8")
        code, said = pipe.engine("aether_converter",
                                 ["ingest", "--job-file", job_file], timeout)
        if code != 0:
            raise RuntimeError(f"ingest exit {code}: {_tail(said, 260)}")
        made.append(job["output_path"])
    for temp in temps:
        try:
            os.remove(temp)
        except OSError:
            pass
    return made, kind, f"{len(made)} ingested tile(s)"


def _acquire_site_analysis(row, layer, plugin, lat, lon, resolution, range_km,
                           buildings: Optional[str] = None):
    """Terrain the Site Analysis way: ``terrain_adapter.prepare_terrain``.

    That is the public entry point all four of the plugin's own callers use
    (both GUI tabs and both Processing algorithms), and it is the only route
    that burns buildings into the surface. Returns the .abt directory.
    """
    adapter = plugin["adapter"]
    return adapter.prepare_terrain(
        dem_layer=layer, tx_lat=lat, tx_lon=lon, max_range_km=range_km,
        resolution_m=resolution, binary_manager=adapter._DefaultBinaryManager(),
        buildings_file=buildings)


def _agree(a_dir, b_dir, plugin) -> Tuple[bool, str]:
    """Do two terrain directories hold the same elevations?

    The checklist's own rule: every case runs through Site Analysis AND the
    Map Converter, and "a divergence between them is a bug by definition".
    Nothing else in this suite has ever compared the two.
    """
    import numpy as np
    abt_mod = plugin["abt"]
    a = {Path(p).name: p for p in abt_mod.list_tiles(str(a_dir))}
    b = {Path(p).name: p for p in abt_mod.list_tiles(str(b_dir))}
    shared = sorted(set(a) & set(b))
    if not shared:
        return False, (f"no tile in common: Site Analysis wrote {sorted(a)[:2]}, "
                       f"the Map Converter wrote {sorted(b)[:2]}")
    worst, n = 0.0, 0
    for name in shared:
        ga = abt_mod.read_tile(abt_mod.read_header(a[name]))
        gb = abt_mod.read_tile(abt_mod.read_header(b[name]))
        if ga is None or gb is None or ga.shape != gb.shape:
            return False, f"{name}: unreadable or different shapes"
        mask = _real_mask(ga, plugin) & _real_mask(gb, plugin)
        if not mask.any():
            continue
        n += int(mask.sum())
        worst = max(worst, float(np.abs(ga[mask].astype("int32")
                                        - gb[mask].astype("int32")).max()))
    worst_m = worst * abt_mod.ELEV_STEP_M
    if not n:
        return False, "the two paths share no real sample to compare"
    if worst_m > 0.5:
        return False, (f"the two tabs disagree by up to {worst_m:,.1f} m over "
                       f"{n:,} shared samples")
    return True, f"both tabs agree to {worst_m:,.1f} m over {n:,} samples"


def _coverage(plugin, pipe: Pipeline, abt_dir: Path, lat: float, lon: float,
              resolution: int, range_km: int, name: str, timeout: int,
              buildings: Optional[str] = None):
    """A real coverage run + export. ``(rc, said, geotiff or None)``."""
    params = plugin["CoverageParams"](
        tx_lat=lat, tx_lon=lon, tx_height=30.0, tx_mode="AGL",
        freq_mhz=900.0, erp_watts=10.0, rx_height=2.0, rx_mode="AGL",
        model="ITM", resolution_m=resolution, max_range_km=range_km,
        backend="CPU", output_name=name, max_ram_gb=8, max_vram_gb=4)
    job = plugin["build_coverage_job"](params, str(abt_dir), str(pipe.scratch))
    if buildings:
        job["processing"]["buildings_file"] = buildings
    job_file = pipe.scratch / f"{name}_job.json"
    job_file.write_text(json.dumps(job, indent=1), encoding="utf-8")
    code, said = pipe.engine("aether_core", ["--config", job_file], timeout)
    if code != 0:
        return code, said, None
    result = next((pipe.scratch / f"{name}{ext}" for ext in (".tiles", ".bit")
                   if (pipe.scratch / f"{name}{ext}").exists()), None)
    sidecar = pipe.scratch / f"{name}.json"
    if result is None or not sidecar.exists():
        return code, "aether_core exited 0 but wrote no result file", None
    tif = pipe.scratch / f"{name}.tif"
    code, said = pipe.engine("aether_export",
                             ["-i", result, "-j", sidecar, "-o", tif], timeout)
    return code, said, (tif if code == 0 and tif.exists() else None)


def coverage_report(path: Path) -> Dict[str, Any]:
    """What the exported coverage GeoTIFF holds."""
    import numpy as np
    from osgeo import gdal
    gdal.UseExceptions()
    dataset = gdal.Open(str(path))
    band = dataset.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    values = band.ReadAsArray().astype("float64")
    width, height = dataset.RasterXSize, dataset.RasterYSize
    mask = np.isfinite(values)
    if nodata is not None:
        mask &= values != nodata
    count = int(mask.sum())
    out = {"px": f"{width}x{height}", "valid": count,
           "valid_pct": round(100.0 * count / (width * height), 1)}
    if count:
        out["min"] = round(float(values[mask].min()), 1)
        out["max"] = round(float(values[mask].max()), 1)
        out["mean"] = round(float(values[mask].mean()), 2)
    dataset = None
    return out


#: Rows whose catalogue ``check`` says the pipeline must NOT produce terrain.
_MUST_FAIL = ("error",)
#: Rows the plugin's own router must refuse before any terrain is fetched.
_MUST_REJECT = ("reject",)


def tier_pipeline(manifest: Dict[str, Any], results: Results, only: Sequence[str],
                  timeout: int, keep: bool) -> None:
    """Run the WHOLE pipeline for every catalogue row, one clean pool each.

    This is the tier that answers the only question that matters: put this
    layer in, and does real terrain and a real coverage come out? Everything
    tier A checks is about the layer; everything here is about the product.
    """
    binaries = {}
    results.plan("-", "engine.present")
    for name in ("aether_converter", "aether_core", "aether_export"):
        found = find_binary(name)
        if found is None:
            results.add("-", "engine.present", "engine binaries", SKIP,
                        f"{name} not found (set AETHER_BIN_DIR) — every pipeline check "
                        f"is a hole in this run, not a pass")
            return
        binaries[name] = found
    results.add("-", "engine.present", "engine binaries", PASS,
                ", ".join(sorted(p.name for p in binaries.values())))

    plugin = _plugin_modules()
    _check_void_sentinel(plugin, results)
    project = open_project(manifest, results)
    if project is None:
        return
    layers = {l.name(): l for l in project.mapLayers().values()}
    out_dir = Path(manifest["__manifest_dir__"])
    scratch_root = out_dir / ".pipeline"
    shutil.rmtree(scratch_root, ignore_errors=True)
    scratch_root.mkdir(parents=True, exist_ok=True)

    rows = [r for r in manifest["rows"] if row_selected(r["row"], only)]
    for row in rows:
        results.plan(row["row"], "pipeline")
    try:
        for row in rows:
            scratch = scratch_root / row["row"].replace(".", "_")
            scratch.mkdir(parents=True, exist_ok=True)
            try:
                with Pipeline(scratch, binaries, keep) as pipe:
                    _pipeline_row(row, layers.get(row["name"]), plugin, pipe,
                                  manifest, results, timeout)
            except Exception as exc:
                results.add(row["row"], "pipeline", "the row's pipeline ran to completion",
                            FAIL, f"the check itself raised {type(exc).__name__}: {exc}")
        _tier_pipeline_combinations(manifest, results, layers, plugin, binaries,
                                    scratch_root, timeout, keep)
    finally:
        if keep:
            results.note(f"pipeline scratch kept at {scratch_root}")
        else:
            shutil.rmtree(scratch_root, ignore_errors=True)


def _check_agrees_with_reference(row, rid, reference_row, abt_dir, tolerance,
                                 plugin, pipe, manifest, results, timeout) -> None:
    """Does this row's terrain match the reference source over the same ground?"""
    import numpy as np
    abt_mod = plugin["abt"]
    by_row = {r["row"]: r for r in manifest["rows"]}
    ref = by_row.get(reference_row)
    layers = _project_layers()
    if ref is None or layers.get(ref["name"]) is None:
        results.add(rid, "pipeline.agrees",
                    f"agrees with {reference_row} over the same ground", SKIP,
                    f"the catalogue's reference row {reference_row} is not in the project")
        return
    probe = manifest.get("elevation_probe") or {}
    lat, lon = float(probe.get("lat", 46.945)), float(probe.get("lon", 7.41))
    adapter = plugin["adapter"]
    bbox = adapter.analysis_bbox(lat, lon, int(row.get("pipeline_range_km") or 3))
    ref_paths, _kind, _note = _acquire(ref, layers[ref["name"]], plugin, pipe, bbox,
                                       [int(row.get("pipeline_res_m") or 30)], timeout)
    ref_by_name = {Path(p).name: p for p in ref_paths}
    worst, samples = 0.0, 0
    for path in abt_mod.list_tiles(str(abt_dir)):
        other = ref_by_name.get(Path(path).name)
        if other is None:
            continue
        mine = abt_mod.read_tile(abt_mod.read_header(path))
        theirs = abt_mod.read_tile(abt_mod.read_header(other))
        if mine is None or theirs is None or mine.shape != theirs.shape:
            continue
        mask = _real_mask(mine, plugin) & _real_mask(theirs, plugin)
        if not mask.any():
            continue
        diff = np.abs(mine[mask].astype("int32") - theirs[mask].astype("int32"))
        samples += int(mask.sum())
        worst = max(worst, float(np.mean(diff)) * abt_mod.ELEV_STEP_M)
    if not samples:
        results.add(rid, "pipeline.agrees",
                    f"agrees with {reference_row} over the same ground", SKIP,
                    "no tile in common with the reference source")
        return
    ok = worst <= tolerance
    results.add(rid, "pipeline.agrees",
                f"agrees with {reference_row} over the same ground",
                PASS if ok else FAIL,
                f"mean |difference| {worst:,.1f} m over {samples:,} shared samples"
                + ("" if ok else f", the catalogue allows {tolerance:,.0f} m — this "
                                 f"source's terrain is wrong, not merely different"),
                known_fail_for(row, "pipeline.agrees"))


def _check_nothing_cached(row, rid, plugin, results: Results) -> None:
    """After a refusal, the pool must hold nothing a later run would reuse.

    Row 1.6's contract in full is "hard error, NOTHING CACHED, no flat-0
    terrain". The hard error is the easy half. The other half is what turns a
    one-off failure into a permanent one: a failed download leaves a full-size
    all-zero .abt in the pool, and every run after it reports a cache hit and
    produces a confident coverage over flat sea.
    """
    adapter = plugin["adapter"]
    abt_mod = plugin["abt"]
    results.plan(rid, "pipeline.nothing_cached")
    try:
        pool = adapter._pool_dir(row["source"])
    except Exception as exc:
        results.add(rid, "pipeline.nothing_cached", "the refusal cached nothing reusable",
                    FAIL, f"the pool directory could not be resolved: {exc}")
        return
    left = abt_mod.list_tiles(pool) if os.path.isdir(pool) else []
    reusable = []
    for path in left:
        grid = abt_mod.read_tile(abt_mod.read_header(path))
        if grid is None:
            continue
        real = _real_mask(grid, plugin)
        reusable.append((Path(path).name, int(real.sum()), int(grid.size)))
    if not reusable:
        results.add(rid, "pipeline.nothing_cached", "the refusal cached nothing reusable",
                    PASS, f"{len(left)} file(s) left in the pool, none of them usable terrain",
                    known_fail_for(row, "pipeline.nothing_cached"))
        return
    name, real, total = reusable[0]
    results.add(rid, "pipeline.nothing_cached", "the refusal cached nothing reusable",
                FAIL,
                f"the failed run left {name} in the pool ({real:,}/{total:,} samples the "
                f"plugin calls terrain). The next run of this layer reports a cache hit, "
                f"skips the download and builds a coverage over it",
                known_fail_for(row, "pipeline.nothing_cached"))


def _check_void_sentinel(plugin, results: Results) -> None:
    """The plugin's "is this terrain?" rule must reject the engine's own VOID.

    ``aether_converter`` writes -9999 COUNTS (-4999.5 m) for a pixel no source
    covered. The plugin's validity floor is ``MIN_VALID_ELEV_M = -5000.0 m``,
    i.e. -10000 counts — one count BELOW the sentinel — so ``src >
    MIN_VALID_ELEV_M`` (abt.paste_tile, raster_tools) calls every hole valid
    ground 5 km down instead of no-data. Nothing in the suite looked at the two
    numbers together, and they are one count apart.
    """
    abt_mod = plugin["abt"]
    results.plan("-", "void.sentinel")
    void_m = CONVERTER_VOID_COUNTS * abt_mod.ELEV_STEP_M
    rejected = void_m <= abt_mod.MIN_VALID_ELEV_M
    results.add("-", "void.sentinel",
                "the plugin's validity floor rejects the engine's VOID",
                PASS if rejected else FAIL,
                f"VOID is {CONVERTER_VOID_COUNTS} counts ({void_m:,.1f} m) and "
                f"MIN_VALID_ELEV_M is {abt_mod.MIN_VALID_ELEV_M:,.1f} m"
                + ("" if rejected else
                   " — one count too low, so every uncovered pixel is pasted into a "
                   "mosaic and into the min-altitude surface as terrain at "
                   f"{void_m:,.1f} m instead of no-data"))


def _pipeline_row(row, layer, plugin, pipe: Pipeline, manifest, results,
                  timeout: int) -> None:
    """Acquire → ingest/download → .abt → coverage → export, for one row."""
    adapter = plugin["adapter"]
    rid = row["row"]
    if layer is None:
        results.add(rid, "pipeline", "terrain out of this layer", FAIL,
                    "the layer is not in the project, so nothing can be run through it")
        return

    if row.get("kind") == "vector":
        _pipeline_vector_row(row, layer, plugin, pipe, manifest, results, timeout)
        return
    resolution = int(row.get("pipeline_res_m") or 30)
    range_km = int(row.get("pipeline_range_km") or 3)
    probe = manifest.get("elevation_probe") or {}
    box = row.get("footprint") or []
    lat, lon = ((box[1] + box[3]) / 2.0, (box[0] + box[2]) / 2.0) if len(box) == 4 else (
        float(probe.get("lat", 46.945)), float(probe.get("lon", 7.41)))
    bbox = adapter.analysis_bbox(lat, lon, range_km)

    must_fail = row.get("check") in _MUST_FAIL
    must_reject = row.get("check") in _MUST_REJECT

    try:
        paths, kind, note = _acquire(row, layer, plugin, pipe, bbox,
                                     [resolution], timeout)
    except Exception as exc:
        said = " ".join(str(exc).split())
        if must_reject:
            results.add(rid, "pipeline", "the plugin refuses to run this as terrain",
                        PASS, _shorten(said, 110), known_fail_for(row, "pipeline"))
        elif must_fail:
            ok, why = _refusal_is_the_right_one(row, said, row["source"], 1)
            results.add(rid, "pipeline", "the pipeline fails, loudly and for the right reason",
                        PASS if ok else FAIL, why, known_fail_for(row, "pipeline"))
            _check_nothing_cached(row, rid, plugin, results)
        else:
            results.add(rid, "pipeline", "terrain out of this layer", FAIL,
                        _tail(said, 240), known_fail_for(row, "pipeline"))
        return

    if must_reject:
        results.add(rid, "pipeline", "the plugin refuses to run this as terrain", FAIL,
                    f"it did not refuse — {note} were produced from a layer the catalogue "
                    f"says must never be offered as terrain",
                    known_fail_for(row, "pipeline"))
        return
    if must_fail:
        results.add(rid, "pipeline", "the pipeline fails, loudly and for the right reason",
                    FAIL, f"it succeeded — {note}. The catalogue says this source must be "
                          f"refused, and terrain nobody can trust is worse than none",
                    known_fail_for(row, "pipeline"))
        return

    report = abt_report(paths, plugin)
    if "unreadable" in report:
        results.add(rid, "pipeline", "terrain out of this layer", FAIL,
                    f"the engine wrote {report['unreadable']} and the plugin cannot read it",
                    known_fail_for(row, "pipeline"))
        return
    if not report["real_px"]:
        results.add(rid, "pipeline", "terrain out of this layer", FAIL,
                    f"{note}, and every sample is VOID — the source contributed nothing "
                    f"and the engine still exited 0", known_fail_for(row, "pipeline"))
        return
    if report["min_m"] == report["max_m"]:
        results.add(rid, "pipeline", "terrain out of this layer", FAIL,
                    f"{note}, every real sample is {report['min_m']:,.1f} m — a constant "
                    f"tile is what a silently-failed source writes, not terrain",
                    known_fail_for(row, "pipeline"))
        return

    at = abt_at(report["grids"], lat, lon, plugin)
    band = row.get("expect_elev_m") or []
    detail = (f"{kind}: {note}, {report['real_px']:,}/{report['total_px']:,} real, "
              f"{report['min_m']:,.1f}..{report['max_m']:,.1f} m")
    if at is not None:
        detail += f", {at:,.1f} m at {lat:.4f},{lon:.4f}"
    if len(band) == 2 and at is not None and not (float(band[0]) <= at <= float(band[1])):
        results.add(rid, "pipeline", "terrain out of this layer", FAIL,
                    detail + f" — outside the plausible {float(band[0]):,.0f}.."
                             f"{float(band[1]):,.0f} m for this probe",
                    known_fail_for(row, "pipeline"))
        return

    abt_dir = pipe.scratch / "terrain"
    abt_dir.mkdir(exist_ok=True)
    for path in paths:
        target = abt_dir / Path(path).name
        if not target.exists():
            try:
                os.link(path, target)
            except OSError:
                target.write_bytes(Path(path).read_bytes())

    # The other tab. The catalogue runs every `check: both` row through Site
    # Analysis AND the Map Converter, and its own header says a divergence
    # between them is a bug by definition — so the two are built and compared,
    # not just each declared to work on its own.
    if row.get("check") in ("both", "buildings"):
        results.plan(rid, "pipeline.both_tabs")
        try:
            sa_dir = _acquire_site_analysis(row, layer, plugin, lat, lon,
                                            resolution, range_km)
            same, why = _agree(sa_dir, abt_dir, plugin)
            results.add(rid, "pipeline.both_tabs",
                        "Site Analysis and the Map Converter build the same terrain",
                        PASS if same else FAIL, why,
                        known_fail_for(row, "pipeline.both_tabs"))
        except Exception as exc:
            results.add(rid, "pipeline.both_tabs",
                        "Site Analysis and the Map Converter build the same terrain",
                        FAIL, f"the Site Analysis path failed where the Map Converter "
                              f"succeeded: {type(exc).__name__}: "
                              f"{' '.join(str(exc).split())[:120]}",
                        known_fail_for(row, "pipeline.both_tabs"))
    # Against the catalogue's reference source, over the same ground. Two
    # independent DEMs of the same place agree to a few metres; a source an
    # order of magnitude past that is not a dataset difference, it is a fault
    # somewhere in acquisition — and it is invisible to every other check here,
    # because wrong-but-plausible terrain produces a wrong-but-plausible
    # coverage.
    tolerance = float(row.get("expect_agrees_m") or 0.0)
    reference_row = manifest.get("reference_row")
    if tolerance and reference_row and rid != reference_row:
        results.plan(rid, "pipeline.agrees")
        _check_agrees_with_reference(row, rid, reference_row, abt_dir, tolerance,
                                     plugin, pipe, manifest, results, timeout)

    name = f"cov_{rid.replace('.', '_')}"
    code, said, tif = _coverage(plugin, pipe, abt_dir, lat, lon, resolution,
                                range_km, name, timeout)
    if tif is None:
        results.add(rid, "pipeline", "terrain out of this layer, and a coverage over it",
                    FAIL, detail + f" — but the run failed (exit {code}): {_tail(said, 170)}",
                    known_fail_for(row, "pipeline"))
        return
    cov = coverage_report(tif)
    if not cov.get("valid"):
        results.add(rid, "pipeline", "terrain out of this layer, and a coverage over it",
                    FAIL, detail + " — the coverage exported with no valid pixel in it",
                    known_fail_for(row, "pipeline"))
        return
    results.add(rid, "pipeline", "terrain out of this layer, and a coverage over it",
                PASS, detail + f" | coverage {cov['px']} {cov['valid_pct']}% valid, "
                               f"{cov['min']}..{cov['max']} dB",
                known_fail_for(row, "pipeline"))


#: The Map Converter stack combinations the catalogue implies but no single
#: row can express. Each names the ROLE a row must play; the rows themselves
#: are picked from the manifest, so this list never goes stale against it.
_COMBINATIONS = (
    {"name": "files + files (priority order)", "roles": ("dem", "dem2"),
     "resolutions": (30,),
     "asserts": "priority"},
    {"name": "xyz under files (mixed acquisition)", "roles": ("xyz", "dem"),
     "resolutions": (30,),
     "asserts": "terrain"},
    {"name": "files under xyz (reversed priority)", "roles": ("dem", "xyz"),
     "resolutions": (30,),
     "asserts": "terrain"},
    {"name": "rendered service under files", "roles": ("rendered", "dem"),
     "resolutions": (30,),
     "asserts": "terrain"},
    {"name": "two resolutions in one run", "roles": ("dem",),
     "resolutions": (30, 90),
     "asserts": "multires"},
    {"name": "files + buildings", "roles": ("dem",), "buildings": True,
     "resolutions": (30,),
     "asserts": "terrain"},
    {"name": "rerun with overwrite off", "roles": ("dem",),
     "resolutions": (30,),
     "asserts": "skip_existing"},
)


def _pick_roles(manifest: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """One manifest row per role the combination matrix needs."""
    rows = manifest["rows"]
    probe = manifest.get("elevation_probe") or {}
    lat, lon = float(probe.get("lat", 46.945)), float(probe.get("lon", 7.41))

    def over_probe(row) -> bool:
        box = row.get("footprint") or []
        return (len(box) == 4 and box[0] <= lon <= box[2] and box[1] <= lat <= box[3])

    dems = [r for r in rows if r["provider"] == "gdal" and r.get("check") == "both"
            and over_probe(r)]
    xyz = [r for r in rows if "type=xyz" in r["source"] and r.get("check") == "both"
           and r.get("expect_class") == "dem"]
    # A rendered ELEVATION service. An imagery basemap is deliberately excluded:
    # the catalogue marks those `check: reject`, and whether the plugin refuses
    # them is asserted per row, not here.
    rendered = [r for r in rows if r["provider"] in ("wms", "wcs")
                and "type=xyz" not in r["source"]
                and r.get("check") not in ("reject",)
                and r.get("expect_class") != "imagery"]
    buildings = [r for r in rows if r.get("check") == "buildings"]
    picked = {}
    if dems:
        picked["dem"] = dems[0]
    if len(dems) > 1:
        picked["dem2"] = dems[1]
    if xyz:
        picked["xyz"] = xyz[0]
    if rendered:
        picked["rendered"] = rendered[0]
    if buildings:
        picked["buildings"] = buildings[0]
    return picked


def _entries_for_roles(roles, picked, layers, plugin, pipe, bbox, resolutions,
                       timeout):
    """Resolved stack entries, in priority order, through the plugin's router."""
    mc = plugin["mc"]
    adapter = plugin["adapter"]
    expected = mc._enumerate_tiles(mc._snap_bbox(bbox, list(resolutions)),
                                   list(resolutions))
    entries = []
    for role in roles:
        row = picked[role]
        layer = layers.get(row["name"])
        entry = _layer_entry(row, layer, plugin, resolutions, bbox)
        resolved = mc._resolve_source_on_main_thread(entry, expected)
        kind = resolved["kind"]
        if kind == "file":
            info = adapter.source_file_info(resolved["path"], row.get("crs") or None)
            entries.append({"kind": "files", "infos": [info]})
        elif kind == "rendered":
            entries.append({"kind": "rendered", "tiles": resolved["tiles"]})
        elif kind == "xyz":
            pool = {}
            for res in sorted({r for r, _t in expected}):
                subtiles = [t for r, t in expected if r == res]
                pool[res] = adapter.ensure_pool_tiles(resolved["uri"], subtiles, res)
            entries.append({"kind": "xyz", "pool": pool})
        else:
            raise RuntimeError(f"unroutable role {role!r} ({kind})")
    return expected, entries


def _tier_pipeline_combinations(manifest, results, layers, plugin, binaries,
                                scratch_root: Path, timeout: int,
                                keep: bool = False) -> None:
    """Run the Map Converter over STACKS, not just single layers.

    A converter that handles every source in isolation and drops the second
    one, or takes them in the wrong priority order, or builds only the first
    of two requested resolutions, passes every per-row check in this file.
    These are the cases only a stack can express.
    """
    adapter = plugin["adapter"]
    mc = plugin["mc"]
    picked = _pick_roles(manifest)
    probe = manifest.get("elevation_probe") or {}
    lat, lon = float(probe.get("lat", 46.945)), float(probe.get("lon", 7.41))

    for combo in _COMBINATIONS:
        label = combo["name"]
        check_id = f"mc:{label}"
        results.plan("mc", check_id)
        missing = [r for r in combo["roles"] if r not in picked]
        if combo.get("buildings") and "buildings" not in picked:
            missing.append("buildings")
        if missing:
            results.add("mc", check_id, label, SKIP,
                        f"the catalogue has no row to play {', '.join(missing)}")
            continue
        scratch = scratch_root / ("mc_" + re.sub(r"[^a-z0-9]+", "_", label.lower()))
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            with Pipeline(scratch, binaries, keep) as pipe:
                _run_combination(combo, label, check_id, picked, layers, plugin,
                                 pipe, adapter, mc, lat, lon, results, timeout)
        except Exception as exc:
            results.add("mc", check_id, label, FAIL,
                        f"{type(exc).__name__}: {' '.join(str(exc).split())[:170]}")


def _run_combination(combo, label, check_id, picked, layers, plugin, pipe,
                     adapter, mc, lat, lon, results, timeout) -> None:
    resolutions = list(combo["resolutions"])
    bbox = adapter.analysis_bbox(lat, lon, 3)
    expected, entries = _entries_for_roles(combo["roles"], picked, layers, plugin,
                                           pipe, bbox, resolutions, timeout)
    out = pipe.scratch / "abt"; out.mkdir(parents=True, exist_ok=True)
    tmp = pipe.scratch / "src"; tmp.mkdir(parents=True, exist_ok=True)
    buildings = None
    if combo.get("buildings"):
        row = picked["buildings"]
        layer = layers.get(row["name"])
        entry = _layer_entry(row, layer, plugin, resolutions, bbox)
        buildings = mc._resolve_source_on_main_thread(entry, expected)

    jobs, temps = mc._build_tile_jobs(expected, str(out), True, entries,
                                      buildings, str(tmp))
    if not jobs:
        results.add("mc", check_id, label, FAIL,
                    "the plugin produced no ingest job for this stack")
        return
    stacked = max(len(j["sources"]) for j in jobs)
    if stacked < len(entries):
        results.add("mc", check_id, label, FAIL,
                    f"{len(entries)} entries in the stack but at most {stacked} reached "
                    f"the converter — an entry was dropped before ingest")
        return
    for job in jobs:
        job_file = tmp / (Path(job["output_path"]).stem + ".json")
        job_file.write_text(json.dumps(job), encoding="utf-8")
        code, said = pipe.engine("aether_converter",
                                 ["ingest", "--job-file", job_file], timeout)
        if code != 0:
            results.add("mc", check_id, label, FAIL,
                        f"ingest exit {code}: {_tail(said, 200)}")
            return
    for temp in temps:
        try:
            os.remove(temp)
        except OSError:
            pass

    produced = sorted(Path(j["output_path"]).name for j in jobs)
    report = abt_report([j["output_path"] for j in jobs], plugin)
    if not report.get("real_px"):
        results.add("mc", check_id, label, FAIL,
                    f"{len(jobs)} tile(s) written and every sample is VOID")
        return
    detail = (f"{len(entries)} entries -> {len(jobs)} tile(s), "
              f"{report['min_m']:,.1f}..{report['max_m']:,.1f} m")

    if combo["asserts"] == "multires":
        wanted = {res for res, _t in expected}
        got = {int(n.rsplit('_', 1)[1].split('m')[0]) for n in produced
               if '_' in n and 'm' in n.rsplit('_', 1)[1]}
        if got != wanted:
            results.add("mc", check_id, label, FAIL,
                        f"asked for {sorted(wanted)} m, the run produced {sorted(got)} m")
            return
        detail += f", resolutions {sorted(got)} m"
    elif combo["asserts"] == "priority":
        # Entry 0 is highest priority: the converter takes the first valid
        # sample per pixel, so the result must follow the FIRST source where
        # it has data — not the second, and not a blend.
        first = picked[combo["roles"][0]]
        alone = _single_source_abt(first, layers, plugin, pipe, adapter, mc,
                                   bbox, resolutions, timeout, "first")
        if alone is None:
            results.add("mc", check_id, label, SKIP,
                        "the priority reference tile could not be built")
            return
        same, why = _same_terrain(alone, [j["output_path"] for j in jobs], plugin)
        if not same:
            results.add("mc", check_id, label, FAIL,
                        f"the stack does not follow its highest-priority source: {why}")
            return
        detail += f", follows {first['row']} (highest priority) — {why}"
    elif combo["asserts"] == "skip_existing":
        again, _t = mc._build_tile_jobs(expected, str(out), False, entries, None, str(tmp))
        if again:
            results.add("mc", check_id, label, FAIL,
                        f"overwrite=False still produced {len(again)} job(s) for tiles "
                        f"that already exist — every rerun rebuilds the whole set")
            return
        detail += ", rerun with overwrite=False built nothing again"
    results.add("mc", check_id, label, PASS, detail)


def _single_source_abt(row, layers, plugin, pipe, adapter, mc, bbox, resolutions,
                       timeout, tag):
    """The same tile built from ONE source, as the priority reference."""
    layer = layers.get(row["name"])
    if layer is None:
        return None
    expected = mc._enumerate_tiles(mc._snap_bbox(bbox, list(resolutions)),
                                   list(resolutions))
    entry = _layer_entry(row, layer, plugin, resolutions, bbox)
    resolved = mc._resolve_source_on_main_thread(entry, expected)
    if resolved.get("kind") != "file":
        return None
    info = adapter.source_file_info(resolved["path"], row.get("crs") or None)
    out = pipe.scratch / tag; out.mkdir(parents=True, exist_ok=True)
    tmp = pipe.scratch / (tag + "_src"); tmp.mkdir(parents=True, exist_ok=True)
    jobs, temps = mc._build_tile_jobs(expected, str(out), True,
                                      [{"kind": "files", "infos": [info]}], None, str(tmp))
    made = []
    for job in jobs:
        job_file = tmp / (Path(job["output_path"]).stem + ".json")
        job_file.write_text(json.dumps(job), encoding="utf-8")
        code, _said = pipe.engine("aether_converter",
                                  ["ingest", "--job-file", job_file], timeout)
        if code != 0:
            return None
        made.append(job["output_path"])
    for temp in temps:
        try:
            os.remove(temp)
        except OSError:
            pass
    return made


def _same_terrain(reference: Sequence[str], produced: Sequence[str], plugin):
    """Do the stack's tiles follow the reference where the reference has data?"""
    import numpy as np
    abt_mod = plugin["abt"]
    by_name = {Path(p).name: p for p in produced}
    checked = 0
    worst = 0.0
    for path in reference:
        other = by_name.get(Path(path).name)
        if other is None:
            return False, f"the stack produced no {Path(path).name}"
        a = abt_mod.read_tile(abt_mod.read_header(str(path)))
        b = abt_mod.read_tile(abt_mod.read_header(str(other)))
        if a is None or b is None:
            return False, "a tile could not be read back"
        mask = _real_mask(a, plugin)
        n = int(mask.sum())
        if not n:
            continue
        diff = np.abs(a[mask].astype("int32") - b[mask].astype("int32"))
        checked += n
        worst = max(worst, float(diff.max()) * abt_mod.ELEV_STEP_M)
    if not checked:
        return False, "the reference source has no real samples to compare"
    if worst > 0.0:
        return False, (f"differs from the highest-priority source by up to {worst:,.1f} m "
                       f"over {checked:,} samples it covers")
    return True, f"identical over the {checked:,} samples it covers"


def _reference_terrain(manifest, layers, plugin, pipe, results, timeout):
    """A known-good .abt directory over the reference AOI, for the rows that
    are not themselves terrain (buildings, sites, links).

    Built from the catalogue's own first ``check: both`` local DEM over the
    probe, so a buildings or P2P failure can never be blamed on the terrain.
    """
    adapter = plugin["adapter"]
    picked = _pick_roles(manifest)
    row = picked.get("dem")
    if row is None:
        return None, None
    probe = manifest.get("elevation_probe") or {}
    lat, lon = float(probe.get("lat", 46.945)), float(probe.get("lon", 7.41))
    bbox = adapter.analysis_bbox(lat, lon, 3)
    paths, _kind, _note = _acquire(row, layers.get(row["name"]), plugin, pipe,
                                   bbox, [30], timeout)
    terrain = pipe.scratch / "reference_terrain"
    terrain.mkdir(exist_ok=True)
    for path in paths:
        target = terrain / Path(path).name
        if not target.exists():
            try:
                os.link(path, target)
            except OSError:
                target.write_bytes(Path(path).read_bytes())
    return terrain, (lat, lon)


def _pipeline_vector_row(row, layer, plugin, pipe, manifest, results, timeout):
    """The pipeline role of a row that is not itself a terrain source.

    Every §3/§5 layer feeds a real run: buildings become a ``buildings_file``
    on a coverage, sites become transmitters, links become P2P jobs, and the
    reference geometry is cross-checked against what the engine is actually
    planned over. "It has features" is not a pipeline test.
    """
    rid = row["row"]
    if layer is None:
        results.add(rid, "pipeline", "this layer drives a real run", FAIL,
                    "the layer is not in the project")
        return
    if row.get("check") == "buildings":
        _pipeline_buildings(row, layer, plugin, pipe, manifest, results, timeout)
    elif rid.startswith("5.2"):
        _pipeline_sites(row, layer, plugin, pipe, manifest, results, timeout)
    elif rid.startswith("5.3"):
        _pipeline_links(row, layer, plugin, pipe, manifest, results, timeout)
    elif rid.startswith("5.1"):
        _pipeline_run_matrix(row, layer, manifest, results)
    elif rid.startswith("5.0"):
        _pipeline_footprints(row, layer, plugin, manifest, results)
    else:
        results.add(rid, "pipeline", "this layer drives a real run", FAIL,
                    f"no pipeline role is defined for check={row.get('check')!r}")


def _pipeline_buildings(row, layer, plugin, pipe, manifest, results, timeout):
    """Buildings must CHANGE the terrain surface, not merely be accepted.

    They reach the engine at INGEST — ``prepare_terrain(buildings_file=…)``
    burns them into the surface — never as a field on a coverage job. Asking
    for a coverage difference without that is a test of nothing, which is what
    an earlier version of this check was.
    """
    picked = _pick_roles(manifest)
    dem_row = picked.get("dem")
    if dem_row is None:
        results.add(row["row"], "pipeline", "buildings change the terrain", SKIP,
                    "the catalogue has no local DEM to burn them into")
        return
    layers = _project_layers()
    dem_layer = layers.get(dem_row["name"])
    probe = manifest.get("elevation_probe") or {}
    lat, lon = float(probe.get("lat", 46.945)), float(probe.get("lon", 7.41))
    source = layer.source()
    plain = source.partition("|")[0]
    if not os.path.exists(plain):
        results.add(row["row"], "pipeline", "buildings change the terrain", FAIL,
                    f"the buildings source does not exist: {plain}")
        return
    bare = _acquire_site_analysis(dem_row, dem_layer, plugin, lat, lon, 30, 3)
    built = _acquire_site_analysis(dem_row, dem_layer, plugin, lat, lon, 30, 3,
                                   buildings=source)
    same, why = _agree(bare, built, plugin)
    detail = f"burning {Path(plain).name} into {dem_row['row']} "
    if same:
        # Say what the source carries, so a reader can tell a plugin defect
        # from a fixture that has nothing to burn. The converter's ladder is
        # absolute roof Z, then an explicit height/render_height, then a
        # guess — a 2D polygon with no heights may legitimately raise nothing.
        detail += (f"changed nothing. The cache identity still claims buildings were "
                   f"applied, and no warning was raised. {_buildings_shape(plain)}")
    else:
        detail += f"raised the surface ({why.split(' over ')[0]})"
    results.add(row["row"], "pipeline", "buildings change the terrain",
                FAIL if same else PASS, detail, known_fail_for(row, "pipeline"))


def _buildings_shape(path: str) -> str:
    """What a buildings source actually carries, for the failure message."""
    try:
        from osgeo import ogr
        source = ogr.Open(path)
        layer = source.GetLayer(0)
        names = {layer.GetLayerDefn().GetFieldDefn(i).GetName()
                 for i in range(layer.GetLayerDefn().GetFieldCount())}
        total = layer.GetFeatureCount()
        heighted = sum(1 for f in layer
                       if any(f.GetField(n) not in (None, "") for n in
                              ("height", "render_height") if n in names))
        has_z = ogr.GT_HasZ(layer.GetGeomType())
        return (f"Source: {total:,} features, {heighted:,} with a height attribute, "
                f"geometry {'has' if has_z else 'has NO'} Z (absolute roof elevation).")
    except Exception as exc:
        return f"(the source could not be inspected: {type(exc).__name__})"


def _pipeline_sites(row, layer, plugin, pipe, manifest, results, timeout):
    """Every site the reference terrain covers must work as a transmitter.

    The catalogue scatters sites deliberately — Bern, the Dead Sea, the Andes,
    Fiji — so a site outside this terrain is not a failure, it is a site with
    its own ground. It is counted and named, never silently dropped.
    """
    terrain, _centre = _reference_terrain(manifest, _project_layers(), plugin,
                                          pipe, results, timeout)
    if terrain is None:
        results.add(row["row"], "pipeline", "every site runs as a transmitter", SKIP,
                    "no reference terrain")
        return
    covered = _terrain_bbox(terrain, plugin)
    ran, failed, elsewhere = 0, [], 0
    names = layer.fields().names()
    for index, feature in enumerate(layer.getFeatures()):
        point = feature.geometry().asPoint()
        label = str(feature["name"]) if "name" in names else f"site {index}"
        if not _inside(covered, point.y(), point.x()):
            elsewhere += 1
            continue
        code, said, tif = _coverage(plugin, pipe, terrain, point.y(), point.x(),
                                    30, 3, f"site_{index}", timeout)
        if tif is None:
            failed.append(f"{label}: exit {code} {_tail(said, 150)}")
        else:
            ran += 1
    detail = f"{ran} site(s) produced a coverage"
    if elsewhere:
        detail += f", {elsewhere} stand on ground this terrain does not cover"
    if failed:
        detail += "; " + "; ".join(failed[:2])
    results.add(row["row"], "pipeline", "every site runs as a transmitter",
                PASS if ran and not failed else FAIL, detail,
                known_fail_for(row, "pipeline"))


def _pipeline_links(row, layer, plugin, pipe, manifest, results, timeout):
    """Every P2P link in the layer must run as a real P2P job.

    The receiver's position is not a field on the job: the plugin writes a
    two-line batch CSV (``p2p_tab._write_temp_batch_csv``) and hands it to
    ``build_p2p_job(batch_file=…)``. Using its own writer is the difference
    between testing the contract and inventing one.
    """
    from waveshed.gui.p2p_tab import _write_temp_batch_csv

    terrain, centre = _reference_terrain(manifest, _project_layers(), plugin,
                                         pipe, results, timeout)
    if terrain is None:
        results.add(row["row"], "pipeline", "every link runs as a P2P job", SKIP,
                    "no reference terrain")
        return
    covered = _terrain_bbox(terrain, plugin)
    ran, failed, elsewhere = 0, [], 0
    for index, feature in enumerate(layer.getFeatures()):
        points = feature.geometry().asPolyline()
        if len(points) < 2:
            failed.append("a link with fewer than two endpoints")
            continue
        if not all(_inside(covered, p.y(), p.x()) for p in (points[0], points[-1])):
            elsewhere += 1          # its own ground, and its own row
            continue
        csv_path = _write_temp_batch_csv(points[0].y(), points[0].x(), 30.0, "AGL",
                                         points[-1].y(), points[-1].x(), 2.0, "AGL")
        params = plugin["P2PParams"](
            tx_lat=points[0].y(), tx_lon=points[0].x(), tx_height=30.0,
            tx_mode="AGL", freq_mhz=900.0, erp_watts=10.0, rx_height=2.0,
            rx_mode="AGL", model="ITM", resolution_m=30, max_range_km=60,
            backend="CPU", output_name=f"p2p_{index}", max_ram_gb=8, max_vram_gb=4)
        job = plugin["build_p2p_job"](params, str(terrain), str(pipe.scratch),
                                      batch_file=csv_path)
        job_file = pipe.scratch / f"p2p_{index}_job.json"
        job_file.write_text(json.dumps(job, indent=1), encoding="utf-8")
        code, said = pipe.engine("aether_core", ["--config", job_file], timeout)
        try:
            os.remove(csv_path)
        except OSError:
            pass
        if code != 0:
            failed.append(f"link {index}: {_tail(said, 160)}")
        else:
            ran += 1
    detail = f"{ran} link(s) ran over the reference terrain"
    if elsewhere:
        detail += f", {elsewhere} sit on ground this terrain does not cover"
    if failed:
        detail += "; " + "; ".join(failed[:2])
    results.add(row["row"], "pipeline", "every link runs as a P2P job",
                PASS if ran and not failed else FAIL, detail,
                known_fail_for(row, "pipeline"))


def _terrain_bbox(abt_dir, plugin) -> Dict[str, float]:
    """The ground a terrain directory covers, from the tiles' own headers."""
    abt_mod = plugin["abt"]
    box = None
    for path in abt_mod.list_tiles(str(abt_dir)):
        header = abt_mod.read_header(path)
        if header is None:
            continue
        span = header.pixel_res * header.size
        edges = {"north": header.ul_lat, "south": header.ul_lat - span,
                 "west": header.ul_lon, "east": header.ul_lon + span}
        if box is None:
            box = edges
        else:
            box = {"north": max(box["north"], edges["north"]),
                   "south": min(box["south"], edges["south"]),
                   "west": min(box["west"], edges["west"]),
                   "east": max(box["east"], edges["east"])}
    return box or {"north": 90.0, "south": -90.0, "west": -180.0, "east": 180.0}


def _inside(box: Dict[str, float], lat: float, lon: float) -> bool:
    return (box["south"] <= lat <= box["north"]
            and box["west"] <= lon <= box["east"])


def _pipeline_run_matrix(row, layer, manifest, results):
    """The boxes a human reads must be the boxes tier B plans over."""
    matrix = {name: matrix_bbox(raw)
              for name, raw in (manifest.get("run_matrix") or {}).items()}
    seen = {}
    for feature in layer.getFeatures():
        box = feature.geometry().boundingBox()
        seen[str(feature["name"])] = [round(box.xMinimum(), 6), round(box.yMinimum(), 6),
                                      round(box.xMaximum(), 6), round(box.yMaximum(), 6)]
    missing = sorted(set(matrix) - set(seen))
    extra = sorted(set(seen) - set(matrix))
    moved = [n for n in matrix if n in seen
             and any(abs(a - b) > 1e-6 for a, b in zip(matrix[n], seen[n]))]
    ok = not (missing or extra or moved)
    detail = f"{len(seen)} cases, identical to the manifest tier B plans over"
    if not ok:
        detail = (f"missing {missing}" if missing else "") + \
                 (f" extra {extra}" if extra else "") + \
                 (f" moved {moved}" if moved else "")
    results.add(row["row"], "pipeline", "the run matrix a human reads is the one that runs",
                PASS if ok else FAIL, detail, known_fail_for(row, "pipeline"))


def _pipeline_footprints(row, layer, plugin, manifest, results):
    """Each footprint must be where the raster really is, per the plugin."""
    adapter = plugin["adapter"]
    out_dir = Path(manifest["__manifest_dir__"])
    by_row = {r["row"]: r for r in manifest["rows"]}
    wrong, checked = [], 0
    for feature in layer.getFeatures():
        rid = str(feature["row"])
        row_def = by_row.get(rid)
        if row_def is None:
            wrong.append(f"{rid}: no such row in the manifest")
            continue
        source = normalise_file_source(row_def["source"], out_dir)
        if _VSI.match(source) or "://" in source:
            continue
        try:
            info = adapter.source_file_info(source, row_def.get("crs") or None)
        except Exception as exc:
            wrong.append(f"{rid}: {type(exc).__name__}")
            continue
        real = info["wgs84_bounds"]
        box = feature.geometry().boundingBox()
        drift = max(abs(real["west"] - box.xMinimum()), abs(real["east"] - box.xMaximum()),
                    abs(real["south"] - box.yMinimum()), abs(real["north"] - box.yMaximum()))
        checked += 1
        if drift > 0.01:
            wrong.append(f"{rid}: off by {drift:.4f}°")
    results.add(row["row"], "pipeline", "every footprint is where its raster really is",
                PASS if checked and not wrong else FAIL,
                f"{checked} footprints match the plugin's own bounds"
                + ("; " + "; ".join(wrong[:3]) if wrong else ""),
                known_fail_for(row, "pipeline"))


def _project_layers() -> Dict[str, Any]:
    from qgis.core import QgsProject
    return {l.name(): l for l in QgsProject.instance().mapLayers().values()}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def write_report(results: Results, manifest: Dict[str, Any], out_dir: Path,
                 tiers: str, rows_filter: Sequence[str] = (),
                 cases_filter: Sequence[str] = ()) -> Dict[str, Any]:
    skipped_rows = manifest.get("skipped_rows") or []
    # A row the catalogue deliberately does not ship (4.4a is a Browser
    # connection on purpose) is not a hole in the run. Counting it as one
    # would make every run of this project report "incomplete" forever, which
    # is how an exit code stops being read.
    unbuilt = [e for e in skipped_rows if not e.get("by_design")]
    # A filtered or single-tier run covers a slice of the catalogue, and it
    # overwrites the same results.md a full run wrote. Saying so in the file is
    # what stops "PASSED" being read as "the whole set passed".
    partial = bool(rows_filter or cases_filter) or set(tiers) != {"a", "b", "c"}
    payload = {
        "build": manifest["build"],
        "when": time.strftime("%Y-%m-%d %H:%M:%S"),
        "tiers": tiers,
        "rows_filter": list(rows_filter),
        "cases_filter": list(cases_filter),
        "partial": partial,
        "passed": results.count(PASS),
        "failed": results.count(FAIL),
        "unexpected_passes": results.count(XPASS),
        "known_failures": results.count(XFAIL),
        "skipped": results.count(SKIP),
        "not_run": results.count(NOTRUN),
        "declared": len(results.planned),
        "rows_not_built": unbuilt,
        "rows_excluded_by_design": [e for e in skipped_rows if e.get("by_design")],
        "notes": results.notes,
        "checks": results.entries,
    }
    (out_dir / "results.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")

    headline = (f"**{payload['passed']} passed, {payload['failed']} failed, "
                f"{payload['unexpected_passes']} unexpected passes, "
                f"{payload['known_failures']} known findings, {payload['skipped']} skipped, "
                f"{payload['not_run']} not run**")
    scope = f"Project build: `{manifest['build']}`  ·  tiers: `{tiers}`"
    if rows_filter:
        scope += f"  ·  --rows `{','.join(rows_filter)}`"
    if cases_filter:
        scope += f"  ·  --cases `{','.join(cases_filter)}`"
    lines = [f"# Torture run — {payload['when']}", "", scope, "", headline, ""]
    if payload["failed"] or payload["unexpected_passes"]:
        lines.append("Result: **FAILED**")
    elif payload["skipped"] or payload["not_run"] or skipped_rows:
        lines.append("Result: **INCOMPLETE** — some checks the catalogue declares did not run.")
    elif partial:
        lines.append("Result: **PARTIAL** — everything in this run's scope passed, but the "
                     "run covered only part of the catalogue. This file has replaced whatever "
                     "a full run wrote; re-run without filters for a complete report.")
    else:
        lines.append("Result: **PASSED** — every check the catalogue declares ran and passed.")
    lines.append("")

    def table(title: str, statuses: Sequence[str], intro: str = "") -> None:
        rows = [e for e in results.entries if e["status"] in statuses]
        if not rows:
            return
        lines.extend([f"## {title}", ""])
        if intro:
            lines.extend([intro, ""])
        lines.extend(["| Row | Check | Detail |", "|---|---|---|"])
        for entry in rows:
            detail = entry["detail"].replace("|", r"\|").replace("\n", " ")
            lines.append(f"| {entry['row']} | {entry['check']} | {detail} |")
        lines.append("")

    table("Failures", (FAIL,))
    table("Unexpected passes", (XPASS,),
          "A documented finding that no longer reproduces. Update the catalogue's "
          "`known_fail` note — leaving it in place is how the suite drifts back into fiction.")
    table("Not run", (NOTRUN,),
          "Declared by the manifest and never executed. Each one is a hole in this run.")
    table("Skipped", (SKIP,))
    table("Known findings", (XFAIL,),
          "Real failures the catalogue already documents in a `known_fail` note. Reported, "
          "but they do NOT fail the run: the suite goes red when reality CHANGES, not when "
          "it matches what was already written down. Fix the plugin and each one becomes an "
          "unexpected pass, which does fail the run.")

    if payload["rows_excluded_by_design"]:
        lines += ["## Rows this build deliberately does not ship", "",
                  "Excluded on purpose, so they are not counted against the run.", "",
                  "| Row | Why |", "|---|---|"]
        for entry in payload["rows_excluded_by_design"]:
            lines.append(f"| {entry.get('what', entry.get('row', '?'))} | "
                         f"{str(entry.get('why', '')).replace('|', chr(92) + '|')} |")
        lines.append("")
    if unbuilt:
        lines += ["## Rows the generator could not build", "",
                  "These never reached the project, so nothing above covers them.", "",
                  "| Row | Why |", "|---|---|"]
        for entry in unbuilt:
            lines.append(f"| {entry.get('what', entry.get('row', '?'))} | "
                         f"{str(entry.get('why', '')).replace('|', chr(92) + '|')} |")
        lines.append("")
    if results.notes:
        lines += ["## Notes (information, not checks)", ""] + \
                 [f"* {n}" for n in results.notes] + [""]
    (out_dir / "results.md").write_text("\n".join(lines), encoding="utf-8")
    return payload


def print_summary(results: Results, payload: Dict[str, Any], out_dir: Path) -> None:
    """The end-of-run block: what failed, in full, then the totals.

    A run can print two hundred lines before it finishes, and scrolling back
    through them to find the four that said FAIL is not a reporting strategy.
    Everything that needs acting on is repeated here, at the bottom, where it
    is the last thing on screen.
    """
    def group(status: str) -> List[Dict[str, str]]:
        return [e for e in results.entries if e["status"] == status]

    def block(title: str, entries: Sequence[Dict[str, str]], explain: str) -> None:
        if not entries:
            return
        print(f"\n{title} ({len(entries)})")
        for line in _wrap(explain, 92):
            print(f"  {line}")
        for entry in entries:
            print(f"    {entry['row']:6s} {entry['check']}")
            if entry["detail"]:
                for line in _wrap(entry["detail"], 92):
                    print(f"           {line}")

    print("\n" + "=" * 78)
    block("FAILED", group(FAIL),
          "The expectation did not hold. These are the run's findings.")
    block("UNEXPECTED PASS", group(XPASS),
          "Documented as broken, but it passed — the catalogue's known_fail note is "
          "stale and needs removing.")
    block("NOT RUN", group(NOTRUN),
          "The catalogue declares these and they never executed — holes in this run.")
    block("SKIPPED", group(SKIP),
          "Not applicable, or a prerequisite was missing.")
    block("KNOWN FINDINGS", group(XFAIL),
          "Real failures the catalogue already documents (known_fail). Reported, but "
          "they do NOT fail the run — the suite goes red when reality changes, not "
          "when it matches what was already written down. Fix the plugin and each one "
          "turns into an UNEXPECTED PASS, which does fail the run.")
    if payload.get("rows_not_built"):
        print(f"\nNOT BUILT ({len(payload['rows_not_built'])})")
        print("  Catalogue rows this build could not produce, so nothing above covers them.")
        for entry in payload["rows_not_built"]:
            print(f"    {entry.get('what', '?')}")
            for line in _wrap(str(entry.get("why", "")), 92):
                print(f"           {line}")

    print("\n" + "-" * 78)
    print(f"{payload['passed']} passed, {payload['failed']} failed, "
          f"{payload['unexpected_passes']} unexpected passes, "
          f"{payload['known_failures']} known findings, {payload['skipped']} skipped, "
          f"{payload['not_run']} not run")
    print(f"full report: {out_dir / 'results.md'}")


def _wrap(text: str, width: int) -> List[str]:
    """*text* as lines of at most *width*, broken on spaces."""
    words, lines, current = text.split(), [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        lines.append(current)
    return lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    # `python3 -OO` strips docstrings, and __doc__ is then None.
    parser = argparse.ArgumentParser(
        description=(__doc__ or "Run the torture set automatically.").splitlines()[0])
    parser.add_argument("--manifest", default="", help="path to manifest.json")
    parser.add_argument("--rows", default="", help="comma-separated row prefixes, e.g. 1.,4.3")
    parser.add_argument("--cases", default="",
                        help="comma-separated run-matrix case names (tier B), e.g. Bern,Prague")
    parser.add_argument("--tier", default="abc",
                        help="a (layers), b (engine geometry), c (the whole pipeline); "
                             "any combination, default abc")
    parser.add_argument("--keep-scratch", action="store_true",
                        help="keep each row's pipeline output instead of deleting it")
    parser.add_argument("--timeout", type=int, default=180,
                        help="seconds for one engine subprocess (default 180)")
    parser.add_argument("--allow-incomplete", action="store_true",
                        help="exit 0 even when checks were skipped or never ran")
    parser.add_argument("--allow-missing-plugin", action="store_true",
                        help="treat an unimportable waveshed package as a skip, not a failure")
    parser.add_argument("--port", type=int, default=8000,
                        help="port for the local tile fixture (default 8000)")
    if argv is None and running_inside_qgis():
        # In the console sys.argv belongs to QGIS ("--project ...", the .qgs
        # path, ...). Parsing it would abort with a usage error, which is
        # exactly the "it does nothing" you get from pasting this in.
        argv = []
    args = parser.parse_args(argv)
    # An unrecognised tier used to run nothing at all and report
    # "every check the catalogue declares ran and passed", exit 0 — the exact
    # failure this rewrite exists to remove. "--tier A" was enough to trigger it.
    args.tier = args.tier.strip().lower()
    if not set(args.tier) <= {"a", "b", "c"} or not args.tier:
        print(f"[error] --tier must be any combination of a, b, c (got {args.tier!r})")
        return 2

    manifest_path = find_manifest(args.manifest)
    if manifest_path is None:
        print("[error] manifest.json not found. Open the torture project first, or pass "
              "--manifest <path>, or run tools/make_torture_project.py to create it.")
        return 2
    for folder in (REPO, Path(__file__).resolve().parent if REPO else None):
        if folder is not None and str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Resolve everything against the manifest, not against this file: in the
    # console the two can live on different drives.
    out_dir = manifest_path.parent
    manifest["__manifest_dir__"] = str(out_dir)
    only = [r.strip() for r in args.rows.split(",") if r.strip()]
    cases = [c.strip() for c in args.cases.split(",") if c.strip()]

    print(f"torture runner — project build {manifest['build']}, {len(manifest['rows'])} rows")
    results = Results()
    for entry in manifest.get("skipped_rows") or []:
        mark = "excluded" if entry.get("by_design") else "NOT BUILT"
        print(f"  [{mark}] {entry.get('what', '?')} — {entry.get('why', '')}")

    app = None
    server = serve_fixture(out_dir, args.port)
    if server is not None:
        print(f"serving the local tile fixture on 127.0.0.1:{args.port}")
    try:
        if "a" in args.tier:
            print("\n== tier A: layers, plugin verdicts, tiles, rendering ==")
            app = start_qgis()
            _check_fixture_server(manifest, results, out_dir, args.port)
            tier_a(manifest, results, only, args.allow_missing_plugin)
        if "b" in args.tier:
            print("\n== tier B: engine geometry ==")
            if app is None and not running_inside_qgis():
                app = start_qgis()
            tier_b(manifest, results, only, cases, args.timeout)
        if "c" in args.tier:
            print("\n== tier C: the whole pipeline, one clean cache per row ==")
            if app is None and not running_inside_qgis():
                app = start_qgis()
            tier_pipeline(manifest, results, only, args.timeout, args.keep_scratch)
    except Exception as exc:
        # Not swallowed — recorded as a failing check, so the report is still
        # written and everything tier A already established survives. This used
        # to escape past write_report, leaving CI with a traceback and no
        # results.json at all.
        import traceback
        traceback.print_exc()
        results.add("-", "run.crashed", "the run reached its end", FAIL,
                    f"{type(exc).__name__}: {exc}")
    finally:
        results.finish()
        if app is not None:
            app.exitQgis()          # only when we created it
        if server is not None:
            server.shutdown()

    payload = write_report(results, manifest, out_dir, args.tier,
                           only, cases)
    print_summary(results, payload, out_dir)
    if payload["failed"] or payload["unexpected_passes"]:
        return 1
    if not results.entries:
        print("[error] the run made no checks at all. That is never a pass.")
        return 2
    incomplete = payload["skipped"] or payload["not_run"] or payload["rows_not_built"]
    if incomplete and not args.allow_incomplete:
        print("[incomplete] the run did not cover everything the catalogue declares. "
              "Pass --allow-incomplete to accept that as success.")
        return 2
    return 0


def _check_fixture_server(manifest: Dict[str, Any], results: Results,
                          out_dir: Path, port: int) -> None:
    """Prove the local rows will be fed by THIS build's fixture."""
    local_rows = [r for r in manifest["rows"]
                  if "localhost" in (r.get("source") or "")
                  or "127.0.0.1" in (r.get("source") or "")]
    if not local_rows:
        return
    results.plan("-", "fixture.server")
    base = xyz_template(local_rows[0]["source"]) or f"http://127.0.0.1:{port}/"
    ok, detail = fixture_server_serves(out_dir, base)
    results.add("-", "fixture.server", "the local fixture server is this build's",
                PASS if ok else FAIL, detail)


def run(*rows: str, tier: str = "ab", **kwargs):
    """Callable entry point, for when you want to re-run it from the console."""
    argv = ["--tier", tier]
    if rows:
        argv += ["--rows", ",".join(rows)]
    for key, value in kwargs.items():
        flag = "--" + key.replace("_", "-")
        argv += [flag] if value is True else [flag, str(value)]
    return main(argv)


# The standard idiom, plus the name the QGIS Python Console actually uses.
# From QGIS's own source: console_sci.py builds its interpreter with
# code.InteractiveInterpreter(locals=None), and the stdlib documents that as a
# namespace with __name__ == "__console__" — while runFile() sets __file__
# before exec'ing the script. So a guard on __main__ never fires there, and one
# on "__file__ not in globals()" never fires either: pressing Run did nothing,
# silently. "__console__" is the one true signal, so it is the one used here.
if __name__ in ("__main__", "__console__"):
    _status = main()
    if __name__ == "__console__":
        print(f"[torture runner] finished, exit status {_status}. "
              f"Call run() to repeat, or run('1.', '4.3') for selected rows.")
    else:
        raise SystemExit(_status)
